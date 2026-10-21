"""The test gate of a TDD plan.

Every stage of a TDD plan ends at a gate the harness checks itself: the pending
tests are found by the tag in their skip reason (``tdd-step-3``,
``tdd-acceptance``), and the plan's test command must exit 0.  A green suite
with the later steps' tests still skipped is what shows those tests do not
break the build."""
from __future__ import annotations

import difflib
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from .paths import BINARY_SNIFF_BYTES, MAX_SCAN_FILE_BYTES, split_lines, walk_files
from .plan import LEGACY_PLAN_FILENAME, Plan
from . import tools

TAG_RE = re.compile(r"\btdd-(?:step-\d+|acceptance)\b")

# Not part of the project: momo's own state in the workdir.  Its run logs hold
# test output, and test output prints the skip reasons.
_SKIP_TOP = {LEGACY_PLAN_FILENAME, ".momo"}

_OUTPUT_CHARS = 4000   # test output shown with a gate result (the end of it)
_MAX_SITES = 8         # file:line locations listed per tag

# A marker line holds a skip marker and nothing else: a decorator or attribute
# above the test (@unittest.skip("…"), @Disabled("…"), #[ignore = "…"]) or a
# skip call inside it (self.skipTest("…"), t.Skip("…")).  A line that also
# opens the test — it.skip("… (tdd-step-1)", () => { — is left to the model.
_SKIP_WORD = re.compile(r"(?i)skip|disabled|ignore")
_OPENS_BLOCK = re.compile(r"[{:]\s*$|=>|\b(?:def|fn|func|function|class)\b")
_ABOVE = ("@", "#[")
# Body lines that do not test anything.
_TRIVIAL = re.compile(r"^(?:pass|\.\.\.|[{}();,]*|(?:#|//).*)$")
# A line that checks something; deleting one during a step weakens a test.
_ASSERT_RE = re.compile(r"(?i)\b(?:assert\w*|expect\w*|should|require\w*|check\w*)\b"
                        r"|\bt\.(?:Error|Fatal)")
_MAX_NOTES = 12        # changed test lines reported to the user per gate


def find_tags(workdir: Path) -> dict[str, list[str]]:
    """tag → ['path:line', …] for every TDD tag in the project's text files."""
    root = workdir.resolve()
    found: dict[str, list[str]] = {}
    for path in walk_files(root):
        rel = path.relative_to(root)
        if rel.parts[0] in _SKIP_TOP:
            continue
        try:
            if path.stat().st_size > MAX_SCAN_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:BINARY_SNIFF_BYTES] or b"tdd-" not in data:
            continue
        text = data.decode("utf-8", errors="replace")
        for n, line in enumerate(split_lines(text), 1):
            for tag in set(TAG_RE.findall(line)):
                found.setdefault(tag, []).append(f"{rel.as_posix()}:{n}")
    return found


def _is_marker(line: str) -> bool:
    return bool(TAG_RE.search(line) and _SKIP_WORD.search(line)
                and not _OPENS_BLOCK.search(line.strip()))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _opens_skipped_test(line: str) -> bool:
    """A tagged line that skips and opens the test: it.skip("… (tdd-step-1)", () => {"""
    return bool(TAG_RE.search(line) and _SKIP_WORD.search(line) and line.rstrip().endswith("{"))


def _past_decorators(lines: list[str], j: int) -> int:
    while j < len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith(_ABOVE)):
        j += 1
    return j


def _body_empty(lines: list[str], j: int, ind: int) -> bool:
    """Whether lines[j:] up to the first line indented less than ind hold
    nothing but blank lines, comments, pass, ... or braces."""
    for line in lines[j:]:
        if not line.strip():
            continue
        if _indent(line) < ind:
            break
        if not _TRIVIAL.match(line.strip()):
            return False
    return True


def _is_hollow(lines: list[str], i: int) -> bool:
    """Whether the test tagged at lines[i] has no real body.  Judged by
    indentation, which every language here lays tests out by."""
    if _opens_skipped_test(lines[i]):
        return _body_empty(lines, i + 1, _indent(lines[i]) + 1)
    if not _is_marker(lines[i]):
        return False
    if not lines[i].lstrip().startswith(_ABOVE):
        return _body_empty(lines, i + 1, _indent(lines[i]))
    j = _past_decorators(lines, i + 1)      # to the test's header line
    if j >= len(lines):
        return True
    if not lines[j].rstrip().endswith(("{", ":")):
        return False        # a one-line test: not worth guessing about
    return _body_empty(lines, j + 1, _indent(lines[j]) + 1)


def _tagged_files(workdir: Path, sites: list[str]) -> dict[Path, list[int]]:
    """'path:line' sites grouped by file (0-based line indexes)."""
    out: dict[Path, list[int]] = {}
    for site in sites:
        rel, _, n = site.rpartition(":")
        out.setdefault(workdir / rel, []).append(int(n) - 1)
    return out


def hollow_sites(workdir: Path, found: dict[str, list[str]]) -> list[str]:
    """'path:line' of every skip marker whose test has no body."""
    hollow = []
    sites = [site for v in found.values() for site in v]
    for path, idxs in _tagged_files(workdir, sites).items():
        try:
            lines = split_lines(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        rel = path.relative_to(workdir).as_posix()
        hollow += [f"{rel}:{i + 1}" for i in sorted(set(idxs))
                   if i < len(lines) and _is_hollow(lines, i)]
    return hollow


# ── which test belongs to which step ─────────────────────────────────────────

def _key(line: str) -> str:
    """A test's opening line as words, without its tag and skip, so it still
    matches once the skip is gone: it.skip("adds (tdd-step-1)", () => { and
    it("adds", () => { are both 'it adds'."""
    text = re.sub(r"\.skip\b|\bx(?=(?:it|describe|test)\b)", "", TAG_RE.sub("", line))
    return " ".join(re.findall(r"\w+", text))


def _header(lines: list[str], i: int) -> int | None:
    """Index of the opening line of the test tagged at lines[i]."""
    if _opens_skipped_test(lines[i]):
        return i
    if lines[i].lstrip().startswith(_ABOVE):
        j = _past_decorators(lines, i + 1)
        return j if j < len(lines) else None
    ind = _indent(lines[i])        # a skip call inside the test: its enclosing line
    return next((j for j in range(i - 1, -1, -1)
                 if lines[j].strip() and _indent(lines[j]) < ind), None)


def test_owners(workdir: Path, found: dict[str, list[str]]) -> dict[str, dict[str, str]]:
    """{path: {test key: tag}} for every tagged test, taken when the tests are
    written, so a later change can be traced to the step whose test it is."""
    owners: dict[str, dict[str, str]] = {}
    for tag, sites in found.items():
        for path, idxs in _tagged_files(workdir, sites).items():
            try:
                lines = split_lines(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            rel = path.relative_to(workdir).as_posix()
            for i in idxs:
                h = _header(lines, i) if i < len(lines) else None
                if h is not None and _key(lines[h]):
                    owners.setdefault(rel, {})[_key(lines[h])] = tag
    return owners


def snapshot(workdir: Path, owners: dict[str, dict[str, str]]) -> dict[str, str]:
    """The text of every file holding tagged tests, to audit a step against."""
    out = {}
    for rel in owners:
        try:
            out[rel] = (workdir / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return out


def _owner(lines: list[str], i: int, heads: dict[str, str]) -> str | None:
    """The tag of the test that lines[i] belongs to (the line itself, or the
    nearest less-indented test opening above it)."""
    if _key(lines[i]) in heads:
        return heads[_key(lines[i])]
    ind = _indent(lines[i])
    for j in range(i - 1, -1, -1):
        if lines[j].strip() and _indent(lines[j]) < ind:
            if _key(lines[j]) in heads:
                return heads[_key(lines[j])]
            ind = _indent(lines[j])
    return None


def _short(text: str, n: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "…"


def audit(before: dict[str, str], workdir: Path, owners: dict[str, dict[str, str]],
          finished: set[str]) -> tuple[str, list[str]]:
    """How the test files changed since the stage started.  (refusal or '',
    notes for the user).  Refused: a changed line in a finished step's test,
    and a deleted check (assertion).  Added tests are fine; any other change
    — a corrected expectation, say — is a note."""
    frozen, deleted, notes = [], [], []
    for rel, old in before.items():
        try:
            new = (workdir / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            new = ""
        if new == old:
            continue
        a, b = split_lines(old), split_lines(new)
        heads = owners.get(rel, {})
        matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for group in matcher.get_grouped_opcodes(0):
            changed = [i for op, i1, i2, _, _ in group if op in ("delete", "replace")
                       for i in range(i1, i2) if a[i].strip()]
            if not changed:
                continue            # only added lines: new tests are fine
            for op, i1, i2, _, _ in group:
                for i in range(i1, i2):
                    if not a[i].strip() or op == "equal":
                        continue
                    owner = _owner(a, i, heads)
                    if owner in finished:
                        frozen.append(f"{rel}:{i + 1} [{owner}]  {_short(a[i])}")
                    elif op == "delete" and _ASSERT_RE.search(a[i]):
                        deleted.append(f"{rel}:{i + 1}  {_short(a[i])}")
            # The note shows the whole changed region, inserted lines included.
            i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
            old_part = " / ".join(x.strip() for x in a[i1:i2] if x.strip())
            new_part = " / ".join(x.strip() for x in b[j1:j2] if x.strip())
            notes.append(f"{rel}:{i1 + 1}  {_short(old_part, 70)}  →  {_short(new_part, 70) or '(removed)'}")
    refusal = ""
    if frozen:
        refusal += ("These tests belong to finished steps and must not change:\n"
                    + "\n".join(f"  - {f}" for f in frozen[:_MAX_SITES]) + "\n"
                    "Restore them. If one of them is wrong, ask the user with ask_user.\n")
    if deleted:
        refusal += ("These checks were deleted from the tests:\n"
                    + "\n".join(f"  - {d}" for d in deleted[:_MAX_SITES]) + "\n"
                    "Never delete a test's checks to get green. If an expectation is wrong, "
                    "change it and say so in your summary, or ask the user.\n")
    return refusal.rstrip(), notes[:_MAX_NOTES]


def switch_on(workdir: Path, tag: str) -> tuple[int, list[str], dict[str, str]]:
    """Delete the marker lines carrying tag.  (markers removed, 'path:line' of
    tagged lines that are not plain markers and so are the model's to change,
    {path: text before} of the files changed)."""
    removed, left, changed = 0, [], {}
    for path, idxs in _tagged_files(workdir, find_tags(workdir).get(tag, [])).items():
        try:
            # Bytes, not text mode: text mode would turn a CRLF file into LF.
            lines = split_lines(path.read_bytes().decode("utf-8"), keepends=True)
        except (OSError, UnicodeDecodeError):
            continue
        drop = {i for i in idxs if i < len(lines) and _is_marker(lines[i])
                and tag in TAG_RE.findall(lines[i])}
        rel = path.relative_to(workdir).as_posix()
        left += [f"{rel}:{i + 1}" for i in sorted(set(idxs) - drop)]
        if drop:
            changed[rel] = "".join(lines)
            path.write_bytes("".join(l for i, l in enumerate(lines) if i not in drop)
                             .encode("utf-8"))
            removed += len(drop)
    return removed, left, changed


# ── the commands ─────────────────────────────────────────────────────────────

def stage_files(plan: Plan, tag: str) -> list[str]:
    """The test files holding a test of this tag (from plan.test_owners)."""
    return sorted(rel for rel, heads in plan.test_owners.items() if tag in heads.values())


def step_command(plan: Plan, tag: str) -> str:
    """plan.step_test_command with this tag's test files filled in: {files} their
    paths, {dirs} their directories as ./dir, {stems} their names without the
    extension.  '' when there is no template or no file to fill in."""
    files = stage_files(plan, tag)
    if not plan.step_test_command or not files:
        return ""
    dirs = sorted({"./" + str(Path(f).parent) if str(Path(f).parent) != "." else "." for f in files})
    fill = {"files": files, "dirs": dirs, "stems": sorted({Path(f).stem for f in files})}
    out = plan.step_test_command
    for key, values in fill.items():
        out = out.replace("{" + key + "}", " ".join(shlex.quote(v) for v in values))
    return out


# Flags that only change how much a test runner prints, not what it runs.
VERBOSITY_FLAGS = {"-v", "-vv", "-vvv", "--verbose", "-q", "-qq", "--quiet"}
_SHELL_OPS = {"|", "||", "&&", ";", ">", ">>", "<", "&", "2>&1", "2>", "1>"}


def same_command(ran: str, planned: str) -> bool:
    """Whether the model's command runs the planned one with the same exit code:
    the same words apart from verbosity flags, and no pipe or other shell
    operator (a pipe's exit code is its last command's).  A trailing tail, head
    or grep pipe is fine: run_command takes it off and keeps the real exit code."""
    ran = tools.split_view_pipe(ran)[0]
    if "$(" in ran or "`" in ran:
        return False
    try:
        a, b = shlex.split(ran), shlex.split(planned)
    except ValueError:
        return False
    if any(t in _SHELL_OPS or t.startswith((">", "<", "|")) or t.endswith((";", "|", "&"))
           for t in a):
        return False
    return [t for t in a if t not in VERBOSITY_FLAGS] == [t for t in b if t not in VERBOSITY_FLAGS]


def run_tests(command: str, workdir: Path, cancel=None) -> tuple[bool, str]:
    """Run the plan's test command.  (passed, the end of its output)."""
    timeout = tools.MAX_COMMAND_TIMEOUT
    with tempfile.TemporaryFile() as out:
        proc, stopped = tools.exec_command(command, timeout, workdir, cancel, out, subprocess.STDOUT)
        if proc is None:
            return False, stopped
        text = tools.output_tail(out)
    if len(text) > _OUTPUT_CHARS:
        text = "…" + text[-_OUTPUT_CHARS:]
    if stopped:
        return False, f"{stopped}\n{text}".rstrip()
    if proc.returncode != 0:
        return False, f"{text}\n[exit code: {proc.returncode}]".lstrip()
    return True, text or "(no output)"


def _sites(found: dict[str, list[str]], tag: str) -> str:
    sites = found.get(tag, [])
    more = f" (+{len(sites) - _MAX_SITES} more)" if len(sites) > _MAX_SITES else ""
    return ", ".join(sites[:_MAX_SITES]) + more


def tag_problems(plan: Plan, idx: int, found: dict[str, list[str]],
                 hollow: list[str] = ()) -> str:
    """What is wrong with the pending markers at the end of stage idx ('' = nothing).
    hollow: the markers whose test has no body (checked at the tests stage)."""
    step = plan.steps[idx]
    if step.stage == "tests":
        missing = [s for s in plan.steps[idx + 1:] if s.tag and s.tag not in found]
        if missing:
            listing = "\n".join(f"  - {s.tag}: {s.title}" for s in missing)
            return ("No pending tests carry these tags yet:\n" + listing + "\n"
                    "Write each step's tests now, skipped with its tag in the skip reason.")
        if hollow:
            more = f" (+{len(hollow) - _MAX_SITES} more)" if len(hollow) > _MAX_SITES else ""
            return ("These tests have no body after their skip marker: "
                    + ", ".join(hollow[:_MAX_SITES]) + more + "\n"
                    "A test that is only a skip checks nothing. Write each full test now — "
                    "setup, the call, the assertions — under its skip marker.")
        return ""
    if step.stage == "acceptance":
        left = sorted(found)
    else:
        active = {s.tag for s in plan.steps[:idx + 1] if s.tag and s.stage == "impl"}
        left = [t for t in plan.tags() if t in active and t in found]
    if not left:
        return ""
    listing = "\n".join(f"  - {t}: {_sites(found, t)}" for t in left)
    return ("These tests are still marked pending, but their step is implemented now:\n"
            + listing + "\nRemove the skip markers (and the tags) so the tests run.")


def check_gate(plan: Plan, idx: int, workdir: Path, cancel=None,
               reuse_green: bool = False) -> tuple[bool, str]:
    """(passed, report) for the end of stage idx: the tag rules first, then the
    test command.  The report says what failed, or ends with the test output.
    reuse_green: the model's own run of the test command just passed and nothing
    was written since, so it stands in for running the suite again."""
    found = find_tags(workdir)
    hollow = hollow_sites(workdir, found) if plan.steps[idx].stage == "tests" else []
    problems = tag_problems(plan, idx, found, hollow)
    if problems:
        return False, problems
    if reuse_green:
        return True, (f"Tests pass: your run of `{plan.test_command}` just before, "
                      "nothing changed since.")
    ok, output = run_tests(plan.test_command, workdir, cancel)
    if ok:
        return True, f"Tests pass: {plan.test_command}\n{output}"
    return False, f"Tests fail: {plan.test_command}\n{output}"
