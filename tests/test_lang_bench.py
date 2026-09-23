"""Regression gate for the per-language code-index scorecard (evals/lang_bench.py).

Each evals/lang/<lang>/ project has hand-written ground truth; every expectation
not listed in its KNOWN_GAPS must hold.  A grammar-wheel upgrade that renames a
node type, or an extractor change that drops a construct, fails here with the
exact definition / search / caller / import that broke.  Runs in about a second.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evals import lang_bench  # noqa: E402

from harness import code_nav  # noqa: E402


@unittest.skipUnless(code_nav.AVAILABLE, "tree-sitter not installed")
class LanguageScorecard(unittest.TestCase):

    def test_every_language_meets_its_ground_truth(self):
        langs = lang_bench.langs_available()
        self.assertGreaterEqual(len(langs), 9)
        for lang in langs:
            with self.subTest(lang):
                r = lang_bench.run_lang(lang)
                self.assertEqual(r.failures, [], f"{lang}: " + "; ".join(f"{k}: {d}" for k, d in r.failures))
                self.assertEqual(r.fixed, [], f"{lang}: known gaps now pass — remove them "
                                              f"from KNOWN_GAPS: {r.fixed}")
                self.assertEqual(r.metrics["def_recall"], 1.0)
                self.assertEqual(r.metrics["search_top1"], 1.0)


if __name__ == "__main__":
    unittest.main()
