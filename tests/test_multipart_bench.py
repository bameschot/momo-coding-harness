"""evals/multipart_bench.py without a model server: its checks grade the
reference solutions (and fail the untouched project), and a run records the
harness's end-of-turn notes and refused edits."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

import multipart_bench as B  # noqa: E402

import harness.harness as hmod  # noqa: E402
from harness.llm.base import ChatResponse, LLMClient, ToolCall  # noqa: E402
from home_tmp import setUpModule, tearDownModule  # noqa: E402,F401

DROP_ADD = {"path": "shop/pricing.py",
            "old_string": "def format_price(amount):\n    \"\"\"An amount with two decimals, e.g. '12.50'.\"\"\"\n"
                          '    return f"{amount:.2f}"',
            "new_string": "def added_thing(amount):\n    return amount"}


class Scripted(LLMClient):
    """An edit that drops format_price, a reply that only announces the next
    step, then a final summary."""
    provider_name = "fake"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.seen = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.seen.append((messages, tools))
        if len(self.seen) == 1:
            return ChatResponse(tool_calls=[ToolCall("edit_file", DROP_ADD)], done_reason="stop")
        if len(self.seen) == 2:
            return ChatResponse(content="My next step is to update shop/pricing.py.",
                                done_reason="stop")
        return ChatResponse(content="Updated shop/pricing.py.", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class Bench(unittest.TestCase):
    def test_checks_grade_the_reference_solutions(self):
        self.assertEqual(B.selftest(), 0)

    def test_a_run_counts_the_notes_and_the_refused_edit(self):
        clients = []

        def make(*a, **k):
            clients.append(Scripted("http://fake", "fake"))
            return clients[-1]
        with mock.patch.object(hmod, "make_client", make):
            r = B.run_once(B.BY_ID["single"], host="http://fake", timeout=20, max_secs=60,
                           context_limit=None, keep=None, tag="t")
        self.assertEqual(r["next_step_notes"], 1)
        self.assertEqual(r["tools"], ["edit_file"])
        self.assertEqual(r["refusals"], 1)
        self.assertEqual(r["lost_defs"], [])
        self.assertEqual(len(clients[0].seen), 3)
        self.assertNotIn("todos", [t["function"]["name"] for t in clients[0].seen[0][1]])

    def test_the_summary_has_one_row_per_task(self):
        row = {"task": "single", "parts": {"tax-table": False}, "parts_passed": 0, "n_parts": 1,
               "all_passed": False, "own_tests": True, "next_step_notes": 1, "refusals": 2,
               "lost_defs": [], "capped": False, "n_calls": 3, "secs": 10.0,
               "eval_tokens": 100, "prompt_total": 1000}
        text = B.summarise([row, dict(row, refusals=0)])
        line = [ln for ln in text.splitlines() if ln.startswith("single")][0]
        self.assertEqual(line.split()[:8], ["single", "2", "0%", "0/2", "2/2", "2", "2", "0"])
        self.assertIn("single tax-table: 2", text)


if __name__ == "__main__":
    unittest.main()
