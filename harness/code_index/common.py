"""What every index_* answer shares: the answer-size caps (in one place, so
the evals can tune them), the path= scope, the degraded/current notes, and
small lookups over the store."""
from __future__ import annotations

import fnmatch
from pathlib import Path

from .. import code_nav
from ..paths import safe_path
from .records import DATA_LANGS, _fmt_size
from .store import ProjectIndex


def _path_filter(path: str | None):
    """Predicate for a workdir-relative path: a directory/file prefix or a glob."""
    if not path or path in (".", "./"):
        return lambda rel: True
    p = path.strip()
    if p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    if code_nav._is_pattern(p):
        return lambda rel: fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*")
    return lambda rel: rel == p or rel.startswith(p + "/")


def _scope(path: str | None, workdir: Path, index: ProjectIndex) -> tuple[str | None, str]:
    """(path= as a project-relative path, '') or (None, an ERROR string).  An
    absolute or ./-prefixed path resolves like every other path argument; a
    glob is matched as written."""
    if not path or not path.strip() or code_nav._is_pattern(path):
        return path, ""
    p = safe_path(path.strip(), workdir)
    if isinstance(p, str):
        return None, p
    rel = index.rel(p)
    if rel is None:
        return None, f"ERROR: {path} is outside the indexed project"
    return (None if rel == "." else rel), ""


def _degraded_note(index: ProjectIndex, component: str | None = None) -> str:
    parts = []
    if index.partial:
        parts.append(f"the index covers only part of the project (memory budget "
                     f"{_fmt_size(index.max_bytes)}; {index.skipped['budget']} files left out)")
    if component and not index.components.get(component, True):
        parts.append({"trigrams": "text search is scanning files (signatures dropped for memory)",
                      "idents": "the identifier index was dropped for memory, so files were re-parsed"
                      }[component])
    if index.skipped.get("limit"):
        parts.append(f"the project has more than {index.max_files:,} files; the ones past "
                     f"that limit are not indexed (/index-max-files)")
    return f"(note: {'; '.join(parts)})" if parts else ""


# Every index answer ends with this.  The 9B re-read the file an index result
# had just pointed at in most runs (evals, 2026-09-23) — "a correct result,
# distrusted" — and wait_fresh() really has re-synced with the disk, so say so
# where the decision to re-check gets made.
_CURRENT = ("(checked against the files on disk just now — this is current and complete: "
            "answer from it; re-reading files to double-check is not needed)")


def _with_note(text: str, index: ProjectIndex, component: str | None = None) -> str:
    note = _degraded_note(index, component)
    return text + (f"\n{note}" if note else "") + "\n" + _CURRENT


def _ready(index, cancel) -> str | None:
    if index is None:
        return "ERROR: the code index is off (the user can turn it on with /index on)"
    return index.wait_fresh(cancel)


# Kinds the HTML extractor gives the page itself; everything else in an HTML
# file comes from its inline <script> JavaScript.
_HTML_KINDS = {"id", "script", "style", "element", "template"}


def _is_code_symbol(lang: str | None, sym) -> bool:
    """A definition in code — not a config key, a CSS rule or an HTML id."""
    if not lang:
        return False
    if lang == "html":
        return sym.kind not in _HTML_KINDS
    return lang not in DATA_LANGS


# Answer sizes: a small model's context is 16k tokens, so one answer stays
# well under 2k.  In one place, so the evals can tune them.
_MAX_SEARCH = 30            # hits listed with an exact match block, and files listed
_MAX_FUZZY = 12             # hits listed when nothing matches exactly: closest first
_MAX_INLINE_RANK = 3        # only the best few hits get their source inlined
# Short definitions whose value IS the answer (a constant, a config key, a CSS
# rule, a SQL table's columns) are shown whole, so nothing is left to open.
_VALUE_KINDS = {"constant", "variable", "key", "list", "table", "var", "rule", "column",
                "env", "arg", "id", "keyframes", "media", "view", "index", "stage", "typedef",
                "type", "enum", "macro"}
_MAX_INLINE_LINES = 6
_MAX_INLINE_HITS = 8
_MAX_APPROX_AFTER_EXACT = 5   # an exact hit is the answer; a few near misses are context
_MAX_TEXT_HITS = 200
_MAX_TEXT_LINE = 200
_MAX_TEXT_CHARS = 6000      # the whole answer, like index_callers
_MAX_TEXT_PER_FILE = 5      # lines shown per file before moving on to the next file
_MAX_CALLER_GROUPS = 25     # places listed at level 1
_MAX_LINES_PER_GROUP = 3
_MAX_EXPAND = 8             # level-1 definitions followed to level 2 (and so on)
_MAX_EXPAND_GROUPS = 5      # places listed per followed definition
_MAX_CALLERS_CHARS = 6000   # the whole answer
_MAX_EXPAND_FILES = 300     # a name used in more files than this is too common to expand


def _read_source(path: Path) -> str:
    """A file's text with its line endings as written: read_text() would turn a
    lone "\r" into a line break the parser does not count."""
    return path.read_bytes().decode("utf-8", errors="replace")


def _defs_named(index: ProjectIndex, name: str, exact: bool = True) -> list:
    """[(FileEntry, Symbol)] of the indexed definitions named `name` — in any
    letter case when not `exact`."""
    with index._lock:
        out = []
        for f, i in index._sym_by_name.get(name.lower(), ()):
            e = index.files[f]
            if e is not None and (not exact or e.symbols[i].name == name):
                out.append((e, e.symbols[i]))
        return out
