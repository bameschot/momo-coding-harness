from __future__ import annotations

import difflib
import re

# Captures the old/new starting line numbers from a unified-diff hunk header,
# e.g. "@@ -12,3 +12,4 @@". The counts are optional (git omits them for a
# single line) and the starts can be 0 for pure add/delete hunks.
_HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


# ── diff construction ─────────────────────────────────────────────────────────

def build_diff_body(
    old_text: str, new_text: str
) -> tuple[list[tuple[str, int | None, int | None, str]], int, int]:
    """Build a tagged unified diff body plus added/removed line counts.

    Returns (body, added, removed) where body is a list of
    (kind, old_no, new_no, text) tuples with kind in {"hunk", "add", "del", "ctx"}.
    old_no/new_no are the 1-based line numbers in the old and new file: context
    lines carry both, removed lines only old_no, added lines only new_no, and hunk
    headers neither (None). The leading "--- "/"+++ " file header lines emitted by
    difflib are stripped — the TUI renders its own header (compact or git-style).

    Body lines are returned unwrapped; the TUI's _LineBuffer provides horizontal
    scrolling, so long lines stay intact and "+"/"-" columns remain aligned.
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    body: list[tuple[str, int | None, int | None, str]] = []
    added = removed = 0
    old_ln = new_ln = 0
    # n=3 lines of surrounding context, matching git's default.
    diff = difflib.unified_diff(old_lines, new_lines, lineterm="", n=3)
    for i, line in enumerate(diff):
        # The first two yielded lines are the "--- " / "+++ " file headers.
        if i < 2:
            continue
        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                old_ln, new_ln = int(m.group(1)), int(m.group(2))
            body.append(("hunk", None, None, line))
        elif line.startswith("+"):
            body.append(("add", None, new_ln, line))
            new_ln += 1
            added += 1
        elif line.startswith("-"):
            body.append(("del", old_ln, None, line))
            old_ln += 1
            removed += 1
        else:
            body.append(("ctx", old_ln, new_ln, line))
            old_ln += 1
            new_ln += 1
    return body, added, removed
