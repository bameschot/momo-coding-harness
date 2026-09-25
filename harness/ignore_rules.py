"""Gitignore-syntax include/exclude rules for the code index.

The index's file filter (``/index-filter``) is one gitignore-style file per
project.  It is seeded once from the project's .gitignore files; after that
only the filter decides, so later .gitignore edits change nothing until
``/index-filter reset``.

Supported syntax, as in git: ``#`` comments, blank lines, trailing spaces
(``\\ `` keeps one), ``\\#`` / ``\\!`` escapes, ``!`` re-includes, a trailing
``/`` matches directories only, a pattern with a ``/`` at the start or in the
middle is anchored at the project root (otherwise it matches at any depth),
``*`` ``?`` ``[...]``, and ``**`` as ``**/x``, ``x/**`` or ``a/**/b``.  As in
git, a file inside an excluded directory cannot be re-included unless the
directory itself is.  ``.git/`` is always excluded.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .paths import SKIP_DIRS

# Folders that are never worth indexing; seeded as editable `name/` lines
# (.git/ is always excluded, so it needs no line).
DEFAULT_EXCLUDES = tuple(f"{d}/" for d in SKIP_DIRS if d != ".git")
_MAX_SEED_GITIGNORES = 500
_MAX_SEED_DIRS = 20_000     # folders searched for nested .gitignore files (a workdir of $HOME)


@dataclass(frozen=True)
class Rule:
    pattern: str        # the source line, for messages
    rx: re.Pattern
    negate: bool
    dir_only: bool


def _glob_segment(seg: str) -> str:
    out = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "\\" and i + 1 < n:
            out.append(re.escape(seg[i + 1]))
            i += 2
            continue
        if c == "*":
            while i < n and seg[i] == "*":
                i += 1
            out.append("[^/]*")
            continue
        if c == "?":
            out.append("[^/]")
        elif c == "[":
            j = seg.find("]", i + 2 if i + 1 < n and seg[i + 1] in "!^" else i + 1)
            if j < 0:
                out.append(re.escape(c))
            else:
                body = seg[i + 1:j]
                neg = body[:1] in ("!", "^")
                if neg:
                    body = body[1:]
                body = body.replace("\\", "\\\\")
                out.append(f"[{'^' if neg else ''}{body}]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _compile(body: str) -> re.Pattern:
    """A pattern without its `!` and trailing `/` -> a regex over a relpath."""
    anchored = "/" in body
    if body.startswith("/"):
        body = body[1:]
    parts = body.split("/")
    rx = "" if anchored else "(?:.*/)?"
    last = len(parts) - 1
    need_sep = False
    for i, seg in enumerate(parts):
        if seg == "**":
            if i == 0:
                rx += "(?:.*/)?"
                need_sep = False
            elif i == last:
                rx += "(?:/.+)?"         # git excludes the folder itself too
            else:
                rx += "(?:/.+)?"
                need_sep = True
            continue
        if need_sep:
            rx += "/"
        rx += _glob_segment(seg)
        need_sep = True
    return re.compile(rx, re.DOTALL)


def _strip_trailing_spaces(line: str) -> str:
    end = len(line)
    while end > 0 and line[end - 1] == " " and not (end > 1 and line[end - 2] == "\\"):
        end -= 1
    return line[:end]


def parse_line(line: str) -> Rule | None:
    line = _strip_trailing_spaces(line.rstrip("\r\n"))
    if not line or line.startswith("#"):
        return None
    negate = line.startswith("!")
    body = line[1:] if negate else line
    if body.startswith(("\\#", "\\!")):
        body = body[1:]
    dir_only = body.endswith("/") and not body.endswith("\\/")
    body = body.rstrip("/") if dir_only else body
    if not body or body == "/":
        return None
    try:
        return Rule(line, _compile(body), negate, dir_only)
    except re.error:
        return None


class Rules:
    """Compiled filter rules.  Immutable; results are cached per path."""

    def __init__(self, rules: list[Rule]):
        self.rules = rules
        self._cache: dict[tuple[str, bool], bool] = {}

    @classmethod
    def parse(cls, text: str) -> "Rules":
        return cls([r for r in map(parse_line, text.splitlines()) if r is not None])

    def __len__(self) -> int:
        return len(self.rules)

    def _match(self, path: str, is_dir: bool) -> bool:
        key = (path, is_dir)
        hit = self._cache.get(key)
        if hit is None:
            hit = False
            for r in reversed(self.rules):          # the last matching rule wins
                if r.dir_only and not is_dir:
                    continue
                if r.rx.fullmatch(path):
                    hit = not r.negate
                    break
            if len(self._cache) > 200_000:
                self._cache.clear()
            self._cache[key] = hit
        return hit

    def dir_excluded(self, rel: str) -> bool:
        """For a top-down walk whose parents are already known to be included."""
        return rel.rsplit("/", 1)[-1] == ".git" or self._match(rel, True)

    def file_excluded(self, rel: str) -> bool:
        """Likewise, for a file whose parent directories are included."""
        return self._match(rel, False)

    def excluded(self, rel: str, is_dir: bool = False) -> bool:
        """Full check: any excluded parent directory excludes the path."""
        parts = rel.split("/")
        if ".git" in parts:
            return True
        for k in range(1, len(parts)):
            if self._match("/".join(parts[:k]), True):
                return True
        return self._match(rel, is_dir)


def _rewrite_nested(line: str, prefix: str) -> str | None:
    """A line of <prefix>/.gitignore as a root-level filter line."""
    stripped = _strip_trailing_spaces(line.rstrip("\r\n"))
    if not stripped or stripped.startswith("#"):
        return None
    neg = stripped.startswith("!")
    body = stripped[1:] if neg else stripped
    dir_only = body.endswith("/") and not body.endswith("\\/")
    core = body.rstrip("/") if dir_only else body
    if not core:
        return None
    if "/" in core:
        new = f"/{prefix}/{core.lstrip('/')}"
    else:
        new = f"/{prefix}/**/{core}"
    return ("!" if neg else "") + new + ("/" if dir_only else "")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def seed_text(root: Path) -> str:
    """The first filter for a project: defaults + every .gitignore (nested ones
    rewritten to their directory) + .git/info/exclude.  Directories that the
    rules so far exclude are not searched for more .gitignore files, as in git.
    The search stops after _MAX_SEED_DIRS folders."""
    root = Path(root)
    is_repo = (root / ".git").exists()
    lines = ["# momo code index filter — gitignore syntax; the last matching line wins.",
             "# `!pattern` re-includes. Seeded once from .gitignore; later .gitignore",
             "# edits are not picked up (/index-filter reset re-seeds).",
             "# Index only src/:  /*  then  !/src/",
             "", "# defaults", *DEFAULT_EXCLUDES]
    if not is_repo:
        lines.append(".*")          # outside git, hidden files were never indexed
    root_gi = _read(root / ".gitignore")
    if root_gi.strip():
        lines += ["", "# .gitignore", *root_gi.splitlines()]
    rules = Rules.parse("\n".join(lines))
    nested: list[str] = []
    seen = dirs = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirs += 1
        if dirs > _MAX_SEED_DIRS:
            nested += ["", f"# (stopped looking for nested .gitignore files after "
                           f"{_MAX_SEED_DIRS:,} folders)"]
            break
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        if rel_dir and ".gitignore" in filenames and seen < _MAX_SEED_GITIGNORES:
            seen += 1
            text = _read(Path(dirpath) / ".gitignore")
            rewritten = [r for r in (_rewrite_nested(l, rel_dir) for l in text.splitlines()) if r]
            if rewritten:
                nested += ["", f"# {rel_dir}/.gitignore", *rewritten]
                rules = Rules(rules.rules + [r for r in map(parse_line, rewritten) if r])
        dirnames[:] = sorted(d for d in dirnames
                             if not rules.dir_excluded(f"{rel_dir}/{d}" if rel_dir else d))
    lines += nested
    exclude = _read(root / ".git" / "info" / "exclude")
    body = [l for l in exclude.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if body:
        lines += ["", "# .git/info/exclude", *body]
    return "\n".join(lines) + "\n"
