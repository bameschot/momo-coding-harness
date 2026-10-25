"""/cavecat's think_trim: the reasoning /think-history sends back is cut to its
plan steps and closing decision (harness/think_trim.py); the transcript keeps it all.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import unittest

from harness import think_trim
from harness.llm.base import ChatResponse, ToolCall
from test_review_fixes import HarnessCase

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

    def test_short_or_single_paragraph_reasoning_is_sent_as_it_is(self):
        self.assertEqual(think_trim.trim("Read util.py next.", CALL), "Read util.py next.")
        self.assertEqual(think_trim.trim(DELIBERATION, CALL), DELIBERATION)

    def test_mostly_decision_is_sent_as_it_is(self):
        text = reasoning("Short intro.", DELIBERATION)      # the decision is nearly all of it
        self.assertEqual(think_trim.trim(text, CALL), text)

    def test_stable(self):
        text = reasoning(DELIBERATION, DELIBERATION, "Edit it.")
        self.assertEqual(think_trim.trim(text, CALL), think_trim.trim(text, dict(CALL)))


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

    def test_cavecat_on_sends_the_trim_and_keeps_the_transcript(self):
        sent = self.second_request_reasoning(cavecat="on")
        self.assertTrue(sent.startswith("[earlier reasoning not sent"), sent)
        self.assertTrue(sent.endswith("List the directory next."))
        stored = [m["content"] for m in self.h.messages if m["role"] == "thinking"]
        self.assertEqual(stored, [self.LONG])

    def test_off_or_forced_off_sends_it_whole(self):
        self.assertEqual(self.second_request_reasoning(), self.LONG)

    def test_an_eval_can_force_it_off(self):
        self.assertEqual(self.second_request_reasoning(cavecat="on", think_trim=False), self.LONG)

    def test_think_history_off_sends_nothing(self):
        self.assertIsNone(self.second_request_reasoning(cavecat="on", think_history="off"))

    def test_the_context_meter_counts_what_is_sent(self):
        self.second_request_reasoning(cavecat="on")
        cats = self.h._context_categories()
        self.assertLess(cats["reasoning"], cats["thinking"])


if __name__ == "__main__":
    unittest.main()
