"""Approval of project MCP servers (<workdir>/.mcp.json).

A project file arrives with a cloned repository, and a stdio server entry is a
command momo would run — so a project server only starts once the user has
approved it.  The approval is a fingerprint of the workdir, the server name and
its launch settings (command, args, env, url, headers — unexpanded), so editing
the entry asks again.  Denials are remembered the same way, which keeps a
denied server quiet instead of asking on every start.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from ..paths import momo_home
from .config import ServerConfig

_lock = threading.Lock()


def _path() -> Path:
    return momo_home() / "mcp-approved.json"


def fingerprint(workdir: Path, cfg: ServerConfig) -> str:
    blob = json.dumps({"workdir": str(Path(workdir).resolve()), "name": cfg.name,
                       "launch": cfg.launch_spec()}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"approved": [], "denied": []}
    return {"approved": list(data.get("approved") or []), "denied": list(data.get("denied") or [])}


def status(workdir: Path, cfg: ServerConfig) -> str:
    """"approved" | "denied" | "pending"; servers outside the project file are approved."""
    if cfg.source != "project":
        return "approved"
    fp = fingerprint(workdir, cfg)
    data = _load()
    if fp in data["approved"]:
        return "approved"
    return "denied" if fp in data["denied"] else "pending"


def decide(workdir: Path, cfg: ServerConfig, approve: bool) -> None:
    from ..session import atomic_write
    fp = fingerprint(workdir, cfg)
    with _lock:
        data = _load()
        for key in ("approved", "denied"):
            if fp in data[key]:
                data[key].remove(fp)
        data["approved" if approve else "denied"].append(fp)
        atomic_write(_path(), json.dumps(data, indent=1) + "\n")
