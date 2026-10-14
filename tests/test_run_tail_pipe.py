"""run_command takes a trailing "| tail -N" off the command and shows the last
N lines itself, so the exit code is the command's own and not tail's (which is
0 whatever the tests did).  The 9B pipes most test runs this way."""
import tempfile
import unittest
from pathlib import Path

from harness import run_store
from harness.tools import _split_tail, dispatch

FAILING = "printf 'a\\nb\\nc\\n'; exit 3"


class SplitTail(unittest.TestCase):
    def test_rewritten_forms(self):
        for cmd, base, n in [
            ("pytest -q 2>&1 | tail -20", "pytest -q", 20),
            ("pytest -q | tail -n 5", "pytest -q", 5),
            ("pytest -q |& tail -n5", "pytest -q", 5),
            ("pytest -q 2>&1 | tail --lines=7", "pytest -q", 7),
            ('python3 -m unittest -v 2>&1 | tail -40; echo "exit=${PIPESTATUS[0]}"',
             "python3 -m unittest -v", 40),
        ]:
            self.assertEqual(_split_tail(cmd)[:2], (base, n), cmd)

    def test_left_alone(self):
        for cmd in ["pytest | grep x", "tail -f log", "cat x | tail -f log", "a | head -3",
                    "a | tail -2 | wc -l", "| tail -2", "tail -20 build.log",
                    'echo "x | tail -2"']:
            self.assertEqual(_split_tail(cmd), (cmd, None, ""), cmd)


class RunCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_in(self, mode, **args):
        store = run_store.RunStore(self.wd / "runs") if mode == "new" else None
        code: dict = {}
        out = dispatch("run_command", args, self.wd, run_mode=mode, run_store=store, run_exit=code)
        return out, code.get("code")

    def test_failing_command_keeps_its_exit_code(self):
        for mode in ("classic", "new"):
            out, code = self.run_in(mode, command=FAILING + " 2>&1 | tail -2")
            self.assertEqual(code, 3, mode)
            self.assertTrue(out.startswith("(ran without `2>&1 | tail -2` so the exit code is "
                                           "the command's own; showing the last 2 lines)"), out)
            self.assertIn("b\nc", out)
            self.assertNotIn("a\n", out.split("\n", 1)[1])
            self.assertIn("[exit code: 3]" if mode == "classic" else "exit code 3", out)

    def test_pipestatus_echo_goes_too(self):
        for mode in ("classic", "new"):
            out, code = self.run_in(mode, command=FAILING + ' 2>&1 | tail -1; echo "exit=${PIPESTATUS[0]}"')
            self.assertEqual(code, 3, mode)
            self.assertNotIn("exit=", out.split("\n", 1)[1])     # the note names it; nothing ran it

    def test_smaller_tail_wins(self):
        out, _ = self.run_in("new", command=FAILING + " | tail -3", tail=1)
        self.assertIn("showing the last 1 lines", out)
        self.assertNotIn("b\n", out.split("\n", 1)[1])

    def test_grep_pipe_is_not_touched(self):
        out, code = self.run_in("classic", command="printf 'x\\ny\\n' | grep y")
        self.assertEqual((out.strip(), code), ("y", 0))


if __name__ == "__main__":
    unittest.main()
