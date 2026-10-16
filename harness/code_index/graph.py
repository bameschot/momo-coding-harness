"""The import graph and file ranking behind index_map (the project's most
imported files first) and index_file (one file's outline, imports and the
files that import it)."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .. import code_nav
from ..paths import MAX_SCAN_FILE_BYTES, safe_path
from .records import DATA_LANGS, _DECL_IMPORT_LANGS, lang_family
from .store import ProjectIndex
from .common import _path_filter, _ready, _scope, _with_note

# Definitions whose bare name says who uses them.  Methods are called through a
# receiver (`x.join(...)` is str.join as often as ProjectIndex.join) and module
# variables have names like `list` or `data`, so matching them by name ranks
# noise — index_map's graph leaves them out.
_RANK_KINDS = {"class", "interface", "struct", "enum", "trait", "type", "typedef", "union",
               "record", "object", "namespace", "module", "function", "constant", "macro"}

_HEADER_EXTS = (".h", ".hh", ".hpp", ".hxx", ".h++", ".inc")


def _import_graph(index: ProjectIndex) -> dict[int, dict[int, int]]:
    """{file: {file it imports: line of the import}}, cached per index version.

    Resolution is textual, in order: the imported names as modules (`from pkg
    import mod`); the longest dotted prefix that names a file
    (`com.acme.Money.of` -> Money.java, `shop::model::Item` -> model.rs); for
    Java/Kotlin, a top-level declaration of the imported name (`import
    shop.Product` -> the file declaring Product).  A C/C++ header include only
    matches headers, never the same-stem .c/.cpp.  A same-named module elsewhere
    can still show up."""
    cached = index._graph_cache
    if cached is not None and cached[0] == index.version:
        return cached[1]
    cands, fqns = _module_names(index)
    out: dict[int, dict[int, int]] = defaultdict(dict)
    for fid, _ in index._alive():
        e = index.files[fid]
        for imp in e.imports:
            targets = _relative_import(imp, e, index)
            if targets is None:
                targets = _resolve_import(imp, e.lang, cands, fqns, index)
            targets.discard(fid)
            for d in targets:
                out[fid].setdefault(d, imp.line)
    index._graph_cache = (index.version, out)
    return out


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


def _relative_import(imp, e: "FileEntry", index: ProjectIndex) -> set[int] | None:
    """Targets of an import written relative to the importing file — Python
    `from . import x` / `from ..pkg import y`, JS/TS `./x` / `../x`, C/C++
    `#include "x.h"` — resolved against that file's directory.  None when the
    import is not relative (or, for C, nothing sits next to the includer), so
    the textual resolver runs instead."""
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
        if rest:
            return find(f"{base}/{rest}.py", f"{base}/{rest}/__init__.py")
        out = set()
        for n in imp.names:                     # from . import utils, models
            out |= find(f"{base}/{n}.py", f"{base}/{n}/__init__.py")
        return out or find(f"{base}/__init__.py")
    if e.lang in code_nav._JS_LANGS and mod.startswith((".", "/")):
        return find(*(f"{here}/{mod}{ext}" for ext in _JS_EXTS))
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


def index_map(path: str | None = None, budget: int = 1500, *, workdir: Path,
              index: ProjectIndex | None = None, cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    try:
        budget = max(300, min(8000, int(budget)))
    except (TypeError, ValueError):
        budget = 1500
    path, err = _scope(path, workdir, index)
    if err:
        return err
    keep = _path_filter(path)
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
        example = e.symbols[0].qualname if e.symbols else None
    if example:
        out.append(f'(next: read_symbol("{e.path}", "{example}") reads one definition'
                   + (f"; index_callers(\"{example.rsplit('.', 1)[-1]}\") shows its users)"
                      if e.lang not in DATA_LANGS else ")"))
    return _with_note("\n".join(out), index)
