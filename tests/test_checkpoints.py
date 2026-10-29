"""File checkpoints (harness/checkpoints.py): what the model's write tools
changed, per turn, put back by /undo, a single file revert, or accepted by
/keep — and the commands, the harness wiring and the Retry offer around them."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import checkpoints as cp_mod
from harness import commands
from harness import session as session_mod
from harness.checkpoints import CheckpointStore
from harness.controller import Controller
from harness.llm.base import ChatResponse, ToolCall
from harness.tools import dispatch

from test_run_output import _Scripted


class _Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.wd = self.home / "project"
        self.wd.mkdir()
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        self.store = CheckpointStore(self.wd, "s1")
        self.turn = 0

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def begin(self, label="change"):
        self.turn += 1
        self.store.begin_turn(self.turn, label)

    def call(self, name, **args):
        return dispatch(name, args, self.wd, checkpoints=self.store)

    def write(self, path, text):
        return self.call("write_file", path=path, content=text)

    def read(self, path):
        return (self.wd / path).read_text()

    def ops(self, turn=0):
        return [(f["op"], f["rel"]) for f in self.store.turns()[turn]["files"]]


class Recording(_Store):
    def test_first_touch_keeps_the_state_before_the_turn(self):
        (self.wd / "a.py").write_text("x = 1\n")
        self.begin()
        self.write("a.py", "x = 2\n")
        self.call("edit_file", path="a.py", old_string="x = 2", new_string="x = 3")
        self.assertEqual(self.ops(), [("M", "a.py")])
        d = self.store.diff(str(self.wd / "a.py"), self.store.turns()[0]["id"])
        self.assertEqual((d["old"], d["new"]), ("x = 1\n", "x = 3\n"))
        self.assertEqual((d["added"], d["removed"]), (1, 1))

    def test_a_failed_or_empty_write_leaves_no_checkpoint(self):
        (self.wd / "a.py").write_text("x = 1\n")
        self.begin()
        out = self.call("edit_file", path="a.py", old_string="nope", new_string="y")
        self.assertTrue(out.startswith("ERROR") or "not found" in out.lower(), out)
        self.write("a.py", "x = 1\n")              # same content: "No change"
        self.assertEqual(self.store.turns(), [])
        self.assertFalse((self.wd / ".momo" / "checkpoints" / "s1" / "index.json").exists())

    def test_momo_dir_and_paths_outside_are_not_tracked(self):
        self.begin()
        self.call("scratch_write", path="probe.py", content="print(1)\n")
        self.write(".momo/notes.txt", "hi\n")
        self.call("write_file", path="../outside.txt", content="no\n")
        self.assertEqual(self.store.turns(), [])

    def test_each_turn_is_its_own_checkpoint_newest_first(self):
        self.begin("one")
        self.write("a.py", "1\n")
        self.begin("two")
        self.write("b.py", "2\n")
        self.write("a.py", "11\n")
        turns = self.store.turns()
        self.assertEqual([t["label"] for t in turns], ["two", "one"])
        self.assertEqual(self.ops(0), [("A", "b.py"), ("M", "a.py")])
        self.assertEqual(self.ops(1), [("A", "a.py")])
        self.assertEqual(self.store.files_changed(), 2)

    def test_commands_are_noted(self):
        self.begin()
        self.call("run_command", command="true")
        self.write("a.py", "1\n")
        self.assertTrue(self.store.turns()[0]["ran_commands"])
        self.assertTrue(self.store.preview()["ran_commands"])
        self.assertIn("not tracked", cp_mod.describe(self.store.preview()))

    def test_it_survives_a_restart(self):
        self.begin()
        self.write("a.py", "1\n")
        again = CheckpointStore(self.wd, "s1")
        self.assertEqual(again.count(), 1)
        again.begin_turn(2, "next")
        again.record(["b.py"], "write_file")
        self.assertEqual({t["id"] for t in again.turns()}, {1, 2})

    def test_the_plan_step_it_ran_survives_a_restart(self):
        ref = {"plan": "Calc", "phase": 1, "story": "S1"}
        self.store.begin_turn(1, "story", step="P1 S1", plan=ref)
        self.write("a.py", "1\n")
        self.begin()
        self.write("b.py", "1\n")
        again = CheckpointStore(self.wd, "s1")
        self.assertEqual([t["plan"] for t in again.turns()], [None, ref])

    def test_a_plan_ref_that_is_not_a_dict_is_dropped_on_load(self):
        self.store.begin_turn(1, "step", plan={"plan": "Calc", "step": 0, "title": "A"})
        self.write("a.py", "1\n")
        self.store._turns[0]["plan"] = "step 0"
        self.store._save()
        self.assertNotIn("plan", CheckpointStore(self.wd, "s1")._turns[0])

    def test_big_files_are_noted_not_saved(self):
        big = self.wd / "big.bin"
        big.write_bytes(b"x" * (cp_mod.MAX_FILE_BYTES + 1))
        self.begin()
        self.call("delete_file", path="big.bin")
        row = self.store.turns()[0]["files"][0]
        self.assertEqual((row["op"], row["restorable"]), ("D", False))
        r = self.store.undo_to()
        self.assertEqual(r["failed"], [("big.bin", "too big to save")])
        self.assertFalse(big.exists())


class Tampering(_Store):
    """The index is in .momo/, which the model can write to: what it says must
    never make undo write outside the workdir."""

    def test_entries_outside_the_workdir_are_dropped_on_load(self):
        import json
        self.begin()
        self.write("a.py", "1\n")
        index = self.wd / ".momo/checkpoints/s1/index.json"
        data = json.loads(index.read_text())
        files = data["turns"][0]["files"]
        entry = next(iter(files.values()))
        victim = self.home / "victim.txt"
        files[str(victim)] = {**entry, "rel": "victim.txt"}
        files[str(self.wd / ".momo" / "x")] = {**entry, "rel": ".momo/x"}
        files[str(self.wd / "sub" / ".." / ".." / "escape.txt")] = {**entry, "rel": "escape"}
        index.write_text(json.dumps(data))
        again = CheckpointStore(self.wd, "s1")
        self.assertEqual([f["rel"] for f in again.turns()[0]["files"]], ["a.py"])
        again.undo_to(force=True)
        self.assertFalse(victim.exists())
        self.assertFalse((self.home / "escape.txt").exists())

    def test_a_blob_id_names_only_a_blob(self):
        import json
        secret = self.home / "secret.txt"
        secret.write_text("private\n")
        (self.wd / "a.py").write_text("0\n")
        self.begin()
        self.write("a.py", "1\n")
        index = self.wd / ".momo/checkpoints/s1/index.json"
        data = json.loads(index.read_text())
        next(iter(data["turns"][0]["files"].values()))["before"]["blob"] = "../../../../secret.txt"
        index.write_text(json.dumps(data))
        r = CheckpointStore(self.wd, "s1").undo_to(force=True)
        self.assertEqual(r["failed"], [("a.py", "its saved copy is missing")])
        self.assertEqual(self.read("a.py"), "1\n")

    def test_a_folder_linked_out_is_not_written_through(self):
        self.begin()
        (self.wd / "pkg").mkdir()
        self.write("pkg/m.py", "1\n")
        outside = self.home / "outside"
        outside.mkdir()
        (self.wd / "pkg" / "m.py").unlink()
        (self.wd / "pkg").rmdir()
        (self.wd / "pkg").symlink_to(outside)        # the folder now points out
        r = self.store.undo_to(force=True)
        self.assertEqual(r["failed"], [("pkg/m.py", "outside the working directory")])
        self.assertEqual(list(outside.iterdir()), [])


class Undo(_Store):
    def test_undo_goes_back_one_turn_at_a_time(self):
        (self.wd / "a.py").write_text("v0\n")
        for v in ("v1\n", "v2\n", "v3\n"):
            self.begin()
            self.write("a.py", v)
        self.store.undo_to()
        self.assertEqual(self.read("a.py"), "v2\n")
        self.store.undo_to()
        self.assertEqual(self.read("a.py"), "v1\n")
        self.store.undo_to()
        self.assertEqual(self.read("a.py"), "v0\n")
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.undo_to(), {"restored": [], "skipped": [], "failed": [],
                                                "ids": []})

    def test_undo_to_a_checkpoint_takes_every_later_one_back(self):
        (self.wd / "a.py").write_text("v0\n")
        self.begin()
        self.write("a.py", "v1\n")
        first = self.store.turns()[0]["id"]
        self.begin()
        self.write("a.py", "v2\n")
        self.write("b.py", "new\n")
        plan = self.store.preview(first)
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(sorted(plan["restore"]), ["a.py", "b.py"])
        r = self.store.undo_to(first)
        self.assertEqual(self.read("a.py"), "v0\n")
        self.assertFalse((self.wd / "b.py").exists())
        self.assertEqual(len(r["ids"]), 2)

    def test_create_delete_and_mode(self):
        f = self.wd / "run.sh"
        f.write_text("echo hi\n")
        os.chmod(f, 0o755)
        self.begin()
        self.call("delete_file", path="run.sh")
        self.write("made.py", "x\n")
        self.store.undo_to()
        self.assertEqual(f.read_text(), "echo hi\n")
        self.assertEqual(f.stat().st_mode & 0o777, 0o755)
        self.assertFalse((self.wd / "made.py").exists())

    def test_a_file_rename_shows_as_one_row_and_goes_back(self):
        (self.wd / "old.py").write_text("x\n")
        self.begin()
        self.call("move_file", src="old.py", dst="pkg/new.py")
        rows = self.store.turns()[0]["files"]
        self.assertEqual([(r["op"], r.get("from"), r["rel"]) for r in rows],
                         [("R", "old.py", "pkg/new.py")])
        self.store.undo_to()
        self.assertEqual(self.read("old.py"), "x\n")
        self.assertFalse((self.wd / "pkg" / "new.py").exists())

    def test_a_folder_rename_goes_back_after_later_edits_inside_it(self):
        (self.wd / "src").mkdir()
        (self.wd / "src" / "m.py").write_text("a\n")
        self.begin()
        self.call("move_file", src="src", dst="lib")
        self.write("lib/m.py", "b\n")
        self.assertEqual(CheckpointStore(self.wd, "s1").count(), 1)    # saved, no blob needed
        self.store.undo_to()
        self.assertEqual(self.read("src/m.py"), "a\n")
        self.assertFalse((self.wd / "lib").exists())

    def test_a_link_is_moved_not_followed(self):
        (self.wd / "target.txt").write_text("t\n")
        (self.wd / "link").symlink_to("target.txt")
        self.begin()
        self.call("delete_file", path="link")
        self.assertEqual(CheckpointStore(self.wd, "s1").count(), 1)
        self.store.undo_to()
        self.assertEqual(os.readlink(self.wd / "link"), "target.txt")
        self.assertEqual(self.read("target.txt"), "t\n")

    def test_edit_files_across_files(self):
        (self.wd / "a.py").write_text("def f(): pass\n")
        (self.wd / "b.py").write_text("f()\n")
        self.begin()
        out = self.call("edit_files", edits=[
            {"path": "a.py", "old_string": "def f()", "new_string": "def g()"},
            {"path": "b.py", "old_string": "f()", "new_string": "g()"}])
        self.assertNotIn("ERROR", out)
        self.assertEqual(sorted(r for _, r in self.ops()), ["a.py", "b.py"])
        self.store.undo_to()
        self.assertEqual((self.read("a.py"), self.read("b.py")), ("def f(): pass\n", "f()\n"))

    def test_a_file_changed_since_is_skipped_unless_forced(self):
        (self.wd / "a.py").write_text("v0\n")
        self.begin()
        self.write("a.py", "v1\n")
        self.write("b.py", "new\n")
        (self.wd / "a.py").write_text("mine\n")        # the user edits it in their editor
        self.assertEqual(self.store.preview()["conflicts"], ["a.py"])
        r = self.store.undo_to()
        self.assertEqual((r["restored"], r["skipped"]), (["b.py"], ["a.py"]))
        self.assertEqual(self.read("a.py"), "mine\n")
        self.assertEqual(self.store.count(), 1)         # a.py is still in the stack
        self.store.undo_to(force=True)
        self.assertEqual(self.read("a.py"), "v0\n")
        self.assertEqual(self.store.count(), 0)

    def test_an_edit_between_turns_is_protected(self):
        (self.wd / "a.py").write_text("v0\n")
        self.begin()
        self.write("a.py", "v1\n")
        first = self.store.turns()[0]["id"]
        (self.wd / "a.py").write_text("mine\n")
        self.begin()
        self.write("a.py", "v2\n")
        self.store.undo_to(first)
        self.assertEqual(self.read("a.py"), "mine\n")   # the newer turn went, the edit stays

    def test_revert_one_file(self):
        (self.wd / "a.py").write_text("v0\n")
        self.begin()
        self.write("a.py", "v1\n")
        self.write("b.py", "new\n")
        self.begin()
        self.write("a.py", "v2\n")
        self.assertEqual(self.store.revert_file(str(self.wd / "a.py")), "")
        self.assertEqual(self.read("a.py"), "v0\n")
        self.assertEqual(self.read("b.py"), "new\n")
        self.assertEqual([[f["rel"] for f in t["files"]] for t in self.store.turns()], [["b.py"]])
        self.assertIn("not changed", self.store.revert_file(str(self.wd / "a.py")))

    def test_since_keep_is_the_net_change(self):
        (self.wd / "a.py").write_text("v0\n")
        self.begin()
        self.write("a.py", "v1\n")
        self.write("tmp.py", "x\n")
        self.begin()
        self.write("a.py", "v0\n")                      # back as it was
        self.call("delete_file", path="tmp.py")
        self.write("c.py", "one\ntwo\n")
        rows = self.store.cumulative()
        self.assertEqual([(r["op"], r["rel"], r["added"]) for r in rows], [("A", "c.py", 2)])
        d = self.store.diff(str(self.wd / "c.py"))
        self.assertEqual((d["old"], d["new"]), ("", "one\ntwo\n"))
        self.assertIsNone(self.store.diff(str(self.wd / "nothing.py")))

    def test_keep_forgets_everything(self):
        self.begin()
        self.write("a.py", "1\n")
        self.begin()
        self.write("b.py", "2\n")
        self.assertEqual(self.store.keep(), (2, 2))
        self.assertEqual(self.store.count(), 0)
        self.assertFalse((self.wd / ".momo" / "checkpoints" / "s1").exists())
        self.assertEqual(self.read("a.py"), "1\n")
        self.assertEqual(self.store.undo_to()["restored"], [])


class Cap(_Store):
    def test_the_oldest_go_past_the_cap_with_one_notice(self):
        notes = []
        self.store.max_turns, self.store.note = 2, notes.append
        for i in range(4):
            self.begin(f"t{i}")
            self.write("a.py", f"{i}\n")
        self.assertEqual([t["label"] for t in self.store.turns()], ["t3", "t2"])
        self.assertEqual(len(notes), 1)
        self.assertIn("/undo-max", notes[0])

    def test_no_cap(self):
        self.store.max_turns = 0
        for i in range(25):
            self.begin()
            self.write("a.py", f"{i}\n")
        self.assertEqual(self.store.count(), 25)

    def test_lowering_the_cap_trims_now(self):
        for i in range(5):
            self.begin()
            self.write("a.py", f"{i}\n")
        self.assertEqual(self.store.would_drop(2), 3)
        self.assertEqual(self.store.set_max_turns(2), 3)
        self.assertEqual(self.store.count(), 2)
        blobs = list((self.wd / ".momo/checkpoints/s1/blobs").iterdir())
        self.assertEqual(len(blobs), 3)                 # 2, 3, 4: the dropped ones are gone


class HarnessWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                        mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
                        mock.patch.object(hmod, "make_client",
                                          lambda *a, **k: _Scripted("http://fake", "fake"))]
        for p in self.patches:
            p.start()
        self.wd = self.home / "project"
        self.wd.mkdir()
        (self.wd / "a.py").write_text("v0\n")
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode("coding")
        self.h.stream = False

    def tearDown(self):
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def turn(self, text, content):
        _Scripted.replies = [ChatResponse(tool_calls=[ToolCall(
            name="write_file", arguments={"path": "a.py", "content": content})])]
        self.h.send(text)

    def cmd(self, text):
        return commands.handle(text, self.h)

    def test_each_message_is_a_checkpoint_and_undo_tells_the_model(self):
        self.turn("make it v1", "v1\n")
        self.turn("make it v2", "v2\n")
        turns = self.h.checkpoints.turns()
        self.assertEqual([(t["turn"], t["label"]) for t in turns],
                         [(2, "make it v2"), (1, "make it v1")])
        self.assertEqual(self.h.status_event().undo_count, 2)
        res = self.cmd("/undo")
        self.assertIn("Restore: a.py", res.confirm_prompt)
        self.assertIn("Reverted 1 file (back to before turn 2)", res.confirm_action())
        self.assertEqual((self.wd / "a.py").read_text(), "v1\n")
        _Scripted.replies = [ChatResponse(content="ok", done_reason="stop")]
        self.h.send("thanks")
        user = [m for m in self.h.messages if m["role"] == "user"][-1]["content"]
        self.assertIn("reverted these files", user)
        self.assertIn("a.py", user)
        self.assertTrue(user.endswith("thanks"))

    def test_undo_list_and_numbers(self):
        self.turn("one", "v1\n")
        self.turn("two", "v2\n")
        listing = self.cmd("/undo list").output
        self.assertIn("#2  turn 2 · two", listing)
        self.assertIn("M a.py  +1 −1", listing)
        self.assertIn("No checkpoint #9", self.cmd("/undo 9").output)
        self.assertIn("Usage", self.cmd("/undo sideways").output)
        res = self.cmd("/undo #1")
        self.assertIn("2 checkpoints (#1–#2)", res.confirm_prompt)
        res.confirm_action()
        self.assertEqual((self.wd / "a.py").read_text(), "v0\n")
        self.assertIn("Nothing to undo", self.cmd("/undo").output)

    def test_undo_without_force_reports_a_conflict(self):
        self.turn("one", "v1\n")
        (self.wd / "a.py").write_text("mine\n")
        self.assertIn("Nothing can be undone", self.cmd("/undo").output)
        res = self.cmd("/undo force")
        self.assertIn("overwritten: a.py", res.confirm_prompt)
        res.confirm_action()
        self.assertEqual((self.wd / "a.py").read_text(), "v0\n")

    def test_keep_and_clear(self):
        self.turn("one", "v1\n")
        res = self.cmd("/keep")
        self.assertIn("1 file from 1 checkpoint", res.confirm_prompt)
        self.assertIn("Kept 1 file", res.confirm_action())
        self.assertIn("Nothing to keep", self.cmd("/keep").output)
        self.turn("two", "v2\n")
        self.cmd("/clear")
        self.assertEqual(self.h.checkpoints.count(), 0)
        self.turn("three", "v3\n")
        self.h.new_session()
        self.assertEqual(self.h.checkpoints.count(), 0)

    def test_undo_max_command_saves_and_trims(self):
        self.assertIn("20 turns", self.cmd("/undo-max").output)
        for i in range(3):
            self.turn(f"t{i}", f"v{i + 1}\n")
        res = self.cmd("/undo-max 1")
        self.assertIn("drops the 2 oldest", res.confirm_prompt)
        self.assertIn("dropped the 2 oldest", res.confirm_action())
        self.assertEqual(self.h.checkpoints.count(), 1)
        self.assertEqual(session_mod.load_prefs()["undo_max"], 1)
        self.assertIn("no cap", self.cmd("/undo-max off").output)
        self.assertEqual(self.h.undo_max, 0)
        self.assertIn("ERROR", self.cmd("/undo-max -3").output)
        self.assertIn("ERROR", self.cmd("/undo-max lots").output)

    def test_plan_steps_are_their_own_checkpoints(self):
        store = self.h.checkpoints
        self.h._turn_count = 4
        store.begin_turn(4, "parse config", step="step 2/5")
        store.after(store.record(["a.py"], "write_file"))
        self.assertEqual(store.turns(), [])              # nothing changed yet
        p = store.record(["a.py"], "write_file")
        (self.wd / "a.py").write_text("x\n")
        store.after(p)
        self.assertEqual(store.turns()[0]["step"], "step 2/5")
        self.assertIn("turn 4, step 2/5", self.h.undo_files())

    def test_a_rename_is_reported_once(self):
        _Scripted.replies = [ChatResponse(tool_calls=[ToolCall(
            name="move_file", arguments={"src": "a.py", "dst": "b.py"})])]
        self.h.send("rename it")
        self.assertIn("Reverted 1 file (back to before turn 1): b.py → a.py",
                      self.h.undo_files())
        self.assertEqual((self.wd / "a.py").read_text(), "v0\n")

    def test_changes_opens_the_menu(self):
        self.assertTrue(self.cmd("/changes").changes_menu)


class RetryOffer(HarnessWiring):
    def setUp(self):
        super().setUp()
        self.c = Controller(self.h)
        self.c._run_bg = lambda fn, *a: fn(*a)     # run the turn inline
        self.lines = []
        self.c._system = self.lines.append

    def test_retry_asks_and_reverts(self):
        self.turn("make it v1", "v1\n")
        _Scripted.replies = [ChatResponse(content="ok", done_reason="stop")]
        self.assertTrue(self.c.retry_last())
        self.assertIn("That reply changed 1 file", self.lines[-1])
        self.assertEqual((self.wd / "a.py").read_text(), "v1\n")    # not yet
        self.c.submit("y")
        self.assertEqual((self.wd / "a.py").read_text(), "v0\n")
        self.assertEqual(self.h.checkpoints.count(), 0)
        self.assertEqual(self.h.messages[-1]["content"], "ok")       # and it was retried

    def test_no_retries_without_reverting(self):
        self.turn("make it v1", "v1\n")
        _Scripted.replies = [ChatResponse(content="ok", done_reason="stop")]
        self.c.retry_last()
        self.c.submit("n")
        self.assertEqual((self.wd / "a.py").read_text(), "v1\n")
        self.assertEqual(self.h.messages[-1]["content"], "ok")

    def test_a_turn_without_file_changes_retries_at_once(self):
        _Scripted.replies = [ChatResponse(content="first", done_reason="stop")]
        self.h.send("hi")
        _Scripted.replies = [ChatResponse(content="second", done_reason="stop")]
        self.c.retry_last()
        self.assertEqual(self.h.messages[-1]["content"], "second")

    def test_file_action_is_refused_while_busy(self):
        self.c._busy = True
        self.assertFalse(self.c.file_action(self.h.keep_files)["ok"])
        self.c._busy = False
        self.assertTrue(self.c.file_action(self.h.keep_files)["ok"])


if __name__ == "__main__":
    unittest.main()
