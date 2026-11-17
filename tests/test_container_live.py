"""momo-box against a real container engine (opt-in).

    MOMO_BOX_LIVE=1 .venv/bin/python -m pytest tests/test_container_live.py -q

Skipped unless MOMO_BOX_LIVE=1 and docker (or podman) answers: it builds the momo
image and starts containers.  No model is needed: a stub llama.cpp-style server
on this machine answers the chat requests.  What it checks is what unit tests
cannot: that the box really has no route out, that the gateway really filters,
and that work really lands in the source repo.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "momo-box"
_DOCKER_APP = "/Applications/Docker.app/Contents/Resources/bin"
PY = "/opt/momo/python/bin/python3"


def _engine() -> str | None:
    path = os.environ.get("PATH", "")
    if os.path.isdir(_DOCKER_APP):
        path = f"{_DOCKER_APP}:{path}"
    for name in ([os.environ["MOMO_BOX_RUNTIME"]] if os.environ.get("MOMO_BOX_RUNTIME")
                 else ["docker", "podman"]):
        exe = shutil.which(name, path=path)
        if exe and subprocess.run([exe, "info"], capture_output=True).returncode == 0:
            return exe
    return None


ENGINE = _engine() if os.environ.get("MOMO_BOX_LIVE") == "1" else None
ANSWER = "stub-model-says-hello"


class _Stub(BaseHTTPRequestHandler):
    """Enough of llama.cpp's server for momo: /props, /v1/models, a streamed chat."""
    seen: list[str] = []

    def log_message(self, *_):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.seen.append(f"GET {self.path}")
        if self.path.startswith("/props"):
            self._json({"default_generation_settings": {"n_ctx": 8192}, "chat_template": ""})
        elif self.path.endswith("/models"):
            self._json({"data": [{"id": "stub"}]})
        else:
            self._json({"status": "ok"})

    def do_POST(self):
        self.seen.append(f"POST {self.path}")
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not self.path.endswith("/chat/completions"):
            self._json({"tokens": []})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in ({"choices": [{"delta": {"content": ANSWER}}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 5, "completion_tokens": 3}}):
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


@unittest.skipUnless(ENGINE, "set MOMO_BOX_LIVE=1 with docker or podman running")
class LiveBox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("MOMO_BOX_LIVE_DIR") or None)
        cls.tmp = Path(cls._tmp.name).resolve()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.server = ThreadingHTTPServer(("0.0.0.0", 0), _Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.shutdown)
        cls.llm = f"http://localhost:{cls.server.server_address[1]}"
        cls.env = {**os.environ, "HOME": str(cls.tmp / "home"),
                   "XDG_CACHE_HOME": str(cls.tmp / "cache"),
                   "PATH": f"{os.path.dirname(ENGINE)}:{os.environ.get('PATH', '')}"}
        (cls.tmp / "home").mkdir()
        cls.src = cls.tmp / "src"
        cls.src.mkdir()
        cls.git("init", "-q", "-b", "main")
        (cls.src / "notes.txt").write_text("hello\n")
        cls.git("add", "-A")
        cls.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        cls.git("branch", "private")

    @classmethod
    def git(cls, *args) -> str:
        r = subprocess.run(["git", "-C", str(cls.src), *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError(f"git {args}: {r.stderr}")
        return r.stdout.strip()

    def box(self, *args, timeout=600) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(LAUNCHER), *args], capture_output=True,
                              text=True, env=self.env, timeout=timeout, stdin=subprocess.DEVNULL)

    def engine(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([ENGINE, *args], capture_output=True, text=True, timeout=120)

    def inside(self, name: str, code: str) -> str:
        r = self.engine("exec", f"momo-{name}", PY, "-c", code)
        return (r.stdout + r.stderr).strip()

    def status(self, name: str, file: str) -> dict:
        try:
            return json.loads((self.tmp / "cache" / "momo-box" / "boxes" / name / "status" / file)
                              .read_text())
        except (OSError, ValueError):
            return {}

    def api(self, name: str, port: int, path: str) -> dict:
        """GET a path of the box's web UI with its token."""
        token = json.loads((self.tmp / "cache" / "momo-box" / "boxes" / name / "box.json")
                           .read_text())["token"]
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     headers={"Cookie": f"momo_token={token}"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as r:
            return json.loads(r.read())

    def flags(self, *extra) -> list[str]:
        return ["--repo", str(self.src), "--llm", self.llm, "--provider", "llamacpp",
                "--no-prepare", *extra]

    def boxes(self) -> set[str]:
        r = self.engine("ps", "-a", "--filter", "label=momo-box=1", "--format", "{{.Names}}")
        return set(r.stdout.split())

    def test_1_headless_run_in_a_locked_box(self):
        _Stub.seen.clear()
        before = self.boxes()                   # your own boxes may be running
        r = self.box("run", "--net", "locked", *self.flags(), "-p", "say hello")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn(ANSWER, r.stdout)
        self.assertIn("POST /v1/chat/completions", _Stub.seen)     # through the gateway filter
        self.assertEqual(self.boxes() - before, set())             # an ephemeral box leaves nothing

    def test_2_web_box_is_isolated_and_lands_its_work(self):
        name = "live-t"
        self.addCleanup(lambda: self.box("rm", name, "--yes"))
        port = _free_port()
        r = self.box("web", "--name", name, "--port", str(port), *self.flags("--memory", "1g"))    # default: pull
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("(pull network) is up", r.stderr)
        # no route out, no DNS; the model server only for inference
        probe = self.inside(name, '''
import socket, urllib.request, urllib.error
def code(method, url):
    try:
        return urllib.request.urlopen(urllib.request.Request(
            url, method=method, data=b"{}" if method == "POST" else None), timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return type(e).__name__
try:
    socket.create_connection(("example.com", 443), timeout=4); print("internet=open")
except Exception as e:
    print("internet=closed")
print("models=%s delete=%s" % (code("GET", "http://gw:9000/v1/models"),
                               code("POST", "http://gw:9000/api/delete")))
''')
        self.assertIn("internet=closed", probe)
        self.assertIn("models=200 delete=403", probe)
        # only the commit it starts from came in
        refs = self.engine("exec", "-w", "/workspace", f"momo-{name}", "git", "for-each-ref",
                           "--format=%(refname)").stdout
        self.assertNotIn("private", refs)
        # a finished turn lands as momo/<name> without anyone asking
        self.engine("exec", f"momo-{name}", "sh", "-c",
                    "echo made-in-the-box > /workspace/out.txt && "
                    "printf '{\"turn\": 1, \"seq\": 1}' > /home/momo/.momo-box/turn.json")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if subprocess.run(["git", "-C", str(self.src), "rev-parse", "-q", "--verify",
                               f"momo/{name}"], capture_output=True).returncode == 0:
                break
            time.sleep(0.25)
        self.assertEqual(self.git("show", f"momo/{name}:out.txt"), "made-in-the-box")
        self.assertEqual(self.git("branch", "--show-current"), "main")       # checkout untouched
        self.assertEqual(self.git("status", "--porcelain"), "")
        # the Setup panel: host-written status, read-only inside the box
        ro = self.inside(name, '''
import json
print(json.load(open("/run/momo-box/setup.json"))["name"])
try:
    open("/run/momo-box/x", "w")
except OSError as e:
    print("ro", e.errno)
''')
        self.assertIn(name, ro)
        self.assertIn("ro 30", ro)                                   # EROFS
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.status(name, "sync.json").get("last"):
            time.sleep(0.25)
        self.assertEqual(self.status(name, "sync.json")["last"]["branch"], f"momo/{name}")
        safeguards = {r["id"]: r["ok"] for r in self.status(name, "setup.json")["safeguards"]}
        self.assertTrue(all(safeguards.values()), safeguards)
        view = self.api(name, port, "/api/setup")
        self.assertEqual(view["kind"], "box")
        self.assertEqual(view["box"]["sync_state"], "ok")
        self.assertEqual(view["model"]["location"]["kind"], "local")
        mem = view["memory"]                                       # the box's own cgroup
        self.assertGreater(mem["current"], 10 * 1024 ** 2)
        self.assertEqual(mem["max"], 1024 ** 3)                    # --memory 1g
        self.assertTrue({"momo", "supervisor"} <= {p["label"] for p in mem["processes"]})
        # stop, start again with its settings, remove
        self.assertEqual(self.box("stop", name).returncode, 0)
        again = self.box("start", name)
        self.assertEqual(again.returncode, 0, again.stderr[-2000:])
        self.assertIn("out.txt", self.engine("exec", "-w", "/workspace", f"momo-{name}", "ls").stdout)
        self.assertEqual(self.box("rm", name, "--yes").returncode, 0)
        left = self.engine("volume", "ls", "-q").stdout
        self.assertNotIn(f"momo-{name}-", left)

    def test_3_repo_network_rules(self):
        """.momo/network.json: its allows, denies and reasons, enforced by the gateway."""
        src = self.tmp / "netsrc"
        (src / ".momo").mkdir(parents=True)
        (src / ".momo/network.json").write_text(json.dumps({
            "instructions": "Only example.com is reachable.", "replace_defaults": True,
            "allow": {"example.com": "test host", "example.org": "other"},
            "deny": {"example.org": "denied on purpose"}}))
        for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
            subprocess.run(["git", "-C", str(src), *args], check=True, capture_output=True)
        name = "live-n"
        self.addCleanup(lambda: self.box("rm", name, "--yes"))
        r = self.box("web", "--name", name, "--port", str(_free_port()), "--repo", str(src),
                     "--llm", self.llm, "--provider", "llamacpp", "--no-prepare")
        self.assertNotEqual(r.returncode, 0)                       # repo network rules need approval
        self.assertIn("needs approval", r.stderr)
        r = self.box("web", "--name", name, "--port", str(_free_port()), "--repo", str(src),
                     "--llm", self.llm, "--provider", "llamacpp", "--no-prepare", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("allow example.org has no effect", r.stderr)
        probe = self.inside(name, '''
import socket
for target in ("example.com:443", "example.org:443", "pypi.org:443"):
    s = socket.create_connection(("gw", 3128), timeout=10)
    s.sendall(("CONNECT %s HTTP/1.1\\r\\n\\r\\n" % target).encode())
    print(target, s.recv(4096).decode(errors="replace").replace("\\r\\n", " "))
    s.close()
''')
        self.assertRegex(probe, r"example.com:443 HTTP/1.1 200")
        self.assertRegex(probe, r"example.org:443 HTTP/1.1 403 .*denied on purpose")
        self.assertRegex(probe, r"pypi.org:443 HTTP/1.1 403")         # the defaults were replaced
        log = self.box("netlog", name).stdout
        self.assertIn("(!example.org: denied on purpose)", log)
        # the Setup panel's network view, from the gateway's log
        deadline = time.monotonic() + 15
        hosts = {}
        while time.monotonic() < deadline:
            hosts = {h["host"]: h for h in self.status(name, "net.json").get("hosts", [])}
            if "example.org" in hosts and "pypi.org" in hosts:
                break
            time.sleep(0.5)
        self.assertEqual(hosts["example.org"]["denied"], 1)
        self.assertEqual(hosts["example.org"]["reason"], "denied on purpose")
        self.assertEqual(hosts["example.com"]["allowed"], 1)
        rules = self.status(name, "setup.json")["allowlist"]["rules"]
        self.assertIn({"kind": "!", "rule": "example.org", "reason": "denied on purpose",
                       "source": "network.json"}, rules)

    def turn(self, name: str, text: str, branch: str, seq: int) -> None:
        """A finished turn in box `name` that writes out.txt; waits until it lands."""
        self.engine("exec", f"momo-{name}", "sh", "-c",
                    f"echo {text} > /workspace/out.txt && "
                    f"printf '{{\"turn\": {seq}, \"seq\": {seq}}}' > /home/momo/.momo-box/turn.json")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            r = subprocess.run(["git", "-C", str(self.src), "show", f"{branch}:out.txt"],
                               capture_output=True, text=True)
            if r.stdout.strip() == text:
                return
            time.sleep(0.25)
        self.fail(f"{text} did not land on {branch}")

    def test_4_a_kept_box_stays_what_it_was(self):
        """--name reuse: the repo, a running box, `allow`, the gateway log, rm."""
        name = "live-k"
        self.addCleanup(lambda: self.box("rm", name, "--yes"))
        port = str(_free_port())
        r = self.box("web", "--name", name, "--port", port, *self.flags())
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.turn(name, "first-box", f"momo/{name}", 1)
        # a running box is not set up again, by run or web
        r = self.box("run", "--name", name, *self.flags(), "-p", "hi", timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("is running", r.stderr)
        self.assertEqual(self.engine("inspect", "-f", "{{.State.Running}}",
                                     f"momo-{name}").stdout.strip(), "true")
        # allow survives a restart; the gateway log does not
        self.assertEqual(self.box("allow", name, "example.net").returncode, 0)
        self.inside(name, '''
import socket
s = socket.create_connection(("gw", 3128), timeout=10)
s.sendall(b"CONNECT refused.invalid:443 HTTP/1.1\\r\\n\\r\\n"); s.recv(4096)
''')
        self.assertIn("refused.invalid", self.box("netlog", name).stdout)
        self.assertEqual(self.box("stop", name).returncode, 0)
        # another repo under this name is refused, and nothing changes
        other = self.tmp / "other"
        other.mkdir()
        for args in (["init", "-q", "-b", "main"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                      "--allow-empty", "-m", "o"]):
            subprocess.run(["git", "-C", str(other), *args], check=True, capture_output=True)
        r = self.box("web", "--name", name, "--port", port, "--repo", str(other),
                     "--llm", self.llm, "--provider", "llamacpp", "--no-prepare")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("its workspace holds", r.stderr)
        r = self.box("start", name)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        allow = self.engine("exec", f"momo-{name}-gw", "cat", "/gw/allow.txt").stdout
        self.assertIn("+example.net", allow)
        self.assertNotIn("refused.invalid", self.box("netlog", name).stdout)
        # rm keeps the branch; a new box of the same name lands next to it
        self.assertEqual(self.box("rm", name, "--yes").returncode, 0)
        self.assertEqual(self.git("show", f"momo/{name}:out.txt"), "first-box")
        self.assertEqual(self.git("for-each-ref", f"refs/momo-box/{name}"), "")
        r = self.box("web", "--name", name, "--port", port, *self.flags())
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.turn(name, "second-box", f"momo/{name}-incoming", 1)
        self.assertEqual(self.git("show", f"momo/{name}:out.txt"), "first-box")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
