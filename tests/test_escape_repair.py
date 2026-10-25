"""Two ways a small model spirals on a quoting slip, in any language
(harness/editing.py, harness/syntax_watch.py): an escape level too many in a
write tool's text (\\" where the file needs "), repaired when the file's own
syntax check proves it; and a syntax error it cannot see, marked at its
column and repeated on every later call until the file parses again.

The Python cases are the recorded failures (signature-change, 2026-10-25).

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import tempfile
import unittest
from pathlib import Path

from harness import code_nav, editing
from harness.llm.base import ChatResponse, ToolCall
from harness.tools import dispatch
from test_review_fixes import HarnessCase

CART = ('class Cart:\n    def summary(self):\n'
        '        return f"{len(self.lines)} lines, total {format_price(self.total())}"\n')
OLD = '        return f"{len(self.lines)} lines, total {format_price(self.total())}"'
OVER = '        return f"{len(self.lines)} lines, total {format_price(self.total(), \\"EUR\\")}"'
CHECKOUT = 'def checkout(total):\n    return {"total": total, "text": f"Pay {format_price(total)}"}\n'
MOVED = '    return {"total": total, "text": f"Pay {format_price(total, "EUR")"}"}'

REPAIRED = "one level of escaping removed"
JS = unittest.skipUnless(code_nav.AVAILABLE and code_nav.grammar_available("javascript"),
                         "needs the JavaScript grammar")
JSON = unittest.skipUnless(code_nav.AVAILABLE and code_nav.grammar_available("json"),
                           "needs the JSON grammar")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.write("cart.py", CART)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        (self.root / rel).write_text(text, encoding="utf-8")

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")

    def run_tool(self, tool, **args):
        return dispatch(tool, args, self.root, edit_features=editing.EDIT_FEATURES)


class EscapeRepair(Base):
    def test_the_recorded_over_escape_is_written_unescaped(self):
        out = self.run_tool("edit_file", path="cart.py", old_string=OLD, new_string=OVER)
        self.assertIn(REPAIRED, out)
        self.assertNotIn("WARNING", out)
        self.assertIn('format_price(self.total(), "EUR")', self.read("cart.py"))
        self.assertNotIn("\\", self.read("cart.py"))

    def test_edit_files_and_write_file_repair_too(self):
        out = self.run_tool("edit_files", edits=[{"path": "cart.py", "old_string": OLD,
                                                  "new_string": OVER}])
        self.assertIn(REPAIRED, out)
        self.assertNotIn("\\", self.read("cart.py"))
        out = self.run_tool("write_file", path="cart2.py", content=CART.replace(OLD, OVER))
        self.assertIn(REPAIRED, out)
        self.assertNotIn("\\", self.read("cart2.py"))

    def test_real_escapes_inside_a_string_are_kept(self):
        self.write("x.py", "a = 1\n")
        out = self.run_tool("edit_file", path="x.py", old_string="a = 1",
                            new_string='a = "say \\"hi\\""')
        self.assertNotIn(REPAIRED, out)
        self.assertEqual(self.read("x.py"), 'a = "say \\"hi\\""\n')

    def test_no_repair_when_one_level_less_still_does_not_parse(self):
        self.write("x.py", "a = 1\n")
        out = self.run_tool("edit_file", path="x.py", old_string="a = 1",
                            new_string='a = (\\"x\\"')
        self.assertNotIn(REPAIRED, out)
        self.assertIn("no longer parses", out)
        self.assertIn('\\"x\\"', self.read("x.py"))

    def test_a_file_broken_before_is_not_repaired(self):
        self.write("x.py", "def f(:\n    pass\nb = 1\n")
        out = self.run_tool("edit_file", path="x.py", old_string="b = 1",
                            new_string='b = \\"x\\"')
        self.assertNotIn(REPAIRED, out)

    @JS
    def test_javascript(self):
        self.write("a.js", 'function f() { return "x"; }\n')
        out = self.run_tool("edit_file", path="a.js", old_string='return "x";',
                            new_string='return \\"x\\" + g(\\"y\\");')
        self.assertIn(REPAIRED, out)
        self.assertEqual(self.read("a.js"), 'function f() { return "x" + g("y"); }\n')

    @JSON
    def test_json(self):
        self.write("d.json", '{"a": 1}\n')
        out = self.run_tool("write_file", path="d.json", content='{\\"a\\": 2}\n')
        self.assertIn(REPAIRED, out)
        self.assertEqual(self.read("d.json"), '{"a": 2}\n')


class Mark(Base):
    def test_the_recorded_moved_quote_is_marked_at_its_column(self):
        self.write("checkout.py", CHECKOUT)
        out = self.run_tool("edit_file", path="checkout.py",
                            old_string=CHECKOUT.splitlines()[1], new_string=MOVED)
        self.assertIn("no longer parses after this change — line 2, column", out)
        row, caret = out.split("WARNING")[1].splitlines()[1:3]
        self.assertTrue(row.endswith(MOVED), row)
        at = caret.index("^")
        self.assertEqual(row[at], "f")           # the start of the broken f-string
        self.assertNotIn(REPAIRED, out)

    @JS
    def test_tree_sitter_names_the_character(self):
        self.write("a.js", 'function f() { return "x"; }\n')
        out = self.run_tool("edit_file", path="a.js", old_string='return "x";',
                            new_string='return "x" + ;')
        self.assertIn("line 1, column", out)
        self.assertIn("^ here:", out)

    def test_mark_names_a_backslash(self):
        mark = editing._mark(['x = a \\ b'], 1, 7)
        self.assertIn("^ here: '\\' (backslash)", mark)
        self.assertEqual(mark.splitlines()[2].index("^"), mark.splitlines()[1].index("\\"))


class Watch(HarnessCase):
    def setUp(self):
        super().setUp()
        self.h.set_mode("coding")
        (self.home / "checkout.py").write_text(CHECKOUT)

    def tool_results(self):
        return [m["content"] for m in self.h.messages if m["role"] == "tool"]

    def break_then(self, *calls):
        self.client.replies = [
            ChatResponse(tool_calls=[ToolCall("edit_file", {
                "path": "checkout.py", "old_string": CHECKOUT.splitlines()[1],
                "new_string": MOVED})])] + [
            ChatResponse(tool_calls=[ToolCall(n, a)]) for n, a in calls] + [
            ChatResponse(content="done")]
        self.h.send("give it a currency")
        return self.tool_results()

    def test_later_calls_say_the_file_still_does_not_parse(self):
        read = ("read_file", {"path": "checkout.py"})
        results = self.break_then(read, read)
        self.assertIn("no longer parses", results[0])
        self.assertIn("NOTE: checkout.py still does not parse since your edit", results[1])
        self.assertNotIn("Inspecting the file further", results[1])
        self.assertIn("Inspecting the file further won't change it", results[2])

    def test_a_fix_ends_the_notes(self):
        fix = ("edit_file", {"path": "checkout.py", "old_string": MOVED,
                             "new_string": CHECKOUT.splitlines()[1]})
        results = self.break_then(fix, ("read_file", {"path": "checkout.py"}))
        self.assertNotIn("NOTE", results[1] + results[2])

    def test_a_file_broken_before_the_turn_is_not_blamed(self):
        (self.home / "checkout.py").write_text(CHECKOUT + "def g(:\n    pass\n")
        self.client.replies = [
            ChatResponse(tool_calls=[ToolCall("edit_file", {
                "path": "checkout.py", "old_string": "total)}", "new_string": "total, 'EUR')}"})]),
            ChatResponse(tool_calls=[ToolCall("read_file", {"path": "checkout.py"})]),
            ChatResponse(content="done")]
        self.h.send("go")
        self.assertNotIn("NOTE", self.tool_results()[1])

    def test_a_new_turn_starts_clean(self):
        self.break_then()
        self.client.replies = [ChatResponse(tool_calls=[ToolCall("read_file", {"path": "checkout.py"})]),
                               ChatResponse(content="done")]
        self.h.send("look again")
        self.assertNotIn("NOTE", self.tool_results()[-1])


if __name__ == "__main__":
    unittest.main()
