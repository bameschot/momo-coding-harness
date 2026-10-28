"""Loop detection for the tool loop: the model making the same call again while
nothing has changed, getting the same error from call after call, or editing
one definition over and over while its check keeps failing.

A call's signature is its name and canonical arguments, plus an epoch that
moves only when the world changed: a write not made before, a compaction, an
answer from the user.  Reading the same file three times with no write in
between is a loop; reading it after each of three different edits is not.  A
write's signature leaves the epoch out, so the same write made again is
counted even though its first run moved the epoch.

The error trigger catches thrashing the signature misses: three different
edit_file calls that all fail with "old_string not found", or three different
commands that all exit with the same exception.  A command counts as failed
when it exits non-zero and its output names an error (a non-zero exit with no
error line, like grep finding nothing, is not counted); the error is its last
such line.  A command that succeeds clears only the errors of commands like it:
a probe that runs fine says nothing about the check that failed.  Exit code 0
is not taken as success when a pipe stage the harness did not strip may have
replaced the command's own (`check.py | sed -n 40,60p` exits with sed's 0):
such a run counts as failed when its output names an error, and as unknown
otherwise; so does an exit 0 whose output names one.  A command running a
scratchpad file is the same check only while that file is unchanged: a probe
rewritten between runs is a new check, so its success clears nothing the old
version failed with (`python3 dbg.py` kept clearing a handshake error the
model had been stuck on for 45 minutes).  Project files are not versioned
this way, since fixing the code a command runs is how its check gets to pass.

The churn trigger catches edit, run, fail, edit again: each new write moves the
epoch, so the signature never repeats, and each failure may name a different
error.  An edit to a definition followed by a failing command is one cycle;
after n cycles on one definition the model gets a nudge, and the next cycle is
a loop.  A command like a failing one succeeding ends the churn.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field

from .momo_dir import SCRATCH_REL
from .tools import split_view_pipe

THRESHOLD = 3     # occurrences of one signature (or one error, or churn cycles) that make a loop
MAX_RESETS = 2    # rewinds per _run_loop before the turn is stopped
# /loop-guard bounds: 1 would call every call a loop.
MIN_THRESHOLD, MAX_THRESHOLD = 2, 20
MAX_RESETS_LIMIT = 10
SIMILAR = 0.8     # token Jaccard at which two commands count as the same check

# Calls that drive the user or the plan, not the loop.
_EXEMPT = frozenset({"ask_user", "ask_choice", "complete_step", "create_plan", "revise_plan"})

_RX_QUOTED = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"|`[^`\n]*`")
_RX_PATH = re.compile(r"\S*/\S*")
_RX_NUMBER = re.compile(r"\d+")
# A line of command output that names a failure: an exception, a failed test,
# a compiler error, a panic.  Stricter than run_store's trouble lines, which
# also take warnings and the word "error" anywhere.
_RX_FAILURE = re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b|\bFAIL(?:ED|URE)?\b|\berror\b[\[:]"
                         r"|\bError:|\berror TS\d+|\bpanic(?:ked)?\b|\bfatal\b")
# Where an edit landed, as editing._report names it: "(inside function f, L3-9)".
_RX_OWNER = re.compile(r"\(inside \w+ ([\w.]+), L\d+-\d+\)")
_RX_TOKEN = re.compile(r"[A-Za-z_]\w*|[^\w\s]")


def call_key(name: str, args) -> str:
    """The canonical form of a call's arguments, as _annotate_result keys it."""
    return json.dumps(args, sort_keys=True, default=str)


def _normalise(line: str) -> str:
    line = _RX_QUOTED.sub("Q", line)
    line = _RX_PATH.sub("P", line)
    return _RX_NUMBER.sub("N", line)


def error_key(result: str) -> str:
    """An error result's first line with the parts that vary between attempts
    (quoted text, paths, numbers) collapsed, so the same failure compares equal."""
    line = result.strip().splitlines()[0] if result.strip() else ""
    return _normalise(line)


def failure_line(result: str) -> str:
    """The last line of a command's output that names a failure ("" if none),
    skipping the harness's own [bracketed] notes around the output."""
    for line in reversed(result.splitlines()):
        line = line.strip()
        if line and not line.startswith("[") and _RX_FAILURE.search(line):
            return line
    return ""


def command_tokens(command: str) -> frozenset[str]:
    """A command as the similarity check sees it: its words and symbols, numbers
    collapsed, without a trailing view pipe (| tail -N) the harness strips anyway."""
    base = split_view_pipe(command)[0]
    return frozenset(_RX_TOKEN.findall(_RX_NUMBER.sub("N", base)))


def pipe_masks_exit(command: str) -> bool:
    """Whether the exit code of command may be a later pipe stage's rather than
    its own: a pipe left after the harness strips its view pipe (| tail -N, which
    it runs without), and no pipefail."""
    base = split_view_pipe(command)[0]
    if "pipefail" in base:
        return False
    try:
        lexer = shlex.shlex(base, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return any(tok in ("|", "|&") for tok in lexer)
    except ValueError:                      # unbalanced quotes: guess from the text
        return bool(re.search(r"(?<![|])\|(?![|])", base))


def similar(a: frozenset[str], b: frozenset[str]) -> bool:
    return len(a & b) >= SIMILAR * max(1, len(a | b))


# A command as a check: its tokens, and the versions of the scratchpad files it
# names (how many times each was written when it ran).
Check = tuple[frozenset[str], frozenset[tuple[str, int]]]


def same_check(a: Check, b: Check) -> bool:
    return a[1] == b[1] and similar(a[0], b[0])


def scratch_path(name: str, args, result: str) -> str:
    """The scratchpad file a successful write made, relative to the scratchpad
    ("" if it made none): scratch_write's path, or a write into .momo/scratch/."""
    if not isinstance(args, dict) or not isinstance(args.get("path"), str):
        return ""
    path = args["path"].strip().removeprefix("./")
    if name == "scratch_write":
        if not result.startswith(("Written:", "Appended to")):
            return ""
        return path.removeprefix(SCRATCH_REL + "/")
    return path.removeprefix(SCRATCH_REL + "/") if path.startswith(SCRATCH_REL + "/") else ""


def names_file(command: str, path: str) -> bool:
    """Whether command names the scratchpad file path (as .momo/scratch/path,
    $MOMO_SCRATCH/path, or path from inside the scratchpad)."""
    return re.search(r"(?<![\w-])" + re.escape(path) + r"(?![\w-])", command) is not None


@dataclass
class Hit:
    kind: str                 # "call": one call repeated; "error": one error repeated;
                              # "churn": one definition edited while its check fails
    name: str
    args: object
    count: int
    error: str = ""           # the repeated error's first line (kind "error"), the last failure (churn)
    result: str = ""          # the latest result, for the fallback analysis
    # The assistant messages of each occurrence, oldest first (by identity): the
    # rewind cuts back to the earliest of them still in the history.
    msgs: list = field(default_factory=list)
    severity: str = "rewind"  # "rewind": cut the loop out; "nudge": a note, history kept
    owner: str = ""           # churn: the definition, "path qualname" (or the path)

    def describe(self) -> str:
        if self.kind == "error":
            return f"{self.name} failing with \"{self.error}\""
        if self.kind == "churn":
            return f"editing {self.owner} while the check still fails with \"{self.error}\""
        return f"{self.name} {call_key(self.name, self.args)}"


class LoopGuard:
    def __init__(self, threshold: int = THRESHOLD):
        self.threshold = threshold       # 0: off, nothing is a loop
        self.reset()

    def reset(self) -> None:
        """Forget everything: a new _run_loop, or the history was rewound."""
        self.epoch = 0
        self._writes: set[tuple[str, str]] = set()
        self._calls: dict[tuple, list] = {}
        self._errors: dict[tuple[str, str], list] = {}
        self._error_cmds: dict[tuple[str, str], list[Check]] = {}
        # Churn: the definitions edited since the last command ran, and per
        # definition its cycles: (edit messages, the failing check, its failure).
        self._pending: dict[tuple[str, str], list] = {}
        self._churn: dict[tuple[str, str], list[tuple[list, Check, str]]] = {}
        self._nudged: set[tuple[str, str]] = set()
        self._versions: dict[str, int] = {}     # scratchpad file -> times written

    def _check(self, command: str) -> Check:
        return (command_tokens(command),
                frozenset((p, v) for p, v in self._versions.items() if names_file(command, p)))

    def bump(self) -> None:
        """The world changed: the same call made from here on may answer differently."""
        self.epoch += 1

    def record(self, name: str, args, result: str, assistant_msg: dict,
               mutating: bool, exit_code: int | None = None) -> Hit | None:
        """Count one executed call; a Hit when it makes a loop (or, severity
        "nudge", when it is about to).  exit_code: a run_command's real exit code."""
        if not self.threshold or name in _EXEMPT:
            return None
        failed = result.startswith("ERROR")
        first_line = result.strip().splitlines()[0] if result.strip() else ""
        if failed and "declined" in first_line:
            return None             # the user said no: repeating it is their call
        command = name == "run_command" and isinstance(args, dict)
        check: Check = (self._check(str(args.get("command") or "")) if command
                        else (frozenset(), frozenset()))
        scratch = scratch_path(name, args, result)
        if scratch:
            self._versions[scratch] = self._versions.get(scratch, 0) + 1
            mutating = True         # a rewritten probe is a change: running it again is not a repeat
        error = first_line
        if not failed and command and exit_code:
            error = failure_line(result)
            failed = True
        elif not failed and command and exit_code == 0:
            shown = failure_line(result)
            if shown and pipe_masks_exit(str(args.get("command") or "")):
                error, failed = shown, True     # the pipe's 0 hid a failure
            elif shown or pipe_masks_exit(str(args.get("command") or "")):
                exit_code = None                # no proof it worked: clears nothing
        key = call_key(name, args)
        sig = (name, key) if mutating else (name, key, self.epoch)
        if mutating and not failed and (name, key) not in self._writes:
            self._writes.add((name, key))
            self.bump()
        if mutating and not failed and not scratch:
            for owner in _owners(name, args, result):
                self._pending.setdefault(owner, []).append(assistant_msg)

        seen = self._calls.setdefault(sig, [])
        seen.append(assistant_msg)
        if len(seen) >= self.threshold:
            return Hit("call", name, args, len(seen), result=result, msgs=_unique(seen))

        if command and (failed or exit_code == 0):
            churn = self._command_ran(check, error if failed else None, assistant_msg)
        else:
            churn = None
        if not failed:
            if not command:
                # This tool works again: its earlier errors were not a dead end.
                self._clear_errors(name)
            elif exit_code == 0:
                self._clear_errors(name, check)
            return churn
        if not error:
            return churn            # a non-zero exit that names no error: not counted
        ekey = (name, _normalise(error) if command else error_key(result))
        errs = self._errors.setdefault(ekey, [])
        errs.append(assistant_msg)
        self._error_cmds.setdefault(ekey, []).append(check)
        if len(errs) >= self.threshold:
            return Hit("error", name, args, len(errs), error=error[:200],
                       result=result, msgs=_unique(errs))
        return churn

    def _clear_errors(self, name: str, check: Check | None = None) -> None:
        """Drop name's errors; with check (a command that succeeded) only those
        the same check failed with."""
        for k in [k for k in self._errors if k[0] == name and (
                check is None or any(same_check(check, c) for c in self._error_cmds.get(k, ())))]:
            del self._errors[k]
            self._error_cmds.pop(k, None)

    def _command_ran(self, check: Check, error: str | None,
                     msg: dict) -> Hit | None:
        """A command ran after the edits in _pending: with error (a failure that
        names one) each edited definition gets a churn cycle; a success like one
        of a definition's failing commands ends its churn.  A failure that names
        no error is not a cycle (error "")."""
        pending, self._pending = self._pending, {}
        if error is None:
            for owner in [o for o, cycles in self._churn.items()
                          if any(same_check(check, c[1]) for c in cycles)]:
                del self._churn[owner]
                self._nudged.discard(owner)
            return None
        if not error:
            return None
        hit = None
        for owner, edits in pending.items():
            cycles = self._churn.setdefault(owner, [])
            cycles.append((edits + [msg], check, error))
            if len(cycles) < self.threshold or hit is not None:
                continue
            msgs = _unique([m for c in cycles for m in c[0]])
            where = f"{owner[1]} in {owner[0]}" if owner[1] else owner[0]
            severity = "rewind" if owner in self._nudged else "nudge"
            self._nudged.add(owner)
            hit = Hit("churn", "edit", None, len(cycles), error=error[:200],
                      msgs=msgs, severity=severity, owner=where)
        return hit


def _owners(name: str, args, result: str) -> list[tuple[str, str]]:
    """The definitions a successful write changed: (path, qualname) from the
    result's "(inside … , L…)", else (path, "") for each path it wrote."""
    if not isinstance(args, dict):
        return []
    if name == "edit_files":
        from .tools import edit_paths
        return [(p, "") for p in dict.fromkeys(edit_paths(args))]
    path = args.get("dst") if name == "move_file" else args.get("path")
    if not isinstance(path, str):
        return []
    m = _RX_OWNER.search(result)
    if m:
        return [(path, m.group(1))]
    if name == "replace_symbol" and isinstance(args.get("name"), str):
        return [(path, args["name"])]
    return [(path, "")]


def _unique(msgs: list) -> list:
    """The messages in order, each once (a batch can hold several occurrences)."""
    out: list = []
    for m in msgs:
        if not any(m is o for o in out):
            out.append(m)
    return out
