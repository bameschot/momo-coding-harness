"""Comparing a file's definitions before and after a change: what is gone,
whose parameters changed.  Shared by the edit tools' follow-up checks
(editing.py) and index_changes."""
from __future__ import annotations

import re

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


def _source(parsed, s) -> str:
    return "\n".join(parsed.lines[s.start - 1:s.end])


def _renamed(before, after, gone, new) -> bool:
    """Whether `new` is `gone` under another name: the same source once the
    name is swapped, whitespace aside."""
    swapped = re.sub(rf"(?<![\w$]){re.escape(gone.name)}(?![\w$])", new.name, _source(before, gone))
    return "".join(swapped.split()) == "".join(_source(after, new).split())


def anchor_loss(before, after, need_added: bool = True) -> list:
    """Definitions a change removes while it adds others: the anchor mistake,
    where the old code was used to find the place for new code and left out of
    the replacement.  [] when nothing is removed, when nothing is added (a plain
    deletion; with need_added=False any removal counts), or for a rename."""
    if before is None or after is None:
        return []
    b, a = _defs(before), _defs(after)
    gone = _gone(b, a)
    added = [v[0] for name, v in a.items() if name not in b]
    if not gone or (need_added and not added):
        return []
    return [g for g in gone if not any(_renamed(before, after, g, n) for n in added)]


def added_names(before, after) -> list[str]:
    """Definition names a change adds, in file order."""
    b = _defs(before)
    return [name for name in _defs(after) if name not in b]


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
