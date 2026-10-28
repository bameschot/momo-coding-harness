"""Context compaction: what it removes, what it must keep, and the summariser call.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import compaction
from harness.llm.base import ChatResponse, LLMClient
from home_tmp import setUpModule, tearDownModule  # noqa: F401


def reply(goal="SUMMARY", constraint="none", nxt="1. carry on"):
    return (f"<think>pondering</think>## Goal\n{goal}\n\n## Constraints & preferences\n"
            f"{constraint}\n\n## Progress\nDone:\n- read files\n\n## Decisions\nnone\n\n"
            f"## Failed attempts / gotchas\nnone\n\n## Next steps\n{nxt}\n")


class SummaryClient(LLMClient):
    provider_name = "fake"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = []
        self.reply = reply()
        self.fail = False
        self.on_call = None

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.calls.append({"messages": messages, "think": think, "num_ctx": num_ctx})
        if self.on_call:
            self.on_call()
        if self.fail:
            raise ConnectionError("server gone")
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
        return compaction.msg_tokens(self.h.messages[0])

    def test_never_wipes_the_conversation(self):
        # Regression: the system prompt alone exceeded limit // 3, so every message went.
        self.history(100)
        self.h.context_limit = self.fixed() + 6000
        notice = self.h.compact()
        roles = [m["role"] for m in self.h.messages]
        self.assertIn("summarised", notice)
        self.assertEqual(self.h.messages[-1]["content"].split(compaction._SUMMARY_CLOSE)[-1], "CURRENT QUESTION")
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
        summary, body = compaction._split_summary(self.h.messages[1]["content"])
        self.assertEqual(body, "do a long task")
        self.assertIn("read: ", summary)          # /fast-compact keeps the harness's facts
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
        self.client.reply = reply("SECOND")
        self.h.messages[-1:-1] = [m for n in range(8) for m in turn(100 + n)]
        self.h.compact()
        text = "".join(str(m.get("content")) for m in self.h.messages)
        self.assertEqual(text.count(compaction._SUMMARY_OPEN), 1)
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
                             32768 - compaction._SUMMARY_REPLY_TOKENS)
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
        with mock.patch.object(compaction, "msg_tokens", wraps=compaction.msg_tokens) as counted:
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


    def test_constraints_and_facts_survive_a_failed_summary(self):
        self.history(0, current="now")
        self.h.messages[1:1] = [
            {"role": "user", "content": "Rename foo to bar. Do not touch the tests."},
            {"role": "assistant", "content": None, "tool_calls": [
                {"function": {"name": "edit_files", "arguments": {"edits": [
                    {"path": "a.py", "old_string": "foo", "new_string": "bar"},
                    {"path": "b.py", "old_string": "foo", "new_string": "bar"}]}}},
                {"function": {"name": "edit_file", "arguments": {"path": "c.py"}}},
                {"function": {"name": "run_command", "arguments": {"command": "pytest -q"}}}]},
            {"role": "tool", "name": "edit_files", "content": "OK"},
            {"role": "tool", "name": "edit_file", "content": "ERROR: old_string not found"},
            {"role": "tool", "name": "run_command", "content": "1 failed\n[exit code: 1]"},
            {"role": "assistant", "content": "a" * 30000},
        ]
        self.h.context_limit = self.fixed() + 6000
        self.client.reply = reply("Rename foo to bar", "- do not touch the tests")
        self.h.compact()
        st = self.h.summary_state
        self.assertEqual(st["files"]["modified"], ["a.py", "b.py"])   # c.py's edit failed
        self.assertEqual(st["commands"][-1]["status"], "exit 1")
        self.assertIn("do not touch the tests", st["sections"]["constraints"])

        # Second compaction: the summariser is down.  Nothing earlier is lost.
        self.client.fail = True
        self.h.messages[-1:-1] = [
            {"role": "user", "content": "now fix the failing test"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"function": {"name": "move_file", "arguments": {"src": "a.py", "dst": "z.py"}}},
                {"function": {"name": "run_command", "arguments": {"command": "pytest  -q"}}}]},
            {"role": "tool", "name": "move_file", "content": "OK"},
            {"role": "tool", "name": "run_command", "content": "5 passed"},
            {"role": "assistant", "content": "a" * 30000},
        ]
        notice = self.h.compact()
        self.assertIn("unusable", notice)
        st = self.h.summary_state
        self.assertEqual(st["n"], 2)
        self.assertIn("do not touch the tests", st["sections"]["constraints"])
        self.assertEqual(st["files"]["modified"], ["b.py", "z.py"])
        self.assertEqual(st["commands"], [{"cmd": "pytest -q", "status": "exit 0",
                                           "failed_before": True}])
        self.assertEqual(st["requests"][0], "Rename foo to bar. Do not touch the tests.")
        text = self.h.messages[1]["content"]
        self.assertIn("pytest -q` → exit 0 (failed earlier)", text)
        self.assertIn("a.py → z.py", text)

    def test_garbled_reply_keeps_previous_sections(self):
        self.history(40)
        self.h.context_limit = self.fixed() + 8000
        self.h.compact()
        self.client.reply = "Sure! Here is a summary: things happened."
        self.h.messages[-1:-1] = [m for n in range(8) for m in turn(100 + n)]
        notice = self.h.compact()
        self.assertIn("unusable", notice)
        self.assertEqual(self.h.summary_state["sections"]["goal"], "SUMMARY")
        # The summariser got the previous sections to update, not to rewrite blind.
        self.assertIn("## Goal\nSUMMARY", self.client.calls[-1]["messages"][0]["content"])

    def test_legacy_summary_is_folded_in(self):
        self.history(10)
        self.h.messages[1]["content"] = ("[Earlier context summary:\nWe were porting X to Y.\n]\n\n"
                                         + self.h.messages[1]["content"])
        self.h.context_limit = self.fixed() + 6000
        self.client.fail = True
        self.h.compact()
        self.assertIn("We were porting X to Y.", self.h.summary_state["sections"]["legacy"])
        self.assertIn("## Earlier notes", self.h.messages[1]["content"])
        self.client.fail = False
        self.h.messages[-1:-1] = [m for n in range(8) for m in turn(100 + n)]
        self.h.compact()
        self.assertIn("We were porting X to Y.", self.client.calls[-1]["messages"][0]["content"])
        self.assertNotIn("legacy", self.h.summary_state["sections"])

    def test_rendered_summary_fits_its_budget(self):
        st = compaction.new_summary_state()
        st["n"] = 9
        st["requests"] = ["r" * 300] * 8
        st["requests_elided"] = 20
        st["files"]["read"] = [f"src/module_{i}/file_{i}.py" for i in range(200)]
        st["files"]["modified"] = [f"src/m{i}.py" for i in range(40)]
        st["commands"] = [{"cmd": "x" * 160, "status": "exit 1", "failed_before": False}] * 8
        st["sections"] = {"goal": "g", "constraints": "- keep the API",
                          "progress": "Done:\n" + "\n".join(f"- step {i} " + "d" * 80 for i in range(40)),
                          "next": "1. finish"}
        text = compaction.render_summary(st, 400)
        self.assertLessEqual(compaction.text_tokens(text), 400)
        self.assertIn("keep the API", text)
        self.assertIn("1. finish", text)

    def test_parse_sections(self):
        self.assertIsNone(compaction.parse_sections("just prose"))
        got = compaction.parse_sections("**Goal:**\nship\n### Next steps:\n1. a\n## Decisions\nNone.")
        self.assertIsNone(got)   # bold-only Goal line is not a heading; only 2 sections
        got = compaction.parse_sections("## Goal:\nship\n### Next steps:\n1. a\n## Decisions\nNone.")
        self.assertEqual(got, {"goal": "ship", "next": "1. a", "decisions": ""})
        got = compaction.parse_sections("## Goal\nship\n## Progress\n### Done\n- x\n### In progress\n- y\n"
                                        "## Next Steps\n1. z")
        self.assertEqual(got["progress"], "### Done\n- x\n### In progress\n- y")


if __name__ == "__main__":
    unittest.main()
