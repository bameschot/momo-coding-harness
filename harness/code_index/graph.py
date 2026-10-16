"""The import graph and file ranking behind index_map (the project's most
imported files first) and index_file (one file's outline, imports and the
files that import it)."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from .. import code_nav
from ..paths import MAX_SCAN_FILE_BYTES, safe_path
from .records import DATA_LANGS, _DECL_IMPORT_LANGS, _WORD_STR, _trigram_ids, lang_family
from .resolve import JsConfig, PyModules, is_stdlib, rust_candidates
from .store import ProjectIndex
from .common import _is_test, _path_filter, _read_source, _ready, _scope, _with_note
from .text import _text_candidates

# Definitions whose bare name says who uses them.  Methods are called through a
# receiver (`x.join(...)` is str.join as often as ProjectIndex.join) and module
# variables have names like `list` or `data`, so matching them by name ranks
# noise — index_map's graph leaves them out.
_RANK_KINDS = {"class", "interface", "struct", "enum", "trait", "type", "typedef", "union",
               "record", "object", "namespace", "module", "function", "constant", "macro"}

_HEADER_EXTS = (".h", ".hh", ".hpp", ".hxx", ".h++", ".inc")


def _import_graph(index: ProjectIndex) -> dict[int, dict[int, int]]:
    """{file: {file it imports: line of the import}}, cached per index version:
    every import's resolved targets (_resolved_imports), plus, for a name
    imported through a re-export (`from pkg import f` where pkg/__init__.py got
    f from .impl), the file that defines it."""
    cached = index._graph_cache
    if cached is not None and cached[0] == index.version:
        return cached[1]
    resolved = _resolved_imports(index)
    out: dict[int, dict[int, int]] = defaultdict(dict)
    for fid, imps in resolved.items():
        for imp, targets in imps:
            for d in targets:
                out[fid].setdefault(d, imp.line)
            for n in _bound_names(imp):
                for t in targets:
                    d = _definer(index, resolved, t, n)
                    if d is not None and d != fid:
                        out[fid].setdefault(d, imp.line)
    index._graph_cache = (index.version, out)
    return out


def _resolved_imports(index: ProjectIndex) -> dict[int, list]:
    """{file: [(Import, {target file ids})]}, cached per index version.

    In order: imports written relative to the file (Python dots, JS ./x, C
    "x.h"); Python's own rules over the project's import roots (resolve.py),
    where a standard-library module is never a project file; then a textual
    match — the imported names as modules (`from pkg import mod`), the longest
    dotted prefix that names a file (`com.acme.Money.of` -> Money.java,
    `shop::model::Item` -> model.rs), for Java/Kotlin a top-level declaration of
    the imported name.  A C/C++ header include only matches headers.  A Python
    import no root explains (a folder put on sys.path at run time) keeps a
    textual match only when it names exactly one file."""
    cached = index._imports_cache
    if cached is not None and cached[0] == index.version:
        return cached[1]
    cands, fqns = _module_names(index)
    py = PyModules(e.path for _, e in index._alive() if e.lang == "python")
    js = JsConfig([e.path for _, e in index._alive() if e.lang == "json"], lambda rel: _read(index, rel)) \
        if any(e.lang in code_nav._JS_LANGS for _, e in index._alive()) else None
    by_path = index._by_path
    out: dict[int, list] = {}
    for fid, e in index._alive():
        rows = []
        for imp in e.imports:
            targets = _relative_import(imp, e, index, js)
            if targets is None and e.lang == "python" and imp.module != "__future__":
                hit = py.resolve(e.path, imp.module, imp.names)
                if hit is not None:
                    targets = {by_path[r] for r in hit}
                elif is_stdlib(imp.module):
                    targets = set()
                else:
                    t = _resolve_import(imp, e.lang, cands, fqns, index)
                    targets = t if len(t) == 1 else set()
            if targets is None:
                targets = _resolve_import(imp, e.lang, cands, fqns, index)
            targets.discard(fid)
            rows.append((imp, targets))
        out[fid] = rows
    index._imports_cache = (index.version, out)
    return out


def _hierarchy(index: ProjectIndex) -> tuple[dict, dict]:
    """({type name: its direct bases}, {type name: [(FileEntry, Symbol) that
    extend or implement it]}), cached per index version.  A Rust `impl Trait
    for T` counts as T implementing Trait."""
    cached = index._hierarchy_cache
    if cached is not None and cached[0] == index.version:
        return cached[1]
    bases: dict[str, set[str]] = defaultdict(set)
    subs: dict[str, list] = defaultdict(list)
    for _, e in index._alive():
        if not e.lang or e.lang in DATA_LANGS:
            continue
        for s in e.symbols:
            if s.bases and s.kind in code_nav.CONTAINER_KINDS:
                bases[s.name].update(s.bases)
                for b in s.bases:
                    subs[b].append((e, s))
    index._hierarchy_cache = (index.version, (bases, subs))
    return bases, subs


def supertypes(index: ProjectIndex, name: str, depth: int = 6) -> set[str]:
    """Every type `name` extends or implements, transitively (lock held)."""
    bases, _ = _hierarchy(index)
    out, todo = set(), [(name, 0)]
    while todo:
        n, d = todo.pop()
        for b in bases.get(n, ()):
            if b not in out and b != name and d < depth:
                out.add(b)
                todo.append((b, d + 1))
    return out


def subtypes(index: ProjectIndex, name: str, family: str | None = None, depth: int = 6) -> list:
    """[(FileEntry, Symbol)] of the types that extend or implement `name`,
    transitively, nearest first; only `family`'s languages when given (lock held)."""
    _, subs = _hierarchy(index)
    out, seen, todo = [], {name}, [(name, 0)]
    while todo:
        n, d = todo.pop(0)
        for e, s in subs.get(n, ()):
            if family is not None and lang_family(e.lang) != family:
                continue
            key = (e.path, s.qualname, s.start)
            if key in seen:
                continue
            seen.add(key)
            out.append((e, s))
            if s.name not in seen and d < depth:
                seen.add(s.name)
                todo.append((s.name, d + 1))
    return out


_ABSTRACT_KINDS = {"interface", "trait"}
_MAX_SUBTYPES = 8


def _places(items: list[str]) -> str:
    more = f" and {len(items) - _MAX_SUBTYPES} more" if len(items) > _MAX_SUBTYPES else ""
    return ", ".join(items[:_MAX_SUBTYPES]) + more


def subtype_line(index: ProjectIndex, name: str, kind: str, lang: str | None) -> str:
    """'Implemented by: Circle (shapes.py:L3-9), ...' for an interface or
    trait, 'Subclasses: ...' for a class; '' when nothing extends it."""
    with index._lock:
        subs = subtypes(index, name, lang_family(lang))
    if not subs:
        return ""
    label = "Implemented by" if kind in _ABSTRACT_KINDS else "Subclasses"
    return f"{label}: " + _places([f"{s.qualname} ({e.path}:L{s.start}-{s.end})" for e, s in subs])


def override_line(index: ProjectIndex, cls: str, member: str, lang: str | None) -> str:
    """'Overridden in: Circle.area (shapes.py:L5-6), ...' — or 'Implemented
    in:' when `cls` is an interface or trait — for cls.member."""
    with index._lock:
        kinds = {s.kind for f, i in index._sym_by_name.get(cls.lower(), ())
                 if index.files[f] is not None and (s := index.files[f].symbols[i]).name == cls}
        subs = {s.name for _, s in subtypes(index, cls, lang_family(lang))}
        found = []
        for f, i in index._sym_by_name.get(member.lower(), ()):
            e = index.files[f]
            if e is None:
                continue
            s = e.symbols[i]
            if s.name == member and "." in s.qualname and s.qualname.rsplit(".", 2)[-2] in subs:
                found.append(f"{s.qualname} ({e.path}:L{s.start}-{s.end})")
    if not found:
        return ""
    label = "Implemented in" if kinds & _ABSTRACT_KINDS else "Overridden in"
    return f"{label}: " + _places(sorted(found))


def _read(index: ProjectIndex, rel: str) -> str | None:
    try:
        return (index.root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _bound_names(imp) -> list[str]:
    """The names an import binds from its module (`from m import a, b`, JS
    `import {a} from`), not a module itself; aliases map back to the original."""
    alias_of = {alias for _, alias in imp.aliases}
    return [n for n in imp.names if n != "*" and n not in alias_of]


def _star(imp) -> bool:
    return "*" in imp.names or imp.text.startswith("export *")


def _definer(index: ProjectIndex, resolved: dict, fid: int, name: str, depth: int = 3):
    """The file that defines `name` as module `fid` exposes it: fid itself, or
    — through `from .x import name` / `export {name} from './x'` / a star
    re-export — the file it came from, up to three hops.  None if neither."""
    e = index.files[fid]
    if e is None:
        return None
    if any(s.depth == 0 and s.name == name for s in e.symbols):
        return fid
    if depth == 0 or e.lang not in ("python", *code_nav._JS_LANGS):
        return None
    for imp, targets in resolved.get(fid, ()):
        orig = next((o for o, a in imp.aliases if a == name), name)
        if orig in imp.names or _star(imp):
            for t in targets:
                d = _definer(index, resolved, t, orig, depth - 1)
                if d is not None:
                    return d
    return None


def _module_names(index: ProjectIndex) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """(module name -> code files an import of it could mean, e.g. 'code_nav',
    'harness.code_nav'; Java/Kotlin 'package.Name' -> the declaring files),
    cached per index version."""
    cached = index._modules_cache
    if cached is not None and cached[0] == index.version:
        return cached[1]
    cands: dict[str, set[int]] = defaultdict(set)
    fqns: dict[str, set[int]] = defaultdict(set)    # "com.acme.Money" -> Money.java
    for fid, e in index._alive():
        if not e.lang or e.lang in DATA_LANGS:
            continue
        for c in code_nav.module_candidates_rel(e.path)[1]:
            cands[c].add(fid)
        if e.package:
            fqns[e.package + ".*"].add(fid)
            for sym in e.symbols:
                if sym.depth == 0:
                    fqns[f"{e.package}.{sym.name}"].add(fid)
        elif e.lang in _DECL_IMPORT_LANGS:
            for sym in e.symbols:           # the default package: `import CdpClient`
                if sym.depth == 0:
                    fqns[sym.name].add(fid)
    index._modules_cache = (index.version, (cands, fqns))
    return cands, fqns


_JS_EXTS = ("", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx", ".d.ts",
            "/index.js", "/index.ts", "/index.tsx", "/index.jsx")


def _relative_import(imp, e: "FileEntry", index: ProjectIndex, js: JsConfig | None = None
                     ) -> set[int] | None:
    """Targets of an import resolved from where the importing file is — Python
    `from . import x` / `from ..pkg import y`, JS/TS `./x` / `../x` and what
    tsconfig paths / baseUrl / a local package name make of a bare specifier,
    Rust `crate::` / `self::` / `super::` / `mod foo;`, C/C++ `#include "x.h"`.
    None when the import is none of these (or nothing matches), so the textual
    resolver runs instead."""
    here = Path(e.path).parent
    by_path = index._by_path

    def find(*cands: str) -> set[int]:
        out = set()
        for c in cands:
            rel = Path(c).as_posix()
            parts = []
            for part in rel.split("/"):
                if part == "..":
                    if not parts:
                        return set()
                    parts.pop()
                elif part not in ("", "."):
                    parts.append(part)
            fid = by_path.get("/".join(parts))
            if fid is not None:
                out.add(fid)
        return out

    mod = imp.module
    if e.lang == "python" and mod.startswith("."):
        dots = len(mod) - len(mod.lstrip("."))
        base = here
        for _ in range(dots - 1):
            base = base.parent
        rest = mod.lstrip(".").replace(".", "/")
        pkg = f"{base}/{rest}" if rest else f"{base}"
        # `from .web import tls` / `from . import utils`: a name that is a
        # module means that module; any other name means the package's file.
        out, loose = set(), not imp.names
        for n in imp.names:
            sub = find(f"{pkg}/{n}.py", f"{pkg}/{n}/__init__.py") if n != "*" else set()
            out |= sub
            loose = loose or not sub
        if loose or not out:
            out |= find(f"{pkg}.py", f"{pkg}/__init__.py") if rest else find(f"{base}/__init__.py")
        return out
    if e.lang in code_nav._JS_LANGS and mod.startswith((".", "/")):
        return find(*(f"{here}/{mod}{ext}" for ext in _JS_EXTS))
    if e.lang in code_nav._JS_LANGS and js is not None:
        for base in js.candidates(e.path, mod):
            hit = find(*(f"{base}{ext}" for ext in _JS_EXTS))
            if hit:
                return hit
        return None
    if e.lang == "rust":
        cands = rust_candidates(e.path, mod.replace(" ", ""), imp.names, by_path)
        return None if cands is None else find(*cands)
    if e.lang in ("c", "cpp") and '"' in imp.text:
        hit = find(f"{here}/{mod}")
        return hit or None                      # else: an -I include path, matched textually
    return None


def _resolve_import(imp, lang: str | None, cands, fqns, index: ProjectIndex) -> set[int]:
    norm = code_nav._norm_module(imp.module)
    parts = norm.split(".") if norm else []
    targets: set[int] = set()
    if lang in _DECL_IMPORT_LANGS:
        # `import com.acme.Money.of` / `import shop.Product` / `import a.b.*`:
        # the longest prefix that is a declared package.name wins.
        raw = imp.module.replace("::", ".")
        if raw.endswith(".*"):
            targets = set(fqns.get(raw, ()))
        for k in range(len(parts), 0, -1):
            if targets:
                break
            targets = set(fqns.get(".".join(parts[:k]), ()))
        if fqns:
            # The project declares packages, so an import none of them covers
            # is a library's (`java.util.List`), not a file named List or util
            # — unless a file's path spells it out: maestro/drivers/AndroidDriver.kt
            # for `maestro.drivers.AndroidDriver` (its class did not parse).
            for k in range(len(parts), 1, -1):
                if targets:
                    break
                targets = {d for d in cands.get(".".join(parts[:k]), ())
                           if index.files[d].lang in _DECL_IMPORT_LANGS}
            return targets
    # `from harness import tools` imports the module tools, not the package.
    for n in ([] if targets else imp.names):
        targets |= cands.get(f"{norm}.{n}" if norm else n, set())
    if not targets and parts:
        for k in range(len(parts), 0, -1):          # longest prefix first
            for s0 in range(k):                     # then its longest suffix
                hit = cands.get(".".join(parts[s0:k]))
                if hit:
                    targets = set(hit)
                    break
            if targets:
                break
    if lang in ("c", "cpp") and imp.module.lower().endswith(_HEADER_EXTS):
        targets = {d for d in targets if index.files[d].path.lower().endswith(_HEADER_EXTS)}
    # `import java.util.List` is not util.c; a Kotlin build script is not vite.config.ts.
    fam = lang_family(lang)
    return {d for d in targets if lang_family(index.files[d].lang) == fam
            or (fam == "js" and index.files[d].lang in ("css", "json"))}


def _ranks(index: ProjectIndex):
    """(file rank {fid: score}, used-by {fid: n files}, symbol use {(fid, i): n files}),
    cached per index version.

    A file is "used by" the files that import it; each of its top-level
    definitions those importers also mention adds weight.  Counting a bare name
    anywhere in the project instead ranked noise: `compile`, `split` or `parse`
    are mentioned everywhere through re.compile and str.split."""
    cached = index._rank_cache
    if cached is not None and cached[0] == index.version:
        return cached[1]
    alive = [fid for fid, _ in index._alive()]
    imports = _import_graph(index)
    importers: dict[int, set[int]] = defaultdict(set)
    for r, targets in imports.items():
        for d in targets:
            importers[d].add(r)
    edges: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    sym_use: dict[tuple[int, int], int] = {}
    for d, rs in importers.items():
        for r in rs:
            edges[r][d] += 1.0
        if not index.components["idents"]:
            continue
        for i, s in enumerate(index.files[d].symbols):
            if s.depth != 0 or s.kind not in _RANK_KINDS or len(s.name) < 3:
                continue
            users = index._ref_files(s.name) & rs
            if users:
                sym_use[(d, i)] = len(users)
                for r in users:
                    edges[r][d] += 1.0
    used_by = importers
    n = len(alive) or 1
    rank = {fid: 1.0 / n for fid in alive}
    for _ in range(20):
        nxt = {fid: 0.15 / n for fid in alive}
        dangling = 0.0
        for fid in alive:
            out = edges.get(fid)
            if not out:
                dangling += rank[fid]
                continue
            tot = sum(out.values())
            for d, w in out.items():
                if d in nxt:
                    nxt[d] += 0.85 * rank[fid] * w / tot
        share = 0.85 * dangling / n
        for fid in nxt:
            nxt[fid] += share
        rank = nxt
    result = (rank, {k: len(v) for k, v in used_by.items() if v}, sym_use)
    index._rank_cache = (index.version, result)
    return result


# ── unused definitions ───────────────────────────────────────────────────────
# Top-level definitions nothing else in the project names: not as code (the
# occurrence index) and not as text in code or config (a registry, getattr,
# __all__, a route table).  Methods are not checked: a call through an object
# cannot be tied to one class.  Entry points and public API are left out.

_ENTRY_NAMES = {"main", "setUp", "tearDown", "setUpClass", "tearDownClass", "setUpModule",
                "tearDownModule", "conftest"}
_UNUSED_KINDS = (code_nav.CALLABLE_KINDS | code_nav.CONTAINER_KINDS
                 | {"constant", "variable", "macro", "typedef", "type"}) - {"impl"}
_MAX_UNUSED_CHECKED = 400   # candidates whose text mentions are looked for


def _unused(index: ProjectIndex, keep) -> list | None:
    """[(FileEntry, Symbol, note)] of the top-level definitions under `keep`
    that nothing else refers to; None without the occurrence index."""
    with index._lock:
        if not index.components["idents"]:
            return None
        langs = {e.path: e.lang for _, e in index._alive()}
        cands = []
        for fid, e in index._alive():
            if not e.lang or e.lang in DATA_LANGS or not keep(e.path) or _is_test(e.path):
                continue
            for s in e.symbols:
                n = s.name
                if (s.depth != 0 or s.kind not in _UNUSED_KINDS or len(n) < 3 or n in _ENTRY_NAMES
                        or (n.startswith("__") and n.endswith("__")) or n.startswith("test")
                        or (e.lang in code_nav._JS_LANGS and s.signature.startswith("export"))):
                    continue
                occ = index._occurrences(n)
                if any(f != fid for f in occ) or set(occ.get(fid, ())) - {s.name_line}:
                    continue
                cands.append((e, s))
    out = []
    for e, s in cands[:_MAX_UNUSED_CHECKED]:
        word = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(s.name)}(?![A-Za-z0-9_])")
        files, _ = _text_candidates(index, _trigram_ids(_WORD_STR.findall(s.name.lower())),
                                    lambda rel: langs.get(rel) is not None)
        if any(_names_as_text(index, rel, word, s.name_line if rel == e.path else 0)
               for rel, _syms in files):
            continue
        decorated = e.lang == "python" and s.start < s.name_line
        out.append((e, s, "decorated — may be registered" if decorated else ""))
    return out


def _names_as_text(index: ProjectIndex, rel: str, word, skip_line: int) -> bool:
    try:
        text = _read_source(index.root / rel)
    except OSError:
        return False
    return any(word.search(line) for ln, line in enumerate(text.split("\n"), 1) if ln != skip_line)


def _unused_map(index: ProjectIndex, path: str | None, keep, budget: int) -> str:
    found = _unused(index, keep)
    scope = f" under {path}" if path else ""
    if found is None:
        return _with_note("(the identifier index was dropped for memory, so unused definitions "
                          "cannot be listed — /index-max-mem raises the budget)", index)
    if not found:
        return _with_note(f"No unused top-level definitions{scope}: everything is referred to "
                          f"somewhere in the project.", index)
    by_file: dict[str, list] = defaultdict(list)
    for e, s, note in found:
        by_file[e.path].append((s, note))
    out = [f"{len(found)} unused top-level definition{'s' if len(found) != 1 else ''}{scope} — "
           f"nothing in the project names them, in code or as text in code and config:"]
    size, limit = len(out[0]), budget * 4
    for rel in sorted(by_file):
        block = [rel] + [f"  {s.kind} {s.qualname} (L{s.start}-{s.end})" + (f" — {note}" if note else "")
                         for s, note in by_file[rel]]
        cost = sum(len(b) + 1 for b in block)
        if size + cost > limit and len(out) > 1:
            out.append(f"... more in {len(by_file) - (len(out) - 1)} files — pass path= or a larger budget")
            break
        out.extend(block)
        size += cost
    out.append("(Not checked: methods, dunder names, main, tests, JS exports. A dynamic use the "
               "project spells nowhere — plugins, reflection, another repository — is not seen.)")
    return _with_note("\n".join(out), index)


def index_map(path: str | None = None, budget: int = 1500, show: str = "files", *, workdir: Path,
              index: ProjectIndex | None = None, cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    try:
        budget = max(300, min(8000, int(budget)))
    except (TypeError, ValueError):
        budget = 1500
    show = (show or "files").strip().lower()
    if show not in ("files", "unused"):
        return f"ERROR: show must be 'files' or 'unused', got: {show}"
    path, err = _scope(path, workdir, index)
    if err:
        return err
    keep = _path_filter(path)
    if show == "unused":
        return _unused_map(index, path, keep, budget)
    with index._lock:
        rank, used_by, sym_use = _ranks(index)
        entries = [(fid, e) for fid, e in index._alive() if keep(e.path)]
        # Most importers first — what the header promises; PageRank breaks ties,
        # so a file imported by central files beats one imported by leaves.
        entries.sort(key=lambda fe: (-used_by.get(fe[0], 0), -rank.get(fe[0], 0), fe[1].path))
        limit = budget * 4
        out = []
        head = (f"Project map{f' of {path}' if path and path not in ('.', './') else ''} — "
                f"{len(entries)} files, the most imported first:")
        size = len(head)
        shown = 0
        for fid, e in entries:
            lang = e.lang or "text"
            ub = used_by.get(fid, 0)
            line = f"{e.path} ({lang}, {e.nlines} lines)" + (f" — imported by {ub} files" if ub else "")
            block = [line]
            if e.lang in DATA_LANGS:
                tops = [s.name for s in e.symbols if s.depth == 0][:8]
                if tops:
                    block.append("  " + ", ".join(tops))
            elif e.symbols:
                tops = [(sym_use.get((fid, i), 0), i, s) for i, s in enumerate(e.symbols) if s.depth <= 1]
                tops.sort(key=lambda t: (-t[0], t[1]))
                for n_use, _i, s in tops[:5]:
                    block.append(f"  {s.kind} {s.qualname} (L{s.start})"
                                 + (f" · used in {n_use} files" if n_use else ""))
            cost = sum(len(b) + 1 for b in block)
            if size + cost > limit and shown:
                break
            out.extend(block)
            size += cost
            shown += 1
    trailer = []
    if shown < len(entries):
        trailer.append(f"... {len(entries) - shown} more files — index_map(path=\"<dir>\") zooms "
                       f"in, or pass a larger budget")
    trailer.append("(next: index_file(\"<path>\") shows one file's outline, imports and users)")
    return _with_note("\n".join([head] + out + trailer), index)


_SKIP_REASONS = {"binary": "a binary file",
                 "too_big": f"over {MAX_SCAN_FILE_BYTES // 1_000_000} MB",   # decimal, like everywhere else
                 "budget": "left out: the memory budget is reached (/index-max-mem)",
                 "error": "it could not be read or parsed"}


def _left_out(index: ProjectIndex, rel: str | None) -> str:
    """Why an existing file is not indexed (lock held)."""
    skip = index._skipped.get(rel) if rel else None
    if skip is not None:
        return _SKIP_REASONS.get(skip[0], skip[0])
    if rel and rel in index._beyond:
        return f"past the {index.max_files:,}-file limit (/index-max-files)"
    if rel is None or index._rules.excluded(rel):
        return "excluded by the index filter (/index-filter)"
    return "not indexed"


def index_file(path: str, *, workdir: Path, index: ProjectIndex | None = None, cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    p = safe_path(path, workdir)
    if isinstance(p, str):
        return p
    rel = index.rel(p)
    with index._lock:
        fid = index._by_path.get(rel) if rel else None
        if fid is None:
            if p.is_dir():
                return f"ERROR: {path} is a directory — use index_map(path=\"{path}\")"
            if not p.exists():
                return f"ERROR: file not found: {path}"
            return f"ERROR: {path} is not in the code index ({_left_out(index, rel)}) — use read_file"
        e = index.files[fid]
        rank, used_by, sym_use = _ranks(index)
        out = [f"{e.path} ({e.lang or 'text'}, {e.nlines} lines, {len(e.symbols)} definitions"
               + (f", imported by {used_by[fid]} files" if used_by.get(fid) else "") + ")"]
        if e.has_error:
            out.append(code_nav._ERROR_NOTE)
        if e.symbols:
            out.append("Outline:")
            shown = [s for s in e.symbols if s.depth <= 1]
            for s in shown[:60]:
                out.append(f"  {'  ' * s.depth}L{s.start}-{s.end}  {code_nav._line_label(s)}")
            if len(shown) > 60 or len(shown) < len(e.symbols):
                out.append(f"  ... ({len(e.symbols)} definitions in all — code_outline(\"{e.path}\") "
                           f"lists every one)")
        if e.imports:
            mods = []
            for imp in e.imports[:30]:
                mods.append(imp.module + (f" ({', '.join(imp.names[:6])}"
                                          f"{', ...' if len(imp.names) > 6 else ''})" if imp.names else ""))
            out.append(f"Imports ({len(e.imports)}): " + "; ".join(mods)
                       + (" ..." if len(e.imports) > 30 else ""))
        if e.lang and e.lang not in DATA_LANGS:
            graph = _import_graph(index)
            importers = sorted(((ofid, f"{index.files[ofid].path}:L{targets[fid]}")
                                for ofid, targets in graph.items() if fid in targets),
                               key=lambda t: t[1])
            if not importers:
                out.append("Imported by: (none found — matched on import text)")
            else:
                # Which of this file's definitions each importer uses, so "who
                # uses what" is one call instead of a grep per importer.
                tops = [s for s in e.symbols if s.depth == 0 and len(s.name) >= 3]
                refs = {s.name: index._ref_files(s.name) for s in tops} \
                    if index.components["idents"] else {}
                out.append(f"Imported by ({len(importers)}) — and the definitions each one uses:")
                for ofid, where in importers[:20]:
                    own = {s.name for s in index.files[ofid].symbols if s.depth == 0}
                    names = [s.name for s in tops if s.name not in own
                             and ofid in refs.get(s.name, ())]
                    out.append(f"  {where}" + (f" — uses {', '.join(names[:10])}"
                                               + (" ..." if len(names) > 10 else "") if names else ""))
                if len(importers) > 20:
                    out.append(f"  ... {len(importers) - 20} more")
            used = sorted(((n, i) for (f, i), n in sym_use.items() if f == fid), reverse=True)[:8]
            if used:
                out.append("Most used definitions (files that reference the name):")
                for n, i in used:
                    s = e.symbols[i]
                    out.append(f"  {s.kind} {s.qualname} (L{s.start}) — {n} files")
            if not _is_test(e.path):
                tops = [s.name for s in e.symbols if s.depth == 0 and len(s.name) >= 3]
                tests = {index.files[o].path for o, _w in importers} | (
                    {index.files[f].path for n in tops for f in index._ref_files(n)}
                    if index.components["idents"] else set())
                tests = sorted(t for t in tests if _is_test(t) and t != e.path)
                out.append("Tests that reference this file: " + (", ".join(tests[:10])
                           + (f" and {len(tests) - 10} more" if len(tests) > 10 else "") if tests
                           else "none"))
        example = e.symbols[0].qualname if e.symbols else None
    if e.lang and e.lang not in DATA_LANGS and (unused := _unused(index, lambda rel: rel == e.path)):
        out.append("Unused in the project: " + ", ".join(
            s.qualname + (" (decorated — may be registered)" if note else "") for _e, s, note in unused[:15])
            + (" ..." if len(unused) > 15 else ""))
    if example:
        out.append(f'(next: read_symbol("{e.path}", "{example}") reads one definition'
                   + (f"; index_callers(\"{example.rsplit('.', 1)[-1]}\") shows its users)"
                      if e.lang not in DATA_LANGS else ")"))
    return _with_note("\n".join(out), index)
