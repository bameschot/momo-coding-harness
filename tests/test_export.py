"""/export and the full log: rendering, the compaction archive, and its round trip.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness import transcript
from harness.commands import export_markdown, handle as handle_command, render_markdown

from test_compaction import SummaryClient


def msgs():
    return [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "hello"},
        {"role": "thinking", "content": "THOUGHT A", "unsent": True},
        {"role": "assistant", "content": "let me look",
         "tool_calls": [{"function": {"name": "fetch_url", "arguments": {
             "url": "https://x", "headers": {"Authorization": "Bearer SECRET"}}}}]},
        {"role": "tool", "name": "fetch_url", "content": "body with ```fence``` inside"},
        {"role": "assistant", "content": "done"},
    ]


class Render(unittest.TestCase):
    def test_default_render_leaves_out_internals(self):
        out = render_markdown(msgs())
        for absent in ("SYSTEM PROMPT", "THOUGHT A", "fetch_url\"", "Bearer"):
            self.assertNotIn(absent, out)
        self.assertIn("**You:**\n\nhello", out)
        self.assertIn("**Tool result (fetch_url):**", out)

    def test_fence_survives_backticks_in_content(self):
        out = render_markdown(msgs())
        self.assertIn("````\nbody with ```fence``` inside\n````", out)

    def test_full_render_has_everything(self):
        out = render_markdown(msgs(), full=True, meta={"model": "m1", "session": "s1"})
        self.assertIn("- **Model:** m1", out)
        self.assertIn("- **Session:** s1", out)
        self.assertIn("**System prompt:**", out)
        self.assertIn("SYSTEM PROMPT", out)
        self.assertIn("**Thinking (not sent back):**", out)
        self.assertIn("THOUGHT A", out)
        self.assertIn("**Tool call: `fetch_url`**", out)
        self.assertIn('"url": "https://x"', out)
        self.assertNotIn("SECRET", out)           # secret header masked
        self.assertIn("body with ```fence``` inside", out)
        self.assertIn("let me look", out)


def m(text, role="user"):
    return {"role": role, "content": text}


class Archive(unittest.TestCase):
    def compact(self, archive, history, drop, trims=None, n=1):
        trims = trims or {}
        keep = [k for k in range(len(history)) if k not in drop]
        archive = transcript.archive_compaction(archive, history, set(drop), trims, keep, n,
                                                "2026-10-14T14:03:00")
        for k, text in trims.items():
            history[k]["content"] = text
        return archive, [history[k] for k in keep]

    def texts(self, history, archive):
        return [x["content"] for x, _ in transcript.full_transcript(history, archive)]

    def test_dropped_middle_group_returns_to_its_place(self):
        h = [m("S", "system"), m("u1"), m("a1", "assistant"), m("t1", "tool"), m("u2")]
        arch, h = self.compact([], h, {2, 3})
        self.assertEqual([x["content"] for x in h], ["S", "u1", "u2"])
        self.assertEqual(self.texts(h, arch), ["S", "u1", "a1", "t1", "u2"])
        notes = [n for _, n in transcript.full_transcript(h, arch)]
        self.assertEqual(notes[2], "removed from context by compaction #1 (14:03)")

    def test_order_holds_across_compactions(self):
        h = [m("S", "system")] + [m(f"x{i}") for i in range(8)]
        arch, h = self.compact([], h, {2, 5})          # drops x1, x4
        h.append(m("x8"))
        arch, h = self.compact(arch, h, {1, 3, 4}, n=2)   # drops x0, x3, x5
        self.assertEqual(self.texts(h, arch), ["S"] + [f"x{i}" for i in range(9)])

    def test_trimmed_then_dropped_shows_the_original_once(self):
        full = "R" * 50
        h = [m("S", "system"), m("u1"), m(full, "tool"), m("u2")]
        trimmed = f"RR\n[… 46 chars {transcript.TRIM_MARKER} …]\nRR"
        arch, h = self.compact([], h, set(), {2: trimmed})
        self.assertEqual(h[2]["content"], trimmed)
        self.assertEqual(self.texts(h, arch), ["S", "u1", full, "u2"])
        arch, h = self.compact(arch, h, {2}, n=2)
        self.assertEqual(self.texts(h, arch), ["S", "u1", full, "u2"])
        self.assertEqual(len(arch), 1)

    def test_record_past_the_end_goes_last(self):
        arch = [{"pos": 9, "kind": "removed", "n": 1, "time": "", "message": m("late")}]
        self.assertEqual(self.texts([m("S", "system")], arch), ["S", "late"])


class HarnessExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(hmod, "make_client",
                              lambda *a, **k: SummaryClient("http://fake", "fake")),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
        ]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)
        self.h.set_mode("coding")

    def tearDown(self):
        self.h.logger.close()
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    def fill(self, turns):
        for n in range(turns):
            self.h.messages += [
                {"role": "user", "content": f"QUESTION-{n} " + "q" * 800},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"function": {"name": "read_file", "arguments": {"path": f"F{n}.py"}}}]},
                {"role": "tool", "name": "read_file", "content": f"RESULT-{n} " + "r" * 6000},
                {"role": "assistant", "content": f"ANSWER-{n}"},
            ]
        self.h.messages.append({"role": "user", "content": "CURRENT"})
        self.h._turn_user = self.h.messages[-1]

    def test_full_export_keeps_what_compaction_removed(self):
        self.fill(20)
        self.h.context_limit = hmod._msg_tokens(self.h.messages[0]) + 6000
        changed, _ = self.h._compact()
        self.assertTrue(changed)
        self.assertTrue(self.h.compacted)
        plain = export_markdown(self.h)
        full = export_markdown(self.h, full=True)
        self.assertNotIn("RESULT-0", plain)
        for n in range(20):
            for marker in (f"QUESTION-{n}", f"RESULT-{n}", f"ANSWER-{n}", f"F{n}.py"):
                self.assertIn(marker, full)
        self.assertLess(full.index("RESULT-3"), full.index("ANSWER-3"))
        self.assertLess(full.index("ANSWER-3"), full.index("QUESTION-4"))
        self.assertIn("removed from context by compaction #1", full)

    def test_archive_survives_save_and_load(self):
        self.fill(20)
        self.h.context_limit = hmod._msg_tokens(self.h.messages[0]) + 6000
        self.h._compact()
        archived = len(self.h.compacted)
        self.h._autosave()
        path = self.h.session_path()
        self.h.new_session()
        self.assertEqual(self.h.compacted, [])
        self.h.load_session(path)
        self.assertEqual(len(self.h.compacted), archived)
        self.assertIn("RESULT-0", export_markdown(self.h, full=True))

    def test_clear_drops_the_archive(self):
        self.h.compacted = [{"pos": 1, "kind": "removed", "n": 1, "time": "", "message": m("x")}]
        handle_command("/clear", self.h)
        self.assertEqual(self.h.compacted, [])

    def test_export_full_command_writes_the_file(self):
        self.h.messages += [m("hi"), {"role": "thinking", "content": "PONDER"}]
        out = handle_command("/export full", self.h).output
        files = list(self.home.glob("conversation-full-*.md"))
        self.assertEqual(len(files), 1, out)
        self.assertIn("PONDER", files[0].read_text(encoding="utf-8"))
        handle_command("/export full named.md", self.h)
        self.assertIn("**System prompt:**", (self.home / "named.md").read_text(encoding="utf-8"))
        handle_command("/export", self.h)
        plain = [p for p in self.home.glob("conversation-*.md") if "-full-" not in p.name]
        self.assertNotIn("PONDER", plain[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
