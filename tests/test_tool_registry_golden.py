"""What the model sees of the tools, pinned for every configuration: the tool
schemas in order, the generated tool reference and the system prompt, across
role x run mode x edit features x code index x internet x TDD.

Recorded from the code before the tools were split into one file each
(harness/tools/), so the split could not change a word or an order the model
reads.  An intended change to a schema or prompt regenerates the fixture:

    MOMO_REGEN_GOLDEN=1 PYTHONPATH=.:tests .venv/bin/python -m pytest tests/test_tool_registry_golden.py
"""
import hashlib
import json
import os
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness import tools
from harness.editing import EDIT_FEATURES
from harness.plan import Plan, Step

from test_run_output import _Scripted

FIXTURE = Path(__file__).parent / "golden" / "tool_golden.json"
ROLES = ("design", "chat", "plan", "plan-tdd", "coding", "momo", "plan-exec", "plan-exec-tdd")
RUN_MODES = ("new", "classic")
EDIT_SETS = {"default": EDIT_FEATURES, "none": frozenset(), "match-only": frozenset({"match"})}
# Prompts that are stored whole (the rest by hash), so a mismatch can be read.
_FULL_PROMPTS = {f"{r}|new|default|off|off" for r in ROLES}


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


class _Golden:
    """Builds a Harness per configuration and reads what the model would see."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name).resolve()
        self.wd = home / "project"
        self.wd.mkdir()
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", home / "prefs.json"),
                        mock.patch.object(session_mod, "SESSION_DIR", home / "sessions"),
                        mock.patch.object(hmod, "make_client",
                                          lambda *a, **k: _Scripted("http://fake", "fake"))]
        for p in self.patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        return self

    def __exit__(self, *exc):
        self.h.index = None
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def configure(self, role, run_mode, edits, index, net):
        h = self.h
        h.plan, h.plan_phase, h.plan_kind = None, "investigating", "normal"
        if role.startswith("plan-exec"):
            h.mode = "coding"
            h.plan = Plan(title="P", steps=[Step(title="s")],
                          kind="tdd" if role.endswith("tdd") else "normal")
            h.plan_phase = "executing"
        elif role.startswith("plan"):
            h.mode = "plan"
            h.plan_kind = "tdd" if role.endswith("tdd") else "normal"
        else:
            h.mode = role
        h.run_mode = run_mode
        h.edit_features = EDIT_SETS[edits]
        # The tool set and the prompt only ask whether an index exists.
        h.index = object() if index == "on" else None
        h.net_access = net

    def prompt(self) -> str:
        text = self.h._build_system_prompt().replace(str(self.wd), "<WORKDIR>")
        return text.replace(date.today().isoformat(), "<TODAY>")

    def snapshot(self) -> dict:
        schemas, configs, prompts, choices = {}, {}, {}, {}
        for role in ROLES:
            for run_mode in RUN_MODES:
                for edits in EDIT_SETS:
                    for index in ("off", "on"):
                        for net in ("off", "on"):
                            key = "|".join((role, run_mode, edits, index, net))
                            self.configure(role, run_mode, edits, index, net)
                            tool_list = self.h._current_tools()
                            ids = []
                            for t in tool_list:
                                d = _digest(t)
                                schemas[d] = t
                                ids.append(f"{t['function']['name']}@{d}")
                            text = self.prompt()
                            configs[key] = {"tools": ids, "prompt": _digest(text)}
                            if key in _FULL_PROMPTS:
                                prompts[key] = text
                            if (run_mode, edits, net) == ("new", "default", "off"):
                                # the /tools pickers: order, groups and locks
                                choices[f"{role}|{index}"] = [
                                    [c["name"], c["group"], c["locked"], c["note"]]
                                    for c in self.h.tool_choices()]
        base = [schemas[i.split("@")[1]] for i in configs["coding|new|default|off|on"]["tools"]]
        return {"schemas": schemas, "configs": configs, "prompts": prompts, "choices": choices,
                "reference": {style: tools.render_tool_reference(base, style)
                              for style in ("full", "compact", "minimal")},
                "dispatch": _dispatch_errors(self.wd)}


def _dispatch_errors(wd: Path) -> dict:
    calls = {"unknown_arg": ("write_file", {"path": "a", "content": "b", "old_string": "x"}),
             "missing_arg": ("edit_file", {"path": "a"}),
             "placeholder": ("write_file", {"path": "a", "content": "[written to a]"}),
             "unknown_tool": ("no_such_tool", {}),
             "routed": ("write_file", {"path": "zz.txt", "old_string": "q", "new_string": "r"}),
             "bad_type": ("read_file", {"path": "zz.txt", "start_line": "x"})}
    out = {}
    for key, (name, args) in calls.items():
        out[key] = re.sub(re.escape(str(wd)), "<WORKDIR>", tools.dispatch(name, args, wd))
    return out


class ToolGolden(unittest.TestCase):
    maxDiff = None

    def test_what_the_model_sees_is_unchanged(self):
        with _Golden() as g:
            now = g.snapshot()
        if os.environ.get("MOMO_REGEN_GOLDEN") == "1" or not FIXTURE.exists():
            FIXTURE.write_text(json.dumps(now, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                               encoding="utf-8")
            self.skipTest(f"golden fixture written: {FIXTURE}")
        want = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(sorted(now["configs"]), sorted(want["configs"]))
        for key, cfg in want["configs"].items():
            got = now["configs"][key]
            with self.subTest(config=key):
                self.assertEqual([i.split("@")[0] for i in got["tools"]],
                                 [i.split("@")[0] for i in cfg["tools"]], "tool order")
                for g_id, w_id in zip(got["tools"], cfg["tools"]):
                    if g_id != w_id:
                        self.assertEqual(now["schemas"][g_id.split("@")[1]],
                                         want["schemas"][w_id.split("@")[1]], g_id)
                if key in want["prompts"]:
                    self.assertEqual(now["prompts"][key], want["prompts"][key])
                self.assertEqual(got["prompt"], cfg["prompt"], "system prompt")
        self.assertEqual(now["choices"], want["choices"])
        self.assertEqual(now["reference"], want["reference"])
        self.assertEqual(now["dispatch"], want["dispatch"])


if __name__ == "__main__":
    unittest.main()
