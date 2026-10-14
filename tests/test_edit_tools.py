"""The edit tools' extras (harness/editing.py): what a successful edit reports,
how old_string is found, replace_symbol, and the checks against the rest of
the project — with the code index on and off, and all of it switched off.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import code_index as ci
from harness import code_nav, editing, prompts, tools
from harness.tools import dispatch

INV = '''class Inventory:
    def find(self, sku):
        for it in self.items:
            if it.sku == sku:
                return it
        return None

    def find_all(self, tag):
        out = [it for it in self.items if tag in it.tags]
        if not out:
            return None
        return out

    @property
    def size(self):
        return len(self.items)


def calc_tax(amount):
    return amount * 0.2
'''

NAV = unittest.skipUnless(code_nav.AVAILABLE and code_nav.grammar_available("python"),
                          "needs tree-sitter with the Python grammar")


class Base(unittest.TestCase):
    features = editing.EDIT_FEATURES

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.write("inv.py", INV)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text, newline=None):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8", newline=newline) as f:
            f.write(text)

    def read(self, rel):
        return (self.root / rel).read_bytes().decode("utf-8")

    def run_tool(self, tool, index=None, **args):
        return dispatch(tool, args, self.root, index=index, edit_features=self.features)


@NAV
class Feedback(Base):
    def test_ok_names_the_new_lines_and_their_definition(self):
        out = self.run_tool("edit_file", path="inv.py", old_string="        if not out:\n            return None",
                            new_string="        if not out:\n            return []")
        self.assertTrue(out.startswith("OK — 1 change applied — line 11 now reads "
                                       "(inside method Inventory.find_all, L8-12):"), out)
        self.assertIn("  11:             return []", out)
        self.assertIn("   8:     def find_all(self, tag):", out)   # 3 lines of context

    def test_snippet_of_a_long_change_shows_both_ends(self):
        body = "".join(f"    x{i} = {i}\n" for i in range(60))
        out = self.run_tool("edit_file", path="inv.py", old_string="    return amount * 0.2\n",
                            new_string=body)
        self.assertIn("lines not shown", out)
        self.assertIn("x0 = 0", out)
        self.assertIn("x59 = 59", out)
        self.assertLess(out.count("\n"), 50)

    def test_replace_all_lists_the_changed_lines(self):
        out = self.run_tool("edit_file", path="inv.py", old_string="self.items",
                            new_string="self._items", replace_all=True)
        self.assertEqual(out, "Replaced 3 occurrence(s) — at lines 3, 9, 16 (as the file reads now)")

    def test_whole_definition_rewrite_points_at_replace_symbol(self):
        src = "def f(a):\n" + "".join(f"    a += {i}\n" for i in range(10)) + "    return a\n"
        self.write("r.py", src + "\n\ndef g():\n    return 1\n")
        new = "def f(a):\n    return a + 45\n"
        out = self.run_tool("edit_file", path="r.py", old_string=src, new_string=new)
        self.assertIn('(This rewrote most of function f; replace_symbol("r.py", "f", new_source) '
                      'does that without copying the old text.)', out)
        out = self.run_tool("edit_file", path="r.py", old_string="    return 1", new_string="    return 2")
        self.assertNotIn("replace_symbol", out)

    def test_new_syntax_error_warns_and_keeps_the_edit(self):
        out = self.run_tool("edit_file", path="inv.py", old_string="        return len(self.items)",
                            new_string="        return len(self.items +")
        self.assertIn("WARNING: inv.py no longer parses after this change — line 16", out)
        self.assertIn("return len(self.items +", self.read("inv.py"))

    def test_identical_old_and_new_is_no_change(self):
        out = self.run_tool("edit_file", path="inv.py", old_string="    return amount * 0.2",
                            new_string="    return amount * 0.2")
        self.assertEqual(out, "No change: new_string is identical to the text it replaces, so "
                              "inv.py is unchanged. Line 20 still reads: `return amount * 0.2`")
        self.assertEqual(self.read("inv.py"), INV)

    def test_a_fix_that_did_not_fix_says_so(self):
        self.write("c.py", "def checkout(t):\n    return {\"text\": f\"Pay {t}\"\n")
        out = self.run_tool("edit_file", path="c.py", old_string='    return {"text": f"Pay {t}"',
                            new_string='    return {"text": f"Pay {t}!"')
        self.assertIn("WARNING: c.py still does not parse — line 2", out)
        out = self.run_tool("edit_file", path="c.py", old_string='    return {"text": f"Pay {t}!"',
                            new_string='    return {"text": f"Pay {t}!"')
        self.assertTrue(out.startswith("No change: new_string is identical"), out)
        self.assertIn("still does not parse — line 2", out)
        out = self.run_tool("edit_file", path="c.py", old_string='    return {"text": f"Pay {t}!"',
                            new_string='    return {"text": f"Pay {t}!"}')
        self.assertNotIn("WARNING", out)

    def test_already_broken_file_is_not_blamed_on_the_edit(self):
        self.write("bad.py", "def f(:\n    pass\n\nx = 1\n")
        out = self.run_tool("edit_file", path="bad.py", old_string="x = 1", new_string="x = 2")
        self.assertTrue(out.startswith("OK"), out)
        self.assertNotIn("WARNING", out)

    @unittest.skipUnless(code_nav.grammar_available("javascript"), "needs the JS grammar")
    def test_javascript_syntax_error_is_found_by_tree_sitter(self):
        self.write("a.js", "function f(a) {\n\treturn a + 1;\n}\n")
        out = self.run_tool("edit_file", path="a.js", old_string="\treturn a + 1;",
                            new_string="\treturn (a + 1;")
        self.assertIn("WARNING: a.js no longer parses", out)

    def test_write_file_warns_about_a_syntax_error(self):
        out = self.run_tool("write_file", path="new.py", content="def f(:\n    pass\n")
        self.assertTrue(out.startswith("Written: new.py"), out)
        self.assertIn("WARNING: new.py no longer parses", out)


@NAV
class Matching(Base):
    def test_failed_deletion_is_not_reported_as_already_applied(self):
        # A deletion's new_string is part of its old_string, so it is still in
        # the file when old_string misses (here: blank lines that are not there).
        # Reporting "applied already" left the block in place unnoticed.
        self.write("m.py", 'if __name__ == "__main__":\n    main()\nclass RemoveTests(unittest.TestCase):\n')
        out = self.run_tool("edit_file", path="m.py",
                            old_string='if __name__ == "__main__":\n    main()\n\n\nclass RemoveTests(unittest.TestCase):',
                            new_string="class RemoveTests(unittest.TestCase):")
        self.assertNotIn("No change needed", out)
        self.assertTrue(out.startswith("ERROR"), out)
        self.assertIn('if __name__ == "__main__"', self.read("m.py"))

    def test_line_count_change_with_wrong_indentation_is_reindented(self):
        out = self.run_tool("edit_file", path="inv.py",
                            old_string="if it.sku == sku:\n    return it",
                            new_string="if it.sku == sku:\n    log(sku)\n    return it")
        self.assertIn("(matched ignoring indentation; new_string re-indented to fit)", out)
        self.assertIn("            if it.sku == sku:\n                log(sku)\n                return it\n",
                      self.read("inv.py"))

    def test_unchanged_lines_keep_the_files_text(self):
        # The model's old_string has a slip in a line it does not change: the
        # file's own line is written back, not the slip.
        out = self.run_tool("edit_file", path="inv.py",
                            old_string="for it in self.items:\n  if it.sku  ==  sku:\n    return it",
                            new_string="for it in self.items:\n  if it.sku  ==  sku:\n    return it.copy()\n")
        self.assertTrue(out.startswith("OK"), out)
        text = self.read("inv.py")
        self.assertIn("            if it.sku == sku:\n                return it.copy()\n", text)

    def test_curly_quotes_and_spacing_still_match(self):
        self.write("q.py", "def greet(name):\n    return 'hi ' + name\n")
        out = self.run_tool("edit_file", path="q.py", old_string="return \u2018hi \u2019+name",
                            new_string="return 'hello ' + name")
        self.assertIn("(matched after normalising quotes and spacing)", out)
        self.assertEqual(self.read("q.py"), "def greet(name):\n    return 'hello ' + name\n")

    def test_tab_indented_file_gets_tabs(self):
        self.write("t.js", "function f(a) {\n\tif (a) {\n\t\treturn 1;\n\t}\n}\n")
        out = self.run_tool("edit_file", path="t.js", old_string="if (a) {\n    return 1;\n}",
                            new_string="if (a) {\n    log(a);\n    return 1;\n}")
        self.assertTrue(out.startswith("OK"), out)
        self.assertIn("\tif (a) {\n\t\tlog(a);\n\t\treturn 1;\n\t}\n", self.read("t.js"))

    def test_top_level_anchor_in_a_tab_file_indents_with_tabs(self):
        self.write("u.js", "function g() {\n\treturn 1;\n}\n\nfunction h() {\n\treturn 2;\n}\n")
        out = self.run_tool("edit_file", path="u.js", old_string="function g() {\n  return 1;\n}",
                            new_string="function g() {\n  log();\n  return 1;\n}")
        self.assertTrue(out.startswith("OK"), out)
        self.assertIn("function g() {\n\tlog();\n\treturn 1;\n}\n", self.read("u.js"))

    def test_occurrences_are_listed_with_their_definitions(self):
        out = self.run_tool("edit_file", path="inv.py", old_string="return None", new_string="return []")
        self.assertTrue(out.startswith("ERROR: old_string found 2 times"), out)
        self.assertIn("  L6 (in method Inventory.find)", out)
        self.assertIn("  L11 (in method Inventory.find_all)", out)
        self.assertIn("Add a neighbouring line to old_string", out)

    def test_not_found_shows_the_closest_block(self):
        out = self.run_tool("edit_file", path="inv.py",
                            old_string="def find(self, sku):\n    for it in self.itemz:\n        if it.sku == sku:",
                            new_string="x")
        self.assertIn("The closest block in the file is lines 2-4", out)
        self.assertIn("   3:         for it in self.items:", out)
        self.assertIn("First difference — your line 2: `for it in self.itemz:`; the file's line 3:", out)
        self.assertEqual(self.read("inv.py"), INV)

    def test_one_line_miss_shows_where_it_differs(self):
        # The 9B sent this miss twice: one word wrong in a long line.
        self.write("t.py", "x = 1\nlink = 'See [[C++]] for a related language and more.'\n")
        out = self.run_tool("edit_file", path="t.py",
                            old_string="link = 'See [[The Sun]] for a related language and more.'",
                            new_string="x")
        self.assertIn("\nlink = 'See [[C++]] for a related language and more.'\n", out)
        self.assertIn("First difference — yours: `link = 'See [[The Sun]] for a related "
                      "language and …`; the file's line 2: `link = 'See [[C++]] for a related "
                      "language and …`", out)

    def test_long_old_string_points_at_replace_symbol(self):
        old = "\n".join(f"line {i}" for i in range(12))
        out = self.run_tool("edit_file", path="inv.py", old_string=old, new_string="x")
        self.assertIn("replace_symbol(path, name, new_source)", out)

    def test_ambiguous_tolerant_match_still_refuses(self):
        self.write("d.py", "def a():\n    x = 1\n\ndef b():\n        x = 1\n")
        out = self.run_tool("edit_file", path="d.py", old_string="x = 1\n", new_string="x = 1\ny = 2\n")
        self.assertTrue(out.startswith("ERROR"), out)


@NAV
class ReplaceSymbol(Base):
    def test_method_gets_its_class_indentation(self):
        out = self.run_tool("replace_symbol", path="inv.py", name="Inventory.size",
                            new_source="@property\ndef size(self):\n    return len(self.items) or 0\n")
        self.assertTrue(out.startswith("OK — replaced method Inventory.size (was L14-16)"), out)
        self.assertIn("    @property\n    def size(self):\n        return len(self.items) or 0\n\n\ndef calc_tax",
                      self.read("inv.py"))

    def test_line_number_name_and_prefixed_source(self):
        out = self.run_tool("replace_symbol", path="inv.py", name="20",
                            new_source="  19: def calc_tax(amount):\n  20:     return amount * 0.21\n")
        self.assertTrue(out.startswith("OK — replaced function calc_tax"), out)
        self.assertTrue(self.read("inv.py").endswith("def calc_tax(amount):\n    return amount * 0.21\n"))

    def test_decorator_is_part_of_the_definition(self):
        self.run_tool("replace_symbol", path="inv.py", name="size",
                      new_source="    def size(self):\n        return 0\n")
        text = self.read("inv.py")
        self.assertNotIn("@property", text)
        self.assertIn("    def size(self):\n        return 0\n", text)

    @unittest.skipUnless(code_nav.grammar_available("javascript"), "needs the JS grammar")
    def test_spaces_become_tabs_in_a_tab_file(self):
        self.write("t.js", "class A {\n\tm() {\n\t\treturn 1;\n\t}\n}\n")
        out = self.run_tool("replace_symbol", path="t.js", name="A.m",
                            new_source="    m() {\n        if (x) {\n            return 2;\n        }\n    }\n")
        self.assertTrue(out.startswith("OK"), out)
        self.assertEqual(self.read("t.js"),
                         "class A {\n\tm() {\n\t\tif (x) {\n\t\t\treturn 2;\n\t\t}\n\t}\n}\n")

    def test_crlf_is_kept(self):
        self.write("w.py", "def a():\n    return 1\n\ndef b():\n    return 2\n", newline="\r\n")
        out = self.run_tool("replace_symbol", path="w.py", name="a",
                            new_source="def a():\n    return 10\n")
        self.assertTrue(out.startswith("OK"), out)
        self.assertEqual(self.read("w.py"),
                         "def a():\r\n    return 10\r\n\r\ndef b():\r\n    return 2\r\n")

    def test_empty_source_deletes_and_identical_source_is_no_change(self):
        out = self.run_tool("replace_symbol", path="inv.py", name="calc_tax", new_source="")
        self.assertTrue(out.startswith("OK — deleted function calc_tax"), out)
        self.assertNotIn("calc_tax", self.read("inv.py"))
        out = self.run_tool("replace_symbol", path="inv.py", name="find_all",
                            new_source=INV.split("\n\n")[1].strip("\n") + "\n")
        self.assertTrue(out.startswith("No change"), out)

    def test_errors_write_nothing(self):
        self.write("x.py", "class A:\n    def f(self): pass\n\nclass B:\n    def f(self): pass\n")
        out = self.run_tool("replace_symbol", path="x.py", name="f", new_source="def f(): pass\n")
        self.assertIn("matches 2 definitions", out)
        self.assertTrue(self.run_tool("replace_symbol", path="x.py", name="nope",
                                      new_source="x").startswith("ERROR: no definition named"))
        self.write("n.md", "# x\n")
        self.assertTrue(self.run_tool("replace_symbol", path="n.md", name="x",
                                      new_source="y").startswith("ERROR: n.md is not a source file"))
        self.assertTrue(self.run_tool("replace_symbol", path="x.py", name="A.f",
                                      new_source="[written to x.py]").startswith("ERROR: new_source is a history placeholder"))
        self.assertEqual(self.read("x.py"), "class A:\n    def f(self): pass\n\nclass B:\n    def f(self): pass\n")


@NAV
class ChecksWithoutIndex(Base):
    def test_old_string_in_a_sibling_file(self):
        self.write("cart.py", "def total(x):\n    return x + calc_tax(x)\n")
        out = self.run_tool("edit_file", path="inv.py", old_string="    return x + calc_tax(x)",
                            new_string="    return x")
        self.assertIn("is in cart.py:2, not in inv.py — did you mean that file?", out)

    def test_rename_lists_uses_in_other_files_without_the_index(self):
        self.write("cart.py", "from inv import calc_tax\n\n\ndef total(x):\n    return x + calc_tax(x)\n")
        out = self.run_tool("edit_file", path="inv.py", old_string="def calc_tax(amount):",
                            new_string="def compute_tax(amount):")
        self.assertIn("WARNING: `calc_tax` is no longer defined in inv.py, but these places still "
                      "use it", out)
        self.assertIn("  cart.py:1: from inv import calc_tax", out)
        self.assertIn("  cart.py:5 (in total): return x + calc_tax(x)", out)
        self.assertNotIn("find_references", out)          # the scan covered every file

    def test_a_same_named_method_on_an_object_is_not_a_use(self):
        self.write("rep.py", "import inv as pricing\nfrom inv import calc_tax\n\n\n"
                             "def report(ledger, a):\n    return calc_tax(a) + ledger.calc_tax()"
                             " + pricing.calc_tax(a)\n")
        out = self.run_tool("edit_file", path="inv.py", old_string="def calc_tax(amount):",
                            new_string="def compute_tax(amount):")
        self.assertIn("  rep.py:2: from inv import calc_tax", out)
        self.assertIn("  rep.py:6 (in report):", out)          # calc_tax(a) / pricing.calc_tax(a)
        self.write("rep.py", "def report(ledger):\n    return ledger.calc_tax()\n")
        self.write("inv.py", INV)
        out = self.run_tool("edit_file", path="inv.py", old_string="def calc_tax(amount):",
                            new_string="def compute_tax(amount):")
        self.assertNotIn("rep.py", out)

    def test_nothing_left_says_nothing(self):
        out = self.run_tool("edit_file", path="inv.py", old_string="def calc_tax(amount):",
                            new_string="def compute_tax(amount):")
        self.assertNotIn("WARNING", out)
        self.assertNotIn("NOTE", out)

    def test_a_scan_cut_short_says_so(self):
        self.write("cart.py", "from inv import calc_tax\n\n\ndef total(x):\n    return calc_tax(x)\n")
        with mock.patch.object(editing, "_SCAN_FILES", 0):
            out = self.run_tool("edit_file", path="inv.py", old_string="def calc_tax(amount):",
                                new_string="def compute_tax(amount):")
        self.assertIn('NOTE: `calc_tax` is no longer defined in inv.py. (other files: 0 checked in '
                      'the time allowed — find_references("calc_tax", role="call") lists the rest)',
                      out)

    def test_own_file_uses_are_listed_with_the_scope(self):
        self.write("m.py", "def helper(a):\n    return a\n\n\ndef main():\n    return helper(1)\n")
        self.write("n.py", "from m import helper\n\nX = helper(2)\n")
        out = self.run_tool("edit_file", path="m.py", old_string="def helper(a):",
                            new_string="def helper(a, b):")
        self.assertIn("NOTE: `helper`'s parameters changed from (a) to (a, b)", out)
        self.assertIn("  m.py:6 (in main): return helper(1)", out)
        self.assertIn("  n.py:3 (in X): X = helper(2)", out)

    def test_scan_stops_when_cancelled(self):
        import threading
        ev = threading.Event()
        ev.set()
        uses, complete, checked = editing._scan_uses(self.root, "x.py", "calc_tax", {"call"},
                                                     "python", ev)
        self.assertEqual((uses, complete, checked), ([], False, 0))

    def test_write_file_that_drops_definitions_warns(self):
        out = self.run_tool("write_file", path="inv.py", content="def calc_tax(amount):\n    return 0\n")
        self.assertIn("WARNING: this write removed 1 definition the file had: Inventory.", out)
        self.assertNotIn("find_all", out)


@NAV
class ChecksWithIndex(Base):
    def setUp(self):
        super().setUp()
        self.home = tempfile.TemporaryDirectory()
        self.write("shop/pricing.py", "def calc_tax(amount):\n    return amount * 0.2\n\n\n"
                                      "def format_price(amount):\n    return f'{amount:.2f}'\n")
        self.write("shop/cart.py", "from shop.pricing import calc_tax, format_price\n\n\nclass Cart:\n"
                                   "    def total(self, x):\n        return x + calc_tax(x)\n\n"
                                   "    def render(self):\n        return format_price(self.total(1))\n")
        self.write("app/main.py", "from shop.pricing import format_price\n\n\ndef main():\n"
                                  "    print(format_price(3))\n")
        self.write("app/ledger.py", "class Ledger:\n    def calc_tax(self):\n        return 0\n\n\n"
                                    "def booked(ledger):\n    return ledger.calc_tax()\n")
        self._patches = [mock.patch.object(ci, "_STAT_THROTTLE_S", 0.0),
                         mock.patch.dict(os.environ, {"HOME": self.home.name})]
        for p in self._patches:
            p.start()
        self.idx = ci.ProjectIndex(self.root, ci.DEFAULT_MAX_BYTES)
        self.idx.start()
        self.assertIsNone(self.idx.wait_fresh())

    def tearDown(self):
        self.idx.stop()
        self.idx.join(2)
        for p in reversed(self._patches):
            p.stop()
        self.home.cleanup()
        super().tearDown()

    def test_rename_lists_the_uses_left_in_other_files(self):
        out = self.run_tool("edit_file", index=self.idx, path="shop/pricing.py",
                            old_string="def calc_tax(amount):", new_string="def compute_tax(amount):")
        self.assertIn("WARNING: `calc_tax` is no longer defined in shop/pricing.py", out)
        self.assertIn("  shop/cart.py:6 (in Cart.total): return x + calc_tax(x)", out)
        self.assertIn("  shop/cart.py:1: from shop.pricing import calc_tax", out)
        self.assertNotIn("ledger", out)          # Ledger.calc_tax is another method

    def test_signature_change_lists_every_call(self):
        out = self.run_tool("edit_file", index=self.idx, path="shop/pricing.py",
                            old_string="def format_price(amount):",
                            new_string="def format_price(amount, currency):")
        self.assertIn("parameters changed from (amount) to (amount, currency)", out)
        self.assertIn("  app/main.py:5 (in main): print(format_price(3))", out)
        self.assertIn("  shop/cart.py:9 (in Cart.render): return format_price(self.total(1))", out)

    def test_old_string_found_in_another_indexed_file(self):
        out = self.run_tool("edit_file", index=self.idx, path="shop/pricing.py",
                            old_string="    print(format_price(3))", new_string="x")
        self.assertIn("is in app/main.py:5, not in shop/pricing.py", out)

    def test_unchanged_definitions_add_nothing(self):
        out = self.run_tool("edit_file", index=self.idx, path="shop/pricing.py",
                            old_string="amount * 0.2", new_string="amount * 0.25")
        self.assertNotIn("WARNING", out)
        self.assertNotIn("NOTE", out)


class FeaturesOff(Base):
    """edit_features off: the results the edit tools gave before they had any."""
    features = frozenset()

    def test_results_are_the_old_ones(self):
        self.assertEqual(self.run_tool("edit_file", path="inv.py", old_string="amount * 0.2",
                                       new_string="amount * 0.25"), "OK — 1 change applied")
        self.assertEqual(self.run_tool("edit_file", path="inv.py", old_string="return None",
                                       new_string="x"),
                         "ERROR: old_string found 2 times; must match exactly once "
                         "(set replace_all=true to replace all)")
        self.assertEqual(self.run_tool("edit_file", path="inv.py", old_string="self.items",
                                       new_string="self.items", replace_all=True),
                         "Replaced 3 occurrence(s)")
        out = self.run_tool("edit_file", path="inv.py", old_string="for it in self.itemz:",
                            new_string="x")
        self.assertTrue(out.startswith("ERROR: old_string not found in file. The closest lines "
                                       "in the file are below"), out)
        self.assertNotIn("First difference", out)
        self.assertEqual(self.run_tool("write_file", path="inv.py", content="x = (\n"),
                         "Written: inv.py (1 line, 6 bytes)")

    def test_tool_list(self):
        names = lambda ts: [t["function"]["name"] for t in ts]
        off = tools.with_edit_features(tools.ALL_TOOLS, frozenset())
        self.assertNotIn("replace_symbol", names(off))
        edit = next(t for t in off if t["function"]["name"] == "edit_file")
        self.assertNotIn("within", edit["function"]["parameters"]["properties"])
        on = tools.with_edit_features(tools.ALL_TOOLS, editing.EDIT_FEATURES)
        edit = next(t for t in on if t["function"]["name"] == "edit_file")
        self.assertNotIn("within", edit["function"]["parameters"]["properties"])
        self.assertIn("lists where each occurrence is", edit["function"]["description"])
        if code_nav.AVAILABLE:
            self.assertIn("replace_symbol", names(on))


class PythonHint(unittest.TestCase):
    def test_environment_names_python3_only_when_python_is_missing(self):
        which = lambda present: (lambda cmd: f"/usr/bin/{cmd}" if cmd in present else None)
        with mock.patch.object(prompts.shutil, "which", which({"python3"})):
            self.assertIn("- Python: run it as `python3` (there is no `python` command)",
                          prompts._environment(Path("."), {"run_command"}, "off"))
            self.assertNotIn("Python:", prompts._environment(Path("."), {"read_file"}, "off"))
        with mock.patch.object(prompts.shutil, "which", which({"python", "python3"})):
            self.assertNotIn("Python:", prompts._environment(Path("."), {"run_command"}, "off"))

    def test_command_not_found_gets_a_note(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tools, "_run_command", return_value="/bin/sh: python: command not found"), \
                mock.patch.dict(tools._EXECUTORS, {"run_command": tools._run_command}), \
                mock.patch.object(tools.shutil, "which", lambda c: "/usr/bin/python3"):
            out = dispatch("run_command", {"command": "python x.py"}, Path(tmp))
        self.assertTrue(out.endswith("(note: there is no `python` command here — use python3)"), out)


@NAV
class Prompt(unittest.TestCase):
    def test_editing_bullet_only_where_replace_symbol_is_offered(self):
        coding = prompts._nav_rules("coding", {"edit_file", "replace_symbol", "code_outline",
                                               "read_symbol", "edit_files"}, False)
        self.assertIn("`replace_symbol` rewrites a whole", coding)
        self.assertIn("`edit_files` makes several changes", coding)
        plain = prompts._nav_rules("coding", {"edit_file", "replace_symbol"}, False)
        self.assertNotIn("edit_files", plain)
        chat = prompts._nav_rules("chat", {"code_outline", "read_symbol"}, False)
        self.assertNotIn("replace_symbol", chat)


if __name__ == "__main__":
    unittest.main()
