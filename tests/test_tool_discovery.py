"""Loading tools on demand (/tools discover): which tools are declared and which
only listed, find_tools, loading a tool a call or a result names, a system
prompt that loads never change, the loaded set in the session, the commands,
and MCP servers' exposure — against a scripted model.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import commands
from harness import session as session_mod
from harness import tools
from harness.editing import EDIT_FEATURES
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.mcp import config as cfg_mod
from harness.tools import discover, registry

STUB = str(Path(__file__).parent / "fixtures" / "mcp_stub_server.py")
ON_DEMAND = {"append_to_file", "replace_symbol", "file_info", "grep_extract", "stop_command",
             "file_dependencies", "index_status", "add_search_source"}


class _Scripted(LLMClient):
    provider_name = "fake"
    replies: list = []
    seen_tools: list = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        _Scripted.seen_tools.append([t["function"]["name"] for t in tools])
        return self.replies.pop(0) if self.replies else ChatResponse(content="Done.",
                                                                     done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


def call(name, **args):
    return ChatResponse(content="", tool_calls=[ToolCall(name=name, arguments=args)],
                        done_reason="stop")


def names(schemas):
    return {t["function"]["name"] for t in schemas}


class Registry(unittest.TestCase):
    """Offering as a pure function of the ToolContext."""

    def ctx(self, **kw):
        base = {"role": "coding", "edit_features": EDIT_FEATURES, "index_on": True, "net_on": True}
        return registry.ToolContext(**{**base, **kw})

    def test_off_changes_nothing(self):
        self.assertEqual(registry.offered(self.ctx()), registry.offered(self.ctx(discover="off")))
        self.assertNotIn("find_tools", names(registry.offered(self.ctx())))

    def test_on_lists_instead_of_declaring(self):
        offered = names(registry.offered(self.ctx(discover="all")))
        catalog = names(registry.catalog(self.ctx(discover="all")))
        self.assertIn("find_tools", offered)
        self.assertFalse(offered & catalog)
        self.assertIn("append_to_file", catalog)
        self.assertIn("add_search_source", catalog)
        self.assertNotIn("file_dependencies", catalog)    # the index replaces it
        loaded = names(registry.offered(self.ctx(discover="all", loaded=frozenset({"append_to_file"}))))
        self.assertEqual(loaded - offered, {"append_to_file"})

    def test_gating_still_applies(self):
        self.assertNotIn("append_to_file", names(registry.catalog(self.ctx(role="chat", discover="all"))))
        self.assertNotIn("index_status", names(registry.catalog(self.ctx(discover="all",
                                                                         index_on=False))))
        loaded = self.ctx(role="chat", discover="all", loaded=frozenset({"append_to_file"}))
        self.assertNotIn("append_to_file", names(registry.offered(loaded)))

    def test_what_is_on_demand(self):
        self.assertEqual({t.name for t in registry.builtins() if t.discoverable}, ON_DEMAND)
        # Core since the A/B (evals/README.md): left on demand, the model used rm and
        # mv, re-ran commands, made one edit_file call per change, and lost the
        # scratchpad rule instead of loading them.
        for name in ("ask_user", "create_plan", "complete_step", "revise_plan", "find_tools",
                     "read_file", "edit_file", "run_command", "index_map", "index_changes",
                     "delete_file", "command_output", "edit_files", "move_file", "scratch_write"):
            self.assertFalse(registry.get(name).discoverable, name)

    def test_suggestions_name_on_demand_tools(self):
        for t in registry.builtins():
            with self.subTest(tool=t.name):
                self.assertLessEqual(set(t.suggests), ON_DEMAND)


class Ranking(unittest.TestCase):
    def setUp(self):
        self.catalog = registry.catalog(registry.ToolContext(
            role="coding", edit_features=EDIT_FEATURES, index_on=True, net_on=True, discover="all"))

    def test_queries(self):
        for query, best in [("stop the background server", "stop_command"),
                            ("add text to the end of a file", "append_to_file"),
                            ("rewrite a whole function", "replace_symbol"),
                            ("file size", "file_info")]:
            with self.subTest(query=query):
                ranked = discover.rank(query, self.catalog)
                self.assertEqual(ranked[0], best)
                self.assertLessEqual(len(ranked), discover.QUERY_LOAD)

    def test_no_match(self):
        self.assertEqual(discover.rank("zebra", self.catalog), [])
        self.assertEqual(discover.rank("the a of", self.catalog), [])

    def test_mentioned_whole_words(self):
        self.assertEqual(discover.mentioned('stop_command(path="r1") stops it',
                                            {"stop_command", "command_output"}), {"stop_command"})
        self.assertEqual(discover.mentioned("stop_commands, harness.stop_command",
                                            {"stop_command"}), set())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.wd = self.home / "proj"
        self.wd.mkdir()
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                        mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
                        mock.patch.object(hmod, "make_client",
                                          lambda *a, **k: _Scripted("http://fake", "fake")),
                        mock.patch.object(hmod.Harness, "_start_index", lambda self: None)]
        for p in self.patches:
            p.start()
        _Scripted.replies, _Scripted.seen_tools = [], []
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode("coding")
        self.h.set_tools_discover("all")

    def tearDown(self):
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def declared(self):
        return names(self.h._current_tools())

    def cmd(self, text):
        return commands.handle(text, self.h).output

    def turn(self, *replies):
        _Scripted.replies = list(replies)
        start = len(self.h.messages)
        self.h.send("go")
        return [m["content"] for m in self.h.messages[start:] if m.get("role") == "tool"]


class Harness(Base):
    def test_prompt_lists_the_catalog(self):
        prompt = self.h.messages[0]["content"]
        self.assertIn("## More tools (load with find_tools)", prompt)
        self.assertIn("- append_to_file(path, content)", prompt)
        self.assertNotIn("**append_to_file**", prompt)          # not in the tool reference
        self.assertIn("**find_tools**", prompt)
        self.assertNotIn("append_to_file", self.declared())
        view = self.h.system_prompt_view()
        self.assertIn("discover", [s["key"] for s in view["sections"]])

    def test_off_is_as_before(self):
        self.h.set_tools_discover("off")
        on_prompt = self.h.messages[0]["content"]
        self.assertNotIn("More tools", on_prompt)
        self.assertNotIn("find_tools", self.declared())
        self.assertIn("append_to_file", self.declared())

    def test_find_tools_by_name_then_call(self):
        (self.wd / "a.txt").write_text("x\n")
        results = self.turn(call("find_tools", names=["append_to_file"]),
                            call("append_to_file", path="a.txt", content="y\n"))
        self.assertIn("Loaded append_to_file", results[0])
        self.assertIn('<tool_call>{"name": "append_to_file"', results[0])
        self.assertNotIn("append_to_file", _Scripted.seen_tools[0])
        self.assertIn("find_tools", _Scripted.seen_tools[0])
        self.assertIn("append_to_file", _Scripted.seen_tools[1])
        self.assertEqual((self.wd / "a.txt").read_text(), "x\ny\n")
        self.assertEqual(self.h.loaded_tools, {"append_to_file"})

    def test_find_tools_by_query(self):
        results = self.turn(call("find_tools", query="add text to the end of a file"))
        self.assertIn("Loaded append_to_file", results[0])
        self.assertIn("append_to_file", self.declared())

    def test_find_tools_errors(self):
        self.assertIn("ERROR: pass names", self.h._find_tools({}))
        out = self.h._find_tools({"names": ["nope"]})
        self.assertTrue(out.startswith("ERROR: no loadable tool named nope"))
        self.assertIn("append_to_file", out)                    # lists what can be loaded
        self.assertIn("already available", self.h._find_tools({"names": ["read_file"]}))
        six = sorted(names(self.h._tool_catalog()))[:6]
        self.assertIn("at most 5", self.h._find_tools({"names": six}))
        self.assertIn("No loadable tool", self.h._find_tools({"query": "zebra"}))
        self.cmd("/tools append_to_file off")
        self.assertIn("turned off", self.h._find_tools({"names": ["append_to_file"]}))
        self.assertEqual(self.h.loaded_tools, set())
        self.assertNotIn("append_to_file", names(self.h._tool_catalog()))

    def test_loading_twice_is_harmless(self):
        self.h._find_tools({"names": ["append_to_file"]})
        self.assertIn("Already loaded", self.h._find_tools({"names": ["append_to_file"]}))
        self.assertEqual(self.h.loaded_tools, {"append_to_file"})

    def test_names_as_a_string(self):
        self.h._find_tools({"names": "append_to_file, file_info"})
        self.assertEqual(self.h.loaded_tools, {"append_to_file", "file_info"})

    def test_direct_call_loads_and_runs(self):
        (self.wd / "junk.txt").write_text("x\n")
        results = self.turn(call("append_to_file", path="junk.txt", content="y\n"))
        self.assertFalse(results[0].startswith("ERROR"), results[0])
        self.assertEqual((self.wd / "junk.txt").read_text(), "x\ny\n")
        self.assertIn("append_to_file", self.h.loaded_tools)
        self.assertIn("append_to_file", _Scripted.seen_tools[1])

    def test_text_call_to_an_unloaded_tool(self):
        (self.wd / "junk.txt").write_text("x\n")
        text = ('<tool_call>{"name": "append_to_file", "arguments": {"path": "junk.txt", '
                '"content": "y\\n"}}</tool_call>')
        self.turn(ChatResponse(content=text, done_reason="stop"))
        self.assertEqual((self.wd / "junk.txt").read_text(), "x\ny\n")
        self.assertIn("append_to_file", self.h.loaded_tools)

    def test_a_result_naming_a_tool_loads_it(self):
        results = self.turn(call("run_command", command="echo ready; sleep 30", background=True,
                                 wait_for="ready"),
                            call("stop_command", path="r1"))
        self.assertIn("stop_command(", results[0])
        self.assertEqual(self.h.loaded_tools, {"stop_command"})
        self.assertNotIn("stop_command", _Scripted.seen_tools[0])
        self.assertIn("stop_command", _Scripted.seen_tools[1])
        self.assertFalse(results[1].startswith("ERROR"), results[1])

    def test_loads_never_change_the_system_prompt(self):
        before = self.h.messages[0]["content"]
        self.h.load_tools(["append_to_file", "file_info"], "test")
        self.assertEqual(self.h.messages[0]["content"], before)
        self.h.rebuild_system_prompt()                     # a later rebuild agrees
        self.assertEqual(self.h.messages[0]["content"], before)

    def test_no_find_tools_when_nothing_is_left(self):
        self.h.set_mode("chat")
        for name in names(self.h._tool_catalog()):
            self.cmd(f"/tools {name} off")
        self.assertEqual(self.h._tool_catalog(), [])
        self.assertNotIn("find_tools", self.declared())

    def test_session_keeps_the_loaded_set(self):
        self.turn(call("find_tools", names=["append_to_file"]))
        path = self.h.session_path()
        self.h.new_session()
        self.assertEqual(self.h.loaded_tools, set())
        self.h.load_session(path)
        self.assertEqual(self.h.loaded_tools, {"append_to_file"})
        self.assertIn("append_to_file", self.declared())

    def test_pref(self):
        self.assertEqual(session_mod.load_prefs()["tools_discover"], "all")
        self.cmd("/tools discover off")
        self.assertEqual(session_mod.load_prefs()["tools_discover"], "off")
        self.assertEqual(self.h.tools_discover, "off")
        self.assertIn("ERROR: expected", self.cmd("/tools discover sometimes"))
        self.cmd("/tools discover on")                     # on: the default mode
        self.assertEqual(self.h.tools_discover, "extensions")

    def test_choices_and_commands(self):
        self.h.load_tools(["append_to_file"], "test")
        state = {c["name"]: c["state"] for c in self.h.tool_choices()}
        self.assertEqual(state["read_file"], "core")
        self.assertEqual(state["append_to_file"], "loaded")
        self.assertEqual(state["file_info"], "on demand")
        self.assertEqual(state["delete_file"], "core")
        self.assertTrue(next(c for c in self.h.tool_choices() if c["name"] == "find_tools")["locked"])
        listing = self.cmd("/tools list")
        self.assertIn("append_to_file  · loaded", listing)
        self.assertIn("file_info  · on demand", listing)
        self.assertIn("1 loaded", self.cmd("/tools discover"))
        self.assertIn("Loaded: file_info", self.cmd("/tools load file_info read_file"))
        self.assertIn("Unloaded: append_to_file, file_info", self.cmd("/tools unload all"))
        self.assertEqual(self.h.loaded_tools, set())
        self.assertIn("can't be turned off", self.h.set_tool_enabled("find_tools", False))
        self.cmd("/tools discover off")
        self.assertIn("ERROR: loading tools on demand is off", self.cmd("/tools load append_to_file"))
        self.assertTrue(all(c["state"] == "core" for c in self.h.tool_choices()))


def _ext_tool(name, source, discoverable=None):
    """A custom (source: its file) or workspace (source: "workspace:…") tool."""
    return registry.Tool(
        registry.tool_schema(name, f"Count the widgets ({name}).",
                             {"path": {"type": "string", "description": "Folder"}}, ["path"]),
        run=lambda path, *, workdir: f"{name}: 3 widgets in {path}", modes=registry.ALL_ROLES,
        discoverable=discoverable, source=source)


class Extensions(Base):
    """The default mode: custom, workspace and MCP tools on demand, every
    built-in declared."""

    def setUp(self):
        super().setUp()
        self.h.set_tools_discover("extensions")
        self.addCleanup(registry.set_custom, [])
        self.addCleanup(registry.set_workspace, [])

    def test_default_and_saved_values(self):
        fresh = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.addCleanup(fresh.logger.close)
        self.assertEqual(fresh.tools_discover, "extensions")
        self.assertEqual(registry.discover_mode(True), "all")      # saved before the modes
        self.assertEqual(registry.discover_mode(False), "off")
        self.assertEqual(registry.discover_mode("nonsense"), "extensions")
        self.assertEqual(registry.discover_mode("all"), "all")

    def test_builtins_stay_declared(self):
        declared = self.declared()
        self.assertLessEqual(ON_DEMAND - {"add_search_source", "file_dependencies",
                                          "index_status"}, declared)
        self.assertEqual(self.h._tool_catalog(), [])
        self.assertNotIn("find_tools", declared)                   # nothing to load
        self.assertNotIn("More tools", self.h.messages[0]["content"])
        self.assertNotIn("find_tools", [c["name"] for c in self.h.tool_choices()])

    def test_custom_and_workspace_tools_load_on_demand(self):
        registry.set_custom([_ext_tool("count_widgets", "/home/me/.momo-harness/tools/w.py")])
        registry.set_workspace([_ext_tool("lint_widgets", "workspace:.momo/tools/l.py")])
        self.h.rebuild_system_prompt()
        self.assertEqual(names(self.h._tool_catalog()), {"count_widgets", "lint_widgets"})
        self.assertNotIn("count_widgets", self.declared())
        self.assertIn("find_tools", self.declared())
        self.assertIn("move_file", self.declared())
        prompt = self.h.messages[0]["content"]
        self.assertIn("- count_widgets(path) — Count the widgets (count_widgets).", prompt)
        results = self.turn(call("find_tools", names=["count_widgets"]),
                            call("count_widgets", path="src"))
        self.assertIn("Loaded count_widgets", results[0])
        self.assertEqual(results[1], "count_widgets: 3 widgets in src")
        self.assertIn("count_widgets", _Scripted.seen_tools[1])
        self.assertEqual(self.h.messages[0]["content"], prompt)

    def test_opting_out_keeps_one_declared(self):
        registry.set_custom([_ext_tool("count_widgets", "/x/w.py", discoverable=False)])
        self.assertIn("count_widgets", self.declared())
        self.assertEqual(self.h._tool_catalog(), [])

    def test_off_declares_them(self):
        registry.set_custom([_ext_tool("count_widgets", "/x/w.py")])
        self.h.set_tools_discover("off")
        self.assertIn("count_widgets", self.declared())

    def test_all_adds_the_builtins(self):
        registry.set_custom([_ext_tool("count_widgets", "/x/w.py")])
        self.h.set_tools_discover("all")
        catalog = names(self.h._tool_catalog())
        self.assertIn("count_widgets", catalog)
        self.assertIn("append_to_file", catalog)


class Mcp(unittest.TestCase):
    """MCP servers' exposure: "discover" (the default) lists their tools until
    loaded; "direct" declares them."""
    exposure = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        work = self.home / "proj"
        work.mkdir()
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                        mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
                        mock.patch.object(hmod, "make_client",
                                          lambda *a, **k: _Scripted("http://fake", "fake")),
                        mock.patch.object(hmod.Harness, "_start_index", lambda self: None)]
        for p in self.patches:
            p.start()
        entry = {"command": sys.executable, "args": [STUB]}
        if self.exposure:
            entry["exposure"] = self.exposure
        cfg_mod.global_path().parent.mkdir(parents=True, exist_ok=True)
        cfg_mod.global_path().write_text(json.dumps({"mcpServers": {"stub": entry}}))
        _Scripted.replies, _Scripted.seen_tools = [], []
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=work)
        self.h.set_mode("coding")
        self.h.start_mcp()
        self.assertEqual(self.h.mcp.wait_ready(15), [])
        self.assertEqual(self.h.tools_discover, "extensions")     # the default
        self.h.rebuild_system_prompt()

    def tearDown(self):
        self.h.shutdown_mcp()
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_discover_lists_until_loaded(self):
        self.assertNotIn("mcp__stub__echo", names(self.h._current_tools()))
        self.assertIn("mcp__stub__echo", names(self.h._tool_catalog()))
        prompt = self.h.messages[0]["content"]
        self.assertIn("## MCP servers", prompt)            # its instructions still reach the model
        self.assertIn("- mcp__stub__echo(", prompt)
        _Scripted.replies = [call("find_tools", names=["mcp__stub__echo"]),
                             call("mcp__stub__echo", text="hi")]
        self.h.send("go")
        results = [m["content"] for m in self.h.messages if m.get("role") == "tool"]
        self.assertEqual(results[-1], "echo: hi")
        self.assertIn("mcp__stub__echo", _Scripted.seen_tools[1])
        self.assertEqual(self.h.messages[0]["content"], prompt)
        choice = next(c for c in self.h.tool_choices() if c["name"] == "mcp__stub__write_note")
        self.assertEqual(choice["state"], "on demand")

    def test_resource_tools_stay_declared(self):
        if "mcp_list_resources" in names(self.h.mcp.resource_tool_schemas()):
            self.assertIn("mcp_list_resources", names(self.h._current_tools()))


class McpDirect(Mcp):
    exposure = "direct"

    def test_discover_lists_until_loaded(self):
        self.assertIn("mcp__stub__echo", names(self.h._current_tools()))
        self.assertNotIn("mcp__stub__echo", names(self.h._tool_catalog()))
        self.assertEqual(self.h.mcp.status_rows()[0]["exposure"], "direct")

    def test_bad_exposure_is_a_config_error(self):
        with self.assertRaises(cfg_mod.ConfigError):
            cfg_mod.parse_entry("x", {"command": "y", "exposure": "sometimes"}, "global")
        self.assertEqual(cfg_mod.parse_entry("x", {"command": "y"}, "global").exposure, "discover")


if __name__ == "__main__":
    unittest.main()
