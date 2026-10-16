"""Reading and parsing one file into a FileEntry — in the indexer thread, or in
a worker process for bulk builds (tree-sitter holds the GIL, so threads would
not run in parallel)."""
from __future__ import annotations

import os
import stat as stat_mod
import signal
from pathlib import Path

from .. import code_nav
from ..paths import BINARY_SNIFF_BYTES, MAX_SCAN_FILE_BYTES
from .records import FileEntry, _DECL_IMPORT_LANGS, _signature


def _build_entry(root: Path, rel: str, want_idents: bool, want_sig: bool):
    """Read and index one file outside the lock.  Returns (entry, rows, skip):
    entry None and skip None = the file is gone; skip = (reason, mtime, size)
    of a file that is left out."""
    full = root / rel
    try:
        st = full.stat()
    except OSError:
        return None, None, None
    if not stat_mod.S_ISREG(st.st_mode):
        return None, None, None
    stamp = (st.st_mtime_ns, st.st_size)
    if st.st_size > MAX_SCAN_FILE_BYTES:
        return None, None, ("too_big",) + stamp
    try:
        with open(full, "rb") as f:
            head = f.read(BINARY_SNIFF_BYTES)
            if b"\x00" in head:
                return None, None, ("binary",) + stamp     # never read the rest
            raw = head + f.read()
    except OSError:
        # Unreadable, not gone: remembered by stat, or every stat-diff re-queues it.
        return None, None, ("error",) + stamp
    lang = code_nav.language_for(full)
    symbols: list = []
    imports: list = []
    has_error = False
    rows = None
    package = ""
    nlines = raw.count(b"\n") + (0 if raw.endswith(b"\n") or not raw else 1)
    if lang is not None:
        try:
            parsed = code_nav.parse_uncached(full, raw)
        except Exception:
            parsed = None
            has_error = True
        if parsed is not None:
            symbols, imports = parsed.symbols, parsed.imports
            package = code_nav.package_of(parsed) if lang in _DECL_IMPORT_LANGS else ""
            has_error = parsed.tree.root_node.has_error
            nlines = len(parsed.lines)
            if want_idents:
                rows = code_nav.identifier_rows(parsed)
            del parsed
    entry = FileEntry(path=rel, mtime_ns=st.st_mtime_ns, size=st.st_size, lang=lang,
                      nlines=nlines, has_error=has_error, symbols=symbols, imports=imports,
                      package=package)
    if want_sig:
        entry.sig, entry.sig_bits = _signature(raw.lower())
    return entry, rows, None


def _build_many(root: str, rels: list[str], want_idents: bool, want_sig: bool) -> list:
    """Worker process: _build_entry for each path, None where it raised."""
    out = []
    for rel in rels:
        try:
            out.append(_build_entry(Path(root), rel, want_idents, want_sig))
        except Exception:
            out.append(None)
    return out


def _worker_init() -> None:
    # Ctrl-C belongs to the harness, and a worker's stray output (a grammar's
    # warning) must not land on the TUI's curses screen.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)


def _mp_context():
    # spawn, not fork: forking a process that runs threads can deadlock the child.
    import multiprocessing
    return multiprocessing.get_context("spawn")
