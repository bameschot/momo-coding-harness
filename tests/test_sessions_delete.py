"""Deleting saved sessions from the web UI (POST /api/sessions/delete).

session.SESSION_DIR is computed at import time, so it is patched to a temp
directory here; otherwise the tests would touch the real ~/.momo-harness.
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
from harness.llm.base import ChatResponse, LLMClient
from harness.web.server import start_web_server


class FakeClient(LLMClient):
    provider_name = "fake"

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return ChatResponse(content="ok")

    def context_length(self):
        return 8192

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class DeleteSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.sdir = self.home / "sessions"
        self.sdir.mkdir()
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: FakeClient("http://fake", "fake")
        self._patches = [mock.patch.object(session_mod, "SESSION_DIR", self.sdir),
                         mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                         mock.patch.dict(os.environ, {"HOME": str(self.home)})]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)
        self.current = self.h.session_path().stem
        for name in ("2026-01-01T00-00-00", "2026-01-02T00-00-00", self.current):
            (self.sdir / f"{name}.json").write_text(json.dumps({"messages": []}))
            (self.sdir / f"{name}.log").write_text("{}\n")
        (self.home / "outside.json").write_text("{}")
        self.web = start_web_server(Controller(self.h), "127.0.0.1", 0, None)

    def tearDown(self):
        self.web.close()
        hmod.make_client = self._orig
        for p in reversed(self._patches):
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def _post(self, body, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.web.port, timeout=5)
        c.request("POST", "/api/sessions/delete", json.dumps(body),
                  {"Content-Type": "application/json", **(headers or {})})
        r = c.getresponse()
        data = r.read()
        return r.status, (json.loads(data) if r.status == 200 else data)

    def test_deletes_json_and_log(self):
        status, res = self._post({"names": ["2026-01-01T00-00-00", "2026-01-02T00-00-00"]})
        self.assertEqual(status, 200)
        self.assertEqual(res["deleted"], ["2026-01-01T00-00-00", "2026-01-02T00-00-00"])
        self.assertEqual(res["skipped"], [])
        self.assertEqual(sorted(p.name for p in self.sdir.iterdir()),
                         [f"{self.current}.json", f"{self.current}.log"])

    def test_current_session_is_kept(self):
        status, res = self._post({"names": [self.current]})
        self.assertEqual(res["deleted"], [])
        self.assertEqual(res["skipped"][0]["reason"], "it is the current session")
        self.assertTrue((self.sdir / f"{self.current}.json").exists())

    def test_names_cannot_escape_the_folder(self):
        status, res = self._post({"names": ["../outside", "..", "/etc/passwd", ".hidden", "a/b", "missing"]})
        self.assertEqual(res["deleted"], [])
        reasons = {x["name"]: x["reason"] for x in res["skipped"]}
        for bad in ("../outside", "..", "/etc/passwd", ".hidden", "a/b"):
            self.assertEqual(reasons[bad], "not a session name")
        self.assertEqual(reasons["missing"], "not found")
        self.assertTrue((self.home / "outside.json").exists())

    def test_bad_body(self):
        self.assertEqual(self._post({"names": "x"})[0], 400)
        self.assertEqual(self._post({"names": []})[0], 400)
        self.assertEqual(self._post({})[0], 400)

    def test_cross_origin_refused(self):
        status, _ = self._post({"names": ["2026-01-01T00-00-00"]}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertTrue((self.sdir / "2026-01-01T00-00-00.json").exists())

    def test_listing_drops_deleted(self):
        self._post({"names": ["2026-01-01T00-00-00"]})
        c = http.client.HTTPConnection("127.0.0.1", self.web.port, timeout=5)
        c.request("GET", "/api/sessions")
        names = [s["name"] for s in json.loads(c.getresponse().read())["sessions"]]
        self.assertNotIn("2026-01-01T00-00-00", names)
        self.assertIn("2026-01-02T00-00-00", names)


if __name__ == "__main__":
    unittest.main()
