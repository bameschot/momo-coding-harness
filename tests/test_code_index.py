"""Tests for the in-memory code index (harness/code_index.py) and the index_*
tools, /index commands and web status that sit on it.

Run with HOME pointed at a scratch dir — the pickle tests write to
~/.momo-harness/index, and the harness tests write prefs and sessions.
"""
import http.client
import json
import multiprocessing
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

    def test_gitignore_seeds_the_filter_once(self):
        self.idx.stop()
        ci.filter_path(self.root).unlink()
        _write(self.root, ".gitignore", "generated/\n")
        _write(self.root, "generated/big.py", "def ignored_thing():\n    pass\n")
        _write(self.root, "later/x.py", "def later_thing():\n    pass\n")
        self.idx = self.make_index()
        self.assertTrue(ci.filter_path(self.root).exists())
        self.assertTrue(any("created from .gitignore" in n for n in self.idx.pop_notices()))
        self.assertIn("no definition", self.search("ignored_thing"))
        self.assertIn("function leaf", self.search("leaf"))
        # Only the first creation reads .gitignore; the saved filter decides after that.
        _write(self.root, ".gitignore", "generated/\nlater/\n")
        self.idx.stop()
        self.idx = self.make_index()
        self.assertIn("function later_thing", self.search("later_thing"))
        self.assertEqual(self.idx.pop_notices(), [])

    def test_filter_changes_apply_live(self):
        _write(self.root, "vendor/lib.py", "def vendored():\n    pass\n")
        _write(self.root, "vendor/keep.py", "def kept():\n    pass\n")
        self.assertIn("function vendored", self.search("vendored"))
        self.idx.set_filter(self.idx.filter_text + "vendor/*\n!vendor/keep.py\n")
        self.assertIsNone(self.idx.wait_fresh())
        self.assertIn("no definition", self.search("vendored"))
        self.assertIn("function kept", self.search("kept"))
        # The harness writing an excluded file does not index it.
        _write(self.root, "vendor/new.py", "def fresh_vendor():\n    pass\n")
        self.idx.invalidate([str(self.root / "vendor/new.py")])
        self.assertIn("no definition", self.search("fresh_vendor"))
        self.idx.set_filter("")
        self.assertIsNone(self.idx.wait_fresh())
        self.assertIn("function vendored", self.search("vendored"))

    def test_saved_index_drops_files_the_filter_now_excludes(self):
        _write(self.root, "gen/out.py", "def generated_fn():\n    pass\n")
        self.assertIn("function generated_fn", self.search("generated_fn"))
        self.assertIn("saved", self.idx.save())
        self.idx.stop()
        self.assertEqual(ci.save_filter(self.root, "gen/\n"), "")
        self.idx = self.make_index(load_pickle=True)
        self.assertIn("no definition", self.search("generated_fn"))
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

    def test_file_limit_can_change_live(self):
        with mock.patch.object(ci, "MIN_MAX_FILES", 1):
            self.idx.set_max_files(2)
            self.assertIsNone(self.idx.wait_fresh())
            self.assertEqual(self.idx.live_count(), 2)
            self.assertTrue(self.idx.skipped["limit"])
            self.assertIn("more than 2 files", ci._degraded_note(self.idx))
            self.idx.set_max_files(1000)
            self.assertIsNone(self.idx.wait_fresh())
            self.assertEqual(self.idx.live_count(), len(CHAIN))
            self.assertFalse(self.idx.skipped["limit"])

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


class Parallel(Base):
    """Bulk builds in worker processes give the same index as a serial build."""

    files = {**CHAIN, **{f"pkg/m{i}.py": f"from a import leaf\n\n\ndef f{i}(x):\n"
                                         f"    return leaf() + x  # note{i}\n"
                         for i in range(40)}}

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(ci, "_PARALLEL_MIN", 5),
                  mock.patch.object(ci, "_PARALLEL_CHUNK", 4)):
            p.start()
            self._patches.append(p)

    @staticmethod
    def snapshot(idx):
        with idx._lock:
            by_path = {e.path: (e.symbols, e.imports, e.sig, e.sig_bits, e.names and sorted(e.names))
                       for _, e in idx._alive()}
            occ = {name: sorted((idx.files[v >> ci._LINE_BITS].path, v & ci._LINE_MASK)
                                for v in arr) for name, arr in idx._idents.items()}
            return by_path, occ, dict(idx.mem)

    def test_parallel_matches_serial(self):
        serial = self.make_index(workers=1)
        spy = mock.patch.object(ci.ProjectIndex, "_drain_parallel", autospec=True,
                                side_effect=ci.ProjectIndex._drain_parallel)
        with spy as dp:
            par = self.make_index(workers=2)
        try:
            self.assertTrue(dp.called)
            self.assertFalse(par._pool_broken)
            self.assertEqual(par.live_count(), len(self.files))
            self.assertEqual(self.snapshot(par), self.snapshot(serial))
            self.assertEqual(multiprocessing.active_children(), [])   # pool shut down when idle
        finally:
            for idx in (serial, par):
                idx.stop()
                idx.join(5)

    def test_stop_mid_build_leaves_no_processes(self):
        idx = ci.ProjectIndex(self.root, workers=2)
        idx.start()
        time.sleep(0.05)
        idx.stop()
        idx.join(30)
        self.assertFalse(idx._thread.is_alive())
        self.assertEqual(multiprocessing.active_children(), [])

    def test_pool_failure_falls_back_to_serial(self):
        with mock.patch.object(ci, "ProcessPoolExecutor", side_effect=OSError("no processes")):
            idx = self.make_index(workers=2)
        try:
            self.assertTrue(idx._pool_broken)
            self.assertEqual(idx.live_count(), len(self.files))
            self.assertIn("worker processes failed", " ".join(idx.pop_notices()))
            self.assertIn("building in one process", ci.status_text(idx))
        finally:
            idx.stop()
            idx.join(5)

    def test_budget_reached_during_batch(self):
        with mock.patch.object(ci, "MIN_MAX_BYTES", 1):
            idx = self.make_index(workers=2, max_bytes=20_000)
        try:
            self.assertTrue(idx.partial)
            self.assertGreater(idx.skipped["budget"], 0)
            self.assertEqual(idx.live_count() + idx.skipped["budget"], len(self.files))
        finally:
            idx.stop()
            idx.join(5)

    def test_worker_error_is_an_error_skip(self):
        with mock.patch.object(ci, "_build_entry", side_effect=RecursionError):
            self.assertEqual(ci._build_many(str(self.root), ["a.py", "b.py"], True, True),
                             [None, None])
        self.idx._apply_built(["a.py"], [None])
        self.assertEqual(self.idx.skipped["error"], 1)
        self.assertNotIn("a.py", self.idx._by_path)

    def test_in_flight_file_requeued_by_a_diff_is_not_built_twice(self):
        built = ci._build_entry(self.root, "b.py", True, True)
        with self.idx._cond:
            self.idx._queue["b.py"] = None      # a query's stat-diff saw it in flight
        self.idx._apply_built(["b.py"], [built])
        self.assertNotIn("b.py", self.idx._queue)
        _write(self.root, "c.py", "def changed():\n    pass\n")
        stale = ci._build_entry(self.root, "a.py", True, True)
        with self.idx._cond:
            self.idx._queue["a.py"] = None
        _write(self.root, "a.py", "def leaf2():\n    return 2\n")   # changed after the build
        self.idx._apply_built(["a.py"], [stale])
        self.assertIn("a.py", self.idx._queue)


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

    def test_short_values_are_inlined_and_results_say_current(self):
        _write(self.root, "schema.sql", "CREATE TABLE users (\n  id INTEGER,\n  email TEXT\n);\n")
        _write(self.root, "users.py", "class UserEvent:\n    pass\n")
        out = self.search("users table")
        self.assertIn("schema.sql:L1-4  table users", out.splitlines()[1])   # the kind is a word
        self.assertIn("    3:   email TEXT", out)                          # the source, inline
        self.assertIn("nothing left to read", out)
        self.assertIn("re-reading files to double-check is not needed", out)
        self.assertIn("double-check is not needed", self.text("retry_delay"))

    def test_constant_found_by_its_comment_and_tests_rank_last(self):
        _write(self.root, "tests/test_budget.py", "class Budget:\n    pass\n")
        _write(self.root, "limits.py", "DEFAULT_MAX = 100  # default memory budget of the cache\n")
        out = self.search("budget")
        lines = out.splitlines()
        self.assertIn("limits.py", lines[1])
        self.assertLess(out.index("limits.py"), out.index("tests/test_budget.py"))

    def test_search_expands_abbreviations(self):
        _write(self.root, "conf/db.yaml", "services:\n  db:\n    image: postgres:16\n")
        out = self.search("database image")
        self.assertIn("key services.db.image", out.splitlines()[1])

    def test_kind_miss_shows_other_kinds(self):
        out = self.search("server", kind="function")
        self.assertIn("nothing of kind 'function'", out)
        self.assertIn("table server", out)

    def test_no_hit_points_at_text_or_config_files(self):
        out = self.search("knob")                  # only in notes.txt prose
        self.assertIn("The text appears in: notes.txt ×1", out)
        out = self.search("zzqqxx")
        self.assertIn("Config files in the index (1): conf/app.yaml", out)

    def test_search_finds_files_by_path(self):
        out = self.search("conf/app.yaml")
        self.assertIn("conf/app.yaml  (file, yaml", out)
        self.assertIn('index_file("conf/app.yaml")', out)
        self.assertIn("b.py  (file", self.search("b", kind="file"))

    def test_function_and_method_kinds_find_each_other(self):
        _write(self.root, "k.py", "class K:\n    def clear(self):\n        pass\n")
        self.assertIn("method K.clear", self.search("clear", kind="function"))
        self.assertIn("function mid", self.search("mid", kind="method"))
        self.assertIn("nothing of kind 'class'", self.search("mid", kind="class"))

    def test_callers_of_a_module_name_point_at_index_file(self):
        out = self.callers("b")
        self.assertIn("names a file: b.py", out)
        self.assertIn('index_file("b.py")', out)
        self.assertIn("names a file: b.py", self.callers("b.py"))

    def test_callers_summarise_importing_files(self):
        self.assertIn("Imported in 1 file: b.py", self.callers("leaf"))
        self.assertIn("Imported in 1 file: c.py", self.callers("mid"))
        self.assertNotIn("Imported in", self.callers("top"))

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
        self.assertIn("Imported by (1) — and the definitions each one uses:", out)
        self.assertIn("  c.py:L1 — uses mid", out)
        self.assertIn("function mid", out)
        self.assertIn("ERROR: path outside", ci.index_file("../x", workdir=self.root, index=self.idx))

    def test_tools_without_index(self):
        self.assertIn("code index is off", ci.index_search("x", workdir=self.root, index=None))


class ReviewFixes(Base):
    """Regressions from the 2026-09-23 review of the index."""

    files = {
        **CHAIN,
        "blob.bin": "\0" * 64,
        "shop.py": ("class Money:\n    def add(self, o):\n        return 1\n\n\n"
                    "class Cart:\n    def total(self):\n        return Money().add(1)\n\n\n"
                    "class Report:\n    def total(self):\n        return 5\n\n\n"
                    "def show(r: Report):\n    return r.total()\n\n\n"
                    "def pay(c: Cart):\n    return c.total()\n"),
        "web/app.js": ("function total() { return 1; }\n"
                       "var handler = function () {\n"
                       "  if (x) { const total = 2; }\n"
                       "  return total();\n"
                       "};\n"),
        "web/page.html": ("<html><body>\n<script>\nfunction go() { return 1; }\n"
                          "go();\n</script>\n</body></html>\n"),
        "conf/compose.yaml": "services:\n  web_app:\n    port: 1\n",
        "load.py": "def cfg(load):\n    return load()[\"web_app\"]\n",
        "src/Cart.java": "package shop;\nimport java.util.List;\nclass Cart {}\n",
        "native/util.c": "int util(void) { return 1; }\n",
    }

    def test_skipped_files_are_not_reread(self):
        for _ in range(3):
            self.idx.mark_dirty()
            self.assertIsNone(self.idx.wait_fresh())
        self.assertEqual(self.idx.skipped["binary"], 1)
        with mock.patch.object(ci.ProjectIndex, "_build_entry",
                               side_effect=AssertionError("re-read")):
            self.idx.mark_dirty()
            self.assertIsNone(self.idx.wait_fresh())
        (self.root / "blob.bin").unlink()
        self.assertIsNone(self.idx.wait_fresh())
        self.assertEqual(self.idx.skipped["binary"], 0)

    def test_imports_stay_within_a_language(self):
        with self.idx._lock:
            graph = ci._import_graph(self.idx)
            fam = {self.idx.files[a].path: {self.idx.files[b].path for b in t} for a, t in graph.items()}
        self.assertNotIn("native/util.c", fam.get("src/Cart.java", set()))

    def test_callers_keep_language_and_class(self):
        out = self.callers("Cart.total")
        self.assertIn("function pay", out)
        self.assertNotIn("web/app.js", out)         # a JavaScript total() is another name
        self.assertNotIn("function show", out)      # Report.total
        out = self.callers("Money.add", depth=2)
        self.assertIn("function pay", out)
        self.assertNotIn("function show", out)      # level 2 keeps the class too

    def test_js_var_does_not_leak_block_scope_into_a_nested_function(self):
        self.assertIn("function handler", self.callers("total"))

    def test_callers_of_a_config_key(self):
        out = self.callers("web_app")
        self.assertIn("not code", out)
        self.assertIn("conf/compose.yaml", out)
        self.assertIn("load.py", out)

    def test_html_scripts_with_lang_alias_and_without_the_identifier_index(self):
        self.assertIn("function go", self.search("go", lang="js"))
        with self.idx._cond:
            self.idx.max_bytes = 1
            self.idx._enforce_budget()
        self.assertIn("web/page.html", self.callers("go"))

    def test_text_output_is_capped(self):
        _write(self.root, "many.txt", "".join(f"needle line {i} " + "x" * 150 + "\n"
                                              for i in range(300)))
        out = self.text("needle")
        self.assertLess(len(out), ci._MAX_TEXT_CHARS + 600)
        self.assertIn("narrow with path=", out)

    def test_find_symbol_sees_the_indexed_files_and_kind_equivalence(self):
        _write(self.root, "gen/x.py", "def hidden_one():\n    pass\n")
        self.idx.set_filter("gen/\n")
        self.assertIsNone(self.idx.wait_fresh())
        code_nav.set_index_provider(self.idx.provide, self.idx.paths_under)
        try:
            self.assertIn("no definition", code_nav.find_symbol("hidden_one", workdir=self.root))
            self.assertIn("method Cart.total",
                          code_nav.find_symbol("total", kind="function", workdir=self.root))
        finally:
            code_nav.set_index_provider(None)

    def test_constructor_parameters_are_not_bases(self):
        self.assertEqual(ci._drop_params("(val item: Circle) : Shape()"), " : Shape()")
        self.assertEqual(ci._drop_params("(int x, int y) implements P {"), " implements P {")
        self.assertEqual(ci._drop_params(" extends Shape {"), " extends Shape {")

    def test_module_receivers_are_not_class_callers(self):
        _write(self.root, "use_mod.py", "from . import shop as shop_mod\n\n\n"
                                        "def run():\n    return shop_mod.total()\n")
        self.assertNotIn("use_mod.py", self.callers("Cart.total"))
        self.assertIn("use_mod.py", self.callers("total"))

    def test_pickle_header_fingerprints_the_extractor(self):
        self.assertIn("source", ci._header(self.root))


class ReviewFixes0926(Base):
    """Regressions from the 2026-09-26 review of the index."""

    files = {
        # A form feed is a line break to str.splitlines(), not to the parser.
        "ff.py": "x = 1\n\x0c\ndef foo():\n    return 2\n\n\ndef bar():\n    return foo()\n",
        "pkg/__init__.py": "",
        "pkg/nav.py": "def parse(p):\n    return p\n\n\ndef outline(p):\n    return parse(p)\n",
        "pkg/rules.py": "class Rules:\n    @classmethod\n    def parse(cls, t):\n        return cls()\n",
        "user.py": ("import ast\nfrom pkg import nav\nfrom pkg.rules import Rules\n\n\n"
                    "def go(t):\n    ast.parse(t)\n    Rules.parse(t)\n    return nav.parse(t)\n"),
    }

    def test_line_numbers_follow_newlines_only(self):
        from harness.paths import split_lines
        self.assertEqual(split_lines("a\x0cb\r\nc d\n"), ["a\x0cb", "c d"])
        self.assertEqual("".join(split_lines("a\r\nb\n\nc", keepends=True)), "a\r\nb\n\nc")
        self.assertIn("L8 (call) return foo()", self.callers("foo"))
        self.assertIn("L8 [in bar]: return foo()", self.text("foo()"))
        self.assertIn("   8: ", tools._grep_file("foo", "ff.py", workdir=self.root))

    def test_regex_prefilter_keeps_real_matches(self):
        self.assertEqual(ci._regex_literals(r"(?!foo)bar"), ["bar"])
        self.assertEqual(ci._regex_literals(r"(?<!abc)def"), ["def"])
        self.assertEqual(ci._regex_literals(r"(?P<word>\w+)"), [])
        self.assertEqual(ci._regex_literals(r"x{3,100}"), [])
        self.assertEqual(ci._regex_literals(r"ab{0,3}cde"), ["cde"])
        self.assertEqual(ci._regex_literals(r"(?x) foo # bar"), [])
        self.assertIn("ff.py", self.text(r"(?!zzz)return foo", regex=True))

    def test_module_qualified_callers(self):
        out = self.callers("nav.parse")
        self.assertIn("'nav.parse' is defined at pkg/nav.py:L1-2 (function parse).", out)
        self.assertIn("function outline", out)          # a bare call inside the module
        self.assertIn("return nav.parse(t)", out)
        self.assertNotIn("Rules.parse(t)", out)
        self.assertIn("1 exact match for 'nav.parse'", self.search("nav.parse"))

    def test_bare_callers_drop_library_receivers_and_flag_ambiguity(self):
        out = self.callers("parse")
        self.assertNotIn("ast.parse", out)
        self.assertIn("Rules.parse(t)", out)
        self.assertIn("2 different definitions share this name", out)
        self.assertIn("'nav.parse'", out)
        self.assertIn("'Rules.parse'", out)

    def test_text_is_breadth_first(self):
        _write(self.root, "a_many.txt", "needle\n" * 40)
        _write(self.root, "z_one.txt", "needle\n")
        out = self.text("needle")
        self.assertIn("... 35 more in this file", out)
        self.assertIn("z_one.txt (1)", out)


class IndexRouting(Base):
    """/index-route (on by default): plain-text grep_files and file-name
    find_files are answered from the index; the rest still goes to the disk."""

    files = {**CHAIN, "img/logo.png": "\0PNG" * 8, "gen/.keep": ""}

    def run_tool(self, name, args, route=True):
        return tools.dispatch(name, args, self.root, index=self.idx, index_route=route)

    def test_literal_grep_is_answered_by_index_text(self):
        out = self.run_tool("grep_files", {"pattern": "retry_delay"})
        self.assertTrue(out.startswith("(grep_files is not needed"), out)
        self.assertIn('index_text("retry_delay")', out.splitlines()[0])
        self.assertIn("conf/app.yaml", out)
        self.assertIn("[in server.retry_delay]", out)       # the definition each hit is in
        self.assertIn("notes.txt", out)                      # plain text files too

    def test_regex_grep_runs_on_disk_with_a_leading_note(self):
        out = self.run_tool("grep_files", {"pattern": r"def \w+\("})
        self.assertTrue(out.startswith("(the code index is on:"), out)
        self.assertIn("a.py:1: def leaf():", out)

    def test_literal_grep_falls_back_to_the_disk(self):
        _write(self.root, "gen/out.txt", "zebra_marker\n")
        self.idx.set_filter("gen/\n")
        self.assertIsNone(self.idx.wait_fresh())
        out = self.run_tool("grep_files", {"pattern": "zebra_marker"})
        self.assertIn("gen/out.txt", out)
        if True:
            self.assertTrue(out.startswith("(the code index has no match"), out)

    def test_find_files_from_the_index_and_disk_fallback(self):
        out = self.run_tool("find_files", {"pattern": "*.py"})
        self.assertTrue(out.startswith("(find_files is not needed"), out)
        self.assertIn('index_search(query="*.py", kind="file")', out.splitlines()[0])
        self.assertIn("b.py  (python", out)
        # ...and that call really answers it
        out = self.search("*.py", kind="file")
        self.assertIn("3 files matching", out)
        out = self.run_tool("find_files", {"pattern": "*.png"})     # binary: not indexed
        self.assertTrue(out.startswith("(no file in the code index matches"), out)
        self.assertIn("img/logo.png", out)
        out = self.run_tool("find_files", {"pattern": "conf/*.yaml"})  # a directory part: disk
        self.assertEqual(out, "conf/app.yaml")

    def test_route_off_searches_the_disk_but_still_points_at_the_index(self):
        out = self.run_tool("grep_files", {"pattern": "retry_delay"}, route=False)
        self.assertIn("conf/app.yaml:3:", out)                      # the real grep
        self.assertTrue(out.startswith("(the code index is on:"), out)
        self.assertNotIn(tools._INDEX_GREP_TIP.strip(), out)        # led, not trailed
        self.assertEqual(self.run_tool("find_files", {"pattern": "a.py"}, route=False), "a.py")
        self.assertEqual(tools.dispatch("grep_files", {"pattern": "zzz_none"}, self.root,
                                        index=self.idx, index_route=False), "(no matches)")

    def test_file_tools_point_at_the_index_tools(self):
        self.assertIn("index_map()", self.run_tool("list_directory", {"path": "."}))
        self.assertIn('index_map(path="conf")', self.run_tool("code_outline", {"path": "conf"}))
        out = self.run_tool("grep_file", {"pattern": "leaf", "path": "b.py"})
        self.assertIn('index_text("leaf", path="b.py")', out)
        self.assertIn('index_search("mid")', self.run_tool("find_symbol", {"name": "mid"}))
        self.assertNotIn("index_search", self.run_tool("find_symbol",
                                                       {"name": "5", "directory": "b.py"}))
        plain = tools.dispatch("list_directory", {"path": "."}, self.root)     # index off
        self.assertNotIn("index_map", plain)

    def test_bad_arguments_still_get_the_normal_error(self):
        out = self.run_tool("grep_files", {"pattern": "x", "path": "a.py"})
        self.assertIn("does not accept", out)

    def test_literal_detection(self):
        self.assertEqual(tools._literal("os.path"), "os.path")
        self.assertEqual(tools._literal(r"foo\.bar"), "foo.bar")
        for rx in (r"\bword", "a|b", "def .*x", "(x)", "x+"):
            self.assertIsNone(tools._literal(rx), rx)


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

    def test_prompt_and_descriptions_put_the_index_first(self):
        self.assertNotIn("Code index: ON", self.h.messages[0]["content"])
        self.cmd("/index on")
        for mode in ("coding", "plan", "chat", "design", "momo"):
            self.h.set_mode(mode)
            prompt = self.h.messages[0]["content"]
            self.assertTrue(prompt.startswith("**Code index is ON"), mode)
            self.assertIn("## Code index: ON", prompt[-4000:], mode)
            self.assertIn(hmod._INDEX_FIRST[mode], prompt, mode)
        for name in ("grep_files", "find_files"):
            t = next(t for t in self.h._current_tools() if t["function"]["name"] == name)
            self.assertTrue(t["function"]["description"].startswith("Do NOT use this"), name)
            self.assertNotIn("anyway", t["function"]["description"])
        self.cmd("/index off")
        self.assertNotIn("Code index is ON", self.h.messages[0]["content"])

    def test_route_toggle(self):
        self.assertTrue(self.h.index_route)                        # on by default
        self.assertTrue(self.h.status_event().index_route)
        self.cmd("/index on")
        self.assertIsNone(self.h.index.wait_fresh())
        self.assertTrue(self.h._dispatch("grep_files", {"pattern": "leaf"}).startswith("(grep_files is"))
        self.assertIn("off", self.cmd("/index-route off"))
        self.assertFalse(self.h.index_route)
        self.assertEqual(json.loads((Path(self.home.name) / "prefs.json").read_text())["index_route"],
                         False)
        self.assertFalse(self.h.status_event().index_route)
        out = self.h._dispatch("grep_files", {"pattern": "leaf"})
        self.assertTrue(out.startswith("(the code index is on:"), out)   # disk grep, index hint
        self.assertIn("Code index is ON", self.h.messages[0]["content"])   # the bias stays
        self.assertIn("Route grep/find to the index: off", self.cmd("/index"))
        self.assertIn("on", self.cmd("/index-route on"))
        self.assertTrue(self.h.index_route)
        self.assertTrue(self.h._dispatch("grep_files", {"pattern": "leaf"}).startswith("(grep_files is"))

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
        for want in ("Budget", "Made of:", "Definitions", "Identifiers", "By language:", "/index-filter",
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
        self.assertIn("250,000", self.cmd("/index-max-files 250000"))
        self.assertEqual(self.h.status_event().index_max_files, 250_000)
        self.assertEqual(self.h.index.max_files, 250_000)
        self.assertIn("ERROR", self.cmd("/index-max-files 5"))
        self.assertIn("ERROR", self.cmd("/index-max-files many"))
        self.assertIn("3", self.cmd("/index-workers 3"))
        self.assertEqual(self.h.status_event().index_workers, 3)
        self.assertEqual(self.h.index.workers, 3)
        self.assertIn("Workers", self.cmd("/index"))
        self.assertIn("auto", self.cmd("/index-workers auto"))
        self.assertEqual(self.h.index.workers, 0)
        self.assertIn("ERROR", self.cmd("/index-workers 0"))
        self.assertIn("ERROR", self.cmd("/index-workers lots"))

    def test_persist_is_on_by_default(self):
        self.assertTrue(self.h.index_persist)

    def test_index_filter_commands(self):
        self.cmd("/index on")
        self.h.index.wait_fresh()
        out = self.cmd("/index-filter")
        self.assertIn(str(ci.filter_path(self.wd)), out)
        self.assertIn("node_modules/", out)
        self.assertIn("rules; re-listing", self.cmd("/index-filter add conf/"))
        self.h.index.wait_fresh()
        self.assertEqual(self.h.index.live_count(), len(CHAIN) - 1)
        self.assertIn("conf/", ci.filter_path(self.wd).read_text())
        self.assertIn("ERROR", self.cmd("/index-filter remove nope/"))
        self.cmd("/index-filter remove conf/")
        self.h.index.wait_fresh()
        self.assertEqual(self.h.index.live_count(), len(CHAIN))
        _write(self.wd, ".gitignore", "conf/\n")
        self.cmd("/index-filter reset")
        self.h.index.wait_fresh()
        self.assertEqual(self.h.index.live_count(), len(CHAIN) - 1)
        self.assertIn("ERROR", self.cmd("/index-filter bogus"))

    def test_index_filter_edit_opens_the_editor_in_the_tui_only(self):
        controller = Controller(self.h)
        tui = controller.submit("/index-filter edit", source="tui")
        self.assertEqual(tui.view, {"edit_index_filter": str(ci.filter_path(self.wd))})
        self.assertEqual(controller.submit("/index-filter edit", source="web").view, {})

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
            c = http.client.HTTPConnection("127.0.0.1", web.port, timeout=5)
            c.request("GET", "/api/index-filter")
            f = json.loads(c.getresponse().read())
            self.assertEqual(f["path"], str(ci.filter_path(self.wd)))
            self.assertIn("node_modules/", f["text"])
            self.assertEqual(post("/api/index-filter", {"text": f["text"] + "conf/\n"}), 200)
            self.h.index.wait_fresh()
            self.assertEqual(state()["index_files"], len(CHAIN) - 1)
            self.assertEqual(post("/api/index-filter", {"text": 5}), 400)
        finally:
            web.close()


if __name__ == "__main__":
    unittest.main()
