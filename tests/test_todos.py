"""The todos tool: the model's own checklist in coding and momo modes.  The
whole list is sent on every call, kept through compaction (in the summary) and
through a saved session, and shown in the status bar.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import compaction
from harness import session as session_mod
from harness import todos
from harness.commands import handle as handle_command
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.tools import registry
from harness.tools.registry import ROLES, ToolContext
from home_tmp import setUpModule, tearDownModule  # noqa: F401

THREE = [{"text": "Rename parse()", "status": "done"},
         {"text": "Add a test", "status": "in_progress"},
         {"text": "Update the README", "status": "pending"}]


def _todos(items):
    return ChatResponse(tool_calls=[ToolCall("todos", {"items": items})], done_reason="stop")


DONE = ChatResponse(content="Done.", done_reason="stop")


class ScriptedClient(LLMClient):
    """Plays `script` in order (the last reply repeats); a request with no tools
    is the compaction summary call."""
    provider_name = "fake"
    script: list = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.n = 0

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        if not tools:
            return ChatResponse(content="## Goal\nwork\n\n## Next steps\n1. go on\n", done_reason="stop")
        self.n += 1
        return self.script[min(self.n, len(self.script)) - 1]

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class Rules(unittest.TestCase):
    def test_a_valid_list_is_kept_and_a_missing_status_is_pending(self):
        got = todos.validate([{"text": "  a   b "}, {"text": "c", "status": "DONE"}])
        self.assertEqual(got, [{"text": "a b", "status": "pending"}, {"text": "c", "status": "done"}])
        self.assertEqual(todos.validate([]), [])

    def test_bad_lists_are_errors(self):
        for bad in ("not a list", [{"text": ""}], [{"text": "a", "status": "later"}],
                    [{"text": str(i)} for i in range(todos.MAX_ITEMS + 1)],
                    [{"text": "a", "status": "in_progress"}, {"text": "b", "status": "in_progress"}],
                    [42]):
            with self.subTest(bad=bad):
                self.assertTrue(todos.validate(bad).startswith("ERROR:"))

    def test_render_and_progress(self):
        text = todos.render(THREE)
        self.assertEqual(text.splitlines()[:3],
                         ["[x] Rename parse()", "[>] Add a test", "[ ] Update the README"])
        self.assertIn("1 of 3 done", text)
        self.assertNotIn("All items are done", text)
        self.assertEqual(todos.progress(THREE), "1/3")
        self.assertEqual(todos.progress([]), "")
        self.assertIn("All items are done", todos.render([{"text": "a", "status": "done"}]))

    def test_summary_line_only_while_an_item_is_open(self):
        self.assertIn("[ ] Update the README", todos.summary_line(THREE))
        self.assertEqual(todos.summary_line([{"text": "a", "status": "done"}]), "")
        self.assertEqual(todos.summary_line([]), "")


class Offered(unittest.TestCase):
    def test_only_coding_and_momo_offer_it(self):
        for role in ROLES:
            names = {t["function"]["name"] for t in registry.offered(ToolContext(role=role))}
            with self.subTest(role=role):
                self.assertEqual("todos" in names, role in ("coding", "momo"))


class InTheLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(hmod, "make_client", lambda *a, **k: ScriptedClient("http://fake", "fake")),
            mock.patch.object(session_mod, "SESSION_DIR", self.wd / "sessions"),
        ]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode("coding")
        self.h.stream = False

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def run_script(self, script):
        ScriptedClient.script = script
        self.h.send("go")
        return [m["content"] for m in self.h.messages if m.get("role") == "tool"]

    def test_a_call_keeps_the_list_and_shows_it(self):
        [result] = self.run_script([_todos(THREE), DONE])
        self.assertEqual(self.h.todos, THREE)
        self.assertIn("[>] Add a test", result)
        status = self.h.status_event()
        self.assertEqual(status.todos_progress, "1/3")
        self.assertEqual(status.todos, THREE)

    def test_the_next_call_replaces_the_list(self):
        self.run_script([_todos(THREE), _todos([{"text": "Only this", "status": "pending"}]), DONE])
        self.assertEqual(self.h.todos, [{"text": "Only this", "status": "pending"}])

    def test_an_invalid_list_leaves_the_old_one(self):
        self.h.todos = list(THREE)
        [result] = self.run_script([_todos([{"text": "a", "status": "in_progress"},
                                            {"text": "b", "status": "in_progress"}]), DONE])
        self.assertTrue(result.startswith("ERROR:"))
        self.assertEqual(self.h.todos, THREE)

    def test_compaction_keeps_the_open_items(self):
        self.h.todos = list(THREE)
        self.h.messages = self.h.messages[:1]
        for n in range(30):
            self.h.messages += [{"role": "user", "content": f"q{n} " + "q" * 800},
                                {"role": "assistant", "content": "a" * 3000}]
        self.h.messages.append({"role": "user", "content": "now"})
        self.h._turn_user = self.h.messages[-1]
        self.h.context_limit = compaction.msg_tokens(self.h.messages[0]) + 6000
        self.h.compact()
        text = "\n".join(str(m.get("content") or "") for m in self.h.messages)
        self.assertIn("## Todo list", text)
        self.assertIn("[ ] Update the README", text)

    def test_compaction_leaves_out_a_finished_list(self):
        self.h.todos = [{"text": "a", "status": "done"}]
        self.assertEqual(self.h._todos_summary_line(), "")

    def test_a_saved_session_keeps_the_list_and_a_new_one_clears_it(self):
        self.run_script([_todos(THREE), DONE])
        path = self.h.session_path()
        self.assertEqual(json.loads(path.read_text())["todos"], THREE)
        self.h.new_session()
        self.assertEqual(self.h.todos, [])
        self.h.load_session(path)
        self.assertEqual(self.h.todos, THREE)

    def test_an_old_session_without_todos_loads_empty(self):
        self.run_script([DONE])
        path = self.h.session_path()
        data = json.loads(path.read_text())
        data.pop("todos")
        path.write_text(json.dumps(data))
        self.h.todos = list(THREE)
        self.h.load_session(path)
        self.assertEqual(self.h.todos, [])

    def test_the_todos_command(self):
        self.assertIn("No todo list", handle_command("/todos", self.h).output)
        self.h.todos = list(THREE)
        self.assertIn("[>] Add a test", handle_command("/todos", self.h).output)
        self.assertIn("cleared", handle_command("/todos clear", self.h).output)
        self.assertEqual(self.h.todos, [])
        self.assertTrue(handle_command("/todos nope", self.h).output.startswith("ERROR"))


if __name__ == "__main__":
    unittest.main()
