"""Background commands (run_command background=true) shown in both UIs: the
status event's `background` rows, the TUI's BG: N, /bg and /bg stop, and the
controller's tick that repaints when one ends by itself."""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import commands
from harness import controller as cmod
from harness import run_store
from harness import session as session_mod
from harness.events import StatusEvent
from harness.tools import dispatch

from test_run_output import _Scripted
from test_tui import make_tui

SERVER = "echo starting; sleep 0.3; echo ready on port 1; sleep 60"


class StoreView(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)
        self.store = run_store.RunStore(self.wd / "runs")

    def tearDown(self):
        self.store.stop_all()
        self.tmp.cleanup()

    def start(self, command=SERVER):
        return dispatch("run_command", {"command": command, "background": True,
                                        "wait_for": "ready on"},
                        self.wd, run_mode="new", run_store=self.store)

    def test_lists_a_running_command_until_it_is_stopped(self):
        before = time.time()
        self.start()
        [row] = self.store.background_view()
        self.assertEqual(row["id"], "r1")
        self.assertIn("sleep 60", row["command"])
        self.assertGreaterEqual(row["started"], before - 1)
        self.assertTrue(row["log"].endswith("r1.log"))
        dispatch("stop_command", {"path": "r1"}, self.wd, run_mode="new", run_store=self.store)
        self.assertEqual(self.store.background_view(), [])

    def test_one_that_exits_by_itself_drops_out(self):
        self.start("echo ready on; sleep 0.5")
        deadline = time.monotonic() + 10
        while self.store.background_view() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.store.background_view(), [])

    def test_long_commands_are_shortened(self):
        self.start("echo ready on; sleep 60; " + "true; " * 30)
        [row] = self.store.background_view()
        self.assertLessEqual(len(row["command"]), 60)
        self.assertTrue(row["command"].endswith("…"))


class HarnessSide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                        mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
                        mock.patch.object(hmod, "make_client",
                                          lambda *a, **k: _Scripted("http://fake", "fake"))]
        for p in self.patches:
            p.start()
        self.wd = self.home / "project"
        self.wd.mkdir()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.run_mode = "new"

    def tearDown(self):
        if self.h._run_store is not None:
            self.h._run_store.stop_all()
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def start(self, command=SERVER):
        return dispatch("run_command", {"command": command, "background": True,
                                        "wait_for": "ready on"},
                        self.wd, run_mode="new", run_store=self.h.run_store)

    def cmd(self, text):
        return commands.handle(text, self.h).output

    def test_status_event_without_a_store_is_empty_and_creates_nothing(self):
        self.assertEqual(self.h.status_event().background, [])
        self.assertIsNone(self.h._run_store)

    def test_status_event_lists_running_commands(self):
        self.start()
        self.assertEqual([r["id"] for r in self.h.status_event().background], ["r1"])

    def test_bg_lists_and_stops(self):
        self.assertEqual(self.cmd("/bg"), "No background commands running.")
        self.start()
        self.start()
        out = self.cmd("/bg")
        self.assertIn("Background commands running (2):", out)
        self.assertIn("r1", out)
        self.assertIn("sleep 60", out)
        self.assertIn("r1.log", out)
        self.assertIn("Stopped r1.", self.cmd("/bg stop r1"))
        self.assertEqual([r["id"] for r in self.h.status_event().background], ["r2"])
        self.assertIn("r9 is not a running background command (running: r2)",
                      self.cmd("/bg stop r9"))
        self.assertIn("Stopped 1 background command: r2.", self.cmd("/bg stop all"))
        self.assertEqual(self.h.status_event().background, [])

    def test_bg_usage_and_classic_note(self):
        self.assertIn("Usage:", self.cmd("/bg frobnicate"))
        self.h.run_mode = "classic"
        self.assertIn("need /run-mode new", self.cmd("/bg"))

    def test_bg_is_in_help(self):
        self.assertIn("/bg", [c["cmd"] for c in commands.help_commands()])

    def test_controller_tick_emits_only_on_change(self):
        c = cmod.Controller(self.h)
        with mock.patch.object(self.h, "emit_status") as emit:
            c._bg_tick()
            emit.assert_not_called()          # nothing running, nothing changed
            self.start()
            c._bg_tick()
            self.assertEqual(emit.call_count, 1)
            c._bg_tick()
            self.assertEqual(emit.call_count, 1)
            self.h._run_store.stop_all()
            c._bg_tick()
            self.assertEqual(emit.call_count, 2)


class TuiStatus(unittest.TestCase):
    def apply(self, background):
        t = make_tui()
        t.harness = mock.Mock()
        t._st_dir = "/w"
        t._st_provider = ""
        t._reload_picker = t._reload_changes = lambda: None
        t._apply_status(StatusEvent(mode="coding", model="m", workdir="/w", ctx_pct=1,
                                    ctx_color="normal", background=background))
        return t._st_extra

    def test_bg_count_in_the_status_bar(self):
        self.assertNotIn("BG:", self.apply([]))
        rows = [{"id": "r1", "command": "x", "started": 0, "log": "r1.log"},
                {"id": "r2", "command": "y", "started": 0, "log": "r2.log"}]
        self.assertIn("BG: 2", self.apply(rows))


if __name__ == "__main__":
    unittest.main()
