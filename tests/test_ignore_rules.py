"""The index filter's gitignore matcher, checked against git itself where git
is installed (so the table does not only confirm the matcher's own reading)."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import ignore_rules as ir

# (gitignore text, [(path, is_dir)])
CASES = [
    ("*.log\n", [("a.log", False), ("x/y/b.log", False), ("a.log.txt", False), ("logs", True)]),
    ("build/\n", [("build", True), ("build/x.py", False), ("src/build", True),
                  ("src/build/y.c", False)]),
    ("tmp/\n", [("tmp", False), ("x/tmp", False)]),
    ("/build\n", [("build", True), ("build/x", False), ("src/build", True)]),
    ("doc/*.txt\n", [("doc/a.txt", False), ("doc/sub/a.txt", False), ("x/doc/a.txt", False)]),
    ("**/cache\n", [("cache", True), ("a/b/cache", True), ("a/cache/f", False)]),
    ("a/**/z\n", [("a/z", False), ("a/b/z", False), ("a/b/c/z", False), ("b/a/z", False)]),
    ("out/**\n", [("out", True), ("out/x", False), ("out/z/y", False)]),
    ("*.py\n!keep.py\n", [("a.py", False), ("keep.py", False), ("s/keep.py", False)]),
    ("gen/\n!gen/keep.py\n", [("gen/keep.py", False), ("gen/x.py", False)]),
    ("/*\n!/src/\n", [("README.md", False), ("src", True), ("src/a.py", False), ("lib", True),
                      ("lib/b.py", False)]),
    ("f?o.[ch]\n", [("foo.c", False), ("fxo.h", False), ("fooo.c", False), ("foo.x", False)]),
    ("[!a]*.md\n", [("b.md", False), ("a.md", False)]),
    ("\\#hash\n\\!bang\n", [("#hash", False), ("!bang", False)]),
    ("# comment\n\nspace\\ \n", [("# comment", False), ("space ", False), ("space", False)]),
]


def _git_ignored(text: str, paths) -> set[str]:
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", d], check=True)
        Path(d, ".gitignore").write_text(text)
        for p, is_dir in paths:
            full = Path(d, p)
            if is_dir:
                full.mkdir(parents=True, exist_ok=True)
            else:
                full.parent.mkdir(parents=True, exist_ok=True)
                if not full.is_dir():
                    full.write_text("x")
        inp = "".join(p + ("/" if is_dir else "") + "\0" for p, is_dir in paths)
        r = subprocess.run(["git", "-C", d, "check-ignore", "--no-index", "-z", "--stdin"],
                           input=inp.encode(), capture_output=True)
        return {s.rstrip("/") for s in r.stdout.decode().split("\0") if s}


class Matcher(unittest.TestCase):

    def test_against_git(self):
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        for text, paths in CASES:
            want = _git_ignored(text, paths)
            rules = ir.Rules.parse(text)
            for p, is_dir in paths:
                with self.subTest(rules=text, path=p):
                    self.assertEqual(rules.excluded(p, is_dir), p in want)

    def test_git_dir_always_excluded(self):
        r = ir.Rules.parse("")
        self.assertTrue(r.excluded(".git/config"))
        self.assertTrue(r.dir_excluded("sub/.git"))
        self.assertFalse(r.excluded("a.py"))

    def test_blank_and_comment_lines_are_not_rules(self):
        self.assertEqual(len(ir.Rules.parse("# x\n\n   \n*.o\n")), 1)


class Seed(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def w(self, rel, text=""):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_nested_gitignores_are_rewritten_to_their_directory(self):
        (self.root / ".git" / "info").mkdir(parents=True)
        self.w(".git/info/exclude", "# c\nlocal.txt\n")
        self.w(".gitignore", "*.log\n")
        self.w("pkg/.gitignore", "tmp/\n/only_here.py\n!keep.log\nsub/x.py\n")
        self.w("node_modules/dep/.gitignore", "never_read\n")
        text = ir.seed_text(self.root)
        self.assertIn("/pkg/**/tmp/", text)
        self.assertIn("/pkg/only_here.py", text)
        self.assertIn("!/pkg/**/keep.log", text)
        self.assertIn("/pkg/sub/x.py", text)
        self.assertNotIn("never_read", text)          # inside an excluded folder
        self.assertNotIn("\n.*\n", text)              # a repo keeps hidden files
        r = ir.Rules.parse(text)
        for path, want in [("a.log", True), ("pkg/keep.log", False), ("pkg/a/tmp/f", True),
                           ("tmp/f", False), ("pkg/only_here.py", True),
                           ("pkg/a/only_here.py", False), ("pkg/sub/x.py", True),
                           ("local.txt", True), ("node_modules/x.js", True),
                           (".github/ci.yml", False)]:
            with self.subTest(path=path):
                self.assertEqual(r.excluded(path), want)

    def test_nested_rewrite_matches_git(self):
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.w("pkg/.gitignore", "tmp/\n/top.py\n*.o\n!keep.o\n")
        paths = ["pkg/tmp/a", "pkg/x/tmp/b", "tmp/c", "pkg/top.py", "pkg/x/top.py",
                 "a.o", "pkg/a.o", "pkg/x/keep.o", "pkg/y.o"]
        for p in paths:
            self.w(p, "x")
        r = subprocess.run(["git", "-C", str(self.root), "check-ignore", "--no-index",
                            "-z", "--stdin"], input="\0".join(paths).encode(), capture_output=True)
        want = {s for s in r.stdout.decode().split("\0") if s}
        rules = ir.Rules.parse(ir.seed_text(self.root))
        for p in paths:
            with self.subTest(path=p):
                self.assertEqual(rules.excluded(p), p in want)

    def test_outside_git_hidden_files_are_excluded(self):
        self.assertIn("\n.*\n", ir.seed_text(self.root))


if __name__ == "__main__":
    unittest.main()
