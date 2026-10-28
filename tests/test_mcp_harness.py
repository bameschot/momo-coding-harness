"""MCP in the harness: tools offered per role, the confirm policy, the prompt
section, /mcp commands, status, the web API and the TUI server picker — against
tests/fixtures/mcp_stub_server.py with a scripted model.
"""
import curses
import http.client
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import commands
from harness import controller as ctl_mod
from harness import session as session_mod
from harness import tui as T
from harness.events import AskUserEvent
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.mcp import config as cfg_mod

STUB = str(Path(__file__).parent / "fixtures" / "mcp_stub_server.py")


class _Scripted(LLMClient):
    provider_name = "fake"
    replies: list = []
    seen_tools: list = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        _Scripted.seen_tools.append([t["function"]["name"] for t in tools])
        return self.replies.pop(0) if self.replies else ChatResponse(content="Done.", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


def call(name, **args):
    return ChatResponse(content="", tool_calls=[ToolCall(name=name, arguments=args)], done_reason="stop")


class Base(unittest.TestCase):
    servers = {"stub": {"command": sys.executable, "args": [STUB]}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.work = self.home / "proj"
        self.work.mkdir()
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                        mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
                        mock.patch.object(hmod, "make_client",
                                          lambda *a, **k: _Scripted("http://fake", "fake")),
                        mock.patch.object(hmod.Harness, "_start_index", lambda self: None)]
        for p in self.patches:
            p.start()
        cfg_mod.global_path().parent.mkdir(parents=True, exist_ok=True)
        cfg_mod.global_path().write_text(json.dumps({"mcpServers": self.servers}))
        _Scripted.replies, _Scripted.seen_tools = [], []
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.work)
        self.h.set_mode("coding")
        self.h.start_mcp()
        self.assertEqual(self.h.mcp.wait_ready(15), [])
        self.events = self.h.event_queue.subscribe() if hasattr(self.h.event_queue, "subscribe") else None

    def tearDown(self):
        self.h.shutdown_mcp()
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def names(self):
        return [t["function"]["name"] for t in self.h._current_tools()]

    def cmd(self, text):
        return commands.handle(text, self.h)

    def turn(self, *replies, answers=()):
        _Scripted.replies = list(replies)
        for a in answers:
            self.h._user_input_queue.put(a)
        asked = []
        real = self.h._ask_user
        self.h._ask_user = lambda q: (asked.append(q), real(q))[1]
        try:
            self.h.send("go")
        finally:
            self.h._ask_user = real
        results = [m["content"] for m in self.h.messages if m.get("role") == "tool"]
        return results, asked


class Tools(Base):
    def test_model_calls_an_mcp_tool(self):
        results, asked = self.turn(call("mcp__stub__echo", text="hi"))
        self.assertEqual(results, ["echo: hi"])
        self.assertEqual(asked, [])                      # read-only: no confirm under "writes"
        self.assertIn("mcp__stub__echo", _Scripted.seen_tools[0])

    def test_prompt_section_and_reference(self):
        self.turn()                                      # the first turn picks up the servers
        prompt = self.h.messages[0]["content"]
        self.assertIn("## MCP servers", prompt)
        self.assertIn("Use echo to test.", prompt)       # the server's instructions
        self.assertIn("mcp__stub__echo", [t["function"]["name"] for t in self.h._current_tools()])
        secs = self.h.system_prompt_sections()
        self.assertEqual("".join(s["sep"] + s["text"] for s in secs), prompt)

    def test_read_only_roles_get_read_only_tools(self):
        self.h.set_mode("design")
        names = self.names()
        self.assertIn("mcp__stub__echo", names)
        self.assertNotIn("mcp__stub__write_note", names)
        results, _ = self.turn(call("mcp__stub__write_note", text="x"))
        self.assertIn("only offered in coding mode", results[-1])

    def test_picker_groups_and_per_tool_off(self):
        choice = next(c for c in self.h.tool_choices() if c["name"] == "mcp__stub__echo")
        self.assertEqual(choice["group"], "mcp · stub")
        self.assertEqual(self.h.tool_choices()[-1]["group"][:3], "mcp")   # MCP sections last
        self.assertIn("off", self.cmd("/tools mcp__stub__echo off").output)
        self.assertNotIn("mcp__stub__echo", self.names())

    def test_resource_tools(self):
        results, _ = self.turn(call("mcp_list_resources"), call("mcp_read_resource", server="stub", uri="notes://a"))
        self.assertIn("notes://a", results[0])
        self.assertIn("Note A text", results[1])

    def test_master_switch(self):
        self.cmd("/mcp off")
        self.assertFalse([n for n in self.names() if n.startswith("mcp")])
        self.assertFalse(session_mod.load_prefs()["mcp"])
        self.cmd("/mcp on")
        self.assertEqual(self.h.mcp.wait_ready(10), [])
        self.assertIn("mcp__stub__echo", self.names())


class Confirm(Base):
    def test_writes_ask_and_decline(self):
        results, asked = self.turn(call("mcp__stub__write_note", text="x"), answers=["n"])
        self.assertEqual(len(asked), 1)
        self.assertIn("Reply 'y'", asked[0])
        self.assertIn("'a' to always allow", asked[0])
        self.assertIn("stub › write_note", asked[0])
        self.assertIn("declined", results[-1])

    def test_yes_then_always(self):
        results, asked = self.turn(call("mcp__stub__write_note", text="x"), answers=["y"])
        self.assertEqual(results[-1], "saved")
        results, asked = self.turn(call("mcp__stub__write_note", text="x"),
                                   call("mcp__stub__write_note", text="y"), answers=["a"])
        self.assertEqual(len(asked), 1)                  # "a" covers the second call
        self.assertEqual(results[-2:], ["saved", "saved"])

    def test_confirm_off(self):
        self.cmd("/mcp-confirm off")
        results, asked = self.turn(call("mcp__stub__write_note", text="x"))
        self.assertEqual((asked, results[-1]), ([], "saved"))


class AlwaysPolicy(Base):
    servers = {"stub": {"command": sys.executable, "args": [STUB], "confirm": "always"}}

    def test_even_read_only_asks(self):
        _, asked = self.turn(call("mcp__stub__echo", text="x"), answers=["y"])
        self.assertEqual(len(asked), 1)


class NeverPolicy(Base):
    servers = {"stub": {"command": sys.executable, "args": [STUB], "confirm": "never"}}

    def test_nothing_asks(self):
        _, asked = self.turn(call("mcp__stub__write_note", text="x"))
        self.assertEqual(asked, [])


class Commands(Base):
    def test_list_tools_log_and_status(self):
        out = self.cmd("/mcp list").output
        self.assertIn("stub", out)
        self.assertIn("ready", out)
        self.assertTrue(self.cmd("/mcp").mcp_picker)
        tools = self.cmd("/mcp tools stub").output
        self.assertIn("mcp__stub__write_note  [destructive]", tools)
        self.assertIn("mcp__stub__echo  [read-only]", tools)
        self.assertIn("stub started", self.cmd("/mcp log stub").output)
        self.assertIn("ERROR", self.cmd("/mcp tools nope").output)
        st = self.h.status_event()
        self.assertEqual((st.mcp_ready, st.mcp_total, st.mcp_attention), (1, 1, False))

    def test_disable_is_saved(self):
        self.cmd("/mcp disable stub")
        self.assertEqual(session_mod.load_prefs()["mcp_disabled"], ["stub"])
        self.assertNotIn("mcp__stub__echo", self.names())
        self.cmd("/mcp enable stub")
        self.assertEqual(session_mod.load_prefs()["mcp_disabled"], [])

    def test_add_and_rm(self):
        out = self.cmd(f'/mcp add two --env A=1 "{sys.executable}" "{STUB}"').output
        self.assertIn("Added", out)
        entry = json.loads(cfg_mod.global_path().read_text())["mcpServers"]["two"]
        self.assertEqual(entry, {"command": sys.executable, "args": [STUB], "env": {"A": "1"}})
        self.assertEqual(self.h.mcp.wait_ready(10), [])
        self.assertIn("mcp__two__echo", self.names())
        out = self.cmd("/mcp add web --http https://example.com/mcp --header Authorization=Bearer\\ ${TOK}").output
        self.assertIn("Added", out)
        self.assertEqual(json.loads(cfg_mod.global_path().read_text())["mcpServers"]["web"],
                         {"type": "http", "url": "https://example.com/mcp",
                          "headers": {"Authorization": "Bearer ${TOK}"}})
        self.assertIn("Removed", self.cmd("/mcp rm two").output)
        self.assertNotIn("two", self.h.mcp.clients)

    def test_busy_guard(self):
        blocked = ctl_mod._blocked_while_busy
        self.assertTrue(blocked("/mcp", ["/mcp", "restart stub"]))
        self.assertTrue(blocked("/mcp", ["/mcp", "off"]))
        self.assertFalse(blocked("/mcp", ["/mcp", "list"]))
        self.assertFalse(blocked("/mcp", ["/mcp"]))

    def test_help_lines_parse(self):
        usages = [c["usage"] for c in commands.help_commands() if c["cmd"] == "/mcp"]
        self.assertIn("/mcp on|off", usages)


class ProjectServer(Base):
    servers = {}

    def test_workdir_change_loads_the_project_file_and_waits_for_approval(self):
        other = self.home / "other"
        other.mkdir()
        (other / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "proj": {"command": sys.executable, "args": [STUB]}}}))
        out = self.cmd(f"/workdir {other}").output
        self.assertIn("/mcp approve", out)
        self.assertTrue(self.h.status_event().mcp_attention)
        self.cmd("/mcp approve proj")
        self.assertEqual(self.h.mcp.wait_ready(10), [])
        self.assertIn("mcp__proj__echo", self.names())


class Web(Base):
    def setUp(self):
        super().setUp()
        from harness.web.server import start_web_server
        self.c = ctl_mod.Controller(self.h)
        self.web = start_web_server(self.c, "127.0.0.1", 0, None)
        self.port = int(self.web.url.split(":")[2].split("/")[0])

    def tearDown(self):
        self.web.close()
        super().tearDown()

    def req(self, method, path, body=None, origin=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            if origin:
                headers["Origin"] = f"http://127.0.0.1:{self.port}"
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, (json.loads(data) if data.startswith(b"{") else data)

    def test_view_resources_and_attach(self):
        status, v = self.req("GET", "/api/mcp")
        self.assertEqual(status, 200)
        self.assertEqual(v["servers"][0]["name"], "stub")
        self.assertIn("mcp__stub__echo", [t["name"] for t in v["servers"][0]["tool_list"]])
        _, r = self.req("GET", "/api/mcp/resources?server=stub")
        self.assertEqual([x["uri"] for x in r["resources"]], ["notes://a", "notes://b"])
        _, a = self.req("GET", "/api/mcp/resource?server=stub&uri=notes://a")
        self.assertEqual(a["text"], "Note A text")
        status, _ = self.req("GET", "/api/mcp/resource?server=nope&uri=x")
        self.assertEqual(status, 502)
        _, st = self.req("GET", "/api/state")
        self.assertEqual(st["mcp_servers"], ["stub"])

    def test_add_server_needs_an_origin(self):
        body = {"action": "add", "name": "x", "entry": {"command": "true"}}
        status, _ = self.req("POST", "/api/mcp/servers", body, origin=False)
        self.assertEqual(status, 403)
        status, r = self.req("POST", "/api/mcp/servers", body)
        self.assertEqual(status, 200, r)
        self.assertIn("x", json.loads(cfg_mod.global_path().read_text())["mcpServers"])
        status, r = self.req("POST", "/api/mcp/servers", {"action": "remove", "name": "x"})
        self.assertEqual(status, 200, r)
        status, r = self.req("POST", "/api/mcp/servers", {"action": "add", "name": "bad name", "entry": {}})
        self.assertEqual(status, 400)


class Picker(Base):
    def test_tui_server_picker(self):
        t = T.TUI.__new__(T.TUI)
        t.harness = self.h
        sent = []

        class C:
            def submit(self_, text, source=""):
                sent.append(text)
                return commands.handle(text, self.h)

        t.controller = C()
        t._drain_events = lambda: None
        t._close_suggest = lambda: None
        t._open_picker("mcp")
        item = t._picker["items"][0]
        self.assertEqual(item["name"], "stub")
        self.assertIn("8 tools", item["group"])
        t._picker_key(" ")
        self.assertEqual(sent, ["/mcp disable stub"])
        self.assertFalse(t._picker["items"][0]["enabled"])
        t._picker_key("r")
        t._picker_key(13)
        self.assertEqual(sent[-1], "/mcp tools stub")
        self.assertIsNone(t._picker)
        self.assertEqual(T._tool_label("mcp__stub__echo"), "stub › echo")
        self.assertEqual(T._tool_label("read_file"), "read_file")


if __name__ == "__main__":
    unittest.main()
