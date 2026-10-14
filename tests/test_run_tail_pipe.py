"""run_command takes a trailing "| tail -N", "| head -N" or "| grep …" off the
command and shows those lines itself, so the exit code is the command's own and
not the pipe's last command's (tail's is 0 whatever the tests did).  The 9B
pipes most test runs this way."""
import tempfile
import unittest
from pathlib import Path

from harness import run_store
from harness import tdd
from harness.tools import _split_view_pipe, dispatch

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
            self.assertEqual(_split_view_pipe(cmd)[:2], (base, {"tail": n}), cmd)

    def test_head_and_grep_forms(self):
        for cmd, base, view in [
            ("pytest -v 2>&1 | head -80", "pytest -v", {"head": 80}),
            ("pytest | head -n 5", "pytest", {"head": 5}),
            ('cd /w && python -m unittest 2>&1 | grep -E "^(FAIL|ERROR):"',
             "cd /w && python -m unittest", {"grep": "^(FAIL|ERROR):", "context": 0}),
            ('pytest 2>&1 | grep -A4 "^FAIL"', "pytest", {"grep": "^FAIL", "context": 4}),
            ('pytest 2>&1 | grep -B2 -A10 "line 173, in x" | head -20', "pytest",
             {"grep": "line 173, in x", "context": 10}),
            ("pytest | grep -i 'assert'", "pytest", {"grep": "assert", "context": 0}),
            ("pytest|grep -e FAIL", "pytest", {"grep": "FAIL", "context": 0}),
        ]:
            self.assertEqual(_split_view_pipe(cmd)[:2], (base, view), cmd)

    def test_left_alone(self):
        for cmd in ["tail -f log", "cat x | tail -f log", "a | tail -2 | wc -l", "| tail -2",
                    "tail -20 build.log", 'echo "x | tail -2"', "pytest | grep -v x",
                    "pytest | grep -c FAIL", "pytest | grep -F x", "| grep x", "head -3 f",
                    'a | grep "$HOME"', r"a | grep 'f\(x\)'", 'echo "a | grep b"',
                    "pytest | grep x y"]:
            self.assertEqual(_split_view_pipe(cmd), (cmd, None, ""), cmd)

    def test_tdd_gate_accepts_a_piped_run(self):
        self.assertTrue(tdd.same_command('python -m unittest -v 2>&1 | grep -E "FAIL|OK"',
                                         "python -m unittest"))
        self.assertTrue(tdd.same_command("python -m unittest | head -40", "python -m unittest"))


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

    def test_grep_pipe_keeps_the_exit_code(self):
        for mode in ("classic", "new"):
            out, code = self.run_in(mode, command=FAILING + ' 2>&1 | grep -A1 "^a"')
            self.assertEqual(code, 3, mode)
            self.assertTrue(out.startswith("(ran without `2>&1 | grep -A1 \"^a\"` so the exit "
                                           "code is the command's own; showing the lines matching "
                                           "`^a` (case-insensitive) with 1 lines of context)"), out)
            body = out.split("\n", 1)[1]
            self.assertIn("1: a", body)
            self.assertIn("2- b", body)
            self.assertNotIn(" c\n", body + "\n")    # line 3 is outside the context

    def test_head_pipe_keeps_the_exit_code(self):
        for mode in ("classic", "new"):
            out, code = self.run_in(mode, command=FAILING + " | head -1")
            self.assertEqual(code, 3, mode)
            self.assertIn("showing the first 1 lines", out)
            body = out.split("\n", 1)[1]
            self.assertIn("a", body)
            self.assertNotIn("b\n", body)

    def test_models_grep_param_wins_over_a_piped_grep(self):
        out, _ = self.run_in("new", command="printf 'x\\ny\\n' | grep x", grep="y")
        self.assertFalse(out.startswith("(ran without"))

    def test_unsupported_grep_still_runs_through_the_shell(self):
        out, code = self.run_in("classic", command="printf 'x\\ny\\n' | grep -v x")
        self.assertEqual((out.strip(), code), ("y", 0))


if __name__ == "__main__":
    unittest.main()
