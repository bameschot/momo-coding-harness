"""/run-mode new: run_command saves its output to a log and returns a view.

The view helpers are checked on plain text; run_command and command_output
through dispatch; the existing run_command suites are run again in new mode.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import test_run_command_web as web_base
import test_tool_robustness as base

import harness.harness as hmod
from harness import commands, run_store
from harness import session as session_mod
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.tools import dispatch

_FOOTER_RE = re.compile(r"\n?\[output saved: .*", re.DOTALL)


def body(out: str) -> str:
    """A new-mode result without its footer (the log path and the hint)."""
    return _FOOTER_RE.sub("", out)


def saved_path(out: str) -> str:
    m = re.search(r"\[output saved: (\S+\.log) — ", out)
    assert m, out
    return m.group(1)


class Views(unittest.TestCase):
    def test_short_text_is_returned_whole(self):
        self.assertEqual(run_store.window("a\nb", 100), "a\nb")

    def test_long_text_keeps_a_quarter_head_and_three_quarter_tail(self):
        text = "\n".join(f"line {i:04d}" for i in range(1, 2001))   # 10 chars + \n each
        out = run_store.window(text, 1000)
        head, note, tail = re.split(r"\n(\[… .* not shown.*\])\n", out)
        self.assertTrue(head.startswith("line 0001"))
        self.assertTrue(tail.endswith("line 2000"))
        self.assertLessEqual(len(head), 250)
        self.assertLessEqual(len(tail), 750)
        self.assertGreater(len(tail), 2 * len(head))
        # Cut on line boundaries, and the note counts what is missing.
        shown = head.split("\n") + tail.split("\n")
        self.assertTrue(all(re.fullmatch(r"line \d{4}", ln) for ln in shown))
        self.assertIn(f"{2000 - len(shown):,} lines", note)

    def test_numbered_note_names_the_missing_lines(self):
        text = "\n".join(f"line {i:04d}" for i in range(1, 2001))
        out = run_store.window(text, 1000, numbered=True)
        head, note, tail = re.split(r"\n(\[… .* not shown.*\])\n", out)
        first, last = (int(n.replace(",", "")) for n in
                       re.search(r"lines ([\d,]+)-([\d,]+)", note).groups())
        self.assertEqual(head.split("\n")[-1], f"line {first - 1:04d}")
        self.assertEqual(tail.split("\n")[0], f"line {last + 1:04d}")

    def test_default_view_without_trouble_is_the_plain_window(self):
        text = "\n".join(f"line {i:04d}" for i in range(1, 2001))
        self.assertEqual(run_store.default_view(text, 1000),
                         (run_store.window(text, 1000, numbered=True), False))
        self.assertEqual(run_store.default_view("short", 1000), ("short", False))

    def test_one_huge_line_is_cut_on_both_ends(self):
        out = run_store.window("x" * 50_000, 1000)
        self.assertLess(len(out), 1200)
        self.assertIn("not shown", out)

    def test_tail(self):
        out = run_store.view("\n".join(map(str, range(1, 101))), 5000, tail_n=3)
        self.assertEqual(out, "[last 3 of 100 lines, from line 98]\n98\n99\n100")

    def test_grep_with_context_and_line_numbers(self):
        text = "a\nb\nERROR one\nc\nd\ne\nerror two\nf"
        out = run_store.view(text, 5000, pattern="error", context=1)
        self.assertEqual(out, '[grep "error": 2 matching lines of 8]\n'
                              "2- b\n3: ERROR one\n4- c\n--\n6- e\n7: error two\n8- f")

    def test_grep_and_tail_give_the_last_matches(self):
        text = "\n".join(f"hit {i}" for i in range(10))
        out = run_store.view(text, 5000, pattern="hit", tail_n=2)
        self.assertIn("last 2 of 10 matching", out)
        self.assertTrue(out.endswith("9: hit 8\n10: hit 9"), out)

    def test_invalid_regex_is_matched_literally(self):
        out = run_store.view("f(x\ny", 5000, pattern="f(x")
        self.assertIn("matched as plain text", out)
        self.assertIn("1: f(x", out)

    def test_bre_alternation(self):
        out = run_store.view("error\nok\nfail", 5000, pattern="error\\|fail")
        self.assertIn("2 matching", out)

    def test_many_matches_are_windowed(self):
        text = "\n".join(f"error {i}" for i in range(5000))
        out = run_store.view(text, 1000, pattern="error")
        self.assertLess(len(out), 1200)
        self.assertIn("5,000 matching", out)
        self.assertTrue(out.endswith("error 4999"))

    def test_limit_zero_is_no_limit(self):
        text = "\n".join(f"line {i}" for i in range(5000)) + "\nFAILED x"
        self.assertEqual(run_store.window(text, 0), text)
        self.assertEqual(run_store.default_view(text, 0), (text, False))
        self.assertEqual(run_store.view(text, 0), text)
        self.assertEqual(run_store.view(text, 0, tail_n=4000).count("\n"), 4000)

    def test_no_match(self):
        self.assertIn("no matching lines in 2", run_store.view("a\nb", 5000, pattern="zzz"))

    def test_line_range_and_negative_start(self):
        text = "\n".join(map(str, range(1, 11)))
        self.assertEqual(run_store.view(text, 5000, start_line=2, end_line=3),
                         "[lines 2-3 of 10]\n2: 2\n3: 3")
        self.assertIn("[lines 9-10 of 10]", run_store.view(text, 5000, start_line=-2))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.wd = self.root / "wd"
        self.wd.mkdir()
        self.store = run_store.RunStore(self.root / "runs" / "session")

    def tearDown(self):
        self.tmp.cleanup()

    def cmd(self, command, limit=5000, **args):
        return dispatch("run_command", {"command": command, **args}, self.wd,
                        run_mode="new", run_store=self.store, run_output_limit=limit)

    def output(self, path, limit=5000, **args):
        return dispatch("command_output", {"path": path, **args}, self.wd,
                        run_mode="new", run_store=self.store, run_output_limit=limit)


class RunCommandNew(Base):
    def test_small_output_is_whole_and_names_the_full_log_path(self):
        out = self.cmd("echo hi; echo oops >&2; exit 3")
        self.assertEqual(body(out), "hi\noops")          # stdout and stderr, in order
        path = saved_path(out)
        self.assertTrue(Path(path).is_absolute())
        self.assertEqual(Path(path), self.store.root / "r1.log")
        self.assertIn("2 lines", out)
        self.assertIn("exit code 3", out)
        self.assertNotIn("command_output(", out)          # nothing hidden, no hint

    def test_the_printed_path_works_in_command_output(self):
        out = self.cmd("seq 1 50")
        again = self.output(saved_path(out), tail=2)
        self.assertEqual(body(again), "[last 2 of 50 lines, from line 49]\n49\n50")
        self.assertIn(saved_path(out), again)            # the footer repeats the path

    def test_large_output_is_windowed_with_a_hint(self):
        out = self.cmd("seq 1 20000; echo 'FAILED test_x - AssertionError'", limit=2000)
        self.assertLessEqual(len(body(out)), 2000)
        self.assertTrue(body(out).startswith("1\n2\n"))
        self.assertIn("FAILED test_x", body(out))       # the ending survives
        # The note names the missing lines and the call that shows them.
        m = re.search(r"\[… lines (\d+)-([\d,]+) \([\d,]+ chars\) not shown — "
                      r'command_output\(path="r1", start_line=…, end_line=…\) shows them …\]', out)
        self.assertTrue(m, out)
        first = int(m.group(1))                          # seq: line N is "N"
        self.assertEqual(body(out).split("\n")[first - 2], str(first - 1))
        self.assertNotIn("lines mention", out)           # the one trouble line is in view

    def test_trouble_in_the_middle_is_shown_numbered(self):
        out = self.cmd("seq 1 5000; echo 'src/x.c:4: warning: unused'; seq 1 5000; "
                       "echo 'Error 1'", limit=2000)
        self.assertLessEqual(len(body(out)), 2000)
        self.assertIn("5001: src/x.c:4: warning: unused", out)
        self.assertIn("the 1 that mentions error/fail/warning:", out)
        self.assertIn("[… output continues at line", out)
        self.assertTrue(body(out).endswith("Error 1"))
        self.assertNotIn("lines mention", out)           # all of them shown

    def test_trouble_that_does_not_fit_is_counted_in_the_footer(self):
        out = self.cmd("for i in $(seq 1 3000); do echo \"line $i\"; "
                       "[ $((i % 10)) = 0 ] && echo \"ERROR number $i\"; done; echo end",
                       limit=2000)
        self.assertLessEqual(len(body(out)), 2000)
        self.assertIn("the first ", out)
        self.assertIn('lines mention error/fail/warning — command_output(path="r1", '
                      'grep="error|fail|warning|traceback")', out)

    def test_command_output_names_the_command_and_its_exit_code(self):
        self.cmd("echo one; exit 4")
        out = self.output("r1")
        self.assertIn("— `echo one; exit 4` — 1 lines", out)
        self.assertIn("exit code 4]", out)
        missing = self.output("r7")
        self.assertIn(f"{self.store.root / 'r1.log'} (`echo one; exit 4`, exit code 4)", missing)

    def test_log_text_is_cached_until_the_file_changes(self):
        self.cmd("seq 1 3")
        store = run_store.RunStore(self.store.root)      # a cold cache
        log = store.root / "r1.log"
        clean = mock.Mock(side_effect=lambda t: t.strip())
        self.assertEqual(store.read(log, clean), "1\n2\n3")
        self.assertEqual(store.read(log, clean), "1\n2\n3")
        self.assertEqual(clean.call_count, 1)
        with open(log, "a") as f:
            f.write("4\n")
        self.assertEqual(store.read(log, clean), "1\n2\n3\n4")
        self.assertEqual(clean.call_count, 2)

    def test_tail_and_grep_args(self):
        self.assertEqual(body(self.cmd("seq 1 1000", tail=2)),
                         "[last 2 of 1,000 lines, from line 999]\n999\n1000")
        self.assertIn("500: 500", self.cmd("seq 1 1000", grep="^500$"))

    def test_command_output_accepts_id_and_file_name(self):
        self.cmd("echo one")
        for ref in ("r1", "r1.log", str(self.store.root / "r1.log")):
            with self.subTest(ref=ref):
                self.assertEqual(body(self.output(ref)), "one")

    def test_command_output_only_ever_opens_its_own_logs(self):
        self.cmd("echo one")
        secret = self.root / "r1.log"
        secret.write_text("secret\n")
        (self.store.root / "r9.log").symlink_to(secret)
        for ref in ("/etc/passwd", "r9", "", "r2", "../x.txt"):
            with self.subTest(ref=ref):
                out = self.output(ref)
                self.assertTrue(out.startswith("ERROR: no saved command output"), out)
                self.assertNotIn("secret\n", out)
        # A path elsewhere that ends in rN.log names this store's rN, never that file;
        # so does a mistyped session directory.
        for ref in (str(secret), "../r1.log", str(self.store.root.parent / "other" / "r1.log")):
            with self.subTest(ref=ref):
                self.assertEqual(body(self.output(ref)), "one")
        self.assertIn(str(self.store.root / "r1.log"), self.output("nope"))

    def test_old_logs_are_pruned(self):
        for i in range(run_store.KEEP_LOGS + 3):
            self.cmd(f"echo {i}")
        logs = self.store.logs()
        self.assertEqual(len(logs), run_store.KEEP_LOGS)
        self.assertEqual(logs[-1].name, f"r{run_store.KEEP_LOGS + 3}.log")
        self.assertTrue(self.output("r1").startswith("ERROR"))

    def test_timeout_leaves_a_queryable_log(self):
        out = self.cmd("echo before; sleep 30", timeout=1)
        self.assertTrue(out.startswith("ERROR: command timed out"), out)
        self.assertIn("before", out)
        self.assertEqual(body(self.output(saved_path(out))), "before")

    def test_interrupt_leaves_a_queryable_log(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        out = dispatch("run_command", {"command": "echo started; sleep 30"}, self.wd,
                       cancel=cancel, run_mode="new", run_store=self.store)
        self.assertIn("interrupted", out)
        self.assertEqual(body(self.output("r1")), "started")

    def test_classic_mode_ignores_the_store(self):
        out = dispatch("run_command", {"command": "echo hi"}, self.wd,
                       run_mode="classic", run_store=self.store)
        self.assertEqual(out, "hi")
        self.assertEqual(self.store.logs(), [])

    def test_clear_removes_the_logs(self):
        self.cmd("echo x")
        self.store.clear()
        self.assertFalse(self.store.root.exists())
        self.assertIn("r1.log", saved_path(self.cmd("echo y")))


# The classic suites, run again in new mode with the footer stripped: the same
# commands must behave the same way.
class Compact(unittest.TestCase):
    """/cavecat's read-less: redundant runs of lines collapse into a note that
    names their lines in the log."""
    def test_passing_lines_collapse_and_failures_stay(self):
        lines = ([f"test_{i} (t.T.test_{i}) ... ok" for i in range(1, 21)]
                 + ["test_bad (t.T.test_bad) ... FAIL", "FAILED (failures=1)"])
        out = run_store.compact("\n".join(lines), "r4")
        self.assertIn('[lines 1–20: 20 passing lines not shown — '
                      'command_output(path="r4", start_line=1, end_line=20)]', out)
        self.assertIn("test_bad (t.T.test_bad) ... FAIL", out)
        self.assertTrue(out.endswith("FAILED (failures=1)"))

    def test_check_marks_and_pass_words_from_other_runners(self):
        lines = ([f"  \u2713 renders item {i} ({i} ms)" for i in range(8)]
                 + [f"ok  \texample.com/pkg{i}\t0.0{i}s" for i in range(8)])
        out = run_store.compact("\n".join(lines), "r1")
        self.assertEqual(out.count("passing lines not shown"), 1)
        self.assertNotIn("renders", out)

    def test_a_pass_line_with_a_trouble_word_is_kept(self):
        lines = [f"case {c} PASSED" for c in "abcdefgh"] + ["case z PASSED with 2 warnings"] \
            + [f"case {c} PASSED" for c in "ijklmnop"]
        out = run_store.compact("\n".join(lines), "r1")
        self.assertIn("case z PASSED with 2 warnings", out)
        self.assertEqual(out.count("passing lines not shown"), 2)

    def test_the_same_line_repeated(self):
        out = run_store.compact("start\n" + "retrying connection\n" * 15 + "end", "r1")
        self.assertEqual(out, "start\nretrying connection\n"
                              "[lines 3–16: the same line 14 more times]\nend")

    def test_lines_that_differ_only_in_numbers_keep_first_and_last(self):
        out = run_store.compact("\n".join(f"step {i}/40 loss {i * 3}" for i in range(1, 41)), "r2")
        self.assertEqual(out.split("\n"), [
            "step 1/40 loss 3",
            '[lines 2–39: 38 similar lines — command_output(path="r2", start_line=2, end_line=39)]',
            "step 40/40 loss 120"])

    def test_small_savings_leave_it_as_it_is(self):
        text = "a\n" + "same\n" * 5 + "b"
        self.assertIs(run_store.compact(text, "r1"), text)


PASSES = 'for i in $(seq 1 30); do echo "test_$i ... ok"; done'


class CompactRunCommand(Base):
    def run_(self, command, **args):
        return dispatch("run_command", {"command": command, **args}, self.wd, run_mode="new",
                        run_store=self.store, run_output_limit=0, run_compact=True)

    def test_default_view_is_compacted_and_the_log_is_whole(self):
        out = self.run_(PASSES + "; echo done")
        self.assertIn("30 passing lines not shown", out)
        self.assertIn("31 lines", out)                  # the footer counts the whole log
        self.assertIn("test_17 ... ok", self.output(saved_path(out), limit=0))

    def test_tail_and_grep_views_are_not_compacted(self):
        self.assertNotIn("not shown", self.run_(PASSES, tail=20))
        self.assertNotIn("not shown", self.run_(PASSES, grep="ok"))

    def test_off_by_default(self):
        self.assertNotIn("not shown", self.cmd(PASSES, limit=0))

    def test_a_cut_view_is_not_compacted(self):
        # still past the limit compacted, the default view numbers lines, which must
        # be the log's own
        cmd = PASSES + '; for i in $(seq 1 400); do echo "x$i"; echo y; done'
        out = dispatch("run_command", {"command": cmd}, self.wd, run_mode="new",
                       run_store=self.store, run_output_limit=2000, run_compact=True)
        self.assertNotIn("passing lines not shown", out)
        self.assertIn("not shown", out)                 # the plain cut window


class CompactShellSuite(unittest.TestCase):
    """The eval's shell project (evals/shell/project), run for real: compacted,
    each output keeps the facts the shell tasks ask for at a fifth of the size."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name) / "project"
        shutil.copytree(Path(__file__).parent.parent / "evals" / "shell" / "project", self.wd,
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.store = run_store.RunStore(Path(self.tmp.name) / "runs")

    def tearDown(self):
        self.tmp.cleanup()

    def both(self, command):
        out = [dispatch("run_command", {"command": command}, self.wd, run_mode="new",
                        run_store=self.store, run_output_limit=0, run_compact=c)
               for c in (False, True)]
        return [body(o) for o in out]

    def test_test_run_keeps_failures_and_summary(self):
        whole, short = self.both("./run_tests.sh")
        for fact in ("FAIL: test_restock_047", "FAIL: test_price_203", "47 != 48",
                     "45000 != 50000", "Ran 300 tests"):
            self.assertIn(fact, short)
        self.assertLess(len(short), len(whole) / 5)

    def test_build_keeps_the_warning_from_the_middle(self):
        whole, short = self.both("./build.sh")
        self.assertIn("deprecated key 'max_conn'", short)
        self.assertIn("0 errors", short)
        self.assertLess(len(short), len(whole) / 5)

    def test_short_lint_output_is_untouched(self):
        whole, short = self.both("./lint.sh")
        self.assertEqual(short, whole)


class ClassicSuiteInNewMode(base.RunCommand):
    def setUp(self):
        super().setUp()
        self.store = run_store.RunStore(self.root / ".runs")

    def run_tool(self, name, **args):
        return body(dispatch(name, args, self.root, run_mode="new", run_store=self.store))

    def test_cancel_stops_a_running_command(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        t0 = time.monotonic()
        out = dispatch("run_command", {"command": "sleep 30"}, self.root, cancel=cancel,
                       run_mode="new", run_store=self.store)
        self.assertIn("interrupted", out)
        self.assertLess(time.monotonic() - t0, 10)


class WebSuiteInNewMode(web_base.Dispatch):
    def run_cmd(self, command, net_access):
        store = run_store.RunStore(Path(self.tmp.name) / ".runs")
        return body(dispatch("run_command", {"command": command}, self.workdir, net_access,
                             run_mode="new", run_store=store))


class _Scripted(LLMClient):
    provider_name = "fake"
    replies: list[ChatResponse] = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return self.replies.pop(0) if self.replies else ChatResponse(content="Done.",
                                                                     done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class HarnessWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                        mock.patch.object(hmod, "make_client",
                                          lambda *a, **k: _Scripted("http://fake", "fake"))]
        for p in self.patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)
        self.h.set_mode("coding")

    def tearDown(self):
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def names(self):
        return [t["function"]["name"] for t in self.h._current_tools()]

    def test_new_is_the_default_and_offers_command_output(self):
        self.assertEqual(self.h.run_mode, "new")
        names = self.names()
        self.assertEqual(names[names.index("run_command") + 1], "command_output")
        # The full description travels in the schema (the prompt's tool
        # reference is compact by default).
        run = next(t for t in self.h._current_tools() if t["function"]["name"] == "run_command")
        self.assertIn("tail=N", run["function"]["description"])

    def test_run_mode_command_swaps_the_schema(self):
        out = commands.handle("/run-mode classic", self.h).output
        self.assertIn("classic", out)
        self.assertNotIn("command_output", self.names())
        self.assertNotIn("command_output", self.h.messages[0]["content"])
        commands.handle("/run-mode on", self.h)
        self.assertEqual(self.h.run_mode, "new")
        self.assertIn("command_output", self.names())
        self.assertTrue(commands.handle("/run-mode sideways", self.h).output.startswith("ERROR"))

    def test_plan_modes_follow_the_run_mode(self):
        self.h.mode = "plan"
        self.assertIn("command_output", self.names())
        self.h.mode = "chat"
        self.assertNotIn("run_command", self.names())
        self.assertNotIn("command_output", self.names())

    def test_output_limit_command(self):
        self.assertEqual(self.h.run_output_limit, 0)      # off by default
        out = self.h._dispatch("run_command", {"command": "seq 1 20000"})
        self.assertEqual(body(out), "\n".join(map(str, range(1, 20001))))
        self.assertIn("off", commands.handle("/run-output-limit", self.h).output)
        self.assertIn("ERROR", commands.handle("/run-output-limit 10", self.h).output)
        commands.handle("/run-output-limit 800", self.h)
        self.assertEqual(self.h.run_output_limit, 800)
        out = self.h._dispatch("run_command", {"command": "seq 1 5000"})
        self.assertLess(len(body(out)), 900)
        commands.handle("/run-output-limit off", self.h)
        self.assertEqual(self.h.run_output_limit, 0)
        self.assertIn("whole output", commands.handle("/run-mode", self.h).output)

    def test_logs_live_in_the_session_dir_and_go_with_it(self):
        out = self.h._dispatch("run_command", {"command": "echo hi"})
        path = Path(saved_path(out))
        self.assertEqual(path.parent, self.h.workdir / ".momo" / "runs" / self.h._ts)
        # inside the workdir, so the model's own file tools read it too
        self.assertIn("hi", self.h._dispatch("read_file", {"path": str(path)}))
        commands.handle("/clear", self.h)
        self.assertFalse(path.exists())
        out = self.h._dispatch("run_command", {"command": "echo again"})
        self.h.new_session()
        self.assertFalse(Path(saved_path(out)).exists())

    def tool_result_of(self, command):
        _Scripted.replies = [ChatResponse(tool_calls=[ToolCall(name="run_command",
                                                               arguments={"command": command})])]
        self.h.send("run it")
        return [m["content"] for m in self.h.messages if m.get("role") == "tool"][-1]

    def test_tool_result_cap_leaves_windowed_output_alone(self):
        # /tool-result would cut the head+tail view from the front and lose the
        # footer with the log path; new-mode results window themselves.
        self.h.max_tool_result = 500
        out = self.tool_result_of("seq 1 5000")
        self.assertNotIn("truncated after", out)
        self.assertIn("5000", out)
        self.assertIn("[output saved: ", out)
        commands.handle("/run-mode classic", self.h)
        self.assertIn("truncated after", self.tool_result_of("seq 1 5000"))

if __name__ == "__main__":
    unittest.main()
