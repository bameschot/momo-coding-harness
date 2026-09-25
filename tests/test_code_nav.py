"""Tests for the tree-sitter code navigation tools.

The per-grammar node-type tables in code_nav are the fragile part: a grammar
wheel upgrade can rename a node and silently empty a tool's output.  Every
supported language therefore gets a tiny fixture and the same round of
assertions.

Run with:  python -m unittest discover tests
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from harness import code_nav

FIXTURES = Path(__file__).parent / "fixtures"

# A deliberately unparseable file, plus a scratch area for tests that write.
# Both live in a temp directory rather than in tests/: a broken file checked into
# the repo made every project-wide code_nav scan report "1 file with syntax
# errors", and scratch files written under tests/ survive a crashed test run.
SCRATCH: Path
BROKEN: Path


def setUpModule():
    global SCRATCH, BROKEN
    SCRATCH = Path(tempfile.mkdtemp(prefix="code_nav_test_"))
    BROKEN = SCRATCH
    (SCRATCH / "broken.py").write_text("def broken(\n    this is not valid python ((\n")


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)

# One fixture per supported grammar.  Each declares a Parser type with a parse
# method, a free `run` function, and at least one import — so the same
# assertions hold everywhere and a grammar that stops producing one of them
# fails loudly.
LANGS = {
    "sample.py":   "python",
    "sample.java": "java",
    "sample.c":    "c",
    "sample.cpp":  "cpp",
    "sample.kt":   "kotlin",
    "sample.rs":   "rust",
    "sample.js":   "javascript",
    "sample.ts":   "typescript",
    "sample.tsx":  "tsx",
}
# sample.c has a struct, not a class, and sample.tsx is a React fixture with no
# Parser — those two are excluded from the Parser-shaped assertions.
HAS_PARSE_METHOD = {"sample.py", "sample.java", "sample.cpp", "sample.kt",
                    "sample.rs", "sample.js", "sample.ts"}
HAS_RUN_FUNCTION = {"sample.py", "sample.c", "sample.cpp", "sample.kt",
                    "sample.rs", "sample.js", "sample.ts"}


# Config / markup / script grammars.  Their "definitions" are keys, ids,
# selectors, tables and functions; each fixture pins the qualified names its
# extractor must produce, so a renamed node type in a wheel upgrade fails here.
DATA_LANGS = {
    "sample.yaml": "yaml",
    "sample.toml": "toml",
    "sample.json": "json",
    "sample.html": "html",
    "sample.css":  "css",
    "sample.sql":  "sql",
    "sample.sh":   "bash",
    "Dockerfile":  "dockerfile",
}
DATA_SYMBOLS = {
    "sample.yaml": [("services.web", "table"), ("services.web.environment.LOG_LEVEL", "key"),
                    ("services.web.ports", "list")],
    "sample.toml": [("title", "key"), ("tool.poetry", "table"),
                    ("tool.poetry.dependencies.requests.optional", "key"),
                    ("servers[]", "table"), ("servers[].host", "key")],
    "sample.json": [("scripts", "table"), ("scripts.build", "key"), ("workspaces[].path", "key")],
    "sample.html": [("app", "id"), ("app.top-nav", "id"), ("app.row-template", "id"),
                    ("app.js", "script")],
    "sample.css":  [(":root", "rule"), (".btn.primary", "rule"), ("#app > nav", "rule"),
                    ("--accent", "var"), ("spin", "keyframes")],
    "sample.sql":  [("users", "table"), ("users.email", "column"), ("active_users", "view"),
                    ("idx_users_email", "index"), ("add_one", "function")],
    "sample.sh":   [("build", "function"), ("deploy", "function")],
    "Dockerfile":  [("builder", "stage"), ("builder.VERSION", "arg"), ("builder.APP_HOME", "env")],
}


class LanguageCoverage(unittest.TestCase):
    """Every grammar in _EXTENSIONS must have a fixture, or it is untested."""

    def test_every_grammar_has_a_fixture(self):
        grammars = set(code_nav._EXTENSIONS.values()) | {"dockerfile"}
        self.assertEqual(grammars, set(LANGS.values()) | set(DATA_LANGS.values()))

    def test_fixtures_parse_without_errors(self):
        for name in LANGS:
            with self.subTest(name):
                parsed = code_nav.parse(FIXTURES / name)
                self.assertFalse(parsed.tree.root_node.has_error,
                                 f"{name} does not parse cleanly")

    def test_language_detected_from_extension(self):
        for name, lang in LANGS.items():
            with self.subTest(name):
                self.assertEqual(code_nav.language_for(FIXTURES / name), lang)


class DataGrammars(unittest.TestCase):

    def test_fixtures_parse_without_errors(self):
        for name, lang in DATA_LANGS.items():
            with self.subTest(name):
                self.assertEqual(code_nav.language_for(FIXTURES / name), lang)
                parsed = code_nav.parse(FIXTURES / name)
                self.assertFalse(parsed.tree.root_node.has_error, f"{name} does not parse cleanly")

    def test_expected_symbols(self):
        for name, want in DATA_SYMBOLS.items():
            got = {(s.qualname, s.kind) for s in code_nav.parse(FIXTURES / name).symbols}
            for q, kind in want:
                with self.subTest(name=name, symbol=q):
                    self.assertIn((q, kind), got)

    def test_block_ends_before_the_next_key(self):
        # A TOML table / YAML mapping node ends at column 0 of the next line;
        # its span must not claim that line.
        syms = {s.qualname: s for s in code_nav.parse(FIXTURES / "sample.toml").symbols}
        self.assertEqual((syms["tool.poetry"].start, syms["tool.poetry"].end), (3, 5))
        syms = {s.qualname: s for s in code_nav.parse(FIXTURES / "sample.yaml").symbols}
        self.assertEqual(syms["services"].end, 11)

    def test_read_symbol_on_a_key_path(self):
        out = code_nav.read_symbol("sample.yaml", "services.web", workdir=FIXTURES)
        self.assertIn("image: nginx", out)
        self.assertNotIn("postgres", out)

    def test_dockerfile_detected_by_basename(self):
        self.assertEqual(code_nav.language_for(Path("sub/Dockerfile.prod")), "dockerfile")
        self.assertEqual(code_nav.language_for(Path("Makefile")), None)


class ModuleConstants(unittest.TestCase):
    """Module-level constants and variables are definitions too; locals are not."""

    CASES = {
        "c.py":   ("MAX_SIZE = 10\n_UNITS: dict = {\n  'k': 1,\n}\nx, y = 1, 2\n"
                   "if True:\n    FLAG = 1\nclass A:\n    ATTR = 1\n    def f(self):\n        local = 1\n",
                   {("MAX_SIZE", "constant", 1, 1), ("_UNITS", "constant", 2, 4),
                    ("FLAG", "constant", 7, 7), ("A", "class", 8, 11),
                    ("ATTR", "constant", 9, 9)}),          # a class attribute
        "c.js":   ("const MAX = 1;\nlet counter = 0;\nexport const API_URL = 'x';\nconst f = () => 1;\n",
                   {("MAX", "constant", 1, 1), ("counter", "variable", 2, 2),
                    ("API_URL", "constant", 3, 3), ("f", "function", 4, 4)}),
        "c.c":    ('#define MAX_LEN 64\n#define SQ(x) ((x)*(x))\nstatic int counter = 0;\n'
                   'int f(void);\nextern int g;\n',
                   {("MAX_LEN", "constant", 1, 1), ("SQ", "macro", 2, 2),
                    ("counter", "variable", 3, 3)}),
        "c.rs":   ("const MAX: usize = 3;\nstatic mut COUNT: u32 = 0;\n",
                   {("MAX", "constant", 1, 1), ("COUNT", "constant", 2, 2)}),
        "C.java": ("class C {\n  public static final int MAX = 3;\n  private int count = 0;\n}\n",
                   {("C", "class", 1, 4), ("MAX", "constant", 2, 2)}),
    }

    def test_constants_per_language(self):
        for name, (src, want) in self.CASES.items():
            with self.subTest(name):
                f = SCRATCH / name
                f.write_text(src)
                got = {(s.name, s.kind, s.start, s.end) for s in code_nav.parse(f).symbols
                       if s.kind != "method"}
                self.assertEqual(got, want)

    def test_exported_definitions_are_found(self):
        f = SCRATCH / "exp.ts"
        f.write_text("export function exported() { return helper(); }\nexport class Ex {}\n"
                     "function helper() {}\nexport { a } from './m';\n")
        names = {s.name for s in code_nav.parse(f).symbols}
        self.assertEqual(names, {"exported", "Ex", "helper"})
        out = code_nav.find_references("helper", "exp.ts", workdir=SCRATCH)
        self.assertIn("(call)", out)           # was tagged (import) inside an export
        self.assertIn("./m", code_nav.file_dependencies("exp.ts", direction="imports",
                                                         workdir=SCRATCH))


class ScopeRoles(unittest.TestCase):
    """A parameter or local that shares a name is not a use; a C prototype is a
    declaration, not a use."""

    def roles(self, name, src, fname):
        f = SCRATCH / fname
        f.write_text(src)
        parsed = code_nav.parse(f)
        return {r + 1: role for r, (role, *_rest) in code_nav.references_in(parsed, name).items()}

    def test_parameter_and_local_shadowing(self):
        src = ("def total():\n    return 1\n\n\ndef f(total):\n    return total + 1\n\n\n"
               "def g():\n    total = 2\n    return total\n\n\ndef h():\n    return total()\n")
        self.assertEqual(self.roles("total", src, "shadow.py"),
                         {1: "def", 5: "local", 6: "local", 10: "local", 11: "local", 15: "call"})

    def test_kotlin_parameter(self):
        src = "fun total() = 1\nfun pct(total: Int) = total * 2\nfun g() = total()\n"
        self.assertEqual(self.roles("total", src, "shadow.kt"), {1: "def", 2: "local", 3: "call"})

    def test_c_prototype_is_a_declaration(self):
        src = "int clamp(int v);\nint *mk(void);\nint clamp(int v) { return v; }\nint g(void) { return clamp(1); }\n"
        self.assertEqual(self.roles("clamp", src, "proto.c"), {1: "decl", 3: "def", 4: "call"})
        self.assertEqual(self.roles("mk", src, "proto.c"), {2: "decl"})

    def test_receiver_declared_type(self):
        cases = {
            "t.java": ("class A { void f(Cart cart) { var c = new Cart(); Money m = x;\n"
                       "cart.add(1);\nc.add(2);\nm.add(3); } }", "add", {"Cart", "Money"}),
            "t.kt":   ("fun f(cart: Cart) {\n val c = Cart()\n cart.add(1)\n c.add(2)\n}\n", "add", {"Cart"}),
            "t.py":   ("def f(cart: Cart):\n    c = Cart()\n    cart.add(1)\n    c.add(2)\n", "add", {"Cart"}),
            "t.ts":   ("function f(cart: Cart) { const c = new Cart();\ncart.add(1);\nc.add(2); }", "add", {"Cart"}),
            "t.rs":   ("fn f(cart: &Cart) { let c = Cart::new();\ncart.add(1);\nc.add(2); }", "add", {"Cart"}),
            "t.cpp":  ("void f(const Shape& s) { Circle c(2.0);\ns.area();\nc.area(); }", "area", {"Shape", "Circle"}),
        }
        for fname, (src, name, want) in cases.items():
            with self.subTest(fname):
                f = SCRATCH / fname
                f.write_text(src)
                rows = code_nav.references_in(code_nav.parse(f), name)
                self.assertEqual({t for role, _r, t in rows.values() if role == "call"} - {None}, want)

    def test_python_scoping_matches_symtable(self):
        """Cases the ast/symtable oracle (evals/oracle_bench.py) found on real code."""
        src = ("def total(): pass\n"
               "def a(xs):\n"
               "    if (total := len(xs)):\n"              # 3 walrus binds
               "        return total\n"                    # 4
               "def b(*total): return total\n"             # 5 *args
               "def c():\n"
               "    global total\n"
               "    total = 1\n"                           # 8 global: not local
               "def d():\n"
               "    import total\n"                        # 10 function-level import binds
               "    return total\n"                        # 11
               "def e(ys):\n"
               "    f = lambda total: total\n"             # 13 the lambda's own parameter
               "    return total()\n"                      # 14 not hidden by the lambda
               "def g():\n"
               "    total = 1\n"
               "    def inner():\n"
               "        return total\n"                    # 18 closure over g's local
               "    return inner\n"
               "def h():\n"
               "    with open(p) as (total, x):\n"         # 21 nested destructuring
               "        pass\n")
        r = self.roles("total", src, "scoping.py")
        self.assertEqual(r[1], "def")
        for line in (3, 4, 5, 10, 11, 13, 16, 18, 21):
            with self.subTest(line=line):
                self.assertIn(r[line], ("local", "import"), r)
        self.assertEqual(r[14], "call")
        self.assertNotEqual(r[8], "local")

    def test_js_arrow_parameter_does_not_hide_outer_calls(self):
        src = "function total() {}\nfunction f(xs) {\n  xs.map((total) => total);\n  return total();\n}\n"
        r = self.roles("total", src, "arrow.js")
        self.assertEqual(r[4], "call")

    def test_imported_destructure_is_not_local(self):
        src = 'async function f() {\n  const { g } = await import("./m.js");\n  return g();\n}\n'
        self.assertEqual(self.roles("g", src, "dyn.js"), {2: "import", 3: "call"})


class OracleFindings(unittest.TestCase):
    """Extraction cases found by comparing with ast / tags.scm on real code."""

    def parse(self, fname, src):
        f = SCRATCH / fname
        f.write_text(src)
        return code_nav.parse(f)

    def test_python_chained_assignment_and_future_import(self):
        p = self.parse("chain.py", "from __future__ import annotations\na = b = 2\n")
        self.assertEqual({s.name for s in p.symbols}, {"a", "b"})
        self.assertEqual([(i.module, i.names) for i in p.imports], [("__future__", ["annotations"])])

    def test_cpp_reference_returning_functions(self):
        p = self.parse("ref.hpp", "struct M {\n  M& rotate(float a) { return *this; }\n"
                                  "  M& operator+=(const M& o) { return *this; }\n};\n")
        self.assertEqual({s.qualname for s in p.symbols}, {"M", "M.rotate", "M.operator+="})

    def test_minified_definition_and_call_on_one_line(self):
        # A minified library: the method is defined and called on the same line.
        f = SCRATCH / "min.js"
        f.write_text("class A{go(){return 1}run(){return this.go()}}\n")
        rows = code_nav.references_in(code_nav.parse(f), "go")
        self.assertEqual(rows[0][0], "call")          # the call is not lost to the def

    def test_js_comma_chain_and_this_assignments(self):
        p = self.parse("chain.js", "o.cancel = function(){}, Ht.get = function(t){ return t };\n"
                                   "class C { constructor(){ this.hover = (t) => t; } }\n")
        quals = {s.qualname for s in p.symbols}
        self.assertTrue({"o.cancel", "Ht.get", "C.hover"} <= quals, quals)

    def test_inline_script_javascript_is_indexed(self):
        f = SCRATCH / "app.html"
        f.write_text("<html><body>\n<div id=\"out\"></div>\n<script>\n"
                     "function render(items) {\n  return items.map(fmt);\n}\n"
                     "const fmt = (x) => x.toFixed(2);\n"
                     "render([1]);\n</script>\n"
                     "<script src=\"lib.js\"></script>\n</body></html>\n")
        p = code_nav.parse(f)
        kinds = {(s.qualname, s.kind, s.start) for s in p.symbols}
        self.assertIn(("render", "function", 4), kinds)      # real line in the .html
        self.assertIn(("fmt", "function", 7), kinds)
        self.assertIn(("out", "id", 2), kinds)               # the HTML symbols stay
        rows = {r + 1: v[0] for r, v in code_nav.references_in(p, "render").items()}
        self.assertEqual(rows, {4: "def", 8: "call"})
        self.assertIn(("fmt", 5), code_nav.identifier_rows(p))

    def test_js_function_assigned_to_a_property(self):
        p = self.parse("prop.js", "Module.locateFile = (p) => p;\nglobalThis.f0 = function () {};\n"
                                  "$('#x').onclick = () => 1;\n")
        self.assertEqual({s.qualname for s in p.symbols}, {"Module.locateFile", "globalThis.f0"})


class KotlinGrammarQuirks(unittest.TestCase):
    """tree-sitter-kotlin misparses these; found against the Kotlin compiler's
    own parser on the Maestro repo (evals/oracle_bench.py --langs kotlin)."""

    def roles(self, src, name):
        f = SCRATCH / "quirk.kt"
        f.write_text(src)
        return {r + 1: v[:2] for r, v in code_nav.references_in(code_nav.parse(f), name).items()}

    def test_generic_calls_parsed_as_comparisons(self):
        src = ("fun f(x: X) {\n    assertThrows<E> { g() }\n    x.setAll<T>(m)\n"
               "    val y = emptyList<String>()\n}\n")
        self.assertEqual(self.roles(src, "assertThrows"), {2: ("call", None)})
        self.assertEqual(self.roles(src, "setAll"), {3: ("call", "x")})
        self.assertEqual(self.roles(src, "emptyList"), {4: ("call", None)})

    def test_negated_call(self):
        self.assertEqual(self.roles("fun f() = !isReady(p)\n", "isReady"), {1: ("call", None)})

    def test_call_after_an_operator(self):
        src = "fun f() {\n    val a = x ?: emptyList<String>()\n    val m = mapOf(\"k\" to emptyMap<A, B>())\n}\n"
        self.assertEqual(self.roles(src, "emptyList"), {2: ("call", None)})
        self.assertEqual(self.roles(src, "emptyMap"), {3: ("call", None)})

    def test_named_companion_object(self):
        f = SCRATCH / "comp.kt"
        f.write_text("class A {\n    companion object Factory {\n        fun make() = A()\n    }\n}\n")
        quals = {s.qualname for s in code_nav.parse(f).symbols}
        self.assertTrue({"A.Factory", "A.Factory.make"} <= quals, quals)


class CGrammarQuirks(unittest.TestCase):
    """tree-sitter-c misreads macro-heavy C; found against clang's AST on the
    mgba repo (evals/oracle_bench.py --langs c-clang)."""

    def parse(self, src):
        f = SCRATCH / "quirk.c"
        f.write_text(src)
        return code_nav.parse(f)

    def roles(self, src, name):
        return {r + 1: v[0] for r, v in code_nav.references_in(self.parse(src), name).items()}

    def test_call_statement_misparsed_as_a_declaration_is_a_call(self):
        # In macro-heavy files (`DEFINE_OP(B, ...; cycles += WritePC(cpu);)`)
        # a call statement comes out as this shape: a declaration of a
        # function inside a function body — which real C practically never has.
        src = "void f(void) {\n\tcycles WritePC(cpu);\n}\n"
        self.assertEqual(self.roles(src, "WritePC"), {2: "call"})

    def test_function_pointer_variable_is_not_a_call(self):
        src = "void f(void) {\n\tuint32_t (*lookup)(void*, uint32_t);\n\tlookup = g;\n}\n"
        self.assertNotIn("call", self.roles(src, "lookup").values())

    def test_prototypes_at_file_scope_stay_declarations(self):
        self.assertEqual(self.roles("int clamp(int v);\n", "clamp"), {1: "decl"})

    def test_keywords_are_never_names(self):
        src = ("int f(int a) {\n#ifdef X\n\tif (a) { return 1; }\n#endif\n"
               "\telse if (a > 2) {\n\t\treturn 2;\n\t}\n\treturn 0;\n}\n")
        parsed = self.parse(src)
        self.assertNotIn("if", {s.name for s in parsed.symbols})
        self.assertEqual(code_nav.references_in(parsed, "if"), {})
        self.assertNotIn("if", {n for n, _ in code_nav.identifier_rows(parsed)})


class ConstantDocs(unittest.TestCase):

    def test_trailing_and_leading_comments(self):
        f = SCRATCH / "cd.py"
        f.write_text("A = 1  # the a value\nB = 2\n# about c\nC = 3\nD = 4  # d here\nE = 5\n")
        docs = {s.name: s.doc for s in code_nav.parse(f).symbols}
        self.assertEqual(docs, {"A": "the a value", "B": "", "C": "about c", "D": "d here", "E": ""})


class AliasedImports(unittest.TestCase):

    def test_from_import_alias_records_the_real_name(self):
        f = SCRATCH / "al.py"
        f.write_text("from . import net as net_mod\nfrom os import path as p, sep\n")
        imps = code_nav.parse(f).imports
        self.assertEqual([i.names for i in imps], [["net"], ["path", "sep"]])


class DocLines(unittest.TestCase):

    def test_python_docstring_and_js_comment(self):
        src = SCRATCH / "doc.py"
        src.write_text('def f():\n    """First line.\n\n    More."""\n\n\ndef g():\n    pass\n')
        syms = {s.name: s.doc for s in code_nav.parse(src).symbols}
        self.assertEqual(syms, {"f": "First line.", "g": ""})
        js = SCRATCH / "doc.js"
        js.write_text("/**\n * Adds two numbers.\n * @param a\n */\nfunction add(a, b) { return a + b; }\n")
        self.assertEqual(code_nav.parse(js).symbols[0].doc, "Adds two numbers.")


class Outline(unittest.TestCase):

    def test_file_outline_lists_definitions(self):
        for name in HAS_PARSE_METHOD:
            with self.subTest(name):
                out = code_nav.code_outline(name, workdir=FIXTURES)
                self.assertIn(LANGS[name], out.splitlines()[0])
                self.assertIn("Parser", out)
                self.assertIn("parse", out)

    def test_file_outline_depth_limits_nesting(self):
        full = code_nav.code_outline("sample.py", workdir=FIXTURES)
        top = code_nav.code_outline("sample.py", depth=1, workdir=FIXTURES)
        self.assertIn("def parse", full)
        self.assertNotIn("def parse", top)
        self.assertIn("class Parser", top)

    def test_directory_outline_maps_every_file(self):
        out = code_nav.code_outline(".", workdir=FIXTURES)
        for name in LANGS:
            self.assertIn(name, out)
        # One line per file, not a full outline: the nested method is omitted.
        self.assertIn("class Parser", out)
        self.assertRegex(out, r"sample\.py \(\d+ lines\):")

    def test_directory_outline_is_much_smaller_than_per_file_outlines(self):
        combined = sum(len(code_nav.code_outline(n, workdir=FIXTURES)) for n in LANGS)
        mapped = len(code_nav.code_outline(".", workdir=FIXTURES))
        self.assertLess(mapped, combined,
                        "the point of the directory map is to cost less than outlining each file")

    def test_directory_outline_depth_2_includes_methods(self):
        out = code_nav.code_outline(".", depth=2, workdir=FIXTURES)
        self.assertIn("method parse", out)

    def test_unsupported_file_redirects_to_grep(self):
        out = code_nav.code_outline("../test_code_nav.py", workdir=FIXTURES / "x")
        self.assertTrue(out.startswith("ERROR: path outside working directory")
                        or "not a supported source file" in out, out)

    def test_outline_of_missing_path(self):
        self.assertIn("not found", code_nav.code_outline("nope.py", workdir=FIXTURES))


class FindSymbol(unittest.TestCase):

    def test_bare_name_found_in_every_language(self):
        for name in HAS_PARSE_METHOD:
            with self.subTest(name):
                out = code_nav.find_symbol("parse", name, workdir=FIXTURES)
                self.assertIn(name, out)
                self.assertIn("parse", out)

    def test_qualified_name(self):
        out = code_nav.find_symbol("Parser.parse", "sample.py", workdir=FIXTURES)
        self.assertIn("method Parser.parse", out)

    def test_kind_filter(self):
        out = code_nav.find_symbol("Parser", ".", kind="class", workdir=FIXTURES)
        self.assertIn("sample.py", out)
        # Rust's Parser is a struct and C's Config is a struct, so neither shows.
        self.assertNotIn("struct", out)

    def test_wildcard_enumerates(self):
        out = code_nav.find_symbol("*", ".", kind="class", workdir=FIXTURES)
        self.assertIn("class Parser", out)
        self.assertNotIn("method", out)

    def test_wildcard_pattern_matches_a_suffix(self):
        out = code_nav.find_symbol("Pars*", "sample.py", workdir=FIXTURES)
        self.assertIn("Parser", out)
        self.assertNotIn("function run", out)

    def test_wildcard_is_case_sensitive(self):
        self.assertIn("parse", code_nav.find_symbol("pars*", "sample.py", workdir=FIXTURES))
        self.assertIn("no definition",
                      code_nav.find_symbol("PARS*", "sample.py", workdir=FIXTURES))

    def test_exact_match_unaffected_by_pattern_support(self):
        out = code_nav.find_symbol("run", "sample.py", workdir=FIXTURES)
        self.assertEqual(len(out.splitlines()), 1, out)
        self.assertIn("function run", out)

    def test_missing_name_suggests_grep(self):
        out = code_nav.find_symbol("nosuchthing", ".", workdir=FIXTURES)
        self.assertIn("grep_files", out)


class ReadSymbol(unittest.TestCase):

    def test_returns_the_definition_with_read_file_line_format(self):
        out = code_nav.read_symbol("sample.py", "Parser.parse", workdir=FIXTURES)
        self.assertIn("method Parser.parse", out.splitlines()[0])
        # Body lines must match read_file's "%4d: " prefix so the model can
        # paste them straight into edit_file.
        self.assertIn("\n   8:     def parse", out)

    def test_ambiguous_name_lists_candidates(self):
        out = code_nav.read_symbol("sample.ts", "run", workdir=FIXTURES)
        self.assertIn("matches 3 definitions", out)
        self.assertIn("Parser.run", out)

    def test_unknown_name_lists_what_is_there(self):
        out = code_nav.read_symbol("sample.py", "nope", workdir=FIXTURES)
        self.assertIn("Definitions in this file", out)
        self.assertIn("Parser.parse", out)

    def test_line_number_reads_the_enclosing_definition(self):
        out = code_nav.read_symbol("sample.py", "9", workdir=FIXTURES)
        self.assertIn("method Parser.parse", out.splitlines()[0])

    def test_line_number_accepts_an_l_prefix(self):
        a = code_nav.read_symbol("sample.py", "L9", workdir=FIXTURES)
        b = code_nav.read_symbol("sample.py", "9", workdir=FIXTURES)
        self.assertEqual(a, b)

    def test_line_outside_any_definition(self):
        out = code_nav.read_symbol("sample.py", "1", workdir=FIXTURES)
        self.assertIn("not inside any definition", out)

    def test_line_out_of_range(self):
        out = code_nav.read_symbol("sample.py", "9999", workdir=FIXTURES)
        self.assertIn("outside", out)


class FindReferences(unittest.TestCase):

    def test_definition_is_tagged(self):
        for name in HAS_PARSE_METHOD:
            with self.subTest(name):
                out = code_nav.find_references("parse", name, workdir=FIXTURES)
                self.assertIn("(def)", out)

    def test_call_sites_are_tagged_call(self):
        for name in HAS_RUN_FUNCTION & HAS_PARSE_METHOD:
            with self.subTest(name):
                out = code_nav.find_references("parse", name, role="call",
                                               workdir=FIXTURES)
                self.assertIn("(call", out)
                self.assertNotIn("(def)", out)

    def test_imports_are_tagged_import(self):
        cases = {"sample.py": "OrderedDict", "sample.java": "List",
                 "sample.rs": "HashMap", "sample.kt": "max",
                 "sample.ts": "helper", "sample.js": "helper"}
        for name, symbol in cases.items():
            with self.subTest(name):
                out = code_nav.find_references(symbol, name, workdir=FIXTURES)
                self.assertIn("(import)", out)

    def test_type_references_are_tagged_type(self):
        out = code_nav.find_references("Parser", "sample.ts", workdir=FIXTURES)
        self.assertIn("(type)", out)

    def test_receiver_is_reported(self):
        out = code_nav.find_references("parse", "sample.py", role="call",
                                       workdir=FIXTURES)
        self.assertIn("recv p", out)

    def test_receiver_side_identifier_is_not_a_call(self):
        # `p` in `p.parse("hi")` is the receiver, not a call of `p` — and since
        # it is run()'s parameter, it is tagged as that local.
        out = code_nav.find_references("p", "sample.kt", workdir=FIXTURES)
        self.assertIn("(local)", out)
        self.assertNotIn("(call", out)

    def test_qualified_name_filters_by_receiver(self):
        out = code_nav.find_references("p.parse", "sample.py", role="call",
                                       workdir=FIXTURES)
        self.assertIn("recv p", out)
        out = code_nav.find_references("Other.parse", "sample.py", role="call",
                                       workdir=FIXTURES)
        self.assertIn("no 'call' references", out)

    def test_enclosing_definition_is_named(self):
        out = code_nav.find_references("parse", "sample.py", workdir=FIXTURES)
        self.assertIn("[in run]", out)

    def test_unknown_role_is_rejected(self):
        out = code_nav.find_references("parse", ".", role="calls", workdir=FIXTURES)
        self.assertTrue(out.startswith("ERROR: unknown role"), out)

    def test_role_with_no_matches_reports_what_exists(self):
        out = code_nav.find_references("Id", "sample.ts", role="call", workdir=FIXTURES)
        self.assertIn("references found are", out)

    def test_call_hint_is_absent_when_there_are_no_calls(self):
        out = code_nav.find_references("Props", "sample.tsx", workdir=FIXTURES)
        self.assertIn("(type)", out)
        self.assertNotIn("role=call", out)


class FileDependencies(unittest.TestCase):

    def test_imports_extracted_for_every_language(self):
        expected = {
            "sample.py":   "collections",
            "sample.java": "java.util.List",
            "sample.c":    "stdio.h",
            "sample.cpp":  "string",
            "sample.kt":   "kotlin.math.max",
            "sample.rs":   "std::collections::HashMap",
            "sample.js":   "./helper.js",
            "sample.ts":   "./helper",
            "sample.tsx":  "react",
        }
        self.assertEqual(set(expected), set(LANGS), "every fixture needs an import")
        for name, module in expected.items():
            with self.subTest(name):
                out = code_nav.file_dependencies(name, direction="imports",
                                                 workdir=FIXTURES)
                self.assertIn(module, out)

    def test_imported_names_are_listed(self):
        out = code_nav.file_dependencies("sample.py", direction="imports",
                                         workdir=FIXTURES)
        self.assertIn("OrderedDict", out)

    def test_c_include_forms(self):
        out = code_nav.file_dependencies("sample.c", direction="imports",
                                         workdir=FIXTURES)
        self.assertIn("stdio.h", out)   # <system>
        self.assertIn("local.h", out)   # "local"

    def test_nested_imports_are_found(self):
        """An import deferred inside a function is still a dependency."""
        src = SCRATCH
        f = src / "lazy.py"
        f.write_text("def go():\n    from collections import deque\n    return deque()\n")
        try:
            out = code_nav.file_dependencies("lazy.py", direction="imports", workdir=src)
            self.assertIn("collections", out)
            self.assertIn("deque", out)
        finally:
            f.unlink()

    def test_importers_are_found(self):
        out = code_nav.file_dependencies("sample.ts", direction="importers",
                                         workdir=FIXTURES)
        # Nothing in the fixtures imports sample.ts.
        self.assertIn("IMPORTED BY (0)", out)

    def test_importers_of_a_real_module(self):
        repo = Path(__file__).resolve().parent.parent
        out = code_nav.file_dependencies("harness/paths.py", direction="importers",
                                         workdir=repo)
        self.assertIn("harness/tools.py", out)
        self.assertIn("harness/code_nav.py", out)

    def test_direction_both_has_two_sections(self):
        out = code_nav.file_dependencies("sample.py", workdir=FIXTURES)
        self.assertIn("IMPORTS", out)
        self.assertIn("IMPORTED BY", out)

    def test_bad_direction_is_rejected(self):
        out = code_nav.file_dependencies("sample.py", direction="sideways",
                                         workdir=FIXTURES)
        self.assertTrue(out.startswith("ERROR"), out)


class Incompleteness(unittest.TestCase):
    """A file that cannot be parsed contributes nothing, so the tools must say
    so — otherwise an empty result reads as proof of absence."""

    def test_outline_flags_a_broken_file(self):
        out = code_nav.code_outline("broken.py", workdir=BROKEN)
        self.assertIn("syntax errors", out)

    def test_find_symbol_admits_the_gap(self):
        out = code_nav.find_symbol("broken", ".", workdir=BROKEN)
        self.assertIn("syntax errors", out)

    def test_find_references_admits_the_gap(self):
        out = code_nav.find_references("broken", ".", workdir=BROKEN)
        self.assertIn("syntax errors", out)

    def test_directory_outline_admits_the_gap(self):
        out = code_nav.code_outline(".", workdir=BROKEN)
        self.assertIn("syntax errors", out)


class Caching(unittest.TestCase):

    def test_index_does_not_retain_trees(self):
        code_nav._cache.clear()
        code_nav._index_cache.clear()
        for name in LANGS:
            code_nav.index(FIXTURES / name)
        self.assertEqual(len(code_nav._cache), 0,
                         "index() must not populate the heavy tree cache")
        self.assertEqual(len(code_nav._index_cache), len(LANGS))

    def test_index_cache_is_lru_not_cleared(self):
        code_nav._index_cache.clear()
        original = code_nav._MAX_INDEX_CACHE
        code_nav._MAX_INDEX_CACHE = 3
        try:
            for name in LANGS:
                code_nav.index(FIXTURES / name)
            # Over the cap it evicts one at a time instead of dropping everything.
            self.assertEqual(len(code_nav._index_cache), 3)
        finally:
            code_nav._MAX_INDEX_CACHE = original
            code_nav._index_cache.clear()

    def test_reparse_after_mtime_change(self):
        f = SCRATCH / "churn.py"
        f.write_text("def one():\n    pass\n")
        try:
            self.assertIn("one", code_nav.find_symbol("one", "churn.py", workdir=BROKEN))
            f.write_text("def two():\n    pass\n")
            self.assertIn("two", code_nav.find_symbol("two", "churn.py", workdir=BROKEN))
        finally:
            f.unlink()

    def test_parse_populates_both_tiers(self):
        code_nav._cache.clear()
        code_nav._index_cache.clear()
        code_nav.parse(FIXTURES / "sample.py")
        self.assertEqual(len(code_nav._cache), 1)
        self.assertEqual(len(code_nav._index_cache), 1)


class PathSafety(unittest.TestCase):

    def test_every_tool_refuses_to_escape_the_workdir(self):
        calls = [
            lambda: code_nav.code_outline("../../etc/passwd", workdir=FIXTURES),
            lambda: code_nav.find_symbol("x", "../..", workdir=FIXTURES),
            lambda: code_nav.read_symbol("../../etc/passwd", "x", workdir=FIXTURES),
            lambda: code_nav.find_references("x", "../..", workdir=FIXTURES),
            lambda: code_nav.file_dependencies("../../etc/passwd", workdir=FIXTURES),
        ]
        for i, call in enumerate(calls):
            with self.subTest(i):
                self.assertIn("outside working directory", call())


if __name__ == "__main__":
    unittest.main()
