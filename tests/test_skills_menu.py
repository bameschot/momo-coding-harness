"""The web UI's Skills menu: Harness.skill_choices(), the active skills on the
status event, and GET /api/skills.
"""
import http.client
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import commands
from harness import controller as ctl_mod
from harness import session as session_mod
from harness.llm.base import ChatResponse, LLMClient


class _Fake(LLMClient):
    provider_name = "fake"

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return ChatResponse(content="Done.", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class Skills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.skills = home / "skills"
        self.skills.mkdir()
        (self.skills / "alpha.md").write_text(
            "# Skill: Alpha Things\n\n## Core rules\n\n- one\n\n## Errors (the usual ones)\n\n"
            "```\ncode line\n```\n\n## Testing — briefly\n")
        (self.skills / "beta.md").write_text(
            "# Skill: Beta\n\nDo the **beta** thing as one\nchange set.\n\n## Steps\n")
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", home / "prefs.json"),
                        mock.patch.object(session_mod, "SESSION_DIR", home / "sessions"),
                        mock.patch.object(hmod, "_SKILLS_DIR", self.skills),
                        mock.patch.object(hmod, "make_client", lambda *a, **k: _Fake("http://fake", "fake")),
                        mock.patch.object(hmod.Harness, "_start_index", lambda self: None)]
        for p in self.patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=home)

    def tearDown(self):
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_choices(self):
        commands.handle("/load-skill beta", self.h)
        a, b = self.h.skill_choices()
        self.assertEqual((a["name"], a["title"], a["active"]), ("alpha", "Alpha Things", False))
        # Section headings without "Core rules", asides and code.
        self.assertEqual(a["summary"], "Errors · Testing")
        self.assertEqual((b["title"], b["summary"], b["active"]), ("Beta", "Do the beta thing as one change set.", True))
        self.assertGreater(b["tokens"], 0)

    def test_status_carries_active_skills_in_order(self):
        commands.handle("/load-skill beta", self.h)
        commands.handle("/load-skill alpha", self.h)
        self.assertEqual(self.h.status_event().skills, ["beta", "alpha"])
        commands.handle("/unload-skill beta", self.h)
        self.assertEqual(self.h.status_event().skills, ["alpha"])

    def test_api(self):
        from harness.web.server import start_web_server
        web = start_web_server(ctl_mod.Controller(self.h), "127.0.0.1", 0, None)
        try:
            port = int(web.url.split(":")[2].split("/")[0])
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/skills", headers={"Host": f"127.0.0.1:{port}"})
            data = json.loads(conn.getresponse().read())
            self.assertEqual([s["name"] for s in data["skills"]], ["alpha", "beta"])
            conn.request("GET", "/skills.js", headers={"Host": f"127.0.0.1:{port}"})
            r = conn.getresponse()
            self.assertEqual((r.status, r.getheader("Content-Type").split(";")[0]), (200, "text/javascript"))
            r.read()
            conn.close()
        finally:
            web.close()


if __name__ == "__main__":
    unittest.main()
