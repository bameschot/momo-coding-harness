"""evals/tdd_bench.py needs a live model; this keeps its grader honest without
one: the hidden checker must fail the untouched fixture and pass a reference
solution, and the fixture's own suite must be green to start from."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
TDD = REPO / "evals" / "tdd"

SOLUTION = '''

    def remove(self, name: str, qty: int) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        have = self._stock.get(name, 0)
        if have < qty:
            raise ValueError("not enough stock")
        if have == qty:
            del self._stock[name]
        else:
            self._stock[name] = have - qty

    def low_stock(self, threshold: int) -> list[str]:
        return sorted(n for n, c in self._stock.items() if c < threshold)
'''


def check(project: Path) -> dict:
    out = subprocess.run([sys.executable, "-I", str(TDD / "check.py"), str(project)],
                         capture_output=True, text=True, timeout=60).stdout
    return json.loads(out.strip().splitlines()[-1])


class HiddenCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name) / "project"
        shutil.copytree(TDD / "project", self.proj, ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_fixture_suite_is_green(self):
        r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                           cwd=self.proj, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_untouched_fixture_fails_every_case(self):
        self.assertEqual(check(self.proj)["passed"], 0)

    def test_reference_solution_passes(self):
        inv = self.proj / "shop" / "inventory.py"
        inv.write_text(inv.read_text() + SOLUTION)
        self.assertEqual(check(self.proj), {"passed": 8, "total": 8, "failures": {}})

    def test_off_by_one_threshold_is_caught(self):
        inv = self.proj / "shop" / "inventory.py"
        inv.write_text(inv.read_text() + SOLUTION.replace("c < threshold", "c <= threshold"))
        self.assertIn("t_low_stock_strict", check(self.proj)["failures"])


class Summary(unittest.TestCase):
    def test_summary_renders(self):
        sys.path.insert(0, str(REPO))
        # The bench points HOME at a temp dir on import; not for this process.
        with mock.patch.dict(os.environ, {"MOMO_EVAL_KEEP_HOME": "1"}):
            from evals import tdd_bench
        rec = {"kind": "tdd", "secs": 400.0, "plan_done": True, "calls": 20, "eval_tokens": 8000,
               "prompt_tokens": 300000, "stages": 4, "edits_after_tests": 2,
               "run_before_complete": 3, "refusals": {"missing tag": 1},
               "dropped_verify_step": False, "suite_ok": True,
               "hidden": {"passed": 8, "total": 8, "failures": {}}}
        text = tdd_bench.summary([rec, {**rec, "secs": 500.0, "refusals": {}}])
        self.assertIn("hidden 8/8        2/2", text)
        self.assertIn("seconds           450 (400-500)", text)
        self.assertIn("{'missing tag': 1}", text)

    def test_slow_suite_test_is_planted_in_the_copy_only(self):
        with mock.patch.dict(os.environ, {"MOMO_EVAL_KEEP_HOME": "1"}):
            from evals import tdd_bench
        compile(tdd_bench._SLOW_TEST.format(secs=0.1), "test_slow.py", "exec")
        self.assertFalse((TDD / "project" / "tests" / "test_slow.py").exists())


if __name__ == "__main__":
    unittest.main()
