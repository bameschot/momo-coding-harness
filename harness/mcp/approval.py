"""Approval of project MCP servers (<workdir>/.mcp.json).

A project file arrives with a cloned repository, and a stdio server entry is a
command momo would run — so a project server only starts once the user has
approved it.  The approval is a fingerprint of the workdir, the server name and
its launch settings (command, args, env, url, headers — unexpanded), so editing
the entry asks again.  The store itself is shared with workspace tools
(harness/approvals.py).
"""
from __future__ import annotations

from pathlib import Path

from ..approvals import ApprovalStore
from ..approvals import fingerprint as _fingerprint
from ..paths import momo_home
from .config import ServerConfig


def _path() -> Path:
    return momo_home() / "mcp-approved.json"


_STORE = ApprovalStore(_path)


def fingerprint(workdir: Path, cfg: ServerConfig) -> str:
    return _fingerprint(workdir=str(Path(workdir).resolve()), name=cfg.name,
                        launch=cfg.launch_spec())


def status(workdir: Path, cfg: ServerConfig) -> str:
    """"approved" | "denied" | "pending"; servers outside the project file are approved."""
    if cfg.source != "project":
        return "approved"
    return _STORE.status(fingerprint(workdir, cfg))


def decide(workdir: Path, cfg: ServerConfig, approve: bool) -> None:
    _STORE.decide(fingerprint(workdir, cfg), approve)
