"""The status momo-box writes for a box's Setup panel (momo's web UI) and for
`momo-box dash`.

    ~/.cache/momo-box/boxes/<name>/status/
        setup.json   how the box is set up, its allowlist and safeguards (launcher)
        sync.json    the syncer: alive, last land, history, last error
        net.json     the gateway's decisions, aggregated from net.log (syncer)

The folder is mounted read-only at /run/momo-box: the model can read it but
not change it.  Nothing in it may be secret (no web token) or say where things
live on this machine (the repo appears by name only).  Whatever came from the
box (net.log lines, the export record) is typed and cleaned before it is
written here.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = 1
HISTORY_KEEP = 50
RECENT_KEEP = 100
HOSTS_KEEP = 300
BEAT_EVERY = 15.0           # seconds between heartbeats written to sync.json


def clean(text, limit: int = 300) -> str:
    out = "".join(ch for ch in str(text) if ch.isprintable())
    return out[:limit]


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v):
        return v
    return None


def write_json(path: Path, data: dict):
    """Atomically, readable by the box's uid (0644)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=1))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class StatusWriter:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._beat = 0.0

    def ensure(self) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        os.chmod(self.folder, 0o755)
        return self.folder

    def _write(self, name: str, data: dict):
        self.ensure()
        write_json(self.folder / name, {**data, "schema": SCHEMA, "updated": time.time()})

    def read(self, name: str) -> dict:
        return read_json(self.folder / name)

    def write_setup(self, setup: dict):
        self._write("setup.json", setup)

    # ── sync.json ────────────────────────────────────────────────────────────

    def beat(self, force: bool = False):
        """The syncer is alive (written at most every BEAT_EVERY seconds)."""
        now = time.time()
        if not force and now - self._beat < BEAT_EVERY:
            return
        self._beat = now
        data = self.read("sync.json")
        data["alive_ts"] = now
        self._write("sync.json", data)

    def synced(self, result: dict, line: str | None, beat: bool = True):
        """After a sync attempt that did not fail: the turn it saw and, when
        something landed, the land.  beat=False: not the syncer (a one-off
        `momo-box sync`), so it says nothing about the syncer being alive."""
        now = time.time()
        data = self.read("sync.json")
        if beat:
            data["alive_ts"] = now
            self._beat = now
        data["error"] = None
        if _num(result.get("turn")) is not None:
            data["turn"] = result["turn"]
        if result.get("landed"):
            last = {"ts": now, "line": clean(line or "", 300),
                    "branch": clean(result.get("branch") or "", 120),
                    "incoming": bool(result.get("incoming")),
                    "reason": clean(result.get("reason") or "", 200),
                    "commits": _num(result.get("commits")) or 0,
                    "shortstat": clean(result.get("shortstat") or "", 200),
                    "skipped_large": [clean(x, 200) for x in result.get("skipped_large") or []][:20],
                    "head": clean(result.get("head") or "", 64),
                    "turn": _num(result.get("turn"))}
            data["last"] = last
            hist = [h for h in data.get("history") or [] if isinstance(h, dict)]
            hist.append({k: last[k] for k in ("ts", "branch", "incoming", "commits",
                                              "shortstat", "head", "turn")})
            data["history"] = hist[-HISTORY_KEEP:]
        self._write("sync.json", data)

    def failed(self, text: str, beat: bool = True):
        now = time.time()
        data = self.read("sync.json")
        if beat:
            data["alive_ts"] = now
            self._beat = now
        data["error"] = {"ts": now, "text": clean(text, 500)}
        self._write("sync.json", data)

    def write_net(self, snapshot: dict):
        self._write("net.json", snapshot)

    def write_stats(self, gateway: dict | None):
        """What only the host sees: the gateway container's memory."""
        self._write("stats.json", {"gateway": gateway, "ts": time.time()})


# ── setup.json ───────────────────────────────────────────────────────────────

_LABELS = {"local": "your machine", "lan": "your network", "internet": "internet"}


def locate(url: str) -> dict:
    """Where the model server is, as the user named it (before the box's
    rewrite to the gateway)."""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        port = p.port
    except ValueError:
        return {"kind": "unknown", "label": "unknown", "host": "", "port": None, "https": False}
    kind = "internet"
    if host in ("localhost", "host.docker.internal", "host.containers.internal") \
            or host.endswith(".localhost"):
        kind = "local"
    else:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if host.endswith((".local", ".lan", ".home", ".internal")) or "." not in host:
                kind = "lan"
        else:
            kind = "local" if ip.is_loopback else "lan" if (ip.is_private or ip.is_link_local) \
                else "internet"
    return {"kind": kind, "label": _LABELS[kind], "host": clean(host, 200), "port": port,
            "https": p.scheme == "https"}


def allow_rules(lines: list[str]) -> list[dict]:
    """allow.txt lines as the panel's table: kind (+ yours, ! denied, plain),
    rule, reason and where it came from."""
    out = []
    for ln in lines:
        rule, _, why = ln.partition("#")
        rule, why = rule.strip(), why.strip()
        if not rule:
            continue
        kind = rule[0] if rule[0] in "+!" else ""
        bare = rule.lstrip("+!")
        if kind == "+":
            source = "you (momo-box allow)" if why == "momo-box allow" else "you (--allow-host)"
        elif why == "registry":
            source = "toolchain"
        elif why == ".momo/box.json":
            source = "box.json"
        else:
            source = "network.json"
        reason = "" if why in ("--allow-host", "momo-box allow", "registry", ".momo/box.json") \
            else why
        out.append({"kind": kind, "rule": clean(bare, 200), "reason": clean(reason, 200),
                    "source": source})
    return out


def safeguards(net: str, inspect: dict | None, internal: bool | None, *,
               llm_filtered: bool, llm_paths: list[str], llm_https: bool) -> list[dict]:
    """Each safeguard checked against the running container (docker inspect),
    not against the flags we meant to pass.  `internal`: the box network's
    Internal flag (None for an open box)."""
    rows = []

    def add(id_, ok, label, detail=""):
        rows.append({"id": id_, "ok": bool(ok), "label": label, "detail": clean(detail, 300)})
    gated = net in ("pull", "locked")
    if gated:
        add("internal-network", internal is True, "no route out",
            "internal network: the gateway is the only way through" if internal
            else "the box's network is not internal")
        add("proxy", True, "allowlist proxy" if net == "pull" else "no proxy",
            "only the allowlisted hosts and ports, no TLS interception" if net == "pull"
            else "locked: nothing but the model server")
        if llm_https:
            add("llm-filter", True, "model server only",
                "an https server: an opaque tunnel to it, no other host")
        else:
            add("llm-filter", llm_filtered, "model API filtered",
                ("inference paths only: " + ", ".join(llm_paths)) if llm_filtered
                else "--llm-filter off: every request to the model server passes")
    else:
        add("internal-network", False, "no route out", "open network: the internet and your LAN")
        add("proxy", False, "allowlist proxy", "open network: no allowlist")
        add("llm-filter", False, "model API filtered", "no gateway in an open box")
    if not isinstance(inspect, dict):
        add("inspected", False, "container inspected", "docker inspect gave nothing")
        return rows
    hc = inspect.get("HostConfig") if isinstance(inspect.get("HostConfig"), dict) else {}
    cfg = inspect.get("Config") if isinstance(inspect.get("Config"), dict) else {}
    mounts = [m for m in inspect.get("Mounts") or [] if isinstance(m, dict)]
    add("read-only-root", hc.get("ReadonlyRootfs") is True, "read-only root filesystem",
        "only /workspace, /home/momo and /tmp are writable")
    drop = [str(c).upper() for c in hc.get("CapDrop") or []]
    added = [str(c) for c in hc.get("CapAdd") or []]
    # Docker records `--cap-drop ALL` as ALL.  Podman lists each dropped capability
    # and reports what is left in BoundingCaps (null: none); its EffectiveCaps is
    # null for any non-root user, so it can't tell a partial drop from a full one.
    none_left = "ALL" in drop or "CAP_ALL" in drop or (
        "BoundingCaps" in inspect and not inspect.get("BoundingCaps")
        and not inspect.get("EffectiveCaps"))
    add("cap-drop", none_left and not added, "no capabilities",
        "all dropped" + (f", but added: {', '.join(added)}" if added else ""))
    sec = [str(o) for o in hc.get("SecurityOpt") or []]
    add("no-new-privileges", any(o.startswith("no-new-privileges") and not o.endswith("false")
                                 for o in sec), "no new privileges", "setuid binaries can't gain root")
    add("not-privileged", hc.get("Privileged") is not True, "not privileged",
        "privileged mode is on" if hc.get("Privileged") else "")
    sockets = [m.get("Source", "") for m in mounts
               if str(m.get("Source", "")).endswith((".sock", "docker.sock", "podman.sock"))]
    add("no-engine-socket", not sockets, "no container-engine socket",
        f"mounted: {', '.join(sockets)}" if sockets else "the box can't start containers")
    pids = _num(hc.get("PidsLimit")) or 0
    mem = _num(hc.get("Memory")) or 0
    cpus = (_num(hc.get("NanoCpus")) or 0) / 1e9
    add("limits", pids > 0, "resource limits",
        f"{pids or 'no'} pids limit, memory {_size(mem) if mem else 'unlimited'}, "
        f"{f'{cpus:g} CPUs' if cpus else 'all CPUs'}")
    user = str(cfg.get("User") or "")
    add("uid", user not in ("", "0", "root") and not user.startswith("0:"), "not root",
        f"runs as {user or 'root (image default)'}")
    inbox = next((m for m in mounts if m.get("Destination") == "/in"), None)
    add("inbox-read-only", inbox is not None and inbox.get("RW") is False, "source read-only",
        "the bundle at /in is mounted read-only" if inbox else "no /in mount")
    status = next((m for m in mounts if m.get("Destination") == "/run/momo-box"), None)
    add("status-read-only", status is not None and status.get("RW") is False,
        "this status read-only", "the model can read /run/momo-box but not change it")
    return rows


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:g} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ── net.json ─────────────────────────────────────────────────────────────────

class NetAggregator:
    """net.log lines (from inside the gateway, which saw requests the model
    made) reduced to totals, per-host counts and the latest decisions."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.totals = {"allowed": 0, "denied": 0}
        self.hosts: dict[tuple[str, int], dict] = {}
        self.recent: deque[dict] = deque(maxlen=RECENT_KEEP)
        self.llm = {"requests": 0, "inference": 0, "refused": 0, "last_refused": "",
                    "last_error": "", "last_ts": None}
        self.changed = True

    def feed(self, line: str) -> bool:
        """One net.log line; False when it is not a decision."""
        try:
            e = json.loads(line)
        except ValueError:
            return False
        if not isinstance(e, dict) or not isinstance(e.get("allowed"), bool):
            return False
        ts = _num(e.get("ts")) or time.time()
        allowed = e["allowed"]
        method = clean(e.get("method") or "", 10)
        if e.get("kind") == "llm":
            path = clean(e.get("path") or "", 200)
            entry = {"ts": ts, "kind": "llm", "method": method, "path": path, "allowed": allowed}
            status = _num(e.get("status"))
            if status is not None:
                entry["status"] = status
            self.llm["requests"] += 1
            self.llm["last_ts"] = ts
            if not allowed:
                self.llm["refused"] += 1
                self.llm["last_refused"] = f"{method} {path}"
            if e.get("error"):
                self.llm["last_error"] = clean(e["error"], 200)
                entry["error"] = self.llm["last_error"]
            if method == "POST":
                self.llm["inference"] += 1
            # momo's health checks (GET /v1/models every few seconds) would push
            # everything else out of the feed: they are counted, not listed.
            if method != "GET" or not allowed or entry.get("error"):
                self.recent.append(entry)
            self.changed = True
            return True
        host = clean(e.get("host") or "", 200).lower()
        port = _num(e.get("port"))
        port = port if isinstance(port, int) and 0 < port < 65536 else 0
        if not host:
            return False
        rule, reason = clean(e.get("rule") or "", 200), clean(e.get("reason") or "", 200)
        self.totals["allowed" if allowed else "denied"] += 1
        row = self.hosts.get((host, port))
        if row is None:
            if len(self.hosts) >= HOSTS_KEEP:
                oldest = min(self.hosts, key=lambda k: self.hosts[k]["last_ts"])
                del self.hosts[oldest]
            row = self.hosts[(host, port)] = {"host": host, "port": port, "allowed": 0,
                                              "denied": 0, "last_ts": ts, "rule": "", "reason": ""}
        row["allowed" if allowed else "denied"] += 1
        row["last_ts"] = ts
        row["rule"], row["reason"] = rule, reason
        entry = {"ts": ts, "kind": "proxy", "method": method, "host": host, "port": port,
                 "allowed": allowed}
        if rule:
            entry["rule"] = rule
        if reason:
            entry["reason"] = reason
        self.recent.append(entry)
        self.changed = True
        return True

    def snapshot(self) -> dict:
        self.changed = False
        return {"totals": dict(self.totals),
                "hosts": sorted(self.hosts.values(), key=lambda r: -r["last_ts"]),
                "recent": list(self.recent), "llm": dict(self.llm)}


# ── for `momo-box dash` (the box's own momo derives these itself) ────────────

SILENT_AFTER = 60.0
RECENT_DENY_SECS = 600


def sync_state(sync: dict | None, now: float | None = None) -> str:
    """ok | incoming | error | syncer-silent | unknown (as setup_view.sync_state,
    without the box's own turn count)."""
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
    return "incoming" if last.get("incoming") else "ok"


def recent_denied(net: dict | None, now: float | None = None) -> int:
    if not net or not isinstance(net.get("recent"), list):
        return 0
    now = time.time() if now is None else now
    return sum(1 for e in net["recent"] if isinstance(e, dict) and e.get("allowed") is False
               and e.get("kind") != "llm" and now - (_num(e.get("ts")) or 0) < RECENT_DENY_SECS)


# ── memory (docker stats) ────────────────────────────────────────────────────

_UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
          "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def parse_size(text: str) -> int | None:
    """'19.57MiB' → bytes; None when it is not a size."""
    import re
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgt]?i?b)\s*", str(text), re.I)
    if not m:
        return None
    try:
        return int(float(m.group(1)) * _UNITS[m.group(2).lower()])
    except (ValueError, KeyError):
        return None


def parse_mem_usage(text: str) -> dict | None:
    """docker stats' MemUsage, '19.57MiB / 1GiB' → {current, max}."""
    used, sep, limit = str(text).partition("/")
    current = parse_size(used)
    if current is None:
        return None
    return {"current": current, "max": parse_size(limit) if sep else None}


def docker_stats(lines: str) -> dict[str, dict]:
    """`docker stats --no-stream --format '{{json .}}'` output → {name: {current, max}}.
    Docker gives MemUsage as text ('19.57MiB / 1GiB'), Podman as bytes, with
    MemLimit beside it."""
    out = {}
    for line in lines.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("Name"), str):
            used, limit = row.get("MemUsage", ""), row.get("MemLimit")
            if isinstance(used, (int, float)) and not isinstance(used, bool) and used >= 0:
                ok = isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0
                mem = {"current": int(used), "max": int(limit) if ok else None}
            else:
                mem = parse_mem_usage(used)
            if mem is not None:
                out[clean(row["Name"], 100)] = mem
    return out
