"""Which session a launch reopens, and what /clear leaves behind.  A cleared
session used to stay on disk as it was, so the next launch brought its plan
back into the system prompt; an unused /new session was skipped for the older
one; and the compaction summary outlived /clear, so the next compaction wrote
the old requests into the new conversation's first message.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness.commands import handle as handle_command
from harness.llm.base import ChatResponse, LLMClient
from harness.plan import Plan, Step
from home_tmp import setUpModule, tearDownModule  # noqa: F401


class Echo(LLMClient):
    provider_name = "fake"

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return ChatResponse(content="ok", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class Restore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()   # /var -> /private/var on macOS
        self._patches = [
            mock.patch.object(hmod, "make_client", lambda *a, **k: Echo("http://fake", "fake")),
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
            # one-second names: a /new in the launch's second would reuse its file
            mock.patch.object(session_mod, "new_timestamp",
                              lambda it=iter(range(10**6)): f"2026-11-01T00-00-{next(it):06d}"),
        ]
        for p in self._patches:
            p.start()
        self.work = self.home / "work"          # workspace A
        self.work.mkdir()
        self.other = self.home / "other"        # workspace B
        self.other.mkdir()
        self.elsewhere = self.home / "elsewhere"   # a folder momo is started from
        self.elsewhere.mkdir()
        self.harnesses = []
        self.h = self.launch()

    def tearDown(self):
        for h in self.harnesses:
            h.logger.close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def launch(self, given=None, cwd=None, fresh=False):
        """What main.py does at startup: `given` is --workspace, `cwd` the folder
        momo is started from (default: workspace A)."""
        last, remembered = (None, None) if fresh else session_mod.restore_target(given)
        workdir = given or remembered or (cwd or self.work)
        h = hmod.Harness(host="http://fake", model="fake", workdir=workdir)
        h.stream = False
        h.session_titles = False
        self.harnesses.append(h)
        if last is None:
            h._mark_current()
        else:
            h.load_session(last)
            if given is not None or remembered is not None or not h.workdir.is_dir():
                if h.workdir.resolve() != workdir:
                    h.workdir = workdir
                    h._mark_current()
        return h

    def with_plan(self, text="plan OLD-FEATURE"):
        self.h.send(text)
        self.h.plan = Plan(title="OLD-FEATURE plan", goal="old", steps=[Step(title="old step")])
        self.h.plan_phase = "awaiting_approval"
        self.h._autosave()

    def users(self, h):
        return [m["content"] for m in h.messages if m.get("role") == "user"]

    def test_a_used_session_is_reopened_with_its_plan(self):
        self.with_plan()
        h2 = self.launch()
        self.assertEqual(self.users(h2), ["plan OLD-FEATURE"])
        self.assertEqual(h2.plan.title, "OLD-FEATURE plan")

    def test_a_cleared_session_does_not_come_back(self):
        self.with_plan()
        handle_command("/clear", self.h)
        self.assertFalse(session_mod.session_path(self.h._ts).exists())
        h2 = self.launch()
        self.assertEqual(self.users(h2), [])
        self.assertIsNone(h2.plan)
        h2.set_mode("plan")
        self.assertFalse(any("OLD-FEATURE" in s["text"] for s in h2.system_prompt_sections()))

    def test_after_clear_the_new_conversation_is_reopened(self):
        self.with_plan()
        handle_command("/clear", self.h)
        self.h.send("NEW work")
        h2 = self.launch()
        self.assertEqual(self.users(h2), ["NEW work"])
        self.assertIsNone(h2.plan)

    def test_an_unused_new_session_starts_empty_not_with_the_old_one(self):
        self.with_plan()
        handle_command("/new", self.h)
        h2 = self.launch()
        self.assertEqual(self.users(h2), [])
        self.assertIsNone(h2.plan)
        # the old session is still there to open on purpose
        self.assertEqual(len(session_mod.list_sessions()), 1)

    def test_an_opened_session_is_the_one_reopened(self):
        self.with_plan("first")
        first = self.h._ts
        handle_command("/new", self.h)
        self.h.send("second")
        self.h.load_session(session_mod.session_path(first))
        h2 = self.launch()
        self.assertEqual(self.users(h2), ["first"])

    def test_without_a_remembered_session_the_newest_is_reopened(self):
        self.with_plan()
        prefs = session_mod.load_prefs()
        for key in ("last_session", "last_workdir", "workspace_sessions"):
            prefs.pop(key)
        session_mod.prefs_path().write_text(__import__("json").dumps(prefs))
        h2 = self.launch()
        self.assertEqual(self.users(h2), ["plan OLD-FEATURE"])

    def test_clear_drops_what_was_carried_from_the_conversation(self):
        self.with_plan()
        self.h.summary_state["requests"].append("plan OLD-FEATURE")
        self.h.compacted = [{"n": 1}]
        self.h.title = "Old feature"
        handle_command("/clear", self.h)
        self.assertNotIn("OLD-FEATURE", str(self.h.summary_state))
        self.assertEqual((self.h.compacted, self.h.title, self.h.plan), ([], "", None))
        self.assertEqual(len(self.h.messages), 1)

    def test_clear_keeps_a_locked_title(self):
        self.h.send("hi")
        self.h.title, self.h.title_locked = "Mine", True
        handle_command("/clear", self.h)
        self.assertEqual(self.h.title, "Mine")


class Workspaces(Restore):
    """--workspace and the remembered workspace."""

    def test_no_argument_reopens_the_last_session_in_its_workspace(self):
        self.h.send("in A")
        h2 = self.launch(cwd=self.elsewhere)
        self.assertEqual((h2.workdir, self.users(h2)), (self.work, ["in A"]))

    def test_a_cleared_session_opens_empty_in_its_workspace(self):
        self.with_plan()
        handle_command("/clear", self.h)
        h2 = self.launch(cwd=self.elsewhere)
        self.assertEqual((h2.workdir, self.users(h2), h2.plan), (self.work, [], None))

    def test_a_workspace_change_in_an_empty_session_is_remembered(self):
        handle_command(f"/workspace {self.other}", self.h)
        h2 = self.launch(cwd=self.elsewhere)
        self.assertEqual((h2.workdir, self.users(h2)), (self.other, []))

    def test_a_workspace_change_after_the_last_save_is_remembered(self):
        self.h.send("in A")
        handle_command(f"/workspace {self.other}", self.h)
        h2 = self.launch(cwd=self.elsewhere)
        self.assertEqual((h2.workdir, self.users(h2)), (self.other, ["in A"]))

    def test_given_workspace_reopens_its_own_last_session(self):
        self.h.send("in A")
        handle_command("/new", self.h)
        handle_command(f"/workspace {self.other}", self.h)
        self.h.send("in B")
        handle_command("/new", self.h)
        handle_command(f"/workspace {self.work}", self.h)
        self.h.send("in A again")
        h2 = self.launch(given=self.other)
        self.assertEqual((h2.workdir, self.users(h2)), (self.other, ["in B"]))
        h3 = self.launch(given=self.work)
        self.assertEqual((h3.workdir, self.users(h3)), (self.work, ["in A again"]))

    def test_given_workspace_without_a_session_starts_empty_there(self):
        self.with_plan()
        h2 = self.launch(given=self.other)
        self.assertEqual((h2.workdir, self.users(h2), h2.plan), (self.other, [], None))
        # and that is now the last one
        h3 = self.launch(cwd=self.elsewhere)
        self.assertEqual((h3.workdir, self.users(h3)), (self.other, []))

    def test_given_workspace_whose_session_was_cleared_does_not_fall_back(self):
        self.h.send("older in A")
        handle_command("/new", self.h)
        self.with_plan()
        handle_command("/clear", self.h)
        h2 = self.launch(given=self.work)
        self.assertEqual((self.users(h2), h2.plan), ([], None))

    def test_a_session_moved_to_another_workspace_is_not_reopened_for_the_old_one(self):
        self.h.send("in A")
        handle_command(f"/workspace {self.other}", self.h)
        self.h.send("now in B")
        h2 = self.launch(given=self.work)
        self.assertEqual((h2.workdir, self.users(h2)), (self.work, []))

    def test_legacy_prefs_reopen_the_newest_session_of_the_given_workspace(self):
        self.h.send("in A")
        prefs = session_mod.load_prefs()
        for key in ("last_session", "last_workdir", "workspace_sessions"):
            prefs.pop(key)
        session_mod.prefs_path().write_text(__import__("json").dumps(prefs))
        h2 = self.launch(given=self.work)
        self.assertEqual(self.users(h2), ["in A"])
        h3 = self.launch(given=self.other)
        self.assertEqual(self.users(h3), [])

    def test_a_remembered_workspace_that_is_gone_falls_back_to_the_start_folder(self):
        gone = self.home / "gone"
        gone.mkdir()
        handle_command(f"/workspace {gone}", self.h)
        gone.rmdir()
        h2 = self.launch(cwd=self.elsewhere)
        self.assertEqual(h2.workdir, self.elsewhere)

    def test_fresh_reopens_nothing(self):
        self.h.send("in A")
        h2 = self.launch(cwd=self.elsewhere, fresh=True)
        self.assertEqual((h2.workdir, self.users(h2)), (self.elsewhere, []))


if __name__ == "__main__":
    unittest.main()
