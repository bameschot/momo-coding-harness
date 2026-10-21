"""grep_files / search_file output: a hit on a huge line (minified code) must not
flood the context."""
import tempfile
import unittest
from pathlib import Path

from harness import tools
from harness.tools.builtin.grep_file import grep_file
from harness.tools.builtin.grep_files import grep_files
from harness.tools.common import MAX_GREP_LINE_CHARS


class GrepHitClipping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "bundle.js").write_text("a" * 100_000 + "class FooController{}" + "b" * 100_000 + "\n")
        (self.root / "short.kt").write_text("class BarController\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_long_line_is_clipped_around_the_match(self):
        out = grep_files("class .*Controller", ".", workdir=self.root)
        long_hit = next(l for l in out.splitlines() if l.startswith("bundle.js"))
        self.assertLess(len(long_hit), MAX_GREP_LINE_CHARS + 100)
        self.assertIn("FooController", long_hit)
        self.assertIn("[line is 200,021 chars]", long_hit)
        self.assertIn("short.kt:1: class BarController", out)

    def test_search_file_clips_too(self):
        out = grep_file("FooController", "bundle.js", workdir=self.root)
        self.assertLess(len(out), MAX_GREP_LINE_CHARS + 100)
        self.assertIn("FooController", out)


if __name__ == "__main__":
    unittest.main()
