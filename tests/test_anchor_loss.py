"""An edit that removes a definition while it adds a different one is refused
before anything is written: the model used the old code as an anchor to place
new code and left it out of new_string.  Sending the same call again applies
it.  Checked on every language project in evals/lang (with the parser, and
with the parser off: the header rule), and on edits a 26B made in the multi-part
bench (tests/golden/anchor_edits.json)."""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import code_nav, editing
from harness.tools import dispatch

REPO = Path(__file__).resolve().parent.parent
LANGS = REPO / "evals" / "lang"
CORPUS = Path(__file__).parent / "golden" / "anchor_edits.json"
NAV = unittest.skipUnless(code_nav.AVAILABLE, "tree-sitter not installed")


def _word(name):
    return rf"(?<![\w$]){re.escape(name)}(?![\w$])"


def _same_shape(a_src, b_src):
    """Both headers have the same indentation and first word (`def`/`def`,
    `public`/`public`): the cases the header rule is meant for."""
    ha = editing._header(editing._first_line(a_src))
    hb = editing._header(editing._first_line(b_src))
    return bool(ha and hb and ha[:2] == hb[:2])


def _pairs(fits=lambda a_src, b_src: True):
    """(language, project file, A, B): two callable definitions in one file
    whose names are unique among its definitions and absent from each other."""
    out = []
    for proj in sorted(p / "project" for p in LANGS.iterdir() if (p / "project").is_dir()):
        found = None
        for f in sorted(proj.rglob("*")):
            lang = code_nav.language_for(f) if f.is_file() else None
            if not lang:
                continue
            parsed = code_nav.parse_bytes(lang, f.read_bytes())
            counts = {}
            for s in parsed.symbols:
                counts[s.name] = counts.get(s.name, 0) + 1
            calls = [s for s in parsed.symbols if s.kind in code_nav.CALLABLE_KINDS
                     and s.end > s.start and counts[s.name] == 1]

            def src(s):
                return "\n".join(parsed.lines[s.start - 1:s.end])
            for a in calls:
                for b in calls:
                    if (a is not b and not re.search(_word(a.name), src(b))
                            and not re.search(_word(b.name), src(a))
                            and not (a.start <= b.start <= a.end or b.start <= a.start <= b.end)
                            and fits(src(a), src(b))):
                        found = (proj.parent.name, f.relative_to(proj), src(a), src(b), a.name, b.name)
                        break
                if found:
                    break
            if found:
                break
        if found:
            out.append(found)
    return out


@NAV
class AcrossLanguages(unittest.TestCase):
    """For each language: A is the anchor, C a different definition (B under
    the new name `added_thing`)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        editing._REFUSED.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def place(self, lang, rel):
        dest = self.root / lang / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(LANGS / lang / "project" / rel, dest)
        return f"{lang}/{rel}", dest

    def run_tool(self, tool, **args):
        return dispatch(tool, args, self.root)

    def test_every_language_has_a_case(self):
        self.assertGreaterEqual(len(_pairs()), 12)

    def check(self, parser: bool):
        for lang, rel, a_src, b_src, a_name, b_name in _pairs():
            c_src = re.sub(_word(b_name), "added_thing", b_src)
            with self.subTest(lang=lang, parser=parser):
                path, dest = self.place(lang, rel)
                original = dest.read_text()
                header_case = (parser or editing._header_swap(a_src, c_src) is not None)
                # The anchor mistake: refused, nothing written; sent again, applied.
                out = self.run_tool("edit_file", path=path, old_string=a_src, new_string=c_src)
                if header_case:
                    self.assertTrue(out.startswith("ERROR: not written"), out)
                    self.assertEqual(dest.read_text(), original)
                    again = self.run_tool("edit_file", path=path, old_string=a_src, new_string=c_src)
                    self.assertTrue(again.startswith("Applied as sent (repeated)"), again)
                    self.assertNotIn(a_src, dest.read_text())
                dest.write_text(original)
                # Keeping the anchor, before or after the new code: applied.
                for new in (a_src + "\n\n" + c_src, c_src + "\n\n" + a_src):
                    out = self.run_tool("edit_file", path=path, old_string=a_src, new_string=new)
                    self.assertFalse(out.startswith("ERROR"), out)
                    dest.write_text(original)
                # A rename and a plain deletion: applied.
                renamed = re.sub(_word(a_name), "renamed_thing", a_src)
                for new in (renamed, ""):
                    out = self.run_tool("edit_file", path=path, old_string=a_src, new_string=new)
                    self.assertFalse(out.startswith("ERROR"), (new[:40], out))
                    dest.write_text(original)
                if parser:
                    # The same through edit_files and replace_symbol.
                    out = self.run_tool("edit_files", edits=[{"path": path, "old_string": a_src,
                                                              "new_string": c_src}])
                    self.assertTrue(out.startswith("ERROR: not written"), out)
                    self.assertEqual(dest.read_text(), original)
                    out = self.run_tool("replace_symbol", path=path, name=a_name, new_source=c_src)
                    self.assertTrue(out.startswith("ERROR: not written"), out)
                    self.assertEqual(dest.read_text(), original)

    def test_with_the_parser(self):
        self.check(parser=True)

    def test_without_the_parser(self):
        with mock.patch.object(editing, "_lang", return_value=None):
            self.check(parser=False)

    def test_header_rule_fires_on_every_same_shape_pair(self):
        cases = _pairs(_same_shape)
        self.assertGreaterEqual(len(cases), 10, [c[0] for c in cases])
        for lang, _rel, a_src, b_src, _a, b_name in cases:
            with self.subTest(lang=lang):
                self.assertIsNotNone(
                    editing._header_swap(a_src, re.sub(_word(b_name), "added_thing", b_src)))


class HeaderRule(unittest.TestCase):
    def test_swaps(self):
        for old, new in (("    def restock(self, sku, qty):\n        pass", "    def remove(self, sku):\n        pass"),
                         ("fn area(&self) -> f64 {\n  1.0\n}", "fn perimeter(&self) -> f64 {\n  2.0\n}"),
                         ("func Add(a int) int {\n  return a\n}", "func Sub(a int) int {\n  return -a\n}"),
                         ("public int total() {\n  return 1;\n}", "public int count() {\n  return 2;\n}"),
                         ("class Basket:\n    x = 1", "class Order:\n    y = 2")):
            with self.subTest(old=old[:20]):
                self.assertIsNotNone(editing._header_swap(old, new))

    def test_not_swaps(self):
        for old, new in (("def f(a):\n  return a", "def g(a):\n  return a"),                  # rename
                         ("def f(a):\n  return a", "def f(a, b):\n  return a + b"),            # same name
                         ("def f(a):\n  return a", "def f(a):\n  return a\n\ndef g():\n  pass"),  # kept
                         ("    if x:\n        y()", "    if z:\n        w()"),                    # not a header
                         ("def f(a):\n  return a", "public int g() {\n}"),                     # other kind
                         ("x = 1\ny = 2", "z = 3")):
            with self.subTest(old=old[:20], new=new[:20]):
                self.assertIsNone(editing._header_swap(old, new))


@NAV
class RecordedEdits(unittest.TestCase):
    """Edits a 26B made in the multi-part bench, with the file before each."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rows = json.loads(CORPUS.read_text())["edits"]
        editing._REFUSED.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def refused(self, row):
        p = self.root / row["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(row["before"])
        editing._REFUSED.clear()
        out = dispatch("edit_file", {"path": row["path"], "old_string": row["old_string"],
                                     "new_string": row["new_string"],
                                     "replace_all": row["replace_all"]}, self.root)
        return out.startswith("ERROR: not written")

    def test_the_parser_refuses_exactly_the_anchor_losses(self):
        for row in self.rows:
            with self.subTest(run=row["run"], path=row["path"]):
                self.assertEqual(self.refused(row), row["anchor_loss"])

    def test_the_header_rule_alone(self):
        with mock.patch.object(editing, "_lang", return_value=None):
            got = [(self.refused(r), r["anchor_loss"]) for r in self.rows]
        caught = sum(1 for r, want in got if r and want)
        extra = sum(1 for r, want in got if r and not want)
        self.assertEqual(extra, 0)
        self.assertGreaterEqual(caught, sum(want for _, want in got) - 2)


if __name__ == "__main__":
    unittest.main()
