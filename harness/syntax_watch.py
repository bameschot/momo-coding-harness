"""Files this turn's writes left unparseable, watched across the calls after.

An edit result already warns when the edit breaks the file's syntax.  The 9B
then often stops trusting the file rather than fixing it: 25-50 calls of
byte dumps, parser probes and scratch scripts on a line it believed valid,
until the eval's time cap (2026-10-25).  So while a file this turn wrote has
more syntax errors than it had before the turn, every later tool result says
so, with the marked line, and after a couple of calls that do not touch it,
that looking further will not change it.  A file broken before the turn is
not blamed on it; one that parses again, by any route, goes quiet.
"""
from __future__ import annotations

from pathlib import Path

from . import editing
from .paths import safe_path, split_lines

_IDLE_PUSH = 2       # calls without an edit to the file, then the push to edit it
_ATTEMPT_PUSH = 2    # failed fixes, then the push to rewrite the line whole


class SyntaxWatch:
    def __init__(self):
        self._files: dict[Path, dict] = {}

    def reset(self) -> None:
        """A new turn: nothing is watched."""
        self._files = {}

    def note_write(self, path: str, workdir: Path, old_text: str | None) -> None:
        """Before-the-turn error count for a path a write tool is about to change
        (old_text: the file before this call; None when it did not exist)."""
        p = safe_path(path, workdir)
        if isinstance(p, str) or p in self._files:
            return
        base = editing.syntax_errors(p, old_text) if old_text else []
        if base is None:                 # no syntax check for this kind of file
            return
        self._files[p] = {"rel": path, "base": len(base), "broken": False,
                          "attempts": 0, "idle": 0}

    def after_call(self, written: list[str], workdir: Path) -> str:
        """Notes to add to the result of the call that just ran."""
        wrote = {p for w in written if not isinstance(p := safe_path(w, workdir), str)}
        notes = []
        for p, st in list(self._files.items()):
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                del self._files[p]       # deleted or moved: nothing left to parse
                continue
            errs = editing.syntax_errors(p, text)
            if errs is None or len(errs) <= st["base"]:
                st.update(broken=False, attempts=0, idle=0)
                continue
            was_broken, st["broken"] = st["broken"], True
            if p in wrote:               # the write tool's own result has the warning
                st["idle"] = 0
                if was_broken:
                    st["attempts"] += 1
                    if st["attempts"] >= _ATTEMPT_PUSH:
                        notes.append(f"\nNOTE: that is attempt {st['attempts'] + 1} at this "
                                     f"error in {st['rel']}. Rewrite the marked line as a "
                                     f"whole, writing it exactly as it should read.")
                continue
            st["idle"] += 1
            note = (f"\nNOTE: {st['rel']} still does not parse since your edit — "
                    f"{editing.syntax_mark(split_lines(text), errs[0])}")
            if st["idle"] >= _IDLE_PUSH:
                note += ("\nInspecting the file further won't change it: fix the marked spot "
                         "with edit_file, writing quotes and backslashes exactly as they "
                         "should appear in the file.")
            notes.append(note)
        return "".join(notes)
