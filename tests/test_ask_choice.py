"""ask_choice: a multiple-choice question the user answers by picking an option,
or by adding information that has the model rewrite the question."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness.llm.base import ChatResponse, LLMClient, ToolCall


class _Scripted(LLMClient):
    provider_name = "fake"
    replies: list[ChatResponse] = []
    prompts: list[str] = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        if not tools:   # the refinement call: no tools
            _Scripted.prompts.append(messages[-1]["content"])
        return self.replies.pop(0) if self.replies else ChatResponse(content="Done.",
                                                                     done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


def ask(question="Which test framework?", options=("pytest", "unittest")):
    return ChatResponse(content="", done_reason="stop", tool_calls=[
        ToolCall(name="ask_choice", arguments={"question": question, "options": list(options)})])


def refined(question, options):
    return ChatResponse(content=json.dumps({"question": question, "options": options}),
                        done_reason="stop")


class AskChoice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                        mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
                        mock.patch.object(hmod, "make_client",
                                          lambda *a, **k: _Scripted("http://fake", "fake"))]
        for p in self.patches:
            p.start()
        _Scripted.replies, _Scripted.prompts = [], []
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)
        self.h.set_mode("coding")

    def tearDown(self):
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def turn(self, *replies, answers=()):
        """Run a turn; returns the tool results and each question asked, with its options."""
        _Scripted.replies = list(replies)
        queued, asked = list(answers), []

        def fake_ask(question, options=None):
            asked.append((question, list(options or [])))
            return queued.pop(0)
        self.h._ask_user = fake_ask
        self.h.send("go")
        results = [m["content"] for m in self.h.messages if m.get("role") == "tool"]
        return results, asked

    def test_offered(self):
        self.assertIn("ask_choice", [t["function"]["name"] for t in self.h._current_tools()])

    def test_pick_by_number(self):
        results, asked = self.turn(ask(), answers=["2"])
        self.assertEqual(results, ["User chose option 2: unittest"])
        self.assertEqual(asked, [("Which test framework?", ["pytest", "unittest"])])

    def test_pick_by_label(self):
        results, _ = self.turn(ask(), answers=[" PyTest "])
        self.assertEqual(results, ["User chose option 1: pytest"])

    def test_information_refines_the_question(self):
        results, asked = self.turn(
            ask(), refined("Which Django test runner?", ["pytest-django", "Django's runner"]),
            answers=["it's a Django project", "1"])
        self.assertEqual(asked[1], ("Which Django test runner?",
                                    ["pytest-django", "Django's runner"]))
        self.assertIn("it's a Django project", _Scripted.prompts[0])
        self.assertIn("1. pytest", _Scripted.prompts[0])
        self.assertEqual(results, ["User chose option 1: pytest-django\n"
                                   "Context the user gave while choosing:\n"
                                   "- it's a Django project"])

    def test_unusable_refinement_returns_the_users_words(self):
        results, asked = self.turn(ask(), ChatResponse(content="Sure! pytest.", done_reason="stop"),
                                   answers=["it's a Django project"])
        self.assertEqual(len(asked), 1)
        self.assertTrue(results[0].startswith("User did not pick an option."))
        self.assertIn("- it's a Django project", results[0])
        self.assertIn("1. pytest", results[0])

    def test_refinement_rounds_are_capped(self):
        rounds = hmod._CHOICE_MAX_ROUNDS
        replies = [refined(f"Q{i}", ["a", "b"]) for i in range(rounds)]
        results, asked = self.turn(ask(), *replies,
                                   answers=[f"note {i}" for i in range(rounds + 1)])
        self.assertEqual(len(asked), rounds + 1)
        self.assertEqual(len(_Scripted.prompts), rounds)
        self.assertTrue(results[0].startswith("User did not pick an option."))
        self.assertIn(f"- note {rounds}", results[0])

    def test_bad_options_are_an_error(self):
        for options in (["only one"], "a, b", ["a", ""], [str(i) for i in range(7)]):
            with self.subTest(options=options):
                self.h.messages = self.h.messages[:1]
                results, asked = self.turn(
                    ChatResponse(content="", done_reason="stop", tool_calls=[
                        ToolCall(name="ask_choice",
                                 arguments={"question": "Q?", "options": options})]))
                self.assertEqual(asked, [])
                self.assertTrue(results[0].startswith("ERROR:"), results[0])


class Helpers(unittest.TestCase):
    def test_match_choice(self):
        opts = ["pytest", "unittest"]
        self.assertEqual(hmod._match_choice("2.", opts), 1)
        self.assertEqual(hmod._match_choice("UNITTEST", opts), 1)
        self.assertIsNone(hmod._match_choice("3", opts))
        self.assertIsNone(hmod._match_choice("pytest please", opts))


if __name__ == "__main__":
    unittest.main()
