"""Syntax-aware code navigation built on tree-sitter.

Backs the code_outline / find_symbol / read_symbol / find_references /
file_dependencies tools.  Everything here is read-only.  Grammars come from the
per-language ``tree-sitter-<lang>`` wheels, which bundle the compiled grammar, so
nothing is downloaded at runtime.  If tree-sitter is not installed, AVAILABLE is
False and tools.py leaves these tools out of every mode's tool set.
"""
import fnmatch
import importlib
import re
import warnings
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .paths import MAX_SCAN_FILE_BYTES, safe_path, walk_files

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
    # Config, markup and scripts: their "definitions" are keys, ids, selectors,
    # tables and functions (see _DATA_EXTRACTORS), not classes.
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".html": "html", ".htm": "html",
    ".css": "css",
    ".sql": "sql",
    ".sh": "bash", ".bash": "bash",
    ".dockerfile": "dockerfile",
}
SUPPORTED_EXTENSIONS = ", ".join(sorted(_EXTENSIONS)) + ", Dockerfile"

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
    "function_expression":            "function",   # only when named: (function boot() {})()
    "pair":                           "function",   # { remove: (id) => id }, only function values
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
_BINDING_NODES = {"variable_declarator", "field_definition", "public_field_definition", "pair"}
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
        "type_alias":           "type",
        "companion_object":     "object",   # only when named: companion object Factory
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
        "const_item":             "constant",
        "static_item":            "constant",
    },
    "javascript": _JS_DEFS,
    "typescript": _TS_DEFS,
    "tsx":        _TS_DEFS,
}

# grammar name -> import/include statement node types.  Used by
# file_dependencies and to tag a reference as role "import".
_IMPORTS: dict[str, set[str]] = {
    "python": {"import_statement", "import_from_statement", "future_import_statement"},
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
# decl: a declaration without a body (a C/C++ prototype); local: a parameter or
# local variable that merely shares the name — neither is a use of the definition.
REFERENCE_ROLES = ("call", "def", "decl", "import", "type", "local", "other")
NOT_USES = frozenset({"def", "decl", "local"})

# Scope rules, one small table for every grammar: the nodes that open a function
# scope, and the positions where an identifier BINDS a name in that scope.
_FUNC_SCOPES = {"function_definition", "function_declaration", "method_declaration",
                "method_definition", "constructor_declaration", "function_item",
                "arrow_function", "function_expression", "lambda", "lambda_literal",
                "closure_expression", "generator_function_declaration", "anonymous_function",
                "lambda_expression"}
_PARAM_NODES = {"parameters", "formal_parameters", "formal_parameter", "parameter_list",
                "parameter_declaration", "typed_parameter", "default_parameter",
                "typed_default_parameter", "parameter", "function_value_parameters",
                "required_parameter", "optional_parameter", "closure_parameters",
                "lambda_parameters", "object_pattern", "array_pattern", "pattern_list",
                "tuple_pattern", "variable_declaration",
                "as_pattern_target",            # with ... as x / except E as x
                "list_splat_pattern", "dictionary_splat_pattern",   # *args, **kwargs
                "rest_pattern",                 # JS ...rest
                "list_pattern",                 # Python typ, [data] = ...
                "type_pattern"}                 # Java `case Square sq ->`
# parent type -> the field that binds (None: any child identifier)
_BINDING_FIELDS = {"assignment": "left", "for_statement": "left", "for_in_statement": "left",
                   "variable_declarator": "name", "init_declarator": "declarator",
                   "let_declaration": "pattern", "declaration": "declarator",
                   "for_in_clause": "left",              # [x for x in xs]
                   "enhanced_for_statement": "name",     # for (Square sq : xs)
                   "lambda_expression": "parameters",    # Java q -> q.area()
                   "named_expression": "name"}           # Python (n := len(x))
_IMPORT_ANCESTOR_DEPTH = 6  # an identifier sits close to its import statement

# Kinds whose nested functions are reported as methods.
_CONTAINER_KINDS = {"class", "interface", "enum", "record", "struct", "union",
                    "trait", "impl", "object"}

# Wrapper nodes whose span belongs to the definition they wrap (decorators,
# template headers), so read_symbol returns them too.
_WRAPPERS = {"decorated_definition", "template_declaration"}

_parsers: dict = {}


def language_for(path: Path) -> str | None:
    lang = _EXTENSIONS.get(path.suffix.lower())
    if lang is None and (path.name == "Dockerfile" or path.name.startswith("Dockerfile.")):
        lang = "dockerfile"
    if lang is None or not grammar_available(lang):
        return None
    return lang


# Grammars whose wheel failed to import, so they are skipped rather than
# failing every scan that meets such a file.
_missing: set[str] = set()


def _parser(lang: str):
    p = _parsers.get(lang)
    if p is None:
        mod_name, attr = _GRAMMAR_LOADERS.get(lang, (f"tree_sitter_{lang}", "language"))
        mod = importlib.import_module(mod_name)
        # Older grammar wheels (tree-sitter-dockerfile) hand back a bare int,
        # which tree-sitter accepts with a DeprecationWarning — and a warning
        # printed to stderr corrupts the curses screen.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            p = _parsers[lang] = Parser(Language(getattr(mod, attr)()))
    return p


def grammar_available(lang: str) -> bool:
    if lang in _parsers:
        return True
    if lang in _missing:
        return False
    try:
        _parser(lang)
        return True
    except (ImportError, AttributeError, ValueError):
        _missing.add(lang)
        return False


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
    doc: str = ""    # first line of its docstring / leading doc comment, for search


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
    sub: "_Parsed | None" = None   # HTML: the page's inline <script> JavaScript


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


def _is_ident(node_type: str) -> bool:
    """Identifier-like leaves, including destructuring (`const { a } = ...`)."""
    return node_type.endswith("identifier") or node_type.endswith("identifier_pattern")


def _c_declarator_name(node):
    """Follow the declarator chain of a C/C++ function or typedef down to the
    node that names it (identifier, qualified_identifier, destructor_name, ...)."""
    while node is not None:
        inner = node.child_by_field_name("declarator")
        if inner is None and node.type in ("parenthesized_declarator", "reference_declarator"):
            # `typedef int (*cmp_fn)(...)`: the name sits inside the parentheses;
            # `mat3_t& rotate(...)`: the reference declarator has no field for it.
            inner = next((c for c in node.named_children), None)
        if inner is None:
            return node
        node = inner
    return None


def _strip_template_args(s: str) -> str:
    """'Box<T>::Iter::done' -> 'Box::Iter::done' (nested <...> too)."""
    prev = None
    while prev != s:
        prev, s = s, re.sub(r"<[^<>]*>", "", s)
    return s


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
        # "n::A::f" -> "n.A.f" so dotted lookup works the same in every language,
        # and "Box<T>::put" -> "Box.put": template arguments are not part of the name.
        return _strip_template_args(_text(n)).replace("::", "."), n.start_point[0]
    if node.type in _BINDING_NODES:
        value = node.child_by_field_name("value")
        n = (node.child_by_field_name("name") or node.child_by_field_name("property")
             or node.child_by_field_name("key"))
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
    if n is None and node.type == "type_alias":
        n = node.child_by_field_name("type")     # Kotlin puts the alias name there
    if n is None:
        return None
    if lang in ("c", "cpp"):
        return _strip_template_args(_text(n)).replace("::", "."), n.start_point[0]
    return _text(n), n.start_point[0]


_MAX_DOC = 120
_DOC_MARKERS = re.compile(r"^\s*(?:/\*\*?|\*/|\*|///?|//!|#|--|\"\"\"|\'\'\'|[rbuRBU]?\"\"\"|\")\s*")


def _clean_doc(text: str) -> str:
    """The first line of prose in a docstring or comment, markers stripped."""
    for raw in text.splitlines():
        line = _DOC_MARKERS.sub("", raw).strip().rstrip("*/").strip().strip('"\'').strip()
        if line and not line.startswith("@"):
            return line[:_MAX_DOC]
    return ""


def _doc_line(node, span) -> str:
    """A definition's docstring (Python: first statement of the body) or the doc
    comment directly above it (every other grammar).  Lets index_search match a
    description ('turns a size string into bytes') as well as a name."""
    body = node.child_by_field_name("body")
    if body is not None and body.named_child_count:
        first = body.named_children[0]
        if first.type == "expression_statement" and first.named_child_count \
                and first.named_children[0].type == "string":
            return _clean_doc(_text(first.named_children[0]))
    prev = span.prev_named_sibling
    if prev is not None and "comment" in prev.type and prev.end_point[0] >= span.start_point[0] - 1:
        # Walk back over a run of line comments (/// a, /// b) to its first line.
        while True:
            p2 = prev.prev_named_sibling
            if p2 is None or "comment" not in p2.type or p2.end_point[0] < prev.start_point[0] - 1:
                break
            prev = p2
        return _clean_doc(_text(prev))
    return ""


# ── module-level constants and variables ──────────────────────────────────────
# `MAX_SIZE = 10`, `const API_URL = ...`, `#define LEN 64`, Java `static final`
# fields: things a model looks up by name exactly like a function.  Only module
# (or namespace) scope, plus Java/Kotlin class constants — locals inside a
# function body would flood the index.

_UPPER = re.compile(r"_*[A-Z][A-Z0-9_]*")


def _var_kind(name: str, declared_const: bool = False) -> str:
    return "constant" if declared_const or _UPPER.fullmatch(name) else "variable"


def _c_decl_name(node):
    """The identifier a C/C++ declarator declares, or None for a function
    prototype (those are declarations of functions, not variables)."""
    while node is not None:
        if node.type == "function_declarator":
            return None
        if node.type == "identifier":
            return node
        node = node.child_by_field_name("declarator")
    return None


def _bindings(node, lang: str, parent_kind: str | None,
              parent_qual: str = "") -> list[tuple[str, object, str, object]]:
    """(name, name node, kind, span node) for each variable `node` declares at
    module scope (or, for Java/Kotlin, as a class constant)."""
    out = []
    t = node.type
    module = parent_kind is None or (lang == "cpp" and parent_kind == "namespace")
    if lang == "python" and module and t == "expression_statement":
        chain = [a for a in node.named_children if a.type == "assignment"]
        while chain and (nxt := chain[-1].child_by_field_name("right")) is not None \
                and nxt.type == "assignment":
            chain.append(nxt)                     # a = b = 2
        for a in chain:
            if a.type == "assignment":
                left = a.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    right = a.child_by_field_name("right")
                    kind = "function" if right is not None and right.type == "lambda" \
                        else _var_kind(_text(left))
                    out.append((_text(left), left, kind, node))
    elif lang == "python" and parent_kind == "class" and t == "expression_statement":
        # Class attributes and dataclass fields (`sku: str`, `ATTR = 1`, x = y = f).
        chain = [a for a in node.named_children if a.type == "assignment"]
        while chain and (nxt := chain[-1].child_by_field_name("right")) is not None \
                and nxt.type == "assignment":
            chain.append(nxt)
        for a in chain:
            if a.type == "assignment":
                left = a.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    out.append((_text(left), left, _var_kind(_text(left)), node))
    elif lang in ("javascript", "typescript", "tsx") and parent_kind == "class" \
            and t in ("field_definition", "public_field_definition") \
            and any(c.type == "static" or _text(c) == "static" for c in node.children):
        # Static class fields (`static MAX = 20`); instance fields are state.
        v = node.child_by_field_name("value")
        n = node.child_by_field_name("property") or node.child_by_field_name("name")
        if n is not None and (v is None or v.type not in _FUNCTION_VALUES):
            out.append((_text(n), n, _var_kind(_text(n)), node))
    elif lang in ("javascript", "typescript", "tsx") and (module or parent_kind == "namespace") \
            and t in ("lexical_declaration", "variable_declaration"):
        decls = [d for d in node.named_children if d.type == "variable_declarator"]
        for d in decls:
            n = d.child_by_field_name("name")
            v = d.child_by_field_name("value")
            if n is None or n.type != "identifier" or (v is not None and v.type in _FUNCTION_VALUES):
                continue
            kind = _var_kind(_text(n))
            if v is not None and v.type == "class":
                kind = "class"                      # const Base = class {...}
            elif (v is not None and v.type == "call_expression" and _text(n)[:1].isupper()
                  and any(a.type in _FUNCTION_VALUES
                          for a in (v.child_by_field_name("arguments") or v).named_children)):
                kind = "function"                   # const Input = forwardRef((p, r) => ...)
            out.append((_text(n), n, kind, node if len(decls) == 1 else d))
    elif lang in _JS_LANGS and t == "assignment_expression":
        # Assignments that define — as statements or inside a comma chain
        # (`o.cancel = function(){}, Ht.get = ...` in minified code):
        # exports.f = function, module.exports.f = () =>, Counter.prototype.inc =
        # function (a method of Counter), this.x = () => in a method (a member of
        # its class), Module.locateFile = ... (a function on that object).
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is not None and left.type == "member_expression" and right is not None \
                and right.type in _FUNCTION_VALUES:
            parts = _text(left).split(".")
            prop = left.child_by_field_name("property")
            if prop is None or not all(re.fullmatch(r"[A-Za-z_$][\w$]*", x) for x in parts):
                pass                                   # a[i] = ..., f().x = ...: not a name
            elif parts[0] in ("exports", "module"):
                if module:
                    out.append((parts[-1], prop, "function", node, parts[-1]))
            elif len(parts) == 3 and parts[1] == "prototype":
                out.append((parts[-1], prop, "method", node, f"{parts[0]}.{parts[-1]}"))
            elif parts[0] == "this" and len(parts) == 2 and parent_kind == "method" and "." in parent_qual:
                cls = parent_qual.rsplit(".", 1)[0]
                out.append((parts[-1], prop, "method", node, f"{cls}.{parts[-1]}"))
            elif parts[0] not in ("this", "self"):
                # Module.locateFile = (...) => ..., globalThis.f = function ...
                out.append((parts[-1], prop, "function", node, ".".join(parts)))
    elif lang in ("c", "cpp") and t == "enumerator":
        n = node.child_by_field_name("name")
        if n is not None:
            out.append((_text(n), n, "constant", node))
    elif lang == "kotlin" and t == "enum_entry" and parent_kind == "enum":
        n = next((c for c in node.named_children if _is_ident(c.type)), None)
        if n is not None:
            out.append((_text(n), n, "constant", node))
    elif lang == "java" and t == "enum_constant" and parent_kind == "enum":
        n = node.child_by_field_name("name")
        if n is not None:
            out.append((_text(n), n, "constant", node))
    elif lang in ("c", "cpp") and module:
        if t in ("preproc_def", "preproc_function_def"):
            n = node.child_by_field_name("name")
            if n is not None:
                out.append((_text(n), n, "macro" if t == "preproc_function_def" else "constant", node))
        elif t == "declaration" and not any(c.type == "storage_class_specifier" and _text(c) == "extern"
                                            for c in node.children):
            const = any(c.type == "type_qualifier" and _text(c) in ("const", "constexpr")
                        for c in node.children)
            decls = [node.children[i] for i in range(node.child_count)
                     if node.field_name_for_child(i) == "declarator"]
            for d in decls:
                n = _c_decl_name(d)
                if n is not None:
                    out.append((_text(n), n, _var_kind(_text(n), const), node if len(decls) == 1 else d))
    elif lang == "java" and t in ("field_declaration", "constant_declaration") \
            and parent_kind in ("class", "interface", "enum", "record"):
        mods = next((c for c in node.children if c.type == "modifiers"), None)
        words = set(_text(mods).split()) if mods is not None else set()
        if t == "constant_declaration" or parent_kind == "interface" or {"static", "final"} <= words:
            decls = [node.children[i] for i in range(node.child_count)
                     if node.field_name_for_child(i) == "declarator"]
            for d in decls:
                n = d.child_by_field_name("name")
                if n is not None:
                    out.append((_text(n), n, "constant", node if len(decls) == 1 else d))
    elif lang == "kotlin" and t == "property_declaration":
        mods = next((c for c in node.children if c.type == "modifiers"), None)
        const = mods is not None and "const" in _text(mods).split()
        if module or const:
            v = next((c for c in node.named_children if c.type == "variable_declaration"), None)
            n = next((c for c in v.named_children if c.type in ("identifier", "simple_identifier")),
                     None) if v is not None else None
            if n is not None:
                out.append((_text(n), n, _var_kind(_text(n), const), node))
    return out


def _const_doc(span) -> str:
    """A constant's description: the comment trailing it on its last line
    (`LIMIT = 5  # max retries`), else the comment block directly above it."""
    nxt = span.next_named_sibling
    if nxt is not None and "comment" in nxt.type and nxt.start_point[0] == span.end_point[0]:
        return _clean_doc(_text(nxt))
    prev = span.prev_named_sibling
    if prev is not None and "comment" in prev.type:
        before = prev.prev_named_sibling
        if before is not None and before.end_point[0] == prev.start_point[0]:
            return ""       # that comment trails the line above; it describes that line
    return _doc_line(span, span)


_JS_LANGS = ("javascript", "typescript", "tsx")


def _scoped_enum(node) -> bool:
    """C++ `enum class X {...}`: its constants are X::A, not plain A."""
    return any(c.type in ("class", "struct") for c in node.children)


def _is_require_call(node) -> bool:
    """`require("./m")` or a dynamic `import("./m")` with a literal path."""
    if node.type != "call_expression":
        return False
    fn = node.child_by_field_name("function")
    if fn is None or not (fn.type == "import" or (fn.type == "identifier" and _text(fn) == "require")):
        return False
    args = node.child_by_field_name("arguments")
    return args is not None and any(c.type in ("string", "template_string") for c in args.named_children)


def _require_binding(call):
    """The variable_declarator a require()/import() call initialises, if any
    (`const { a } = require("./m")`, `const m = await import("./m")`)."""
    p = call.parent
    if p is not None and p.type == "await_expression":
        p = p.parent
    return p if p is not None and p.type == "variable_declarator" else None


def _is_local_export(node) -> bool:
    """`export function f() {}` / `export const X = 1`: an export_statement that
    declares something here rather than re-exporting from another module
    (`export {x} from './m'`).  Only the re-export form is an import."""
    return node.type == "export_statement" and node.child_by_field_name("source") is None


def _extract(root, lang: str, lines: list[str],
             imports_out: list | None = None) -> list[Symbol]:
    """Collect definitions, and (when `imports_out` is given) import statement
    nodes in the same traversal — a second full walk of the tree costs about a
    third of the time of a project-wide scan."""
    defs = _DEFS[lang]
    import_types = _IMPORTS.get(lang, frozenset()) if imports_out is not None else frozenset()
    out: list[Symbol] = []
    namespaces: set[str] = set()

    def walk(node, parent_qual: str, parent_kind: str | None, depth: int) -> None:
        for child in node.children:
            if child.type in import_types and not _is_local_export(child):
                # An import statement holds no definitions, so stop here.
                imports_out.append(child)
                continue
            if imports_out is not None and lang in _JS_LANGS and _is_require_call(child):
                imports_out.append(child)     # require("./m") / import("./m")
                continue
            if lang in _JS_LANGS and child.type == "variable_declarator":
                v, n = child.child_by_field_name("value"), child.child_by_field_name("name")
                if v is not None and v.type in ("object", "class") and n is not None \
                        and n.type == "identifier":
                    # const api = { fetchAll() {} } / const Base = class { hello() {} }:
                    # the members belong to api / Base.
                    q = f"{parent_qual}.{_text(n)}" if parent_qual else _text(n)
                    walk(v, q, "object" if v.type == "object" else "class", depth + 1)
                    continue
            for vname, vnode, vkind, vspan, *own_qual in _bindings(child, lang, parent_kind, parent_qual):
                vrow = vspan.start_point[0]
                scope = parent_qual
                if child.type == "enumerator" and parent_kind == "enum" and not _scoped_enum(
                        node.parent if node.type == "enumerator_list" and node.parent is not None else node):
                    scope = parent_qual.rsplit(".", 1)[0] if "." in parent_qual else ""
                out.append(Symbol(
                    name=vname,
                    qualname=own_qual[0] if own_qual else (f"{scope}.{vname}" if scope else vname),
                    kind=vkind,
                    start=vrow + 1,
                    # A #define ends at column 0 of the next line.
                    end=vspan.end_point[0] + (0 if vspan.end_point[1] == 0
                                              and vspan.end_point[0] > vrow else 1),
                    name_line=vnode.start_point[0] + 1,
                    signature=lines[vrow].strip()[:_MAX_SIGNATURE] if vrow < len(lines) else "",
                    depth=depth,
                    doc=_const_doc(vspan),
                ))
            kind = defs.get(child.type)
            if kind is None:
                walk(child, parent_qual, parent_kind, depth)
                continue
            got = _def_name(child, lang, kind)
            if (got is not None and lang in ("c", "cpp") and kind in ("struct", "union", "enum", "class")
                    and child.child_by_field_name("body") is None):
                got = None      # `struct node *next;` names a type, it does not define one
            if got is None:
                walk(child, parent_qual, parent_kind, depth)
                continue
            name, name_row = got
            if kind == "function" and parent_kind in _CONTAINER_KINDS:
                kind = "method"
            if kind == "namespace":
                namespaces.add(name.rsplit(".", 1)[-1])
            if (lang in ("c", "cpp") and kind == "function" and "." in name
                    and name.rsplit(".", 2)[-2] not in namespaces):
                # `double Circle::area() const {...}` out of line: a method of
                # Circle — unless the scope is a namespace declared here.
                kind = "method"
            if lang == "kotlin" and kind == "class" and any(c.type == "interface" for c in child.children):
                kind = "interface"
            if lang == "kotlin" and kind == "class" and any(
                    c.type == "modifiers" and "enum" in _text(c).split() for c in child.children):
                kind = "enum"
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
                doc=_doc_line(child, span),
            ))
            if kind == "typedef":
                # `typedef struct node {...} node_t;` defines node beside node_t,
                # not inside it.
                walk(child, parent_qual, parent_kind, depth)
            else:
                walk(child, qual, kind, depth + 1)

    walk(root, "", None, 0)
    return out


# ── config / markup / script "definitions" ──────────────────────────────────
# These grammars have no classes or functions, but they do have things a model
# looks up by name: a YAML/TOML/JSON key path, an HTML id, a CSS selector, a SQL
# table, a shell function, a Dockerfile stage.  Each extractor returns ordinary
# Symbols, so find_symbol / read_symbol / code_outline work on them unchanged.

_MAX_DATA_DEPTH = 6        # key paths deeper than this are not listed
_MAX_DATA_SYMBOLS = 2000   # a generated lockfile must not flood the index


def _sym(name: str, qual: str, kind: str, node, depth: int, lines: list[str],
         name_node=None, end_row: int | None = None) -> Symbol:
    row = node.start_point[0]
    name_row = name_node.start_point[0] if name_node is not None else row
    if end_row is None:
        end_row = node.end_point[0]
        # A node that ends at column 0 ends on the line before: YAML and TOML
        # blocks swallow the newline that separates them from the next key.
        if node.end_point[1] == 0 and end_row > row:
            end_row -= 1
    return Symbol(name=name, qualname=qual, kind=kind, start=row + 1,
                  end=end_row + 1,
                  name_line=name_row + 1,
                  signature=lines[row].strip()[:_MAX_SIGNATURE] if row < len(lines) else "",
                  depth=depth)


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _value_kind(node) -> str:
    if node is None:
        return "key"
    t = node.type
    if "mapping" in t or t in ("object", "inline_table", "table"):
        return "table"
    if "sequence" in t or t == "array":
        return "list"
    for c in node.named_children:          # YAML wraps values in block_node/flow_node
        if c.type in ("block_mapping", "flow_mapping"):
            return "table"
        if c.type in ("block_sequence", "flow_sequence"):
            return "list"
    return "key"


def _extract_yaml(root, lines: list[str]) -> list[Symbol]:
    out: list[Symbol] = []

    def walk(node, parent: str, depth: int) -> None:
        for c in node.named_children:
            if len(out) >= _MAX_DATA_SYMBOLS:
                return
            if c.type in ("block_mapping_pair", "flow_pair"):
                k = c.child_by_field_name("key")
                v = c.child_by_field_name("value")
                if k is None:
                    continue
                key = _unquote(_text(k))
                qual = f"{parent}.{key}" if parent else key
                out.append(_sym(key, qual, _value_kind(v), c, depth, lines, k))
                if v is not None and depth + 1 < _MAX_DATA_DEPTH:
                    walk(v, qual, depth + 1)
            elif c.type in ("block_sequence_item", "flow_sequence"):
                walk(c, f"{parent}[]" if parent else "[]", depth)
            else:
                walk(c, parent, depth)

    walk(root, "", 0)
    return out


def _toml_key(node) -> list[str]:
    if node.type == "dotted_key":
        parts: list[str] = []
        for c in node.named_children:
            parts.extend(_toml_key(c))
        return parts
    return [_unquote(_text(node))]


def _extract_toml(root, lines: list[str]) -> list[Symbol]:
    out: list[Symbol] = []
    _KEYS = ("bare_key", "quoted_key", "dotted_key")

    def pairs(node, parent: str, depth: int) -> None:
        for c in node.named_children:
            if len(out) >= _MAX_DATA_SYMBOLS:
                return
            if c.type != "pair":
                continue
            k = next((x for x in c.named_children if x.type in _KEYS), None)
            if k is None:
                continue
            parts = _toml_key(k)
            qual = ".".join([parent, *parts] if parent else parts)
            v = c.named_children[-1] if len(c.named_children) > 1 else None
            out.append(_sym(parts[-1], qual, _value_kind(v), c, depth, lines, k))
            if v is not None and v.type == "inline_table" and depth + 1 < _MAX_DATA_DEPTH:
                pairs(v, qual, depth + 1)

    pairs(root, "", 0)   # top-level pairs before the first [table]
    for c in root.named_children:
        if c.type not in ("table", "table_array_element"):
            continue
        k = next((x for x in c.named_children if x.type in _KEYS), None)
        if k is None:
            continue
        parts = _toml_key(k)
        qual = ".".join(parts) + ("[]" if c.type == "table_array_element" else "")
        out.append(_sym(parts[-1], qual, "table", c, 0, lines, k))
        pairs(c, qual, 1)
    return out


def _extract_json(root, lines: list[str]) -> list[Symbol]:
    out: list[Symbol] = []

    def walk(node, parent: str, depth: int) -> None:
        for c in node.named_children:
            if len(out) >= _MAX_DATA_SYMBOLS or depth >= _MAX_DATA_DEPTH:
                return
            if c.type == "pair":
                k = c.child_by_field_name("key")
                v = c.child_by_field_name("value")
                if k is None:
                    continue
                key = _unquote(_text(k))
                qual = f"{parent}.{key}" if parent else key
                out.append(_sym(key, qual, _value_kind(v), c, depth, lines, k))
                if v is not None:
                    walk(v, qual + ("[]" if v.type == "array" else ""), depth + 1)
            elif c.type == "array":
                walk(c, f"{parent}[]" if parent else "[]", depth)
            elif c.type == "object":
                walk(c, parent, depth)

    walk(root, "", 0)
    return out


def _html_attrs(start_tag) -> dict[str, str]:
    attrs = {}
    for a in start_tag.named_children:
        if a.type != "attribute":
            continue
        n = next((x for x in a.named_children if x.type == "attribute_name"), None)
        v = next((x for x in a.named_children
                  if x.type in ("attribute_value", "quoted_attribute_value")), None)
        if n is not None:
            attrs[_text(n).lower()] = _unquote(_text(v)) if v is not None else ""
    return attrs


def _extract_html(root, lines: list[str]) -> list[Symbol]:
    out: list[Symbol] = []

    def walk(node, parent: str, depth: int) -> None:
        for c in node.named_children:
            if len(out) >= _MAX_DATA_SYMBOLS:
                return
            if c.type not in ("element", "script_element", "style_element", "template_element"):
                walk(c, parent, depth)
                continue
            tag_node = next((x for x in c.named_children
                             if x.type in ("start_tag", "self_closing_tag")), None)
            if tag_node is None:
                walk(c, parent, depth)
                continue
            tn = next((x for x in tag_node.named_children if x.type == "tag_name"), None)
            tag = _text(tn).lower() if tn is not None else ""
            attrs = _html_attrs(tag_node)
            name = kind = None
            if attrs.get("id"):
                name, kind = attrs["id"], "id"
            elif c.type == "script_element":
                name, kind = (attrs.get("src") or "script"), "script"
            elif c.type == "style_element":
                name, kind = "style", "style"
            elif "-" in tag:
                name, kind = tag, "element"
            elif tag == "template":
                name, kind = "template", "template"
            if name is None:
                walk(c, parent, depth)
                continue
            qual = f"{parent}.{name}" if parent and kind == "id" else name
            out.append(_sym(name, qual, kind, c, depth, lines, tag_node))
            walk(c, qual if kind == "id" else parent, depth + 1)

    walk(root, "", 0)
    return out


def _extract_css(root, lines: list[str]) -> list[Symbol]:
    out: list[Symbol] = []

    def walk(node, depth: int) -> None:
        for c in node.named_children:
            if len(out) >= _MAX_DATA_SYMBOLS:
                return
            if c.type == "rule_set":
                sels = next((x for x in c.named_children if x.type == "selectors"), None)
                if sels is not None:
                    # One symbol per selector in a comma list, so '.btn' finds
                    # the rule written as '.btn, .link'.
                    for sel in " ".join(_text(sels).split()).split(","):
                        sel = sel.strip()
                        if sel:
                            out.append(_sym(sel, sel, "rule", c, depth, lines, sels))
                block = next((x for x in c.named_children if x.type == "block"), None)
                if block is not None:
                    for d in block.named_children:
                        pn = next((x for x in d.named_children if x.type == "property_name"), None) \
                            if d.type == "declaration" else None
                        if pn is not None and _text(pn).startswith("--"):
                            out.append(_sym(_text(pn), _text(pn), "var", d, depth + 1, lines, pn))
                    walk(block, depth + 1)
            elif c.type == "keyframes_statement":
                n = next((x for x in c.named_children if x.type == "keyframes_name"), None)
                if n is not None:
                    out.append(_sym(_text(n), _text(n), "keyframes", c, depth, lines, n))
            elif c.type in ("media_statement", "supports_statement", "at_rule"):
                head = lines[c.start_point[0]].strip() if c.start_point[0] < len(lines) else ""
                head = head.split("{", 1)[0].strip()[:_MAX_SIGNATURE] or c.type
                out.append(_sym(head, head, "media" if c.type == "media_statement" else "at-rule",
                                c, depth, lines))
                block = next((x for x in c.named_children if x.type == "block"), None)
                if block is not None:
                    walk(block, depth + 1)

    walk(root, 0)
    return out


def _extract_sql(root, lines: list[str]) -> list[Symbol]:
    out: list[Symbol] = []
    stack = [root]
    while stack and len(out) < _MAX_DATA_SYMBOLS:
        node = stack.pop()
        if not node.type.startswith("create_"):
            stack.extend(reversed(node.named_children))
            continue
        kind = node.type.removeprefix("create_")
        ref = next((x for x in node.named_children if x.type == "object_reference"), None)
        if kind == "index":
            ref = node.child_by_field_name("column") or ref
        if ref is None:
            continue
        nn = ref.child_by_field_name("name") or ref
        name = _unquote(_text(nn).strip("`[]"))
        qual = _unquote(_text(ref).replace('"', "").replace("`", ""))
        out.append(_sym(name, qual, kind, node, 0, lines, nn))
        if kind == "table":
            cols = next((x for x in node.named_children if x.type == "column_definitions"), None)
            for col in (cols.named_children if cols is not None else []):
                cn = col.child_by_field_name("name") if col.type == "column_definition" else None
                if cn is not None:
                    out.append(_sym(_text(cn), f"{qual}.{_text(cn)}", "column", col, 1, lines, cn))
    return out


def _extract_bash(root, lines: list[str]) -> list[Symbol]:
    out: list[Symbol] = []

    def walk(node, parent: str, depth: int) -> None:
        for c in node.named_children:
            if c.type == "function_definition":
                n = c.child_by_field_name("name")
                if n is None:
                    continue
                qual = f"{parent}.{_text(n)}" if parent else _text(n)
                out.append(_sym(_text(n), qual, "function", c, depth, lines, n))
                walk(c, qual, depth + 1)
            else:
                walk(c, parent, depth)

    walk(root, "", 0)
    return out


def _extract_dockerfile(root, lines: list[str]) -> list[Symbol]:
    out: list[Symbol] = []
    instrs = root.named_children
    froms = [i for i, c in enumerate(instrs) if c.type == "from_instruction"]
    stage_q = ""
    for i, c in enumerate(instrs):
        if c.type == "from_instruction":
            alias = c.child_by_field_name("as")
            spec = next((x for x in c.named_children if x.type == "image_spec"), None)
            name = _text(alias) if alias is not None else (_text(spec) if spec is not None
                                                            else f"stage{froms.index(i)}")
            nxt = next((j for j in froms if j > i), None)
            end_row = (instrs[nxt].start_point[0] - 1 if nxt is not None
                       else instrs[-1].end_point[0])
            out.append(_sym(name, name, "stage", c, 0, lines, alias or spec, end_row=end_row))
            stage_q = name
        elif c.type in ("arg_instruction", "env_instruction"):
            kind = "arg" if c.type == "arg_instruction" else "env"
            targets = [c] if kind == "arg" else [x for x in c.named_children if x.type == "env_pair"]
            for t in targets:
                n = t.child_by_field_name("name")
                if n is None:
                    continue
                qual = f"{stage_q}.{_text(n)}" if stage_q else _text(n)
                out.append(_sym(_text(n), qual, kind, t, 1 if stage_q else 0, lines, n))
    return out


_DATA_EXTRACTORS = {
    "yaml": _extract_yaml, "toml": _extract_toml, "json": _extract_json,
    "html": _extract_html, "css": _extract_css, "sql": _extract_sql,
    "bash": _extract_bash, "dockerfile": _extract_dockerfile,
}


def _symbols_for(root, lang: str, lines: list[str], imports_out: list | None = None) -> list[Symbol]:
    data = _DATA_EXTRACTORS.get(lang)
    if data is not None:
        return data(root, lines)
    symbols = _extract(root, lang, lines, imports_out)
    kw = _KEYWORDS.get(lang)
    if kw:
        # `#ifdef X ... #endif else if (c) {...}` parses as a function named `if`.
        symbols = [s for s in symbols if s.name not in kw]
    return symbols


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
            if _is_ident(n.type):
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
    names = ["*"] if any(c.type == "wildcard_import" for c in node.children) else []
    if node.type == "future_import_statement":
        return [("__future__", [_text(c) for i, c in enumerate(node.children)
                                if node.field_name_for_child(i) == "name"])]
    for i, c in enumerate(node.children):
        if node.field_name_for_child(i) != "name":
            continue
        # `from . import net as net_mod` imports `net`; the alias is local.
        if c.type == "aliased_import":
            c = c.child_by_field_name("name") or c
        names.append(_text(c))
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
            elif any(c.type == "asterisk" for c in node.children):
                pairs = [(_text(scoped) + ".*", [])]     # import a.b.*: the whole package
            else:
                full = _text(scoped)
                pairs = [(full, [full.rsplit(".", 1)[-1]] if "." in full else [])]
        elif lang == "kotlin":
            q = next((c for c in node.children if c.type == "qualified_identifier"), None)
            if q is None:
                pairs = []
            elif any(_text(c) == "*" for c in node.children):
                pairs = [(_text(q) + ".*", [])]
            else:
                full = _text(q)
                pairs = [(full, [full.rsplit(".", 1)[-1]] if "." in full else [])]
        elif node.type == "call_expression":   # require("./m") / import("./m")
            args = node.child_by_field_name("arguments")
            lit = next((c for c in args.named_children if c.type in ("string", "template_string")), None)
            decl = _require_binding(node)
            target = decl.child_by_field_name("name") if decl is not None else None
            names = ([_text(target)] if target is not None and target.type == "identifier"
                     else _leaf_names(target) if target is not None else [])
            pairs = [(_strip_quotes(_text(lit)).strip("`"), names)] if lit is not None else []
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
    parsed = _parse_bytes(lang, path.read_bytes())
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


# The project index (code_index.ProjectIndex) registers itself here while /index
# is on, so every index() caller — read_file's footer, the directory outline,
# find_symbol's line form — reads the one shared store instead of keeping a
# second copy of the same symbols in _index_cache.
_index_provider = None  # callable(key: str, stamp) -> _Index | None
# ...and its file list, so a directory scan sees the same files the index does
# (git-tracked, not git-ignored) instead of walking the disk itself.
_index_files = None     # callable(root: Path) -> list[Path] | None


def set_index_provider(fn, files=None) -> None:
    global _index_provider, _index_files
    _index_provider = fn
    _index_files = files if fn is not None else None


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
    provider = _index_provider
    if provider is not None:
        got = provider(key, stamp)
        if got is not None:
            return got
    hit = _index_cache.get(key)
    if hit and hit[0] == stamp:
        _index_cache.move_to_end(key)
        return hit[1]
    cached = _cache.get(key)
    if cached and cached[0] == stamp:
        _put_index(key, stamp, cached[1])
        return _index_cache[key][1]
    idx = make_index(_parse_bytes(lang, path.read_bytes()))
    while len(_index_cache) >= _MAX_INDEX_CACHE:
        _index_cache.popitem(last=False)
    _index_cache[key] = (stamp, idx)
    return idx  # tree goes out of scope here — that is the point


def package_of(parsed: _Parsed) -> str:
    """The `package a.b.c` a Java/Kotlin file declares, '' for everything else."""
    for c in parsed.tree.root_node.named_children:
        if c.type in ("package_declaration", "package_header"):
            name = next((x for x in c.named_children if "identifier" in x.type), None)
            return _text(name).replace(" ", "") if name is not None else ""
    return ""


def parse_uncached(path: Path, raw: bytes | None = None) -> _Parsed | None:
    """Parse without touching either cache — for the project indexer, which
    keeps what it needs itself and must not evict the trees read_symbol uses."""
    lang = language_for(path)
    if lang is None:
        return None
    if raw is None:
        raw = path.read_bytes()
    return _parse_bytes(lang, raw)


def _parse_bytes(lang: str, raw: bytes) -> _Parsed:
    """One parse of a file's bytes: tree, symbols, imports — and, for HTML, the
    JavaScript of its inline <script> blocks as an embedded parse."""
    tree = _parser(lang).parse(raw)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    imp_nodes: list = []
    symbols = _symbols_for(tree.root_node, lang, lines, imp_nodes)
    parsed = _Parsed(lang, lines, tree, symbols, _build_imports(imp_nodes, lang, lines))
    if lang == "html":
        parsed.sub = _inline_scripts(tree, raw)
        if parsed.sub is not None:
            parsed.symbols = symbols + parsed.sub.symbols
            parsed.imports = parsed.imports + parsed.sub.imports
    return parsed


# ── JavaScript inside HTML ───────────────────────────────────────────────────
# A single-file web app keeps all its code in <script> blocks.  They are parsed
# as one JavaScript "mirror" of the page: every script line at its original row
# and column, everything else blank — so rows and columns in the mirror ARE the
# HTML file's, and every symbol, occurrence and caller maps straight back.

_JS_SCRIPT_TYPES = ("", "text/javascript", "application/javascript", "module", "text/babel")


def _inline_scripts(tree, raw: bytes) -> "_Parsed | None":
    if not grammar_available("javascript"):
        return None
    nrows = raw.count(b"\n") + 1
    rows = [""] * nrows
    found = False
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type != "script_element":
            stack.extend(n.children)
            continue
        start = next((c for c in n.named_children if c.type == "start_tag"), None)
        attrs = _html_attrs(start) if start is not None else {}
        body = next((c for c in n.named_children if c.type == "raw_text"), None)
        if body is None or "src" in attrs or attrs.get("type", "").lower() not in _JS_SCRIPT_TYPES:
            continue
        row, col = body.start_point
        for i, text in enumerate(_text(body).split("\n")):
            if row + i < nrows:
                rows[row + i] = (" " * col if i == 0 else "") + text
        found = True
    if not found:
        return None
    return _parse_bytes("javascript", "\n".join(rows).encode("utf-8"))


def make_index(parsed: _Parsed) -> _Index:
    return _Index(parsed.lang, len(parsed.lines), parsed.symbols, parsed.imports,
                  parsed.tree.root_node.has_error)


# ── identifier occurrences ───────────────────────────────────────────────────
# One tree-sitter query per grammar capturing every *identifier leaf.  Running it
# in C is ~3x faster than walking the tree in Python, which is what makes a
# whole-project occurrence index affordable (5500 files: ~3 s).

_ident_cursors: dict = {}


def _ident_cursor(lang: str):
    cur = _ident_cursors.get(lang)
    if cur is None:
        from tree_sitter import Query, QueryCursor
        L = _parser(lang).language
        kinds = sorted({L.node_kind_for_id(i) for i in range(L.node_kind_count)
                        if L.node_kind_is_named(i) and L.node_kind_is_visible(i)
                        and _is_ident(L.node_kind_for_id(i) or "")})
        if not kinds:
            _ident_cursors[lang] = False
            return False
        cur = _ident_cursors[lang] = QueryCursor(
            Query(L, "[" + " ".join(f"({k})" for k in kinds) + "] @id"))
    return cur


def identifier_rows(parsed: _Parsed) -> set[tuple[str, int]]:
    """Every (identifier text, 1-based line) in a parsed file, deduplicated."""
    if parsed.sub is not None:
        return identifier_rows(parsed.sub)      # HTML: its inline scripts
    cur = _ident_cursor(parsed.lang) if parsed.lang in _DEFS else False
    if not cur:
        return set()
    caps = cur.captures(parsed.tree.root_node).get("id", [])
    kw = _KEYWORDS.get(parsed.lang, ())
    return {(t, c.start_point[0] + 1) for c in caps
            if c.child_count == 0 and (t := _text(c)) not in kw}


# Reserved words are never names — but in a region tree-sitter could not parse
# (C code passed as a macro argument: `DEFINE_OP(MRC, if (x) {...})`) they
# come out as identifiers, and `if (x)` as a call of `if`.
_C_KEYWORDS = frozenset("""auto break case char const continue default do double else enum
    extern float for goto if inline int long register restrict return short signed sizeof
    static struct switch typedef union unsigned void volatile while _Bool _Complex _Atomic
    _Alignas _Alignof _Generic _Noreturn _Static_assert _Thread_local""".split())
_KEYWORDS = {"c": _C_KEYWORDS,
             "cpp": _C_KEYWORDS | frozenset("""alignas alignof and asm bool catch class
                 const_cast constexpr decltype delete dynamic_cast explicit export false friend
                 mutable namespace new noexcept not nullptr operator or private protected public
                 reinterpret_cast static_assert static_cast template this throw true try typeid
                 typename using virtual""".split())}


def _in_closing_tag(node) -> bool:
    p = node.parent
    for _ in range(3):
        if p is None:
            return False
        if p.type == "jsx_closing_element":
            return True
        p = p.parent
    return False


# Fields that hold the NAME of what a node defines, across the grammars.
_DEF_NAME_FIELDS = {"name", "declarator", "key", "property", "pattern", "left", "type"}


def _is_def_name(node) -> bool:
    """This identifier is where something is defined (its name field), not a
    use of that name on the same line."""
    if node.parent is not None and node.parent.type in _MEMBER_NODES \
            and node.parent.type not in ("qualified_identifier", "scoped_identifier"):
        # obj.name / this.name is a use — unless it is what an assignment
        # defines: `exports.makeCounter = function ...`, `C.prototype.inc = ...`
        return _field_of(node) in ("property", "attribute", "field") \
            and _field_of(node.parent) == "left"
    if node.parent is not None and node.parent.type in ("qualified_identifier", "scoped_identifier",
                                                        "destructor_name", "operator_name"):
        node = node.parent
    field = _field_of(node)
    if field in _DEF_NAME_FIELDS:
        return True
    # grammars that name definitions without a field (Kotlin, some Rust/C++ nodes)
    return field is None and node.parent is not None and _field_of(node.parent) != "arguments" \
        and node.parent.type not in _CALL_NODES and node.parent.type not in _MEMBER_NODES


def references_in(parsed: _Parsed, bare: str, rows: set[int] | None = None
                  ) -> dict[int, tuple[str, str | None, str | None]]:
    """{0-based row: (role, receiver, receiver's declared type)} for the uses of
    `bare` in one file.  With
    `rows`, only those rows are examined and subtrees outside them are skipped,
    so a lookup driven by the occurrence index touches a few nodes, not all."""
    if parsed.sub is not None:
        return references_in(parsed.sub, bare, rows)   # HTML: its inline scripts
    if bare in _KEYWORDS.get(parsed.lang, ()):
        return {}
    target = bare.encode()
    def_lines = {s.name_line for s in parsed.symbols if s.name == bare}
    out: dict[int, tuple[str, str | None]] = {}
    lo = min(rows) if rows else 0
    hi = max(rows) if rows else 1 << 30
    stack = [parsed.tree.root_node]
    while stack:
        node = stack.pop()
        if node.end_point[0] < lo or node.start_point[0] > hi:
            continue
        if node.child_count == 0:
            # Leaf identifier nodes only: comments and string contents are
            # separate node types, and substrings never match exactly.
            if _is_ident(node.type) and node.text == target and not _in_closing_tag(node):
                r = node.start_point[0]
                if rows is not None and r not in rows:
                    continue
                # A definition is the name node itself, not every use on its line:
                # minified code puts a whole library — definitions and calls — on one line.
                got = _classify(node, parsed.lang, r + 1 in def_lines and _is_def_name(node))
                prev = out.get(r)
                # "call" is the most informative label for a shared row.
                if prev is None or (prev[0] != "call" and got[0] == "call"):
                    out[r] = got
        else:
            stack.extend(node.children)
    return out


# A small model does not reliably tell a function from a method (an out-of-line
# C++ `Circle::area` is a method; asked for as kind="function" it vanished and
# the 9B wandered — evals 2026-09-23), so these kinds find each other.
CALLABLE_KINDS = {"function", "method", "constructor"}


def kind_matches(actual: str, wanted: str) -> bool:
    wanted = wanted.strip().lower()
    return actual == wanted or (actual in CALLABLE_KINDS and wanted in CALLABLE_KINDS)


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
    if root.is_file():
        if language_for(root):
            yield root
        return
    listed = _index_files(root) if _index_files is not None else None
    if listed is not None:
        yield from listed
        return
    for fpath in walk_files(root):
        if language_for(fpath) is None:
            continue
        try:
            if fpath.stat().st_size > MAX_SCAN_FILE_BYTES:
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


# Public because read_file's footer, find_symbol's line form and read_symbol's
# line form all need the same "which definition is line N in?" lookup.
def enclosing(idx: "_Index", line: int) -> Symbol | None:
    return _enclosing(idx.symbols, line)


_LINE_QUERY = re.compile(r"[Ll]?(\d+)")


def line_query(name: str) -> int | None:
    """The line number in a name like '1300' or 'L1300', else None."""
    m = _LINE_QUERY.fullmatch(name.strip())
    return int(m.group(1)) if m else None


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


def _binds(node) -> bool:
    """True when this identifier leaf introduces a name (parameter, local)."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in ("pointer_declarator", "reference_declarator"):
        return _binds(parent)             # `Shape& s`, `int *p`
    if parent.type in ("tuple", "list", "parenthesized_expression"):
        return _binds(parent)             # with ... as (a, b) / (a, b) = ...
    if parent.type in _PARAM_NODES:
        return True
    field = _field_of(node)
    if field == "name" and (parent.type in _FUNC_SCOPES or parent.type in _NESTED_CLASSES):
        return True                       # a nested def / class binds its name
    if parent.type == "aliased_import":
        return field == "alias"           # import a as b / from x import y as b
    if parent.type == "dotted_name" and parent.parent is not None:
        stmt = parent.parent
        if stmt.type == "import_statement":      # import a.b binds a
            return parent.named_children[0].id == node.id
        if stmt.type == "import_from_statement" and _field_of(parent) == "name":
            return True                          # from x import y binds y
    want = _BINDING_FIELDS.get(parent.type)
    return want is not None and field == want


_CLASS_BODIES = {"class_body", "enum_body", "declaration_list", "field_declaration_list"}
# Class definitions nested in a function: their name binds there, their body
# is a scope of its own (Python class attributes are not the function's locals).
_NESTED_CLASSES = {"class_definition", "class_declaration", "class"}


# Nodes that open a block scope in block-scoped languages.  A `const total` in
# an if-block does not hide a call to total() after the block.
_BLOCKS = {"statement_block", "block", "compound_statement", "switch_block", "switch_body",
           "for_statement", "for_in_statement", "enhanced_for_statement", "catch_clause",
           "if_statement", "while_statement", "control_structure_body"}


def _function_scoped(binding) -> bool:
    """Python bindings and JS `var` belong to the whole function; parameters
    belong to it everywhere.  Everything else is block-scoped."""
    if binding.parent is not None and binding.parent.type in _PARAM_NODES \
            and binding.parent.type not in ("variable_declaration", "object_pattern",
                                            "array_pattern", "as_pattern_target",
                                            "pattern_list", "tuple_pattern", "type_pattern"):
        return True
    n = binding
    own_function = True
    while n.parent is not None:
        if own_function and n.type == "variable_declaration" \
                and n.parent.type != "property_declaration":
            return True                               # JS `var`
        if n.type in _FUNC_SCOPES:
            own_function = False      # an enclosing function's `var` is not this binding's
        n = n.parent
    return n.type == "module"                         # Python's root node


def _in_scope(binding, use) -> bool:
    if _function_scoped(binding):
        return True
    block = binding.parent
    while block is not None and block.type not in _BLOCKS and block.type not in _FUNC_SCOPES:
        block = block.parent
    return block is None or (block.start_byte <= use.start_byte and use.end_byte <= block.end_byte)


def _scan_binding(scope, target: bytes, use=None):
    """A binding of `target` in this scope.  A nested function or class is a
    scope of its own: only its NAME binds here, never its parameters or body —
    `items.map((total) => total)` must not make `total` local to the outer
    function.  The scope's own name belongs to the scope around it."""
    stack = [c for i, c in enumerate(scope.children) if scope.field_name_for_child(i) != "name"]
    while stack:
        n = stack.pop()
        if n.child_count == 0:
            if (n.text == target and _is_ident(n.type) and _binds(n) and not _imported_binding(n)
                    and (use is None or _in_scope(n, use))):
                return n
        elif n.type in _FUNC_SCOPES or n.type in _NESTED_CLASSES:
            nm = n.child_by_field_name("name")
            if nm is not None and nm.text == target and _is_ident(nm.type):
                return nm
        else:
            stack.extend(n.children)
    return None


def _declared_global(scope, target: bytes) -> bool:
    """Python `global x` in this function: x is the module's, not a local."""
    stack = [scope]
    while stack:
        n = stack.pop()
        if n.type == "global_statement":
            if any(c.text == target for c in n.named_children):
                return True
        elif n is scope or n.type not in _FUNC_SCOPES:
            stack.extend(n.children)
    return False


def _binding_of(node, fields: bool = False):
    """The parameter / local that binds this identifier's name in the enclosing
    function, or None (a module-level or outside name).  With `fields`, a field
    declared in the enclosing class body counts too (`lines` in `lines.add()`)."""
    scope = node.parent
    while scope is not None and scope.type not in _FUNC_SCOPES:
        scope = scope.parent
    if scope is None:
        return None
    innermost = scope
    found = None
    root = scope
    while root.parent is not None:
        root = root.parent
    python = root.type == "module"            # only Python has `global x`
    while scope is not None:
        if python and _declared_global(scope, node.text):
            return None
        found = _scan_binding(scope, node.text, use=node)
        if found is not None:
            break
        # a closure: the name may be a local of an enclosing function
        scope = scope.parent
        while scope is not None and scope.type not in _FUNC_SCOPES:
            scope = scope.parent
    if found is None and fields:
        body = innermost.parent
        while body is not None and body.type not in _CLASS_BODIES:
            body = body.parent
        if body is not None:
            found = _scan_binding(body, node.text)
    return found


def _is_local(node) -> bool:
    """The name is a parameter or local of the enclosing function, so a bare use
    of it there refers to that, not to a same-named definition elsewhere."""
    return _binding_of(node) is not None


# ── declared types (light typing from syntax) ────────────────────────────────
# The type a variable's declaration spells out — `Cart cart`, `cart: Cart`,
# `new Cart()`, `Cart()`, `Cart::new()` — so `cart.add(...)` can be told apart
# from another class's add.  Nothing is inferred: an unwritten type is None.

_NOT_TYPES = {"var", "val", "let", "auto", "const", "mut", "dyn", "impl", "final",
              "None", "null", "undefined", "NoneType"}
# Wrappers whose type argument is the object's real type: Optional[Order] is an Order.
_WRAPPERS_T = {"Optional", "Option", "Nullable", "Box", "Rc", "Arc", "Ref", "RefCell",
               "Mutex", "Readonly", "typing"}
_TYPE_PATH = re.compile(r"[A-Za-z_]\w*(?:(?:::|\.)[A-Za-z_]\w*)*")


def _type_name(t) -> str | None:
    """'Optional[Order]' / 'Cart | null' / '&mut Cart' / 'List<Line>' / 'geo::Shape'
    -> 'Order' / 'Cart' / 'Cart' / 'List' / 'Shape'."""
    for path in _TYPE_PATH.findall(_text(t)):
        last = re.split(r"::|\.", path)[-1]
        if last not in _NOT_TYPES and last not in _WRAPPERS_T:
            return last
    return None


def _ctor_type(v) -> str | None:
    """`new Cart()`, `Cart()`, `Cart::new()`, `Item { .. }` -> 'Cart' / 'Item'."""
    if v.type == "await_expression" and v.named_child_count:
        v = v.named_children[0]
    if v.type in ("new_expression", "object_creation_expression"):
        t = v.child_by_field_name("constructor") or v.child_by_field_name("type")
        return _type_name(t) if t is not None else None
    if v.type == "struct_expression":
        t = v.child_by_field_name("name")
        return _type_name(t) if t is not None else None
    if v.type in ("call", "call_expression"):
        fn = v.child_by_field_name("function") or (v.named_children[0] if v.named_child_count else None)
        if fn is None:
            return None
        name = _text(fn)
        if "::" in name:                      # Rust Cart::new()
            return name.rsplit("::", 2)[-2].split("<")[0]
        if _is_ident(fn.type) and name[:1].isupper():   # Python/Kotlin Cart()
            return name
    return None


def declared_type(binding) -> str | None:
    """The type the declaration of `binding` (a parameter / local name) spells
    out: a `type` field, a Kotlin type child, or a constructor-call initialiser."""
    n = binding.parent
    for _ in range(4):
        if n is None or n.type in _FUNC_SCOPES:
            return None
        t = n.child_by_field_name("type")
        if t is not None and (name := _type_name(t)):
            return name
        t = next((c for c in n.named_children if c.type in ("user_type", "type_identifier")
                  and c.id != binding.id), None)
        if t is not None and (name := _type_name(t)):
            return name
        v = n.child_by_field_name("value") or n.child_by_field_name("right")
        if v is None and n.type == "property_declaration" and n.named_child_count:
            v = n.named_children[-1]
        if v is not None and (name := _ctor_type(v)):
            return name
        n = n.parent
    return None


def _receiver_type(recv_node, lang: str) -> str | None:
    if recv_node is None:
        return None
    if _is_ident(recv_node.type):
        b = _binding_of(recv_node, fields=True)
        if b is not None:
            return declared_type(b)
        return _imported_name(recv_node, lang)
    return _ctor_type(recv_node)              # new Cart().add(...), Cart().total()


def _imported_name(node, lang: str) -> str | None:
    """The name a module-level import binds this receiver to, as imported:
    `from . import session as session_mod` -> 'session', `import * as api` ->
    'api', `import a.Cart` -> 'Cart'.  So `session_mod.save()` is known to be
    the module's save, not some class's.  None when no import binds it."""
    kinds = _IMPORTS.get(lang, ())
    if not kinds:
        return None
    target = node.text
    root = node
    while root.parent is not None:
        root = root.parent
    for st in root.named_children:
        if st.type not in kinds:
            continue
        if lang in ("java", "kotlin", "rust"):
            # One path per statement; it binds its last name, or an alias.
            path = st.child_by_field_name("argument") or \
                (st.named_children[0] if st.named_child_count else None)
            if path is None or "{" in _text(path):
                continue
            if path.type == "use_as_clause":             # Rust use a::B as C
                alias, path = path.child_by_field_name("alias"), path.child_by_field_name("path")
            else:
                alias = next((c for c in st.named_children[1:] if _is_ident(c.type)), None)
            if path is None:
                continue
            last = re.split(r"[.:]+", _text(path).replace("static ", ""))[-1]
            if (alias.text if alias is not None else last.encode()) == target:
                return last
            continue
        stack = [st]
        while stack:
            n = stack.pop()
            if n.child_count:
                stack.extend(n.children)
                continue
            if n.text != target or not _is_ident(n.type):
                continue
            p = n.parent
            if lang == "python":
                if not _binds(n):
                    continue                              # the module path, not a name
                if p is not None and p.type == "aliased_import":
                    return re.split(r"[.]+", _text(p.child_by_field_name("name")))[-1]
                return _text(n)
            if p is not None and p.type == "import_specifier":
                alias = p.child_by_field_name("alias")
                if alias is not None:
                    if alias.id != n.id:
                        continue                          # {a as b}: `a` is not bound here
                    return _text(p.child_by_field_name("name"))
            return _text(n)
    return None


def _imported_binding(node) -> bool:
    """`const { a } = await import("./m")`: `a` is the imported function, not a
    local of its own."""
    d, depth = node.parent, 0
    while d is not None and depth < 4 and d.type != "variable_declarator":
        d, depth = d.parent, depth + 1
    if d is None or d.type != "variable_declarator":
        return False
    v = d.child_by_field_name("value")
    if v is not None and v.type == "await_expression" and v.named_child_count:
        v = v.named_children[0]
    return v is not None and _is_require_call(v)


def _is_prototype(node) -> bool:
    """C/C++: the name in `int clamp(int v);` — declared, not defined or used."""
    d = node.parent
    while d is not None and d.type in ("qualified_identifier", "function_declarator",
                                       "pointer_declarator", "reference_declarator"):
        if d.type == "function_declarator":
            host = d.parent
            while host is not None and host.type in ("pointer_declarator", "reference_declarator"):
                host = host.parent
            return host is not None and host.type in ("declaration", "field_declaration") \
                and not _in_function_body(host)
        d = d.parent
    return False


def _in_function_body(node) -> bool:
    p = node.parent
    while p is not None:
        if p.type in ("compound_statement", "function_definition"):
            return True
        if p.type in ("translation_unit", "field_declaration_list", "declaration_list"):
            return False
        p = p.parent
    return False


def _misparsed_c_call(node) -> bool:
    """C statements tree-sitter reads as something else, mostly next to a
    preprocessor split or inside code passed to a macro:
    * `x += f(a);)` / `free(p);` as a DECLARATION of `f` — a function
      prototype inside a function body is legal but practically unheard of;
    * `f(a, b);` as a macro type specifier `f(type)` in a declaration."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "macro_type_specifier" and _field_of(node) == "name":
        return _in_function_body(parent)
    d = parent          # directly: `(*lookupEntry)(void*)` is a function-pointer variable
    if d.type != "function_declarator" or _field_of(node) != "declarator":
        return False
    host = d.parent
    while host is not None and host.type in ("pointer_declarator", "init_declarator"):
        host = host.parent
    return host is not None and host.type == "declaration" and _in_function_body(host)


def _kotlin_misparsed_call(node):
    """Calls tree-sitter-kotlin parses as something else, recovered from shape.
    * `f<T>(x)` / `f<T> { }` / `a.f<T>(x)` come out as the comparison
      `(f < T) > (x)`; chained comparisons are a compile error in Kotlin, so that
      shape can only be a call with type arguments.
    * `!f(x)`, `a ?: f<T>()` and `k to f<T>()` come out as a call of the
      whole operator expression."""
    parent = node.parent
    if parent is None:
        return None
    callee, recv = node, None
    if parent.type == "navigation_expression" and parent.named_children[-1].id == node.id:
        callee, recv = parent, _text(parent.named_children[0])[:40]
    lt = callee.parent
    if lt is not None and lt.type == "binary_expression" and lt.child_count == 3 \
            and lt.children[0].id == callee.id and lt.children[1].type == "<":
        gt = lt.parent
        if gt is not None and gt.type == "binary_expression" and gt.child_count == 3 \
                and gt.children[0].id == lt.id and gt.children[1].type == ">" \
                and gt.children[2].type in ("parenthesized_expression", "lambda_literal"):
            return "call", recv, (callee.named_children[0] if recv else None)
    if parent.type in ("unary_expression", "binary_expression", "infix_expression", "elvis_expression") \
            and parent.named_children[-1].id == node.id and parent.parent is not None \
            and parent.parent.type == "call_expression" and parent.parent.named_children[0].id == parent.id:
        # `!f(x)`, `a ?: f<T>()`, `"k" to f<T>()`: the grammar calls the whole
        # operator expression; the call belongs to its last operand.
        return "call", None, None
    return None


def _classify(node, lang: str, is_def: bool) -> tuple[str, str | None, str | None]:
    """(role, receiver) for an identifier leaf: what the use actually is, and
    what it hangs off when it is a member access.  Purely syntactic — there is
    no scope resolution here, so the receiver is reported rather than resolved."""
    role, recv, recv_node = _classify_node(node, lang, is_def)
    return role, recv, _receiver_type(recv_node, lang) if role == "call" and recv_node is not None else None


def _classify_node(node, lang: str, is_def: bool):
    if is_def:
        return "def", None, None
    import_types = _IMPORTS.get(lang, ())
    anc, depth = node.parent, 0
    while anc is not None and depth < _IMPORT_ANCESTOR_DEPTH:
        if anc.type in import_types and not _is_local_export(anc):
            return "import", None, None
        if (lang in _JS_LANGS and anc.type == "variable_declarator"
                and _field_of(node) != "value"):
            value = anc.child_by_field_name("value")
            if value is not None and value.type == "await_expression" and value.named_child_count:
                value = value.named_children[0]
            name = anc.child_by_field_name("name")
            if (value is not None and _is_require_call(value) and name is not None
                    and name.start_byte <= node.start_byte and node.end_byte <= name.end_byte):
                return "import", None, None     # const { a } = require("./m")
        anc, depth = anc.parent, depth + 1

    parent = node.parent
    if parent is None:
        return "other", None, None
    if lang in ("c", "cpp"):
        if _is_prototype(node):
            return "decl", None, None
        if lang == "c" and _misparsed_c_call(node):      # C++: `Foo f(a, b);` constructs
            return "call", None, None
    if parent.type not in _MEMBER_NODES and _is_local(node):
        return "local", None, None
    if lang == "kotlin":
        call = _kotlin_misparsed_call(node)
        if call is not None:
            return call
    field = _field_of(node)
    receiver = recv_node = None
    if parent.type in _MEMBER_NODES:
        first = next((ch for ch in parent.children if ch.is_named), None)
        if field in _RECEIVER_FIELDS or (field is None and first is not None
                                         and first.id == node.id):
            # The identifier IS the receiver (`ast` in `ast.parse`) — not a use
            # of the name we were asked about in any interesting sense, and a
            # local of that name (`total.write()`) is not one at all.
            return ("local" if _is_local(node) else "other"), None, None
        for f in _RECEIVER_FIELDS:
            recv = parent.child_by_field_name(f)
            if recv is not None:
                receiver, recv_node = _text(recv)[:40], recv
                break
        else:
            # Grammars like Kotlin's navigation_expression name no fields at
            # all; there the receiver is simply what comes first.
            if first is not None and first.id != node.id:
                receiver, recv_node = _text(first)[:40], first
        gp = parent.parent
        if gp is not None and gp.type in _CALL_NODES and _is_callee(parent):
            return "call", receiver, recv_node
        if node.type == "type_identifier" or parent.type == "scoped_type_identifier":
            return "type", receiver, None
        return "other", receiver, None

    if parent.type in _CALL_NODES and _is_callee(node):
        # Java `cart.add(x)` is one method_invocation node: its receiver is the
        # `object` field, not a separate member-access node.
        obj = parent.child_by_field_name("object") if field == "name" else None
        return "call", (_text(obj)[:40] if obj is not None else None), obj
    # Calls the grammar does not model as call nodes:
    if parent.type == "infix_expression" and lang == "kotlin":
        kids = parent.named_children
        if len(kids) == 3 and kids[1].id == node.id:
            return "call", None, None                 # a percentOf b
    if parent.type == "token_tree":                   # Rust: println!("{}", f(x))
        nxt = node.next_sibling
        if nxt is not None and nxt.type == "token_tree" and nxt.child_count \
                and nxt.children[0].type == "(":
            prev = node.prev_sibling
            if prev is not None and prev.type == "::" and prev.prev_sibling is not None:
                return "call", _text(prev.prev_sibling)[:40], None
            return "call", None, None
    if parent.type in ("jsx_opening_element", "jsx_self_closing_element") and field == "name":
        return "call", None, None                     # <Component ... />
    if node.type == "type_identifier" or parent.type in _TYPE_PARENTS:
        return "type", None, None
    return "other", None, None


# ── executors ────────────────────────────────────────────────────────────────

_MAX_SYMBOL_RESULTS = 100
_MAX_REF_RESULTS    = 200
_MAX_SYMBOL_LINES   = 400
_MAX_MAP_FILES      = 300
_MAX_DEP_RESULTS    = 200


def code_outline(path: str = ".", depth: int | None = None, *, workdir: Path) -> str:
    p = safe_path(path, workdir)
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
    root = safe_path(directory, workdir)
    if isinstance(root, str):
        return root
    if not root.exists():
        return f"ERROR: not found: {directory}"
    # "which definition is line N in?" — one header line, where read_symbol would
    # return the whole definition.  This is the cheap answer to a stack trace or a
    # grep hit, so it belongs on the *find* tool.
    line = line_query(name)
    if line is not None:
        if root.is_dir():
            return (f"ERROR: looking up line {line} needs a single file, not a directory — "
                    f"pass the file as 'directory', e.g. find_symbol(\"{name}\", \"path/to/file.py\")")
        try:
            idx = index(root)
        except OSError as e:
            return f"ERROR: {e}"
        if idx is None:
            return _unsupported(directory)
        if not 1 <= line <= max(idx.nlines, 1):
            return f"ERROR: line {line} is outside {directory} (1-{idx.nlines})"
        owner = enclosing(idx, line)
        if owner is None:
            return (f"(line {line} of {_rel(root, workdir)} is not inside any definition — "
                    "it is at module level)")
        return (f"{_rel(root, workdir)}:L{owner.start}-{owner.end}  {owner.kind} {owner.qualname}"
                f"  | {owner.signature}")
    stats: dict = {}
    hits = []
    kinds: dict[str, int] = {}
    files = set()
    for f, parsed in _scan(root, stats):
        for s in parsed.symbols:
            if _matches(s, name) and (not kind or kind_matches(s.kind, kind)):
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
    p = safe_path(path, workdir)
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
    line = line_query(name)
    if line is not None:
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
    root = safe_path(directory, workdir)
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
    stats: dict = {}
    hits = []
    by_role: dict[str, int] = {}
    recvs: dict[str, int] = {}
    direct = 0
    for f, parsed in _scan(root, stats, tree=True):
        rows = references_in(parsed, bare)
        rel = _rel(f, workdir)
        for r in sorted(rows):
            kind, recv, _rtype = rows[r]
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
    # A package import names the directory: pkg/__init__.py, a Rust
    # module's mod.rs, a JS/TS folder's index.js.
    if stem in ("__init__", "mod", "index") and len(parts) > 1:
        cands.add(".".join(parts[:-1]))
        cands.add(parts[-2])
    return stem, cands


def file_dependencies(path: str, direction: str = "both", *, workdir: Path) -> str:
    """What `path` imports, and which files import it.  Resolution is textual,
    not a real module resolver — see the note in the returned output."""
    if direction not in ("both", "imports", "importers"):
        return "ERROR: direction must be 'both', 'imports' or 'importers'"
    p = safe_path(path, workdir)
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
