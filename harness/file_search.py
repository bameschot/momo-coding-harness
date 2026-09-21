"""Workspace path search for @-mentions (shared by the TUI and the web UI)."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .tools import _SKIP_DIRS

_SEARCH_MAX_FILES = 20_000
_SEARCH_TTL_S = 10.0

_search_cache: dict = {"root": None, "ts": 0.0, "files": []}
_search_lock = threading.Lock()  # the TUI and web server threads share the cache


def workspace_files(root: Path) -> list[str]:
    """All non-hidden workspace files as relative paths, cached for a few seconds."""
    with _search_lock:
        now = time.monotonic()
        if _search_cache["root"] == root and now - _search_cache["ts"] < _SEARCH_TTL_S:
            return _search_cache["files"]
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
            rel_dir = os.path.relpath(dirpath, root)
            for f in sorted(filenames):
                if f.startswith("."):
                    continue
                files.append(f if rel_dir == "." else f"{rel_dir}/{f}")
                if len(files) >= _SEARCH_MAX_FILES:
                    break
            if len(files) >= _SEARCH_MAX_FILES:
                break
        _search_cache.update(root=root, ts=now, files=files)
        return files


def fuzzy_search(files: list[str], q: str, limit: int = 30) -> list[str]:
    """Case-insensitive subsequence match; basename hits first, then shorter paths."""
    q = q.lower()
    if not q:
        return files[:limit]
    scored = []
    for f in files:
        fl = f.lower()
        i = 0
        for ch in fl:
            if i < len(q) and ch == q[i]:
                i += 1
        if i < len(q):
            continue
        base = fl.rsplit("/", 1)[-1]
        rank = 0 if base.startswith(q) else 1 if q in base else 2 if q in fl else 3
        scored.append((rank, len(f), f))
    scored.sort()
    return [f for _, _, f in scored[:limit]]
