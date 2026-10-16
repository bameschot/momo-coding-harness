"""index_search: definitions, keys, selectors and files by exact, fuzzy or
paraphrased name; a line number's enclosing definition; and find_files
answered from the index's file list."""
from __future__ import annotations

import fnmatch
import re
from functools import lru_cache
from pathlib import Path

from .. import code_nav
from ..paths import safe_path, split_lines
from .records import DATA_LANGS
from .store import ProjectIndex
from .common import (_HTML_KINDS, _MAX_APPROX_AFTER_EXACT, _MAX_FUZZY, _MAX_INLINE_HITS,
                     _MAX_INLINE_LINES, _MAX_INLINE_RANK, _MAX_SEARCH, _VALUE_KINDS, _path_filter,
                     _read_source, _ready, _scope, _with_note)
from .text import _text_mentions
from .graph import _module_names

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t


@lru_cache(maxsize=65536)
def _tokens(name: str) -> tuple[str, ...]:
    """'parseConfigFile' / 'parse_config_file' / 'Parser.parse' -> lowercase words,
    plural 's' dropped so 'bytes' meets 'byte'."""
    out = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        out.extend(_stem(t.lower()) for t in _CAMEL.findall(part))
    return tuple(out)


# Words a paraphrased question carries that never name anything.
_STOPWORDS = frozenset("""a an the to of in into for and or is it its that this from by on with as
    like what where which how does do get gets turn turns function functions method methods class
    defined define code thing""".split())


# Abbreviations a name uses where a question spells the word out ("database"
# for services.db).  Both directions: a query "cfg" also meets "config".
_ALIAS_GROUPS = (("database", "db"), ("config", "configuration", "cfg", "conf"),
                 ("environment", "env"), ("message", "msg"), ("button", "btn"),
                 ("authentication", "auth", "authorization"), ("repository", "repo"),
                 ("directory", "dir"), ("application", "app"), ("password", "pwd", "passwd"),
                 ("number", "num"), ("temporary", "tmp", "temp"), ("image", "img"),
                 ("parameter", "param"), ("argument", "arg"), ("request", "req"),
                 ("response", "resp", "res"), ("error", "err"), ("initialize", "init"),
                 ("maximum", "max"), ("minimum", "min"), ("attribute", "attr"),
                 ("document", "doc"), ("reference", "ref"), ("connection", "conn"),
                 ("service", "svc"), ("utility", "util"), ("library", "lib"))
_ALIASES: dict[str, frozenset[str]] = {}
for _g in _ALIAS_GROUPS:
    for _w in _g:
        _ALIASES[_stem(_w)] = frozenset(_stem(x) for x in _g if x != _w)


def _query_tokens(q: str) -> tuple[str, ...]:
    toks = _tokens(q)
    kept = tuple(t for t in toks if t not in _STOPWORDS)
    return kept or toks


_CODE_KIND_ORDER = {"class": 0, "interface": 0, "struct": 0, "trait": 0, "enum": 1,
                    "function": 1, "method": 1, "table": 2, "stage": 2}


def _score(s, q: str, ql: str, qtoks: tuple[str, ...], pattern: bool) -> int:
    if pattern:
        return 100 if code_nav._matches(s, q) else 0
    name, qual = s.name, s.qualname
    if name == q or qual == q:
        return 100
    nl, qualL = name.lower(), qual.lower()
    if nl == ql or qualL == ql:
        return 95
    if "." in ql and qualL.endswith("." + ql):
        return 90
    if " " in ql and nl.replace("_", "") == ql.replace(" ", "").replace("_", ""):
        return 92       # "make counter" / "is entity" -> makeCounter / isEntity
    if nl.startswith(ql):
        return 75 - min(10, len(nl) - len(ql))
    if len(ql) >= 3 and ql in nl:
        return 65
    if len(ql) >= 3 and ql in qualL:
        return 60
    if qtoks:
        # Words in any order, matched against the name and against the first
        # line of the docstring: 'size string to bytes' finds parse_size through
        # "Bytes from a plain number or a unit-suffixed string".
        # The kind counts as a word of the name: "users table" is the table users,
        # not the class UserEvent that merely shares "user".
        nf = _overlap(qtoks, _tokens(qual) + (_stem(s.kind),))
        df = _overlap(qtoks, _tokens(s.doc)) if getattr(s, "doc", "") else 0.0
        if nf >= 0.99:
            name_sc = 55
        elif nf >= 0.5 and len(qtoks) > 1:
            name_sc = int(30 + 20 * nf)
        elif nf > 0:
            name_sc = int(12 + 16 * nf)
        else:
            name_sc = 0
        if df >= 0.99:
            doc_sc = 45
        elif df >= 0.5:
            doc_sc = int(20 + 20 * df)
        else:
            doc_sc = 0
        if name_sc or doc_sc:
            return min(58, max(name_sc, doc_sc) + (6 if name_sc and doc_sc else 0))
    if len(ql) >= 3 and " " not in ql:
        it = iter(nl)
        if all(c in it for c in ql.replace("_", "")):
            return 20
        if ql in s.signature.lower():
            return 15
    return 0


def _overlap(qtoks: tuple[str, ...], stoks: tuple[str, ...]) -> float:
    """Fraction of query words found among a name's (or doc line's) words; a
    prefix either way ('conf' / 'config') counts three quarters."""
    if not stoks:
        return 0.0
    hit = 0.0
    for t in qtoks:
        if t in stoks or not _ALIASES.get(t, frozenset()).isdisjoint(stoks):
            hit += 1
        elif len(t) >= 3 and any(st.startswith(t) or (len(st) >= 3 and t.startswith(st))
                                 for st in stoks):
            hit += 0.75
    return hit / len(qtoks)


# Test code: `tests/`, `test_x.py`, `x_test.go`, `x.test.ts`, `x.spec.js`, ...
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|specs?|fixtures?|testdata|evals?)/"
                        r"|(^|/)test_[^/]*$|_test\.[^/.]+$|\.(test|spec)\.[^/.]+$")


def _is_test(rel: str) -> bool:
    return bool(_TEST_PATH.search(rel))


_PATHLIKE = re.compile(r"[/\\]|\.[A-Za-z0-9]{1,6}$")


_CALLABLE_KINDS = code_nav.CALLABLE_KINDS
_kind_matches = code_nav.kind_matches

# What a model may pass as lang=, and the languages it also covers: JavaScript
# lives in HTML pages too, and TSX is TypeScript.
_LANG_ALIASES = {"js": "javascript", "jsx": "javascript", "node": "javascript", "mjs": "javascript",
                 "ts": "typescript", "py": "python", "python3": "python", "kt": "kotlin",
                 "kts": "kotlin", "rs": "rust", "c++": "cpp", "cxx": "cpp", "cc": "cpp",
                 "hpp": "cpp", "sh": "bash", "shell": "bash", "yml": "yaml", "htm": "html",
                 "docker": "dockerfile", "postgres": "sql", "postgresql": "sql", "mysql": "sql"}
_LANG_ALSO = {"javascript": ("html",), "typescript": ("tsx",)}


def _norm_lang(lang: str | None) -> str | None:
    if not lang:
        return None
    w = lang.strip().lower()
    return _LANG_ALIASES.get(w, w)


def _file_score(rel: str, q: str) -> int:
    ql, rl = q.lower().lstrip("./"), rel.lower()
    base = rl.rsplit("/", 1)[-1]
    if code_nav._is_pattern(ql):                 # a glob, like find_files: '*.py', 'test_*'
        return 100 if fnmatch.fnmatch(base, ql) or fnmatch.fnmatch(rl, ql) else 0
    if rl == ql:
        return 100
    if rl.endswith("/" + ql):
        return 95
    if base == ql or base.rsplit(".", 1)[0] == ql:
        return 90
    if len(ql) >= 3 and ql in rl:
        return 60
    return 0


def index_search(query: str, kind: str | None = None, path: str | None = None,
                 lang: str | None = None, *, workdir: Path, index: ProjectIndex | None = None,
                 cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    path, err = _scope(path, workdir, index)
    if err:
        return err
    return _search(query, kind, path, lang, workdir, index)


def _search(query: str, kind: str | None, path: str | None, lang: str | None, workdir: Path,
            index: ProjectIndex) -> str:
    q = (query or "").strip().replace("::", ".")
    if not q:
        return "ERROR: query is empty — pass a name or part of one, e.g. 'parse_config'"
    line = code_nav.line_query(q)
    if line is not None:
        return _line_owner(index, line, path, workdir)
    if " " in q and not code_nav._is_pattern(q) and len(_tokens(q)) == 1:
        q = q.replace(" ", "")
    ql = q.lower()
    qtoks = _query_tokens(q)
    pattern = code_nav._is_pattern(q)
    keep = _path_filter(path)
    want_files = kind == "file" or (kind is None and bool(_PATHLIKE.search(q)))
    lang = _norm_lang(lang)
    langs = {lang, *_LANG_ALSO.get(lang, ())} if lang else None
    hits = []
    file_hits = []
    with index._lock:
        # 'code_nav.parse': a module-qualified name is exact for the module's
        # top-level parse, though no qualname spells the module.
        mod_fids, mod_name = set(), None
        if "." in q and not pattern:
            qual_mod, mod_name = q.rsplit(".", 1)
            mod_fids = _module_names(index)[0].get(qual_mod.replace("/", "."), set())
        for fid, e in index._alive():
            if (langs and e.lang not in langs) or not keep(e.path):
                continue
            page_js = lang == "javascript" and e.lang == "html"   # only its scripts
            in_mod = fid in mod_fids
            if want_files:
                fs = _file_score(e.path, q)
                if fs:
                    file_hits.append((-fs, len(e.path), e.path, e))
            if kind == "file":
                continue
            for s in e.symbols:
                if kind and not _kind_matches(s.kind, kind):
                    continue
                if page_js and s.kind in _HTML_KINDS:
                    continue
                sc = 100 if in_mod and s.depth == 0 and s.name == mod_name else \
                    _score(s, q, ql, qtoks, pattern)
                if sc:
                    hits.append((-sc, _CODE_KIND_ORDER.get(s.kind, 3), len(s.qualname),
                                 e.path, s.start, s, e.lang, _is_test(e.path)))
        total_files = len(index._by_path)
    file_hits.sort(key=lambda h: h[:3])
    if not hits and kind and kind != "file" and not file_hits:
        # The name exists, just not as that kind: say so rather than "nothing".
        other = _search(query, None, path, lang, workdir, index)     # already fresh
        if not other.startswith("(no definition"):
            return (f"(nothing of kind '{kind}' matches '{query}' — without kind= it matches "
                    f"these:)\n" + other)
    if file_hits and (not hits or -file_hits[0][0] >= -min(h[0] for h in hits)):
        # The query names a file: say so, and point at the tool that describes one.
        lines = [f"{h[2]}  (file, {h[3].lang or 'text'}, {h[3].nlines} lines, "
                 f"{len(h[3].symbols)} definitions)" for h in file_hits[:_MAX_SEARCH]]
        head = f"{len(file_hits)} file{'s' if len(file_hits) != 1 else ''} matching '{query}':"
        more = [f"... ({_MAX_SEARCH} of {len(file_hits)})"] if len(file_hits) > _MAX_SEARCH else []
        return _with_note("\n".join([head] + lines + more + [
            f'(next: index_file("{file_hits[0][2]}") shows its outline, imports and the files '
            f'that import it)']), index)
    if not hits:
        lines = [f"(no definition, key, selector or table matches '{query}' in {total_files} "
                 f"indexed files)"]
        mentions = _text_mentions(index, q, keep)
        if mentions:
            lines.append("The text appears in: " + mentions + " — index_text shows the lines")
        else:
            with index._lock:
                data = sorted({e.path for _, e in index._alive() if e.lang in DATA_LANGS
                               and e.lang not in ("html", "css") and keep(e.path)})
            if data:
                lines.append(f"Config files in the index ({len(data)}): " + ", ".join(data[:12])
                             + (" ..." if len(data) > 12 else "")
                             + " — code_outline lists a file's keys")
        return _with_note("\n".join(lines), index)
    # Test code ranks below every real match in the source: a test class named
    # Budget must not shadow the constant a question about the budget is after.
    source_match = any(not h[7] and h[0] <= -40 for h in hits)
    hits.sort(key=lambda h: (source_match and h[7],) + h[:5])
    out = []
    exact_block = hits[0][0] <= -90
    if exact_block:
        n_exact = sum(1 for h in hits if h[0] <= -90)
        shown_hits = hits[:min(_MAX_SEARCH, n_exact + _MAX_APPROX_AFTER_EXACT)]
    else:
        n_exact = 0
        shown_hits = hits[:_MAX_FUZZY]
    shown_sep = False
    file_lines: dict[str, list[str]] = {}
    inlined = 0
    best_whole = False
    for i, h in enumerate(shown_hits):
        sc, s, rel = -h[0], h[5], h[3]
        if exact_block and sc < 90 and not shown_sep:
            out.append("— approximate matches —")
            shown_sep = True
        out.append(f"{rel}:L{s.start}-{s.end}  {s.kind} {s.qualname}  | {s.signature}")
        span = s.end - s.start + 1
        if s.kind not in _VALUE_KINDS or span > _MAX_INLINE_LINES or i >= _MAX_INLINE_RANK:
            continue
        if span == 1:
            best_whole = best_whole or i == 0     # the signature is the whole source
            continue
        if inlined >= _MAX_INLINE_HITS:
            continue
        if rel not in file_lines:
            try:
                file_lines[rel] = split_lines(_read_source(index.root / rel))
            except OSError:
                file_lines[rel] = []
        src = file_lines[rel][s.start - 1:s.end]
        if len(src) == span:
            out.extend(f"    {s.start + k}: {line.rstrip()[:160]}" for k, line in enumerate(src))
            inlined += 1
            best_whole = best_whole or i == 0
    if exact_block:
        # Exact hits are the answer; the loose ones (a subsequence of the name
        # matches thousands) are only counted.
        head = f"{n_exact} exact match{'es' if n_exact != 1 else ''} for '{query}'"
        if len(hits) > n_exact:
            head += f" (+{len(hits) - n_exact} similar names)"
    else:
        head = (f"{len(hits)} match{'es' if len(hits) != 1 else ''} for '{query}' "
                f"(no exact name match — closest first)")
    trailer = []
    if len(hits) > len(shown_hits):
        trailer.append(f"... ({len(shown_hits)} of {len(hits)} — narrow with kind=, lang= or path=)")
    best = hits[0][5]
    if -hits[0][0] < 90 or hits[0][6] in DATA_LANGS:
        # No exact definition, or only a markup/config one: the answer may be
        # prose (docs, comments), which index_search cannot see.
        mentions = _text_mentions(index, q, keep)
        if mentions:
            trailer.append("(the text also appears in: " + mentions + " — index_text shows the lines)")
    if best_whole:
        trailer.append("(the best match's full source is shown above — nothing left to read)")
    else:
        # Callers make sense for code — including JavaScript inside an HTML page —
        # not for a config key or a CSS rule: decide by what the definition is.
        code_like = best.kind in _CALLABLE_KINDS or best.kind in code_nav.CONTAINER_KINDS
        callee = q if -hits[0][0] == 100 and best.depth == 0 and best.name == mod_name else best.name
        trailer.append(f'(next: read_symbol("{hits[0][3]}", "{best.qualname}") reads the best match'
                       + (f"; index_callers(\"{callee}\") shows who uses it)" if code_like else ")"))
    return _with_note("\n".join([head + ":"] + out + trailer), index)


def _line_owner(index: ProjectIndex, line: int, path: str | None, workdir: Path) -> str:
    """'Which definition is line N of this file in?' — one line of output."""
    if not path:
        return (f"ERROR: looking up line {line} needs the file it is in — pass it as path, "
                f"e.g. index_search(\"{line}\", path=\"src/app.py\")")
    p = safe_path(path, workdir)
    if isinstance(p, str):
        return p
    rel = index.rel(p)
    with index._lock:
        fid = index._by_path.get(rel) if rel else None
        e = index.files[fid] if fid is not None else None
    if e is None:
        return (f"ERROR: {path} is not an indexed file" + (" — pass the file, not a directory"
                                                          if p.is_dir() else ""))
    if not 1 <= line <= max(e.nlines, 1):
        return f"ERROR: line {line} is outside {path} (1-{e.nlines})"
    owner = code_nav.enclosing_symbol(e.symbols, line)
    if owner is None:
        return f"(line {line} of {e.path} is not inside any definition — it is at module level)"
    return (f"{e.path}:L{owner.start}-{owner.end}  {owner.kind} {owner.qualname}  | {owner.signature}\n"
            f'(next: read_symbol("{e.path}", "{owner.qualname}") reads it)')


_MAX_FIND = 100     # same cap as find_files


def index_find_files(pattern: str, directory: str | None, *, index: ProjectIndex,
                     cancel=None) -> str | None:
    """find_files answered from the index's file list: a bare glob ('*.py',
    'config*') matched against file names under `directory`.  None when no
    indexed file matches, so the caller can look on disk (images, git-ignored
    files); an ERROR string when the wait for the index was cancelled."""
    if (err := _ready(index, cancel)):
        return err
    keep = _path_filter(directory)
    with index._lock:
        hits = sorted((e for _, e in index._alive()
                       if keep(e.path) and fnmatch.fnmatch(e.path.rsplit("/", 1)[-1], pattern)),
                      key=lambda e: e.path)
    if not hits:
        return None
    lines = [f"{e.path}  ({e.lang or 'text'}, {e.nlines} lines"
             + (f", {len(e.symbols)} definitions" if e.symbols else "") + ")"
             for e in hits[:_MAX_FIND]]
    if len(hits) > _MAX_FIND:
        lines.append(f"... (first {_MAX_FIND} of {len(hits)} files — use a more specific pattern "
                     f"or directory)")
    return "\n".join(lines)
