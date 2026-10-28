"""/think-keep's trim: the reasoning /think-history sends back is cut to its
plan steps and closing sentences, within a budget that grows with √length and
the /think-keep level (harness/think_trim.py); the transcript keeps it all.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import unittest

from harness import session as session_mod
from harness import think_trim
from harness.commands import handle as handle_command
from harness.llm.base import ChatResponse, ToolCall
from test_review_fixes import HarnessCase
from home_tmp import setUpModule, tearDownModule  # noqa: F401

DELIBERATION = ("The parser lives in shop/util.py. It splits on letters, but the tests in "
                "tests/test_shop.py also pass '1h-5m', which must fail. ") * 6
CALL = {"role": "assistant", "content": "",
        "tool_calls": [{"function": {"name": "edit_file", "arguments": {
            "path": "shop/util.py", "old_string": "x",
            "new_string": "def parse_duration(text):\n    total = 0\n    for value, unit in PARTS:\n"
                          "        total += int(value) * UNITS[unit]\n    return total"}}}]}


def reasoning(*paras):
    return "\n\n".join(paras)


class Trim(unittest.TestCase):
    def test_keeps_the_decision_and_says_what_was_left_out(self):
        text = reasoning(DELIBERATION, DELIBERATION, "So: reject a minus sign, then edit parse_duration.")
        out = think_trim.trim(text, CALL)
        self.assertTrue(out.startswith("[earlier reasoning not sent: "), out)
        self.assertTrue(out.endswith("So: reject a minus sign, then edit parse_duration."))
        self.assertLess(len(out), len(text) / 3)

    def test_plan_steps_are_kept(self):
        text = reasoning(DELIBERATION, "Plan:\n1. read util.py\n2. add the check\n- run the tests",
                         DELIBERATION, "Now step 2.")
        out = think_trim.trim(text, CALL)
        self.assertIn("plan steps kept", out)
        for step in ("1. read util.py", "2. add the check", "- run the tests"):
            self.assertIn(step, out)

    def test_a_closing_draft_the_call_repeats_points_at_the_call(self):
        draft = ("```python\ndef parse_duration(text):\n    total = 0\n    for value, unit in PARTS:\n"
                 "        total += int(value) * UNITS[unit]\n    return total\n```")
        out = think_trim.trim(reasoning(DELIBERATION, "I'll write it:", draft), CALL)
        self.assertIn("I'll write it:", out)
        self.assertIn("[repeated in the edit_file call below]", out)
        self.assertNotIn("total += int(value)", out)

    def test_a_draft_that_differs_from_the_call_is_kept(self):
        draft = "```python\ndef other():\n    return compute_something_else()\n    # more lines\n```"
        out = think_trim.trim(reasoning(DELIBERATION, "Draft:", draft), CALL)
        self.assertIn("compute_something_else", out)

    def test_short_reasoning_is_sent_as_it_is(self):
        self.assertEqual(think_trim.trim("Read util.py next.", CALL), "Read util.py next.")
        short = "The parser is in shop/util.py and splits on letters. " * 7     # ~370 chars
        self.assertEqual(think_trim.trim(short, CALL), short)

    def test_the_floor(self):
        text = "The parser is in shop/util.py and splits on letters. " * 9 + "Now edit it."
        out = think_trim.trim(text, CALL)
        self.assertTrue(out == text or len(out) >= think_trim.FLOOR, out)
        self.assertEqual(think_trim.budget(10), think_trim.FLOOR)

    def test_longer_reasoning_keeps_more_but_a_smaller_share(self):
        short = reasoning(*[DELIBERATION] * 2, "Edit it.")
        long = reasoning(*[DELIBERATION] * 12, "Edit it.")
        a, b = think_trim.trim(short, CALL), think_trim.trim(long, CALL)
        self.assertGreater(len(b), len(a))
        self.assertLess(len(b) / len(long), len(a) / len(short))

    def test_the_levels_keep_more_in_turn(self):
        text = reasoning(*[DELIBERATION] * 12, "Edit it.")
        lean, more, most = (think_trim.trim(text, CALL, k) for k in ("lean", "more", "most"))
        self.assertLess(len(lean), len(more))
        self.assertLess(len(more), len(most))
        for keep, out in (("lean", lean), ("more", more), ("most", most)):
            kept = out.split("\n\n", 1)[1]
            self.assertLessEqual(len(kept), think_trim.budget(len(text), keep), keep)

    def test_prose_is_cut_at_sentences_and_ends_with_the_last(self):
        text = "Short intro.\n\n" + DELIBERATION * 3 + "So edit it."     # one long closing paragraph
        out = think_trim.trim(text, CALL)
        kept = out.split("\n\n", 1)[1]
        self.assertTrue(kept.endswith("So edit it."))
        self.assertTrue(kept[0].isupper(), kept)                    # starts at a sentence
        self.assertNotIn("Short intro.", kept)

    def test_one_paragraph_keeps_its_last_sentences(self):
        text = DELIBERATION * 2 + "Then I reject the minus. Now edit parse_duration."
        out = think_trim.trim(text, CALL)
        self.assertTrue(out.startswith("[earlier reasoning not sent: "), out)
        self.assertTrue(out.endswith("Then I reject the minus. Now edit parse_duration."), out)
        self.assertLessEqual(len(out.split("\n\n", 1)[1]), think_trim.budget(len(text)))
        listed = "1. read util.py\n2. add the check\n" + DELIBERATION.strip() * 2
        self.assertIn("2. add the check", think_trim.trim(listed, CALL))

    def test_a_last_sentence_over_the_budget_keeps_its_end(self):
        text = "word " * 400 + "end."
        out = think_trim.trim(text, CALL)
        self.assertTrue(out.endswith("end."), out[-50:])
        self.assertIn("…", out)

    def test_code_is_never_cut(self):
        code = "```python\n" + "\n".join(f"step_{i}(x)  # keep" for i in range(40)) + "\n```"
        self.assertEqual(think_trim.trim(code, CALL), code)    # nothing but code
        out = think_trim.trim(reasoning(DELIBERATION, "Write:", code), CALL)
        self.assertIn("step_39(x)  # keep", out)
        self.assertIn("step_0(x)  # keep", out)

    def test_stable(self):
        text = reasoning(DELIBERATION, DELIBERATION, "Edit it.")
        for keep in think_trim.KEEP_LEVELS:
            self.assertEqual(think_trim.trim(text, CALL, keep), think_trim.trim(text, dict(CALL), keep))


class Harness(HarnessCase):
    LONG = reasoning(DELIBERATION, DELIBERATION, "List the directory next.")

    def second_request_reasoning(self, **settings):
        self.h.set_mode("coding")
        self.h.think_history = "turn"
        for k, v in settings.items():
            setattr(self.h, k, v)
        self.client.replies = [
            ChatResponse(thinking=self.LONG, tool_calls=[ToolCall("list_directory", {})]),
            ChatResponse(content="done")]
        self.h.send("look around")
        sent = [m.get("reasoning") for m in self.client.calls[1] if m.get("role") == "assistant"]
        return sent[-1]

    def test_sends_it_whole_by_default(self):
        self.assertEqual(self.h.think_keep, "off")
        self.assertEqual(self.second_request_reasoning(), self.LONG)

    def test_lean_sends_the_trim_and_keeps_the_transcript(self):
        sent = self.second_request_reasoning(think_keep="lean")
        self.assertTrue(sent.startswith("[earlier reasoning not sent"), sent)
        self.assertTrue(sent.endswith("List the directory next."))
        stored = [m["content"] for m in self.h.messages if m["role"] == "thinking"]
        self.assertEqual(stored, [self.LONG])

    def test_cavecat_does_not_change_it(self):
        off = self.second_request_reasoning()
        self.h.cavecat = "on"
        self.assertEqual(next(iter(self.h._sent_reasoning().values())), off)

    def test_off_sends_it_whole(self):
        self.assertEqual(self.second_request_reasoning(think_keep="off", cavecat="on"), self.LONG)

    def test_think_history_off_sends_nothing(self):
        self.assertIsNone(self.second_request_reasoning(think_history="off"))

    def test_think_keep_most_sends_more_and_is_not_served_from_a_lean_trim(self):
        lean = self.second_request_reasoning(think_keep="lean")
        self.h.think_keep = "most"
        most = self.h._sent_reasoning()
        self.assertGreater(len(next(iter(most.values()))), len(lean))

    def test_command_saves_prefs_and_status(self):
        self.assertIn("sent back whole", handle_command("/think-keep", self.h).output)
        self.assertIn("re-reads", handle_command("/think-keep more", self.h).output)
        self.assertEqual(self.h.think_keep, "more")
        self.assertEqual(session_mod.load_prefs()["think_keep"], "more")
        self.assertEqual(self.h.status_event().think_keep, "more")
        self.assertIn("sent back whole", handle_command("/think-keep off", self.h).output)
        self.assertEqual(session_mod.load_prefs()["think_keep"], "off")
        self.assertIn("ERROR", handle_command("/think-keep all", self.h).output)
        self.assertEqual(self.h.think_keep, "off")
        self.h.think_history = "off"
        self.assertIn("applies with /think-history", handle_command("/think-keep", self.h).output)

    def test_the_context_meter_counts_what_is_sent(self):
        self.second_request_reasoning(think_keep="lean")
        cats = self.h._context_categories()
        self.assertLess(cats["reasoning"], cats["thinking"])


if __name__ == "__main__":
    unittest.main()
