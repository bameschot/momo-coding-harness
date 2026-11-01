"""The coder and chat loop around the model (a review against pi and crush):
messages typed while a turn runs reach the model after the current step,
transient server errors are retried, a prompt the server rejects as too long is
compacted and sent again, and the round cap ends with the model's own summary.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness.controller import Controller
from harness.events import ChatEvent, ErrorEvent
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.llm.http import LLMHTTPError
from home_tmp import setUpModule, tearDownModule  # noqa: F401


def reply(text):
    return ChatResponse(content=text, done_reason="stop")


def read(path):
    return ChatResponse(content=f"Reading {path}.",
                        tool_calls=[ToolCall("read_file", {"path": path})], done_reason="stop")


class Scripted(LLMClient):
    """Each script entry is a ChatResponse, an exception to raise, or a callable
    run with the request (to act mid-request) that returns one of those."""
    provider_name = "fake"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.script, self.requests = [], []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools})
        step = self.script.pop(0) if self.script else reply("Done.")
        if callable(step) and not isinstance(step, ChatResponse):
            step = step()
        if isinstance(step, Exception):
            raise step
        return step

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self._patches = [
            mock.patch.object(hmod, "make_client", lambda *a, **k: Scripted("http://fake", "fake")),
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
            mock.patch.object(hmod, "_SERVER_RETRY_DELAYS", (0, 0, 0)),
        ]
        for p in self._patches:
            p.start()
        self.work = self.home / "work"
        self.work.mkdir()
        for i in range(10):
            (self.work / f"f{i}.txt").write_text(f"file {i}\n")
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.work)
        self.h.stream = False
        self.h.session_titles = False
        self.h.set_mode("coding")
        self.client = self.h.client
        self.events = self.h.event_queue.subscribe(replay=False)

    def tearDown(self):
        self.events.close()
        for p in self._patches:
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.events.get(timeout=0.05))
            except Exception:
                return out

    def users(self):
        return [m["content"] for m in self.h.messages if m.get("role") == "user"]


class Steering(Base):
    def test_a_message_typed_mid_turn_reaches_the_next_request(self):
        def second():
            self.h.steer("also check f2.txt")
            return read("f1.txt")
        self.client.script = [read("f0.txt"), second, reply("Both read.")]
        self.h.send("read f0.txt and f1.txt")
        third = self.client.requests[2]["messages"]
        self.assertEqual(third[-1], {"role": "user", "content": "also check f2.txt"})
        self.assertEqual(self.users(), ["read f0.txt and f1.txt", "also check f2.txt"])

    def test_a_message_typed_during_the_final_reply_runs_as_one_more_turn(self):
        c = Controller(self.h)
        def final():
            self.h.steer("and now f3.txt")
            return reply("Read f0.")
        self.client.script = [read("f0.txt"), final, reply("Read f3 too.")]
        c._busy = True
        self.h.send("read f0.txt")
        c._finish_bg(self.h.send)
        self.assertEqual(self.users(), ["read f0.txt", "and now f3.txt"])
        self.assertEqual(self.h.messages[-1]["content"], "Read f3 too.")
        self.assertFalse(c.busy)

    def test_typing_while_busy_queues_instead_of_refusing(self):
        c = Controller(self.h)
        c._busy = True
        c.submit("steer this way")
        self.assertEqual(self.h.take_steer(), ["steer this way"])
        texts = [e.text for e in self.drain() if isinstance(e, ChatEvent)]
        self.assertFalse(any("Busy" in t for t in texts))
        c._busy = False

    def test_interrupt_drops_the_queue_with_a_note(self):
        def second():
            self.h.steer("never mind")
            self.h._cancel.set()
            return read("f1.txt")
        self.client.script = [read("f0.txt"), second]
        self.h.send("go")
        self.assertEqual(self.h.take_steer(), [])
        texts = [e.text for e in self.drain() if isinstance(e, ChatEvent)]
        self.assertTrue(any("1 queued message not sent" in t for t in texts), texts)


class ServerErrors(Base):
    def test_a_transient_error_is_retried(self):
        self.client.script = [LLMHTTPError("busy", 503), LLMHTTPError("busy", 503), reply("ok")]
        self.h.send("hi")
        self.assertEqual(len(self.client.requests), 3)
        self.assertEqual([m["role"] for m in self.h.messages[1:]], ["user", "assistant"])
        texts = [e.text for e in self.drain() if isinstance(e, ChatEvent)]
        self.assertTrue(any("retrying in 0 s (2/3)" in t for t in texts), texts)

    def test_a_dropped_connection_is_retried(self):
        self.client.script = [LLMHTTPError("POST /v1/chat/completions: [Errno 61] Connection refused"),
                              reply("ok")]
        self.h.send("hi")
        self.assertEqual(self.h.messages[-1]["content"], "ok")

    def test_three_retries_then_the_error(self):
        self.client.script = [LLMHTTPError("busy", 503)] * 4
        self.h.send("hi")
        self.assertEqual(len(self.client.requests), 4)
        self.assertTrue(any(isinstance(e, ErrorEvent) for e in self.drain()))

    def test_a_client_error_is_not_retried(self):
        self.client.script = [LLMHTTPError("bad request: unknown field", 400)]
        self.h.send("hi")
        self.assertEqual(len(self.client.requests), 1)
        self.assertTrue(any(isinstance(e, ErrorEvent) for e in self.drain()))

    def test_escape_stops_the_wait(self):
        with mock.patch.object(hmod, "_SERVER_RETRY_DELAYS", (30,)):
            self.client.script = [LLMHTTPError("busy", 503)]
            threading.Timer(0.2, self.h._cancel.set).start()
            start = time.monotonic()
            self.h.send("hi")
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(len(self.client.requests), 1)

    def test_a_prompt_too_long_is_compacted_once_and_sent_again(self):
        calls = []
        def compact(summarise=True):
            calls.append(1)
            return True, "Context compacted: removed 4 messages."
        self.h._compact = compact
        overflow = LLMHTTPError("the request exceeds the available context size, try increasing it", 400)
        self.client.script = [overflow, reply("ok")]
        self.h.send("hi")
        self.assertEqual((len(calls), self.h.messages[-1]["content"]), (1, "ok"))

    def test_a_second_prompt_too_long_ends_the_turn(self):
        self.h._compact = lambda summarise=True: (True, "Context compacted.")
        overflow = LLMHTTPError("prompt is too long", 400)
        self.client.script = [overflow, overflow]
        self.h.send("hi")
        self.assertEqual(len(self.client.requests), 2)
        self.assertTrue(any(isinstance(e, ErrorEvent) for e in self.drain()))


class RoundCap(Base):
    def run_capped(self, wrap_up):
        self.client.script = [read(f"f{i}.txt") for i in range(3)] + [wrap_up]
        self.h._append_user("read everything")
        return self.h._run_loop(self.h._current_tools(), 3)

    def test_the_cap_ends_with_the_models_summary(self):
        outcome = self.run_capped(reply("Read f0-f2. Left: f3-f9. Next: read them in one call."))
        self.assertEqual(outcome, "limit")
        self.assertEqual(self.client.requests[-1]["tools"], [])
        self.assertEqual(self.h.messages[-1], {"role": "assistant",
                                               "content": "Read f0-f2. Left: f3-f9. Next: read them in one call."})
        events = self.drain()
        self.assertIn("Read f0-f2. Left: f3-f9. Next: read them in one call.",
                      [e.text for e in events if isinstance(e, ChatEvent) and e.role == "assistant"])
        self.assertFalse(any(isinstance(e, ErrorEvent) for e in events))

    def test_an_empty_summary_keeps_the_error(self):
        self.run_capped(reply(""))
        self.assertEqual(self.h.messages[-1]["role"], "tool")
        self.assertTrue(any(isinstance(e, ErrorEvent) for e in self.drain()))


if __name__ == "__main__":
    unittest.main()
