"""harness/settings.py: the one table of saved settings.  These tests keep the
table, the prefs keys the code saves, the command-line flags, the /commands,
the status event and the web settings drawer in step."""
import argparse
import dataclasses
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import commands, settings
from harness.events import StatusEvent
from harness.llm.base import LLMClient
from home_tmp import setUpModule, tearDownModule  # noqa: F401

ROOT = Path(__file__).parent.parent / "harness"
INDEX_HTML = ROOT / "web" / "static" / "index.html"


class Fake(LLMClient):
    provider_name = "fake"

    def chat(self, *a, **k):
        raise AssertionError("no requests here")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


def harness(tmp):
    with mock.patch.object(hmod, "make_client", lambda *a, **k: Fake("http://fake", "fake")):
        return hmod.Harness(host="http://fake", model="fake", workdir=Path(tmp))


def parser():
    p = argparse.ArgumentParser()
    settings.add_arguments(p)
    return p


class Error(Exception):
    pass


def fail(msg):
    raise Error(msg)


class TableMatchesTheCode(unittest.TestCase):
    def test_every_saved_key_is_in_the_table(self):
        saved = set()
        for f in ROOT.rglob("*.py"):
            text = f.read_text()
            for call in re.findall(r"save_prefs\(([^()]*(?:\([^()]*\)[^()]*)*)\)", text):
                saved |= set(re.findall(r"(\w+)=", call))
            # the commands' way: commands._set / _set_result and settings.store
            saved |= set(re.findall(r"\b(?:_set|_set_result|store)\(\s*\w+,\s*\"(\w+)\"", text))
        saved.discard("kwargs")
        prefs = {s.pref for s in settings.SETTINGS}
        self.assertEqual(saved - prefs - settings.NOT_IN_TABLE, set())
        # and the table names nothing the code never saves
        self.assertEqual(prefs - saved, set())

    def test_every_setting_reaches_the_status_event(self):
        fields = {f.name for f in dataclasses.fields(StatusEvent)}
        attrs = {s.attr for s in settings.SETTINGS if s.attr}
        self.assertEqual(attrs - fields, set())
        with tempfile.TemporaryDirectory() as d:
            h = harness(d)
            h.think_budget, h.idle_recap_secs = 12345, 77
            ev = h.status_event()
            self.assertEqual((ev.think_budget, ev.idle_recap_secs), (12345, 77))
            h.logger.close()

    def test_the_web_drawer_names_real_settings(self):
        named = set(re.findall(r'data-setting="(\w+)"', INDEX_HTML.read_text()))
        attrs = {s.attr for s in settings.SETTINGS if s.attr}
        self.assertGreater(len(named), 20)
        self.assertEqual(named - attrs, set())

    def test_prefs_flags_and_attrs_are_unique(self):
        for field in ("pref", "flag", "attr"):
            vals = [getattr(s, field) for s in settings.SETTINGS if getattr(s, field)]
            self.assertEqual(len(vals), len(set(vals)), field)

    def test_every_attr_exists_and_its_default_parses_as_itself(self):
        with tempfile.TemporaryDirectory() as d:
            h = harness(d)
            for s in settings.SETTINGS:
                if s.attr is None:
                    continue
                with self.subTest(setting=s.attr):
                    self.assertTrue(hasattr(h, s.attr))
                    self.assertEqual(s.parse(getattr(h, s.attr)), (getattr(h, s.attr), ""))
            h.logger.close()


class Apply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = harness(self.tmp.name)

    def tearDown(self):
        self.h.logger.close()
        self.tmp.cleanup()

    def apply(self, argv=(), prefs=None):
        return settings.apply(self.h, parser().parse_args(list(argv)), prefs or {}, fail)

    def test_nothing_given_keeps_the_defaults(self):
        before = {s.attr: getattr(self.h, s.attr) for s in settings.SETTINGS if s.attr}
        self.assertEqual(self.apply(), {"index": True})
        self.assertEqual({a: getattr(self.h, a) for a in before}, before)

    def test_a_pref_is_applied(self):
        self.apply(prefs={"think": "high", "loop_guard": 0, "index": False, "guides": True,
                          "index_max_mem": 200 * 2**20, "plan_questions": "off"})
        self.assertEqual(self.h.think_level, "high")
        self.assertEqual(self.h.loop_guard, 0)
        self.assertTrue(self.h.guides)
        self.assertEqual(self.h.index_max_bytes, 200 * 2**20)
        self.assertFalse(self.h.plan_questions)

    def test_a_bad_pref_is_ignored(self):
        self.apply(prefs={"think": "loud", "undo_max": -3, "run_output_limit": 7,
                          "max_output_tokens": True})
        self.assertEqual(self.h.think_level, "on")
        self.assertEqual(self.h.undo_max, hmod.DEFAULT_UNDO_MAX)
        self.assertEqual(self.h.max_output_tokens, hmod.DEFAULT_MAX_OUTPUT_TOKENS)

    def test_a_flag_beats_the_pref(self):
        out = self.apply(["--think", "off", "--no-index", "--loop-guard", "off",
                          "--net-max-bytes", "500kb", "--think-repeat", "off"],
                         prefs={"think": "high", "index": True, "loop_guard": 5})
        self.assertEqual(self.h.think_level, "off")
        self.assertEqual(out["index"], False)
        self.assertEqual(self.h.loop_guard, 0)
        self.assertEqual(self.h.net_max_bytes, 500 * 1024)
        self.assertFalse(self.h.think_repeat)

    def test_a_bad_flag_is_a_usage_error(self):
        for argv in (["--max-output-tokens", "10"], ["--net-max-bytes", "lots"],
                     ["--loop-guard", "99"], ["--index-workers", "0"],
                     ["--net-max-chars", "5"], ["--undo-max", "-1"]):
            with self.subTest(argv=argv), self.assertRaises(Error) as cm:
                self.apply(argv)
            self.assertTrue(str(cm.exception).startswith(argv[0] + ":"))

    def test_aliases_and_old_saved_forms(self):
        self.apply(prefs={"tools_discover": True, "cavecat": next(iter(settings.CAVECAT_ALIASES)),
                          "idle_recap_secs": 3})
        self.assertEqual(self.h.tools_discover, "all")
        self.assertIn(self.h.cavecat, settings.CAVECAT_LEVELS)
        self.assertEqual(self.h.idle_recap_secs, 10)


class Commands(unittest.TestCase):
    """The /commands check what you type with the table's parsers and save it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = harness(self.tmp.name)
        self.saved = {}
        p = mock.patch.object(settings.session_mod, "save_prefs",
                              lambda **kw: self.saved.update(kw))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.h.logger.close()
        self.tmp.cleanup()

    def run_cmd(self, line):
        return commands.handle(line, self.h).output or ""

    def test_a_valid_value_is_set_and_saved(self):
        for line, attr, pref, want in [
            ("/max-output-tokens 30,000", "max_output_tokens", "max_output_tokens", 30000),
            ("/max-output-tokens off", "max_output_tokens", "max_output_tokens", 0),
            ("/think-budget 9_000", "think_budget", "think_budget", 9000),
            ("/loop-guard off", "loop_guard", "loop_guard", 0),
            ("/loop-guard resets 1", "loop_resets", "loop_resets", 1),
            ("/run-mode off", "run_mode", "run_mode", "classic"),
            ("/think YES", "think_level", "think", "on"),
            ("/cavecat full", "cavecat", "cavecat", "on"),
            ("/net-max-bytes 500kb", "net_max_bytes", "net_max_bytes", 500 * 1024),
            ("/index-workers auto", "index_workers", "index_workers", 0),
            ("/plan stories approve once", "stories_approve", "stories_approve", "auto"),
            ("/session-titles off", "session_titles", "session_titles", False),
            ("/companion-idle-recap 120", "idle_recap_secs", "idle_recap_secs", 120),
        ]:
            with self.subTest(line=line):
                out = self.run_cmd(line)
                self.assertNotIn("ERROR", out)
                self.assertEqual(getattr(self.h, attr), want)
                self.assertEqual(self.saved.get(pref), want)

    def test_a_bad_value_is_the_flags_error_and_changes_nothing(self):
        for line, pref in [("/max-output-tokens 10", "max_output_tokens"),
                           ("/loop-guard 99", "loop_guard"),
                           ("/net-max-bytes 10gb", "net_max_bytes"),
                           ("/net-max-chars 5", "net_max_chars"),
                           ("/tool-ref tiny", "tool_ref"),
                           ("/undo-max -2", "undo_max")]:
            with self.subTest(line=line):
                s = settings.by_pref(pref)
                before = getattr(self.h, s.attr)
                out = self.run_cmd(line)
                _, err = s.parse(line.split(maxsplit=1)[1])
                self.assertEqual(out, f"ERROR: {err}")
                self.assertEqual(getattr(self.h, s.attr), before)
                self.assertNotIn(pref, self.saved)

    def test_on_uses_the_current_or_default_value(self):
        self.h.loop_guard = 0
        self.run_cmd("/loop-guard on")
        self.assertEqual(self.h.loop_guard, settings.loop_guard_mod.THRESHOLD)
        self.h.think_budget = 0
        self.run_cmd("/think-budget on")
        self.assertEqual(self.h.think_budget, settings.think_watch_mod.DEFAULT_BUDGET)

    def test_a_status_event_follows_a_change(self):
        self.run_cmd("/think-budget repeat off")
        self.assertFalse(self.h.status_event().think_repeat)
        self.assertIs(self.saved.get("think_repeat"), False)


if __name__ == "__main__":
    unittest.main()
