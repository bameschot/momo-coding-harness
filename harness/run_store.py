"""Saved run_command output (/run-mode new).

In the new run mode a command's whole output is written to a log file under
~/.momo-harness/runs/<session>/, and the model gets back a bounded view of it:
the tail, a grep, a line range, or (by default) the beginning and end.  The log
stays queryable through command_output, so a second look never means running a
test suite again.  The directory lives outside the workdir, so the logs never
show up in git status or the code index.

The view helpers work on plain text and never touch the disk, so they are shared
by run_command and command_output and unit-testable without a subprocess.
"""
from __future__ import annotations

import re
import shutil
import threading
import time
from pathlib import Path

DEFAULT_LIMIT = 5000          # chars one run_command / command_output view may return
MIN_LIMIT = 500
KEEP_LOGS = 20                # per session; older logs are deleted
KEEP_BYTES = 256 * 2**20      # per session, however few logs that is
STALE_SECS = 3 * 24 * 3600    # other sessions' run dirs older than this are removed

# Words that make a line worth a look.  A hint, not a verdict: "0 errors" counts too.
_TROUBLE_RE = re.compile(r"\b(?:error|errors|fail|failed|failure|failures|fatal|panic|"
                         r"traceback|exception|warning|warnings)\b", re.IGNORECASE)
_TROUBLE_GREP = "error|fail|warning|traceback"
_LOG_RE = re.compile(r"r(\d+)(?:\.log)?")


def runs_root() -> Path:
    """Evaluated per call, so a test that points HOME elsewhere is honoured."""
    return Path.home() / ".momo-harness" / "runs"


class RunStore:
    """The log files of one session's commands, r1.log, r2.log, ..."""

    def __init__(self, root: Path):
        self.root = root
        self._n = 0
        self._lock = threading.Lock()

    def new_log(self) -> tuple[str, Path]:
        """A fresh log path (not created), after pruning the oldest logs."""
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._n == 0:          # carry on after logs a previous store left
                self._n = max((int(m.group(1)) for p in self.root.iterdir()
                               if (m := _LOG_RE.fullmatch(p.name))), default=0)
            self._n += 1
            run_id = f"r{self._n}"
            self._prune(keep=KEEP_LOGS - 1)
            return run_id, self.root / f"{run_id}.log"

    def logs(self) -> list[Path]:
        """Logs still on disk, oldest first."""
        if not self.root.is_dir():
            return []
        found = [(int(m.group(1)), p) for p in self.root.iterdir()
                 if (m := _LOG_RE.fullmatch(p.name)) and p.name.endswith(".log")]
        return [p for _, p in sorted(found)]

    def _prune(self, keep: int) -> None:
        logs = self.logs()
        for p in logs[:max(0, len(logs) - keep)]:
            p.unlink(missing_ok=True)
        logs = self.logs()
        total = sum(p.stat().st_size for p in logs)
        for p in logs[:-1]:            # never the newest
            if total <= KEEP_BYTES:
                break
            total -= p.stat().st_size
            p.unlink(missing_ok=True)

    def resolve(self, ref: str) -> Path | None:
        """The log a model's reference names: the full path from a result footer,
        a bare file name (r3.log) or the id (r3).  Only the rN part is used, and
        only this store's own file is ever opened — never the path as given — so
        command_output cannot read arbitrary files.  A mistyped directory still
        finds the log: the 9B garbled the long session path once in 13 calls."""
        ref = (ref or "").strip().strip("\"'`")
        name = Path(ref).name if ref else ""
        if not _LOG_RE.fullmatch(name):
            return None
        p = self.root / (name if name.endswith(".log") else f"{name}.log")
        if p.is_symlink() or not p.is_file():
            return None
        return p

    def clear(self) -> None:
        with self._lock:
            shutil.rmtree(self.root, ignore_errors=True)
            self._n = 0


def prune_stale(current: Path | None = None) -> None:
    """Remove other sessions' run dirs that have not been written for a while
    (a crash or kill skips the clean-up a normal session change does)."""
    root = runs_root()
    if not root.is_dir():
        return
    cutoff = time.time() - STALE_SECS
    for d in root.iterdir():
        try:
            if d.is_dir() and d != current and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ── views ─────────────────────────────────────────────────────────────────────

def _fit_head(lines: list[str], budget: int) -> tuple[list[str], int]:
    """Leading lines within budget chars; a single over-long first line is cut."""
    out, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > budget:
            if not out and budget > 0:
                return [ln[:budget] + "…"], 1
            break
        out.append(ln)
        used += len(ln) + 1
    return out, len(out)


def _fit_tail(lines: list[str], budget: int) -> tuple[list[str], int]:
    """Trailing lines within budget chars; a single over-long last line is cut."""
    out, used = [], 0
    for ln in reversed(lines):
        if used + len(ln) + 1 > budget:
            if not out and budget > 0:
                return ["…" + ln[-budget:]], 1
            break
        out.append(ln)
        used += len(ln) + 1
    out.reverse()
    return out, len(out)


def window(text: str, limit: int, how: str = "") -> str:
    """text whole when it fits in limit chars; otherwise its first quarter and
    last three quarters of the limit, cut on line boundaries, with a note of
    what was left out in between.  how: what to pass for the missing part."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    head, nh = _fit_head(lines, limit // 4)
    tail, nt = _fit_tail(lines[nh:], limit - limit // 4)
    omitted = lines[nh:len(lines) - nt]
    chars = sum(len(ln) + 1 for ln in omitted)
    if not omitted:                # one huge line, cut on both ends
        chars = len(text) - sum(len(ln) + 1 for ln in head + tail)
    note = (f"[… {len(omitted):,} lines ({chars:,} chars) not shown"
            + (f" — {how}" if how else "") + " …]")
    return "\n".join(head + [note] + tail)


def tail(lines: list[str], n: int) -> str:
    n = max(1, n)
    start = max(0, len(lines) - n)
    shown = lines[start:]
    return f"[last {len(shown):,} of {len(lines):,} lines, from line {start + 1:,}]\n" + "\n".join(shown)


def line_range(lines: list[str], start: int | None, end: int | None) -> str:
    total = len(lines)
    start = 1 if start is None else start
    if start < 0:                        # from the end, like read_file
        start = max(1, total + start + 1)
    start = max(1, start)
    end = total if end is None or end > total else end
    if start > total:
        return f"[the output has only {total:,} lines]"
    if end < start:
        return f"ERROR: end_line ({end}) is before start_line ({start})"
    width = len(str(end))
    body = "\n".join(f"{i:>{width}}: {lines[i - 1]}" for i in range(start, end + 1))
    return f"[lines {start:,}-{end:,} of {total:,}]\n{body}"


def compile_pattern(pattern: str) -> tuple[re.Pattern, str]:
    """A case-insensitive regex, and a note when the pattern was taken literally.
    grep -E habits are accepted: BRE's \\| alternation means |."""
    pat = pattern.replace("\\|", "|")
    try:
        return re.compile(pat, re.IGNORECASE), ""
    except re.error:
        return (re.compile(re.escape(pattern), re.IGNORECASE),
                " (not a valid regex, matched as plain text)")


def grep(lines: list[str], pattern: str, context: int = 0, last: int | None = None) -> str:
    """Matching lines with their line numbers ("N: "), context lines as "N- ",
    groups separated by "--".  last: only the last N matches."""
    rx, note = compile_pattern(pattern)
    hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
    total = len(lines)
    if not hits:
        return f'[grep "{pattern}"{note}: no matching lines in {total:,}]'
    shown = hits[-last:] if last else hits
    context = max(0, context)
    keep: dict[int, bool] = {}
    for i in shown:
        for j in range(max(0, i - context), min(total, i + context + 1)):
            keep[j] = keep.get(j, False) or j == i
    width = len(str(max(keep) + 1))
    out, prev = [], None
    for j in sorted(keep):
        if prev is not None and j != prev + 1:
            out.append("--")
        out.append(f"{j + 1:>{width}}{':' if keep[j] else '-'} {lines[j]}")
        prev = j
    which = (f"last {len(shown):,} of {len(hits):,}" if len(shown) < len(hits)
             else f"{len(hits):,}")
    return (f'[grep "{pattern}"{note}: {which} matching lines of {total:,}]\n'
            + "\n".join(out))


def view(text: str, limit: int, *, tail_n: int | None = None, pattern: str | None = None,
         context: int = 0, start_line: int | None = None, end_line: int | None = None) -> str:
    """The part of a command's output the model asked for, capped at limit chars."""
    lines = text.split("\n") if text else []
    if pattern:
        body = grep(lines, pattern, context, tail_n)
        how = "narrow the pattern, or pass tail= for the last matches"
    elif tail_n:
        body = tail(lines, tail_n)
        how = "pass a smaller tail="
    elif start_line is not None or end_line is not None:
        body = line_range(lines, start_line, end_line)
        how = "ask for a smaller line range"
    else:
        body = text
        how = "pass tail=, grep= or start_line/end_line to see it"
    return window(body, limit, how)


def footer(path: Path, text: str, exit_code: int | None, hint: bool,
           stopped: str | None = None) -> str:
    """The closing lines of every new-mode result: where the log is, its size,
    how the command ended, and (hint: a default view that hid lines) how to
    look at the rest, pointing at the lines that mention trouble."""
    n_lines = text.count("\n") + 1 if text else 0
    size = path.stat().st_size if path.exists() else 0
    kb = f"{size / 1024:,.0f} KB" if size >= 1024 else f"{size:,} bytes"
    end = stopped or ("still running" if exit_code is None else f"exit code {exit_code}")
    lines = [f"[output saved: {path} — {n_lines:,} lines, {kb}, {end}]"]
    if hint:
        trouble = sum(1 for ln in text.split("\n") if _TROUBLE_RE.search(ln))
        if trouble:
            lines.append(f'[{trouble:,} {"line mentions" if trouble == 1 else "lines mention"} error/fail/warning — '
                         f'command_output(path="{path}", grep="{_TROUBLE_GREP}") lists them]')
        else:
            lines.append(f'[command_output(path="{path}", grep="...") searches it, '
                         f'tail=N shows the end]')
    return "\n".join(lines)
