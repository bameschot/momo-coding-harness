from __future__ import annotations

import difflib


# ── diff construction ─────────────────────────────────────────────────────────

def build_diff_body(old_text: str, new_text: str) -> tuple[list[tuple[str, str]], int, int]:
    """Build a tagged unified diff body plus added/removed line counts.

    Returns (body, added, removed) where body is a list of (kind, text) tuples
    with kind in {"hunk", "add", "del", "ctx"}.  The leading "--- "/"+++ " file
    header lines emitted by difflib are stripped — the TUI renders its own header
    (compact or git-style) so those are redundant here.

    Body lines are returned unwrapped; the TUI's _LineBuffer provides horizontal
    scrolling, so long lines stay intact and "+"/"-" columns remain aligned.
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    body: list[tuple[str, str]] = []
    added = removed = 0
    # n=3 lines of surrounding context, matching git's default.
    diff = difflib.unified_diff(old_lines, new_lines, lineterm="", n=3)
    for i, line in enumerate(diff):
        # The first two yielded lines are the "--- " / "+++ " file headers.
        if i < 2:
            continue
        if line.startswith("@@"):
            body.append(("hunk", line))
        elif line.startswith("+"):
            body.append(("add", line))
            added += 1
        elif line.startswith("-"):
            body.append(("del", line))
            removed += 1
        else:
            body.append(("ctx", line))
    return body, added, removed
