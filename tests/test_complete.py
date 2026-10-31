"""Input-box completion (harness/complete.py), shared by the TUI and the web UI:
every word of a /command from the /help usages, path arguments folder by
folder, and @ references that browse once a "/" is typed."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import complete as C
from harness.commands import help_commands


class FakeSources(C.Sources):
    """The real /help usages and a temp workspace; fixed live values."""

    def __init__(self, workdir: Path):
        self.commands = help_commands()
        self._wd = workdir

    @property
    def workdir(self):
        return self._wd

    def values(self, cmd, name, wait):
        if name == "server":
            return [("files", "ready"), ("github", "needs-approval")]
        if cmd == "/think":
            return [("off", ""), ("on", ""), ("high", "")]
        return []


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name).resolve()
        (self.wd / "src" / "sub").mkdir(parents=True)
        (self.wd / "src" / "a.py").write_text("x")
        (self.wd / "src" / "sub" / "b.py").write_text("y")
        (self.wd / "src" / "Zeta.txt").write_text("z")
        (self.wd / ".git").mkdir()
        (self.wd / "node_modules").mkdir()
        (self.wd / ".env").write_text("")
        self.src = FakeSources(self.wd)

    def tearDown(self):
        self.tmp.cleanup()

    def run_at(self, text, cursor=None, wait=True):
        return C.complete(text, len(text) if cursor is None else cursor, self.src, wait=wait)

    def values(self, text, **kw):
        got = self.run_at(text, **kw)
        return [] if got is None else [i.value for i in got.items]


class CommandNames(Base):
    def test_a_prefix_lists_the_usages_and_goes_on_to_the_arguments(self):
        got = self.run_at("/pl")
        self.assertEqual(got.start, 0)
        self.assertTrue(all(i.value == "/plan " and i.more for i in got.items))
        self.assertIn("/plan stories", [i.label for i in got.items])

    def test_a_command_typed_in_full_is_not_offered_again(self):
        self.assertEqual(self.values("/clear"), ["/clear-token"])
        self.assertIsNone(self.run_at("/compact"))

    def test_a_command_without_arguments_ends_there(self):
        [item] = [i for i in self.run_at("/retr").items]
        self.assertEqual((item.value, item.more), ("/retry", False))


class Levels(Base):
    def test_each_word_gets_its_choices(self):
        self.assertIn("stories ", self.values("/plan "))
        got = self.run_at("/plan stories ap")
        self.assertEqual(got.start, len("/plan stories "))
        # Prefix matches first, then the rest that contain it.
        self.assertEqual([(i.value, i.more) for i in got.items], [("approve ", True), ("cap ", True)])
        self.assertEqual(self.values("/plan stories approve "), ["each", "once"])
        self.assertEqual(self.values("/todos "), ["clear"])

    def test_a_word_some_usage_continues_after_goes_on(self):
        stories = [i for i in self.run_at("/plan ").items if i.label == "stories"][0]
        self.assertTrue(stories.more)

    def test_live_values(self):
        self.assertEqual(self.values("/think h"), ["high", "xhigh"])
        self.assertEqual(self.values("/mcp tools "), ["files", "github"])

    def test_a_subcommand_beats_a_placeholder(self):
        # "/tools <name> on|off" would also take "discover" as a tool name.
        self.assertEqual(self.values("/tools discover "), ["all", "extensions", "off"])

    def test_an_optional_word_may_be_skipped(self):
        self.assertEqual(self.values("/undo file "), ["force ", "src/"])
        self.assertEqual(self.values("/undo file force sr"), ["src/"])

    def test_nothing_for_a_number(self):
        self.assertIsNone(self.run_at("/net-cache drop "))


class PathArguments(Base):
    def test_folder_by_folder_inside_the_workspace(self):
        got = self.run_at("/read sr")
        self.assertEqual([(i.value, i.more) for i in got.items], [("src/", True)])
        self.assertEqual(self.values("/read src/"), ["src/sub/", "src/a.py", "src/Zeta.txt"])
        self.assertEqual(self.values("/read src/z"), ["src/Zeta.txt"])        # case-insensitive
        self.assertEqual(self.values("/grep pattern src/s"), ["src/sub/"])

    def test_hidden_and_skipped_names(self):
        self.assertEqual(self.values("/ls "), ["src/"])
        self.assertEqual(self.values("/ls ."), [".env"])          # a dot asks for dot-names; .git stays out

    def test_outside_the_workspace_offers_nothing(self):
        self.assertIsNone(self.run_at("/read ../"))
        self.assertIsNone(self.run_at("/read /etc/"))

    def test_workspace_takes_any_folder_and_only_folders(self):
        home = self.wd / "home"
        (home / "projects" / "momo").mkdir(parents=True)
        (home / "notes.txt").write_text("")
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(self.values("/workspace ~/"), ["~/projects/"])
            self.assertEqual(self.values("/workspace ~/projects/m"), ["~/projects/momo/"])
        self.assertEqual(self.values(f"/workspace {self.wd}/sr"), [f"{self.wd}/src/"])
        self.assertEqual(self.values("/workspace src/"), ["src/sub/"])        # relative to the workdir


class AtReferences(Base):
    def test_a_slash_browses_the_folder(self):
        got = self.run_at("look at @src/")
        self.assertEqual(got.start, len("look at "))
        self.assertEqual([(i.value, i.more) for i in got.items],
                         [("@src/sub/", True), ("`src/a.py` ", False), ("`src/Zeta.txt` ", False)])
        self.assertEqual(got.items[0].label, "@ src/sub/")

    def test_without_a_slash_a_fuzzy_search_with_folders(self):
        with mock.patch.object(C, "workspace_files", return_value=["src/a.py", "src/sub/b.py"]):
            values = self.values("@sub")
        self.assertIn("@src/sub/", values)
        self.assertIn("`src/sub/b.py` ", values)

    def test_the_tui_does_not_wait_for_the_walk(self):
        with mock.patch.object(C, "workspace_files_nowait", return_value=None):
            got = self.run_at("@a", wait=False)
        self.assertTrue(got.waiting)
        self.assertEqual(got.items[0].kind, "note")

    def test_only_the_text_before_the_cursor_counts(self):
        text = "see @src/ and more"
        got = self.run_at(text, cursor=len("see @src/"))
        self.assertEqual(got.start, len("see "))
        self.assertIsNone(self.run_at("hello world"))
        self.assertIsNone(self.run_at("/plan\nstories "))


if __name__ == "__main__":
    unittest.main()
