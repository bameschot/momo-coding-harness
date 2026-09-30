"""/think-history off|turn|all: the model's own stored reasoning goes back to it
on the assistant message it preceded, within the chosen window, and counts
toward the context only when it does.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness.commands import handle as handle_command
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.llm.llamacpp_client import LlamaCppClient
from harness.llm.ollama_client import OllamaClient


class FakeClient(LLMClient):
    """Replies with a read_file call (reasoning "plan A"), then an answer."""
    provider_name = "fake"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.requests = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.requests.append({"messages": messages, "preserve": self.preserve_thinking})
        if len(self.requests) == 1:
            return ChatResponse(thinking="plan A", done_reason="stop",
                                tool_calls=[ToolCall("read_file", {"path": "a.txt"})])
        return ChatResponse(content="done", thinking="plan B", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


def assistants(messages):
    return [m for m in messages if m.get("role") == "assistant"]


class ThinkHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "a.txt").write_text("hello\n")
        self._patches = [
            mock.patch.object(hmod, "make_client", lambda *a, **k: FakeClient("http://fake", "fake")),
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
        ]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)
        self.h.stream = False

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def history(self):
        """Two finished turns, each: user, thinking, assistant."""
        self.h.messages += [
            {"role": "user", "content": "q1"},
            {"role": "thinking", "content": "old thought"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "thinking", "content": "new thought"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}]},
            {"role": "tool", "name": "read_file", "content": "hello"},
        ]

    def reasonings(self):
        return [m.get("reasoning") for m in assistants(self.h._api_messages())]

    def test_turn_sends_only_the_current_turn(self):
        self.history()
        self.assertEqual(self.reasonings(), [None, "new thought"])
        self.assertFalse(any(m.get("role") == "thinking" for m in self.h._api_messages()))

    def test_all_and_off(self):
        self.history()
        self.h.think_history = "all"
        self.assertEqual(self.reasonings(), ["old thought", "new thought"])
        self.h.think_history = "off"
        self.assertEqual(self.reasonings(), [None, None])

    def test_stored_history_is_not_changed(self):
        self.history()
        self.h._api_messages()
        self.assertFalse(any("reasoning" in m for m in self.h.messages))

    def test_unsent_and_orphaned_reasoning_stay_out(self):
        self.h.messages += [
            {"role": "user", "content": "q"},
            {"role": "thinking", "content": "cut off", "unsent": True},
            {"role": "assistant", "content": "partial"},
            {"role": "thinking", "content": "no reply after this"},
            {"role": "user", "content": "Please respond."},
        ]
        self.h.think_history = "all"
        self.assertEqual(self.reasonings(), [None])

    def test_answer_from_thinking_is_not_sent_twice(self):
        self.h.messages += [{"role": "user", "content": "q"},
                            {"role": "thinking", "content": "the answer is 4"}]
        self.h._answer_from_thinking("the answer is 4")
        self.assertEqual(self.reasonings(), [None])

    def test_tool_loop_sends_reasoning_back(self):
        self.h.send("read a.txt")
        second = self.h.client.requests[1]
        call = assistants(second["messages"])[-1]
        self.assertEqual(call.get("reasoning"), "plan A")
        self.assertIs(second["preserve"], False)
        self.h.think_history = "off"
        self.h.client.requests.clear()
        self.h.send("again")
        self.assertIsNone(self.h.client.requests[0]["preserve"])
        self.assertFalse(any(m.get("reasoning") for m in self.h.client.requests[1]["messages"]))

    def test_command_saves_prefs_and_status(self):
        self.assertIn("since your last message", handle_command("/think-history", self.h).output)
        handle_command("/think-history all", self.h)
        self.assertEqual(session_mod.load_prefs()["think_history"], "all")
        self.assertEqual(self.h.status_event().think_history, "all")
        self.assertIn("ERROR", handle_command("/think-history some", self.h).output)

    def test_compaction_unsends_old_reasoning_first(self):
        self.h.think_history = "all"
        big = "x" * 40000
        self.h.messages += [
            {"role": "user", "content": "q1"},
            {"role": "thinking", "content": big},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        self.h._turn_user = self.h.messages[-1]
        self.h.context_limit = 12000
        changed, notice = self.h._compact(summarise=False)
        self.assertTrue(changed)
        self.assertIn("stopped sending back 1 earlier reasoning block", notice)
        self.assertIn("removed 0 messages", notice)       # nothing else had to go
        self.assertTrue(self.h.messages[-3]["unsent"])
        self.assertEqual(self.reasonings(), [None])


class Adapters(unittest.TestCase):
    MSGS = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "reasoning": "plan",
             "tool_calls": [{"function": {"name": "f", "arguments": {}}}]},
            {"role": "tool", "content": "r"},
            {"role": "assistant", "content": "done", "reasoning": "check"}]

    def test_llamacpp_reasoning_content(self):
        out = LlamaCppClient._to_openai_messages(self.MSGS)
        self.assertEqual([m.get("reasoning_content") for m in out], [None, "plan", None, "check"])
        self.assertFalse(any("reasoning" in m for m in out))

    def test_ollama_thinking(self):
        out = OllamaClient._wire_messages(self.MSGS)
        self.assertEqual([m.get("thinking") for m in out], [None, "plan", None, "check"])
        self.assertFalse(any("reasoning" in m for m in out))


if __name__ == "__main__":
    unittest.main()
