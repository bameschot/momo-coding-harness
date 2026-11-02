"""How a model reply is read (a review of the output processing): a tool call
mentioned in an answer is not run, the narration around a call written as text
is kept, offers are not "next steps", reasoning that only closes </think> is
not shown as the answer, and a write without a path is not sent to a made-up
file outside design mode.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness import tools
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.toolcall_text import (_derive_write_path, _extract_and_strip_thinking,
                                   _extract_text_tool_calls, announces_next_step,
                                   recover_text_calls, template_leak)
from home_tmp import setUpModule, tearDownModule  # noqa: F401

GOLDEN = Path(__file__).parent / "golden" / "next_step_replies.json"


def names(calls):
    return [(c["name"], c["arguments"]) for c in calls]


class Recovery(unittest.TestCase):
    def test_a_call_in_a_code_example_is_not_recovered(self):
        text = ("Your loader works like this:\n```python\ndata = read_file('config.json')\n```\n"
                "It reads the config once at startup.")
        self.assertEqual(recover_text_calls(text, tools.CHAT_TOOLS), ([], text))

    def test_a_call_mentioned_in_a_summary_is_not_run(self):
        text = "Done. The helper is used as `write_file('out.txt', 'hello')` in main.py."
        self.assertEqual(_extract_text_tool_calls(text, tools.ALL_TOOLS), [])

    def test_gemma_print_call_at_the_end_is_recovered_and_the_narration_kept(self):
        text = ("I'll write the file now.\n```tool_code\n"
                "print(write_file(path='a.py', content='x = 1\\n'))\n```")
        calls, rest = recover_text_calls(text, tools.ALL_TOOLS)
        self.assertEqual(names(calls), [("write_file", {"path": "a.py", "content": "x = 1\n"})])
        self.assertEqual(rest, "I'll write the file now.")

    def test_two_calls_at_the_end_are_both_recovered(self):
        text = "Reading both.\nread_file(path='a.py')\nread_file(path='b.py')"
        calls, rest = recover_text_calls(text, tools.ALL_TOOLS)
        self.assertEqual(names(calls), [("read_file", {"path": "a.py"}), ("read_file", {"path": "b.py"})])
        self.assertEqual(rest, "Reading both.")

    def test_a_call_inside_a_string_argument_is_part_of_it(self):
        text = ("Let me edit.\nedit_file(path='m.py', old_string='return read_file(x)', "
                "new_string='return 1')")
        calls, _ = recover_text_calls(text, tools.ALL_TOOLS)
        self.assertEqual([c["name"] for c in calls], ["edit_file"])

    def test_a_dict_argument_is_the_arguments_object(self):
        calls, _ = recover_text_calls('write_file({"path": "n.md", "content": "# T"})', tools.ALL_TOOLS)
        self.assertEqual(names(calls), [("write_file", {"path": "n.md", "content": "# T"})])

    def test_tagged_formats_are_recovered_anywhere(self):
        text = 'before <tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call> after'
        calls, rest = recover_text_calls(text, tools.ALL_TOOLS)
        self.assertEqual(names(calls), [("read_file", {"path": "a.py"})])
        self.assertEqual(rest, "before  after")

    def test_a_json_call_in_a_fence_at_the_end(self):
        text = 'Calling it.\n```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```'
        calls, rest = recover_text_calls(text, tools.ALL_TOOLS)
        self.assertEqual(names(calls), [("read_file", {"path": "a.py"})])
        self.assertEqual(rest, "Calling it.")


STORY_MUD = json.loads((Path(__file__).parent / "golden" / "story_mud_text_calls.json")
                       .read_text(encoding="utf-8"))


class SloppyToolCallJson(unittest.TestCase):
    """story-mud run 2: P2.S2 failed after writing its call as <tool_call> JSON
    four times, none of it strict JSON."""

    def test_the_four_story_mud_replies_are_recovered(self):
        got = [[c["name"] for c in recover_text_calls(r, tools.ALL_TOOLS)[0]]
               for r in STORY_MUD["text_calls"]]
        self.assertEqual(got, [["write_file", "run_command"], ["write_file", "run_command"],
                               ["run_command"], ["write_file"]])
        for reply in STORY_MUD["text_calls"]:
            calls, rest = recover_text_calls(reply, tools.ALL_TOOLS)
            self.assertNotIn("<tool_call", rest)
            for c in calls:
                if c["name"] == "write_file":
                    self.assertEqual(c["arguments"]["path"], "world.json")
                    self.assertEqual(len(json.loads(c["arguments"]["content"])), 6)

    def test_each_slip(self):
        cases = {
            "a raw newline in a string":
                '<tool_call>{"name": "write_file", "arguments": {"path": "a", "content": "x\ny"}}</tool_call>',
            "a backslash before a line break":
                '<tool_call>{"name": "write_file", "arguments": {"path": "a", "content": "x\\\ny"}}</tool_call>',
            "a dropped closing brace":
                '<tool_call>{"name": "write_file", "arguments": {"path": "a", "content": "x\\ny"}</tool_call>',
        }
        for why, text in cases.items():
            with self.subTest(why):
                self.assertEqual(names(recover_text_calls(text, tools.ALL_TOOLS)[0]),
                                 [("write_file", {"path": "a", "content": "x\ny"})])

    def test_a_nameless_payload_goes_to_the_one_tool_it_fits(self):
        calls, _ = recover_text_calls('<tool_call>{"command": "ls"}<tool_call|>', tools.ALL_TOOLS)
        self.assertEqual(names(calls), [("run_command", {"command": "ls"})])
        # {"path": …} fits read_file, list_directory, … — no guess.
        self.assertEqual(recover_text_calls('<tool_call>{"path": "a.py"}</tool_call>',
                                            tools.ALL_TOOLS)[0], [])

    def test_calls_in_call_tags(self):
        # run 3's planner: scratch_write, then create_plan, as <call>{…}</call> text
        scratch, plan = STORY_MUD["call_tag_calls"][:2]
        calls, rest = recover_text_calls(scratch, tools.ALL_TOOLS)
        self.assertEqual([c["name"] for c in calls], ["scratch_write"])
        self.assertNotIn("<call>", rest)
        calls, _ = recover_text_calls(plan, tools.PLAN_INVESTIGATE_TOOLS)
        self.assertEqual([c["name"] for c in calls], ["create_plan"])
        self.assertEqual(len(calls[0]["arguments"]["requirements"]), 6)

    def test_unreadable_stays_unread(self):
        text = '<tool_call>{"name": "write_file", "arguments": {"path": "a", content: x}}</tool_call>'
        self.assertEqual(recover_text_calls(text, tools.ALL_TOOLS)[0], [])


class TemplateLeak(unittest.TestCase):
    def test_the_story_mud_leaked_calls_are_refused(self):
        for c in STORY_MUD["leaked_calls"]:
            self.assertIn("chat-template tokens", template_leak(c["name"], c["args"]))

    def test_file_text_may_quote_the_tokens(self):
        self.assertEqual(template_leak("write_file", {"path": "notes.md",
                                                      "content": "gemma ends a call with <tool_call|>"}), "")
        self.assertEqual(template_leak("read_file", {"path": "a.py"}), "")


class NextStep(unittest.TestCase):
    def test_offers_and_deferrals_are_not_next_steps(self):
        for text in ("Updated a.py and b.py; tests pass. Let me know if you want more, and I'll add them.",
                     "All done. If you'd like, I'll also add a test for the empty case.",
                     "Fixed the bug in parse(). Next time, let's add a regression test.",
                     "Done: renamed Foo to Bar in 3 files. I'll leave the docs as they are."):
            with self.subTest(text=text):
                self.assertFalse(announces_next_step(text))

    def test_announcements_still_are(self):
        for text in ("Let me read config.py to see how the port is set.",
                     "I read the files. My next step is to implement the changes in shop/pricing.py.",
                     "I will now proceed to modify `shop/shipping.py`."):
            with self.subTest(text=text):
                self.assertTrue(announces_next_step(text))

    def test_the_labelled_bench_replies_are_unchanged(self):
        for r in json.loads(GOLDEN.read_text())["replies"]:
            self.assertEqual(announces_next_step(r["reply_tail"]), r["early_stop"], r["reply_tail"][-120:])


class Thinking(unittest.TestCase):
    def test_only_a_closing_tag(self):
        self.assertEqual(_extract_and_strip_thinking("I should read a.py.\n</think>\nHere it is."),
                         ("Here it is.", "I should read a.py."))

    def test_every_block_is_kept(self):
        self.assertEqual(_extract_and_strip_thinking("<think>one</think>A<think>two</think>B"),
                         ("AB", "one\n\ntwo"))

    def test_cut_off_and_plain(self):
        self.assertEqual(_extract_and_strip_thinking("<think>cut"), ("", "cut"))
        self.assertEqual(_extract_and_strip_thinking("plain"), ("plain", ""))


class WritePath(unittest.TestCase):
    def test_inferred_only_for_documents_or_design_mode(self):
        self.assertEqual(_derive_write_path("# Task API\n\nText.", "coding"), "task-api.md")
        self.assertEqual(_derive_write_path("x = 1\n", "design"), "design.md")
        self.assertIsNone(_derive_write_path("x = 1\n", "coding"))
        self.assertIsNone(_derive_write_path("# Helpers\nimport os\n", "coding"))


class Scripted(LLMClient):
    provider_name = "fake"
    replies: list = []

    def chat(self, messages, tools_, think=None, num_ctx=None, on_delta=None):
        return self.replies.pop(0) if self.replies else ChatResponse(content="Done.", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class InTheLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.work = self.home / "work"
        self.work.mkdir()
        self._patches = [
            mock.patch.object(hmod, "make_client", lambda *a, **k: Scripted("http://fake", "fake")),
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
        ]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.work)
        self.h.stream = False
        self.h.set_mode("coding")

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def test_an_answer_that_mentions_a_call_stays_the_answer(self):
        answer = "Done. The helper is used as `write_file('out.txt', 'hello')` in main.py."
        Scripted.replies = [ChatResponse(content=answer, done_reason="stop")]
        self.h.send("explain")
        self.assertFalse((self.work / "out.txt").exists())
        self.assertEqual(self.h.messages[-1], {"role": "assistant", "content": answer})

    def test_the_narration_before_a_text_call_is_stored(self):
        (self.work / "a.py").write_text("x = 1\n")
        Scripted.replies = [ChatResponse(content="Reading a.py first.\nread_file(path='a.py')",
                                         done_reason="stop")]
        self.h.send("look at a.py")
        call = next(m for m in self.h.messages if m.get("tool_calls"))
        self.assertEqual(call["content"], "Reading a.py first.")
        self.assertEqual(call["tool_calls"][0]["function"]["name"], "read_file")

    def test_a_write_without_path_in_coding_mode_is_an_error_not_a_file(self):
        Scripted.replies = [ChatResponse(tool_calls=[ToolCall("write_file", {"content": "x = 1\n"})],
                                         done_reason="stop")]
        self.h.send("write it")
        result = next(m for m in self.h.messages if m.get("role") == "tool")
        self.assertIn("path", result["content"])
        self.assertTrue(result["content"].startswith("ERROR"), result["content"])
        self.assertEqual([p.name for p in self.work.iterdir() if p.is_file()], [])


if __name__ == "__main__":
    unittest.main()
