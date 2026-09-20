"""Syntax-aware code navigation built on tree-sitter.

Backs the code_outline / find_symbol / read_symbol / find_references /
file_dependencies tools.  Everything here is read-only.  Grammars come from the
per-language ``tree-sitter-<lang>`` wheels, which bundle the compiled grammar, so
nothing is downloaded at runtime.  If tree-sitter is not installed, AVAILABLE is
False and tools.py leaves these tools out of every mode's tool set.
"""
import fnmatch
import importlib
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

try:
    from tree_sitter import Language, Parser
    AVAILABLE = True
except ImportError:  # optional dependency — the harness runs without it
    AVAILABLE = False


# ── language registry ────────────────────────────────────────────────────────

# extension -> grammar name.  .h is parsed as C++: the C++ grammar handles plain
# C headers too, and C++ headers commonly use .h.
_EXTENSIONS = {
    ".py": "python", ".pyi": "python",
    ".java": "java",
    ".c": "c",
    ".h": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".kt": "kotlin", ".kts": "kotlin",
    ".rs": "rust",
    # The JavaScript grammar also parses JSX; TypeScript ships separate TS and TSX grammars.
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
}
SUPPORTED_EXTENSIONS = ", ".join(sorted(_EXTENSIONS))

# grammar name -> (module, function returning the language pointer), for the
# grammars that do not follow the tree_sitter_<name>.language() convention.
_GRAMMAR_LOADERS = {
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx":        ("tree_sitter_typescript", "language_tsx"),
}

# grammar name -> {definition node type: kind}.  Only named definitions are
# listed; declarations without a body (C prototypes, C++ in-class method
# declarations) are deliberately left out.
_C_DEFS = {
    "function_definition": "function",
    "struct_specifier":    "struct",
    "union_specifier":     "union",
    "enum_specifier":      "enum",
    "type_definition":     "typedef",
}
_JS_DEFS = {
    "class_declaration":              "class",
    "function_declaration":           "function",
    "generator_function_declaration": "function",
    "method_definition":              "function",
    # `const f = () => ...`, `const f = function () {...}` and class fields
    # `handle = () => ...` — only counted when the value is a function.
    "variable_declarator":            "function",
    "field_definition":               "function",
}
_TS_DEFS = {
    **_JS_DEFS,
    "abstract_class_declaration": "class",
    "interface_declaration":      "interface",
    "type_alias_declaration":     "type",
    "enum_declaration":           "enum",
    "internal_module":            "namespace",
    "module":                     "namespace",
    "function_signature":         "function",
    "method_signature":           "function",
    "abstract_method_signature":  "function",
    "public_field_definition":    "function",
}
_FUNCTION_VALUES = {"arrow_function", "function_expression", "function",
                    "generator_function"}
_BINDING_NODES = {"variable_declarator", "field_definition", "public_field_definition"}
_DEFS: dict[str, dict[str, str]] = {
    "python": {
        "class_definition":    "class",
        "function_definition": "function",
    },
    "java": {
        "class_declaration":       "class",
        "interface_declaration":   "interface",
        "enum_declaration":        "enum",
        "record_declaration":      "record",
        "annotation_type_declaration": "annotation",
        "method_declaration":      "function",
        "constructor_declaration": "constructor",
    },
    "c": _C_DEFS,
    "cpp": {
        **_C_DEFS,
        "class_specifier":      "class",
        "namespace_definition": "namespace",
    },
    "kotlin": {
        "class_declaration":    "class",
        "object_declaration":   "object",
        "function_declaration": "function",
    },
    "rust": {
        "struct_item":            "struct",
        "enum_item":              "enum",
        "union_item":             "union",
        "trait_item":             "trait",
        "impl_item":              "impl",
        "function_item":          "function",
        "function_signature_item": "function",
        "mod_item":               "module",
        "macro_definition":       "macro",
    },
    "javascript": _JS_DEFS,
    "typescript": _TS_DEFS,
    "tsx":        _TS_DEFS,
}

# grammar name -> import/include statement node types.  Used by
# file_dependencies and to tag a reference as role "import".
_IMPORTS: dict[str, set[str]] = {
    "python": {"import_statement", "import_from_statement"},
    "java":   {"import_declaration"},
    "c":      {"preproc_include"},
    "cpp":    {"preproc_include"},
    "kotlin": {"import"},
    "rust":   {"use_declaration"},
    # export_statement carries a `source` field for re-exports (`export {x} from './m'`).
    "javascript": {"import_statement", "export_statement"},
    "typescript": {"import_statement", "export_statement"},
    "tsx":        {"import_statement", "export_statement"},
}

# Reference-role classification.  Grammars name these nodes consistently enough
# that one shared table per category beats nine per-language ones; anything
# unrecognised falls through to role "other", which is never wrong, only vague.
_CALL_NODES = {"call", "call_expression", "method_invocation", "function_call_expression",
               "macro_invocation", "new_expression", "object_creation_expression",
               "constructor_invocation", "explicit_constructor_invocation"}
_MEMBER_NODES = {"attribute", "member_expression", "field_expression", "field_access",
                 "navigation_expression", "navigation_suffix", "scoped_identifier",
                 "qualified_identifier", "scoped_type_identifier"}
# Field names that put the identifier on the receiver side of a member access
# (`ast` in `ast.parse`) rather than on the member side (`parse`).
_RECEIVER_FIELDS = ("object", "argument", "value", "path", "scope")
_TYPE_PARENTS = {"type_identifier", "type_annotation", "generic_type", "type_arguments",
                 "type_parameter", "type_parameters", "superclass", "super_interfaces",
                 "base_class_clause", "scoped_type_identifier", "qualified_type",
                 "user_type", "extends_interfaces", "nullable_type", "type_constraint",
                 "trait_bounds", "constrained_type_parameter"}
REFERENCE_ROLES = ("call", "def", "import", "type", "other")
_IMPORT_ANCESTOR_DEPTH = 6  # an identifier sits close to its import statement

# Kinds whose nested functions are reported as methods.
_CONTAINER_KINDS = {"class", "interface", "enum", "record", "struct", "union",
                    "trait", "impl", "object"}

# Wrapper nodes whose span belongs to the definition they wrap (decorators,
# template headers), so read_symbol returns them too.
_WRAPPERS = {"decorated_definition", "template_declaration"}

_parsers: dict = {}


def language_for(path: Path) -> str | None:
    return _EXTENSIONS.get(path.suffix.lower())


def _parser(lang: str):
    p = _parsers.get(lang)
    if p is None:
        mod_name, attr = _GRAMMAR_LOADERS.get(lang, (f"tree_sitter_{lang}", "language"))
        mod = importlib.import_module(mod_name)
        p = _parsers[lang] = Parser(Language(getattr(mod, attr)()))
    return p


# ── symbol extraction ────────────────────────────────────────────────────────

@dataclass
class Symbol:
    name: str        # bare name, e.g. "send"
    qualname: str    # dotted path of enclosing definitions, e.g. "Harness.send"
    kind: str        # class / function / method / struct / ...
    start: int       # 1-based first line (including decorators / template header)
    end: int         # 1-based last line
    name_line: int   # 1-based line of the name itself
    signature: str   # first source line of the definition, stripped
    depth: int


@dataclass
class Import:
    line: int         # 1-based line of the statement
    module: str       # as written: "os.path", ".tools", "stdio.h", "std::collections", "./m"
    names: list[str]  # names pulled out of the module, [] for a whole-module import
    text: str         # the statement's first source line, stripped


@dataclass
class _Parsed:
    lang: str
    lines: list[str]
    tree: object
    symbols: list[Symbol]
    imports: list[Import] = field(default_factory=list)


@dataclass
class _Index:
    """Everything the project-wide tools need about a file, minus the parse tree.
    A retained tree costs ~0.65 MB per file, which is why the tree cache has to
    stay small while this one can cover a whole repo."""
    lang: str
    nlines: int
    symbols: list[Symbol]
    imports: list[Import]
    has_error: bool


# Two tiers, both LRU.  Only find_references and the single-file tools need the
# tree; find_symbol, a directory outline and file_dependencies read the index,
# so a repeated scan of a large repo costs ~0.04 s instead of re-parsing it.
# (A single LRU cannot help a full scan at all: walking files in the same order
# every time evicts exactly what the next call reads first.)
#
# The tree cache stays deliberately small.  A retained tree measured ~0.65 MB
# per file, so the old 2000-entry cap could hold 1.2 GB — unaffordable next to a
# local model's own memory.  200 trees (~130 MB) covers a whole project of the
# size this harness is usually pointed at; beyond that, find_references re-parses
# rather than competing with the model for RAM.
_cache: "OrderedDict[str, tuple[tuple[int, int], _Parsed]]" = OrderedDict()
_index_cache: "OrderedDict[str, tuple[tuple[int, int], _Index]]" = OrderedDict()
_MAX_CACHE = 200
_MAX_INDEX_CACHE = 20000
_MAX_SIGNATURE = 160


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _c_declarator_name(node):
    """Follow the declarator chain of a C/C++ function or typedef down to the
    node that names it (identifier, qualified_identifier, destructor_name, ...)."""
    while node is not None:
        inner = node.child_by_field_name("declarator")
        if inner is None:
            return node
        node = inner
    return None


def _strip_generics(s: str) -> str:
    i = s.find("<")
    return s[:i] if i > 0 else s


def _def_name(node, lang: str, kind: str) -> tuple[str, int] | None:
    """Return (name, 0-based line of the name) for a definition node, or None
    for anonymous definitions (e.g. the struct inside `typedef struct {...} P;`)."""
    if lang in ("c", "cpp") and kind in ("function", "typedef"):
        n = _c_declarator_name(node.child_by_field_name("declarator"))
        if n is None:
            return None
        # "n::A::f" -> "n.A.f" so dotted lookup works the same in every language
        return _text(n).replace("::", "."), n.start_point[0]
    if node.type in _BINDING_NODES:
        value = node.child_by_field_name("value")
        n = node.child_by_field_name("name") or node.child_by_field_name("property")
        if (value is None or value.type not in _FUNCTION_VALUES or n is None
                or n.type not in ("identifier", "property_identifier")):
            return None
        return _text(n), n.start_point[0]
    if lang == "rust" and kind == "impl":
        t = node.child_by_field_name("type")
        if t is None:
            return None
        return _strip_generics(_text(t)), t.start_point[0]
    n = node.child_by_field_name("name")
    if n is None:
        return None
    return _text(n), n.start_point[0]


def _extract(root, lang: str, lines: list[str],
             imports_out: list | None = None) -> list[Symbol]:
    """Collect definitions, and (when `imports_out` is given) import statement
    nodes in the same traversal — a second full walk of the tree costs about a
    third of the time of a project-wide scan."""
    defs = _DEFS[lang]
    import_types = _IMPORTS.get(lang, frozenset()) if imports_out is not None else frozenset()
    out: list[Symbol] = []

    def walk(node, parent_qual: str, parent_kind: str | None, depth: int) -> None:
        for child in node.children:
            if child.type in import_types:
                # An import statement holds no definitions, so stop here.
                imports_out.append(child)
                continue
            kind = defs.get(child.type)
            if kind is None:
                walk(child, parent_qual, parent_kind, depth)
                continue
            got = _def_name(child, lang, kind)
            if got is None:
                walk(child, parent_qual, parent_kind, depth)
                continue
            name, name_row = got
            if kind == "function" and parent_kind in _CONTAINER_KINDS:
                kind = "method"
            if lang == "kotlin" and kind == "class" and any(c.type == "interface" for c in child.children):
                kind = "interface"
            span = child.parent if child.parent is not None and child.parent.type in _WRAPPERS else child
            start_row = span.start_point[0]
            qual = f"{parent_qual}.{name}" if parent_qual else name
            # Signature: the first line, unless it does not mention the name (a TS
            # decorator, a C return type on its own line) — then the name's line.
            sig_row = child.start_point[0]
            if sig_row < len(lines) and name.rsplit(".", 1)[-1] not in lines[sig_row]:
                sig_row = name_row
            out.append(Symbol(
                name=name.rsplit(".", 1)[-1],
                qualname=qual,
                kind=kind,
                start=start_row + 1,
                end=child.end_point[0] + 1,
                name_line=name_row + 1,
                signature=lines[sig_row].strip()[:_MAX_SIGNATURE] if sig_row < len(lines) else "",
                depth=depth,
            ))
            walk(child, qual, kind, depth + 1)

    walk(root, "", None, 0)
    return out


# ── import extraction ────────────────────────────────────────────────────────

def _strip_quotes(s: str) -> str:
    return s.strip().strip('<>"\'')


def _leaf_names(node) -> list[str]:
    """Identifier leaves under a node, in source order (an import's name list)."""
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.child_count == 0:
            if n.type.endswith("identifier"):
                out.append(_text(n))
        else:
            stack.extend(reversed(n.children))
    return out


def _py_import(node) -> list[tuple[str, list[str]]]:
    if node.type == "import_statement":
        mods = []
        for i, c in enumerate(node.children):
            if node.field_name_for_child(i) != "name":
                continue
            if c.type == "aliased_import":
                n = c.child_by_field_name("name")
                mods.append((_text(n) if n is not None else _text(c), []))
            else:
                mods.append((_text(c), []))
        return mods
    mod = node.child_by_field_name("module_name")
    names = [_text(c) for i, c in enumerate(node.children)
             if node.field_name_for_child(i) == "name"]
    return [(_text(mod) if mod is not None else "", names)]


def _rust_import(node) -> list[tuple[str, list[str]]]:
    arg = node.child_by_field_name("argument")
    if arg is None:
        return []
    if arg.type == "scoped_use_list":
        p = arg.child_by_field_name("path")
        lst = arg.child_by_field_name("list")
        return [(_text(p) if p is not None else "", _leaf_names(lst) if lst is not None else [])]
    if arg.type == "use_as_clause":
        p = arg.child_by_field_name("path")
        a = arg.child_by_field_name("alias")
        return [(_text(p) if p is not None else "", [_text(a)] if a is not None else [])]
    if arg.type == "use_wildcard":
        return [(_text(arg).removesuffix("::*"), [])]
    if arg.type == "use_list":
        return [("", _leaf_names(arg))]
    if arg.type == "scoped_identifier":
        name = arg.child_by_field_name("name")
        return [(_text(arg), [_text(name)] if name is not None else [])]
    return [(_text(arg), [])]


def _build_imports(found: list, lang: str, lines: list[str]) -> list[Import]:
    """Turn the import nodes collected by _extract into Import records.  Nodes
    nested inside a function count: an import deferred to break a cycle or keep
    an optional dependency optional is still a real dependency, and this
    codebase itself relies on them."""
    if not found:
        return []
    out: list[Import] = []
    for node in sorted(found, key=lambda n: n.start_point):
        row = node.start_point[0]
        pairs: list[tuple[str, list[str]]]
        if lang == "python":
            pairs = _py_import(node)
        elif lang == "rust":
            pairs = _rust_import(node)
        elif lang in ("c", "cpp"):
            p = node.child_by_field_name("path")
            pairs = [(_strip_quotes(_text(p)), [])] if p is not None else []
        elif lang == "java":
            scoped = next((c for c in node.children
                           if c.type in ("scoped_identifier", "identifier")), None)
            if scoped is None:
                pairs = []
            else:
                full = _text(scoped)
                pairs = [(full, [full.rsplit(".", 1)[-1]] if "." in full else [])]
        elif lang == "kotlin":
            q = next((c for c in node.children if c.type == "qualified_identifier"), None)
            if q is None:
                pairs = []
            else:
                full = _text(q)
                pairs = [(full, [full.rsplit(".", 1)[-1]] if "." in full else [])]
        else:  # javascript / typescript / tsx
            src = node.child_by_field_name("source")
            if src is None:  # a plain `export {x}` with no `from` is not an import
                continue
            clause = next((c for c in node.children
                           if c.type in ("import_clause", "export_clause")), None)
            pairs = [(_strip_quotes(_text(src)), _leaf_names(clause) if clause is not None else [])]
        for module, names in pairs:
            if module or names:
                out.append(Import(line=row + 1, module=module, names=names,
                                  text=lines[row].strip()[:_MAX_SIGNATURE] if row < len(lines) else ""))
    return out


def parse(path: Path) -> _Parsed | None:
    """Parse a supported file (cached by mtime+size).  Returns None for
    unsupported extensions.  Raises OSError if the file cannot be read."""
    lang = language_for(path)
    if lang is None:
        return None
    st = path.stat()
    key = str(path)
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        _cache.move_to_end(key)
        return hit[1]
    raw = path.read_bytes()
    tree = _parser(lang).parse(raw)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    imp_nodes: list = []
    symbols = _extract(tree.root_node, lang, lines, imp_nodes)
    parsed = _Parsed(lang, lines, tree, symbols, _build_imports(imp_nodes, lang, lines))
    while len(_cache) >= _MAX_CACHE:
        _cache.popitem(last=False)
    _cache[key] = (stamp, parsed)
    _put_index(key, stamp, parsed)
    return parsed


def _put_index(key: str, stamp: tuple[int, int], parsed: _Parsed) -> None:
    while len(_index_cache) >= _MAX_INDEX_CACHE:
        _index_cache.popitem(last=False)
    _index_cache[key] = (stamp, _Index(parsed.lang, len(parsed.lines), parsed.symbols,
                                       parsed.imports, parsed.tree.root_node.has_error))


def index(path: Path) -> _Index | None:
    """Symbols and imports for a supported file, without retaining the parse
    tree.  Use this for project-wide scans; use parse() when the tree is needed.
    Returns None for unsupported extensions, raises OSError if unreadable."""
    lang = language_for(path)
    if lang is None:
        return None
    st = path.stat()
    key = str(path)
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _index_cache.get(key)
    if hit and hit[0] == stamp:
        _index_cache.move_to_end(key)
        return hit[1]
    cached = _cache.get(key)
    if cached and cached[0] == stamp:
        _put_index(key, stamp, cached[1])
        return _index_cache[key][1]
    raw = path.read_bytes()
    tree = _parser(lang).parse(raw)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    imp_nodes: list = []
    symbols = _extract(tree.root_node, lang, lines, imp_nodes)
    idx = _Index(lang, len(lines), symbols, _build_imports(imp_nodes, lang, lines),
                 tree.root_node.has_error)
    while len(_index_cache) >= _MAX_INDEX_CACHE:
        _index_cache.popitem(last=False)
    _index_cache[key] = (stamp, idx)
    return idx  # tree goes out of scope here — that is the point


def _is_pattern(name: str) -> bool:
    return any(c in name for c in "*?[")


def _matches(sym: Symbol, name: str) -> bool:
    name = name.replace("::", ".")
    if _is_pattern(name):
        # A dotted pattern is matched against the qualified name, a bare one
        # against the bare name, mirroring the exact-match rules below.
        if "." in name:
            return (fnmatch.fnmatchcase(sym.qualname, name)
                    or fnmatch.fnmatchcase(sym.qualname, "*." + name))
        return fnmatch.fnmatchcase(sym.name, name)
    if "." in name:
        return sym.qualname == name or sym.qualname.endswith("." + name)
    return sym.name == name


# ── tool helpers ─────────────────────────────────────────────────────────────
# tools.py imports this module at load time, so its helpers (_safe_path,
# _SKIP_DIRS, ...) are imported inside the functions to avoid a circular import.

def _rel(p: Path, workdir: Path) -> str:
    try:
        return str(p.relative_to(workdir.resolve()))
    except ValueError:
        return str(p)


def _unsupported(path: str) -> str:
    return (f"ERROR: {path} is not a supported source file (supported: {SUPPORTED_EXTENSIONS}). "
            "Use grep_file / read_file instead.")


def _iter_source_files(root: Path, stats: dict | None = None):
    """Yield supported source files under root (or root itself if it is a file),
    skipping the same noise directories and oversized files as grep_files.  With
    `stats`, counts what was skipped so the caller can admit the gap."""
    from .tools import _SKIP_DIRS, _MAX_GREP_FILE_BYTES
    if root.is_file():
        if language_for(root):
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if language_for(fpath) is None:
                continue
            try:
                if fpath.stat().st_size > _MAX_GREP_FILE_BYTES:
                    if stats is not None:
                        stats["too_big"] = stats.get("too_big", 0) + 1
                    continue
            except OSError:
                if stats is not None:
                    stats["unreadable"] = stats.get("unreadable", 0) + 1
                continue
            yield fpath


def _scan(root: Path, stats: dict, *, tree: bool = False):
    """Walk every supported file under root, tallying what could not be used.
    A file that fails to parse cleanly contributes nothing, so the caller must
    say so — otherwise the model reads an empty result as proof of absence.
    Yields _Index objects unless `tree` is set, which yields full _Parsed."""
    load = parse if tree else index
    for f in _iter_source_files(root, stats):
        try:
            got = load(f)
        except OSError:
            stats["unreadable"] = stats.get("unreadable", 0) + 1
            continue
        if got is None:
            continue
        stats["searched"] = stats.get("searched", 0) + 1
        if (got.tree.root_node.has_error if tree else got.has_error):
            stats["errors"] = stats.get("errors", 0) + 1
        yield f, got


def _scan_note(stats: dict) -> str:
    """One line admitting the files the scan could not read, or ''."""
    bad = stats.get("errors", 0) + stats.get("too_big", 0) + stats.get("unreadable", 0)
    if not bad:
        return ""
    parts = []
    if stats.get("errors"):
        parts.append(f"{stats['errors']} with syntax errors")
    if stats.get("too_big"):
        parts.append(f"{stats['too_big']} over the 2 MB size limit")
    if stats.get("unreadable"):
        parts.append(f"{stats['unreadable']} unreadable")
    return (f"(searched {stats.get('searched', 0)} files; {' and '.join(parts)} "
            "may be missing from these results — use grep_files to double-check)")


def _line_label(sym: Symbol) -> str:
    sig = sym.signature
    if sym.name not in sig:
        sig = f"{sym.kind} {sym.qualname}: {sig}"
    return sig


def _enclosing(symbols: list[Symbol], line: int, skip_name: str | None = None) -> Symbol | None:
    """Innermost definition containing `line`.  With skip_name, the definition of
    that name starting on this line is ignored, so a definition site reports its
    parent rather than itself."""
    best = None
    for s in symbols:  # pre-order, so a later match is nested deeper
        if s.start <= line <= s.end and not (skip_name and s.name == skip_name and s.name_line == line):
            best = s
    return best


_ERROR_NOTE ="(note: the file has syntax errors or unsupported syntax; results may be incomplete)"


def _field_of(node) -> str | None:
    """The field name this node fills in its parent, or None."""
    parent = node.parent
    if parent is None:
        return None
    for i, c in enumerate(parent.children):
        if c.id == node.id:
            return parent.field_name_for_child(i)
    return None


# Field names holding the thing being called, across the supported grammars.
_CALLEE_FIELDS = {"function", "name", "macro", "type", "constructor"}


def _is_callee(node) -> bool:
    """True when `node` is what its enclosing call actually calls, rather than
    one of the arguments."""
    parent = node.parent
    if parent is None:
        return False
    f = _field_of(node)
    if f is not None:
        return f in _CALLEE_FIELDS
    first = next((c for c in parent.children if c.is_named), None)
    return first is not None and first.id == node.id


def _classify(node, lang: str, is_def: bool) -> tuple[str, str | None]:
    """(role, receiver) for an identifier leaf: what the use actually is, and
    what it hangs off when it is a member access.  Purely syntactic — there is
    no scope resolution here, so the receiver is reported rather than resolved."""
    if is_def:
        return "def", None
    import_types = _IMPORTS.get(lang, ())
    anc, depth = node.parent, 0
    while anc is not None and depth < _IMPORT_ANCESTOR_DEPTH:
        if anc.type in import_types:
            return "import", None
        anc, depth = anc.parent, depth + 1

    parent = node.parent
    if parent is None:
        return "other", None
    field = _field_of(node)
    receiver = None
    if parent.type in _MEMBER_NODES:
        first = next((ch for ch in parent.children if ch.is_named), None)
        if field in _RECEIVER_FIELDS or (field is None and first is not None
                                         and first.id == node.id):
            # The identifier IS the receiver (`ast` in `ast.parse`) — not a use
            # of the name we were asked about in any interesting sense.
            return "other", None
        for f in _RECEIVER_FIELDS:
            recv = parent.child_by_field_name(f)
            if recv is not None:
                receiver = _text(recv)[:40]
                break
        else:
            # Grammars like Kotlin's navigation_expression name no fields at
            # all; there the receiver is simply what comes first.
            if first is not None and first.id != node.id:
                receiver = _text(first)[:40]
        gp = parent.parent
        if gp is not None and gp.type in _CALL_NODES and _is_callee(parent):
            return "call", receiver
        if node.type == "type_identifier" or parent.type == "scoped_type_identifier":
            return "type", receiver
        return "other", receiver

    if parent.type in _CALL_NODES and _is_callee(node):
        return "call", None
    if node.type == "type_identifier" or parent.type in _TYPE_PARENTS:
        return "type", None
    return "other", None


# ── executors ────────────────────────────────────────────────────────────────

_MAX_SYMBOL_RESULTS = 100
_MAX_REF_RESULTS    = 200
_MAX_SYMBOL_LINES   = 400
_MAX_MAP_FILES      = 300
_MAX_DEP_RESULTS    = 200


def code_outline(path: str = ".", depth: int | None = None, *, workdir: Path) -> str:
    from .tools import _safe_path
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    if p.is_dir():
        return _directory_outline(p, depth if depth is not None else 1, workdir)
    if not p.is_file():
        return f"ERROR: not found: {path}"
    try:
        parsed = parse(p)
    except OSError as e:
        return f"ERROR: {e}"
    if parsed is None:
        return _unsupported(path)
    out = [f"{_rel(p, workdir)}  ({parsed.lang}, {len(parsed.lines)} lines)"]
    if not parsed.symbols:
        out.append("(no classes or functions found)")
    for s in parsed.symbols:
        if depth is not None and s.depth >= depth:
            continue
        out.append(f"{'  ' * s.depth}L{s.start}-{s.end}  {_line_label(s)}")
    if parsed.tree.root_node.has_error:
        out.append(_ERROR_NOTE)
    return "\n".join(out)


def _directory_outline(root: Path, depth: int, workdir: Path) -> str:
    """One condensed line per source file: the map a model needs to orient in a
    tree without outlining every file separately."""
    stats: dict = {}
    out: list[str] = []
    shown = 0
    for f, idx in _scan(root, stats):
        if shown >= _MAX_MAP_FILES:
            break
        tops = [s for s in idx.symbols if s.depth < depth]
        names = ", ".join(f"{s.kind} {s.name}" for s in tops)
        out.append(f"{_rel(f, workdir)} ({idx.nlines} lines): {names or '-'}")
        shown += 1
    if not out:
        return (f"(no supported source files under {_rel(root, workdir)} — supported: "
                f"{SUPPORTED_EXTENSIONS})")
    total = stats.get("searched", shown)
    if shown < total or shown >= _MAX_MAP_FILES:
        out.append(f"... (first {shown} files — outline a subdirectory to see the rest)")
    out.append(f"(top-level definitions only; code_outline a single file for its full structure"
               + (f", or depth={depth + 1} for one level more)" if depth == 1 else ")"))
    note = _scan_note(stats)
    if note:
        out.append(note)
    return "\n".join(out)


def find_symbol(name: str, directory: str = ".", kind: str | None = None, *, workdir: Path) -> str:
    from .tools import _safe_path
    root = _safe_path(directory, workdir)
    if isinstance(root, str):
        return root
    if not root.exists():
        return f"ERROR: not found: {directory}"
    stats: dict = {}
    hits = []
    kinds: dict[str, int] = {}
    files = set()
    for f, parsed in _scan(root, stats):
        for s in parsed.symbols:
            if _matches(s, name) and (not kind or s.kind == kind):
                hits.append(f"{_rel(f, workdir)}:L{s.start}-{s.end}  {s.kind} {s.qualname}  | {s.signature}")
                kinds[s.kind] = kinds.get(s.kind, 0) + 1
                files.add(f)
    note = _scan_note(stats)
    if not hits:
        return (f"(no definition of '{name}' found in supported source files — "
                "try grep_files for other file types or dynamic definitions)"
                + (f"\n{note}" if note else ""))
    total = len(hits)
    if total > _MAX_SYMBOL_RESULTS:
        if _is_pattern(name):
            # A wildcard query that overflows wants a shape, not 100 arbitrary
            # rows: say how many of each kind and where, then let it narrow.
            breakdown = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
            out = (f"{total} definitions matching '{name}' in {len(files)} files: {breakdown}.\n"
                   f"Too many to list — narrow with kind=, a subdirectory, or a more specific pattern.")
            return out + (f"\n{note}" if note else "")
        out = ("\n".join(hits[:_MAX_SYMBOL_RESULTS])
               + f"\n... (first {_MAX_SYMBOL_RESULTS} of {total} — use a qualified name like Class.method or a narrower directory)")
        return out + (f"\n{note}" if note else "")
    return "\n".join(hits) + (f"\n{note}" if note else "")


def read_symbol(path: str, name: str, *, workdir: Path) -> str:
    from .tools import _safe_path
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    if not p.is_file():
        return f"ERROR: file not found: {path}"
    try:
        parsed = parse(p)
    except OSError as e:
        return f"ERROR: {e}"
    if parsed is None:
        return _unsupported(path)
    # A line number instead of a name: read the definition containing that line.
    # Closes the loop after a grep_files hit, a find_references hit or a
    # traceback, all of which hand the model `file:line`.
    if re.fullmatch(r"[Ll]?\d+", name.strip()):
        line = int(name.strip().lstrip("Ll"))
        if not 1 <= line <= max(len(parsed.lines), 1):
            return f"ERROR: line {line} is outside {path} (1-{len(parsed.lines)})"
        owner = _enclosing(parsed.symbols, line)
        if owner is None:
            return (f"(line {line} of {_rel(p, workdir)} is not inside any definition — "
                    "use read_file with start_line/end_line)")
        found = [owner]
    else:
        found = [s for s in parsed.symbols if _matches(s, name)]
    if not found:
        known = ", ".join(s.qualname for s in parsed.symbols[:40]) or "(none)"
        return f"ERROR: no definition named '{name}' in {path}. Definitions in this file: {known}"
    if len(found) > 1:
        cands = "\n".join(f"  L{s.start}-{s.end}  {s.kind} {s.qualname}" for s in found)
        return (f"'{name}' matches {len(found)} definitions in {path} — pass a qualified name "
                f"(e.g. Class.method) or use read_file with start_line/end_line:\n{cands}")
    s = found[0]
    end = min(s.end, s.start + _MAX_SYMBOL_LINES - 1)
    body = "\n".join(f"{i:4}: {parsed.lines[i - 1]}" for i in range(s.start, end + 1)
                     if i - 1 < len(parsed.lines))
    head = f"{_rel(p, workdir)}  {s.kind} {s.qualname}  L{s.start}-{s.end}"
    if end < s.end:
        body += (f"\n[{s.end - s.start + 1} lines total — showing the first {_MAX_SYMBOL_LINES}; "
                 f"use code_outline or read_file with start_line={end + 1}]")
    return head + "\n" + body


def find_references(name: str, directory: str = ".", role: str | None = None,
                    *, workdir: Path) -> str:
    from .tools import _safe_path
    root = _safe_path(directory, workdir)
    if isinstance(root, str):
        return root
    if not root.exists():
        return f"ERROR: not found: {directory}"
    if role and role not in REFERENCE_ROLES:
        return f"ERROR: unknown role '{role}' (use one of: {', '.join(REFERENCE_ROLES)})"
    # Match the last component of a qualified name: identifiers are single names.
    # Any qualifier becomes a receiver constraint, so find_references("JSON.parse")
    # returns only the calls on JSON and not the unrelated same-named methods.
    dotted = name.replace("::", ".")
    bare = dotted.rsplit(".", 1)[-1]
    want_recv = dotted.rsplit(".", 1)[0] if "." in dotted else None
    target = bare.encode()
    stats: dict = {}
    hits = []
    by_role: dict[str, int] = {}
    recvs: dict[str, int] = {}
    direct = 0
    for f, parsed in _scan(root, stats, tree=True):
        def_lines = {s.name_line for s in parsed.symbols if s.name == bare}
        # One entry per row, keeping the first (leftmost) occurrence's role.
        rows: dict[int, tuple[str, str | None]] = {}
        stack = [parsed.tree.root_node]
        while stack:
            node = stack.pop()
            if node.child_count == 0:
                # Leaf identifier nodes only: comments and string contents are
                # separate node types, and substrings never match exactly.
                if node.type.endswith("identifier") and node.text == target:
                    r = node.start_point[0]
                    kind, recv = _classify(node, parsed.lang, r + 1 in def_lines)
                    prev = rows.get(r)
                    # "call" is the most informative label for a shared row.
                    if prev is None or (prev[0] != "call" and kind == "call"):
                        rows[r] = (kind, recv)
            else:
                stack.extend(node.children)
        rel = _rel(f, workdir)
        for r in sorted(rows):
            kind, recv = rows[r]
            by_role[kind] = by_role.get(kind, 0) + 1
            if kind == "call":
                if recv:
                    recvs[recv] = recvs.get(recv, 0) + 1
                else:
                    direct += 1
            if role and kind != role:
                continue
            # A qualified query keeps only matching receivers; the definition
            # itself always stays, since that is what the model is looking for.
            if want_recv and kind != "def" and recv != want_recv:
                continue
            line = parsed.lines[r] if r < len(parsed.lines) else ""
            # Name the enclosing definition so the model does not have to guess
            # which function a call site sits in.
            owner = _enclosing(parsed.symbols, r + 1, skip_name=bare if kind == "def" else None)
            where = f" [in {owner.qualname}]" if owner else ""
            tag = f" ({kind}" + (f", recv {recv})" if recv else ")")
            hits.append(f"{rel}:{r + 1}:{where}{tag} {line.strip()}")
    note = _scan_note(stats)
    counts = ", ".join(f"{n} {k}" for k, n in sorted(by_role.items(), key=lambda kv: -kv[1]))
    # Spell out how the calls split between the bare name and calls on some
    # receiver: that split is what tells the model whether it is looking at one
    # function or several unrelated same-named ones.
    split = ""
    if recvs:
        top = ", ".join(f"{r}×{n}" if n > 1 else r
                        for r, n in sorted(recvs.items(), key=lambda kv: -kv[1])[:5])
        split = (f" ({direct} on the bare name, {sum(recvs.values())} on a receiver: {top}"
                 f"{', ...' if len(recvs) > 5 else ''} — qualify the name, e.g. "
                 f"'{next(iter(sorted(recvs, key=lambda r: -recvs[r])))}.{bare}', to keep only one)")
    if not hits:
        if by_role:
            which = f"'{role}' " if role else ""
            qual = f" on receiver '{want_recv}'" if want_recv else ""
            return (f"(no {which}references to '{bare}'{qual} — the {sum(by_role.values())} "
                    f"references found are: {counts}.{split} Re-run with a different role, "
                    "or omit it.)" + (f"\n{note}" if note else ""))
        return (f"(no references to '{name}' found in supported source files — "
                "try grep_files for other file types)" + (f"\n{note}" if note else ""))
    total = len(hits)
    out = hits[:_MAX_REF_RESULTS]
    trailer = []
    if total > _MAX_REF_RESULTS:
        trailer.append(f"... (first {_MAX_REF_RESULTS} of {total} references — "
                       "narrow with role= or a subdirectory)")
    if not role and len(by_role) > 1:
        # Only advertise role=call when there are calls to narrow to: a hint
        # that leads to an empty result is worse than no hint.
        hint = " — pass role=call to see only call sites" if by_role.get("call") else ""
        trailer.append(f"(by role: {counts}{hint})")
    if not want_recv and split:
        trailer.append(f"(calls:{split})")
    if note:
        trailer.append(note)
    return "\n".join(out + trailer)


# ── dependency edges ─────────────────────────────────────────────────────────

def _norm_module(module: str) -> str:
    """Normalise an import target to dotted form for comparison: `std::x` and
    `./x/y` and `x/y.h` all become `x.y`-ish, with relative markers dropped."""
    m = module.replace("::", ".").replace("/", ".").replace("\\", ".")
    for ext in (".h", ".hpp", ".hh", ".hxx", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"):
        if m.endswith(ext):
            m = m[: -len(ext)]
            break
    m = m.lstrip(".")
    for prefix in ("crate.", "super.", "self.", "..", "."):
        if m.startswith(prefix):
            m = m[len(prefix):]
    return m


def _module_candidates(p: Path, workdir: Path) -> tuple[str, set[str]]:
    """(bare stem, dotted paths) that an import of `p` could plausibly name."""
    rel = _rel(p, workdir)
    stem = p.stem
    dotted = rel.rsplit(".", 1)[0].replace("/", ".").replace("\\", ".")
    cands = {dotted, stem}
    parts = dotted.split(".")
    # Any suffix of the path: `harness.code_nav` also matches `from .code_nav`.
    for i in range(len(parts)):
        cands.add(".".join(parts[i:]))
    # A package import names the directory, not __init__.
    if stem == "__init__" and len(parts) > 1:
        cands.add(".".join(parts[:-1]))
        cands.add(parts[-2])
    return stem, cands


def file_dependencies(path: str, direction: str = "both", *, workdir: Path) -> str:
    """What `path` imports, and which files import it.  Resolution is textual,
    not a real module resolver — see the note in the returned output."""
    from .tools import _safe_path
    if direction not in ("both", "imports", "importers"):
        return "ERROR: direction must be 'both', 'imports' or 'importers'"
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    if not p.is_file():
        return f"ERROR: file not found: {path}"
    try:
        parsed = parse(p)
    except OSError as e:
        return f"ERROR: {e}"
    if parsed is None:
        return _unsupported(path)

    out = [f"{_rel(p, workdir)}  ({parsed.lang})"]

    if direction in ("both", "imports"):
        out.append("")
        out.append(f"IMPORTS ({len(parsed.imports)}):")
        if not parsed.imports:
            out.append("  (none)")
        for imp in parsed.imports[:_MAX_DEP_RESULTS]:
            names = f"  ({', '.join(imp.names[:12])}{', ...' if len(imp.names) > 12 else ''})" if imp.names else ""
            out.append(f"  L{imp.line}  {imp.module}{names}")
        if len(parsed.imports) > _MAX_DEP_RESULTS:
            out.append(f"  ... (first {_MAX_DEP_RESULTS} of {len(parsed.imports)})")

    if direction in ("both", "importers"):
        stem, cands = _module_candidates(p, workdir)
        stats: dict = {}
        found: list[str] = []
        target = p.resolve()
        for f, other in _scan(workdir, stats):
            if f.resolve() == target:
                continue
            for imp in other.imports:
                norm = _norm_module(imp.module)
                if norm in cands or norm.endswith("." + stem) or stem in imp.names:
                    found.append(f"  {_rel(f, workdir)}:L{imp.line}  {imp.text}")
                    break
        out.append("")
        out.append(f"IMPORTED BY ({len(found)}):")
        if not found:
            out.append("  (none found)")
        for line in found[:_MAX_DEP_RESULTS]:
            out.append(line)
        if len(found) > _MAX_DEP_RESULTS:
            out.append(f"  ... (first {_MAX_DEP_RESULTS} of {len(found)})")
        note = _scan_note(stats)
        if note:
            out.append(f"  {note}")
        out.append("  (importers are matched on the import text, not resolved: a same-named "
                   "module elsewhere can appear, and re-exports or dynamic imports can be missed)")

    return "\n".join(out)
