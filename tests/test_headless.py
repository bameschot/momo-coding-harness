"""Headless one-shot runs (`momo -p`): unattended answers instead of blocking
questions, the stdout/stderr split, exit codes, and the CLI's flag rules."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import headless
from harness import session as session_mod
from harness.events import AskUserEvent, ChatEvent
from harness.llm.base import ChatResponse, LLMClient, ToolCall

REPO = Path(__file__).resolve().parent.parent


def call(name, **args):
    return ChatResponse(content="", tool_calls=[ToolCall(name=name, arguments=args)],
                        done_reason="stop")


def reply(text):
    return ChatResponse(content=text, done_reason="stop")


class Scripted(LLMClient):
    provider_name = "fake"
    script: list = []
    models = ["fake"]
    hang = False                    # chat() blocks until abort()

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._aborted = threading.Event()

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        if Scripted.hang:
            self._aborted.wait(10)
            raise RuntimeError("request aborted")
        if Scripted.script:
            item = Scripted.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return reply("Done.")

    def context_length(self):
        return 32768

    def list_models(self):
        return list(Scripted.models)

    def abort(self):
        self._aborted.set()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.wd = self.home / "project"
        self.wd.mkdir()
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                        mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
                        mock.patch.object(hmod, "make_client",
                                          lambda *a, **k: Scripted("http://fake", "fake"))]
        for p in self.patches:
            p.start()
        Scripted.script, Scripted.models, Scripted.hang = [], ["fake"], False
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.plan_questions = False
        self.h.stream = False
        self.h.set_mode("coding")
        self.h.unattended = "decline"
        self.sub = self.h.event_queue.subscribe(replay=False)

    def tearDown(self):
        Scripted.script, Scripted.models, Scripted.hang = [], ["fake"], False
        self.sub.close()
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def events(self):
        out = []
        while True:
            try:
                out.append(self.sub.get_nowait())
            except Exception:
                return out

    def tool_results(self):
        return [m["content"] for m in self.h.messages if m.get("role") == "tool"]


class UnattendedAnswers(Base):
    def test_policy_per_kind(self):
        h = self.h
        self.assertEqual(h._ask_as("confirm", "Run it?"), "n")
        self.assertEqual(h._ask_as("approve", "Execute this plan?"), "y")
        self.assertEqual(h._ask_as("guidance", "Still failing"), "pause")
        self.assertEqual(h._ask_user("Which file?"), hmod.NO_USER_ANSWER)
        self.assertFalse(h._confirm("Run it?"))
        h.unattended = "yes"
        self.assertEqual(h._ask_as("confirm", "Run it?"), "y")
        self.assertTrue(h._confirm("Run it?"))
        self.assertEqual(h._ask_user("Which file?"), hmod.NO_USER_ANSWER)

    def test_question_and_answer_are_shown(self):
        self.h._ask_as("confirm", "Run it?")
        evs = self.events()
        self.assertIn(AskUserEvent("Run it?", []), evs)
        self.assertIn(ChatEvent("system", "(no user) answered: n"), evs)
        self.assertFalse(self.h.awaiting_input)

    def test_interactive_still_blocks_on_the_queue(self):
        self.h.unattended = None
        self.h._user_input_queue.put("typed")
        self.assertEqual(self.h._ask_as("confirm", "Run it?"), "typed")

    def test_ask_user_tool_gets_the_no_user_answer(self):
        Scripted.script = [call("ask_user", question="Which file should I edit?"), reply("ok")]
        self.h.send("go")
        self.assertEqual(self.tool_results(), [f"User answered: {hmod.NO_USER_ANSWER}"])

    def test_run_confirm_declined_then_allowed(self):
        self.h.run_confirm = True
        Scripted.script = [call("run_command", command="echo hi"), reply("ok")]
        self.h.send("go")
        self.assertEqual(self.tool_results(), ["ERROR: command declined by user"])
        self.h.unattended = "yes"
        Scripted.script = [call("run_command", command="echo hi"), reply("ok")]
        self.h.send("again")
        self.assertIn("hi", self.tool_results()[-1])

    def test_ask_choice_returns_at_once(self):
        Scripted.script = [call("ask_choice", question="Which framework?",
                                options=["pytest", "unittest"]), reply("ok")]
        self.h.send("go")
        self.assertEqual(self.tool_results(), [hmod.NO_USER_CHOICE])
        self.assertEqual(Scripted.script, [])      # no rewrite call was made

    def test_plan_is_approved_and_runs(self):
        self.h.set_mode("plan")
        Scripted.script = [
            call("create_plan", title="Say hi", goal="g",
                 steps=[{"title": "Write hi.txt", "details": "d", "files": ["hi.txt"]}]),
            call("write_file", path="hi.txt", content="hi\n"),
            call("complete_step", summary="wrote hi.txt"),
        ]
        self.h.send("add a hi file")
        self.assertEqual((self.wd / "hi.txt").read_text(), "hi\n")
        self.assertEqual(self.h.mode, "coding")
        self.assertIsNone(self.h.plan)

    def test_system_prompt_says_so_only_when_unattended(self):
        line = "Non-interactive run"
        self.h.rebuild_system_prompt()
        self.assertIn(line, self.h.messages[0]["content"])
        self.h.unattended = None
        self.h.rebuild_system_prompt()
        self.assertNotIn(line, self.h.messages[0]["content"])


class Run(Base):
    def run_headless(self, prompt="go", **kw):
        out, err = io.StringIO(), io.StringIO()
        code = headless.run(self.h, prompt, out=out, err=err, **kw)
        return code, out.getvalue(), err.getvalue()

    def test_answer_on_stdout_progress_on_stderr(self):
        Scripted.script = [call("write_file", path="a.txt", content="x\n"), reply("All done.")]
        code, out, err = self.run_headless()
        self.assertEqual(code, headless.EXIT_OK)
        self.assertEqual(out, "All done.\n")
        self.assertIn("→ write_file  a.txt", err)
        self.assertIn("created a.txt (+1 −0)", err)
        self.assertNotIn("All done.", err)

    def test_preamble_is_shown_before_its_tool_call(self):
        # Live (Gemma 4 26B): text alongside a call was held as the answer and
        # only printed after the next reply, i.e. below the calls it described.
        Scripted.script = [
            ChatResponse(content="I will look at the files first.", done_reason="stop",
                         tool_calls=[ToolCall(name="list_directory", arguments={"path": "."})]),
            call("write_file", path="a.txt", content="x\n"),
            reply("All done.")]
        code, out, err = self.run_headless()
        self.assertEqual((code, out), (headless.EXIT_OK, "All done.\n"))
        self.assertIn("I will look at the files first.", err)
        self.assertLess(err.index("I will look at the files first."), err.index("→ list_directory"))
        self.assertLess(err.index("→ list_directory"), err.index("→ write_file"))

    def test_quiet_leaves_stderr_empty(self):
        Scripted.script = [call("write_file", path="a.txt", content="x\n"), reply("All done.")]
        code, out, err = self.run_headless(quiet=True)
        self.assertEqual((code, out, err), (headless.EXIT_OK, "All done.\n", ""))

    def test_session_is_saved(self):
        Scripted.script = [reply("Hello.")]
        self.run_headless("hello")
        saved = list((self.home / "sessions").glob("*.json"))
        self.assertEqual(len(saved), 1)
        self.assertIn("hello", saved[0].read_text())

    def test_unreachable_backend_exits_1(self):
        Scripted.models = []
        self.h.event_queue.put(ChatEvent("system", self.h.check_backend()))
        code, out, err = self.run_headless()
        self.assertEqual((code, out), (headless.EXIT_ERROR, ""))
        self.assertIn("not reachable", err)
        code, _, err = self.run_headless(quiet=True)
        self.assertEqual(code, headless.EXIT_ERROR)
        self.assertIn("not reachable", err)       # even --quiet says why

    def test_backend_error_exits_1(self):
        Scripted.script = [RuntimeError("model not found")]
        code, out, err = self.run_headless()
        self.assertEqual(code, headless.EXIT_ERROR)
        self.assertIn("ERROR", err)

    def test_timeout_exits_124(self):
        Scripted.hang = True
        code, _out, err = self.run_headless(timeout=0.3)
        self.assertEqual(code, headless.EXIT_TIMEOUT)
        self.assertIn("--timeout", err)

    def test_paused_plan_exits_3(self):
        self.h.unattended = "decline"
        self.h.set_mode("plan")
        Scripted.script = [
            call("create_plan", title="Two", goal="g",
                 steps=[{"title": "One", "details": "d", "files": ["a.txt"]},
                        {"title": "Two", "details": "d", "files": ["b.txt"]}]),
        ]
        with mock.patch.object(hmod.Harness, "_execute_plan",
                               lambda self: None):    # the plan stops before any step
            code, _out, err = self.run_headless()
        self.assertEqual(code, headless.EXIT_PLAN_STOPPED)
        self.assertIn("The plan stopped at step 1/2", err)

    def test_control_characters_are_stripped(self):
        Scripted.script = [call("write_file", path="a\x1b[31m.txt", content="x"), reply("ok")]
        _code, _out, err = self.run_headless()
        self.assertNotIn("\x1b", err)


class Cli(unittest.TestCase):
    def momo(self, *args, stdin=""):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, PYTHONPATH=str(REPO))
            return subprocess.run([sys.executable, "-m", "harness.main", *args], cwd=REPO,
                                  env=env, input=stdin, capture_output=True, text=True,
                                  timeout=60)

    def test_headless_flags_need_p(self):
        for flags in (["--yes"], ["--quiet"], ["--timeout", "5"], ["--continue"],
                      ["--session", "x"]):
            r = self.momo(*flags)
            self.assertEqual(r.returncode, 2, flags)
            self.assertIn("only applies with -p", r.stderr)

    def test_p_conflicts(self):
        self.assertIn("drop --web-only", self.momo("-p", "x", "--web-only").stderr)
        self.assertIn("use one", self.momo("-p", "x", "--continue", "--session", "s").stderr)
        self.assertIn("positive", self.momo("-p", "x", "--timeout", "0").stderr)
        self.assertIn("stdin is empty", self.momo("-p", "-").stderr)
        self.assertIn("no saved session", self.momo("-p", "x", "--session", "nope").stderr)

    def test_headless_is_the_old_web_only(self):
        r = self.momo("--headless", "--no-web")
        self.assertIn("--headless is now --web-only", r.stderr)
        self.assertIn("--web-only needs the web UI", r.stderr)

    def test_unreachable_backend(self):
        # Nothing listens on port 9: the run reports it and exits 1, without hanging.
        r = self.momo("-p", "hi", "--quiet", "--no-index",
                      "--provider", "llamacpp", "--host", "http://127.0.0.1:9")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertIn("not reachable", r.stderr)


class Prompt(unittest.TestCase):
    """_headless_prompt against real pipes and files: what stdin is decides
    whether it is read (a non-TTY stdin that never closes must not hang)."""

    def prompt(self, text, stdin):
        from harness import main as main_mod
        parser = mock.Mock(error=mock.Mock(side_effect=SystemExit(2)))
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(main_mod, "_STDIN_WAIT", 0.2):
            return main_mod._headless_prompt(text, parser)

    def pipe(self, data: str | None):
        """A pipe as stdin; data=None leaves the write end open (never EOF)."""
        r, w = os.pipe()
        if data is not None:
            os.write(w, data.encode())
            os.close(w)
        else:
            self.addCleanup(os.close, w)
        f = os.fdopen(r)
        self.addCleanup(f.close)
        return f

    def test_dash_reads_stdin(self):
        self.assertEqual(self.prompt("-", self.pipe("explain this\n")), "explain this\n")

    def test_piped_stdin_is_attached(self):
        got = self.prompt("why does this fail?", self.pipe("Traceback...\n"))
        self.assertTrue(got.startswith("why does this fail?"))
        self.assertIn('<attachment name="stdin"', got)
        self.assertIn("Traceback...", got)

    def test_file_stdin_is_attached(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".log") as f:
            f.write("log line\n")
            f.flush()
            f.seek(0)
            self.assertIn("log line", self.prompt("summarise", f))

    def test_open_pipe_without_data_does_not_hang(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(self.prompt("hi", self.pipe(None)), "hi")
        self.assertIn("not attached", err.getvalue())

    def test_character_device_is_not_read(self):
        with open(os.devnull) as devnull:
            self.assertEqual(self.prompt("hi", devnull), "hi")

    def test_empty_pipe_keeps_the_prompt(self):
        self.assertEqual(self.prompt("hi", self.pipe("")), "hi")


if __name__ == "__main__":
    unittest.main()
