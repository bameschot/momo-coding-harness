"""Comparing a file's definitions before and after a change: what is gone,
whose parameters changed.  Shared by the edit tools' follow-up checks
(editing.py) and index_changes."""
from __future__ import annotations

from .. import code_nav
from .records import DATA_LANGS

_DEF_KINDS = code_nav.CALLABLE_KINDS | code_nav.CONTAINER_KINDS if code_nav.AVAILABLE else set()


def _defs(parsed) -> dict[str, list]:
    """bare name -> its code definitions in a parsed file."""
    out: dict[str, list] = {}
    if parsed is None or parsed.lang in DATA_LANGS:
        return out          # config keys and CSS rules have no callers
    for s in parsed.symbols:
        if s.kind in _DEF_KINDS:
            out.setdefault(s.name, []).append(s)
    return out


def _gone(b: dict, a: dict) -> list:
    """Definitions in b whose name a no longer has — a removed class, not also
    each of its methods."""
    gone = [v[0] for name, v in b.items() if name not in a]
    quals = {s.qualname for s in gone}
    return [s for s in gone if s.qualname.rsplit(".", 1)[0] not in quals or "." not in s.qualname]


_MAX_PARAM_LINES = 20       # a parameter list longer than this is compared by its start


def _params(parsed, s) -> str:
    """The parameter list after a definition's name: `def f(a, b=1) -> int:`
    -> '(a, b=1)', also when it spans several lines (whitespace collapsed).
    The rest of the name line when no parenthesis follows the name there."""
    if not 0 < s.name_line <= len(parsed.lines):
        return ""
    line = parsed.lines[s.name_line - 1]
    at = line.find(s.name)
    rest = line[at + len(s.name):] if at >= 0 else line
    o = rest.find("(")
    if o < 0:
        return rest.strip()
    text = "\n".join([rest] + parsed.lines[s.name_line:s.name_line + _MAX_PARAM_LINES])
    depth = 0
    for i in range(o, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return " ".join(text[o:i + 1].split()).replace("( ", "(").replace(" )", ")")
    return rest[o:].strip()


def _same(a: str, b: str) -> bool:
    """Equal but for whitespace."""
    return "".join(a.split()) == "".join(b.split())
