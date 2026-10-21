"""The workspace's .momo/ folder: scratchpad (scratch_write), plan file, run
logs — git-ignored, never indexed, skipped by project-wide search, and the
scratch tool cannot write outside it."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import code_index as ci
from harness import commands, momo_dir, run_store
from harness import session as session_mod
from harness.ignore_rules import Rules
from harness.plan import LEGACY_PLAN_FILENAME, Plan, Step, read_plan_file, write_plan_file
from harness.plan import delete_plan_file
from harness.plan_runner import KEEPS_GREEN
from harness.prompts import MODES
from harness.tools import PLAN_EXECUTE_TOOLS, PLAN_TDD_EXECUTE_TOOLS, dispatch

from test_run_output import _Scripted


class _Workdir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name).resolve() / "project"
        self.wd.mkdir()
        self.outside = Path(self.tmp.name).resolve() / "outside"
        self.outside.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, content="x", **kw):
        return dispatch("scratch_write", {"path": path, "content": content, **kw}, self.wd)


class ScratchWrite(_Workdir):
    def test_writes_inside_and_creates_the_gitignore(self):
        out = self.write("probe.py", "print(1)\n")
        self.assertEqual(out, "Written: .momo/scratch/probe.py (1 line, 9 bytes)")
        self.assertEqual((self.wd / ".momo/scratch/probe.py").read_text(), "print(1)\n")
        self.assertIn("*", (self.wd / ".momo/.gitignore").read_text().splitlines())

    def test_accepted_path_forms(self):
        for path in ("a.txt", ".momo/scratch/b.txt", "./.momo/scratch/c.txt",
                     str(self.wd / ".momo/scratch/d.txt"), "out/deep/e.txt"):
            with self.subTest(path=path):
                self.assertFalse(self.write(path).startswith("ERROR"), path)
        names = sorted(p.name for p in (self.wd / ".momo/scratch").rglob("*") if p.is_file())
        self.assertEqual(names, ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"])

    def test_append(self):
        self.write("log.txt", "one\n")
        self.assertTrue(self.write("log.txt", "two\n", append=True).startswith("Appended to"))
        self.assertTrue(self.write("log.txt", "three\n", append="true").startswith("Appended"))
        self.assertEqual((self.wd / ".momo/scratch/log.txt").read_text(), "one\ntwo\nthree\n")

    def test_escapes_are_refused(self):
        (self.wd / "src.py").write_text("keep\n")
        for path in ("../x.txt", "../../src.py", "../../../outside/x", "/etc/passwd",
                     str(self.outside / "x"), str(self.wd / "src.py"), "a\0b", "", ".",
                     "sub/../../x"):
            with self.subTest(path=path):
                self.assertTrue(self.write(path).startswith("ERROR"), path)
        self.assertEqual((self.wd / "src.py").read_text(), "keep\n")
        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertFalse((self.wd / ".momo/x.txt").exists())

    def test_symlink_inside_pointing_out_is_refused(self):
        momo_dir.ensure(self.wd, "scratch")
        os.symlink(self.outside, self.wd / ".momo/scratch/link")
        os.symlink(self.wd / "victim.py", self.wd / ".momo/scratch/file-link")
        self.assertTrue(self.write("link/x.txt").startswith("ERROR"))
        self.assertTrue(self.write("file-link").startswith("ERROR"))
        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertFalse((self.wd / "victim.py").exists())

    def test_symlinked_momo_dir_is_refused(self):
        os.symlink(self.outside, self.wd / ".momo")
        out = self.write("x.txt")
        self.assertTrue(out.startswith("ERROR") and "symlink" in out, out)
        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertIsInstance(momo_dir.resolve_scratch("x.txt", self.wd), str)

    def test_symlinked_scratch_dir_is_refused(self):
        (self.wd / ".momo").mkdir()
        os.symlink(self.outside, self.wd / ".momo/scratch")
        self.assertTrue(self.write("x.txt").startswith("ERROR"))
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_content_guards_apply(self):
        out = self.write("x.txt", "[written to x.txt]")
        self.assertTrue(out.startswith("ERROR"), out)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_ignores_the_whole_folder(self):
        subprocess.run(["git", "init", "-q", str(self.wd)], check=True)
        self.write("probe.py")
        write_plan_file(Plan(title="t", steps=[Step(title="s")]), self.wd)
        store = run_store.RunStore(run_store.session_root(self.wd, "s1"))
        store.new_log()[1].write_text("log\n")
        status = subprocess.run(["git", "-C", str(self.wd), "status", "--porcelain",
                                 "--untracked-files=all"], capture_output=True, text=True).stdout
        self.assertEqual(status, "")
        for rel in (".momo/scratch/probe.py", ".momo/plan.md", ".momo/runs/s1/r1.log"):
            self.assertTrue((self.wd / rel).exists(), rel)


class WriteToolsUnderMomo(_Workdir):
    def test_write_file_into_scratch_gets_the_gitignore_too(self):
        out = dispatch("write_file", {"path": ".momo/scratch/n.md", "content": "# n\n"}, self.wd)
        self.assertFalse(out.startswith("ERROR"), out)
        self.assertTrue((self.wd / ".momo/.gitignore").is_file())

    def test_project_writes_create_nothing(self):
        dispatch("write_file", {"path": "a.py", "content": "x = 1\n"}, self.wd)
        dispatch("read_file", {"path": "a.py"}, self.wd)
        self.assertFalse((self.wd / ".momo").exists())


class Search(_Workdir):
    def setUp(self):
        super().setUp()
        (self.wd / "app.py").write_text("NEEDLE = 1\n")
        self.write("probe.py", "NEEDLE = 2\n")

    def test_project_wide_search_skips_it(self):
        self.assertNotIn(".momo", dispatch("grep_files", {"pattern": "NEEDLE"}, self.wd))
        self.assertNotIn(".momo", dispatch("find_files", {"pattern": "*.py"}, self.wd))
        self.assertNotIn(".momo", dispatch("find_files", {"pattern": "**/*.py"}, self.wd))

    def test_search_rooted_inside_finds_it(self):
        self.assertIn("probe.py", dispatch("grep_files", {"pattern": "NEEDLE",
                                                          "directory": ".momo/scratch"}, self.wd))
        self.assertIn("probe.py", dispatch("find_files", {"pattern": "*.py",
                                                          "directory": ".momo/scratch"}, self.wd))
        self.assertIn("NEEDLE = 2", dispatch("read_file", {"path": ".momo/scratch/probe.py"},
                                             self.wd))

    def test_index_never_holds_it_whatever_the_filter(self):
        self.assertTrue(Rules.parse("").excluded(".momo/scratch/probe.py"))
        self.assertTrue(Rules.parse("!.momo/\n!.momo/**").excluded(".momo/scratch/probe.py"))
        self.assertTrue(Rules.parse("").dir_excluded(".momo"))
        idx = ci.ProjectIndex(self.wd, filter_text="", workers=1)   # a filter with no .momo/ line
        idx.start()
        try:
            self.assertIsNone(idx.wait_fresh())
            self.assertTrue(idx.has_file("app.py"))
            self.assertFalse(idx.has_file(".momo/scratch/probe.py"))
        finally:
            idx.stop()
            idx.join(2)


class PlanFile(_Workdir):
    def plan(self, title="P"):
        return Plan(title=title, steps=[Step(title="one")])

    def test_lives_in_momo_dir(self):
        self.assertEqual(write_plan_file(self.plan(), self.wd), "")
        self.assertTrue((self.wd / ".momo/plan.md").is_file())
        self.assertTrue((self.wd / ".momo/.gitignore").is_file())
        self.assertEqual(read_plan_file(self.wd).title, "P")
        delete_plan_file(self.wd)
        self.assertFalse((self.wd / ".momo/plan.md").exists())

    def test_legacy_file_is_read_then_removed(self):
        (self.wd / LEGACY_PLAN_FILENAME).write_text(self.plan("Old").to_markdown())
        self.assertEqual(read_plan_file(self.wd).title, "Old")
        write_plan_file(self.plan("New"), self.wd)
        self.assertFalse((self.wd / LEGACY_PLAN_FILENAME).exists())
        self.assertEqual(read_plan_file(self.wd).title, "New")

    def test_symlinked_momo_dir_is_not_written(self):
        os.symlink(self.outside, self.wd / ".momo")
        self.assertIn("symlink", write_plan_file(self.plan(), self.wd))
        self.assertEqual(list(self.outside.iterdir()), [])


class HarnessWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.wd = self.home / "project"
        self.wd.mkdir()
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                        mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
                        mock.patch.object(hmod, "make_client",
                                          lambda *a, **k: _Scripted("http://fake", "fake"))]
        for p in self.patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode("coding")

    def tearDown(self):
        self.h.drop_run_logs()
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def names(self):
        return {t["function"]["name"] for t in self.h._current_tools()}

    def test_offered_in_every_mode_and_named_in_the_prompt(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.h.set_mode(mode)
                self.assertIn("scratch_write", self.names())
                self.assertIn("Scratchpad: `.momo/scratch/`", self.h.messages[0]["content"])
        for tools in (PLAN_EXECUTE_TOOLS, PLAN_TDD_EXECUTE_TOOLS):
            self.assertIn("scratch_write", {t["function"]["name"] for t in tools})
        self.assertIn("scratch_write", KEEPS_GREEN)

    def test_prompt_line_follows_the_tool_choice(self):
        commands.handle("/tools scratch_write off", self.h)
        self.assertNotIn("scratch_write", self.names())
        self.assertNotIn("Scratchpad", self.h.messages[0]["content"])

    def test_not_terminal_in_design_mode(self):
        self.h.set_mode("design")
        from harness.llm.base import ChatResponse, ToolCall
        _Scripted.replies = [
            ChatResponse(tool_calls=[ToolCall(name="scratch_write",
                                              arguments={"path": "n.md", "content": "x"})]),
            ChatResponse(content="", done_reason="stop"),
            ChatResponse(content="The answer.", done_reason="stop")]
        self.h.send("think it through")
        replies = [m["content"] for m in self.h.messages if m.get("role") == "assistant"
                   and m.get("content")]
        self.assertIn("The answer.", replies)
        self.assertTrue((self.wd / ".momo/scratch/n.md").is_file())

    def test_run_command_sees_momo_scratch(self):
        out = self.h._dispatch("run_command", {"command": 'echo "dir=$MOMO_SCRATCH"'})
        self.assertIn(f"dir={self.wd / '.momo' / 'scratch'}", out)

    def test_run_logs_created_only_when_a_command_runs(self):
        self.h._dispatch("read_file", {"path": "."})
        self.h._dispatch("scratch_write", {"path": "a", "content": "b"})
        self.assertFalse((self.wd / ".momo/runs").exists())

    def test_workdir_change_keeps_the_run_logs(self):
        out = self.h._dispatch("run_command", {"command": "echo first"})
        other = self.home / "other"
        other.mkdir()
        commands.handle(f"/workspace {other}", self.h)
        self.assertIn("first", self.h._dispatch("command_output", {"path": "r1"}))

    def test_read_only_workdir_falls_back_to_home(self):
        os.chmod(self.wd, 0o555)
        try:
            out = self.h._dispatch("run_command", {"command": "echo ro"})
            self.assertIn(str(self.home / ".momo-harness" / "runs" / self.h._ts), out)
        finally:
            os.chmod(self.wd, 0o755)

    def test_scratch_command(self):
        self.assertIn("(empty)", commands.handle("/scratch", self.h).output)
        self.h._dispatch("scratch_write", {"path": "probe.py", "content": "x"})
        self.assertIn("probe.py", commands.handle("/scratch", self.h).output)
        res = commands.handle("/scratch clear", self.h)
        self.assertIn("Delete everything", res.confirm_prompt)
        self.assertTrue((self.wd / ".momo/scratch/probe.py").exists())     # asks first
        self.assertIn("1 entries removed", res.confirm_action())
        self.assertFalse((self.wd / ".momo/scratch/probe.py").exists())
        self.assertTrue((self.wd / ".momo/.gitignore").exists())
        self.assertTrue(commands.handle("/scratch nope", self.h).output.startswith("Usage"))


if __name__ == "__main__":
    unittest.main()
