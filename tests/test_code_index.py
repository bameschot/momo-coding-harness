"""Tests for the in-memory code index (harness/code_index.py) and the index_*
tools, /index commands and web status that sit on it.

Run with HOME pointed at a scratch dir — the pickle tests write to
~/.momo-harness/index, and the harness tests write prefs and sessions.
"""
import http.client
import json
import os
import pickle
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import code_index as ci
from harness import code_nav, tools

FIXTURES = Path(__file__).parent / "fixtures"


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    # Filesystems with coarse mtimes: make sure a rewrite changes the stamp.
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    return p


CHAIN = {
    "a.py": "def leaf():\n    return 1\n",
    "b.py": "from a import leaf\n\n\ndef mid():\n    return leaf() + 1\n",
    "c.py": "from b import mid\n\n\ndef top():\n    return mid()\n",
    "conf/app.yaml": "server:\n  port: 8080\n  retry_delay: 5\n",
    "notes.txt": "remember the retry_delay knob\n",
}


class Base(unittest.TestCase):
    """A scratch project, a started index, and no stat-diff throttle."""

    files = CHAIN
    max_bytes = ci.DEFAULT_MAX_BYTES

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        for rel, text in self.files.items():
            _write(self.root, rel, text)
        self._patches = [mock.patch.object(ci, "_STAT_THROTTLE_S", 0.0),
                         mock.patch.dict(os.environ, {"HOME": self.home.name})]
        for p in self._patches:
            p.start()
        self.idx = self.make_index()

    def make_index(self, load_pickle=False, **kw):
        idx = ci.ProjectIndex(self.root, kw.pop("max_bytes", self.max_bytes), **kw)
        idx.start(load_pickle=load_pickle)
        self.assertIsNone(idx.wait_fresh())
        return idx

    def tearDown(self):
        self.idx.stop()
        self.idx.join(2)
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()
        self.home.cleanup()

    def search(self, q, **kw):
        return ci.index_search(q, workdir=self.root, index=self.idx, **kw)

    def text(self, q, **kw):
        return ci.index_text(q, workdir=self.root, index=self.idx, **kw)

    def callers(self, name, **kw):
        return ci.index_callers(name, workdir=self.root, index=self.idx, **kw)


class Build(Base):

    def test_indexes_code_config_and_text(self):
        self.assertEqual(self.idx.live_count(), len(CHAIN))
        self.assertIn("a.py:L1-2  function leaf", self.search("leaf"))
        self.assertIn("key server.retry_delay", self.search("retry_delay"))
        self.assertIn("notes.txt", self.text("retry_delay"))

    def test_provider_serves_code_nav(self):
        code_nav.set_index_provider(self.idx.provide)
        try:
            with mock.patch.object(code_nav, "_extract", side_effect=AssertionError("re-parsed")):
                got = code_nav.index(self.root / "b.py")
            self.assertEqual([s.name for s in got.symbols], ["mid"])
        finally:
            code_nav.set_index_provider(None)


class Refresh(Base):

    def test_edit_is_seen(self):
        _write(self.root, "a.py", "def leaf_renamed():\n    return 1\n")
        out = self.search("leaf_renamed")
        self.assertIn("a.py:L1-2  function leaf_renamed", out)
        self.assertNotIn("function leaf ", self.search("leaf", kind="function").split("\n")[1])

    def test_delete_and_rename(self):
        (self.root / "c.py").rename(self.root / "d.py")
        (self.root / "notes.txt").unlink()
        out = self.search("top")
        self.assertIn("d.py:", out)
        self.assertNotIn("c.py:", out)
        self.assertIn("no matches", self.text("knob"))

    def test_git_ignored_files_are_excluded(self):
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        _write(self.root, ".gitignore", "generated/\n")
        _write(self.root, "generated/big.py", "def ignored_thing():\n    pass\n")
        self.idx.rebuild()
        self.assertIsNone(self.idx.wait_fresh())
        self.assertIn("no definition", self.search("ignored_thing"))
        self.assertIn("function leaf", self.search("leaf"))

    def test_harness_write_visible_without_stat_diff(self):
        # With a long throttle only dispatch's invalidation can make this visible.
        with mock.patch.object(ci, "_STAT_THROTTLE_S", 3600.0):
            self.idx._dirty = False
            self.idx._last_diff = time.monotonic()
            out = tools.dispatch("edit_file", {"path": "a.py", "old_string": "def leaf():",
                                               "new_string": "def leaf_edited():"},
                                 self.root, index=self.idx)
            self.assertNotIn("ERROR", out)
            self.assertIn("function leaf_edited", self.search("leaf_edited"))

    def test_run_command_forces_a_stat_diff(self):
        with mock.patch.object(ci, "_STAT_THROTTLE_S", 3600.0):
            self.idx._dirty = False
            self.idx._last_diff = time.monotonic()
            tools.dispatch("run_command",
                           {"command": "printf 'def from_shell():\\n    pass\\n' > e.py"},
                           self.root, index=self.idx)
            self.assertIn("function from_shell", self.search("from_shell"))

    def test_many_changes_compact_cleanly(self):
        for i in range(30):
            _write(self.root, f"gen/m{i}.py", f"def gen_{i}():\n    return leaf()\n")
        self.assertIn("gen/m7.py", self.search("gen_7"))
        for i in range(30):
            (self.root / f"gen/m{i}.py").unlink()
        self.assertIsNone(self.idx.wait_fresh())
        self.assertNotIn("gen/", self.callers("leaf"))
        # Removed files' ids are recycled without leaving stale occurrences.
        _write(self.root, "gen/new.py", "def reuse():\n    return mid()\n")
        out = self.callers("mid")
        self.assertIn("gen/new.py", out)
        self.assertNotIn("gen/m", out)


class Blocking(Base):

    def _slow(self, delay):
        real = self.idx._build_entry

        def slow(*a, **k):
            time.sleep(delay)
            return real(*a, **k)
        return mock.patch.object(self.idx, "_build_entry", side_effect=slow)

    def test_query_waits_for_the_refresh(self):
        with self._slow(0.4):
            _write(self.root, "a.py", "def leaf_after_wait():\n    return 1\n")
            t = time.monotonic()
            out = self.search("leaf_after_wait")
            self.assertGreater(time.monotonic() - t, 0.3)
        self.assertIn("function leaf_after_wait", out)

    def test_cancel_interrupts_the_wait(self):
        cancel = threading.Event()
        with self._slow(2.0):
            _write(self.root, "a.py", "def x():\n    pass\n")
            threading.Timer(0.3, cancel.set).start()
            t = time.monotonic()
            out = ci.index_search("x", workdir=self.root, index=self.idx, cancel=cancel)
            self.assertLess(time.monotonic() - t, 1.5)
        self.assertTrue(out.startswith("ERROR: cancelled"), out)

    def test_on_wait_reports_progress_once(self):
        seen = []
        with self._slow(0.3):
            _write(self.root, "a.py", "def y():\n    pass\n")
            _write(self.root, "b.py", "def z():\n    pass\n")
            self.assertIsNone(self.idx.wait_fresh(on_wait=lambda d, t: seen.append((d, t))))
        self.assertEqual(len(seen), 1)

    def test_stopped_index_errors_instead_of_hanging(self):
        self.idx.stop()
        self.assertTrue(self.search("leaf").startswith("ERROR"))


class Budget(Base):

    def test_degrade_order_and_recovery(self):
        m = dict(self.idx.mem)
        self.assertTrue(all(v > 0 for v in m.values()), m)
        with mock.patch.object(ci, "MIN_MAX_BYTES", 1):
            self.idx.set_max_bytes(m["files"] + m["idents"] + m["trigrams"] - 1)
            self.assertEqual(self.idx.components, {"trigrams": False, "idents": True})
            self.assertIn("scanning files", self.text("retry_delay"))
            self.assertIn("conf/app.yaml", self.text("retry_delay"))

            self.idx.set_max_bytes(m["files"] + 10)
            self.assertEqual(self.idx.components, {"trigrams": False, "idents": False})
            out = self.callers("leaf")
            self.assertIn("b.py", out)          # re-parsed instead of the dropped index
            self.assertIn("identifier index was dropped", out)

            self.idx.set_max_bytes(m["files"] // 2)
            self.assertTrue(self.idx.partial)
            _write(self.root, "late.py", "def late_arrival():\n    pass\n")
            out = self.search("late_arrival")
            self.assertIn("no definition", out)
            self.assertIn("covers only part", out)
            notes = " ".join(self.idx.pop_notices())
            self.assertIn("text-search", notes)
            self.assertIn("identifier index", notes)

            self.idx.set_max_bytes(ci.DEFAULT_MAX_BYTES)
            self.assertIsNone(self.idx.wait_fresh())
            self.assertFalse(self.idx.degraded())
            self.assertIn("late.py", self.search("late_arrival"))
            self.assertNotIn("dropped", self.callers("leaf"))

    def test_breakdown_adds_up(self):
        b = ci.breakdown(self.idx)
        self.assertEqual(sum(c["bytes"] for c in b["categories"]), b["used"])
        self.assertEqual(sum(l["bytes"] for l in b["languages"]) + b["shared"], b["used"])
        self.assertGreater(b["shared"], 0)
        self.assertEqual(sum(l["files"] for l in b["languages"]), len(CHAIN))
        langs = {l["lang"] for l in b["languages"]}
        self.assertEqual(langs, {"python", "yaml", "text"})
        with mock.patch.object(ci, "MIN_MAX_BYTES", 1):
            self.idx.set_max_bytes(b["used"] - 1)
        b = ci.breakdown(self.idx)
        self.assertFalse({c["key"]: c["enabled"] for c in b["categories"]}["trigrams"])
        self.assertEqual(sum(c["bytes"] for c in b["categories"]), b["used"])

    def test_estimate_tracks_contents(self):
        before = self.idx.mem_used()
        _write(self.root, "big.py", "".join(f"def f{i}(x):\n    return g{i}(x)\n" for i in range(200)))
        self.assertIsNone(self.idx.wait_fresh())
        grown = self.idx.mem_used() - before
        self.assertGreater(grown, 200 * ci._C_SYMBOL)
        (self.root / "big.py").unlink()
        self.assertIsNone(self.idx.wait_fresh())
        self.assertLess(abs(self.idx.mem_used() - before), 2000)


class Pickle(Base):

    def test_round_trip(self):
        self.assertIn("saved", self.idx.save())
        self.assertEqual(oct(ci.pickle_path(self.root).stat().st_mode & 0o777), "0o600")
        self.idx.stop()
        calls = []
        real = ci.ProjectIndex._build_entry

        def counting(self_, rel, *a):
            calls.append(rel)
            return real(self_, rel, *a)
        with mock.patch.object(ci.ProjectIndex, "_build_entry", counting):
            _write(self.root, "a.py", "def leaf_v2():\n    return 1\n")
            self.idx = self.make_index(load_pickle=True)
        self.assertEqual(calls, ["a.py"])       # only the changed file was re-indexed
        self.assertTrue(any("loaded" in n for n in self.idx.pop_notices()))
        self.assertIn("function leaf_v2", self.search("leaf_v2"))
        self.assertIn("b.py", self.callers("mid"))

    def _reload_notice(self) -> str:
        self.idx.stop()
        self.idx = self.make_index(load_pickle=True)
        return " ".join(self.idx.pop_notices())

    def test_version_mismatch_rebuilds(self):
        self.idx.save()
        with mock.patch.object(ci, "FORMAT_VERSION", ci.FORMAT_VERSION + 1):
            self.assertIn("discarded", self._reload_notice())
        self.assertIn("function leaf", self.search("leaf"))

    def test_other_workdir_pickle_is_discarded(self):
        other = tempfile.TemporaryDirectory()
        try:
            o = ci.ProjectIndex(Path(other.name))
            o.start()
            o.wait_fresh()
            o.save()
            o.stop()
            ci.pickle_path(self.root).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ci.pickle_path(Path(other.name)), ci.pickle_path(self.root))
            self.assertIn("discarded", self._reload_notice())
        finally:
            other.cleanup()

    def test_tampered_pickle_does_not_run_code(self):
        marker = Path(self.home.name) / "pwned"

        class Evil:
            def __reduce__(self):
                return (os.system, (f"touch {marker}",))
        path = ci.pickle_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(ci._header(self.root), f)
            pickle.dump({"files": [], "idents": {}, "x": Evil()}, f)
        os.chmod(path, 0o600)
        notice = self._reload_notice()
        self.assertIn("not loaded", notice)
        self.assertFalse(marker.exists())
        self.assertIn("function leaf", self.search("leaf"))

    def test_world_writable_file_is_refused(self):
        self.idx.save()
        os.chmod(ci.pickle_path(self.root), 0o666)
        self.assertIn("writable by other users", self._reload_notice())


class Tools(Base):

    def test_search_ranks_exact_then_approximate(self):
        out = self.search("mid")
        lines = out.splitlines()
        self.assertIn("b.py:L4-5  function mid", lines[1])
        out = self.search("delay retry")          # words in any order
        self.assertIn("server.retry_delay", out)

    def test_search_matches_docstrings_and_ignores_filler_words(self):
        _write(self.root, "units.py", 'def parse_size(value):\n'
               '    """Bytes from a plain number or a unit-suffixed string."""\n    return 0\n')
        out = self.search("turn a size string into bytes")
        self.assertIn("function parse_size", out.splitlines()[1])

    def test_search_finds_files_by_path(self):
        out = self.search("conf/app.yaml")
        self.assertIn("conf/app.yaml  (file, yaml", out)
        self.assertIn('index_file("conf/app.yaml")', out)
        self.assertIn("b.py  (file", self.search("b", kind="file"))

    def test_search_line_form(self):
        out = self.search("5", path="b.py")
        self.assertTrue(out.startswith("b.py:L4-5  function mid"), out)
        self.assertIn("needs the file", self.search("5"))
        self.assertIn("module level", self.search("1", path="b.py"))
        self.assertIn("outside", self.search("99", path="b.py"))

    def test_search_filters(self):
        self.assertIn("no definition", self.search("leaf", lang="yaml"))
        self.assertIn("a.py", self.search("leaf", path="a.py"))
        self.assertIn("no definition", self.search("leaf", path="conf"))

    def test_text_regex_and_short_queries(self):
        out = self.text(r"ret\w+_delay", regex=True)
        self.assertIn("conf/app.yaml", out)
        self.assertIn("notes.txt", out)
        self.assertIn("[in server.retry_delay]", out)
        self.assertIn("ERROR: invalid regex", self.text("(", regex=True))
        out = self.text("1")                       # no trigram: scans everything
        self.assertIn("every indexed file was scanned", out)

    def test_regex_literals_are_conservative(self):
        self.assertEqual(ci._regex_literals(r"foo|bar"), [])
        self.assertEqual(ci._regex_literals(r"(abc)?def"), [])
        self.assertEqual(ci._regex_literals(r"colou?r_name"), ["colo", "r_name"])
        self.assertEqual(ci._regex_literals(r"\x41bcd"), ["bcd"])

    def test_callers_depth_two_follows_the_chain(self):
        out = self.callers("leaf", depth=2)
        self.assertIn("'leaf' is defined at a.py:L1-2", out)
        self.assertIn("b.py:L4-5  function mid", out)
        self.assertIn("(call) return leaf() + 1", out)
        self.assertIn("via mid", out)
        self.assertIn("c.py:L4-5  function top", out)
        self.assertIn("Impact:", out)

    def test_callers_matches_find_references(self):
        def sites(text):
            return {l.split(":")[0] + ":" + l.split(":")[1] for l in text.splitlines()
                    if "(call)" in l}
        ref = code_nav.find_references("leaf", ".", role="call", workdir=self.root)
        got = self.callers("leaf", role="call")
        self.assertEqual({s.split(":")[0] for s in sites(ref)}, {"b.py"})
        self.assertIn("L5 (call)", got)

    def test_map_and_file(self):
        out = ci.index_map(workdir=self.root, index=self.idx)
        self.assertLess(out.index("a.py"), out.index("c.py"))   # a.py is used, c.py is not
        out = ci.index_file("b.py", workdir=self.root, index=self.idx)
        self.assertIn("Imports (1): a (leaf)", out)
        self.assertIn("Imported by (1): c.py:L1", out)
        self.assertIn("function mid", out)
        self.assertIn("ERROR: path outside", ci.index_file("../x", workdir=self.root, index=self.idx))

    def test_tools_without_index(self):
        self.assertIn("code index is off", ci.index_search("x", workdir=self.root, index=None))


class ToolSet(unittest.TestCase):

    def test_index_tools_replace_overlapping_code_nav_tools(self):
        names = [t["function"]["name"] for t in tools.with_index_tools(tools.ALL_TOOLS)]
        for gone in tools.INDEX_REPLACES:
            self.assertNotIn(gone, names)
        for new in tools.INDEX_TOOL_NAMES:
            self.assertIn(new, names)
        self.assertIn("read_symbol", names)
        # find_symbol stays: its line form is the one-line answer the model uses.
        self.assertIn("find_symbol", names)
        # Index tools sit where the code-nav block starts, not at the end.
        self.assertLess(names.index("index_search"), names.index("read_symbol"))

    def test_schemas_validate_arguments(self):
        out = tools.dispatch("index_search", {"query": "x", "index": "nope"}, Path("."))
        self.assertIn("does not accept", out)


# ── harness, commands and web ────────────────────────────────────────────────

import harness.harness as hmod
from harness import session as session_mod
from harness.commands import handle as handle_command
from harness.controller import Controller
from harness.llm.base import ChatResponse, LLMClient
from harness.web.server import start_web_server


class FakeClient(LLMClient):
    provider_name = "fake"

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return ChatResponse(content="ok", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class HarnessIndex(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp2 = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name).resolve()
        for rel, text in CHAIN.items():
            _write(self.wd, rel, text)
        _write(Path(self.tmp2.name), "other.py", "def elsewhere():\n    pass\n")
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: FakeClient("http://fake", "fake")
        self._patches = [mock.patch.object(session_mod, "_PREFS_PATH", Path(self.home.name) / "prefs.json"),
                         mock.patch.dict(os.environ, {"HOME": self.home.name}),
                         mock.patch.object(ci, "_STAT_THROTTLE_S", 0.0)]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode("coding")

    def tearDown(self):
        self.h.shutdown_index()
        hmod.make_client = self._orig
        for p in reversed(self._patches):
            p.stop()
        self.h.logger.close()
        for t in (self.tmp, self.tmp2, self.home):
            t.cleanup()

    def cmd(self, text) -> str:
        return handle_command(text, self.h).output

    def tool_names(self):
        return {t["function"]["name"] for t in self.h._current_tools()}

    def test_toggle_swaps_tools_and_prompt(self):
        self.assertIn("find_symbol", self.tool_names())
        self.assertIn("Code index: on", self.cmd("/index on"))
        self.assertIn("index_search", self.tool_names())
        self.assertIn("find_symbol", self.tool_names())
        self.assertNotIn("find_references", self.tool_names())
        self.assertIn("index_callers", self.h.messages[0]["content"])
        self.assertIsNone(self.h.index.wait_fresh())
        self.assertIn("function leaf", self.h._dispatch("index_search", {"query": "leaf"}))
        self.cmd("/index off")
        self.assertIsNone(self.h.index)
        self.assertIn("find_references", self.tool_names())
        self.assertIsNone(code_nav._index_provider)
        self.assertEqual(json.loads((Path(self.home.name) / "prefs.json").read_text())["index"], False)

    def test_workdir_change_reindexes(self):
        self.cmd("/index on")
        first = self.h.index
        self.cmd(f"/workspace {self.tmp2.name}")
        self.assertIsNot(self.h.index, first)
        self.assertEqual(self.h.index.root, Path(self.tmp2.name).resolve())
        self.assertIsNone(self.h.index.wait_fresh())
        self.assertIn("function elsewhere", self.h._dispatch("index_search", {"query": "elsewhere"}))
        self.assertTrue(first._stop)

    def test_index_command_shows_composition(self):
        self.assertIn("Code index: off", self.cmd("/index"))
        self.cmd("/index on")
        self.h.index.wait_fresh()
        out = self.cmd("/index")
        for want in ("Budget", "Made of:", "Definitions", "Identifiers", "By language:",
                     "python", "█"):
            self.assertIn(want, out)

    def test_status_fields_and_budget_command(self):
        self.cmd("/index on")
        self.h.index.wait_fresh()
        st = self.h.status_event()
        self.assertTrue(st.index_enabled)
        self.assertEqual(st.index_files, len(CHAIN))
        self.assertIn("50 MB", self.cmd("/index-max-mem 50mb"))
        self.assertEqual(self.h.status_event().index_max_bytes, 50 * 1024 * 1024)
        self.assertEqual(self.h.index.max_bytes, 50 * 1024 * 1024)
        self.assertIn("ERROR", self.cmd("/index-max-mem lots"))

    def test_persist_saves_on_shutdown_and_loads_on_start(self):
        self.cmd("/index-persist on")
        self.cmd("/index on")
        self.h.index.wait_fresh()
        self.h.shutdown_index()
        self.assertTrue(ci.pickle_path(self.wd).exists())
        self.h.index_enabled = False
        self.cmd("/index on")
        self.h.index.wait_fresh()
        deadline = time.monotonic() + 2
        notes = []
        while time.monotonic() < deadline and not any("loaded" in n for n in notes):
            notes += [e.text for e in self._drain_events() if hasattr(e, "text")]
            time.sleep(0.05)
        self.assertTrue(any("loaded" in n for n in notes), notes)

    def _drain_events(self):
        sub = getattr(self, "_sub", None)
        if sub is None:
            sub = self._sub = self.h.event_queue.subscribe(replay=True)
        out = []
        try:
            while True:
                out.append(sub.get_nowait())
        except Exception:
            return out

    def test_web_status_reflects_index(self):
        controller = Controller(self.h)
        web = start_web_server(controller, "127.0.0.1", 0, None)
        try:
            def post(path, body):
                c = http.client.HTTPConnection("127.0.0.1", web.port, timeout=5)
                c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
                r = c.getresponse()
                r.read()
                return r.status

            def state():
                c = http.client.HTTPConnection("127.0.0.1", web.port, timeout=5)
                c.request("GET", "/api/state")
                return json.loads(c.getresponse().read())["status"]

            self.assertEqual(post("/api/submit", {"text": "/index on"}), 200)
            self.h.index.wait_fresh()
            st = state()
            self.assertTrue(st["index_enabled"])
            self.assertEqual(st["index_files"], len(CHAIN))
            self.assertEqual(post("/api/submit", {"text": "/index-max-mem 50mb"}), 200)
            self.assertEqual(state()["index_max_bytes"], 50 * 1024 * 1024)
            c = http.client.HTTPConnection("127.0.0.1", web.port, timeout=5)
            c.request("GET", "/api/index")
            b = json.loads(c.getresponse().read())
            self.assertTrue(b["enabled"])
            self.assertEqual(b["limit"], 50 * 1024 * 1024)
            self.assertEqual([x["key"] for x in b["categories"]],
                             ["files", "symbols", "imports", "idents", "trigrams"])
        finally:
            web.close()


if __name__ == "__main__":
    unittest.main()
