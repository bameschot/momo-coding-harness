"""Tool failures that used to escape as exceptions or quietly damage files.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import code_index as ci
from harness import tools
from harness.tools import dispatch


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        for p in self.root.rglob("*"):
            try:
                os.chmod(p, 0o644, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass
        self.tmp.cleanup()

    def run_tool(self, name, **args):
        return dispatch(name, args, self.root)


class EditFile(Base):
    def test_read_only_file_is_an_error_not_an_exception(self):
        f = self.root / "ro.txt"
        f.write_text("hello\n")
        f.chmod(0o444)
        out = self.run_tool("edit_file", path="ro.txt", old_string="hello", new_string="bye")
        self.assertTrue(out.startswith("ERROR"), out)
        self.assertEqual(f.read_text(), "hello\n")

    def test_crlf_endings_are_kept(self):
        f = self.root / "crlf.txt"
        f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
        out = self.run_tool("edit_file", path="crlf.txt", old_string="one\ntwo", new_string="1\n2")
        self.assertTrue(out.startswith("OK"), out)
        self.assertEqual(f.read_bytes(), b"1\r\n2\r\nthree\r\n")

    def test_non_utf8_file_is_refused_untouched(self):
        f = self.root / "latin.txt"
        f.write_bytes(b"caf\xe9 x\n")
        out = self.run_tool("edit_file", path="latin.txt", old_string="x", new_string="y")
        self.assertTrue(out.startswith("ERROR") and "UTF-8" in out, out)
        self.assertEqual(f.read_bytes(), b"caf\xe9 x\n")


    def test_empty_old_string_is_refused(self):
        f = self.root / "a.txt"
        f.write_text("abc\ndef\n")
        out = self.run_tool("edit_file", path="a.txt", old_string="", new_string="Z", replace_all=True)
        self.assertTrue(out.startswith("ERROR"), out)
        self.assertEqual(f.read_text(), "abc\ndef\n")

    def test_empty_old_string_fills_an_empty_file(self):
        f = self.root / "a.txt"
        f.write_text("")
        out = self.run_tool("edit_file", path="a.txt", old_string="", new_string="hi\n")
        self.assertTrue(out.startswith("OK"), out)
        self.assertEqual(f.read_text(), "hi\n")

    def test_lineno_prefix_is_not_stripped_from_real_text(self):
        # old_string carries read_file's prefix; new_string is real code whose
        # dict entry looks like one.
        f = self.root / "d.py"
        f.write_text('x = {\n    1: "one",\n}\n')
        out = self.run_tool("edit_file", path="d.py", old_string="   1: x = {",
                            new_string="y = {")
        self.assertTrue(out.startswith("OK"), out)
        out = self.run_tool("edit_file", path="d.py", old_string='   2:     1: "one",',
                            new_string='    1: "uno",')
        self.assertTrue(out.startswith("OK"), out)
        self.assertEqual(f.read_text(), 'y = {\n    1: "uno",\n}\n')

    def test_tolerant_match_keeps_an_indentation_change(self):
        f = self.root / "a.py"
        f.write_text("def f():\n  x = 1\n")
        # The model's copy has 4 spaces where the file has 2, and indents one more level.
        out = self.run_tool("edit_file", path="a.py", old_string="    x = 1", new_string="        x = 1")
        self.assertTrue(out.startswith("OK"), out)
        self.assertEqual(f.read_text(), "def f():\n      x = 1\n")

    def test_tolerant_noop_is_not_reported_as_a_change(self):
        f = self.root / "a.py"
        f.write_text("def f():\n  x = 1\n")
        out = self.run_tool("edit_file", path="a.py", old_string="    x = 1", new_string="    x = 1  ")
        self.assertTrue(out.startswith("No change"), out)

    def test_short_new_string_elsewhere_is_not_already_applied(self):
        f = self.root / "r.py"
        f.write_text("def g():\n    return None\n")
        out = self.run_tool("edit_file", path="r.py", old_string="retrun 1", new_string="return None")
        self.assertTrue(out.startswith("ERROR: old_string not found"), out)

    def test_already_applied_names_the_line(self):
        f = self.root / "r.py"
        f.write_text("a\ndef g():\n    return compute(1)\n")
        out = self.run_tool("edit_file", path="r.py", old_string="def g():\n    return compute(0)",
                            new_string="def g():\n    return compute(1)")
        self.assertTrue(out.startswith("No change needed") and "line 2" in out, out)


class RunCommand(Base):
    def test_non_utf8_output(self):
        out = self.run_tool("run_command", command="printf 'a\\377b'")
        self.assertEqual(out, "a�b")

    def test_stdin_is_not_the_terminal(self):
        t0 = time.monotonic()
        out = self.run_tool("run_command", command="cat; echo done", timeout=10)
        self.assertEqual(out, "done")
        self.assertLess(time.monotonic() - t0, 5)

    def test_timeout_kills_grandchildren(self):
        t0 = time.monotonic()
        out = self.run_tool("run_command", command="sleep 30 & sleep 30", timeout=1)
        self.assertIn("timed out after 1s", out)
        self.assertLess(time.monotonic() - t0, 10)

    def test_cancel_stops_a_running_command(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        t0 = time.monotonic()
        out = dispatch("run_command", {"command": "sleep 30"}, self.root, cancel=cancel)
        self.assertIn("interrupted", out)
        self.assertLess(time.monotonic() - t0, 10)


    def test_timeout_keeps_the_output_so_far(self):
        out = self.run_tool("run_command", command="echo before; sleep 30", timeout=1)
        self.assertTrue(out.startswith("ERROR: command timed out"), out)
        self.assertIn("before", out)

    def test_progress_bar_keeps_its_last_frame(self):
        out = self.run_tool("run_command", command="printf '10%%\\r50%%\\r100%%\\ndone\\n'")
        self.assertEqual(out, "100%\ndone")

    def test_background_child_does_not_hold_the_call(self):
        t0 = time.monotonic()
        out = self.run_tool("run_command", command="sleep 30 & echo started", timeout=20)
        self.assertEqual(out, "started")
        self.assertLess(time.monotonic() - t0, 10)

    def test_daemon_outlives_a_normal_exit(self):
        # A build daemon (Gradle, mvnd) started by a command must keep running.
        pidfile = self.root / "pid"
        out = self.run_tool("run_command", command=f"nohup sleep 30 >/dev/null 2>&1 & echo $! > {pidfile}")
        self.assertEqual(out, "(no output)")
        pid = int(pidfile.read_text())
        try:
            time.sleep(0.3)
            os.kill(pid, 0)          # raises if the daemon was killed
        finally:
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def test_runaway_output_is_stopped(self):
        with mock.patch.object(tools, "_MAX_OUTPUT_BYTES", 100_000):
            out = self.run_tool("run_command", command="yes", timeout=20)
        self.assertTrue(out.startswith("ERROR: command stopped"), out)
        self.assertLess(len(out), 10_000)


class Arguments(Base):
    def test_numbers_as_strings(self):
        (self.root / "f.txt").write_text("a\nb\nc\n")
        out = self.run_tool("read_file", path="f.txt", start_line="2", end_line="2")
        self.assertIn("2: b", out)
        self.assertNotIn("ERROR", out)

    def test_boolean_as_string(self):
        (self.root / "f.txt").write_text("x x\n")
        out = self.run_tool("edit_file", path="f.txt", old_string="x", new_string="y",
                            replace_all="true")
        self.assertIn("Replaced 2", out)


class Links(Base):
    def test_delete_removes_the_link_not_its_target(self):
        target = self.root / "real.txt"
        target.write_text("keep")
        (self.root / "link.txt").symlink_to(target)
        self.assertEqual(self.run_tool("delete_file", path="link.txt"), "OK")
        self.assertFalse((self.root / "link.txt").is_symlink())
        self.assertEqual(target.read_text(), "keep")

    def test_move_does_not_overwrite(self):
        (self.root / "a.txt").write_text("a")
        (self.root / "b.txt").write_text("b")
        out = self.run_tool("move_file", src="a.txt", dst="b.txt")
        self.assertTrue(out.startswith("ERROR"), out)
        self.assertEqual((self.root / "b.txt").read_text(), "b")

    def test_move_outside_is_refused(self):
        (self.root / "a.txt").write_text("a")
        out = self.run_tool("move_file", src="a.txt", dst="../escaped.txt")
        self.assertTrue(out.startswith("ERROR"), out)


    def test_file_info_reports_a_symlink(self):
        (self.root / "real.txt").write_text("x\n")
        (self.root / "link.txt").symlink_to("real.txt")
        out = self.run_tool("file_info", path="link.txt")
        self.assertIn("type:     symlink -> real.txt", out)
        self.assertIn("type:     directory", self.run_tool("file_info", path="."))


class HarnessDispatch(Base):
    def test_raising_tool_becomes_an_error_result(self):
        import harness.harness as hmod

        class H:                       # just what Harness._dispatch touches
            workdir = self.root
            net_access, net_max_bytes, index, index_route = "off", 0, None, True
            run_mode, run_store, run_output_limit = "classic", None, 5000
            _cancel = threading.Event()

            def _read_budget(self):
                return None

            def _wait_index(self, name):
                return None

            def _fetch_chars(self):
                return 0

        with mock.patch.object(hmod, "dispatch", side_effect=RuntimeError("boom")):
            out = hmod.Harness._dispatch(H(), "read_file", {"path": "x"})
        self.assertEqual(out, "ERROR: read_file failed: RuntimeError: boom")


class Search(Base):
    def test_grep_files_on_a_file_greps_that_file(self):
        (self.root / "a.txt").write_text("abc\n")
        self.assertEqual(self.run_tool("grep_files", pattern="abc", directory="a.txt"), "a.txt:1: abc")

    def test_missing_directory_is_an_error(self):
        self.assertTrue(self.run_tool("grep_files", pattern="x", directory="nope").startswith("ERROR"))
        self.assertTrue(self.run_tool("find_files", pattern="*.py", directory="nope").startswith("ERROR"))

    def test_find_files_skips_noise_dirs_and_recurses(self):
        for rel in ("a.py", "sub/b.py", "node_modules/c.py", "sub/.venv/d.py", "e.txt"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("")
        self.assertEqual(self.run_tool("find_files", pattern="*.py"), "a.py\nsub/b.py")
        self.assertEqual(self.run_tool("find_files", pattern="**/*.py"), "a.py\nsub/b.py")
        self.assertEqual(self.run_tool("find_files", pattern="sub/*.py"), "sub/b.py")

    def test_bad_input_is_an_error_not_an_exception(self):
        self.assertTrue(self.run_tool("find_files", pattern="/etc/*").startswith("ERROR"))
        self.assertTrue(self.run_tool("read_file", path="a\x00b").startswith("ERROR"))


class ReadAndGrep(Base):
    def test_read_past_the_end_is_an_error(self):
        (self.root / "a.txt").write_text("a\nb\n")
        out = self.run_tool("read_file", path="a.txt", start_line=50)
        self.assertTrue(out.startswith("ERROR") and "has 2 lines" in out, out)
        self.assertTrue(self.run_tool("read_file", path="a.txt", start_line=2, end_line=1).startswith("ERROR"))
        self.assertEqual(self.run_tool("read_file", path="a.txt", start_line=2), "   2: b\n")

    def test_read_directory_and_binary_are_errors(self):
        (self.root / "sub").mkdir()
        (self.root / "b.bin").write_bytes(b"\x00\x01abc")
        self.assertIn("list_directory", self.run_tool("read_file", path="sub"))
        self.assertIn("binary", self.run_tool("read_file", path="b.bin"))
        self.assertIn("binary", self.run_tool("grep_file", pattern="abc", path="b.bin"))

    def test_single_file_greps_are_capped(self):
        (self.root / "a.txt").write_text("x\n" * 500)
        out = self.run_tool("grep_file", pattern="x", path="a.txt")
        self.assertEqual(len(out.splitlines()), tools._MAX_GREP_RESULTS + 1)
        self.assertIn("of 500 matches", out)
        out = self.run_tool("grep_extract", pattern="x", path="a.txt")
        self.assertIn("of 500 matches", out)

    def test_grep_extract_group_and_clip(self):
        (self.root / "a.txt").write_text("v=" + "9" * 1000 + "\n")
        self.assertIn("does not exist", self.run_tool("grep_extract", pattern="v=(9+)", path="a.txt", group=2))
        out = self.run_tool("grep_extract", pattern="v=(9+)", path="a.txt", group=1)
        self.assertIn("[match is 1,000 chars]", out)
        self.assertLess(len(out), 400)

    def test_grep_files_stops_on_cancel(self):
        (self.root / "a.txt").write_text("x\n")
        cancel = threading.Event()
        cancel.set()
        out = dispatch("grep_files", {"pattern": "x"}, self.root, cancel=cancel)
        self.assertIn("interrupted", out)

    def test_file_info_counts_lines_without_decoding(self):
        (self.root / "latin.txt").write_bytes(b"caf\xe9\nb")
        self.assertIn("lines:    2", self.run_tool("file_info", path="latin.txt"))
        (self.root / "b.bin").write_bytes(b"\x00\x01")
        self.assertIn("lines:    (binary)", self.run_tool("file_info", path="b.bin"))

    def test_list_directory_dirs_first_hidden_filtered(self):
        for d in ("zdir", ".hid"):
            (self.root / d).mkdir()
        for f in ("B.txt", "a.txt", ".env"):
            (self.root / f).write_text("")
        out = self.run_tool("list_directory")
        self.assertEqual([ln.split()[1] for ln in out.splitlines()], ["zdir/", "a.txt", "B.txt"])
        self.assertIn(".env", self.run_tool("list_directory", show_hidden=True))


class Guards(Base):
    def test_delete_directory_is_a_clear_error(self):
        (self.root / "sub").mkdir()
        out = self.run_tool("delete_file", path="sub")
        self.assertIn("is a directory", out)
        self.assertTrue((self.root / "sub").is_dir())

    def test_edit_new_string_with_a_cut_marker_is_refused(self):
        f = self.root / "a.py"
        f.write_text("a = 1\n")
        out = self.run_tool("edit_file", path="a.py", old_string="a = 1",
                            new_string="a = 2\n[… 1,234 chars removed by context compaction …]")
        self.assertTrue(out.startswith("ERROR: new_string contains a harness note"), out)
        self.assertEqual(f.read_text(), "a = 1\n")

    def test_write_routed_to_edit(self):
        f = self.root / "a.py"
        f.write_text("a = 1\n")
        out = self.run_tool("write_file", path="a.py", old_string="a = 1", new_string="a = 2")
        self.assertTrue(out.startswith("(note: routed write_file to edit_file) OK"), out)
        self.assertEqual(f.read_text(), "a = 2\n")

    def test_descriptions_quote_the_limits(self):
        descs = {t["function"]["name"]: t["function"]["description"] for t in tools.ALL_TOOLS}
        self.assertIn(f"capped at {tools._MAX_FIND_RESULTS} files", descs["find_files"])
        self.assertIn(f"capped at {tools._MAX_GREP_RESULTS} matches", descs["grep_files"])
        self.assertIn("nothing is moved", descs["move_file"])


class IndexFilterLoading(Base):
    def test_filter_is_not_seeded_on_the_constructing_thread(self):
        with mock.patch.object(ci, "load_filter", side_effect=AssertionError("walked")) as lf:
            idx = ci.ProjectIndex(self.root)
        lf.assert_not_called()
        self.assertTrue(idx.filter_pending)


if __name__ == "__main__":
    unittest.main()
