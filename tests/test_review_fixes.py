"""Regressions for the issues found in the 2026-10-16 code review.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import compaction, companion, prompts, session as session_mod, tdd, tools
from harness.commands import handle as handle_command
from harness.controller import Controller
from harness.llm.base import ChatResponse, LLMClient
from harness.llm.llamacpp_client import LlamaCppClient
from harness.llm.ollama_client import OllamaClient
from harness.logger import Logger
from harness.toolcall_text import (_extract_text_tool_calls, _strip_text_tool_calls,
                                   _strip_turn_markers)
from home_tmp import setUpModule, tearDownModule  # noqa: F401


class ScriptedClient(LLMClient):
    """Replies from a list; an Exception in it is raised instead."""
    provider_name = "fake"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.replies: list = []
        self.calls: list = []
        self.on_call = None

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.calls.append(messages)
        if self.on_call:
            self.on_call()
        reply = self.replies.pop(0) if self.replies else ChatResponse(content="ok")
        if isinstance(reply, Exception):
            raise reply
        return reply

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class HarnessCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: ScriptedClient("http://fake", "fake")
        self._patches = [mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
                         mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json")]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)
        self.h.stream = False
        self.client = self.h.client

    def tearDown(self):
        self.h.logger.close()
        for p in self._patches:
            p.stop()
        hmod.make_client = self._orig
        self.tmp.cleanup()


class Adapters(unittest.TestCase):
    def test_bad_host_keeps_the_working_client(self):
        for cls in (OllamaClient, LlamaCppClient):
            with self.subTest(cls.__name__):
                c = cls(host="http://127.0.0.1:1", model="m")
                before = c._client
                for bad in ("ftp://x", "http://[::1", "localhost:99999"):
                    with self.assertRaises(ValueError):
                        c.set_host(bad)
                self.assertEqual(c.host, "http://127.0.0.1:1")
                self.assertIs(c._client, before)
                self.assertFalse(c._client._closed)

    def test_ollama_tool_results_carry_the_tool_name(self):
        wire = OllamaClient._wire_messages([{"role": "tool", "name": "read_file", "content": "x"}])
        self.assertEqual(wire[0]["tool_name"], "read_file")
        self.assertNotIn("name", wire[0])

    def test_llamacpp_non_object_arguments_become_empty(self):
        r = LlamaCppClient._normalize({"choices": [{"message": {"tool_calls": [
            {"function": {"name": "read_file", "arguments": "[1, 2]"}}]}}]})
        self.assertEqual(r.tool_calls[0].arguments, {})


class Turns(HarnessCase):
    def test_esc_during_the_compaction_summary_ends_the_turn(self):
        self.h.set_mode("coding")
        self.h.messages = self.h.messages[:1]
        for n in range(60):
            self.h.messages += [{"role": "user", "content": f"q{n} " + "q" * 800},
                                {"role": "assistant", "content": "a" * 4000}]
        self.h.context_fixed = True
        self.h.context_limit = compaction.msg_tokens(self.h.messages[0]) + 6000
        self.client.on_call = self.h._cancel.set      # Esc while the summary generates
        self.h.send("next")
        self.assertEqual(len(self.client.calls), 1)   # the summariser, and no model turn

    def test_a_turn_after_an_error_does_not_stack_user_messages(self):
        self.client.replies = [RuntimeError("server down"), ChatResponse(content="fine")]
        self.h.send("first")
        self.h.send("second")
        roles = [m["role"] for m in self.h.messages[1:]]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertEqual(self.h.messages[1]["content"], "first\n\nsecond")

    def test_a_turn_after_an_interrupted_tool_call_bridges_the_tool_result(self):
        self.h.messages += [{"role": "user", "content": "q"},
                            {"role": "assistant", "content": None, "tool_calls": [
                                {"function": {"name": "list_directory", "arguments": {}}}]},
                            {"role": "tool", "name": "list_directory", "content": "(empty)"}]
        self.h.send("go on")
        roles = [m["role"] for m in self.h.messages[1:]]
        self.assertEqual(roles[:5], ["user", "assistant", "tool", "assistant", "user"])


class Commands(HarnessCase):
    def test_a_failing_command_is_reported_not_raised(self):
        c = Controller(self.h)
        sub = self.h.event_queue.subscribe(replay=False)
        with mock.patch.object(self.client, "set_host", side_effect=RuntimeError("boom")):
            c.submit("/host http://elsewhere:1")
        texts = []
        while True:
            try:
                ev = sub.get_nowait()
            except Exception:
                break
            texts.append(getattr(ev, "text", ""))
        self.assertTrue(any("/host failed: RuntimeError: boom" in t for t in texts), texts)

    def test_bad_host_url_is_an_error(self):
        self.client.set_host = OllamaClient.set_host.__get__(self.client)   # validating set_host
        self.client._make_client = OllamaClient._make_client.__get__(self.client)
        out = handle_command("/host ftp://x", self.h).output
        self.assertTrue(out.startswith("ERROR: unsupported scheme"), out)
        self.assertEqual(self.client.host, "http://fake")

    def test_limits_are_saved(self):
        handle_command("/tool-result 5000", self.h)
        handle_command("/net-max-bytes 500kb", self.h)
        handle_command("/net-max-chars 12000", self.h)
        prefs = session_mod.load_prefs()
        self.assertEqual((prefs["max_tool_result"], prefs["net_max_bytes"], prefs["net_max_chars"]),
                         (5000, 500 * 1024, 12000))

    def test_skills_rebuild_the_whole_prompt(self):
        names = self.h.list_available_skills()
        if not names:
            self.skipTest("no skills shipped")
        self.h.load_skill(names[0])
        self.assertEqual(self.h.messages[0]["content"], self.h._build_system_prompt())


class Logs(unittest.TestCase):
    def test_log_goes_next_to_the_session_file(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(session_mod, "SESSION_DIR", Path(d) / "s"):
            log = Logger("2026-01-01T00-00-00")
            log.close()
            self.assertEqual(log.path, Path(d) / "s" / "2026-01-01T00-00-00.log")


class Tdd(unittest.TestCase):
    def test_switch_on_keeps_crlf_line_endings(self):
        with tempfile.TemporaryDirectory() as d:
            wd = Path(d)
            src = ("import unittest\r\n\r\nclass T(unittest.TestCase):\r\n"
                   "    @unittest.skip(\"tdd-step-1\")\r\n    def test_a(self):\r\n"
                   "        self.assertEqual(1, 1)\r\n")
            (wd / "test_a.py").write_bytes(src.encode())
            removed, _left, _changed = tdd.switch_on(wd, "tdd-step-1")
            self.assertEqual(removed, 1)
            out = (wd / "test_a.py").read_bytes().decode()
            self.assertNotIn("tdd-step-1", out)
            self.assertEqual(out.count("\r\n"), out.count("\n"))


class TextToolCalls(unittest.TestCase):
    def test_phi_call_is_stripped_whole(self):
        text = 'before <|tool_call|>{"name": "read_file", "arguments": {"path": "a.py"}} after'
        self.assertEqual(_extract_text_tool_calls(text, tools.ALL_TOOLS),
                         [{"name": "read_file", "arguments": {"path": "a.py"}}])
        self.assertEqual(_strip_text_tool_calls(text), "before  after")

    def test_command_r_arguments_with_a_brace_in_a_string(self):
        text = 'Action: read_file\nAction Input: {"path": "a}.py"}'
        self.assertEqual(_extract_text_tool_calls(text, tools.ALL_TOOLS),
                         [{"name": "read_file", "arguments": {"path": "a}.py"}}])
        self.assertEqual(_strip_text_tool_calls(text), "")

    def test_leaked_turn_markers_are_stripped(self):
        text = ('Calling it now.<|im_end|>\n<|im_start|>assistant\n'
                '{"name": "read_file", "arguments": {"path": "a.py"}}')
        self.assertEqual(_extract_text_tool_calls(text, tools.ALL_TOOLS),
                         [{"name": "read_file", "arguments": {"path": "a.py"}}])
        self.assertEqual(_strip_turn_markers("Done.<end_of_turn>\n<start_of_turn>model\nOk."),
                         "Done.\nOk.")
        self.assertEqual(_strip_turn_markers("<|im_end|>"), "")


class Prompts(unittest.TestCase):
    def test_internet_line_follows_the_offered_net_tools(self):
        env = prompts._environment(Path("."), {"web_search"}, "on")
        self.assertIn("Internet: on — use web_search", env)
        self.assertIn("Internet: off", prompts._environment(Path("."), set(), "on"))


class Dispatch(unittest.TestCase):
    def test_an_error_inside_a_tool_is_not_called_bad_arguments(self):
        def boom(*, workdir):
            raise TypeError("inner bug")

        def two(a, *, workdir):
            return a

        fake = {n: tools.Tool(tools.tool_schema(n, "test tool", {}, []), run=fn)
                for n, fn in (("boom", boom), ("two", two))}
        with mock.patch.dict(tools.registry._BUILTIN, fake):
            with self.assertRaises(TypeError):
                tools.dispatch("boom", {}, Path("."))
            self.assertIn("ERROR: bad arguments for two", tools.dispatch("two", {}, Path(".")))


class Companion(unittest.TestCase):
    def test_every_frame_is_cat_width_and_blinks_close_the_eye(self):
        for frames in (companion.MOMO_WR, companion.MOMO_WL, companion.MOMO_SIT,
                       companion.MOMO_SIT_L, [companion.MOMO_WR_BLINK, companion.MOMO_WL_BLINK]):
            for frame in frames:
                self.assertTrue(all(len(line) == companion.CAT_W for line in frame), frame)
        self.assertIn("-", companion.MOMO_WL_BLINK[1])
        self.assertIn("-", companion.MOMO_WR_BLINK[1])


if __name__ == "__main__":
    unittest.main()
