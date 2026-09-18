"""Syntax-aware code navigation built on tree-sitter.

Backs the code_outline / find_symbol / read_symbol / find_references tools.
Everything here is read-only.  Grammars come from the per-language
``tree-sitter-<lang>`` wheels, which bundle the compiled grammar, so nothing is
downloaded at runtime.  If tree-sitter is not installed, AVAILABLE is False and
tools.py leaves these tools out of every mode's tool set.
"""
import importlib
import os
from dataclasses import dataclass
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
class _Parsed:
    lang: str
    lines: list[str]
    tree: object
    symbols: list[Symbol]


_cache: dict[str, tuple[tuple[int, int], _Parsed]] = {}
_MAX_CACHE = 2000
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


def _extract(root, lang: str, lines: list[str]) -> list[Symbol]:
    defs = _DEFS[lang]
    out: list[Symbol] = []

    def walk(node, parent_qual: str, parent_kind: str | None, depth: int) -> None:
        for child in node.children:
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
        return hit[1]
    raw = path.read_bytes()
    tree = _parser(lang).parse(raw)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    parsed = _Parsed(lang, lines, tree, _extract(tree.root_node, lang, lines))
    if len(_cache) >= _MAX_CACHE:
        _cache.clear()
    _cache[key] = (stamp, parsed)
    return parsed


def _matches(sym: Symbol, name: str) -> bool:
    name = name.replace("::", ".")
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


def _iter_source_files(root: Path):
    """Yield supported source files under root (or root itself if it is a file),
    skipping the same noise directories and oversized files as grep_files."""
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
                    continue
            except OSError:
                continue
            yield fpath


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


# ── executors ────────────────────────────────────────────────────────────────

_MAX_SYMBOL_RESULTS = 100
_MAX_REF_RESULTS    = 200
_MAX_SYMBOL_LINES   = 400


def code_outline(path: str, *, workdir: Path) -> str:
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
    out = [f"{_rel(p, workdir)}  ({parsed.lang}, {len(parsed.lines)} lines)"]
    if not parsed.symbols:
        out.append("(no classes or functions found)")
    for s in parsed.symbols:
        out.append(f"{'  ' * s.depth}L{s.start}-{s.end}  {_line_label(s)}")
    if parsed.tree.root_node.has_error:
        out.append(_ERROR_NOTE)
    return "\n".join(out)


def find_symbol(name: str, directory: str = ".", kind: str | None = None, *, workdir: Path) -> str:
    from .tools import _safe_path
    root = _safe_path(directory, workdir)
    if isinstance(root, str):
        return root
    if not root.exists():
        return f"ERROR: not found: {directory}"
    hits = []
    for f in _iter_source_files(root):
        try:
            parsed = parse(f)
        except OSError:
            continue
        for s in parsed.symbols:
            if _matches(s, name) and (not kind or s.kind == kind):
                hits.append(f"{_rel(f, workdir)}:L{s.start}-{s.end}  {s.kind} {s.qualname}  | {s.signature}")
    if not hits:
        return (f"(no definition of '{name}' found in supported source files — "
                "try grep_files for other file types or dynamic definitions)")
    total = len(hits)
    if total > _MAX_SYMBOL_RESULTS:
        return "\n".join(hits[:_MAX_SYMBOL_RESULTS]) + f"\n... (first {_MAX_SYMBOL_RESULTS} of {total} — use a qualified name like Class.method or a narrower directory)"
    return "\n".join(hits)


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


def find_references(name: str, directory: str = ".", *, workdir: Path) -> str:
    from .tools import _safe_path
    root = _safe_path(directory, workdir)
    if isinstance(root, str):
        return root
    if not root.exists():
        return f"ERROR: not found: {directory}"
    # Match the last component of a qualified name: identifiers are single names.
    target = name.replace("::", ".").rsplit(".", 1)[-1].encode()
    hits = []
    for f in _iter_source_files(root):
        try:
            parsed = parse(f)
        except OSError:
            continue
        def_lines = {s.name_line for s in parsed.symbols if s.name == target.decode()}
        rows: set[int] = set()
        stack = [parsed.tree.root_node]
        while stack:
            node = stack.pop()
            if node.child_count == 0:
                # Leaf identifier nodes only: comments and string contents are
                # separate node types, and substrings never match exactly.
                if node.type.endswith("identifier") and node.text == target:
                    rows.add(node.start_point[0])
            else:
                stack.extend(node.children)
        rel = _rel(f, workdir)
        for r in sorted(rows):
            line = parsed.lines[r] if r < len(parsed.lines) else ""
            is_def = r + 1 in def_lines
            # Name the enclosing definition so the model does not have to guess
            # which function a call site sits in.
            owner = _enclosing(parsed.symbols, r + 1, skip_name=target.decode() if is_def else None)
            where = f" [in {owner.qualname}]" if owner else ""
            tag = " (def)" if is_def else ""
            hits.append(f"{rel}:{r + 1}:{where}{tag} {line.strip()}")
    if not hits:
        return (f"(no references to '{name}' found in supported source files — "
                "try grep_files for other file types)")
    total = len(hits)
    if total > _MAX_REF_RESULTS:
        return "\n".join(hits[:_MAX_REF_RESULTS]) + f"\n... (first {_MAX_REF_RESULTS} of {total} references — specify a narrower directory)"
    return "\n".join(hits)
