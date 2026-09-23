"""Context compaction: what it removes, what it must keep, and the summariser call.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness.llm.base import ChatResponse, LLMClient


class SummaryClient(LLMClient):
    provider_name = "fake"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = []
        self.reply = "<think>pondering</think>SUMMARY"
        self.on_call = None

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.calls.append({"messages": messages, "think": think, "num_ctx": num_ctx})
        if self.on_call:
            self.on_call()
        return ChatResponse(content=self.reply, done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


def turn(n):
    return [
        {"role": "user", "content": f"question {n} " + "q" * 800},
        {"role": "thinking", "content": "t" * 2000},
        {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": f"{n}.py"}}}]},
        {"role": "tool", "name": "read_file", "content": "r" * 6000},
        {"role": "assistant", "content": "a" * 1000},
    ]


class Compaction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: SummaryClient("http://fake", "fake")
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=Path(self.tmp.name))
        self.h.set_mode("coding")
        self.client = self.h.client

    def tearDown(self):
        hmod.make_client = self._orig
        self.h.logger.close()
        self.tmp.cleanup()

    def history(self, turns, current="CURRENT QUESTION"):
        self.h.messages = self.h.messages[:1]
        for n in range(turns):
            self.h.messages += turn(n)
        self.h.messages.append({"role": "user", "content": current})
        self.h._turn_user = self.h.messages[-1]

    def fixed(self):
        return hmod._msg_tokens(self.h.messages[0])

    def test_never_wipes_the_conversation(self):
        # Regression: the system prompt alone exceeded limit // 3, so every message went.
        self.history(100)
        self.h.context_limit = self.fixed() + 6000
        notice = self.h.compact()
        roles = [m["role"] for m in self.h.messages]
        self.assertIn("summarised", notice)
        self.assertEqual(self.h.messages[-1]["content"].split(hmod._SUMMARY_CLOSE)[-1], "CURRENT QUESTION")
        self.assertEqual(roles[1], "user")
        self.assertLessEqual(self.h._estimate(), self.h.context_limit)

    def test_keeps_the_running_turns_latest_tool_group(self):
        self.history(3, current="now")
        self.h.messages += [
            {"role": "assistant", "content": None,
             "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "big.py"}}}]},
            {"role": "tool", "name": "read_file", "content": "x" * 40000},
        ]
        self.h.context_limit = self.fixed() + 4000
        notice = self.h.compact(summarise=False)
        roles = [m["role"] for m in self.h.messages]
        self.assertEqual(roles[-3:], ["user", "assistant", "tool"])
        self.assertIn("trimmed 1 tool result", notice)
        self.assertIn("removed by context compaction", self.h.messages[-1]["content"])
        self.assertLessEqual(self.h._estimate(), self.h.context_limit)

    def test_drops_older_tool_groups_of_the_running_turn(self):
        self.history(0, current="do a long task")
        for n in range(20):
            self.h.messages += [
                {"role": "assistant", "content": None,
                 "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": f"{n}"}}}]},
                {"role": "tool", "name": "read_file", "content": "r" * 4000}]
        self.h.context_limit = self.fixed() + 8000
        self.h.compact(summarise=False)
        self.assertEqual(self.h.messages[1]["content"], "do a long task")
        self.assertLess(len(self.h.messages), 42)
        self.assertEqual(self.h.messages[-1]["role"], "tool")

    def test_system_prompt_over_the_limit_changes_nothing(self):
        self.history(5)
        before = list(self.h.messages)
        self.h.context_limit = self.fixed() - 1
        notice = self.h.compact()
        self.assertEqual(self.h.messages, before)
        self.assertIn("Raise it with /context", notice)

    def test_summaries_replace_not_stack(self):
        self.history(40)
        self.h.context_limit = self.fixed() + 8000
        self.h.compact()
        self.client.reply = "SECOND"
        self.h.messages[-1:-1] = [m for n in range(8) for m in turn(100 + n)]
        self.h.compact()
        text = "".join(str(m.get("content")) for m in self.h.messages)
        self.assertEqual(text.count(hmod._SUMMARY_OPEN), 1)
        self.assertIn("SECOND", text)
        # The earlier summary was handed to the summariser to fold in.
        self.assertIn("SUMMARY", self.client.calls[-1]["messages"][0]["content"])

    def test_summariser_call(self):
        self.history(40)
        self.h.context_limit = self.fixed() + 8000
        self.h.compact()
        call = self.client.calls[-1]
        self.assertIs(call["think"], False)
        self.assertEqual(call["num_ctx"], 32768)
        self.assertLessEqual(len(call["messages"][0]["content"]) // 4,
                             32768 - hmod._SUMMARY_REPLY_TOKENS)
        joined = "".join(str(m.get("content")) for m in self.h.messages)
        self.assertNotIn("pondering", joined)

    def test_cancel_during_summary_leaves_history_intact(self):
        self.history(40)
        before = [dict(m) for m in self.h.messages]
        self.h.context_limit = self.fixed() + 8000
        self.client.on_call = self.h._cancel.set
        notice = self.h.compact()
        self.assertIn("cancelled", notice)
        self.assertEqual(self.h.messages, before)

    def test_linear_cost(self):
        self.history(100)
        self.h.context_limit = self.fixed() + 6000
        with mock.patch.object(hmod, "_msg_tokens", wraps=hmod._msg_tokens) as counted:
            t0 = time.perf_counter()
            self.h.compact(summarise=False)
            elapsed = time.perf_counter() - t0
        self.assertLess(counted.call_count, 2 * 501)
        self.assertLess(elapsed, 0.5)  # was ~0.1 s at O(n²)

    def test_trimming_leaves_real_headroom(self):
        # Regression: pass 3 trimmed only down to the limit, leaving the context a
        # few tokens under it — ~100% full, yet auto-compaction never fired again.
        self.history(0, current="now")
        self.h.messages += [
            {"role": "assistant", "content": None,
             "tool_calls": [{"function": {"name": "grep_files", "arguments": {"pattern": "x"}}}]},
            {"role": "tool", "name": "grep_files", "content": "x" * 80000},
        ]
        self.h.context_limit = self.fixed() + 12000
        self.h.compact(summarise=False)
        self.assertLessEqual(self.h._estimate(), self.fixed() + 12000 // 3 + 64)


if __name__ == "__main__":
    unittest.main()
