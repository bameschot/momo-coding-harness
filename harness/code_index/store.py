"""ProjectIndex: the in-memory store, the indexer thread that keeps it current,
and its memory budget, file limit, filter and save/load.  Its settings parsers
are shared by the CLI flags, the saved prefs and the /index-* commands."""
from __future__ import annotations

import array
import os
import pickle
import stat as stat_mod
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from .. import code_nav, ignore_rules
from ..net import format_size, parse_size
from ..session import atomic_write
from . import build, persist
from .records import (FileEntry, _C_FILE, _C_IMPORT, _C_NAMEREF, _C_NEWNAME, _C_OCC,
                      _C_SIG_OVERHEAD, _C_SYMBOL, _LINE_BITS, _LINE_MASK, _fmt_size)
from .persist import _RestrictedUnpickler, _header, _untrusted_reason, pickle_path

DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # default memory budget of the index (/index-max-mem)
MIN_MAX_BYTES = 1024 * 1024            # smallest budget /index-max-mem accepts
DEFAULT_MAX_FILES = 100_000            # a workdir of $HOME must not index the whole disk (/index-max-files)
MIN_MAX_FILES = 100                    # smallest file limit /index-max-files accepts
MAX_WORKERS = 64                       # largest worker count /index-workers accepts

# Every query re-checks the disk (a stat-diff: ~20 ms for 1,500 files).  Only a
# tree whose stat-diff is slow is throttled, to 10x what the last one took.
_STAT_THROTTLE_S = 0.0      # a minimum throttle: 0 here; tests raise it to act as a slow tree
_SLOW_DIFF_S = 0.2
_THROTTLE_PER_DIFF = 10
_WAIT_POLL_S = 0.25         # wait_fresh re-checks the cancel flag this often
_PROGRESS_EVERY_S = 0.25    # on_change is called at most this often while building
_COMPACT_AFTER = 500        # dead files before the occurrence index is compacted mid-drain
_UNPARTIAL_AT = 0.8         # budget share below which files left out are indexed again
_PARALLEL_MIN = 200         # queued files before worker processes pay for their start-up
_PARALLEL_CHUNK = 16        # files per worker task
_AUTO_WORKERS_CAP = 8       # measured: no gain past 8 (a few big files dominate)


def auto_workers() -> int:
    """The worker count /index-workers auto picks: a core left for the UI."""
    return max(1, min((os.cpu_count() or 1) - 1, _AUTO_WORKERS_CAP))


# Setting parsers shared by the CLI flags, the saved prefs and the /index-*
# commands: (value, "") or (None, what is wrong).

def parse_max_mem(value) -> tuple[int | None, str]:
    size = parse_size(value)
    if size is None:
        return None, f"not a size: {value}. Use bytes or a unit, e.g. 100mb, 512kb, 1gb."
    if size < MIN_MAX_BYTES:
        return None, f"the minimum is {format_size(MIN_MAX_BYTES)}"
    return size, ""


def parse_max_files(value) -> tuple[int | None, str]:
    if isinstance(value, bool):
        return None, f"not a number: {value}"
    try:
        n = int(str(value).replace(",", "").replace("_", ""))
    except ValueError:
        return None, f"not a number: {value}"
    if n < MIN_MAX_FILES:
        return None, f"the minimum is {MIN_MAX_FILES:,} files"
    return n, ""


def parse_workers(value) -> tuple[int | None, str]:
    """auto -> 0 (a saved pref stores it as the int 0), else 1..MAX_WORKERS."""
    s = str(value).strip().lower()
    if s == "auto" or (value == 0 and type(value) is int):
        return 0, ""
    if s.isdigit() and 1 <= int(s) <= MAX_WORKERS:
        return int(s), ""
    return None, f"expected auto or a number from 1 to {MAX_WORKERS}, got: {value}"


class ProjectIndex:
    """See the package docstring.  All public methods are thread-safe."""

    def __init__(self, root: Path, max_bytes: int = DEFAULT_MAX_BYTES, on_change=None,
                 max_files: int = DEFAULT_MAX_FILES, filter_text: str | None = None,
                 workers: int = 0, on_wait=None):
        """filter_text None: load (or seed and save) the project's filter file,
        on the indexer thread — seeding walks the tree for .gitignore files.
        workers 0: auto_workers(); 1: build in the indexer thread only.
        on_wait: callable(done, total), wait_fresh's default when a query blocks."""
        self.root = root.resolve()
        self.max_bytes = max(MIN_MAX_BYTES, int(max_bytes))
        self.max_files = max(MIN_MAX_FILES, int(max_files))
        self.workers = max(0, min(MAX_WORKERS, int(workers)))
        self._pool_broken = False               # worker processes failed: build serially
        self.on_change = on_change              # callable(index), throttled
        self.on_wait = on_wait
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self.files: list[FileEntry | None] = []
        self._free: list[int] = []
        self._by_path: dict[str, int] = {}
        self._sym_by_name: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._idents: dict[str, array.array] = {}
        self._dead: set[int] = set()            # removed file ids not yet compacted away
        self._dead_names: set[str] = set()
        self._queue: dict[str, None] = {}       # ordered set of relpaths to (re)index
        self._busy = False                      # the worker holds an item, or re-lists, outside the lock
        self._built = False
        self._stop = False
        self._dirty = True                      # force a stat-diff at the next query
        self._rescan = False                    # the worker must re-list the project first
        self._listing = 0                       # bumped when the filter or file limit changes
        self._last_diff = 0.0
        self._last_progress = 0.0
        self.state = "idle"                     # idle | building | refreshing | stopped
        self.done = 0
        self.total = 0
        self.components = {"trigrams": True, "idents": True}
        self.partial = False                    # symbols alone exceeded the budget
        self.mem = {"files": 0, "idents": 0, "trigrams": 0}
        self.skipped = {"binary": 0, "too_big": 0, "budget": 0, "error": 0, "limit": 0}
        # Files left out, with the stat they were judged at: an unchanged one is
        # not re-read at every stat-diff.  rel -> (reason, mtime_ns, size)
        self._skipped: dict[str, tuple[str, int, int]] = {}
        self._beyond: frozenset[str] = frozenset()   # listed past max_files: kept out
        # Until the worker has loaded the filter file, nothing is listed (the
        # first _diff runs after it) and index_filter() reads the file itself.
        self.filter_pending = filter_text is None
        self.filter_text = filter_text or ""
        self._rules = ignore_rules.Rules.parse(self.filter_text)
        self._diff_cost = 0.0
        self.version = 0
        self._notices: list[str] = []
        self._rank_cache: tuple | None = None
        self._graph_cache: tuple | None = None
        self._modules_cache: tuple | None = None
        self._imports_cache: tuple | None = None
        self._hierarchy_cache: tuple | None = None
        self.last_error = ""
        self._thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, load_pickle: bool = False) -> None:
        self._thread = threading.Thread(target=self._run, args=(load_pickle,),
                                        name="code-index", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self.state = "stopped"
            self._cond.notify_all()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ── notices / status ─────────────────────────────────────────────────────

    def pop_notices(self) -> list[str]:
        with self._lock:
            out, self._notices = self._notices, []
        return out

    def mem_used(self) -> int:
        return sum(self.mem.values())

    def live_count(self) -> int:
        with self._lock:
            return len(self._by_path)

    def has_file(self, *rels: str) -> bool:
        """Whether any of these project-relative paths is indexed."""
        with self._lock:
            return any(r in self._by_path for r in rels)

    def has_files_under(self, rel_dir: str) -> bool:
        """Whether any indexed file lies under this directory ("." = the root)."""
        with self._lock:
            return self._has_under(rel_dir)

    def _has_under(self, rel_dir: str) -> bool:
        prefix = "" if rel_dir in ("", ".") else rel_dir.rstrip("/") + "/"
        return any(r.startswith(prefix) for r in self._by_path)

    def degraded(self) -> bool:
        return self.partial or not all(self.components.values())

    def is_fresh(self) -> bool:
        with self._lock:
            return self._built and not self._queue and not self._busy and not self._rescan

    def _changed(self, force: bool = False) -> None:
        cb = self.on_change
        if cb is None:
            return
        now = time.monotonic()
        if not force and now - self._last_progress < _PROGRESS_EVERY_S:
            return
        self._last_progress = now
        try:
            cb(self)
        except Exception:
            pass

    # ── freshness ────────────────────────────────────────────────────────────

    def invalidate(self, paths) -> None:
        """Queue files the harness itself just wrote, moved or deleted."""
        with self._cond:
            for p in paths:
                if not p:
                    continue
                full = Path(p) if os.path.isabs(str(p)) else self.root / str(p)
                rel = self.rel(full)
                if rel is None:
                    continue
                if rel not in self._by_path and (full.is_dir() or self._has_under(rel)):
                    # A folder was moved or removed: the files in it are only
                    # found by re-listing, whatever the stat-diff throttle.
                    self._dirty = True
                    continue
                # A filtered-out file is only queued to drop it (the worker skips it).
                if rel in self._by_path or not self._rules.excluded(rel):
                    self._queue[rel] = None
            self._cond.notify_all()

    def mark_dirty(self) -> None:
        """Something outside the harness's file tools may have changed files
        (run_command): the next query does a full stat-diff, whatever the throttle."""
        self._dirty = True

    def wait_fresh(self, cancel: threading.Event | None = None, on_wait=None) -> str | None:
        """Block until every change on disk is indexed.  Returns an ERROR
        string when cancelled or stopped, else None."""
        if self._stop:
            return self._stopped_error()
        on_wait = on_wait or self.on_wait
        self._maybe_diff()
        waited = False
        with self._cond:
            while self._queue or self._busy or self._rescan or not self._built:
                if self._stop:
                    return self._stopped_error()
                if cancel is not None and cancel.is_set():
                    return "ERROR: cancelled while waiting for the code index"
                if not waited and on_wait is not None:
                    waited = True
                    try:
                        on_wait(self.done, self.total)
                    except Exception:
                        pass
                self._cond.wait(_WAIT_POLL_S)
        return None

    def _stopped_error(self) -> str:
        if self.last_error:
            return f"ERROR: the code index stopped after an internal error ({self.last_error})"
        return "ERROR: the code index is off"

    def _maybe_diff(self) -> None:
        if not self._built:
            return          # the worker's initial diff is still to come or running
        slow = self._diff_cost if self._diff_cost > _SLOW_DIFF_S else 0.0
        throttle = max(_STAT_THROTTLE_S, _THROTTLE_PER_DIFF * slow)
        if not self._dirty and time.monotonic() - self._last_diff < throttle:
            return
        self._diff()

    def rel(self, p: Path) -> str | None:
        try:
            rel = p.resolve().relative_to(self.root)
        except (ValueError, OSError):
            return None
        return rel.as_posix()

    def _list_files(self) -> list[str]:
        """Every file the filter lets in, pruning excluded folders.  Stops one
        past max_files (a workdir of $HOME must not walk the whole disk)."""
        rules = self._rules
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = os.path.relpath(dirpath, self.root).replace(os.sep, "/")
            prefix = "" if rel_dir == "." else rel_dir + "/"
            dirnames[:] = sorted(d for d in dirnames if not rules.dir_excluded(prefix + d))
            for f in sorted(filenames):
                rel = prefix + f
                if rules.file_excluded(rel):
                    continue
                out.append(rel)
                if len(out) > self.max_files:
                    return out
        return out

    def _diff(self) -> None:
        """Stat every project file against the index and queue what changed."""
        self._dirty = False
        t0 = time.monotonic()
        listing = self._listing
        listed = self._list_files()
        limit = self.max_files
        over = len(listed) - limit
        beyond = set(listed[limit:])
        if over > 0:
            listed = listed[:limit]
        with self._lock:
            known = {rel: (self.files[fid].mtime_ns, self.files[fid].size)
                     for rel, fid in self._by_path.items()}
            known.update((rel, (m, n)) for rel, (_, m, n) in self._skipped.items())
        changed: list[str] = []
        seen = set()
        for rel in listed:
            seen.add(rel)
            try:
                st = os.stat(self.root / rel)
            except OSError:
                if rel in known:
                    changed.append(rel)
                continue
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            if known.get(rel) != (st.st_mtime_ns, st.st_size):
                changed.append(rel)
        gone = [rel for rel in known if rel not in seen]
        changed.extend(gone)
        if over > 0:
            beyond.update(gone)     # the listing stopped at the limit: unseen, not gone
        with self._cond:
            if listing == self._listing:    # else a newer filter or limit re-lists
                # A flag, not a count: the listing stops one past the limit.
                self.skipped["limit"] = 1 if over > 0 else 0
                self._beyond = frozenset(beyond)
            for rel in changed:
                self._queue[rel] = None
            self._last_diff = time.monotonic()
            self._diff_cost = self._last_diff - t0
            if changed:
                self._cond.notify_all()

    # ── worker ───────────────────────────────────────────────────────────────

    def _run(self, load_pickle: bool) -> None:
        try:
            with self._cond:
                self.state = "building"
            self._changed(force=True)
            if self.filter_pending:
                text, note = persist.load_filter(self.root)
                with self._cond:
                    if self.filter_pending:         # set_filter() may have won meanwhile
                        self.filter_pending = False
                        self.filter_text = text
                        self._rules = ignore_rules.Rules.parse(text)
                    if note:
                        self._notices.append(note)
            if load_pickle:
                msg = self.load()
                if msg:
                    with self._lock:
                        self._notices.append(msg)
            self._diff()
            self._drain()
        except Exception as e:  # never let the indexer die silently
            with self._cond:
                self.last_error = f"{type(e).__name__}: {e}"
                self._notices.append(f"Code index stopped after an internal error: "
                                     f"{self.last_error} — /index rebuild or /index off")
                self._stop = True           # waiters get an error instead of hanging
                self._queue.clear()
                self._busy = False
                self.state = "stopped"
                self._cond.notify_all()
            self._changed(force=True)

    def _drain(self) -> None:
        while True:
            with self._cond:
                if self._rescan and not self._stop:
                    # Busy until the re-listing has queued its changes: a query
                    # in between must not answer from the cleared or old index.
                    self._rescan = False
                    self._busy = True
                    self._cond.release()
                    try:
                        self._diff()
                    finally:
                        self._cond.acquire()
                        self._busy = False
                        self._cond.notify_all()
                    continue
                while not self._queue and not self._stop and not self._rescan:
                    if self._dead:
                        self._compact()
                    if not self._built:
                        self._built = True
                    if self.state != "idle":
                        self.state = "idle"
                        self.done = self.total = 0
                        self._cond.notify_all()
                        self._changed(force=True)
                    self._cond.notify_all()
                    self._cond.wait()
                if self._stop:
                    self._cond.notify_all()
                    return
                if self._rescan:
                    continue
                if self.state == "idle":
                    self.state = "refreshing" if self._built else "building"
                    self.done = 0
                nworkers = self.worker_count()
                if nworkers > 1 and len(self._queue) >= _PARALLEL_MIN and not self._pool_broken:
                    batch = list(self._queue)
                    self._queue.clear()
                    self._busy = True
                    self.total = self.done + len(batch)
                    want = (self.components["idents"], self.components["trigrams"])
                    batch = self._take_local(batch)
                else:
                    batch = None
            if batch is not None:
                try:
                    self._drain_parallel(batch, nworkers, *want)
                finally:
                    with self._cond:
                        self._busy = False
                        self._cond.notify_all()
                self._changed()
                continue
            with self._cond:
                rel = next(iter(self._queue))
                del self._queue[rel]
                self._busy = True
                self.total = self.done + len(self._queue) + 1
                want_idents = self.components["idents"]
                want_sig = self.components["trigrams"]
                settled = self._settled(rel)
            try:
                if settled is not None:
                    entry, rows, skip = settled
                else:
                    entry, rows, skip = self._build_entry(rel, want_idents, want_sig)
            except Exception:
                entry, rows, skip = None, None, self._skip_stat(rel, "error")
            with self._cond:
                self._busy = False
                self.done += 1
                if not self._stop:
                    self._apply(rel, entry, rows, skip)
                    if len(self._dead) >= _COMPACT_AFTER:
                        self._compact()
                self._cond.notify_all()
            self._changed()

    def worker_count(self) -> int:
        return self.workers or auto_workers()

    def _take_local(self, batch: list[str]) -> list[str]:
        """Settle, under the lock, the batch files no worker needs to read
        (filtered out, or new while the budget is reached); return the rest."""
        rest = []
        for rel in batch:
            settled = self._settled(rel)
            if settled is None:
                rest.append(rel)
                continue
            self._apply(rel, *settled)
            self.done += 1
        return rest

    def _settled(self, rel: str):
        """(entry, rows, skip) for a file nobody needs to read — filtered out or
        past the file limit (as if gone), or new while the budget is reached —
        else None.  Lock held."""
        if rel in self._beyond or self._rules.excluded(rel):
            return None, None, None
        if self.partial and rel not in self._by_path:
            return None, None, self._skip_stat(rel, "budget")
        return None

    def _drain_parallel(self, rels: list[str], nworkers: int, want_idents: bool,
                        want_sig: bool) -> None:
        """Build rels in worker processes and apply each chunk's results under
        the lock as it arrives.  On stop or rescan the files not yet reached go
        back to the queue; if the pool breaks, they are built serially."""
        if not rels:
            return          # _take_local settled the whole batch: no pool to start
        chunks = [rels[i:i + _PARALLEL_CHUNK] for i in range(0, len(rels), _PARALLEL_CHUNK)]
        todo = iter(chunks)
        pending: dict = {}
        pool = None
        try:
            pool = ProcessPoolExecutor(nworkers, initializer=build._worker_init,
                                       mp_context=build._mp_context())
            while True:
                while len(pending) < nworkers * 2 and not (self._stop or self._rescan):
                    chunk = next(todo, None)
                    if chunk is None:
                        break
                    fut = pool.submit(build._build_many, str(self.root), chunk, want_idents, want_sig)
                    pending[fut] = chunk
                if not pending:
                    return
                done, _ = wait(pending, timeout=_WAIT_POLL_S, return_when=FIRST_COMPLETED)
                for fut in done:
                    chunk = pending.pop(fut)
                    try:
                        results = fut.result()
                    except BrokenProcessPool:
                        pending[fut] = chunk        # re-queued below
                        raise
                    except Exception:
                        results = [None] * len(chunk)
                    self._apply_built(chunk, results)
                self._changed()
        except (BrokenProcessPool, OSError) as e:   # a worker died, or none could start
            with self._cond:
                self._pool_broken = True
                self._notices.append(f"Code index: worker processes failed ({type(e).__name__}); "
                                     f"indexing continues in one process.")
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
            left = [rel for c in pending.values() for rel in c] + [rel for c in todo for rel in c]
            if left:
                with self._cond:
                    for rel in left:
                        self._queue.setdefault(rel, None)
                    self.total = self.done + len(self._queue)
                    self._cond.notify_all()

    def _apply_built(self, rels: list[str], results: list) -> None:
        """Apply one worker chunk; a None result is a file whose build raised."""
        with self._cond:
            for rel, res in zip(rels, results):
                if self._stop:
                    break
                self.done += 1
                settled = self._settled(rel)
                if settled is not None:
                    entry, rows, skip = settled
                elif res is None:
                    entry, rows, skip = None, None, self._skip_stat(rel, "error")
                else:
                    entry, rows, skip = res
                self._apply(rel, entry, rows, skip)
                if rel in self._queue and self._built_current(rel, entry, skip):
                    del self._queue[rel]    # a query's stat-diff saw it in flight
                if len(self._dead) >= _COMPACT_AFTER:
                    self._compact()
            self.total = max(self.total, self.done + len(self._queue))
            self._cond.notify_all()

    def _built_current(self, rel: str, entry, skip) -> bool:
        """Whether what was just applied for rel still matches the disk."""
        if entry is not None:
            built = (entry.mtime_ns, entry.size)
        elif skip is not None:
            built = skip[1:]
        else:
            built = None
        try:
            st = (self.root / rel).stat()
        except OSError:
            return built is None
        return built == (st.st_mtime_ns, st.st_size)

    def set_workers(self, n: int) -> None:
        """0 = auto.  Takes effect at the next bulk batch."""
        with self._cond:
            self.workers = max(0, min(MAX_WORKERS, int(n)))
            self._pool_broken = False
        self._changed(force=True)

    def _skip_stat(self, rel: str, reason: str):
        try:
            st = (self.root / rel).stat()
        except OSError:
            return None                 # gone: nothing to remember
        return reason, st.st_mtime_ns, st.st_size

    def _build_entry(self, rel: str, want_idents: bool, want_sig: bool):
        return build._build_entry(self.root, rel, want_idents, want_sig)

    # ── mutation (lock held) ─────────────────────────────────────────────────

    def _remove(self, rel: str) -> None:
        fid = self._by_path.pop(rel, None)
        if fid is None:
            return
        e = self.files[fid]
        self.files[fid] = None
        self._dead.add(fid)
        self._dead_names.update(e.names)
        self._dead_names.update(s.name.lower() for s in e.symbols)
        self.mem["files"] -= e.cost
        self.mem["idents"] -= e.cost_idents
        self.mem["trigrams"] -= e.cost_sig
        self.version += 1

    def _apply(self, rel: str, entry: FileEntry | None, rows, skip=None) -> None:
        self._remove(rel)
        old_skip = self._skipped.pop(rel, None)
        if skip is not None:
            self._skipped[rel] = skip
        if skip is not None or old_skip is not None:
            self._count_skipped()
        if entry is None:
            if self.partial and self.mem_used() <= self.max_bytes * _UNPARTIAL_AT:
                self._unpartial()           # files were removed: room for the left-out ones
            return
        if self._free:
            fid = self._free.pop()
            self.files[fid] = entry
        else:
            fid = len(self.files)
            self.files.append(entry)
        self._by_path[rel] = fid
        for i, s in enumerate(entry.symbols):
            self._sym_by_name[s.name.lower()].append((fid, i))
        entry.cost = _C_FILE + len(entry.symbols) * _C_SYMBOL + len(entry.imports) * _C_IMPORT
        if rows and self.components["idents"]:
            names = set()
            base = fid << _LINE_BITS
            idents = self._idents
            intern = sys.intern      # one string per identifier, shared with entry.names
            for name, line in rows:
                if len(name) < 2 or line > _LINE_MASK:
                    continue
                name = intern(name)
                arr = idents.get(name)
                if arr is None:
                    arr = idents[name] = array.array("Q")
                    self.mem["idents"] += _C_NEWNAME
                arr.append(base | line)
                names.add(name)
            entry.names = tuple(names)
            entry.cost_idents = len(rows) * _C_OCC + len(names) * _C_NAMEREF
        if entry.sig_bits and self.components["trigrams"]:
            entry.cost_sig = entry.sig_bits // 8 + _C_SIG_OVERHEAD
        else:
            entry.sig, entry.sig_bits = 0, 0
        self.mem["files"] += entry.cost
        self.mem["idents"] += entry.cost_idents
        self.mem["trigrams"] += entry.cost_sig
        self.version += 1
        self._enforce_budget()

    def _count_skipped(self) -> None:
        counts = dict.fromkeys(("binary", "too_big", "budget", "error"), 0)
        for reason, _, _ in self._skipped.values():
            counts[reason] = counts.get(reason, 0) + 1
        self.skipped.update(counts)

    def _unpartial(self) -> None:
        """Index the files the budget left out again (their stat is forgotten,
        so the next stat-diff queues them)."""
        self.partial = False
        for rel in [r for r, v in self._skipped.items() if v[0] == "budget"]:
            del self._skipped[rel]
            self._queue[rel] = None
        self._count_skipped()

    def _compact(self) -> None:
        """Drop the removed files' occurrences and symbols, then free their ids."""
        dead = self._dead
        if not dead:
            return
        for name in self._dead_names:
            arr = self._idents.get(name)
            if arr is not None:
                kept = array.array("Q", (v for v in arr if (v >> _LINE_BITS) not in dead))
                if kept:
                    self._idents[name] = kept
                else:
                    del self._idents[name]
                    self.mem["idents"] -= _C_NEWNAME
            lst = self._sym_by_name.get(name)
            if lst is not None:
                kept_l = [t for t in lst if t[0] not in dead]
                if kept_l:
                    self._sym_by_name[name] = kept_l
                else:
                    del self._sym_by_name[name]
        self._free.extend(sorted(dead, reverse=True))
        self._dead = set()
        self._dead_names = set()

    def _enforce_budget(self) -> None:
        if self.mem_used() <= self.max_bytes:
            return
        if self.components["trigrams"]:
            self.components["trigrams"] = False
            for e in self.files:
                if e is not None:
                    e.sig, e.sig_bits, e.cost_sig = 0, 0, 0
            self.mem["trigrams"] = 0
            self._notices.append(
                f"Code index over its {_fmt_size(self.max_bytes)} budget: dropped the text-search "
                f"signatures — index_text now scans files instead. Raise it with /index-max-mem.")
        if self.mem_used() <= self.max_bytes:
            return
        if self.components["idents"]:
            self.components["idents"] = False
            self._idents = {}
            for e in self.files:
                if e is not None:
                    e.names, e.cost_idents = (), 0
            self.mem["idents"] = 0
            self._notices.append(
                f"Code index over its {_fmt_size(self.max_bytes)} budget: dropped the identifier "
                f"index — index_callers now re-parses files. Raise it with /index-max-mem.")
        if self.mem_used() > self.max_bytes and not self.partial:
            self.partial = True
            self._notices.append(
                f"Code index over its {_fmt_size(self.max_bytes)} budget with symbols alone: "
                f"further files are not indexed. Raise it with /index-max-mem.")

    def set_max_bytes(self, n: int) -> None:
        with self._cond:
            old = self.max_bytes
            self.max_bytes = max(MIN_MAX_BYTES, int(n))
            if self.max_bytes < old:
                self._enforce_budget()
            elif self.degraded():
                # Re-enable everything and re-index, so the dropped parts are rebuilt.
                self.components = {"trigrams": True, "idents": True}
                self._unpartial()
                self._built_rebuild()
            self.version += 1
            self._cond.notify_all()
        self._changed(force=True)

    def set_max_files(self, n: int) -> None:
        """Change the file limit; the worker re-lists the project, indexing
        files now within it and dropping those now beyond it."""
        with self._cond:
            self.max_files = max(MIN_MAX_FILES, int(n))
            self._listing += 1
            self._rescan = True
            self._cond.notify_all()
        self._changed(force=True)

    def set_filter(self, text: str) -> None:
        """Swap the filter rules; the worker re-lists the project, dropping the
        files now excluded and indexing the ones now let in."""
        rules = ignore_rules.Rules.parse(text)
        with self._cond:
            self.filter_pending = False
            self.filter_text = text
            self._rules = rules
            self._listing += 1
            self._rescan = True
            self._cond.notify_all()
        self._changed(force=True)

    def filter_rule_count(self) -> int:
        return len(self._rules)

    def _built_rebuild(self) -> None:
        for rel in list(self._by_path):
            self._queue[rel] = None
        self._rescan = True

    def rebuild(self) -> None:
        """Forget everything and index the project again."""
        with self._cond:
            self.files, self._free = [], []
            self._by_path = {}
            self._sym_by_name = defaultdict(list)
            self._idents = {}
            self._dead, self._dead_names = set(), set()
            self._queue.clear()
            self.components = {"trigrams": True, "idents": True}
            self.partial = False
            self.skipped = {k: 0 for k in self.skipped}
            self._skipped = {}
            self.mem = {k: 0 for k in self.mem}
            self._rescan = True
            self.version += 1
            self._cond.notify_all()

    # ── the code_nav provider ────────────────────────────────────────────────

    def provide(self, key: str, stamp) -> "code_nav._Index | None":
        rel = self.rel(Path(key))
        if rel is None:
            return None
        with self._lock:
            fid = self._by_path.get(rel)
            if fid is None:
                return None
            e = self.files[fid]
            if e.lang is None or (e.mtime_ns, e.size) != tuple(stamp):
                return None
            return code_nav._Index(e.lang, e.nlines, e.symbols, e.imports, e.has_error)

    def paths_under(self, root: Path) -> "list[Path] | None":
        """The indexed source files under `root`, for code_nav's directory scans
        (find_symbol, code_outline) — the same files the index_* tools see.
        None when `root` is outside the project."""
        rel = self.rel(root)
        if rel is None:
            return None
        prefix = "" if rel == "." else rel + "/"
        with self._lock:
            return [self.root / path for path, fid in sorted(self._by_path.items())
                    if self.files[fid].lang and (not prefix or path.startswith(prefix))]

    # ── pickle ───────────────────────────────────────────────────────────────

    def save(self) -> str:
        path = pickle_path(self.root)
        header = pickle.dumps(_header(self.root), protocol=pickle.HIGHEST_PROTOCOL)
        with self._lock:
            if not self._built:
                return "Code index not saved: it is still being built."
            self._compact()
            payload = {
                "files": self.files, "free": self._free, "idents": self._idents,
                "components": dict(self.components), "partial": self.partial,
                "skipped": dict(self.skipped), "mem": dict(self.mem),
                "skipped_files": dict(self._skipped), "max_bytes": self.max_bytes,
            }
            # Serialised under the lock (a consistent snapshot, in C); the
            # slower disk write happens after queries and status polls may run.
            data = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
            del payload
        try:
            atomic_write(path, header, data, private_dir=True)
        except OSError as e:
            return f"ERROR: could not save the code index: {e}"
        return f"Code index saved: {path} ({_fmt_size(path.stat().st_size)})"

    def load(self) -> str:
        """Replace the in-memory state with the saved pickle.  The next stat-diff
        re-indexes whatever changed since it was written.  Returns a notice."""
        path = pickle_path(self.root)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return ""
        except OSError as e:
            return f"Code index file not loaded: {e}"
        why = _untrusted_reason(path, st)
        if why:
            return f"Code index file not loaded: {why}."
        try:
            with open(path, "rb") as f:
                # A fresh unpickler per pickle: one instance keeps its memo across
                # load() calls, and the payload's memo indices would then point at
                # the header's objects.
                header = _RestrictedUnpickler(f).load()
                if header != _header(self.root):
                    return ("Code index file discarded (it was written by a different version, "
                            "Python or grammar set): rebuilding.")
                payload = _RestrictedUnpickler(f).load()
        except (pickle.UnpicklingError, EOFError, AttributeError, ImportError,
                TypeError, ValueError, IndexError, KeyError) as e:
            return f"Code index file not loaded ({e}): rebuilding."
        try:
            files = payload["files"]
            if not isinstance(files, list) or not all(e is None or isinstance(e, FileEntry)
                                                       for e in files):
                raise ValueError("bad file list")
            idents = payload["idents"]
            if not isinstance(idents, dict):
                raise ValueError("bad identifier table")
        except (KeyError, TypeError, ValueError) as e:
            return f"Code index file not loaded ({e}): rebuilding."
        with self._cond:
            self.files = files
            self._free = list(payload.get("free", []))
            self._idents = idents
            self._by_path = {e.path: i for i, e in enumerate(files) if e is not None}
            self._sym_by_name = defaultdict(list)
            for fid, e in enumerate(files):
                if e is not None:
                    for i, s in enumerate(e.symbols):
                        self._sym_by_name[s.name.lower()].append((fid, i))
            self.components = dict(payload.get("components", self.components))
            self.partial = bool(payload.get("partial", False))
            self.skipped = {**self.skipped, **payload.get("skipped", {})}
            sk = payload.get("skipped_files", {})
            self._skipped = {r: v for r, v in sk.items()
                             if isinstance(r, str) and isinstance(v, tuple) and len(v) == 3} \
                if isinstance(sk, dict) else {}
            self._count_skipped()
            self.mem = {**{k: 0 for k in self.mem}, **payload.get("mem", {})}
            self._dead, self._dead_names = set(), set()
            self.version += 1
            if self.degraded() and self.max_bytes > payload.get("max_bytes", self.max_bytes):
                # Saved over a smaller budget: rebuild what it dropped.
                self.components = {"trigrams": True, "idents": True}
                self._unpartial()
                self._built_rebuild()
            self._enforce_budget()
        return f"Code index loaded from {path} ({len(self._by_path)} files)"

    # ── read helpers (lock held by callers) ──────────────────────────────────

    def _alive(self):
        for fid, e in enumerate(self.files):
            if e is not None:
                yield fid, e

    def _occurrences(self, name: str) -> dict[int, list[int]]:
        """{file id: [1-based lines]} for an identifier, skipping removed files."""
        arr = self._idents.get(name)
        out: dict[int, list[int]] = defaultdict(list)
        if arr is None:
            return out
        dead = self._dead
        for v in arr:
            fid = v >> _LINE_BITS
            if fid not in dead:
                out[fid].append(v & _LINE_MASK)
        return out

    def _ref_files(self, name: str) -> set[int]:
        arr = self._idents.get(name)
        if arr is None:
            return set()
        return {v >> _LINE_BITS for v in arr} - self._dead
