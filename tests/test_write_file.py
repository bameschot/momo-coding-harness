"""write_file / append_to_file: what reaches the disk and what stays in history.

A qwen model used to see "[written to <path>]" in place of its own past file
bodies; it then rewrote the same file in a loop and finally copied the
placeholder, overwriting real content.  These pin the fix.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import tempfile
import unittest
from pathlib import Path

import harness.harness as hmod
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.llm.ollama_client import _xml_escape_for_ollama
from harness.tools import dispatch

BODY = "def f():\n    return 1 < 2 & True\n"


class ScriptedClient(LLMClient):
    """Replies with one write_file call, then a plain answer."""
    provider_name = "fake"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.replies = [
            ChatResponse(content="", tool_calls=[
                ToolCall(name="write_file", arguments={"path": "f.py", "content": BODY})],
                done_reason="stop"),
            ChatResponse(content="Done.", done_reason="stop"),
        ]

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return self.replies.pop(0) if self.replies else ChatResponse(content="Done.", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class HistoryKeepsContent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: ScriptedClient("http://fake", "qwen3.5:9b")
        self.h = hmod.Harness(host="http://fake", model="qwen3.5:9b", workdir=Path(self.tmp.name))
        self.h.set_mode("coding")
        self.h.stream = False

    def tearDown(self):
        hmod.make_client = self._orig
        self.h.logger.close()
        self.tmp.cleanup()

    def test_qwen_history_stores_real_content(self):
        self.h.send("write f.py")
        calls = [tc for m in self.h.messages if m.get("role") == "assistant"
                 for tc in (m.get("tool_calls") or [])]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["arguments"]["content"], BODY)
        self.assertEqual((Path(self.tmp.name) / "f.py").read_text(), BODY)


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)
        (self.wd / "a.py").write_text("real\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_placeholder_never_written(self):
        for tool in ("write_file", "append_to_file"):
            for content in ("[written to a.py]", "  [written to /abs/a.py]\n"):
                r = dispatch(tool, {"path": "a.py", "content": content}, self.wd)
                self.assertTrue(r.startswith("ERROR:"), r)
                self.assertEqual((self.wd / "a.py").read_text(), "real\n")

    def test_shortened_result_markers_never_written(self):
        # Copies of the notes Harness._compact and the tool-result size cutoff
        # splice in (harness.py) — keep in step if their wording changes.
        markers = ("[… 12,345 chars removed by context compaction …]",
                   "... (truncated after 4000 chars of 9000 — use read_file with "
                   "start_line/end_line for specific sections)")
        for tool in ("write_file", "append_to_file"):
            for marker in markers:
                content = f"import os\n{marker}\nprint('end')\n"
                r = dispatch(tool, {"path": "a.py", "content": content}, self.wd)
                self.assertTrue(r.startswith("ERROR:"), r)
                self.assertEqual((self.wd / "a.py").read_text(), "real\n")

    def test_marker_source_code_is_fine(self):
        # The harness's own f-string templates (no digits) must stay writable.
        src = 'x = f"[… {cut:,} chars removed by context compaction …]"\n'
        r = dispatch("write_file", {"path": "m.py", "content": src}, self.wd)
        self.assertTrue(r.startswith("Written:"), r)

    def test_placeholder_inside_real_content_is_fine(self):
        r = dispatch("write_file", {"path": "b.md", "content": "see [written to x] below\n"}, self.wd)
        self.assertTrue(r.startswith("Written:"), r)

    def test_write_reports_size(self):
        r = dispatch("write_file", {"path": "c.py", "content": "a\nb\n"}, self.wd)
        self.assertEqual(r, "Written: c.py (2 lines, 4 bytes)")

    def test_identical_rewrite_is_no_change(self):
        r = dispatch("write_file", {"path": "a.py", "content": "real\n"}, self.wd)
        self.assertTrue(r.startswith("No change:"), r)

    def test_append_reports_size(self):
        r = dispatch("append_to_file", {"path": "a.py", "content": "more\n"}, self.wd)
        self.assertEqual(r, "OK — appended 1 line, 5 bytes to a.py")
        self.assertEqual((self.wd / "a.py").read_text(), "real\nmore\n")


class OllamaEscaping(unittest.TestCase):
    def test_write_content_escaped_for_qwen_template(self):
        msgs = [{"role": "assistant", "content": None, "tool_calls": [
            {"function": {"name": "write_file", "arguments": {"path": "f.py", "content": BODY}}}]}]
        out = _xml_escape_for_ollama(msgs)
        self.assertEqual(out[0]["tool_calls"][0]["function"]["arguments"]["content"],
                         "def f():\n    return 1 &lt; 2 &amp; True\n")
        # the stored history is untouched
        self.assertEqual(msgs[0]["tool_calls"][0]["function"]["arguments"]["content"], BODY)


if __name__ == "__main__":
    unittest.main()
