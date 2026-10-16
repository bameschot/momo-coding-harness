"""index_callers: the uses of a definition, classified by syntax (call,
import, type, other) and narrowed by class, module, receiver type and
language family — then, with depth, who uses those."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import code_nav
from .records import DATA_LANGS, lang_family
from .store import ProjectIndex
from .common import (_MAX_CALLERS_CHARS, _MAX_CALLER_GROUPS, _MAX_EXPAND, _MAX_EXPAND_FILES,
                     _MAX_EXPAND_GROUPS, _MAX_LINES_PER_GROUP, _defs_named, _is_code_symbol, _ready,
                     _with_note)
from .text import _text_mentions
from .graph import _import_graph, _module_names

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
    """`t` is `target` or (transitively) names it in its class declaration line:
    `class FlatPricer extends Pricer`, `class Circle : public Shape`,
    `class Line(Base):`.  A type not defined in the project is not a subtype."""
    if t == target:
        return True
    if depth == 0:
        return False
    sigs = [(e.lang, s.signature) for e, s in _defs_named(index, t)
            if s.kind in code_nav.CONTAINER_KINDS]
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
    # Any letter case: `harness.send` is the method of the class Harness.
    is_class = any(s.kind in code_nav.CONTAINER_KINDS
                   for _, s in _defs_named(index, qualifier.rsplit(".", 1)[-1], exact=False))
    return (bare, None, qualifier) if is_class else (bare, qualifier, None)


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
    with index._lock:
        if index.components["idents"]:
            occ = index._occurrences(bare)
            targets = [(e.path, set(lines)) for fid, lines in occ.items()
                       if (e := index.files[fid]) is not None and e.lang
                       and (family is None or lang_family(e.lang) == family)]
        else:
            targets = [(e.path, None) for _, e in index._alive()
                       if e.lang and (e.lang not in DATA_LANGS or e.lang == "html")
                       and (family is None or lang_family(e.lang) == family)]
    uses, defs = [], []
    for i, (rel, rows) in enumerate(sorted(targets, key=lambda t: t[0])):
        if cancel is not None and i % 20 == 0 and cancel.is_set():
            break
        try:
            parsed = code_nav.parse(index.root / rel, cache=False)
        except (OSError, ImportError):
            continue
        if parsed is None:
            continue
        found = code_nav.references_in(parsed, bare, {r - 1 for r in rows} if rows else None)
        for r in sorted(found):
            role, recv, rtype = found[r]
            if role == "def":
                sym = next((s for s in parsed.symbols if s.name == bare and s.name_line == r + 1), None)
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
    the name at top level, the names the module goes by, and the files that
    import it."""
    homes: set[str]
    names: set[str]
    importers: set[str]

    def reaches(self, rel: str, recv: str | None, rtype: str | None) -> bool:
        if recv is not None:
            return rtype in self.names or recv.rsplit(".", 1)[-1] in self.names
        return rel in self.homes or rel in self.importers


def _module_scope(index: ProjectIndex, qualifier: str, bare: str) -> _Module | None:
    """`code_nav` in `code_nav.parse`, when it names a project module that
    defines `bare` at top level; None otherwise (then it filters receivers)."""
    q = qualifier.replace("/", ".")
    with index._lock:
        cands, _ = _module_names(index)
        fids = {f for f in cands.get(q, ())
                if any(s.depth == 0 and s.name == bare for s in index.files[f].symbols)}
        if not fids:
            return None
        homes = {index.files[f].path for f in fids}
        names = {q.rsplit(".", 1)[-1]} | {Path(h).stem for h in homes}
        importers = {index.files[r].path for r, targets in _import_graph(index).items()
                     if not fids.isdisjoint(targets)}
    return _Module(homes, names, importers)


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


def index_callers(name: str, depth: int = 1, role: str | None = None, *, workdir: Path,
                  index: ProjectIndex | None = None, cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    try:
        depth = max(1, min(3, int(depth)))
    except (TypeError, ValueError):
        depth = 1
    if role and role not in _USE_ROLES:
        return f"ERROR: unknown role '{role}' (use one of: {', '.join(_USE_ROLES)})"
    dotted = (name or "").strip().replace("::", ".")
    if not dotted:
        return "ERROR: name is empty"
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
    impact_defs = {(k[0], k[1]) for k in groups if k[1]}
    impact_files = {k[0] for k in groups}
    frontier = [(items[0][4], key) for key, items in groups.items()
                if items[0][4] is not None and any(u[2] != "import" for u in items)]
    visited = {dotted} | {o.qualname for o, _ in frontier}
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
    out.append(f"Impact: {len(impact_defs)} definition{'s' if len(impact_defs) != 1 else ''} "
               f"in {len(impact_files)} file{'s' if len(impact_files) != 1 else ''}"
               + (f" over {depth} levels." if depth > 1 else "."))
    first = next(iter(groups.values()))[0]
    hint = f'(next: read_symbol("{first[0]}", "L{first[1]}") reads a caller whole'
    if depth == 1 and frontier:
        hint += "; depth=2 shows who calls those"
    tail = [out.pop(), hint + ")"]     # the Impact line and the hint always survive
    text, size = [], sum(len(t) + 1 for t in tail)
    for line in out:
        if size + len(line) + 1 > _MAX_CALLERS_CHARS:
            text.append(f"... (output capped at {_MAX_CALLERS_CHARS} chars — narrow with "
                        f"role='call', a qualified name, or depth=1)")
            break
        text.append(line)
        size += len(line) + 1
    return _with_note("\n".join(text + tail), index, "idents")
