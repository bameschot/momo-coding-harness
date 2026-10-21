"""Custom tools: files in ~/.momo-harness/tools/ (harness/tools/custom.py),
loaded, checked and offered like the built-in tools."""
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import commands
from harness import session as session_mod
from harness.llm.base import ChatResponse, ToolCall
from harness.tools import custom, dispatch, offered, registry, render_tool_reference

from test_run_output import _Scripted

EXAMPLE = Path(__file__).resolve().parent.parent / "docs" / "examples" / "tools" / "word_count.py"

_TOOL = '''
from harness.tools import ALL_ROLES, Tool, tool_schema

def run(text: str, *, workdir, **kw):
    return "echo: " + text

TOOL = Tool(tool_schema({name!r}, "Echo the text back.", {{"text": {{"type": "string"}}}}, ["text"]),
            run=run{extra})
'''


class _Folder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.folder = root / "tools"
        self.folder.mkdir(mode=0o700)
        os.chmod(self.folder, 0o700)
        self.wd = root / "project"
        self.wd.mkdir()
        self.addCleanup(registry.set_custom, [])
        self.addCleanup(self.tmp.cleanup)

    def put(self, name: str, text: str, mode: int = 0o600) -> Path:
        p = self.folder / name
        p.write_text(textwrap.dedent(text))
        os.chmod(p, mode)
        return p

    def tool(self, file: str, name: str, extra: str = "", mode: int = 0o600) -> Path:
        if "modes=" not in extra:
            extra = ", modes=ALL_ROLES" + extra
        return self.put(file, _TOOL.format(name=name, extra=extra), mode)

    def load(self):
        return custom.load_custom_tools(self.folder)


class Loading(_Folder):
    def test_the_example_loads_and_works(self):
        shutil.copy(EXAMPLE, self.folder / "word_count.py")
        os.chmod(self.folder / "word_count.py", 0o600)
        report = self.load()
        self.assertEqual((report.loaded, report.errors), (["word_count"], []))
        (self.wd / "a.txt").write_text("one two\nthree\n")
        self.assertEqual(dispatch("word_count", {"path": "a.txt"}, self.wd),
                         "a.txt: 2 lines, 3 words, 14 characters")
        self.assertTrue(dispatch("word_count", {"path": "../x"}, self.wd).startswith("ERROR"))
        for role in ("chat", "design", "plan", "coding"):
            names = [t["function"]["name"] for t in offered(role)]
            self.assertEqual(names[-1], "word_count", role)     # after every built-in tool
        ref = render_tool_reference(offered("chat"), "compact")
        self.assertIn('{"name": "word_count", "arguments": {"path": "README.md"}}', ref)
        self.assertFalse((self.folder / "__pycache__").exists())

    def test_reload_replaces_the_set(self):
        self.tool("echo.py", "echo_text")
        self.assertEqual(self.load().loaded, ["echo_text"])
        (self.folder / "echo.py").unlink()
        self.tool("other.py", "other_text")
        self.assertEqual(self.load().loaded, ["other_text"])
        self.assertIsNone(registry.get("echo_text"))
        self.assertEqual(custom.load_custom_tools(self.folder / "missing").loaded, [])
        self.assertIsNone(registry.get("other_text"))

    def test_refusals(self):
        cases = {
            "bad.py": ("Bad-Name", "", "must be 2-64 characters"),
            "clash.py": ("read_file", "", "is a built-in tool"),
            "mcp.py": ("mcp_thing", "", "reserved for MCP"),
            "harness.py": ("asks_harness", ", harness=True", "only for built-in tools"),
            "locked.py": ("locks_it", ", locked=True", "only for built-in tools"),
            "inject.py": ("wants_harness", ", inject=('harness',)", "unknown state"),
            "roles.py": ("odd_role", ", modes=frozenset({'admin'})", "are not roles"),
        }
        for file, (name, extra, _) in cases.items():
            self.tool(file, name, extra)
        self.put("norun.py", '''
            from harness.tools import Tool, tool_schema
            TOOL = Tool(tool_schema("no_run", "Nothing to run.", {}, []))
        ''')
        self.put("notool.py", "X = 1\n")
        self.put("boom.py", "raise RuntimeError('broken on import')\n")
        self.put("exits.py", "import sys\nsys.exit(3)\n")
        self.put("_helper.py", "raise RuntimeError('never imported')\n")
        self.tool("ok.py", "still_fine")
        self.tool("twice.py", "still_fine")
        report = self.load()
        why = dict(report.errors)
        for file, (_, _, expect) in cases.items():
            self.assertIn(expect, why[file], file)
        self.assertIn("run is missing", why["norun.py"])
        self.assertIn("defines no TOOL", why["notool.py"])
        self.assertIn("broken on import", why["boom.py"])
        self.assertIn("Traceback", report.details["boom.py"])
        self.assertIn("SystemExit", why["exits.py"])
        self.assertIn("defined twice", why["twice.py"])
        self.assertNotIn("_helper.py", why)
        self.assertEqual(report.loaded, ["still_fine"])
        self.assertIn("not loaded", report.summary())

    def test_untrusted_files_and_folders_are_not_loaded(self):
        self.tool("shared.py", "shared_tool", mode=0o664)
        report = self.load()
        self.assertIn("writable by other users", dict(report.errors)["shared.py"])
        self.assertEqual(report.loaded, [])
        os.chmod(self.folder, 0o777)
        report = self.load()
        self.assertIn("writable by other users", report.errors[0][1])
        os.chmod(self.folder, 0o700)
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            self.assertIn("not owned by you", self.load().errors[0][1])
        link = self.folder / "link.py"
        link.symlink_to(self.folder / "shared.py")
        self.assertIn("not a regular file", dict(self.load().errors)["link.py"])


class InTheHarness(_Folder):
    def setUp(self):
        super().setUp()
        home = Path(self.tmp.name).resolve()
        # ~/.momo-harness/tools is the folder the harness reads
        (home / ".momo-harness").mkdir()
        self.folder.rename(home / ".momo-harness" / "tools")
        self.folder = home / ".momo-harness" / "tools"
        patches = [mock.patch.dict(os.environ, {"HOME": str(home)}),
                   mock.patch.object(session_mod, "_PREFS_PATH", home / "prefs.json"),
                   mock.patch.object(session_mod, "SESSION_DIR", home / "sessions"),
                   mock.patch.object(hmod, "make_client",
                                     lambda *a, **k: _Scripted("http://fake", "fake"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.tool("echo.py", "echo_text")

    def harness(self):
        h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.addCleanup(h.logger.close)
        h.set_mode("coding")
        return h

    def call(self, h, name, args):
        _Scripted.replies = [ChatResponse(tool_calls=[ToolCall(name=name, arguments=args)]),
                             ChatResponse(content="Done.", done_reason="stop")]
        h.send("go")
        return [m["content"] for m in h.messages if m.get("role") == "tool"][-1]

    def test_loaded_at_start_offered_and_pickable(self):
        h = self.harness()
        self.assertEqual(h.custom_tools.loaded, ["echo_text"])
        self.assertIn("echo_text", h.messages[0]["content"])           # the tool reference
        choice = next(c for c in h.tool_choices() if c["name"] == "echo_text")
        self.assertEqual(choice["group"], "custom")
        self.assertEqual(self.call(h, "echo_text", {"text": "hi"}), "echo: hi")
        commands.handle("/tools echo_text off", h)
        self.assertNotIn("echo_text", [t["function"]["name"] for t in h._current_tools()])

    def test_tools_reload_and_errors(self):
        h = self.harness()
        self.tool("second.py", "second_tool")
        self.put("broken.py", "raise ValueError('nope')\n")
        out = commands.handle("/tools reload", h).output
        self.assertIn("second_tool", out)
        self.assertIn("broken.py not loaded", out)
        self.assertIn("second_tool", h.messages[0]["content"])
        self.assertIn("ValueError: nope", commands.handle("/tools errors", h).output)
        self.assertIn("custom tool broken.py not loaded", commands.handle("/tools list", h).output)

    def test_confirm_asks_before_each_call(self):
        self.tool("careful.py", "careful_tool", extra=", confirm=True")
        h = self.harness()
        asked = []
        h._ask_user = lambda q: asked.append(q) or "n"
        out = self.call(h, "careful_tool", {"text": "x"})
        self.assertTrue(out.startswith("ERROR: the user declined"), out)
        self.assertIn("careful_tool", asked[0])
        h._ask_user = lambda q: "y"
        self.assertEqual(self.call(h, "careful_tool", {"text": "x"}), "echo: x")

    def test_ctx_reaches_the_harness(self):
        self.put("asks.py", '''
            from harness.tools import ALL_ROLES, Tool, tool_schema

            def run(*, workdir, ctx):
                ctx.note("checking with the user")
                return "answer: " + ctx.ask_user("Which one?")

            TOOL = Tool(tool_schema("asks_user", "Ask the user something.", {}, []),
                        run=run, modes=ALL_ROLES, inject=("ctx",))
        ''')
        h = self.harness()
        h._ask_user = lambda q: f"<{q}>"
        self.assertEqual(self.call(h, "asks_user", {}), "answer: <Which one?>")


if __name__ == "__main__":
    unittest.main()
