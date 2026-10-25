"""Running shell commands: what run_command, command_output and stop_command
share, and what the TDD gate (tdd.py) runs its test command with."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from .. import momo_dir
from .. import run_store as run_store_mod

MAX_COMMAND_TIMEOUT = 900   # 15 minutes — upper bound for a single run_command
# Without timeout= a command gets this long.  The model never passes timeout=, so a
# default at the ceiling let its own infinite loop block a turn for 15 minutes
# (edit evals, 2026-10-07); a build that needs longer is told how to ask for it.
DEFAULT_COMMAND_TIMEOUT = 120

MAX_OUTPUT_BYTES = 64 * 2**20      # a command writing more than this is stopped
OUTPUT_READ_BYTES = 4 * 2**20      # per stream, the tail read back into the result
STOPPED_TAIL_CHARS = 4000          # output shown after a timeout / interrupt


# Web clients a model reaches for when fetch_url is missing (i.e. /net is off).
# Matched in command position only — at the start, or after ; & | ( $( ` or a
# newline, optionally behind sudo/env/time/exec/command/nohup — so `grep -rn curl .`
# and `echo "curl"` still run.  This steers the model; it is NOT a sandbox:
# `python -c` with urllib, mvn, or `bash -c "curl ..."` still reach the network.
_WEB_CLIENT_RE = re.compile(
    r"(?:^|[;&|(`\n]|\$\()\s*"
    r"(?:(?:sudo|time|exec|command|nohup)\s+|env\s+(?:\S+=\S*\s+)*)*"
    r"(?:\S*/)?(curl|wget)(?=\s|$|[;&|)`])"
)


def web_client_in(command: str) -> str | None:
    m = _WEB_CLIENT_RE.search(command)
    return m.group(1) if m else None


def web_refusal(command: str, net_access: str) -> tuple[str | None, str]:
    """(refusal, note): steer page reads and API calls to fetch_url; downloads to
    disk still need curl/wget.  With /net off, running curl would quietly bypass
    the user's setting (observed: a 9B model with no fetch_url fell back to 50
    curl calls over two sessions), so refuse and say what to do instead."""
    client = web_client_in(command)
    if not client:
        return None, ""
    if net_access == "off":
        return (f"ERROR: this command was not run: it uses {client} to reach the "
                "internet, and internet access is off. Do not retry with another "
                "command or script. Tell the user: \"Internet access is off. Run "
                "/net on and ask again.\" Turning it on gives you the fetch_url tool "
                "and lets curl and wget run."), ""
    return None, (f"(note: to read a page or call an API, use the fetch_url tool instead "
                  f"of {client} — it returns cleaner text. {client} is fine for "
                  "downloading files to disk.)\n")


def clean_output(text: str) -> str:
    """CRLF as LF, and a line a progress bar redrew with \r as its last frame
    only — pip / npm / curl bars otherwise cost thousands of tokens."""
    lines = []
    for ln in text.replace("\r\n", "\n").split("\n"):
        if "\r" in ln:
            frames = [f for f in ln.split("\r") if f]
            ln = frames[-1] if frames else ""
        lines.append(ln)
    return "\n".join(lines).strip("\n").rstrip()


def output_tail(f) -> str:
    """The text a command wrote to temp file f: its last OUTPUT_READ_BYTES."""
    size = f.seek(0, os.SEEK_END)
    skip = max(0, size - OUTPUT_READ_BYTES)
    f.seek(skip)
    text = clean_output(f.read().decode("utf-8", errors="replace"))
    return f"[… first {skip:,} bytes of output not shown …]\n{text}" if skip else text


def clamp_timeout(timeout) -> int:
    """Clamp to the 15-minute ceiling; missing or invalid values get the
    default, so a hung command can never block the worker thread for long."""
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        return DEFAULT_COMMAND_TIMEOUT
    if timeout <= 0:
        return DEFAULT_COMMAND_TIMEOUT
    return min(timeout, MAX_COMMAND_TIMEOUT)


def longer(stopped: str, defaulted: bool) -> str:
    """For a command the default timeout stopped: how to give it longer."""
    if not (defaulted and stopped.startswith("command timed out")):
        return ""
    return (f"\n(it ran with the default {DEFAULT_COMMAND_TIMEOUT}s limit — if it really "
            f"needs longer, e.g. a full build or test suite, run it again with timeout= up to "
            f"{MAX_COMMAND_TIMEOUT}; a quick check that hangs is stuck, not slow)")


def command_env(workdir: Path) -> dict:
    """The environment of a run_command: PYTHONUNBUFFERED (a background
    command's "ready" line must not sit in a buffer) and MOMO_SCRATCH, the
    scratchpad's absolute path (created, so a script can write there)."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    folder = momo_dir.ensure(workdir, momo_dir.SCRATCH)
    if not isinstance(folder, str):
        env["MOMO_SCRATCH"] = str(folder)
    return env


def exec_command(command: str, timeout: int, workdir: Path, cancel, out, err):
    """Run command with its output going to the open files out and err (err may
    be subprocess.STDOUT).  Returns (proc, stopped) — stopped says why it was
    killed, None when it exited — or (None, error text) when it cannot start.

    No stdin: a command that reads it (git commit without -m, a REPL) would
    otherwise fight the TUI for the terminal.  Output goes to files, not pipes:
    memory stays bounded however much it writes, a grandchild holding the output
    open cannot keep the call waiting, and what was written before a timeout is
    still there to show.  Its own session, so a timeout or Esc kills what it
    started too.  A normal exit leaves background processes alone: build daemons
    (Gradle, mvnd, Kotlin) outlive the command that started them, and killing
    them would cost every later build a cold JVM."""
    try:
        proc = subprocess.Popen(command, shell=True, cwd=workdir, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err, start_new_session=True,
                                env=command_env(workdir))
    except OSError as e:
        return None, f"ERROR: {e}"
    files = [f for f in (out, err) if hasattr(f, "fileno")]
    deadline = time.monotonic() + timeout
    while True:
        try:
            proc.wait(timeout=0.25)
            return proc, None
        except subprocess.TimeoutExpired:
            pass
        stopped = None
        if cancel is not None and cancel.is_set():
            stopped = "command interrupted by the user"
        elif time.monotonic() >= deadline:
            stopped = f"command timed out after {timeout}s"
        elif sum(os.fstat(f.fileno()).st_size for f in files) > MAX_OUTPUT_BYTES:
            stopped = (f"command stopped: it wrote more than "
                       f"{MAX_OUTPUT_BYTES // 2**20} MB of output")
        if stopped:
            run_store_mod.kill_group(proc)
            return proc, stopped


# A trailing "| tail -N" makes the exit code tail's own, which is always 0, so a
# failing test run looks like a passing one.  The 9B pipes most of its test runs
# this way, the tool description notwithstanding, sometimes adding
# '; echo "exit=${PIPESTATUS[0]}"' to get the code back.  "| grep …" and
# "| head -N" do the same (a plan run piped 11 of 14 suite runs through them and
# was told "exit code 0" on failing suites), and each new slice meant running the
# suite again.  Such a pipe is taken off and becomes a view of the output: the
# same lines, the real exit code, and the whole output kept in the run log.
# "tail - 20" (a 9B typo: to the shell that is stdin and a file named 20) counts too.
_TAIL_PIPE = re.compile(
    r"\s*(?:2>&1\s*)?\|&?\s*tail\s+(?:-n\s*(\d+)|-\s?(\d+)|--lines[= ](\d+))\s*"
    r"(?:;\s*echo\b[^;|&]*\$\{?PIPESTATUS\[0\]\}?[^;|&]*)?\s*$")
_HEAD_N = r"head\s+(?:-n\s*(\d+)|-\s?(\d+)|--lines[= ](\d+))"
_HEAD_PIPE = re.compile(r"\s*(?:2>&1\s*)?\|&?\s*" + _HEAD_N + r"\s*$")
# grep with only the flags a view can honour (-E/-i/-n need nothing; -A/-B/-C
# become context), one pattern, and optionally a head after it, which is
# dropped: the view is windowed anyway.  A double-quoted pattern with $ or `
# would expand in the shell, so it is left alone.
_GREP_PIPE = re.compile(
    r"\s*(?:2>&1\s*)?\|&?\s*grep((?:\s+(?:-[Ein]+|-[ABC]\s*\d+|--extended-regexp))*)"
    r"(?:\s+-e)?\s+('[^']*'|\"(?:[^\"\\$`]|\\.)*\"|[^\s'\"|;&<>()$`\\-][^\s'\"|;&<>()$`\\]*)"
    r"(?:\s*\|\s*" + _HEAD_N + r")?\s*$")


def split_view_pipe(command: str) -> tuple[str, dict | None, str]:
    """(command without a trailing tail/grep/head pipe, the view it asked for, the
    text taken off); (command, None, '') when it does not end in one.  view is
    {"tail": N}, {"head": N} or {"grep": pattern, "context": N}."""
    for rx in (_TAIL_PIPE, _GREP_PIPE, _HEAD_PIPE):     # grep before head: "grep x | head -5"
        m = rx.search(command)
        if m:
            break
    else:
        return command, None, ""
    base = command[:m.start()].rstrip()
    try:
        if not shlex.split(base):     # nothing before the pipe
            return command, None, ""
    except ValueError:                # the "pipe" sits inside an open quote
        return command, None, ""
    if rx is not _GREP_PIPE:
        n = int(next(g for g in m.groups() if g))
        return base, {"tail" if rx is _TAIL_PIPE else "head": n}, m.group(0).strip()
    flags, pattern = m.group(1), shlex.split(m.group(2))[0]
    if not ("E" in flags or "extended" in flags) and re.search(r"\\[(){}+?]", pattern):
        return command, None, ""      # basic-regex groups mean something else in Python
    context = max((int(c) for c in re.findall(r"-[ABC]\s*(\d+)", flags)), default=0)
    return base, {"grep": pattern, "context": context}, m.group(0).strip()


# ── saved logs (/run-mode new) ───────────────────────────────────────────────

def read_log(store: "run_store_mod.RunStore", path: Path) -> str:
    """A saved log as the model sees it: cleaned the same way every time, so the
    line numbers of one view hold for the next."""
    return store.read(path, clean_output)


def log_view(text: str, limit: int, tail, grep, context, start_line=None, end_line=None,
             how: str = "", compact: str | None = None):
    """(view, hint): the requested part of a log, and whether the footer should
    point at trouble lines a default view had to leave out.  how: what the note
    on a cut default view says to pass for the rest.  compact: the run id, to
    collapse redundant lines of a default view (run_store.compact) — only when
    the result fits limit whole, since a cut view numbers its lines."""
    if not (grep or tail or start_line is not None or end_line is not None):
        if compact:
            short = run_store_mod.compact(text, compact)
            if short is not text and (not limit or len(short) <= limit):
                return short, False
        return run_store_mod.default_view(text, limit, how)
    return run_store_mod.view(text, limit, tail_n=tail, pattern=grep, context=context or 0,
                              start_line=start_line, end_line=end_line), False


def no_log(path: str, store: "run_store_mod.RunStore") -> str:
    logs = store.logs()
    have = ("Saved logs: " + "; ".join(store.describe(p) for p in logs[-5:])) if logs \
        else "No command output is saved in this session — run the command with run_command."
    return (f"ERROR: no saved command output at {path!r}. Pass the path printed at the "
            f"end of a run_command result. {have}")


def new_run_mode(ctx) -> bool:
    """windows_itself for the run tools: in /run-mode new they return a view
    of a saved log, so the generic /tool-result cut must leave them alone."""
    return ctx.run_mode == "new"
