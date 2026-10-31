"""Settled results: a tool call the harness does not run (cut off mid-call by the
output limit, arguments that are not valid JSON, the user interrupting the
batch) still gets an answer, and the answer says why and what a retry should
change, instead of "missing required argument" or a run on default arguments.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import settle
from harness.llm.base import ChatResponse, LLMClient, ToolCall, parse_tool_args
from harness.llm.llamacpp_client import LlamaCppClient
from harness.llm.ollama_client import OllamaClient
from harness.toolcall_text import partial_text_call
from home_tmp import setUpModule, tearDownModule  # noqa: F401

CUT = '{"path": "big.py", "content": "def f():\\n    return 1\\n'   # no closing quote or brace


def _cut_write(thinking=""):
    """A write_file whose arguments the output limit cut off."""
    args, err = parse_tool_args(CUT)
    return ChatResponse(thinking=thinking, done_reason="length", eval_tokens=1000,
                        tool_calls=[ToolCall("write_file", args, err)])


def _call(name, **args):
    return ChatResponse(tool_calls=[ToolCall(name, args)], done_reason="stop")


DONE = ChatResponse(content="Done.", done_reason="stop")


class ScriptedClient(LLMClient):
    """Plays `script` in order (the last reply repeats) and records each request."""
    provider_name = "fake"
    script: list = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.requests = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.requests.append({"messages": [dict(m) for m in messages], "think": think})
        return self.script[min(len(self.requests), len(self.script)) - 1]

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class Adapters(unittest.TestCase):
    def test_parse_tool_args(self):
        self.assertEqual(parse_tool_args('{"a": 1}'), ({"a": 1}, ""))
        self.assertEqual(parse_tool_args(""), ({}, ""))
        args, err = parse_tool_args(CUT)
        self.assertEqual(args, {})
        self.assertTrue(err)
        self.assertEqual(parse_tool_args("[1]")[1], "expected a JSON object, got list")

    def test_llamacpp_keeps_the_parse_error(self):
        data = {"choices": [{"finish_reason": "length", "message": {"tool_calls": [
            {"function": {"name": "write_file", "arguments": CUT}}]}}]}
        call = LlamaCppClient._normalize(data).tool_calls[0]
        self.assertEqual(call.arguments, {})
        self.assertTrue(call.args_error)

    def test_llamacpp_stream_keeps_the_parse_error(self):
        chunks = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
                      "name": "write_file", "arguments": CUT[:20]}}]}}]},
                  {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
                      "arguments": CUT[20:]}}]}, "finish_reason": "length"}]}]
        lines = [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]
        resp = LlamaCppClient._consume_stream(lines, lambda *_: None)
        self.assertEqual(resp.done_reason, "length")
        self.assertEqual(resp.tool_calls[0].arguments, {})
        self.assertTrue(resp.tool_calls[0].args_error)

    def test_llamacpp_complete_arguments_have_no_error(self):
        data = {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "read_file", "arguments": '{"path": "a"}'}}]}}]}
        call = LlamaCppClient._normalize(data).tool_calls[0]
        self.assertEqual((call.arguments, call.args_error), ({"path": "a"}, ""))

    def test_ollama_string_arguments_keep_the_parse_error(self):
        call = OllamaClient._convert_calls([{"function": {"name": "write_file", "arguments": CUT}}])[0]
        self.assertEqual(call.arguments, {})
        self.assertTrue(call.args_error)


class Wording(unittest.TestCase):
    def test_cause_names_the_cap_only_when_the_reply_used_it(self):
        self.assertIn("1,000-token output limit", settle.cut_off_cause(1000, 990))
        self.assertEqual(settle.cut_off_cause(1000, 400), "the context window filled up")
        self.assertEqual(settle.cut_off_cause(0, 5000), "the context window filled up")

    def test_a_cut_off_write_is_split_with_append_to_file_when_offered(self):
        text = settle.settled_result("truncated", "write_file", {"write_file", "append_to_file"},
                                     cause="the context window filled up")
        self.assertTrue(text.startswith("ERROR: not run"))
        self.assertIn("append_to_file", text)
        alone = settle.settled_result("truncated", "write_file", {"write_file"}, cause="x")
        self.assertNotIn("append_to_file", alone)

    def test_a_cut_off_edit_asks_for_smaller_edits(self):
        self.assertIn("smaller edits", settle.retry_advice("edit_file", ()))


class PartialTextCall(unittest.TestCase):
    tools = [{"function": {"name": n, "parameters": {"properties": {}}}}
             for n in ("write_file", "read_file")]

    def test_an_unclosed_tag(self):
        text = 'Writing it now.\n<tool_call>\n{"name": "write_file", "arguments": {"path": "a.py", "content": "x'
        name, start = partial_text_call(text, self.tools)
        self.assertEqual(name, "write_file")
        self.assertEqual(text[:start].rstrip(), "Writing it now.")

    def test_a_tag_that_names_no_tool_yet(self):
        self.assertEqual(partial_text_call("<tool_call>\n{", self.tools)[0], "a tool")

    def test_bare_json_and_python_call(self):
        self.assertEqual(partial_text_call('{"name": "read_file", "arguments": {"pa', self.tools)[0],
                         "read_file")
        self.assertEqual(partial_text_call("write_file(path='a.py', content='x", self.tools)[0],
                         "write_file")

    def test_prose_is_not_a_call(self):
        self.assertIsNone(partial_text_call("I read the file and it looks fine. The next", self.tools))
        self.assertIsNone(partial_text_call("Call read_file(path) to see it.", self.tools))


class Loop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)
        (self.wd / "a.txt").write_text("hello\n")
        self._patch = mock.patch.object(hmod, "make_client",
                                        lambda *a, **k: ScriptedClient("http://fake", "fake"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def run_script(self, script, mode="coding"):
        ScriptedClient.script = script
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode(mode)
        self.h.stream = False
        self.h.send("go")
        return self.h.messages[self.h.messages.index(self.h._turn_user):]

    def results(self, turn):
        return [m["content"] for m in turn if m["role"] == "tool"]

    def test_a_cut_off_write_is_not_run_and_says_to_split(self):
        turn = self.run_script([_cut_write(thinking="plan the file"), DONE])
        self.assertFalse((self.wd / "big.py").exists())
        [result] = self.results(turn)
        self.assertTrue(result.startswith("ERROR: not run"))
        self.assertIn("append_to_file", result)
        self.assertNotIn("required argument", result)
        self.assertFalse(self.h.client.requests[1]["think"])     # thinking shared the budget
        self.assertEqual(turn[-1]["content"], "Done.")

    def test_malformed_arguments_are_not_run_on_defaults(self):
        args, err = parse_tool_args('{"path": ".",}')
        bad = ChatResponse(done_reason="stop", tool_calls=[ToolCall("list_directory", args, err)])
        with mock.patch.object(hmod, "dispatch") as dispatch:
            turn = self.run_script([bad, DONE])
        dispatch.assert_not_called()
        [result] = self.results(turn)
        self.assertIn("not valid JSON", result)

    def test_complete_calls_before_the_cut_off_one_still_run(self):
        args, err = parse_tool_args(CUT)
        batch = ChatResponse(done_reason="length", tool_calls=[
            ToolCall("read_file", {"path": "a.txt"}), ToolCall("write_file", args, err)])
        turn = self.run_script([batch, DONE])
        read, write = self.results(turn)
        self.assertIn("hello", read)
        self.assertTrue(write.startswith("ERROR: not run"))
        self.assertFalse((self.wd / "big.py").exists())

    def test_a_cut_off_text_call_does_not_end_the_turn(self):
        text = ('Here it is.\n<tool_call>\n{"name": "write_file", "arguments": '
                '{"path": "big.py", "content": "def f():\\n')
        cut = ChatResponse(content=text, done_reason="length", eval_tokens=100)
        turn = self.run_script([cut, DONE])
        self.assertEqual([m["role"] for m in turn], ["user", "assistant", "user", "assistant"])
        self.assertEqual(turn[1]["content"], "Here it is.")       # the half markup is cut off
        self.assertTrue(turn[2]["content"].startswith("[Note from the harness, not the user]"))
        self.assertIn("write_file", turn[2]["content"])
        self.assertEqual(turn[3]["content"], "Done.")
        self.assertFalse((self.wd / "big.py").exists())

    # The retry prompt goes in only after a tool result (never two user turns).
    def test_an_empty_cut_off_asks_for_a_smaller_call(self):
        self.run_script([_call("read_file", path="a.txt"), ChatResponse(done_reason="length"), DONE])
        retry = self.h.client.requests[2]["messages"][-1]
        self.assertEqual(retry["role"], "user")
        self.assertIn("append_to_file", retry["content"])

    def test_an_empty_cut_off_after_thinking_keeps_the_old_retry(self):
        self.run_script([_call("read_file", path="a.txt"),
                         ChatResponse(thinking="hmm " * 20, done_reason="length"), DONE])
        retry = self.h.client.requests[2]["messages"][-1]
        self.assertIn("Do NOT output any reasoning", retry["content"])

    def test_the_same_cut_off_again_and_again_is_a_loop(self):
        ScriptedClient.script = [_cut_write()]
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode("coding")
        self.h.stream = False
        self.h.loop_resets = 0       # stop at the first loop instead of rewinding
        self.h.send("go")
        self.assertEqual(len(self.h.client.requests), 3)

    def test_esc_during_a_batch_settles_the_rest(self):
        batch = ChatResponse(done_reason="stop", tool_calls=[
            ToolCall("read_file", {"path": "a.txt"}),
            ToolCall("write_file", {"path": "b.txt", "content": "x"})])

        def first_call_interrupts(*a, **k):
            self.h._cancel.set()
            return "hello"

        with mock.patch.object(hmod, "dispatch", side_effect=first_call_interrupts) as dispatch:
            turn = self.run_script([batch, DONE])
        self.assertEqual(dispatch.call_count, 1)
        self.assertFalse((self.wd / "b.txt").exists())
        read, write = self.results(turn)
        self.assertIn("interrupted", write)
        self.assertEqual(len(self.h.client.requests), 1)     # the turn ended there


if __name__ == "__main__":
    unittest.main()
