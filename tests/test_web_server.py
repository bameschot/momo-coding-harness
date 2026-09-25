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


if __name__ == "__main__":
    unittest.main()
