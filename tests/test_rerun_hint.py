"""Notes the harness adds from the history of the turn: a run_command repeated
while nothing was written points at the earlier log (the 9B re-ran a test
suite 8 times to grep other slices of it), and a call that failed made again
verbatim is called out (it sent one failing edit_file twice).

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import tempfile
import unittest
from pathlib import Path

import harness.harness as hmod
from harness.llm.base import ChatResponse, LLMClient, ToolCall

SUITE = "printf 'FAIL: a\\nok\\n'; exit 1"
REPEAT = "You made this exact call just before"


def _call(name, **args):
    return ChatResponse(content="", tool_calls=[ToolCall(name=name, arguments=args)],
                        done_reason="stop")


class ScriptedClient(LLMClient):
    """Replies with the tool calls in `script`, one per turn, then a plain answer."""
    provider_name = "fake"
    script: list = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.replies = list(type(self).script)

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return self.replies.pop(0) if self.replies else ChatResponse(content="Done.", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)
        (self.wd / "a.py").write_text("x = 1\n")
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: ScriptedClient("http://fake", "fake")

    def tearDown(self):
        hmod.make_client = self._orig
        self.h.logger.close()
        self.tmp.cleanup()

    def run_script(self, script, run_mode="new"):
        ScriptedClient.script = script
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode("coding")
        self.h.stream = False
        self.h.run_mode = run_mode
        self.h.send("go")
        return [m["content"] for m in self.h.messages if m.get("role") == "tool"]


class RerunHint(Base):
    def test_unchanged_rerun_points_at_the_earlier_log(self):
        results = self.run_script([
            _call("run_command", command=SUITE + " 2>&1 | tail -5"),
            _call("read_file", path="a.py"),
            _call("run_command", command=SUITE + ' 2>&1 | grep "FAIL"'),
        ])
        self.assertFalse(results[0].startswith("[same command"))
        first_log = results[0].split("[output saved: ", 1)[1].split(" — ", 1)[0]
        self.assertTrue(results[2].startswith(
            f'[same command as {Path(first_log).name} and no files changed since — '
            f'command_output(path="{first_log}", grep="…")'), results[2])
        self.assertIn("exit code 1", results[2])        # it still ran

    def test_a_write_in_between_ends_it(self):
        results = self.run_script([
            _call("run_command", command=SUITE),
            _call("edit_file", path="a.py", old_string="x = 1", new_string="x = 2"),
            _call("run_command", command=SUITE),
        ])
        self.assertFalse(results[2].startswith("[same command"), results[2])

    def test_another_command_in_between_ends_it(self):
        results = self.run_script([
            _call("run_command", command=SUITE),
            _call("run_command", command="touch b.py"),
            _call("run_command", command=SUITE),
        ])
        self.assertFalse(results[2].startswith("[same command"), results[2])

    def test_classic_mode_has_no_log_to_point_at(self):
        results = self.run_script([
            _call("run_command", command=SUITE),
            _call("run_command", command=SUITE),
        ], run_mode="classic")
        self.assertFalse(results[1].startswith("[same command"), results[1])


class RepeatedFailure(Base):
    def test_second_identical_failure_is_called_out(self):
        bad = _call("edit_file", path="a.py", old_string="y = 1", new_string="y = 2")
        results = self.run_script([bad, bad])
        self.assertTrue(results[0].startswith("ERROR"))
        self.assertNotIn(REPEAT, results[0])
        self.assertTrue(results[1].startswith("ERROR"))
        self.assertIn(REPEAT, results[1])

    def test_a_changed_call_is_not(self):
        results = self.run_script([
            _call("edit_file", path="a.py", old_string="y = 1", new_string="y = 2"),
            _call("edit_file", path="a.py", old_string="z = 1", new_string="z = 2"),
        ])
        self.assertNotIn(REPEAT, results[1])


if __name__ == "__main__":
    unittest.main()
