"""Remembered approvals for things a workspace brings along that momo would
run: project MCP servers (mcp/approval.py) and workspace tools
(tools/workspace.py).

An approval is a fingerprint (a hash of whatever the decision was about), kept
in a file under ~/.momo-harness/ — never in the workspace, so a cloned
repository cannot approve itself.  Changing what was approved changes the
fingerprint, so it asks again.  Denials are remembered the same way, which
keeps a denied item quiet instead of asking on every start.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path


def fingerprint(**parts) -> str:
    """A stable hash of the given values (JSON-serialisable)."""
    blob = json.dumps(parts, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ApprovalStore:
    """approved / denied fingerprints in one JSON file; path is a callable so
    a HOME changed after import (tests) is honoured."""

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"approved": [], "denied": []}
        return {"approved": list(data.get("approved") or []),
                "denied": list(data.get("denied") or [])}

    def status(self, fp: str) -> str:
        """"approved" | "denied" | "pending"."""
        data = self._load()
        if fp in data["approved"]:
            return "approved"
        return "denied" if fp in data["denied"] else "pending"

    def decide(self, fp: str, approve: bool) -> None:
        from .session import atomic_write
        with self._lock:
            data = self._load()
            for key in ("approved", "denied"):
                if fp in data[key]:
                    data[key].remove(fp)
            data["approved" if approve else "denied"].append(fp)
            atomic_write(self._path(), json.dumps(data, indent=1) + "\n")
