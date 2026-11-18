"""The web UI's Setup panel (harness/setup_view.py): where momo and its model
server run, throughput, server load, and inside a momo-box the host-written
status files."""
import http.client
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import harness as hmod
from harness import net as net_mod
from harness import session as session_mod
from harness import setup_view as sv
from harness.controller import Controller
from harness.llm.base import ChatResponse, LLMClient
from harness.llm.llamacpp_client import LlamaCppClient
from harness.llm.ollama_client import OllamaClient
from harness.web.server import start_web_server

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "container"))
import entrypoint  # noqa: E402


class StreamClient(LLMClient):
    provider_name = "fake"
    up = True

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        if on_delta:
            time.sleep(0.02)
            on_delta("content", "hello there")
        time.sleep(0.08)
        return ChatResponse(content="hello there", eval_tokens=12)

    def context_length(self):
        return 8192

    def list_models(self):
        return ["fake"] if self.up else []

    def abort(self):
        pass


class _HarnessCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(hmod, "make_client", lambda *a, **k: StreamClient("http://localhost:9931", "fake")),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
        ]
        for p in self._patches:
            p.start()
        self.ws = self.home / "ws"
        self.ws.mkdir()
        self.h = hmod.Harness(host="http://localhost:9931", model="fake", workdir=self.ws)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()


class Locate(unittest.TestCase):
    def test_classes(self):
        cases = {"http://localhost:11434": "local", "http://127.0.0.1:8080": "local",
                 "http://[::1]:8080": "local", "http://host.docker.internal:9931": "host",
                 "http://192.168.1.20:8080": "lan", "http://10.0.0.5": "lan",
                 "http://gpu.local:8080": "lan", "http://nas:8080": "lan",
                 "https://api.example.com/v1": "internet", "http://8.8.8.8": "internet",
                 "localhost:8080": "local", "http://": "unknown"}
        for url, kind in cases.items():
            with self.subTest(url=url):
                self.assertEqual(sv.locate(url)["kind"], kind)
        self.assertTrue(sv.locate("https://api.example.com")["https"])
        self.assertFalse(sv.locate("http://10.0.0.5")["https"])
        self.assertEqual(sv.locate("http://10.0.0.5")["label"], "your network")

    def test_display_url_drops_credentials_and_query(self):
        self.assertEqual(sv.display_url("http://user:pw@host:8080/v1?token=abc"), "http://host:8080/v1")


class Perf(unittest.TestCase):
    def test_ring_and_summary(self):
        ring = sv.PerfRing(keep=3)
        for i in range(5):
            ring.record(started=0.0, first=0.5, ended=2.5, eval_tokens=100 + i)
        rows = ring.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["tok_s"], 52.0)          # 104 tokens in 2 s
        self.assertEqual(rows[-1]["ttft"], 0.5)
        self.assertEqual(ring.summary()["calls"], 3)
        ring.record(started=0.0, first=None, ended=0.01, eval_tokens=None)    # no speed
        self.assertIsNone(ring.rows()[-1]["tok_s"])


class Latency(unittest.TestCase):
    def test_cached_and_not_while_busy(self):
        calls = []
        client = mock.Mock(list_models=lambda: calls.append(1) or ["m"])
        lat = sv.Latency(ttl=10)
        self.assertFalse(lat.probe(client, busy=True, now=100.0))
        self.assertEqual(calls, [])
        self.assertTrue(lat.probe(client, busy=False, now=100.0))
        self.assertTrue(lat.view()["ok"])
        self.assertFalse(lat.probe(client, busy=False, now=105.0))        # cached
        self.assertEqual(len(calls), 1)
        down = mock.Mock(list_models=lambda: [])
        self.assertTrue(lat.probe(down, busy=False, now=111.0))          # reachable → down
        self.assertFalse(lat.view()["ok"])
        self.assertIsNone(lat.view()["ms"])


class ServerLoad(unittest.TestCase):
    def test_ollama_ps(self):
        c = OllamaClient("http://localhost:11434", "m")
        c._client = mock.Mock(request_json=lambda m, p, *a, **k: {"models": [
            {"model": "gemma:2b", "size": 2000, "size_vram": 1500, "context_length": 4096,
             "expires_at": "2026-11-16T20:00:00Z"}, "junk"]})
        self.assertEqual(c.server_load(), {"kind": "ollama", "models": [
            {"name": "gemma:2b", "size": 2000, "vram": 1500, "context": 4096,
             "expires": "2026-11-16T20:00:00Z"}]})

    def test_llamacpp_slots(self):
        c = LlamaCppClient("http://localhost:8080", "m")
        c._client = mock.Mock(request_json=lambda m, p, *a, **k: [
            {"id": 0, "is_processing": True}, {"id": 1, "is_processing": False}])
        self.assertEqual(c.server_load(), {"kind": "llama.cpp", "slots": 2, "busy": 1})

        def off(*a, **k):
            raise RuntimeError("501")
        c._client = mock.Mock(request_json=off)
        self.assertIsNone(c.server_load()["slots"])
        # in a box: never asked (it can show other clients' prompts)
        c._client = mock.Mock(request_json=mock.Mock(side_effect=AssertionError("asked")))
        self.assertIn("inside a box", c.server_load(in_box=True)["note"])


class Sanitize(unittest.TestCase):
    def test_plain_data_only(self):
        got = sv.sanitize({"a": "x\x1b[31m‮", "n": float("nan"), "l": list(range(300)),
                           "deep": [[[[[[[["too deep"]]]]]]]], 1: "non-string key"})
        self.assertEqual(got["a"], "x[31m")
        self.assertIsNone(got["n"])
        self.assertEqual(len(got["l"]), 200)
        self.assertNotIn(1, got)
        self.assertNotIn("too deep", json.dumps(got))          # nesting is cut off


class SyncState(unittest.TestCase):
    def test_states(self):
        now = 1000.0
        alive = {"alive_ts": now - 5}
        self.assertEqual(sv.sync_state(None, 0, now), "unknown")
        self.assertEqual(sv.sync_state({"alive_ts": now - 61}, 0, now), "syncer-silent")
        self.assertEqual(sv.sync_state({**alive, "turn": 2, "last": {"ts": now - 1}}, 2, now), "ok")
        self.assertEqual(sv.sync_state({**alive, "turn": 1, "last": {"ts": now - 1}}, 2, now), "pending")
        self.assertEqual(sv.sync_state({**alive, "turn": 2, "last": {"ts": now - 1, "incoming": True}},
                                       2, now), "incoming")
        self.assertEqual(sv.sync_state({**alive, "last": {"ts": now - 9}, "error": {"ts": now - 2}},
                                       0, now), "error")
        self.assertEqual(sv.sync_state({**alive, "turn": 3, "last": {"ts": now - 1},
                                        "error": None}, 3, now), "ok")

    def test_a_running_turn_is_not_a_late_land(self):
        """Changes made during a turn land when it ends: until then the turn is
        running, not pending (a finished turn whose land is late)."""
        now = 1000.0
        alive = {"alive_ts": now - 5}
        self.assertEqual(sv.sync_state({**alive, "turn": 1}, 2, now, running=True), "running")
        self.assertEqual(sv.sync_state({**alive, "turn": 1}, 2, now), "pending")
        self.assertEqual(sv.sync_state({**alive, "turn": 1}, 3, now, running=True), "pending")
        self.assertEqual(sv.sync_state({**alive, "turn": 2}, 2, now, running=True), "running")
        self.assertEqual(sv.sync_state({"alive_ts": now - 61}, 2, now, running=True), "syncer-silent")
        self.assertEqual(sv.sync_state({**alive}, 0, now, running=True), "ok")    # a command, no turn yet
        self.assertEqual(sv.sync_state({**alive, "turn": 1, "error": {"ts": now}}, 2, now,
                                       running=True), "error")

    def test_recent_denied_skips_old_and_model_entries(self):
        now = 10_000.0
        net = {"recent": [{"ts": now - 5, "allowed": False}, {"ts": now - 700, "allowed": False},
                          {"ts": now - 5, "allowed": True},
                          {"ts": now - 5, "allowed": False, "kind": "llm"}]}
        self.assertEqual(sv.recent_denied(net, now), 1)


class PrepareHash(unittest.TestCase):
    def test_matches_the_entrypoint(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "pom.xml").write_text("<project/>")
            (ws / "sub").mkdir()
            (ws / "sub" / "pom.xml").write_text("<x/>")
            (ws / "node_modules").mkdir()
            (ws / "node_modules" / "pom.xml").write_text("skipped")
            cmds, inputs = ["mvn -q dependency:go-offline"], ["pom.xml", "**/pom.xml"]
            self.assertEqual(sv.prepare_hash(cmds, inputs, ws),
                             entrypoint.prepare_hash(cmds, inputs, ws))
            stamp = ws / "prepare.json"
            stamp.write_text(json.dumps({"hash": entrypoint.prepare_hash(cmds, inputs, ws),
                                         "ts": 5.0}))
            tc = {"prepare": cmds, "prepare_inputs": inputs}
            self.assertFalse(sv.prepare_view(tc, stamp, ws)["stale"])
            (ws / "pom.xml").write_text("<project>changed</project>")
            self.assertTrue(sv.prepare_view(tc, stamp, ws)["stale"])


class LocalView(_HarnessCase):
    def test_local_view(self):
        v = self.h.setup_view(web={"bind": "127.0.0.1", "port": 1, "token": True, "loopback": True,
                                   "url": "http://127.0.0.1:1/"})
        self.assertEqual(v["kind"], "local")
        self.assertIsNone(v["box"])
        self.assertEqual(v["model"]["location"]["kind"], "local")
        self.assertTrue(v["model"]["latency"]["ok"])
        self.assertFalse(v["workspace"]["git"]["repo"])
        ids = {r["id"]: r for r in v["safeguards"]}
        self.assertFalse(ids["isolation"]["ok"])            # said plainly
        self.assertTrue(ids["web-token"]["ok"])
        json.dumps(v)                                       # it is served as JSON

    def test_main_chat_calls_fill_the_perf_ring(self):
        self.h.send("hi")
        rows = self.h.perf.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tokens"], 12)
        self.assertIsNotNone(rows[0]["ttft"])
        self.assertGreater(rows[0]["tok_s"], 0)

    def test_status_fields(self):
        st = self.h.status_event()
        self.assertEqual((st.setup_kind, st.setup_label, st.setup_state), ("local", "fake", "ok"))
        self.h.client.up = False
        self.h.latency.ts = 0
        self.assertTrue(self.h.setup_tick(probe=True))
        self.assertEqual(self.h.status_event().setup_state, "bad")
        self.assertFalse(self.h.setup_tick(probe=False))     # nothing new


class BoxStatusRead(unittest.TestCase):
    def test_a_file_that_is_briefly_gone_keeps_its_last_value(self):
        """Podman's macOS file sharing hides a file for seconds after the host
        replaces it; a half-read file looks the same.  Neither blanks the panel."""
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)

            def put(name, data):
                (folder / f"{name}.json").write_text(json.dumps({**data, "schema": sv.SCHEMA}))
            put("setup", {"name": "fix"})
            put("sync", {"turn": 1})
            st = sv.BoxStatus(folder)
            self.assertEqual(st.read()["setup"]["name"], "fix")
            (folder / "setup.json").unlink()
            (folder / "sync.json").write_text('{"turn": 2, "sch')
            got = st.read()
            self.assertEqual((got["setup"]["name"], got["sync"]["turn"]), ("fix", 1))
            put("setup", {"name": "fix2"})
            put("sync", {"turn": 3})
            got = st.read()
            self.assertEqual((got["setup"]["name"], got["sync"]["turn"]), ("fix2", 3))
            self.assertNotIn("net", sv.BoxStatus(folder).read())       # never seen: absent


class BoxView(_HarnessCase):
    def setUp(self):
        super().setUp()
        self.status = self.home / "status"
        self.status.mkdir()
        self.h.box_status_dir = self.status
        now = time.time()
        self.write("setup", {"name": "fix", "net": "pull", "image": "momo-harness:abc",
                             "llm": {"provider": "llamacpp", "location": {"kind": "local",
                                     "label": "your machine", "host": "localhost", "port": 9931,
                                     "https": False}},
                             "toolchain": {"prepare": [], "prepare_inputs": []},
                             "safeguards": [{"id": "read-only-root", "ok": True, "label": "ro"}],
                             "future_field": {"x": 1}})
        self.write("sync", {"alive_ts": now, "turn": 0, "last": {"ts": now - 60, "branch": "momo/fix"}})
        self.write("net", {"recent": [{"ts": now, "allowed": False, "host": "evil.example",
                                       "port": 443}]})

    def write(self, name, data):
        (self.status / f"{name}.json").write_text(json.dumps({"schema": 1, **data}))

    def test_merged_view(self):
        v = self.h.setup_view()
        self.assertEqual(v["kind"], "box")
        self.assertEqual(v["model"]["location"]["label"], "your machine")   # the real server
        self.assertEqual(v["box"]["sync_state"], "ok")
        self.h._turn_count = 1
        self.assertEqual(self.h.setup_view(busy=True)["box"]["sync_state"], "running")
        self.assertEqual(self.h.setup_view()["box"]["sync_state"], "pending")
        self.h._turn_count = 0
        self.assertEqual(v["box"]["recent_denied"], 1)
        self.assertEqual(v["safeguards"][0]["id"], "read-only-root")
        st = self.h.status_event()
        self.assertEqual((st.setup_kind, st.setup_label, st.setup_sync), ("box", "fix · pull", "ok"))
        self.assertEqual(st.box_denied_recent, 1)

    def test_states_follow_the_files(self):
        self.h.setup_tick(probe=False)
        self.write("sync", {"alive_ts": time.time() - 120})
        os.utime(self.status / "sync.json", (time.time() + 5, time.time() + 5))
        self.assertTrue(self.h.setup_tick(probe=False))
        self.assertEqual(self.h.status_event().setup_state, "bad")       # syncer silent
        self.write("sync", {"alive_ts": time.time(), "turn": 0,
                            "last": {"ts": time.time(), "incoming": True}})
        os.utime(self.status / "sync.json", (time.time() + 9, time.time() + 9))
        self.assertTrue(self.h.setup_tick(probe=False))
        self.assertEqual(self.h.status_event().setup_state, "warn")

    def test_memory_in_the_view_and_the_badge(self):
        cg = self.home / "cg"
        fake_cgroup(cg, 870 * 2 ** 20, max_=1024 * 2 ** 20, peak=900 * 2 ** 20)
        self.h.cgroup_root = cg
        self.write("stats", {"gateway": {"current": 20 * 2 ** 20, "max": 2 ** 30}})
        self.write("setup", {"name": "fix", "net": "pull", "engine_memory": 2_525_360_128})
        mem = self.h.setup_view()["memory"]
        self.assertEqual(mem["kind"], "box")
        self.assertEqual((mem["pct"], mem["state"]), (85, "warn"))
        self.assertEqual(mem["gateway"]["current"], 20 * 2 ** 20)
        self.assertEqual(mem["engine_total"], 2_525_360_128)
        self.assertTrue(mem["history"])
        st = self.h.status_event()
        self.assertEqual((st.setup_mem_pct, st.setup_mem_oom, st.setup_state), (85, False, "warn"))
        fake_cgroup(cg, 100, max_=1000, oom=1)
        st = self.h.status_event()
        self.assertEqual((st.setup_mem_oom, st.setup_state), (True, "bad"))

    def test_bad_files_are_ignored(self):
        (self.status / "net.json").write_text("{not json")
        (self.status / "sync.json").write_text(json.dumps({"schema": 2, "alive_ts": 1}))
        (self.status / "setup.json").write_bytes(b"x" * (sv.MAX_FILE + 1))
        v = self.h.setup_view()
        self.assertEqual(v["box"]["setup"], {})
        self.assertIsNone(v["box"]["net"])
        self.assertEqual(v["box"]["sync_state"], "unknown")


def fake_cgroup(root: Path, current, max_="max", peak=None, oom=0):
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.current").write_text(f"{current}\n")
    (root / "memory.max").write_text(f"{max_}\n")
    if peak is not None:
        (root / "memory.peak").write_text(f"{peak}\n")
    (root / "memory.events").write_text(f"low 0\nhigh 0\nmax 0\noom {oom}\noom_kill {oom}\n")
    (root / "memory.stat").write_text("anon 1000\nfile 2000\nkernel 300\nsock 0\n")


class Memory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_cgroup_memory(self):
        self.assertIsNone(sv.cgroup_memory(self.tmp / "absent"))
        fake_cgroup(self.tmp / "cg", 90 * 2 ** 20, peak=364 * 2 ** 20)
        got = sv.cgroup_memory(self.tmp / "cg")
        self.assertEqual((got["current"], got["max"], got["peak"]), (90 * 2 ** 20, None, 364 * 2 ** 20))
        self.assertEqual((got["anon"], got["file"], got["kernel"], got["oom_kills"]), (1000, 2000, 300, 0))
        fake_cgroup(self.tmp / "capped", 900, max_=1000, oom=2)
        got = sv.cgroup_memory(self.tmp / "capped")
        self.assertEqual((got["max"], got["oom_kills"]), (1000, 2))

    def test_mem_state(self):
        self.assertEqual(sv.mem_state(None), (None, False, "ok"))
        self.assertEqual(sv.mem_state({"current": 50, "max": None, "oom_kills": 0}), (None, False, "ok"))
        self.assertEqual(sv.mem_state({"current": 85, "max": 100, "oom_kills": 0}), (85, False, "warn"))
        self.assertEqual(sv.mem_state({"current": 96, "max": 100, "oom_kills": 0}), (96, False, "bad"))
        self.assertEqual(sv.mem_state({"current": 10, "max": 100, "oom_kills": 1}), (10, True, "bad"))
        self.assertEqual(sv.mem_state({"current": 70, "max": 100, "oom_kills": 0, "swap": 30}),
                         (70, False, "warn"))                                    # swapping

    def test_processes_never_show_their_arguments(self):
        proc = self.tmp / "proc"
        for pid, argv, rss in ((7, ["/opt/momo/python/bin/python3", "/opt/momo/app/momo-coding-harness.py",
                                    "--web-token=SECRET-TOKEN"], 96000),
                               (8, ["/opt/momo/python/bin/python3", "-S",
                                    "/opt/momo/app/container/entrypoint.py", "start", "--"], 20000),
                               (9, ["/usr/bin/java", "-Dpassword=hunter2", "org.gradle.launcher.daemon.bootstrap.GradleDaemon"], 400000),
                               (10, ["sh", "-c", "export TOKEN=abc"], 1000)):
            (proc / str(pid)).mkdir(parents=True)
            (proc / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
            (proc / str(pid) / "status").write_text(f"Name:\tx\nVmRSS:\t{rss} kB\n")
        (proc / "self").mkdir()                       # not a pid
        rows = sv.process_memory(proc)
        self.assertEqual([r["label"] for r in rows], ["java (gradle daemon)", "momo", "supervisor", "sh"])
        self.assertEqual(rows[1]["rss"], 96000 * 1024)
        text = json.dumps(rows)
        for secret in ("SECRET-TOKEN", "hunter2", "TOKEN=abc"):
            self.assertNotIn(secret, text)

    def test_self_memory_and_machine(self):
        own = sv.self_memory()
        self.assertGreater(own["current"] or 0, 1_000_000)
        self.assertGreater(own["peak"] or 0, 1_000_000)
        self.assertGreater(sv.machine_memory() or 0, 100_000_000)

    def test_ring(self):
        ring = sv.MemRing(keep=3, every=5)
        for i, t in enumerate((0, 1, 6, 12, 18)):
            ring.add(100 + i, now=1000 + t)
        self.assertEqual([v for _, v in ring.rows()], [102, 103, 104])   # 1 s later skipped, cap 3
        ring.add(None)
        self.assertEqual(len(ring.rows()), 3)


class RecentHosts(unittest.TestCase):
    def test_counts_per_host(self):
        net_mod._RECENT_HOSTS.clear()
        with mock.patch.object(net_mod, "_get_once", side_effect=[{"raw": b""}, "ERROR: no"]):
            net_mod._get("https://docs.example.com/a", allow_private=False, max_bytes=1, timeout=1)
            net_mod._get("https://docs.example.com/b", allow_private=False, max_bytes=1, timeout=1)
        rows = net_mod.recent_hosts()
        self.assertEqual(rows[0]["host"], "docs.example.com")
        self.assertEqual(rows[0]["count"], 2)
        self.assertFalse(rows[0]["ok"])                   # the latest one failed


class SetupApi(_HarnessCase):
    def setUp(self):
        super().setUp()
        self.c = Controller(self.h)
        self.web = start_web_server(self.c, "127.0.0.1", 0, None)

    def tearDown(self):
        self.web.close()
        super().tearDown()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.web.port, timeout=5)
        conn.request("GET", path, headers={"Host": "127.0.0.1"})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r, data

    def test_api_setup_and_state(self):
        r, body = self.get("/api/setup")
        self.assertEqual(r.status, 200)
        v = json.loads(body)
        self.assertEqual(v["kind"], "local")
        self.assertEqual(v["web"]["port"], self.web.port)
        self.assertNotIn("token=", json.dumps(v))
        self.assertEqual(v["memory"]["kind"], "local")
        self.assertGreater(v["memory"]["current"], 0)
        _, body = self.get("/api/state")
        self.assertEqual(json.loads(body)["setup"], {"kind": "local"})
        for name in ("/setup.js", "/setup-render.js"):
            self.assertEqual(self.get(name)[0].status, 200)

    def test_probe_only_while_a_page_watches(self):
        calls = []
        self.h.client.list_models = lambda: calls.append(1) or ["fake"]
        self.c._setup_tick()
        self.assertEqual(calls, [])
        self.c.add_viewer(1)
        self.c._setup_tick()
        self.assertEqual(calls, [1])
        self.c.add_viewer(-1)
        self.assertEqual(self.c.web_viewers, 0)


if __name__ == "__main__":
    unittest.main()


class Activity(unittest.TestCase):
    def test_merge_dedups_overlapping_windows_and_caps(self):
        log = sv.ActivityLog(keep=4)
        win1 = [{"ts": 1.0, "kind": "proxy", "host": "a", "port": 443, "allowed": True},
                {"ts": 2.0, "kind": "proxy", "host": "b", "port": 443, "allowed": False}]
        self.assertEqual(log.merge(win1), 2)
        # The next net.json window overlaps the last one.
        win2 = win1[1:] + [{"ts": 3.0, "kind": "llm", "method": "POST", "path": "/v1/chat", "allowed": True}]
        self.assertEqual(log.merge(win2), 1)
        self.assertEqual([e["ts"] for e in log.rows()], [3.0, 2.0, 1.0])     # newest first
        log.merge([{"ts": float(t), "host": "c", "allowed": True} for t in range(4, 7)])
        self.assertEqual(log.total, 4)
        self.assertEqual([e["ts"] for e in log.rows()], [6.0, 5.0, 4.0, 3.0])
        # An entry pushed out of the cap is forgotten, so it is no longer deduped.
        self.assertEqual(log.merge(win1[:1]), 1)
        self.assertEqual(log.rows(limit=1)[0]["ts"], 1.0)
        self.assertEqual(log.merge("garbage"), 0)

    def test_web_requests_are_logged_without_the_query(self):
        before = net_mod.ACTIVITY.total
        with mock.patch.object(net_mod, "_get_once", return_value={"status": 200}):
            net_mod._get("https://example.com/search?q=secret", allow_private=False,
                         max_bytes=10, timeout=1)
        with mock.patch.object(net_mod, "_get_once", return_value="ERROR: refused | nope"):
            net_mod._get("https://bad.example/x", method="POST", allow_private=False,
                         max_bytes=10, timeout=1)
        self.assertEqual(net_mod.ACTIVITY.total, before + 2)
        bad, ok = net_mod.recent_activity(2)
        self.assertEqual((ok["host"], ok["path"], ok["method"], ok["allowed"]),
                         ("example.com", "/search", "GET", True))
        self.assertNotIn("secret", json.dumps(ok))
        self.assertEqual((bad["method"], bad["allowed"]), ("POST", False))
        self.assertIn("refused", bad["error"])


class Report(unittest.TestCase):
    def test_sections_rows_and_escaping(self):
        view = {"kind": "local", "machine": {"host": "mac", "os": "Darwin", "python": "3.14"},
                "workspace": {"path": "/w", "git": {"repo": False}},
                "network": {"web_tools": "on", "net_confirm": True, "mcp": [], "recent": []},
                "model": {"provider": "llama.cpp", "url": "http://x", "model": "m"},
                "safeguards": [{"ok": False, "label": "sandbox", "detail": "none"}],
                "memory": {"available": True, "current": 2**20}, "turn": 3, "box": None}
        rows = [{"ts": 2.0, "kind": "web", "method": "GET", "host": "b.example", "path": "/a|b",
                 "allowed": False, "error": "ERROR: line\nbreak"},
                {"ts": 1.0, "kind": "web", "method": "GET", "host": "a.example", "path": "/", "allowed": True}]
        md = sv.report_markdown(view, rows, [["think_budget", 8000, "--think-budget"]],
                                since=0.5, session="s1", now=3.0)
        for head in ("# momo setup report", "## Setup", "## Model", "## Settings", "## Network",
                     "## Activity", "## Sync", "## Safeguards", "## Memory"):
            self.assertIn(head, md)
        self.assertIn("2 requests since", md)
        self.assertIn("GET b.example/a\\|b", md)          # a pipe can't split the row
        self.assertIn("ERROR: line break", md)
        self.assertIn("a.example/", md)
        self.assertIn("| think_budget | 8000 | --think-budget |", md)
        self.assertNotIn("## Toolchain", md)                # box only


class ReportHarness(_HarnessCase):
    def test_box_merges_windows_and_setup_export_writes_all(self):
        from harness import commands
        status = self.home / "status"
        status.mkdir()
        self.h.box_status_dir = status

        def window(ts_list):
            (status / "net.json").write_text(json.dumps({"schema": 1, "recent": [
                {"ts": t, "kind": "proxy", "method": "CONNECT", "host": f"h{int(t)}.example",
                 "port": 443, "allowed": True} for t in ts_list]}))
        window([1.0, 2.0])
        self.h._box_data()
        window([2.0, 3.0, 4.0])                             # a different size: re-read
        v = self.h.setup_view(busy=True)
        self.assertEqual(v["activity"]["total"], 4)
        self.assertEqual(v["activity"]["source"], "gateway")
        self.assertEqual(v["activity"]["rows"][0]["host"], "h4.example")
        out = commands.handle("/setup-export", self.h).output
        self.assertIn("4 network requests", out)
        path = Path(out.rsplit(": ", 1)[1])
        self.assertEqual(path.parent, (self.ws / ".momo").resolve())
        md = path.read_text()
        for host in ("h1.example", "h2.example", "h3.example", "h4.example"):
            self.assertIn(host, md)
        self.assertIn("## Toolchain", md)
