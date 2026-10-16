"""index_callers: the uses of a definition, classified by syntax (call,
import, type, other) and narrowed by class, module, receiver type and
language family — then, with depth, who uses those."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .. import code_nav
from ..paths import split_lines
from .records import DATA_LANGS, _WORD_STR, _trigram_ids, lang_family
from .store import ProjectIndex
from .common import (_MAX_CALLERS_CHARS, _MAX_CALLER_GROUPS, _MAX_EXPAND, _MAX_EXPAND_FILES,
                     _MAX_EXPAND_GROUPS, _MAX_LINES_PER_GROUP, _defs_named, _is_code_symbol,
                     _is_test, _read_source, _ready, _with_note)
from .text import _text_candidates, _text_mentions
from .graph import (_bound_names, _definer, _import_graph, _module_names, _resolved_imports, _star,
                    override_line, supertypes)

# Declared types that say nothing about the object: generics and "any".
_OPEN_TYPES = {"Any", "any", "object", "Object", "unknown", "dynamic", "Self", "self"}


def _other_type(index: ProjectIndex, rtype: str | None, cls: str) -> bool:
    """The receiver's declared type rules out `cls`.  Inheritance counts both
    ways: a `Circle c` is a Shape, and a `Shape s` / `Discount d` may BE the
    Circle / Half being asked about (virtual dispatch), so neither is dropped.
    Only an unrelated type rules it out.  Unknown, generic (`T`) and any-typed
    receivers never do."""
    if not rtype or rtype in _OPEN_TYPES or (len(rtype) <= 2 and rtype.isupper()):
        return False
    if _is_subtype(index, cls, rtype):
        return False                    # the receiver's type is a base of cls
    if _is_project_class(index, rtype):
        return not _is_subtype(index, rtype, cls)
    return True


def _is_project_class(index: ProjectIndex, t: str) -> bool:
    return any(s.kind in code_nav.CONTAINER_KINDS for _, s in _defs_named(index, t))


def _is_subtype(index: ProjectIndex, t: str, target: str, depth: int = 4) -> bool:
    """`t` is `target` or (transitively) extends / implements it, by the bases
    the declarations list (`class FlatPricer extends Pricer`, `class Circle :
    public Shape`, `impl Shape for Circle`).  A type not defined in the project
    is not a subtype.  A declaration in a file with syntax errors may have lost
    its bases: its signature line is read instead."""
    if t == target:
        return True
    with index._lock:
        if target in supertypes(index, t):
            return True
    if depth == 0:
        return False
    sigs = [(e.lang, s.signature) for e, s in _defs_named(index, t)
            if s.kind in code_nav.CONTAINER_KINDS and e.has_error and not s.bases]
    for lang, sig in sigs:
        rest = sig.split(t, 1)[-1]
        if lang != "python":
            rest = _drop_params(rest)       # Kotlin `class Box(val item: Circle) : Shape()`
        supers = [w for w in re.findall(r"[A-Za-z_]\w*", rest) if w != t]
        if target in supers or any(_is_subtype(index, w, target, depth - 1)
                                   for w in supers if w[:1].isupper()):
            return True
    return False


_CTOR_PARAMS = re.compile(r"\s*(?:<[^()]*>)?\s*(?:(?:private|protected|internal|public)\s+)?"
                          r"(?:@\w+\s+)*(?:constructor\s*)?\(")


def _drop_params(rest: str) -> str:
    """'(val item: Circle) : Shape()' -> ' : Shape()': the constructor parameters
    right after a class name (Kotlin, Java records) are not its bases.  Python's
    parentheses ARE the bases, so Python never comes here."""
    m = _CTOR_PARAMS.match(rest)
    if not m:
        return rest
    depth = 0
    for k in range(m.end() - 1, len(rest)):
        if rest[k] == "(":
            depth += 1
        elif rest[k] == ")":
            depth -= 1
            if depth == 0:
                return rest[:m.start()] + rest[k + 1:]
    return ""


def query_target(index: ProjectIndex, name: str) -> tuple[str, str | None, str | None]:
    """(bare name, receiver filter, class) for a callers query.  'Harness.send'
    names a method of the class Harness: its callers call it on self/harness/...,
    so the class is a scope, not a receiver filter.  'JSON.parse' is a filter."""
    dotted = name.strip().replace("::", ".")
    bare = dotted.rsplit(".", 1)[-1]
    if "." not in dotted:
        return bare, None, None
    qualifier = dotted.rsplit(".", 1)[0]
    if _module_scope(index, qualifier, bare) is not None:
        return bare, qualifier, None        # a module that defines it: `harness.tools.read_text`
    last = qualifier.rsplit(".", 1)[-1]
    classes = [s for _, s in _defs_named(index, last, exact=False)
               if s.kind in code_nav.CONTAINER_KINDS]
    owners = {s.qualname.rsplit(".", 1)[0].rsplit(".", 1)[-1]
              for _, s in _defs_named(index, bare) if "." in s.qualname}
    # A class that defines the member, in any letter case (`harness.send` is
    # Harness.send), or one spelled exactly; a test class `Tools` does not make
    # `harness.tools.read_text` a method.
    if any(s.name in owners or s.name == last for s in classes):
        return bare, None, qualifier
    return bare, qualifier, None


def uses_of(index: ProjectIndex, bare: str, want_recv: str | None, cancel,
          cls: str | None = None, module: "_Module | None" = None) -> tuple[list, list]:
    """All uses of `bare`: ([(rel, row1, role, recv, owner, line_text)], defs [(rel, Symbol)]).
    With `cls` (a query for cls.bare), drop what syntax shows is another object's
    method: a call on `self.attr` / `this.attr`, or a bare call inside a class
    that defines `bare` itself.  With `module` (a query for module.bare), keep
    only uses that reach that module.  Otherwise, drop calls on a receiver whose
    import or declared type is outside the project (`ast.parse`, `json.loads`)."""
    cls_last = cls.rsplit(".", 1)[-1] if cls else None
    named = [(e.lang, s) for e, s in _defs_named(index, bare) if _is_code_symbol(e.lang, s)]
    # The definitions asked about: for Cart.total only Cart's, not Report.total.
    meant = [(lang, s) for lang, s in named
             if not cls_last or s.qualname == f"{cls_last}.{bare}"
             or s.qualname.endswith(f".{cls_last}.{bare}")] or named
    # A Python method is never called from JavaScript: when every definition
    # meant is in one language family, uses in other languages are other names.
    fams = {lang_family(lang) for lang, _ in meant}
    family = fams.pop() if len(fams) == 1 else None
    named = [s for _, s in named]
    owners_of_name = {s.qualname.rsplit(".", 1)[0] for s in named}
    # Every definition of the name is top-level (a function, not a method), so
    # `self.total` / `this.total` is some object's attribute, never a use of it.
    only_top_level = bool(named) and all(s.depth == 0 for s in named)
    owner_classes = {s.qualname.rsplit(".", 2)[-2] for _, s in meant if "." in s.qualname}
    unscoped = not cls and not want_recv and module is None
    # `from m import f as g`: in that file, g is f.
    aliases = module.aliases if module is not None else _name_aliases(index, bare) if unscoped else {}
    with index._lock:
        if index.components["idents"]:
            occ = index._occurrences(bare)
            targets = [(e.path, set(lines), bare) for fid, lines in occ.items()
                       if (e := index.files[fid]) is not None and e.lang
                       and (family is None or lang_family(e.lang) == family)]
            for rel, names in aliases.items():
                fid = index._by_path.get(rel)
                for a in names:
                    if fid is not None and (lines := index._occurrences(a).get(fid)):
                        targets.append((rel, set(lines), a))
        else:
            targets = [(e.path, None, bare) for _, e in index._alive()
                       if e.lang and (e.lang not in DATA_LANGS or e.lang == "html")
                       and (family is None or lang_family(e.lang) == family)]
            targets += [(rel, None, a) for rel, names in aliases.items() for a in names]
    uses, defs = [], []
    parsed_rel, parsed = None, None
    for i, (rel, rows, name) in enumerate(sorted(targets, key=lambda t: (t[0], t[2]))):
        if cancel is not None and i % 20 == 0 and cancel.is_set():
            break
        if rel != parsed_rel:
            parsed_rel = rel
            try:
                parsed = code_nav.parse(index.root / rel, cache=False)
            except (OSError, ImportError):
                parsed = None
        if parsed is None:
            continue
        found = code_nav.references_in(parsed, name, {r - 1 for r in rows} if rows else None)
        for r in sorted(found):
            role, recv, rtype = found[r]
            if role == "def":
                sym = next((s for s in parsed.symbols if s.name == name and s.name_line == r + 1), None)
                if sym is not None:
                    defs.append((rel, sym))
                continue
            if role in code_nav.NOT_USES:
                continue
            if want_recv and recv != want_recv:
                continue
            if only_top_level and recv and recv.split(".", 1)[0] in ("self", "this", "cls"):
                continue
            owner = code_nav.enclosing_symbol(parsed.symbols, r + 1)
            if cls_last and role == "call":
                if recv and recv.startswith(("self.", "this.")):
                    continue            # an attribute of self is a different object
                if recv and recv.split("(", 1)[0] == "super" and owner is not None \
                        and cls_last in owner.qualname.split(".")[:-1]:
                    continue            # super().m() in C calls C's base, not C.m
                if _other_type(index, rtype, cls_last):
                    continue            # `Cart cart; cart.add()` is not Money.add
                if recv is None and owner is not None:
                    scopes = owner.qualname.split(".")[:-1]
                    here = next((".".join(scopes[:k]) for k in range(len(scopes), 0, -1)
                                 if ".".join(scopes[:k]) in owners_of_name), None)
                    if here and here.rsplit(".", 1)[-1] != cls_last:
                        continue        # a bare call to the enclosing class's own method
            if module is not None and not module.reaches(rel, recv, rtype):
                continue
            if unscoped and role == "call" and _external(index, recv, rtype, owner_classes):
                continue                # ast.parse() is not the project's parse
            line = parsed.lines[r].strip() if r < len(parsed.lines) else ""
            uses.append((rel, r + 1, role, recv, owner, line))
    return uses, defs


def _external(index: ProjectIndex, recv: str | None, rtype: str | None,
              owner_classes: set[str]) -> bool:
    """The receiver is bound to something outside the project: an import of a
    library module (`import ast`) or a declared type the project does not
    define.  Unknown, generic and self receivers never are; neither is a
    library base class of a class defining the name (virtual dispatch)."""
    if not recv or not rtype or recv.split(".", 1)[0] in ("self", "this", "cls", "super"):
        return False
    if rtype in _OPEN_TYPES or (len(rtype) <= 2 and rtype.isupper()):
        return False
    with index._lock:
        if rtype in _module_names(index)[0]:
            return False
    if _is_project_class(index, rtype):
        return False
    return not any(_is_subtype(index, c, rtype) for c in owner_classes)


@dataclass
class _Module:
    """A module a callers query names (`code_nav.parse`): the files that define
    the name at top level, the names the module goes by, and per file what the
    imports bind.  Python and JS/TS imports bind names, so a bare call counts
    only where the file imports that name; other languages' imports (a C
    #include) make every name visible, so any importer counts."""
    homes: set[str]
    names: set[str]
    importers: set[str]
    binders: set[str] = field(default_factory=set)      # files that import the name itself
    loose: set[str] = field(default_factory=set)        # importers whose imports bind everything
    file_names: dict = field(default_factory=dict)      # rel -> module aliases (`import m as x`)
    aliases: dict = field(default_factory=dict)         # rel -> name aliases (`f as g`)

    def reaches(self, rel: str, recv: str | None, rtype: str | None) -> bool:
        if recv is not None:
            last = recv.rsplit(".", 1)[-1]
            return rtype in self.names or last in self.names or last in self.file_names.get(rel, ())
        return rel in self.homes or rel in self.binders or rel in self.loose


def _module_word(rel: str) -> str:
    """The name a module goes by: its stem, or its folder's for a package file."""
    stem = Path(rel).stem
    return Path(rel).parent.name if stem in ("__init__", "index", "mod") and "/" in rel else stem


def _module_scope(index: ProjectIndex, qualifier: str, bare: str) -> _Module | None:
    """`code_nav` in `code_nav.parse`, when it names a project module that
    defines `bare` at top level — or re-exports it (`harness.code_index.
    index_search`: the package got it from .search); None otherwise (then it
    filters receivers)."""
    q = qualifier.replace("/", ".")
    with index._lock:
        cands, _ = _module_names(index)
        resolved = _resolved_imports(index)
        fids = {d for f in cands.get(q, ()) if (d := _definer(index, resolved, f, bare)) is not None}
        if not fids:
            return None
        # The modules that hand the name on: a package's __init__.py, a JS barrel.
        barrels = set()
        for f, imps in resolved.items():
            e = index.files[f]
            if f in fids or e is None:
                continue
            if ((e.lang == "python" and e.path.endswith("__init__.py"))
                    or (e.lang in code_nav._JS_LANGS and any(i.text.startswith("export") for i, _ in imps))) \
                    and _definer(index, resolved, f, bare) in fids:
                barrels.add(f)
        exposed = fids | barrels
        homes = {index.files[f].path for f in fids}
        names = {q.rsplit(".", 1)[-1]} | {_module_word(index.files[f].path) for f in exposed}
        m = _Module(homes, names, set())
        file_names, aliases = defaultdict(set), defaultdict(set)
        for f, imps in resolved.items():
            hits = [imp for imp, targets in imps if not exposed.isdisjoint(targets)]
            if not hits:
                continue
            e = index.files[f]
            m.importers.add(e.path)
            if e.lang != "python" and e.lang not in code_nav._JS_LANGS:
                m.loose.add(e.path)
                continue
            for imp in hits:
                if bare in _bound_names(imp) or _star(imp):
                    m.binders.add(e.path)
                for orig, alias in imp.aliases:
                    if orig == bare:
                        aliases[e.path].add(alias)
                        m.binders.add(e.path)
                    elif orig in ("*", imp.module) or orig.rsplit(".", 1)[-1] in names:
                        file_names[e.path].add(alias)
        m.file_names, m.aliases = dict(file_names), dict(aliases)
    return m


def _name_aliases(index: ProjectIndex, bare: str) -> dict[str, set[str]]:
    """{file: local names} of the project imports `from m import bare as x`."""
    out: dict[str, set[str]] = defaultdict(set)
    with index._lock:
        resolved = _resolved_imports(index)
        for f, imps in resolved.items():
            for imp, targets in imps:
                for orig, alias in imp.aliases:
                    if orig == bare and any(_definer(index, resolved, t, bare) is not None
                                            for t in targets):
                        out[index.files[f].path].add(alias)
    return dict(out)


_USE_ROLES = ("call", "import", "type", "other")


def _data_defs(index: ProjectIndex, name: str) -> list:
    """[(rel, Symbol)] config keys, CSS rules, SQL tables, HTML ids... named `name`."""
    bare = name.rsplit(".", 1)[-1]
    return [(e.path, s) for e, s in _defs_named(index, bare, exact=False)
            if not _is_code_symbol(e.lang, s) and code_nav._matches(s, name)]


def _data_callers(index: ProjectIndex, name: str, bare: str, data: list) -> str:
    """index_callers on a config key / table / selector: code refers to those by
    string, so the answer is where it is defined and which files mention it."""
    where = "; ".join(f"{rel}:L{s.start}-{s.end} ({s.kind} {s.qualname})" for rel, s in data[:5])
    out = [f"'{name}' is not code — it is defined at {where}"
           + (f" and {len(data) - 5} more" if len(data) > 5 else "") + "."]
    homes = {rel for rel, _ in data}
    mentions = _text_mentions(index, bare, lambda rel: rel not in homes, limit=10)
    if mentions:
        out.append(f"Code refers to it by text; '{bare}' appears in: {mentions}.")
        out.append(f'(next: index_text("{bare}") shows those lines)')
    else:
        out.append(f"No other file mentions '{bare}'.")
    return _with_note("\n".join(out), index)


def _files_named(index: ProjectIndex, name: str) -> list[str]:
    """Indexed code files a module-style name refers to: 'format', 'src/format.js',
    'shop.pricing' (by stem, path or dotted path)."""
    q = name.strip().replace("\\", "/")
    dotted = q.replace("/", ".").rsplit(".", 1)[0] if "/" in q else q
    with index._lock:
        out = []
        for _, e in index._alive():
            if not e.lang or e.lang in DATA_LANGS:
                continue
            stem_path = e.path.rsplit(".", 1)[0]
            if (e.path == q or stem_path == q
                    or ("/" not in q and e.path.rsplit("/", 1)[-1] == q)    # 'format.js'
                    or stem_path.replace("/", ".").endswith(dotted)
                    and (stem_path.rsplit("/", 1)[-1] == dotted.rsplit(".", 1)[-1])):
                out.append(e.path)
    return sorted(out)


def _qualified_names(defs) -> list[str]:
    """How to name each definition apart: 'Rules.parse' for a method,
    'code_nav.parse' (the module) for a top-level function."""
    out: list[str] = []
    for rel, sym in defs:
        if "." in sym.qualname:
            n = ".".join(sym.qualname.split(".")[-2:])
        else:
            n = f"{Path(rel).stem}.{sym.name}"
        if n not in out:
            out.append(n)
    return out


_MAX_AS_TEXT_FILES = 400     # candidate files read; a commoner word is no useful pointer


def _as_text(index: ProjectIndex, bare: str, uses, defs) -> list[str]:
    """Where `bare` is written as a whole word but not as code: a string-keyed
    registry, getattr, a route table, config or docs — uses no syntax shows.
    Lines where it is an identifier (a use, or another definition the scoping
    ruled out) do not count.  A statement, so a rename or delete checks them."""
    if len(bare) < 4:
        return []
    word = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(bare)}(?![A-Za-z0-9_])")
    seen = {u[0] for u in uses} | {rel for rel, _ in defs}
    with index._lock:
        if not index.components["idents"]:
            return []           # without the occurrence index, code lines cannot be told apart
        code_rows = {index.files[f].path: set(rows) for f, rows in index._occurrences(bare).items()
                     if index.files[f] is not None}
    cands, _ = _text_candidates(index, _trigram_ids(_WORD_STR.findall(bare.lower())),
                                lambda rel: rel not in seen)
    if len(cands) > _MAX_AS_TEXT_FILES:
        return []
    counts = []
    for rel, _symbols in cands:
        try:
            text = _read_source(index.root / rel)
        except OSError:
            continue
        skip = code_rows.get(rel, ())
        n = sum(len(word.findall(line)) for ln, line in enumerate(split_lines(text), 1)
                if ln not in skip)
        if n:
            counts.append((-n, rel))
    if not counts:
        return []
    counts.sort()
    shown = ", ".join(f"{rel} ×{-n}" for n, rel in counts[:5])
    more = f" and {len(counts) - 5} more files" if len(counts) > 5 else ""
    return [f"Also named as text in: {shown}{more} (a dynamic use, config or docs — not "
            f"counted above)"]


def _tested_line(uses, through: set[str]) -> str:
    """'Tested in: tests/test_x.py (3 uses); through its callers: ...' — which
    test files reach the definition, so a change knows what to run."""
    direct: dict[str, int] = {}
    for u in uses:
        if _is_test(u[0]) and u[2] != "import":
            direct[u[0]] = direct.get(u[0], 0) + 1
    via = sorted(through - set(direct))
    if not direct and not via:
        return "Tested in: no test file uses it" + (" directly" if not through else "") + "."
    parts = []
    if direct:
        ranked = sorted(direct.items(), key=lambda kv: (-kv[1], kv[0]))
        parts.append(", ".join(f"{rel} ({n} use{'s' if n != 1 else ''})" for rel, n in ranked[:8])
                     + (f" and {len(ranked) - 8} more" if len(ranked) > 8 else ""))
    if via:
        parts.append("through its callers: " + ", ".join(via[:8]) + (" ..." if len(via) > 8 else ""))
    return "Tested in: " + "; ".join(parts) + "."


# ── callees: what a definition calls ─────────────────────────────────────────

def _callees(index: ProjectIndex, dotted: str, depth: int, cancel) -> str:
    """index_callers(direction="callees"): the calls in one definition's body,
    each resolved to the project definition it reaches — the same class and its
    bases for self/this, the file's own definitions, what the file imports, a
    receiver's declared type, else the one definition with that name."""
    bare, want_recv, cls = query_target(index, dotted)
    module = _module_scope(index, want_recv, bare) if want_recv else None
    cands = [(e, s) for e, s in _defs_named(index, bare)
             if _is_code_symbol(e.lang, s) and s.kind in code_nav.CALLABLE_KINDS]
    if cls:
        last = cls.rsplit(".", 1)[-1]
        cands = [c for c in cands if c[1].qualname.endswith(f"{last}.{bare}")] or cands
    elif module is not None:
        cands = [c for c in cands if c[0].path in module.homes and c[1].depth == 0] or cands
    elif "." in dotted:
        cands = [c for c in cands if code_nav._matches(c[1], dotted)] or cands
    if not cands:
        return _with_note(f"(no function or method named '{dotted}' in the indexed files)", index)
    e, s = cands[0]
    rows = _callee_rows(index, e, s)
    if rows is None:
        return f"ERROR: could not read {e.path}"
    inside = [r for r in rows if r[3] is not None]
    out = [f"Callees of {e.path}:L{s.start}-{s.end} {s.kind} {s.qualname} — {len(rows)} "
           f"call{'s' if len(rows) != 1 else ''}, {len(inside)} to project definitions:"]
    if len(cands) > 1:
        others = "; ".join(f"{c.path}:L{d.start} {d.qualname}" for c, d in cands[1:4])
        out.append(f"({len(cands) - 1} other definition{'s' if len(cands) > 2 else ''} share the "
                   f"name: {others} — a qualified name picks one)")
    expanded = 0
    for name, row, recv, target in rows:
        if target is None:
            continue
        te, ts, how = target
        call = f"{recv + '.' if recv else ''}{name}()"
        out.append(f"  L{row} {call} → {te.path}:L{ts.start}-{ts.end} {ts.kind} {ts.qualname}"
                   f"{' (by name)' if how else ''} | {ts.signature}")
        if depth > 1 and expanded < _MAX_EXPAND:
            if cancel is not None and cancel.is_set():
                return "ERROR: cancelled"
            expanded += 1
            sub_rows = _callee_rows(index, te, ts) or []
            names = [f"{r[2] + '.' if r[2] else ''}{r[0]}" for r in sub_rows if r[3] is not None]
            if names:
                out.append(f"      → calls {', '.join(names[:8])}" + (" ..." if len(names) > 8 else ""))
    outside = sorted({f"{r[2] + '.' if r[2] else ''}{r[0]}" for r in rows if r[3] is None})
    if outside:
        out.append("Not in the project (library, builtin or unresolved): " + ", ".join(outside[:20])
                   + (" ..." if len(outside) > 20 else ""))
    text, size = [], 0
    for line in out:
        if size + len(line) + 1 > _MAX_CALLERS_CHARS:
            text.append(f"... (output capped at {_MAX_CALLERS_CHARS} chars)")
            break
        text.append(line)
        size += len(line) + 1
    return _with_note("\n".join(text), index)


def _callee_rows(index: ProjectIndex, e, s) -> list | None:
    """[(name, row, receiver, (FileEntry, Symbol, how) | None)] for the calls
    in definition `s` of file `e`; None when the file cannot be read."""
    try:
        parsed = code_nav.parse(index.root / e.path, cache=False)
    except (OSError, ImportError):
        return None
    if parsed is None:
        return None
    calls = code_nav.calls_in(parsed, s.start - 1, s.end - 1)
    owner = s.qualname.rsplit(".", 1)[0].rsplit(".", 1)[-1] if "." in s.qualname else None
    with index._lock:
        resolved = _resolved_imports(index)
        imps = resolved.get(index._by_path.get(e.path), [])
        return [(name, row, recv, _resolve_call(index, e, imps, resolved, name, recv, rtype, owner))
                for name, row, recv, rtype in calls]


def _resolve_call(index: ProjectIndex, e, imps, resolved, name: str, recv: str | None,
                  rtype: str | None, owner: str | None):
    """(FileEntry, Symbol, how) of the project definition a call reaches, how
    = 'by name' when only the name links them; None when nothing in the
    project does (lock held)."""
    family = lang_family(e.lang)
    defs = [(f, sym) for f, sym in _defs_named(index, name)
            if _is_code_symbol(f.lang, sym) and lang_family(f.lang) == family]
    if not defs:
        return None

    def member_of(types: list[str]):
        for t in types:                         # the nearest class first
            for f, sym in defs:
                if "." in sym.qualname and sym.qualname.rsplit(".", 2)[-2] == t:
                    return f, sym, ""
        return None

    def bases_of(t: str) -> list[str]:
        return sorted(supertypes(index, t))

    head = recv.split("(", 1)[0] if recv else None
    if head in ("self", "this", "cls", "super") and owner:
        return member_of(([] if head == "super" else [owner]) + bases_of(owner))
    if recv is None:
        if owner and e.lang != "python":        # Java/Kotlin/C++/JS: a bare call may be this.name()
            hit = member_of([owner] + bases_of(owner))
            if hit:
                return hit
        for f, sym in defs:
            if f.path == e.path and sym.depth == 0:
                return f, sym, ""
        for imp, targets in imps:
            orig = next((o for o, a in imp.aliases if a == name), name)
            if orig in imp.names or _star(imp) or e.lang in ("c", "cpp"):
                for t in targets:
                    d = _definer(index, resolved, t, orig)
                    if d is not None:
                        sym = next((x for x in index.files[d].symbols if x.depth == 0 and x.name == orig), None)
                        if sym is not None:
                            return index.files[d], sym, ""
    else:
        for imp, targets in imps:               # a module receiver: tools.read_text(), ci.x()
            mod_names = {a for o, a in imp.aliases} | {imp.module, imp.module.rsplit(".", 1)[-1]} \
                | set(imp.names)
            if recv in mod_names or head in mod_names:
                for t in targets:
                    d = _definer(index, resolved, t, name)
                    if d is not None:
                        sym = next((x for x in index.files[d].symbols if x.depth == 0 and x.name == name), None)
                        if sym is not None:
                            return index.files[d], sym, ""
        if rtype:
            hit = member_of([rtype] + bases_of(rtype))
            if hit:
                return hit
    # A bare call whose name only one top-level definition has.  A method call
    # on an unknown receiver (`known.update()` on a dict) is not linked by name.
    tops = [(f, sym) for f, sym in defs if sym.depth == 0]
    if recv is None and len(tops) == 1:
        return tops[0][0], tops[0][1], "by name"
    return None


def _group(uses) -> "dict[tuple[str, str | None], list]":
    groups: dict = {}
    for u in uses:
        key = (u[0], u[4].qualname if u[4] else None)
        groups.setdefault(key, []).append(u)
    return groups


def _fmt_group(key, items, indent: str, max_lines: int = _MAX_LINES_PER_GROUP) -> list[str]:
    rel, qual = key
    owner = items[0][4]
    head = (f"{indent}{rel}:L{owner.start}-{owner.end}  {owner.kind} {owner.qualname}"
            if owner else f"{indent}{rel} (module level)")
    out = [head]
    for u in items[:max_lines]:
        tag = u[2] + (f", recv {u[3]}" if u[3] else "")
        text = u[5] if len(u[5]) <= 120 else u[5][:120] + " …"
        out.append(f"{indent}    L{u[1]} ({tag}) {text}")
    if len(items) > max_lines:
        out.append(f"{indent}    ... {len(items) - max_lines} more in this definition")
    return out


def index_callers(name: str, depth: int = 1, role: str | None = None, direction: str = "callers",
                  *, workdir: Path, index: ProjectIndex | None = None, cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    try:
        depth = max(1, min(3, int(depth)))
    except (TypeError, ValueError):
        depth = 1
    if role and role not in _USE_ROLES:
        return f"ERROR: unknown role '{role}' (use one of: {', '.join(_USE_ROLES)})"
    direction = (direction or "callers").strip().lower()
    if direction not in ("callers", "callees"):
        return f"ERROR: direction must be 'callers' or 'callees', got: {direction}"
    dotted = (name or "").strip().replace("::", ".")
    if not dotted:
        return "ERROR: name is empty"
    if direction == "callees":
        return _callees(index, dotted, min(depth, 2), cancel)
    bare, want_recv, cls = query_target(index, dotted)
    module = _module_scope(index, want_recv, bare) if want_recv else None
    if module is not None:
        want_recv = None            # code_nav.parse: parse() inside code_nav counts too
    uses, defs = uses_of(index, bare, want_recv, cancel, cls, module)
    if cancel is not None and cancel.is_set():
        return "ERROR: cancelled"
    if cls:
        defs = [d for d in defs if d[1].qualname == dotted or d[1].qualname.endswith("." + dotted)] or defs
    elif module is not None:
        defs = [d for d in defs if d[0] in module.homes] or defs
    if role:
        uses = [u for u in uses if u[2] == role]
    if not defs and not uses:
        data = _data_defs(index, dotted)
        if data:
            return _data_callers(index, dotted, bare, data)
        files = _files_named(index, dotted)
        if files:
            # `index_callers("format")` asks about a module, not a definition.
            return _with_note(
                f"'{dotted}' is not a definition — it names a file: {', '.join(files[:5])}.\n"
                f'(next: index_file("{files[0]}") lists the files that import it and what '
                f"each one uses)", index)
    out = []
    if defs:
        where = "; ".join(f"{rel}:L{s.start}-{s.end} ({s.kind} {s.qualname})" for rel, s in defs[:5])
        out.append(f"'{dotted}' is defined at {where}" + (f" and {len(defs) - 5} more" if len(defs) > 5 else "") + ".")
        if cls:
            # One language family: only its subtypes; several (a Kotlin and a
            # Rust Shape): all of them.
            with index._lock:
                langs = {index.files[f].lang for rel, _s in defs if (f := index._by_path.get(rel)) is not None}
            lang = langs.pop() if len({lang_family(x) for x in langs}) == 1 else None
            if (ov := override_line(index, cls.rsplit(".", 1)[-1], bare, lang)):
                out.append(ov)
        if not cls and module is None and not want_recv:
            names = _qualified_names(defs)
            if len(names) > 1:
                # A statement, not a "next:" hint: those get followed unasked.
                out.append(f"({len(names)} different definitions share this name and their uses are "
                           f"listed together; a qualified name like {' or '.join(repr(n) for n in names[:3])} "
                           f"lists only one's)")
    else:
        out.append(f"'{dotted}' has no definition in the indexed files (external, dynamic or builtin).")
    if not uses:
        out.append(f"No {role + ' ' if role else ''}uses found"
                   + (f" on receiver '{want_recv}'" if want_recv else "") + ".")
        out += _as_text(index, bare, uses, defs)
        return _with_note("\n".join(out), index, "idents")
    counts: dict[str, int] = {}
    for u in uses:
        counts[u[2]] = counts.get(u[2], 0) + 1
    groups = _group(uses)
    split = ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    importing = sorted({u[0] for u in uses if u[2] == "import"})
    if importing:
        # "Which files import X?" is answered here, before the use-by-use list.
        out.append(f"Imported in {len(importing)} file{'s' if len(importing) != 1 else ''}: "
                   + ", ".join(importing[:15]) + (" ..." if len(importing) > 15 else ""))
    out.append(f"Level 1 — {len(uses)} use{'s' if len(uses) != 1 else ''} in {len(groups)} "
               f"place{'s' if len(groups) != 1 else ''} ({split}):")
    for key in list(groups)[:_MAX_CALLER_GROUPS]:
        out.extend(_fmt_group(key, groups[key], "  "))
    if len(groups) > _MAX_CALLER_GROUPS:
        out.append(f"  ... {len(groups) - _MAX_CALLER_GROUPS} more places")
    out += _as_text(index, bare, uses, defs)
    impact_defs = {(k[0], k[1]) for k in groups if k[1]}
    impact_files = {k[0] for k in groups}
    frontier = [(items[0][4], key) for key, items in groups.items()
                if items[0][4] is not None and any(u[2] != "import" for u in items)]
    visited = {dotted} | {o.qualname for o, _ in frontier}
    tests_through: set[str] = set()
    for level in range(2, depth + 1):
        if not frontier:
            break
        lines, nxt = [], []
        if len(frontier) > _MAX_EXPAND:
            lines.append(f"  (following the first {_MAX_EXPAND} of {len(frontier)} definitions — "
                         f"call index_callers on one of the others to follow it)")
        for owner, _key in frontier[:_MAX_EXPAND]:
            if cancel is not None and cancel.is_set():
                return "ERROR: cancelled"
            with index._lock:
                common = (index.components["idents"]
                          and len(index._ref_files(owner.name)) > _MAX_EXPAND_FILES)
            if common or owner.name == bare:
                lines.append(f"  via {owner.qualname}: (name too common to follow)")
                continue
            # Scoped like level 1: Cart.total's users, not every `total`.
            b2, _recv, c2 = query_target(index, owner.qualname)
            u2, _ = uses_of(index, b2, None, cancel, c2)
            u2 = [u for u in u2 if u[2] in ("call", "other", "type")]
            if not u2:
                continue
            g2 = _group(u2)
            lines.append(f"  via {owner.qualname} ← {len(u2)} use{'s' if len(u2) != 1 else ''}:")
            tests_through.update(u[0] for u in u2 if _is_test(u[0]))
            for key in list(g2)[:_MAX_EXPAND_GROUPS]:
                lines.extend(_fmt_group(key, g2[key], "    ", 1))
                impact_files.add(key[0])
                if key[1]:
                    impact_defs.add(key)
                o2 = g2[key][0][4]
                if o2 is not None and o2.qualname not in visited:
                    visited.add(o2.qualname)
                    nxt.append((o2, key))
            for key in list(g2)[_MAX_EXPAND_GROUPS:]:
                impact_files.add(key[0])
                if key[1]:
                    impact_defs.add(key)
            if len(g2) > _MAX_EXPAND_GROUPS:
                lines.append(f"    ... {len(g2) - _MAX_EXPAND_GROUPS} more places")
        if lines:
            out.append(f"Level {level} — who uses those definitions (matched by name, not resolved):")
            out.extend(lines)
        frontier = nxt
    impact = (f"Impact: {len(impact_defs)} definition{'s' if len(impact_defs) != 1 else ''} "
              f"in {len(impact_files)} file{'s' if len(impact_files) != 1 else ''}"
              + (f" over {depth} levels." if depth > 1 else "."))
    first = next(iter(groups.values()))[0]
    hint = f'(next: read_symbol("{first[0]}", "L{first[1]}") reads a caller whole'
    if depth == 1 and frontier:
        hint += "; depth=2 shows who calls those"
    # The tests line, the Impact line and the hint always survive the cap.
    tail = [_tested_line(uses, tests_through), impact, hint + ")"]
    text, size = [], sum(len(t) + 1 for t in tail)
    for line in out:
        if size + len(line) + 1 > _MAX_CALLERS_CHARS:
            text.append(f"... (output capped at {_MAX_CALLERS_CHARS} chars — narrow with "
                        f"role='call', a qualified name, or depth=1)")
            break
        text.append(line)
        size += len(line) + 1
    return _with_note("\n".join(text + tail), index, "idents")
