"""The web UI's Setup panel: where momo runs, where its model server runs, and
inside a momo-box the box's live status.

Local facts come from this process.  A box's facts come from the read-only
folder momo-box mounts at /run/momo-box (``--box-status``): the host writes
setup.json, sync.json and net.json there, so the model inside the box can read
them but cannot forge them.  They are still parsed as data — size-capped, typed,
control characters stripped — because the page renders them.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import platform
import subprocess
import threading
import time
from collections import deque
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

STARTED = time.time()
PERF_KEEP = 30                 # chat calls the throughput sparkline shows
LATENCY_TTL = 10.0             # seconds a latency probe is reused
LOAD_TTL = 10.0                # and a server-load reading
SLOW_MS = 1000                 # a probe slower than this makes the badge amber
SILENT_AFTER = 60.0            # a syncer heartbeat older than this: "syncer silent"
RECENT_DENY_SECS = 600         # denials in the badge: the last ten minutes
BOX_FILES = ("setup", "sync", "net", "stats")
MEM_KEEP = 60                  # memory samples the sparkline shows (~10 min at 10 s)
MEM_EVERY = 5.0                # seconds between two memory samples, at most
MEM_WARN, MEM_BAD = 80, 95     # % of the memory cap that turns the badge amber / red
MAX_FILE = 1_000_000           # bytes per status file
SCHEMA = 1

_LOCATION_LABELS = {
    "local": "same machine",
    "host": "this machine, from the box",
    "lan": "your network",
    "internet": "internet",
    "unknown": "unknown",
}
_HOST_ALIASES = ("host.docker.internal", "host.containers.internal", "gateway.docker.internal")
_LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa")


def clean(value, limit: int = 300) -> str:
    """One printable line: no control or format characters, capped."""
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def sanitize(value, depth: int = 0):
    """JSON from a status file, reduced to plain data: strings cleaned and
    capped, lists capped, numbers finite, nesting limited."""
    if depth > 6:
        return None
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if abs(value) < 2 ** 53 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return clean(value, 500)
    if isinstance(value, list):
        return [sanitize(v, depth + 1) for v in value[:200]]
    if isinstance(value, dict):
        return {clean(k, 64): sanitize(v, depth + 1)
                for k, v in list(value.items())[:200] if isinstance(k, str)}
    return None


def locate(url: str) -> dict:
    """Where a server is, from its URL: same machine, the box's host, your
    network, or the internet; and whether it is reached over https."""
    raw = url if "://" in url else f"http://{url}"
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return {"kind": "unknown", "label": _LOCATION_LABELS["unknown"], "host": "",
                "port": None, "https": False}
    kind = "internet"
    if not host:
        kind = "unknown"
    elif host == "localhost" or host.endswith(".localhost"):
        kind = "local"
    elif host in _HOST_ALIASES:
        kind = "host"
    else:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if host.endswith(_LAN_SUFFIXES) or "." not in host:
                kind = "lan"
        else:
            if ip.is_loopback:
                kind = "local"
            elif ip.is_private or ip.is_link_local:
                kind = "lan"
    return {"kind": kind, "label": _LOCATION_LABELS[kind], "host": clean(host, 200),
            "port": port, "https": parts.scheme == "https"}


def display_url(url: str) -> str:
    """A server URL without credentials or a query (the token rides in one)."""
    try:
        p = urlsplit(url if "://" in url else f"http://{url}")
        host = p.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{p.port}" if p.port else "")
        return clean(f"{p.scheme}://{netloc}{p.path}".rstrip("/"), 300)
    except ValueError:
        return ""


class PerfRing:
    """Time to first token and generation speed of the last main chat calls."""

    def __init__(self, keep: int = PERF_KEEP):
        self._rows: deque[dict] = deque(maxlen=keep)
        self._lock = threading.Lock()

    def record(self, started: float, first: float | None, ended: float,
               eval_tokens: int | None):
        gen = ended - (first or started)
        tok_s = (eval_tokens / gen) if eval_tokens and gen > 0.05 else None
        row = {"ts": time.time(), "secs": round(ended - started, 3),
               "ttft": round(first - started, 3) if first else None,
               "tokens": eval_tokens if isinstance(eval_tokens, int) else None,
               "tok_s": round(tok_s, 1) if tok_s else None}
        with self._lock:
            self._rows.append(row)

    def rows(self) -> list[dict]:
        with self._lock:
            return list(self._rows)

    def summary(self) -> dict:
        rows = self.rows()

        def median(key):
            vals = sorted(r[key] for r in rows[-5:] if r[key] is not None)
            return vals[len(vals) // 2] if vals else None
        return {"calls": len(rows), "tok_s": median("tok_s"), "ttft": median("ttft")}


class Latency:
    """A timed list-models request, reused for LATENCY_TTL seconds."""

    def __init__(self, ttl: float = LATENCY_TTL):
        self.ttl = ttl
        self.ts = 0.0
        self.ms: int | None = None
        self.ok: bool | None = None
        self._lock = threading.Lock()

    def probe(self, client, *, busy: bool, now: float | None = None) -> bool:
        """Measure again when due and idle; True when the result changed."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if busy or now - self.ts < self.ttl:
                return False
            self.ts = now
        began = time.monotonic()
        try:
            ok = bool(client.list_models())
        except Exception:
            ok = False
        ms = round((time.monotonic() - began) * 1000) if ok else None
        with self._lock:
            changed = (ok, _bucket(ms)) != (self.ok, _bucket(self.ms))
            self.ok, self.ms = ok, ms
        return changed

    def view(self) -> dict:
        with self._lock:
            return {"ok": self.ok, "ms": self.ms,
                    "age": round(time.monotonic() - self.ts, 1) if self.ts else None}


def _bucket(ms: int | None):
    """Latency coarse enough that jitter does not re-send the status bar."""
    if ms is None:
        return None
    return "slow" if ms > SLOW_MS else ms // 50


class Cached:
    """A value computed at most every `ttl` seconds."""

    def __init__(self, ttl: float):
        self.ttl, self.ts, self.value = ttl, 0.0, None
        self._lock = threading.Lock()

    def get(self, compute, now: float | None = None):
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.ts and now - self.ts < self.ttl:
                return self.value
        value = compute()
        with self._lock:
            self.ts, self.value = now, value
        return value


# ── the box's status folder ──────────────────────────────────────────────────

class BoxStatus:
    """The host-written files in --box-status, re-read when one changes."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._key = None
        self._data: dict = {}
        self._lock = threading.Lock()

    def _stamp(self):
        out = []
        for name in BOX_FILES:
            try:
                st = (self.folder / f"{name}.json").stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def read(self) -> dict:
        """{"setup": {...}, "sync": {...}, "net": {...}}: the files that parse."""
        key = self._stamp()
        with self._lock:
            if key == self._key:
                return self._data
        data = {}
        for name in BOX_FILES:
            path = self.folder / f"{name}.json"
            try:
                if path.stat().st_size > MAX_FILE:
                    continue
                got = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if isinstance(got, dict) and got.get("schema") == SCHEMA:
                data[name] = sanitize(got)
        with self._lock:
            self._key, self._data = key, data
        return data

    def changed_since(self, key) -> tuple[bool, object]:
        now = self._stamp()
        return now != key, now


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def sync_state(sync: dict | None, turn: int, now: float | None = None,
               running: bool = False) -> str:
    """ok | running | pending | incoming | error | syncer-silent | unknown.

    running: turn `turn` is under way and every turn before it was synced; its
    work lands when it ends.  pending: a finished turn has not been synced."""
    if not sync:
        return "unknown"
    now = time.time() if now is None else now
    alive = _num(sync.get("alive_ts"))
    if alive is None or now - alive > SILENT_AFTER:
        return "syncer-silent"
    last = sync.get("last") if isinstance(sync.get("last"), dict) else {}
    err = sync.get("error") if isinstance(sync.get("error"), dict) else {}
    if err and (_num(err.get("ts")) or 0) >= (_num(last.get("ts")) or 0):
        return "error"
    if last.get("incoming"):
        return "incoming"
    landed_turn = _num(sync.get("turn")) or 0
    if running and turn >= 1 and turn - landed_turn <= 1:
        return "running"
    if turn > landed_turn:
        return "pending"
    return "ok"


def recent_denied(net: dict | None, now: float | None = None) -> int:
    if not net or not isinstance(net.get("recent"), list):
        return 0
    now = time.time() if now is None else now
    return sum(1 for e in net["recent"] if isinstance(e, dict) and e.get("allowed") is False
               and e.get("kind") != "llm" and now - (_num(e.get("ts")) or 0) < RECENT_DENY_SECS)


# ── prepare: the box's prefetch, and whether its inputs changed since ────────
# A copy of container/entrypoint.py's prepare_hash (tests check they agree): the
# harness does not import the container's code.

_PREPARE_SKIP = {".git", ".momo", "node_modules", ".venv", "venv", "__pycache__", "build",
                 "target", "dist", ".gradle"}


def prepare_hash(commands: list[str], inputs: list[str], ws: Path) -> str | None:
    if not hasattr(PurePosixPath, "full_match"):
        return None
    h = hashlib.sha256(json.dumps(commands).encode())
    if inputs:
        for dirpath, dirnames, filenames in os.walk(ws):
            dirnames[:] = sorted(d for d in dirnames if d not in _PREPARE_SKIP)
            for name in sorted(filenames):
                rel = (Path(dirpath) / name).relative_to(ws).as_posix()
                if any(PurePosixPath(rel).full_match(p) for p in inputs):
                    h.update(rel.encode() + b"\0")
                    try:
                        h.update(hashlib.sha256((ws / rel).read_bytes()).digest())
                    except OSError:
                        pass
    return h.hexdigest()


def prepare_view(toolchain: dict, stamp_path: Path, ws: Path) -> dict:
    commands = [c for c in toolchain.get("prepare") or [] if isinstance(c, str)]
    inputs = [p for p in toolchain.get("prepare_inputs") or [] if isinstance(p, str)]
    out = {"commands": commands, "inputs": inputs, "ran": None, "stale": None}
    if not commands:
        return out
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stamp = {}
    if isinstance(stamp, dict) and _num(stamp.get("ts")):
        out["ran"] = stamp["ts"]
        digest = prepare_hash(commands, inputs, ws)
        if digest is not None and isinstance(stamp.get("hash"), str):
            out["stale"] = digest != stamp["hash"]
    return out


# ── the workspace's git state ────────────────────────────────────────────────

def git_view(workdir: Path) -> dict:
    def git(*args) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(workdir), *args], capture_output=True,
                               text=True, timeout=3, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return {"repo": False}
    status = git("status", "--porcelain") or ""
    last = (git("log", "-1", "--format=%h%x00%s%x00%ct") or "").strip().split("\0")
    return {"repo": True, "branch": clean(branch.strip(), 120),
            "changed": len([ln for ln in status.splitlines() if ln.strip()]),
            "last": ({"sha": clean(last[0], 20), "subject": clean(last[1], 200),
                      "ts": int(last[2]) if last[2].isdigit() else None}
                     if len(last) == 3 else None)}


def machine_view() -> dict:
    uid = os.getuid() if hasattr(os, "getuid") else None
    return {"host": clean(platform.node(), 120),
            "os": clean(f"{platform.system()} {platform.release()}", 120),
            "python": platform.python_version(), "pid": os.getpid(), "uid": uid,
            "started": STARTED}


def local_safeguards(h, web: dict | None) -> list[dict]:
    """What protects you when momo runs on this machine — mostly nothing, said
    plainly, with what momo itself checks."""
    rows = [
        {"id": "isolation", "ok": False, "label": "not isolated",
         "detail": "momo runs on this machine as you; shell commands it runs have your "
                   "permissions. momo-box runs it in a container instead."},
        {"id": "network", "ok": False, "label": "full network for commands",
         "detail": "shell commands can reach the internet and your LAN; "
                   f"momo's own web tools are {h.net_access} (/net)."},
        {"id": "files", "ok": False, "label": "your files",
         "detail": "momo's file tools work in the workspace; shell commands are not confined to it."},
        {"id": "run-confirm", "ok": bool(h.run_confirm), "label": "confirm each command",
         "detail": "on" if h.run_confirm else "off — /run-confirm turns it on"},
        {"id": "net-confirm", "ok": bool(h.net_confirm), "label": "confirm web writes",
         "detail": "on" if h.net_confirm else "off — /net-confirm turns it on"},
        {"id": "workspace-tools", "ok": True, "label": "workspace tools need approval",
         "detail": "a tool in .momo/tools/ runs only after you approve that version"},
    ]
    if web:
        rows.append({"id": "web-token", "ok": bool(web.get("token")) or bool(web.get("loopback")),
                     "label": "web UI access",
                     "detail": ("token required" if web.get("token") else "no token")
                               + (", this machine only" if web.get("loopback") else
                                  f", listening on {web.get('bind')}")})
    return rows


# ── memory ────────────────────────────────────────────────────────────────────
# Inside a box: the container's cgroup (v2, mounted read-only at /sys/fs/cgroup)
# and its processes from /proc.  Locally: momo's own process and the machine.
# A process is shown by a label, never its command line: momo's own carries
# the web token.

CGROUP = Path("/sys/fs/cgroup")


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def cgroup_memory(root: Path = CGROUP) -> dict | None:
    """{current, max, peak, oom_kills, anon, file, kernel} in bytes (max None
    when uncapped); None when this is not a cgroup v2 container."""
    current = _read_int(root / "memory.current")
    if current is None:
        return None
    out = {"current": current, "max": _read_int(root / "memory.max"),
           "peak": _read_int(root / "memory.peak"), "oom_kills": 0,
           "anon": None, "file": None, "kernel": None,
           # Docker lets a box with --memory M swap as much again by default.
           "swap": _read_int(root / "memory.swap.current"),
           "swap_max": _read_int(root / "memory.swap.max")}
    for name, keys in (("memory.events", ("oom_kill",)), ("memory.stat", ("anon", "file", "kernel"))):
        try:
            lines = (root / name).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            key, _, value = line.partition(" ")
            if key in keys and value.strip().isdigit():
                out["oom_kills" if key == "oom_kill" else key] = int(value)
    return out


_TOOLS = (("gradledaemon", "gradle daemon"), ("kotlincompiledaemon", "kotlin daemon"),
          ("gradle", "gradle"), ("plexus-classworlds", "maven"), ("maven", "maven"),
          ("kotlin", "kotlin"))


def _label(argv: list[str]) -> str:
    """What a process is.  The arguments only classify it; none is ever shown."""
    if not argv:
        return "?"
    names = [Path(a).name for a in argv[:3]]
    if "momo-coding-harness.py" in names:
        return "momo"
    if "entrypoint.py" in names:
        i = names.index("entrypoint.py") + 1
        sub = argv[i] if i < len(argv) else "start"
        return {"start": "supervisor", "watch": "sync watcher", "export": "snapshot",
                "prepare": "prepare", "preflight": "preflight"}.get(sub, "momo-box helper")
    name = clean(Path(argv[0]).name, 40) or "?"
    if name in ("java", "node"):
        args = " ".join(argv[1:]).lower()
        tool = next((label for key, label in _TOOLS if key in args), "")
        if tool:
            return f"{name} ({tool})"
    return name


def process_memory(proc: Path = Path("/proc"), top: int = 8) -> list[dict]:
    """The biggest processes by resident memory: [{pid, label, rss}]."""
    rows = []
    try:
        pids = [d for d in os.listdir(proc) if d.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            status = (proc / pid / "status").read_text()
            argv = [a.decode("utf-8", "replace")
                    for a in (proc / pid / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        rss = next((int(ln.split()[1]) * 1024 for ln in status.splitlines()
                    if ln.startswith("VmRSS:") and ln.split()[1].isdigit()), 0)
        if rss:
            rows.append({"pid": int(pid), "label": _label(argv), "rss": rss})
    rows.sort(key=lambda r: -r["rss"])
    return rows[:top]


def machine_memory() -> int | None:
    """This machine's RAM in bytes."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        pass
    try:
        r = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                           timeout=3, stdin=subprocess.DEVNULL)
        return int(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def self_memory() -> dict:
    """momo's own process: {current, peak} in bytes (either may be None)."""
    import resource
    import sys
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak = peak if sys.platform == "darwin" else peak * 1024
    current = None
    status = Path("/proc/self/status")
    if status.is_file():
        current = next((int(ln.split()[1]) * 1024 for ln in status.read_text().splitlines()
                        if ln.startswith("VmRSS:")), None)
    else:
        try:
            r = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True,
                               text=True, timeout=3, stdin=subprocess.DEVNULL)
            current = int(r.stdout.strip()) * 1024
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return {"current": current, "peak": peak or None}


class MemRing:
    """Memory use over the last ~10 minutes: (ts, bytes), at most every MEM_EVERY s."""

    def __init__(self, keep: int = MEM_KEEP, every: float = MEM_EVERY):
        self.every = every
        self._rows: deque = deque(maxlen=keep)
        self._lock = threading.Lock()

    def add(self, value: int | None, now: float | None = None):
        if value is None:
            return
        now = time.time() if now is None else now
        with self._lock:
            if self._rows and now - self._rows[-1][0] < self.every:
                return
            self._rows.append((now, value))

    def rows(self) -> list[list]:
        with self._lock:
            return [[round(t, 1), v] for t, v in self._rows]


def mem_state(mem: dict | None) -> tuple[int | None, bool, str]:
    """(percent of the cap or None, OOM kill seen, ok | warn | bad)."""
    if not mem:
        return None, False, "ok"
    oom = bool(mem.get("oom_kills"))
    pct = None
    if mem.get("max") and mem.get("current") is not None:
        pct = min(100, round(mem["current"] * 100 / mem["max"]))
    # Swapping more than a tenth of the cap: the box is out of memory, only slower.
    swapping = bool(mem.get("max") and (mem.get("swap") or 0) > mem["max"] // 10)
    state = "bad" if oom or (pct is not None and pct >= MEM_BAD) else \
        "warn" if swapping or (pct is not None and pct >= MEM_WARN) else "ok"
    return pct, oom, state
