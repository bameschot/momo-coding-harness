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
ACTIVITY_KEEP = 5000           # network requests the session activity log keeps
ACTIVITY_VIEW = 300            # of which the Setup drawer is sent (it polls every 3 s)
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


class ActivityLog:
    """Every network request this momo saw since it started, up to keep: the
    web tools' own requests (local) or the box gateway's decisions, merged from
    each net.json read, whose window holds only the last 100 (box).  The Setup
    panel's Activity table shows the newest; /setup-export writes all of it."""

    _KEY = ("ts", "kind", "method", "host", "port", "path", "allowed")

    def __init__(self, keep: int = ACTIVITY_KEEP):
        self.since = time.time()
        self._rows: deque[dict] = deque(maxlen=keep)
        self._keys: deque[tuple] = deque(maxlen=keep)
        self._seen: set[tuple] = set()
        self._lock = threading.Lock()

    def _key(self, e: dict) -> tuple:
        return tuple(e.get(k) for k in self._KEY)

    def _append(self, entry: dict) -> bool:
        key = self._key(entry)
        if key in self._seen:
            return False
        if len(self._keys) == self._keys.maxlen:
            self._seen.discard(self._keys[0])
        self._keys.append(key)
        self._seen.add(key)
        self._rows.append(entry)
        return True

    def add(self, entry: dict) -> None:
        with self._lock:
            self._append(dict(entry))

    def merge(self, entries) -> int:
        """Add the entries not seen yet (a net.json window overlaps the last
        one), oldest first; how many were new."""
        if not isinstance(entries, list):
            return 0
        rows = sorted((e for e in entries if isinstance(e, dict) and isinstance(e.get("ts"), (int, float))),
                      key=lambda e: e["ts"])
        with self._lock:
            return sum(self._append(dict(e)) for e in rows)

    def rows(self, limit: int | None = None) -> list[dict]:
        """Newest first; limit keeps only the newest that many."""
        with self._lock:
            rows = list(self._rows)
        rows.reverse()
        return rows if limit is None else rows[:limit]

    @property
    def total(self) -> int:
        with self._lock:
            return len(self._rows)

    def view(self, source: str, limit: int = ACTIVITY_VIEW) -> dict:
        return {"rows": self.rows(limit), "total": self.total, "since": self.since,
                "source": source}


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
        with self._lock:
            before = self._data
        for name in BOX_FILES:
            path = self.folder / f"{name}.json"
            try:
                if path.stat().st_size > MAX_FILE:
                    continue
                got = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                # The host replaces these files whole; on Podman's macOS file
                # sharing a replaced file is missing for a few seconds.  Keep what
                # we had (sync.json's alive_ts still ages, so a dead syncer shows).
                if name in before:
                    data[name] = before[name]
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


# ── /setup-export ─────────────────────────────────────────────────────────────

def _cell(value) -> str:
    """One Markdown table cell: no pipes or line breaks break the row."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = " ".join(str(value).split())
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return out


def _facts(pairs: list[tuple]) -> list[str]:
    rows = [[k, v] for k, v in pairs if v not in (None, "", [], {})]
    return _table(["", ""], rows) if rows else ["(nothing to show)"]


def _iso(ts) -> str:
    if not isinstance(ts, (int, float)):
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _mb(n) -> str:
    return f"{n / 2**20:,.0f} MB" if isinstance(n, (int, float)) else ""


def _request(e: dict) -> str:
    if e.get("kind") == "llm":
        return f"model {e.get('method', '')} {e.get('path', '')}".strip()
    if e.get("kind") == "web":
        return f"{e.get('method', '')} {e.get('host', '')}{e.get('path', '')}".strip()
    port = e.get("port")
    return f"{e.get('method', '')} {e.get('host', '')}{f':{port}' if port else ''}".strip()


def _outcome(e: dict) -> str:
    if e.get("rule"):
        return e["rule"] + (f" — {e['reason']}" if e.get("reason") else "")
    if e.get("error"):
        return e["error"]
    return f"HTTP {e['status']}" if e.get("status") else ""


def report_markdown(view: dict, activity: list[dict], settings: list[list], *,
                    since: float | None = None, session: str = "",
                    now: float | None = None) -> str:
    """The Setup panel as one Markdown document: what was set up when it was
    exported, every setting, and all the session's network activity (newest
    first).  A pure function of its inputs."""
    now = time.time() if now is None else now
    box = view.get("box") or {}
    in_box = view.get("kind") == "box"
    m = view.get("model") or {}
    lines = ["# momo setup report", "",
             *_facts([("exported", _iso(now)), ("runs", "in a momo-box" if in_box else "locally"),
                      ("workspace", (view.get("workspace") or {}).get("path")),
                      ("session", session), ("turns", view.get("turn"))]), ""]

    lines += ["## Setup", ""]
    mach = view.get("machine") or {}
    web = view.get("web") or {}
    if in_box:
        st = box.get("setup") or {}
        src = st.get("source") or {}
        lines += _facts([("box", st.get("name")), ("network", st.get("net")), ("image", st.get("image")),
                         ("source", f"{src.get('name', '')}{' @ ' + src['ref'] if src.get('ref') else ''}"
                          if src else ""),
                         ("created", _iso(st.get("created"))), ("sync", box.get("sync_state"))])
    else:
        lines += _facts([("momo", f"{mach.get('host', '')} · {mach.get('os', '')} · Python {mach.get('python', '')}"),
                         ("web UI", web.get("url")),
                         ("isolation", "none: momo-box runs momo in a container")])
    lines.append("")

    lines += ["## Model", ""]
    lat, ctx, perf = m.get("latency") or {}, m.get("context") or {}, m.get("perf") or {}
    lines += _facts([("server", f"{m.get('provider', '')} · {m.get('url', '')}"),
                     ("where", (m.get("location") or {}).get("label")), ("model", m.get("model")),
                     ("reachable", m.get("reachable")),
                     ("latency", f"{lat['ms']} ms" if isinstance(lat.get("ms"), (int, float)) else ""),
                     ("context", f"{ctx.get('used', 0):,} of {ctx.get('limit', 0):,} tokens ({ctx.get('pct', 0)}%) · "
                                 f"window {ctx.get('window', 0):,}" if ctx else ""),
                     ("generation", f"{perf['tok_s']} tok/s (median, last 5)" if perf.get("tok_s") else ""),
                     ("first token", f"{perf['ttft']:.2f} s" if isinstance(perf.get("ttft"), (int, float)) else ""),
                     ("replies measured", perf.get("calls"))])
    lines.append("")

    lines += ["## Settings", "", *_table(["setting", "value", "flag"], settings), ""]

    lines += ["## Network", ""]
    n = view.get("network") or {}
    lines += _facts([("fetch_url / web_search", n.get("web_tools")), ("confirm writes", n.get("net_confirm")),
                     ("shell commands", "not filtered by momo" if not in_box else "through the box's network")])
    lines.append("")
    if n.get("mcp"):
        lines += ["### MCP servers", "", *_table(["name", "transport", "target", "state", "tools"],
                  [[r.get("name"), r.get("transport"), r.get("target"), r.get("state"), r.get("tools")]
                   for r in n["mcp"]]), ""]
    if in_box:
        st = box.get("setup") or {}
        al = st.get("allowlist") or {}
        net = box.get("net") or {}
        if al.get("rules"):
            lines += ["### Allowlist", "", *_table(["kind", "rule", "why", "from"],
                      [[r.get("kind") or "allowed", r.get("rule"), r.get("reason"), r.get("source")]
                       for r in al["rules"]]), ""]
        if net.get("hosts"):
            lines += ["### Hosts", "", *_table(["host", "allowed", "refused", "last", "rule"],
                      [[f"{x.get('host', '')}{':' + str(x['port']) if x.get('port') else ''}",
                        x.get("allowed", 0), x.get("denied", 0), _iso(x.get("last_ts")), x.get("rule")]
                       for x in net["hosts"]]), ""]
    elif n.get("recent"):
        lines += ["### Hosts reached", "", *_table(["host", "requests", "last", "ok"],
                  [[r.get("host"), r.get("count"), _iso(r.get("ts")), r.get("ok")] for r in n["recent"]]), ""]

    who = "the box gateway's decisions" if in_box else "momo's web tool requests (fetch_url, web_search)"
    lines += ["## Activity", "",
              f"{len(activity):,} request{'s' if len(activity) != 1 else ''} since {_iso(since)}, "
              f"newest first: {who}."
              + ("" if in_box else " Shell commands are not tracked."), ""]
    if activity:
        lines += _table(["time", "ok", "request", "rule / result"],
                        [[_iso(e.get("ts")), e.get("allowed"), _request(e), _outcome(e)] for e in activity])
    else:
        lines.append("(none)")
    lines.append("")

    lines += ["## Sync", ""]
    if in_box:
        sync = box.get("sync") or {}
        last = sync.get("last") or {}
        lines += _facts([("state", box.get("sync_state")), ("last land", _iso(last.get("ts"))),
                         ("branch", last.get("branch")), ("commit", str(last.get("head") or "")[:12]),
                         ("changes", last.get("shortstat"))])
        hist = list(reversed(sync.get("history") or []))
        if hist:
            lines += ["", *_table(["time", "branch", "commits", "changes"],
                      [[_iso(r.get("ts")), r.get("branch"), r.get("commits"), r.get("shortstat")] for r in hist])]
    else:
        w = view.get("workspace") or {}
        g = w.get("git") or {}
        lg = g.get("last") or {}
        lines += _facts([("path", w.get("path")),
                         ("git", f"{g.get('branch')} · {g.get('changed')} changed file(s)" if g.get("repo")
                          else "not a git repository"),
                         ("last commit", f"{lg.get('sha')} {lg.get('subject')} ({_iso(lg.get('ts'))})" if lg else ""),
                         ("momo's changes", f"{w.get('changes_files', 0)} file(s) in {w.get('undo_count', 0)} "
                                            "checkpoint(s) since the last /keep")])
    lines.append("")

    if in_box:
        t = (box.get("setup") or {}).get("toolchain") or {}
        p = box.get("prepare") or {}
        lines += ["## Toolchain", "", *_facts([("from", t.get("source")), ("presets", ", ".join(t.get("presets") or [])),
                  ("base image", t.get("base")), ("packages", " ".join(t.get("packages") or [])),
                  ("prepare", "; ".join(p.get("commands") or [])),
                  ("prepare ran", _iso(p.get("ran"))), ("prepare stale", p.get("stale"))]), ""]

    rows = view.get("safeguards") or []
    lines += ["## Safeguards", ""]
    lines += (_table(["ok", "guard", "what it means"], [[r.get("ok"), r.get("label"), r.get("detail")] for r in rows])
              if rows else ["(no report)"])
    lines.append("")

    mem = view.get("memory") or {}
    lines += ["## Memory", ""]
    lines += (_facts([("in use", _mb(mem.get("current"))), ("cap", _mb(mem.get("max"))), ("peak", _mb(mem.get("peak"))),
                      ("machine", _mb(mem.get("machine_total"))), ("state", mem.get("state")),
                      ("OOM kills", mem.get("oom_kills"))])
              if mem.get("available") else ["(not available)"])
    return "\n".join(lines) + "\n"
