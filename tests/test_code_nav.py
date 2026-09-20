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


class LanguageCoverage(unittest.TestCase):
    """Every grammar in _EXTENSIONS must have a fixture, or it is untested."""

    def test_every_grammar_has_a_fixture(self):
        grammars = set(code_nav._EXTENSIONS.values())
        self.assertEqual(grammars, set(LANGS.values()))

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
        # `p` in `p.parse("hi")` is the receiver, not a call of `p`.
        out = code_nav.find_references("p", "sample.kt", workdir=FIXTURES)
        self.assertIn("(other)", out)
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
        out = code_nav.file_dependencies("harness/tools.py", direction="importers",
                                         workdir=repo)
        self.assertIn("harness/harness.py", out)
        # The lazy `from .tools import _safe_path` inside code_nav's functions.
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
