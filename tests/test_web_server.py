"""Web server behaviour: static caching, HEAD, and mode switches during a turn.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import http.client
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import harness as hmod
from harness import session as session_mod
from harness.controller import Controller
from harness.web.server import start_web_server

from test_sessions_delete import FakeClient


class WebServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(hmod, "make_client", lambda *a, **k: FakeClient("http://fake", "fake")),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
        ]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)
        self.c = Controller(self.h)
        self.web = start_web_server(self.c, "127.0.0.1", 0, None)

    def tearDown(self):
        self.web.close()
        for p in reversed(self._patches):
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.web.port, timeout=5)
        hdrs = {"Host": "127.0.0.1"} | (headers or {})
        if body is not None:
            hdrs["Content-Type"] = "application/json"
            body = json.dumps(body)
        conn.request(method, path, body, hdrs)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r, data

    def test_export_full_download(self):
        self.h.messages += [{"role": "user", "content": "hi"},
                            {"role": "thinking", "content": "PONDER"}]
        r, data = self.request("GET", "/api/export")
        self.assertNotIn(b"PONDER", data)
        r, data = self.request("GET", "/api/export?full=1")
        self.assertEqual(r.status, 200)
        self.assertIn(b"PONDER", data)
        self.assertIn("-full.md", r.getheader("Content-Disposition"))

    def test_static_file_revalidates_with_etag(self):
        r, body = self.request("GET", "/app.js")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Cache-Control"), "no-cache")
        etag = r.getheader("ETag")
        self.assertTrue(etag and body)
        r, body = self.request("GET", "/app.js", headers={"If-None-Match": etag})
        self.assertEqual(r.status, 304)
        self.assertEqual(body, b"")

    def test_api_is_never_cached(self):
        r, _ = self.request("GET", "/api/state")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")

    def test_head_on_static_and_not_on_api(self):
        r, body = self.request("HEAD", "/")
        self.assertEqual(r.status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(r.getheader("Content-Length")), 0)
        r, _ = self.request("HEAD", "/api/events")
        self.assertEqual(r.status, 405)

    def test_idle_stream_sends_a_named_ping(self):
        # An SSE comment would be invisible to the page; its stall watchdog
        # needs an event it can listen for.
        from harness.web import server as server_mod
        with mock.patch.object(server_mod, "_KEEPALIVE_S", 0.05):
            conn = http.client.HTTPConnection("127.0.0.1", self.web.port, timeout=5)
            conn.request("GET", "/api/events", headers={"Host": "127.0.0.1"})
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            seen = b""
            while b"event: ping\ndata: \n\n" not in seen:
                line = r.fp.readline()
                self.assertTrue(line, "stream ended before a ping")
                seen += line
            conn.close()

    def test_mode_switch_refused_while_busy(self):
        self.h.set_mode("design")
        self.c._busy = True                         # a turn is running
        r, body = self.request("POST", "/api/mode", {"mode": "coding"})
        self.assertEqual(r.status, 200)
        self.assertFalse(json.loads(body)["ok"])
        self.assertEqual(self.h.mode, "design")
        self.assertEqual(self.c.submit("/code", source="web").view, {})
        self.assertEqual(self.h.mode, "design")
        self.c._busy = False
        r, body = self.request("POST", "/api/mode", {"mode": "coding"})
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.h.mode, "coding")

    def test_state_carries_the_upload_limit(self):
        _, body = self.request("GET", "/api/state")
        self.assertGreater(json.loads(body)["max_upload"], 0)

    def test_every_module_app_js_imports_is_served(self):
        import re
        from harness.web import server as server_mod
        app = (server_mod._STATIC_DIR / "app.js").read_text(encoding="utf-8")
        for name in re.findall(r'from "\./([\w-]+\.js)"', app):
            r, _ = self.request("GET", f"/{name}")
            self.assertEqual(r.status, 200, name)

    def test_state_carries_a_stories_plan(self):
        from harness.plan import Plan, Step
        from harness.stories import Criterion, Story, StorySet
        _, body = self.request("GET", "/api/state")
        self.assertIsNone(json.loads(body)["plan_view"])
        self.h.plan = Plan(title="P", kind="stories", steps=[Step(title="A", outcome="o", checks=["true"])])
        self.h.plan.story_sets[1] = StorySet(phase=1, approved=True, stories=[
            Story(id="S1", title="T", acceptance=[Criterion("ok", "true")])])
        try:
            _, body = self.request("GET", "/api/state")
            view = json.loads(body)["plan_view"]
            self.assertEqual(view["steps"][0]["stories"][0]["criteria"][0]["check"], "true")
        finally:
            self.h.plan = None

    def test_changes_drawer_api(self):
        from harness.tools import dispatch
        (self.home / "a.py").write_text("x = 1\n")
        store = self.h.checkpoints
        store.begin_turn(1, "edit a")
        dispatch("write_file", {"path": "a.py", "content": "x = 2\n"}, self.h.workdir,
                 checkpoints=store)
        for name in ("/diff.js", "/changes.js"):
            self.assertEqual(self.request("GET", name)[0].status, 200)
        r, body = self.request("GET", "/api/changes")
        data = json.loads(body)
        (t,) = data["turns"]
        (f,) = t["files"]
        self.assertEqual((t["label"], f["rel"], f["op"]), ("edit a", "a.py", "M"))
        self.assertEqual([x["rel"] for x in data["since_keep"]], ["a.py"])
        r, body = self.request("GET", f"/api/changes/diff?path={f['path']}&checkpoint={t['id']}")
        d = json.loads(body)
        self.assertNotIn("old", d)
        self.assertIn(["add", None, 1, "+x = 2"], d["body"])
        # Only a tracked path has a diff: the endpoint never reads any other file.
        r, _ = self.request("GET", f"/api/changes/diff?path={self.home / 'prefs.json'}")
        self.assertEqual(r.status, 404)
        self.assertEqual(self.request("GET", "/api/changes/diff?checkpoint=x")[0].status, 400)
        plan = json.loads(self.request("GET", f"/api/changes/preview?checkpoint={t['id']}")[1])
        self.assertEqual(plan["restore"], ["a.py"])
        self.c._busy = True
        r, _ = self.request("POST", "/api/undo", {"checkpoint": t["id"]})
        self.assertEqual(r.status, 409)
        self.c._busy = False
        self.assertEqual(self.request("POST", "/api/undo", {"checkpoint": "1"})[0].status, 400)
        r, body = self.request("POST", "/api/undo", {"checkpoint": t["id"]})
        self.assertIn("Reverted 1 file", json.loads(body)["message"])
        self.assertEqual((self.home / "a.py").read_text(), "x = 1\n")
        r, body = self.request("POST", "/api/keep", {})
        self.assertIn("Nothing to keep", json.loads(body)["message"])


if __name__ == "__main__":
    unittest.main()
