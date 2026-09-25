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


class HarnessDispatch(Base):
    def test_raising_tool_becomes_an_error_result(self):
        import harness.harness as hmod

        class H:                       # just what Harness._dispatch touches
            workdir = self.root
            net_access, net_max_bytes, index, index_route = "off", 0, None, True
            _cancel = threading.Event()

            def _wait_index(self, name):
                return None

            def _fetch_chars(self):
                return 0

        with mock.patch.object(hmod, "dispatch", side_effect=RuntimeError("boom")):
            out = hmod.Harness._dispatch(H(), "read_file", {"path": "x"})
        self.assertEqual(out, "ERROR: read_file failed: RuntimeError: boom")


class IndexFilterLoading(Base):
    def test_filter_is_not_seeded_on_the_constructing_thread(self):
        with mock.patch.object(ci, "load_filter", side_effect=AssertionError("walked")) as lf:
            idx = ci.ProjectIndex(self.root)
        lf.assert_not_called()
        self.assertTrue(idx.filter_pending)


if __name__ == "__main__":
    unittest.main()
