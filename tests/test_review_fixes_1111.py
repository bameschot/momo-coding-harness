"""Regressions for the 2026-11-11 review (fable-feature-review.md, section A).

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness.controller import Controller, transcript_events
from harness.events import ChatEvent, ErrorEvent, UserEvent
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.tools.builtin.read_file import read_file
from harness.tools import common as common_mod
from home_tmp import setUpModule, tearDownModule  # noqa: F401


class Scripted(LLMClient):
    provider_name = "fake"
    script: list = []
    prompt_tokens = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.requests = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.requests.append([dict(m) for m in messages])
        step = self.script[min(len(self.requests), len(self.script)) - 1]
        if isinstance(step, BaseException):
            raise step
        return step

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


def text(t, **kw):
    return ChatResponse(content=t, done_reason="stop", **kw)


ANNOUNCE = text("I read the files. My next step is to implement the changes in shop/pricing.py.")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(hmod, "make_client",
                                        lambda *a, **k: Scripted("http://fake", "fake"))
        self._patch.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=Path(self.tmp.name))
        self.h.stream = False
        self.h.set_mode("coding")

    def tearDown(self):
        self._patch.stop()
        self.h.logger.close()
        self.tmp.cleanup()


class NotesAreNotUserMessages(Base):
    """A1: a harness note is stored tagged, sent untagged, and never read as
    what the user typed."""

    def run_turn(self):
        Scripted.script = [ANNOUNCE, text("All done.")]
        self.h.send("change four things")

    def test_the_note_is_tagged_and_the_key_is_not_sent(self):
        self.run_turn()
        notes = [m for m in self.h.messages if hmod.is_note(m)]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0][hmod.NOTE_KEY])
        sent = self.h.client.requests[-1]
        self.assertFalse(any(hmod.NOTE_KEY in m for m in sent))
        self.assertTrue(any(m["content"] == notes[0]["content"] for m in sent))

    def test_retry_resends_the_users_message(self):
        self.run_turn()
        self.assertEqual(self.h.truncate_at_last_user(), "change four things")
        self.assertFalse(any(m.get("role") == "user" for m in self.h.messages))

    def test_the_replayed_transcript_shows_the_note_as_a_system_line(self):
        self.run_turn()
        events = transcript_events(self.h.messages)
        self.assertEqual([e.text for e in events if isinstance(e, UserEvent)],
                         ["change four things"])
        self.assertTrue(any(isinstance(e, ChatEvent) and e.role == "system"
                            and e.text.startswith(hmod.NOTE_PREFIX) for e in events))

    def test_a_note_from_an_old_session_is_recognised_by_its_prefix(self):
        self.assertTrue(hmod.is_note({"role": "user", "content": hmod.NOTE_PREFIX + " x"}))
        self.assertFalse(hmod.is_note({"role": "user", "content": "x"}))

    def test_a_message_merged_into_a_note_is_the_users(self):
        self.h.messages.append(hmod.note_message("note"))
        self.h._append_user("my words")
        self.assertFalse(hmod.is_note(self.h.messages[-1]))


class WorkerCrash(Base):
    """A2: a bug in a worker run is reported and logged, the dangling calls
    get results, and busy clears."""

    def test_crash_is_reported_and_calls_settled(self):
        sub = self.h.event_queue.subscribe(replay=False)
        c = Controller(self.h)
        call = ChatResponse(content="", tool_calls=[ToolCall("list_directory", {}),
                                                    ToolCall("list_directory", {"path": "x"})])
        Scripted.script = [call, text("done")]
        with mock.patch.object(hmod.Harness, "_annotate_result",
                               side_effect=RuntimeError("boom")):
            c.submit("look around")
            deadline = time.monotonic() + 5
            while c.busy and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertFalse(c.busy)
        errors = []
        while True:
            try:
                ev = sub.get_nowait()
            except queue.Empty:
                break
            if isinstance(ev, ErrorEvent):
                errors.append(ev.text)
        self.assertTrue(any("RuntimeError: boom" in e for e in errors), errors)
        i = next(k for k, m in enumerate(self.h.messages) if m.get("tool_calls"))
        self.assertEqual([m["role"] for m in self.h.messages[i + 1:]], ["tool", "tool"])
        self.assertIn('"type": "error"', self.h.logger.path.read_text())
        sub.close()


class LoadSessionResetsPlanState(Base):
    """A3."""

    def test_draft_and_queue_do_not_carry_over(self):
        Scripted.script = [text("hi")]
        self.h.send("hello")
        path = self.h.session_path()
        self.h._plan_draft = {"turns": 3}
        self.h._plan_asked = 2
        self.h._steer.append("queued")
        self.h.load_session(path)
        self.assertIsNone(self.h._plan_draft)
        self.assertEqual(self.h._plan_asked, 0)
        self.assertEqual(self.h._steer, [])
        self.assertEqual(self.h._plan_heuristics, {})


class InterruptAQuestion(Base):
    """A4: Esc while the model waits on ask_user ends the turn."""

    def test_cancel_while_waiting(self):
        c = Controller(self.h)
        Scripted.script = [ChatResponse(tool_calls=[ToolCall("ask_user", {"question": "Which?"})]),
                           text("should not be reached")]
        c.submit("do it")
        deadline = time.monotonic() + 5
        while not c.waiting and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(c.waiting)
        self.assertTrue(c.cancel())
        while c.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(c.busy)
        self.assertEqual(len(self.h.client.requests), 1)
        result = next(m for m in self.h.messages if m.get("role") == "tool")
        self.assertIn("interrupted", result["content"])

    def test_a_typed_ahead_answer_still_answers(self):
        self.h.provide_user_input("yes")
        self.assertEqual(self.h._ask_user("ok?"), "yes")

    def test_a_stale_cancel_does_not_answer_the_next_question(self):
        self.h._user_input_queue.put(hmod._CANCELLED)
        self.h.provide_user_input("blue")
        self.assertEqual(self.h._ask_user("colour?"), "blue")


class CompactionUsesTheServersCount(Base):
    """A5: the estimate's shortfall from the last measured reply scales the
    compaction limit and the read budget."""

    def test_ratio_is_learned_and_clamped(self):
        self.h._note_estimate_ratio(1000, 2500)
        self.assertEqual(self.h._est_ratio, 2.5)
        self.assertEqual(self.h._compact_limit(), int(self.h.context_limit / 2.5))
        self.h._note_estimate_ratio(1000, 500)          # never below the plain estimate
        self.assertEqual(self.h._est_ratio, 1.0)
        self.h._note_estimate_ratio(1000, 9000)
        self.assertEqual(self.h._est_ratio, 4.0)

    def test_the_read_budget_shrinks_with_it(self):
        before = self.h._read_budget()
        self.h._note_estimate_ratio(1000, 3000)
        self.assertLess(self.h._read_budget(), before)

    def test_a_reply_updates_it(self):
        Scripted.script = [text("hi", prompt_tokens=10 ** 6, eval_tokens=1)]
        self.h.send("hello")
        self.assertEqual(self.h._est_ratio, 4.0)

    def test_a_model_switch_resets_it(self):
        self.h._est_ratio = 3.0
        self.h.set_model("other")
        self.assertEqual(self.h._est_ratio, 1.0)


class HugeFiles(unittest.TestCase):
    """A6: a file over the limit is refused before it is read."""

    def test_refused_without_reading(self):
        with tempfile.TemporaryDirectory() as d:
            big = Path(d) / "big.log"
            with open(big, "wb") as f:                   # sparse: no disk used
                f.truncate(common_mod.MAX_READ_BYTES + 1)
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read")):
                out = read_file("big.log", workdir=Path(d))
        self.assertTrue(out.startswith("ERROR: big.log is"), out)
        self.assertIn("nothing was read", out)


class SmallFixes(unittest.TestCase):
    """A7."""

    def test_two_sessions_in_one_second_get_different_names(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(session_mod, "SESSION_DIR", Path(d)):
            first = session_mod.new_timestamp()
            (Path(d) / f"{first}.log").write_text("")
            second = session_mod.new_timestamp()
        if second[:19] == first[:19]:                    # same second
            self.assertNotEqual(first, second)
            self.assertTrue(session_mod._NAME_RE.match(second))

    def test_mark_current_and_save_prefs_do_not_lose_each_other(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(session_mod, "_PREFS_PATH", Path(d) / "prefs.json"):
            def marks():
                for i in range(30):
                    session_mod.mark_current(f"s{i}", Path(d) / f"w{i}")
            t = threading.Thread(target=marks)
            t.start()
            for i in range(30):
                session_mod.save_prefs(**{f"k{i}": i})
            t.join()
            prefs = session_mod.load_prefs()
        self.assertEqual(len(prefs["workspace_sessions"]), 30)
        self.assertTrue(all(prefs.get(f"k{i}") == i for i in range(30)))

    def test_write_file_over_crlf_with_lf_is_a_change(self):
        from harness import editing
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.txt").write_bytes(b"one\r\ntwo\r\n")
            out = editing.write_file("a.txt", "one\ntwo\n", workdir=Path(d))
            self.assertFalse(out.startswith("No change"), out)
            self.assertEqual((Path(d) / "a.txt").read_bytes(), b"one\ntwo\n")


if __name__ == "__main__":
    unittest.main()
