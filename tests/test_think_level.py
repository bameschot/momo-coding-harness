"""/think off|on|low|medium|high: the level is saved in prefs, mapped onto what
the served model takes (ThinkingCaps), and reported in the status event.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness.commands import handle as handle_command
from harness.llm.base import ChatResponse, LLMClient, ThinkingCaps

ON_OFF = ThinkingCaps(toggle=True, known=True)
GRADED = ThinkingCaps(toggle=True, levels=("low", "medium", "high"), can_disable=False, known=True)
QWEN38 = ThinkingCaps(toggle=True, levels=("low", "medium", "xhigh"), known=True)
UNKNOWN = ThinkingCaps()
NONE = ThinkingCaps(toggle=False, known=True)


class FakeClient(LLMClient):
    provider_name = "fake"
    caps = ON_OFF

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.sent = []

    def _probe_thinking(self):
        return FakeClient.caps

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.sent.append(self.wire_think(think))
        return ChatResponse(content="ok", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class ThinkLevel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeClient.caps = ON_OFF
        self._patches = [
            mock.patch.object(hmod, "make_client", lambda *a, **k: FakeClient("http://fake", "fake")),
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
        ]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def use(self, caps):
        FakeClient.caps = caps
        self.h.client.forget_thinking_caps()

    def cmd(self, text):
        return handle_command(text, self.h).output

    def test_set_saves_prefs_and_status(self):
        self.use(GRADED)
        self.assertEqual(self.cmd("/think high"), "Thinking: high")
        self.assertEqual(session_mod.load_prefs()["think"], "high")
        st = self.h.status_event()
        self.assertEqual((st.think_level, st.think_effective), ("high", "high"))
        self.assertEqual(st.think_choices, ["on", "low", "medium", "high"])

    def test_bad_value_rejected(self):
        self.assertIn("ERROR", self.cmd("/think ultra"))
        self.assertEqual(self.h.think_level, "on")
        self.assertNotIn("think", session_mod.load_prefs())

    def test_level_on_on_off_model_is_clamped_but_kept(self):
        out = self.cmd("/think high")
        self.assertIn("high → on", out)
        self.assertEqual(self.h.think_level, "high")     # honoured after a model switch
        self.assertEqual(self.h.status_event().think_effective, "on")
        self.use(GRADED)
        self.assertEqual(self.h.effective_think(), "high")

    def test_off_on_graded_model(self):
        self.use(GRADED)
        self.assertIn("off → low", self.cmd("/think off"))

    def test_on_on_graded_model_sends_its_default(self):
        self.use(GRADED)
        self.assertEqual(self.h.effective_think(), "on")
        self.assertEqual(self.h.think_note(), "")

    def test_level_maps_to_nearest_the_model_takes(self):
        self.use(QWEN38)
        self.assertIn("high → xhigh", self.cmd("/think high"))
        self.assertIn("takes low/medium/xhigh", self.h.think_note())
        self.assertEqual(self.cmd("/think xhigh"), "Thinking: xhigh")
        self.assertEqual(self.h.status_event().think_choices, ["off", "on", "low", "medium", "xhigh"])
        self.assertEqual(self.h.effective_think(), "xhigh")
        self.cmd("/think minimal")
        self.assertEqual(self.h.effective_think(), "low")

    def test_unknown_caps_says_so(self):
        self.use(UNKNOWN)
        self.assertIn("doesn't say", self.cmd("/think high"))
        self.assertIn("doesn't say", self.cmd("/think"))
        self.assertFalse(self.h.status_event().think_known)

    def test_caps_reread_each_turn(self):
        # e.g. llama-server was still loading at startup, or relaunched with another model
        self.use(UNKNOWN)
        self.cmd("/think high")
        FakeClient.caps = QWEN38          # the server now answers; nothing forgotten yet
        self.h.send("hi")
        self.assertEqual(self.h.client.sent[-1], "xhigh")
        self.assertTrue(self.h.status_event().think_known)

    def test_model_without_thinking(self):
        self.use(NONE)
        self.assertEqual(self.h.effective_think(), "n/a")
        self.assertIn("doesn't take a thinking setting", self.cmd("/think"))

    def test_turn_sends_the_level(self):
        self.use(GRADED)
        self.cmd("/think low")
        self.h.send("hi")
        self.assertEqual(self.h.client.sent[-1], "low")

    def test_old_bool_view(self):
        self.h.think = False
        self.assertEqual(self.h.think_level, "off")
        self.assertFalse(self.h.think)
        self.h.think = True
        self.assertEqual(self.h.think_level, "on")


if __name__ == "__main__":
    unittest.main()
