"""edit_files: several edits, in one file or across files, in one call — all
written or none.  Also the per-file diffs the harness shows for it.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import code_index as ci
from harness import code_nav, editing, session as session_mod, tools
from harness.events import DiffEvent
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.tools import dispatch

PRICING = ("def format_price(amount):\n    return f\"{amount:.2f}\"\n\n\n"
           "def gross(amount):\n    return amount * 1.2\n")
CART = ("from pricing import format_price\n\n\nclass Cart:\n    def summary(self):\n"
        "        return format_price(self.total())\n")
REPORT = "from pricing import format_price\n\n\ndef line(x):\n    return format_price(x)\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        for rel, text in (("pricing.py", PRICING), ("cart.py", CART), ("report.py", REPORT)):
            (self.root / rel).write_text(text)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, rel):
        return (self.root / rel).read_text()

    def edits(self, edits, index=None, features=editing.EDIT_FEATURES):
        return dispatch("edit_files", {"edits": edits}, self.root, index=index,
                        edit_features=features)


class EditFiles(Base):
    SIGNATURE = [
        {"path": "pricing.py", "old_string": "def format_price(amount):",
         "new_string": "def format_price(amount, currency):"},
        {"path": "pricing.py", "old_string": '    return f"{amount:.2f}"',
         "new_string": '    return f"{amount:.2f} {currency}"'},
        {"path": "cart.py", "old_string": "format_price(self.total())",
         "new_string": 'format_price(self.total(), "EUR")'},
        {"path": "report.py", "old_string": "format_price(x)", "new_string": 'format_price(x, "EUR")'},
    ]

    def test_edits_across_files_in_one_call(self):
        out = self.edits(self.SIGNATURE)
        self.assertTrue(out.startswith("OK — 4 edits applied in 3 files."), out)
        self.assertIn("  pricing.py:1 (in format_price): def format_price(amount, currency):", out)
        self.assertIn('  cart.py:6 (in Cart.summary): return format_price(self.total(), "EUR")', out)
        self.assertIn('format_price(x, "EUR")', self.read("report.py"))
        self.assertNotIn("edit_files changes all", out)       # no pointer at itself

    def test_edits_in_one_file_apply_in_order(self):
        out = self.edits([
            {"path": "pricing.py", "old_string": "amount * 1.2", "new_string": "amount * RATE"},
            {"path": "pricing.py", "old_string": "def gross(amount):",
             "new_string": "RATE = 1.2\n\n\ndef gross(amount):"},
        ])
        self.assertTrue(out.startswith("OK — 2 edits applied in 1 file."), out)
        self.assertTrue(self.read("pricing.py").endswith(
            "RATE = 1.2\n\n\ndef gross(amount):\n    return amount * RATE\n"))

    def test_one_failing_edit_writes_nothing(self):
        bad = self.SIGNATURE[:2] + [{"path": "cart.py", "old_string": "format_price(self.totl())",
                                     "new_string": "x"}] + self.SIGNATURE[3:]
        out = self.edits(bad)
        self.assertTrue(out.startswith("ERROR: edit 3 of 4 (cart.py): old_string not found"), out)
        self.assertIn("Nothing was written — no file was changed", out)
        self.assertIn("format_price(self.total())", out)      # the closest line, to copy
        self.assertEqual(self.read("pricing.py"), PRICING)
        self.assertEqual(self.read("report.py"), REPORT)

    def test_edits_sent_as_a_json_string(self):
        out = self.edits(json.dumps(self.SIGNATURE[2:]))
        self.assertTrue(out.startswith("OK — 2 edits applied in 2 files."), out)

    def test_bad_shapes_are_explained(self):
        self.assertTrue(self.edits([]).startswith("ERROR: edits must be a non-empty list"))
        out = self.edits([{"path": "cart.py", "old_string": "x"}])
        self.assertTrue(out.startswith("ERROR: edit 1 has no new_string"), out)
        out = dispatch("edit_files", {}, self.root)
        self.assertIn("without required argument(s): edits", out)

    def test_placeholder_in_any_edit_is_refused(self):
        out = self.edits([self.SIGNATURE[2], {"path": "report.py", "old_string": "format_price(x)",
                                              "new_string": "[written to report.py]"}])
        self.assertTrue(out.startswith("ERROR: edit 2's new_string holds"), out)
        self.assertEqual(self.read("cart.py"), CART)

    def test_already_applied_edit_is_noted_not_failed(self):
        self.edits(self.SIGNATURE[2:3])
        out = self.edits(self.SIGNATURE[2:])
        self.assertTrue(out.startswith("OK — 1 edit applied in 1 file."), out)
        self.assertIn("edit 1 (cart.py): No change needed", out)

    def test_syntax_note_per_file(self):
        out = self.edits([{"path": "report.py", "old_string": "format_price(x)",
                           "new_string": "format_price(x"}])
        self.assertIn("WARNING: report.py no longer parses", out)

    def test_off_with_the_batch_feature(self):
        names = [t["function"]["name"] for t in tools.offered(
            "coding", edit_features=editing.EDIT_FEATURES - {"batch"})]
        self.assertNotIn("edit_files", names)
        names = [t["function"]["name"] for t in tools.offered(
            "coding", edit_features=editing.EDIT_FEATURES)]
        self.assertIn("edit_files", names)


@unittest.skipUnless(code_nav.AVAILABLE, "needs tree-sitter")
class WithIndex(Base):
    def setUp(self):
        super().setUp()
        self.home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.object(ci.store, "_STAT_THROTTLE_S", 3600.0),
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

    def test_every_edited_path_is_reindexed(self):
        self.idx._dirty = False
        out = self.edits([{"path": "cart.py", "old_string": "def summary(self):",
                           "new_string": "def describe(self):"},
                          {"path": "report.py", "old_string": "def line(x):",
                           "new_string": "def report_line(x):"}], index=self.idx)
        self.assertTrue(out.startswith("OK"), out)
        found = ci.index_search("report_line", workdir=self.root, index=self.idx)
        self.assertIn("report.py", found)
        self.assertIn("Cart.describe", ci.index_search("describe", workdir=self.root, index=self.idx))


class FakeClient(LLMClient):
    provider_name = "fake"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.n = 0

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.n += 1
        if self.n == 1:
            return ChatResponse(done_reason="stop", tool_calls=[ToolCall("edit_files", {"edits": [
                {"path": "cart.py", "old_string": "format_price(self.total())",
                 "new_string": "format_price(self.total(), 'EUR')"},
                {"path": "report.py", "old_string": "format_price(x)",
                 "new_string": "format_price(x, 'EUR')"}]})])
        return ChatResponse(content="done", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class HarnessDiffs(Base):
    def test_one_diff_per_file(self):
        with mock.patch.object(hmod, "make_client", lambda *a, **k: FakeClient("http://fake", "fake")), \
                mock.patch.object(session_mod, "_PREFS_PATH", self.root / "prefs.json"), \
                mock.patch.object(session_mod, "SESSION_DIR", self.root / "sessions"):
            h = hmod.Harness(host="http://fake", model="fake", workdir=self.root)
            h.stream = False
            h.mode = "coding"
            sub = h.event_queue.subscribe(replay=False)
            h.send("change it")
            events = []
            while True:
                ev = sub.get(timeout=5)
                events.append(ev)
                if type(ev).__name__ == "DoneEvent":
                    break
            h.logger.close()
        diffs = [e for e in events if isinstance(e, DiffEvent)]
        self.assertEqual(sorted(d.path for d in diffs), ["cart.py", "report.py"])


if __name__ == "__main__":
    unittest.main()
