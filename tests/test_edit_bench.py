"""Regression gate for edit_file's matcher scorecard (evals/edit_bench.py).

Every mangled edit must come out exactly right or be refused — never written
somewhere wrong — with edit_features on and off; the must-refuse cases must
leave the file alone; and the features-on matcher must stay ahead of the plain
one.  Runs in well under a second.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evals import edit_bench  # noqa: E402

from harness import editing  # noqa: E402


class EditMatcherScorecard(unittest.TestCase):

    def test_no_wrong_edits_and_a_gain_over_the_plain_matcher(self):
        off = edit_bench.run(frozenset())
        on = edit_bench.run(editing.EDIT_FEATURES)
        for name, res in (("off", off), ("on", on)):
            with self.subTest(name):
                wrong = [f"{c['file']}/{c['mangle']}" for c in res["cases"] if c["verdict"] == "wrong"]
                self.assertEqual(wrong, [])
                self.assertEqual(res["neg_wrong"], 0, [n["old"] for n in res["neg"] if n["wrong"]])
        self.assertGreaterEqual(on["correct"], 63)
        self.assertGreater(on["correct"], off["correct"])
        # What the matcher still refuses, it refuses with the text to copy.
        self.assertEqual(on["refused"], on["hinted"])


if __name__ == "__main__":
    unittest.main()
