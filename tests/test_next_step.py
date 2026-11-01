"""A final reply that only announces its next step ("My next step is to implement
the changes in shop/pricing.py.") gets one note in coding mode instead of ending
the turn.  The detector is checked against the final replies of round 1 of
evals/multipart_bench.py (then todos_bench.py), labelled by whether the run had
stopped early.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.toolcall_text import announces_next_step
from home_tmp import setUpModule, tearDownModule  # noqa: F401

FIXTURE = Path(__file__).parent / "golden" / "next_step_replies.json"


class Detector(unittest.TestCase):
    def test_round_one_replies(self):
        replies = json.loads(FIXTURE.read_text())["replies"]
        for r in replies:
            with self.subTest(task=r["task"], arm=r["arm"], tail=r["reply_tail"][-80:]):
                self.assertEqual(announces_next_step(r["reply_tail"]), r["early_stop"])

    def test_announcements(self):
        for text in ("I will now update shop/pricing.py.",
                     "Plan:\n1. Add the uk zone.\n2. Fix slug.\n3. Run the tests.",
                     "Changes so far: pricing.py.\n\nMy next steps are: shipping and the tests.",
                     "I'll start with the cart.",
                     # Round 2 (2026-10-31): a bold file label before the verb, and "Let's".
                     "Plan:\n4.  **`shop/shipping.py`**: Add the uk zone.\n"
                     "5.  **`shop/util.py`**: Update the `slug` function to strip characters.",
                     "Let's check `tests/test_shop.py` to see how items are used."):
            with self.subTest(text=text):
                self.assertTrue(announces_next_step(text))

    def test_not_announcements(self):
        for text in ("", "Should I also update the README?",
                     "Done. Let me know if you want more changes.",
                     "1. Updated `shop/pricing.py`.\n2. Added `Inventory.remove`.\nThe tests pass.",
                     "1. **`shop/pricing.py`**: Added `be` and `fr`.\n2. **`shop/util.py`**: Updated `slug`.",
                     "All four changes are in and the tests pass. Next steps for you: deploy it.",
                     "I have updated shop/pricing.py. All tests pass."):
            with self.subTest(text=text):
                self.assertFalse(announces_next_step(text))


class Scripted(LLMClient):
    provider_name = "fake"
    script: list = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.requests = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.requests.append([dict(m) for m in messages])
        return self.script[min(len(self.requests), len(self.script)) - 1]

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


def text(t):
    return ChatResponse(content=t, done_reason="stop")


ANNOUNCE = text("I read the files. My next step is to implement the changes in shop/pricing.py.")


class InTheLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "a.txt").write_text("hello\n")
        self._patch = mock.patch.object(hmod, "make_client",
                                        lambda *a, **k: Scripted("http://fake", "fake"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def run_script(self, script, mode="coding"):
        Scripted.script = script
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=Path(self.tmp.name))
        self.h.set_mode(mode)
        self.h.stream = False
        self.h.send("change four things")
        return self.h.messages[self.h.messages.index(self.h._turn_user):]

    def notes(self, turn):
        return [m for m in turn if m["role"] == "user" and "describes a next step" in (m["content"] or "")]

    def test_one_note_then_the_turn_goes_on(self):
        turn = self.run_script([ANNOUNCE, text("All changes made and the tests pass.")])
        self.assertEqual(len(self.notes(turn)), 1)
        self.assertTrue(self.notes(turn)[0]["content"].startswith("[Note from the harness, not the user]"))
        self.assertEqual(turn[-1]["content"], "All changes made and the tests pass.")
        self.assertEqual(len(self.h.client.requests), 2)

    def test_at_most_two_notes_per_turn(self):
        self.run_script([ANNOUNCE])          # announces every time
        self.assertEqual(len(self.h.client.requests), 3)

    def test_a_finished_summary_ends_the_turn(self):
        self.run_script([text("1. Updated `a.txt`.\nThe tests pass.")])
        self.assertEqual(len(self.h.client.requests), 1)

    def test_other_modes_are_not_checked(self):
        for mode in ("chat", "design", "plan"):
            with self.subTest(mode=mode):
                turn = self.run_script([ANNOUNCE, text("ok")], mode=mode)
                self.assertEqual(self.notes(turn), [])
                self.h.logger.close()

    def test_a_tool_call_after_the_note_continues_the_work(self):
        turn = self.run_script([ANNOUNCE,
                                ChatResponse(tool_calls=[ToolCall("read_file", {"path": "a.txt"})],
                                             done_reason="stop"),
                                text("Read it; nothing to change.")])
        self.assertIn("read_file", [c["function"]["name"] for m in turn if m.get("tool_calls")
                                    for c in m["tool_calls"]])
        self.assertEqual(turn[-1]["content"], "Read it; nothing to change.")


if __name__ == "__main__":
    unittest.main()
