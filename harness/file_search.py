"""Workspace path search for @-mentions (shared by the TUI and the web UI)."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .paths import walk_files

_SEARCH_MAX_FILES = 20_000
_SEARCH_TTL_S = 10.0

# (root, monotonic time, files), replaced whole so a lock-free read never sees a
# new root with old files.
_search_cache: tuple = (None, 0.0, [])
_search_lock = threading.Lock()  # one walk at a time; the TUI and web threads share it
_refreshing: set = set()         # roots with a background walk running


def workspace_files(root: Path) -> list[str]:
    """All non-hidden workspace files as relative paths, cached for a few seconds."""
    global _search_cache
    with _search_lock:
        c_root, ts, files = _search_cache
        if c_root == root and time.monotonic() - ts < _SEARCH_TTL_S:
            return files
        files = []
        for f in walk_files(root, skip_hidden=True):
            files.append(os.path.relpath(f, root))
            if len(files) >= _SEARCH_MAX_FILES:
                break
        _search_cache = (root, time.monotonic(), files)
        return files


def workspace_files_nowait(root: Path) -> list[str] | None:
    """The cached list for root without waiting: a stale one is returned while a
    background walk refreshes it; None only while the first walk runs.  For the
    TUI, whose key loop must not stall on a large tree."""
    c_root, ts, files = _search_cache
    cached = files if c_root == root else None
    if (cached is None or time.monotonic() - ts >= _SEARCH_TTL_S) and root not in _refreshing:
        _refreshing.add(root)

        def walk():
            try:
                workspace_files(root)
            finally:
                _refreshing.discard(root)
        threading.Thread(target=walk, name="momo-file-search", daemon=True).start()
    return cached


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
