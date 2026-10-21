"""run_command background=true (/run-mode new): a server or watcher is started,
waited on until it is ready, and left running; command_output reads its log as
it grows and stop_command ends it.  Before this, "server &" kept running
untracked after the call, /clear and the harness itself."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import run_store
from harness import tools
from harness.tools import dispatch
from harness.tools.builtin import run_command as run_command_tool

SERVER = "echo starting; sleep 0.3; echo ready on port 1; sleep 60"


class Background(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)
        self.store = run_store.RunStore(self.wd / "runs")

    def tearDown(self):
        self.store.stop_all()
        self.tmp.cleanup()

    def cmd(self, command, run_exit=None, **args):
        return dispatch("run_command", {"command": command, **args}, self.wd,
                        run_mode="new", run_store=self.store, run_exit=run_exit)

    def call(self, name, **args):
        return dispatch(name, args, self.wd, run_mode="new", run_store=self.store)

    def test_wait_for_returns_once_ready_and_leaves_it_running(self):
        t0 = time.monotonic()
        out = self.cmd(SERVER, background=True, wait_for="ready on")
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIn("[ready: its output matched `ready on`]", out)
        self.assertIn("ready on port 1", out)
        self.assertIn(", still running]", out)
        self.assertIn('stop_command(path="r1") stops it', out)
        self.assertEqual(self.store.running(), ["r1"])
        self.assertIn("still running]", self.call("command_output", path="r1"))

        proc = self.store._procs["r1"]
        out = self.call("stop_command", path="r1")
        self.assertTrue(out.startswith("Stopped r1."), out)
        self.assertIn("ready on port 1", out)
        self.assertIn("stopped by stop_command]", out)
        self.assertIsNotNone(proc.poll())
        self.assertEqual(self.store.running(), [])
        self.assertIn("is not running in the background (stopped by stop_command)",
                      self.call("stop_command", path="r1"))

    def test_python_output_is_unbuffered(self):
        out = self.cmd("python3 -c \"import time; print('ready'); time.sleep(60)\"",
                       background=True, wait_for="ready", timeout=5)
        self.assertIn("[ready: its output matched `ready`]", out)

    def test_without_wait_for_it_gets_a_few_seconds(self):
        with mock.patch.object(run_command_tool, "_BACKGROUND_SETTLE", 0.5):
            out = self.cmd("echo up; sleep 60", background=True)
        self.assertIn("up\n", out)
        self.assertEqual(self.store.running(), ["r1"])

    def test_a_command_that_ends_at_once_is_a_normal_run(self):
        run_exit = {}
        out = self.cmd("echo done; exit 2", run_exit=run_exit, background=True, wait_for="never")
        self.assertIn("exited during start-up", out)
        self.assertIn("exit code 2]", out)
        self.assertEqual(run_exit["code"], 2)
        self.assertEqual(self.store.running(), [])

    def test_wait_for_not_seen_in_time(self):
        out = self.cmd("echo booting; sleep 60", background=True, wait_for="ready", timeout=1)
        self.assertIn("`ready` did not appear in its output within 1s", out)
        self.assertEqual(self.store.running(), ["r1"])

    def test_a_command_that_ends_later_gets_its_exit_code(self):
        self.cmd("echo up; sleep 1; exit 3", background=True, wait_for="up")
        time.sleep(1.5)
        self.assertEqual(self.store.running(), [])
        self.assertIn("exit code 3]", self.call("command_output", path="r1"))

    def test_trailing_ampersand_becomes_background(self):
        out = self.cmd(SERVER + " &", wait_for="ready")
        self.assertIn("(ran without the trailing `&`, as background=true)", out)
        self.assertEqual(self.store.running(), ["r1"])
        self.assertEqual(self.cmd("echo a && echo b").split("\n")[:2], ["a", "b"])

    def test_classic_mode_refuses_it(self):
        out = dispatch("run_command", {"command": "sleep 60", "background": True}, self.wd,
                       run_mode="classic")
        self.assertTrue(out.startswith("ERROR: background=true needs /run-mode new"), out)

    def test_clear_and_exit_stop_everything(self):
        self.cmd(SERVER, background=True, wait_for="ready")
        proc = self.store._procs["r1"]
        self.store.clear()
        self.assertIsNotNone(proc.poll())

        self.cmd(SERVER, background=True, wait_for="ready")
        proc = self.store._procs["r1"]
        run_store._stop_everything()
        self.assertIsNotNone(proc.poll())

    def test_too_many_background_commands(self):
        with mock.patch.object(run_store, "MAX_BACKGROUND", 1):
            self.cmd(SERVER, background=True, wait_for="ready")
            out = self.cmd(SERVER, background=True, wait_for="ready")
        self.assertTrue(out.startswith("ERROR: 1 background commands are already running (r1)"),
                        out)

    def test_a_running_commands_log_is_never_pruned(self):
        self.cmd(SERVER, background=True, wait_for="ready")
        with mock.patch.object(run_store, "KEEP_LOGS", 2):
            for i in range(4):
                self.cmd(f"echo {i}")
        names = [p.name for p in self.store.logs()]
        self.assertIn("r1.log", names)
        self.assertIn("ready on port 1", self.call("command_output", path="r1"))

    def test_stop_command_on_a_finished_run(self):
        self.cmd("echo hi")
        out = self.call("stop_command", path="r1")
        self.assertTrue(out.startswith("r1 is not running in the background (exit code 0)."), out)
        self.assertTrue(self.call("stop_command", path="r9").startswith("ERROR: no saved"))

    def test_schema_follows_the_run_mode(self):
        names = lambda mode: [t["function"]["name"] for t in tools.offered("coding", run_mode=mode)
                              if t["function"]["name"] in ("run_command", "command_output",
                                                           "stop_command")]
        self.assertEqual(names("new"), ["run_command", "command_output", "stop_command"])
        self.assertEqual(names("classic"), ["run_command"])


if __name__ == "__main__":
    unittest.main()
