"""In-memory project index behind the index_* tools.

One ProjectIndex per working directory holds, for every text file in the
project: the symbols and imports code_nav extracts (keys, selectors, tables and
stages for config/markup files), an occurrence index of every identifier, and a
per-file trigram signature for full-text search.  A background thread builds it
and keeps it current; every index_* tool first calls wait_fresh(), so a result
is never computed from a stale index.

Memory is budgeted (/index-max-mem, default 100 MB).  Over budget the index
degrades in a fixed order — trigram signatures, then the occurrence index, then
it stops adding files — and says so in every affected result.

The index can be saved to and loaded from a pickle in ~/.momo-harness/index/.
Loading goes through a restricted unpickler that only admits this module's
classes, so a tampered file fails to load instead of running code.
"""
from __future__ import annotations

import array
import fnmatch
import hashlib
import os
import pickle
import re
import stat as stat_mod
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import code_nav

FORMAT_VERSION = 3   # 2: Symbol.doc; 3: module constants, JS/TS exports
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
MIN_MAX_BYTES = 1024 * 1024

_STAT_THROTTLE_S = 2.0      # a query within this long of the last stat-diff trusts it
_WAIT_POLL_S = 0.25         # wait_fresh re-checks the cancel flag this often
_PROGRESS_EVERY_S = 0.25    # on_change is called at most this often while building
_MAX_FILE_BYTES = 2_000_000 # same limit as grep_files
_BINARY_SNIFF_BYTES = 4096
_MAX_FILES = 100_000        # a workdir of $HOME must not index the whole disk
_COMPACT_AFTER = 500        # dead files before the occurrence index is compacted mid-drain

# Estimated bytes per stored object, measured with a deep getsizeof walk and
# tracemalloc on this repo and a 325-file C++/Python tree (imgui), then rounded
# up for dict/array over-allocation.  An estimate — /index status says so.
_C_FILE = 1300        # FileEntry, its path, lists and signature int header
_C_SYMBOL = 780       # Symbol + its strings incl. doc line (~560) and its _sym_by_name slot (~200)
_C_IMPORT = 420
_C_OCC = 9            # one packed (file, line) in an array('Q'), plus growth slack
_C_NAMEREF = 8        # one (interned) name in a file's names tuple
_C_SIG_OVERHEAD = 40
_C_NEWNAME = 160      # a distinct identifier: its key string, array header and dict slot

# Packed occurrence: file id in the high bits, 1-based line in the low 24.
_LINE_BITS = 24
_LINE_MASK = (1 << _LINE_BITS) - 1


# ── per-file record ──────────────────────────────────────────────────────────

@dataclass
class FileEntry:
    path: str                 # workdir-relative, "/"-separated
    mtime_ns: int
    size: int
    lang: str | None          # None: plain text, searchable by index_text only
    nlines: int
    has_error: bool = False
    symbols: list = field(default_factory=list)
    imports: list = field(default_factory=list)
    names: tuple = ()         # distinct identifiers, so the file can be removed from idents
    sig: int = 0              # trigram signature bitmap (0 when trigrams are off)
    sig_bits: int = 0
    cost: int = 0             # estimated bytes: base + symbols + imports
    cost_idents: int = 0
    cost_sig: int = 0


# ── trigram signatures ───────────────────────────────────────────────────────
# Only trigrams inside [a-z0-9_] runs are indexed.  Every such run in a query
# is a substring of a run in any file that matches, so the filter never drops
# a real hit; punctuation-only queries fall back to scanning.

_WORD = re.compile(rb"[a-z0-9_]{3,}")
_WORD_STR = re.compile(r"[a-z0-9_]{3,}")
_TRI_SPACE = 37 ** 3
_CODE = [36] * 256
for _i, _c in enumerate(b"abcdefghijklmnopqrstuvwxyz0123456789"):
    _CODE[_c] = _i


def _trigram_ids(words) -> set[int]:
    ids: set[int] = set()
    C = _CODE
    for w in words:
        if isinstance(w, str):
            w = w.encode("ascii", "ignore")
        for i in range(len(w) - 2):
            ids.add((C[w[i]] * 37 + C[w[i + 1]]) * 37 + C[w[i + 2]])
    return ids


def _signature(raw_lower: bytes) -> tuple[int, int]:
    """(bitmap, bits) — about 4 bits per distinct trigram, exact once the
    bitmap covers the whole trigram space."""
    ids = _trigram_ids(set(_WORD.findall(raw_lower)))
    if not ids:
        return 0, 0
    m = min(_TRI_SPACE, max(512, 4 * len(ids)))
    ba = bytearray((m + 7) // 8)
    for t in ids:
        h = t % m
        ba[h >> 3] |= 1 << (h & 7)
    return int.from_bytes(ba, "little"), m


def _mask(ids: set[int], m: int) -> int:
    out = 0
    for t in ids:
        out |= 1 << (t % m)
    return out


_UNSAFE_REGEX = re.compile(r"\)[?*{]")


def _regex_literals(pattern: str) -> list[str]:
    """Lowercased [a-z0-9_] runs every match of `pattern` must contain, or []
    when that cannot be decided cheaply (alternation, optional groups)."""
    if "|" in pattern or _UNSAFE_REGEX.search(pattern):
        return []
    s = re.sub(r"\\x[0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}|\\N\{[^}]*\}|\\[0-7]{1,3}", " ", pattern)
    s = re.sub(r"\[(?:\\.|[^\]])*\]", " ", s)
    s = re.sub(r"\\.", " ", s)
    out = []
    for m in re.finditer(r"[A-Za-z0-9_]+", s):
        run = m.group()
        if s[m.end():m.end() + 1] in ("?", "*", "{"):
            run = run[:-1]           # the last character is optional
        if len(run) >= 3:
            out.append(run.lower())
    return out


# ── name tokens (fuzzy search) ───────────────────────────────────────────────

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t


@lru_cache(maxsize=65536)
def _tokens(name: str) -> tuple[str, ...]:
    """'parseConfigFile' / 'parse_config_file' / 'Parser.parse' -> lowercase words,
    plural 's' dropped so 'bytes' meets 'byte'."""
    out = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        out.extend(_stem(t.lower()) for t in _CAMEL.findall(part))
    return tuple(out)


# Words a paraphrased question carries that never name anything.
_STOPWORDS = frozenset("""a an the to of in into for and or is it its that this from by on with as
    like what where which how does do get gets turn turns function functions method methods class
    defined define code thing""".split())


def _query_tokens(q: str) -> tuple[str, ...]:
    toks = _tokens(q)
    kept = tuple(t for t in toks if t not in _STOPWORDS)
    return kept or toks


# ── restricted pickle ────────────────────────────────────────────────────────

_ALLOWED_GLOBALS = {
    ("harness.code_index", "FileEntry"),
    ("harness.code_nav", "Symbol"),
    ("harness.code_nav", "Import"),
    ("array", "array"),
    ("array", "_array_reconstructor"),
    ("builtins", "set"),
    ("builtins", "frozenset"),
}


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) in _ALLOWED_GLOBALS:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"{module}.{name} is not allowed in an index file")


def index_dir() -> Path:
    return Path.home() / ".momo-harness" / "index"


def pickle_path(root: Path) -> Path:
    digest = hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:16]
    return index_dir() / f"{digest}.pickle"


def _grammar_versions() -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version
    names = {"tree-sitter"} | {f"tree-sitter-{'typescript' if g == 'tsx' else g}"
                               for g in set(code_nav._EXTENSIONS.values()) | {"dockerfile"}}
    out = {}
    for n in sorted(names):
        try:
            out[n] = version(n)
        except PackageNotFoundError:
            out[n] = ""
    return out


def _header(root: Path) -> dict:
    return {"format": FORMAT_VERSION, "root": str(root), "python": list(sys.version_info[:2]),
            "grammars": _grammar_versions()}


def _fmt_size(n: int) -> str:
    from .net import format_size
    return format_size(max(0, int(n)))


# ── the index ────────────────────────────────────────────────────────────────

class ProjectIndex:
    """See the module docstring.  All public methods are thread-safe."""

    def __init__(self, root: Path, max_bytes: int = DEFAULT_MAX_BYTES, on_change=None):
        self.root = root.resolve()
        self.max_bytes = max(MIN_MAX_BYTES, int(max_bytes))
        self.on_change = on_change              # callable(index), throttled
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
        self._busy = False                      # the worker holds an item outside the lock
        self._built = False
        self._stop = False
        self._dirty = True                      # force a stat-diff at the next query
        self._rescan = False                    # the worker must re-list the project first
        self._last_diff = 0.0
        self._last_progress = 0.0
        self.state = "idle"                     # idle | building | refreshing | stopped
        self.done = 0
        self.total = 0
        self.components = {"trigrams": True, "idents": True}
        self.partial = False                    # symbols alone exceeded the budget
        self.mem = {"files": 0, "idents": 0, "trigrams": 0}
        self.skipped = {"binary": 0, "too_big": 0, "budget": 0, "limit": 0}
        self.version = 0
        self._notices: list[str] = []
        self._rank_cache: tuple | None = None
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

    def degraded(self) -> bool:
        return self.partial or not all(self.components.values())

    def is_fresh(self) -> bool:
        with self._lock:
            return self._built and not self._queue and not self._busy

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
                rel = self._rel(Path(p) if os.path.isabs(str(p)) else self.root / str(p))
                if rel is not None:
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
        if not self._dirty and time.monotonic() - self._last_diff < _STAT_THROTTLE_S:
            return
        self._diff()

    def _rel(self, p: Path) -> str | None:
        try:
            rel = p.resolve().relative_to(self.root)
        except (ValueError, OSError):
            return None
        return rel.as_posix()

    def _list_files(self) -> list[str]:
        from .tools import _SKIP_DIRS
        out: list[str] = []
        try:
            r = subprocess.run(["git", "-C", str(self.root), "ls-files", "-co",
                                "--exclude-standard", "-z"],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                for rel in r.stdout.decode("utf-8", "replace").split("\0"):
                    if rel and not any(part in _SKIP_DIRS for part in rel.split("/")[:-1]):
                        out.append(rel)
                return sorted(set(out))
        except (OSError, subprocess.SubprocessError):
            pass
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in _SKIP_DIRS and not d.startswith("."))
            rel_dir = os.path.relpath(dirpath, self.root)
            for f in sorted(filenames):
                if f.startswith("."):
                    continue
                out.append(f if rel_dir == "." else f"{rel_dir}/{f}".replace(os.sep, "/"))
                if len(out) > _MAX_FILES:
                    return out
        return out

    def _diff(self) -> None:
        """Stat every project file against the index and queue what changed."""
        self._dirty = False
        listed = self._list_files()
        over = len(listed) - _MAX_FILES
        if over > 0:
            listed = listed[:_MAX_FILES]
        with self._lock:
            known = {rel: (self.files[fid].mtime_ns, self.files[fid].size)
                     for rel, fid in self._by_path.items()}
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
        changed.extend(rel for rel in known if rel not in seen)
        with self._cond:
            self.skipped["limit"] = max(0, over)
            for rel in changed:
                self._queue[rel] = None
            self._last_diff = time.monotonic()
            if changed:
                self._cond.notify_all()

    # ── worker ───────────────────────────────────────────────────────────────

    def _run(self, load_pickle: bool) -> None:
        try:
            with self._cond:
                self.state = "building"
            self._changed(force=True)
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
                    self._rescan = False
                    self._cond.release()
                    try:
                        self._diff()
                    finally:
                        self._cond.acquire()
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
                rel = next(iter(self._queue))
                del self._queue[rel]
                self._busy = True
                self.total = self.done + len(self._queue) + 1
                want_idents = self.components["idents"]
                want_sig = self.components["trigrams"]
                known = rel in self._by_path
                blocked = self.partial and not known
            try:
                entry, rows = (None, None) if blocked else self._build_entry(rel, want_idents, want_sig)
            except Exception:
                entry, rows = None, None
            with self._cond:
                self._busy = False
                self.done += 1
                if blocked:
                    self.skipped["budget"] += 1
                elif not self._stop:
                    self._apply(rel, entry, rows)
                    if len(self._dead) >= _COMPACT_AFTER:
                        self._compact()
                self._cond.notify_all()
            self._changed()

    def _build_entry(self, rel: str, want_idents: bool, want_sig: bool):
        """Read and index one file outside the lock.  (None, None) = remove it."""
        full = self.root / rel
        try:
            st = full.stat()
        except OSError:
            return None, None
        if not stat_mod.S_ISREG(st.st_mode):
            return None, None
        if st.st_size > _MAX_FILE_BYTES:
            with self._lock:
                self.skipped["too_big"] += 1
            return None, None
        try:
            raw = full.read_bytes()
        except OSError:
            return None, None
        if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
            with self._lock:
                self.skipped["binary"] += 1
            return None, None
        lang = code_nav.language_for(full)
        symbols: list = []
        imports: list = []
        has_error = False
        rows = None
        nlines = raw.count(b"\n") + (0 if raw.endswith(b"\n") or not raw else 1)
        if lang is not None:
            try:
                parsed = code_nav.parse_uncached(full, raw)
            except Exception:
                parsed = None
                has_error = True
            if parsed is not None:
                symbols, imports = parsed.symbols, parsed.imports
                has_error = parsed.tree.root_node.has_error
                nlines = len(parsed.lines)
                if want_idents:
                    rows = code_nav.identifier_rows(parsed)
                del parsed
        entry = FileEntry(path=rel, mtime_ns=st.st_mtime_ns, size=st.st_size, lang=lang,
                          nlines=nlines, has_error=has_error, symbols=symbols, imports=imports)
        if want_sig:
            entry.sig, entry.sig_bits = _signature(raw.lower())
        return entry, rows

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

    def _apply(self, rel: str, entry: FileEntry | None, rows) -> None:
        self._remove(rel)
        if entry is None:
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
                self.partial = False
                self.skipped["budget"] = 0
                self._built_rebuild()
            self.version += 1
            self._cond.notify_all()
        self._changed(force=True)

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
            self.mem = {k: 0 for k in self.mem}
            self._rescan = True
            self.version += 1
            self._cond.notify_all()

    # ── the code_nav provider ────────────────────────────────────────────────

    def provide(self, key: str, stamp) -> "code_nav._Index | None":
        rel = self._rel(Path(key))
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

    # ── pickle ───────────────────────────────────────────────────────────────

    def save(self) -> str:
        path = pickle_path(self.root)
        with self._lock:
            if not self._built:
                return "Code index not saved: it is still being built."
            self._compact()
            payload = {
                "files": self.files, "free": self._free, "idents": self._idents,
                "components": dict(self.components), "partial": self.partial,
                "skipped": dict(self.skipped), "mem": dict(self.mem),
            }
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                os.chmod(path.parent, 0o700)
                tmp = path.with_suffix(f".tmp{os.getpid()}")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    pickle.dump(_header(self.root), f, protocol=pickle.HIGHEST_PROTOCOL)
                    pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp, path)
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
        if not stat_mod.S_ISREG(st.st_mode):
            return f"Code index file not loaded: {path} is not a regular file."
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return f"Code index file not loaded: {path} is not owned by you."
        if st.st_mode & 0o022:
            return (f"Code index file not loaded: {path} is writable by other users "
                    f"(chmod 600 it, or delete it).")
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
            self.mem = {**{k: 0 for k in self.mem}, **payload.get("mem", {})}
            self._dead, self._dead_names = set(), set()
            self.version += 1
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


# ── tool helpers ─────────────────────────────────────────────────────────────

_CODE_KIND_ORDER = {"class": 0, "interface": 0, "struct": 0, "trait": 0, "enum": 1,
                    "function": 1, "method": 2, "table": 2, "stage": 2}
_DATA_LANGS = set(code_nav._DATA_EXTRACTORS)


def _path_filter(path: str | None):
    """Predicate for a workdir-relative path: a directory/file prefix or a glob."""
    if not path or path in (".", "./"):
        return lambda rel: True
    p = path.strip()
    if p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    if code_nav._is_pattern(p):
        return lambda rel: fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*")
    return lambda rel: rel == p or rel.startswith(p + "/")


def _degraded_note(index: ProjectIndex, component: str | None = None) -> str:
    parts = []
    if index.partial:
        parts.append(f"the index covers only part of the project (memory budget "
                     f"{_fmt_size(index.max_bytes)}; {index.skipped['budget']} files left out)")
    if component and not index.components.get(component, True):
        parts.append({"trigrams": "text search is scanning files (signatures dropped for memory)",
                      "idents": "the identifier index was dropped for memory, so files were re-parsed"
                      }[component])
    if index.skipped.get("limit"):
        parts.append(f"{index.skipped['limit']} files beyond the {_MAX_FILES:,}-file limit are not indexed")
    return f"(note: {'; '.join(parts)})" if parts else ""


def _with_note(text: str, index: ProjectIndex, component: str | None = None) -> str:
    note = _degraded_note(index, component)
    return text + (f"\n{note}" if note else "")


def _ready(index, cancel) -> str | None:
    if index is None:
        return "ERROR: the code index is off (the user can turn it on with /index on)"
    return index.wait_fresh(cancel)


def _score(s, q: str, ql: str, qtoks: tuple[str, ...], pattern: bool) -> int:
    if pattern:
        return 100 if code_nav._matches(s, q) else 0
    name, qual = s.name, s.qualname
    if name == q or qual == q:
        return 100
    nl, qualL = name.lower(), qual.lower()
    if nl == ql or qualL == ql:
        return 95
    if "." in ql and qualL.endswith("." + ql):
        return 90
    if nl.startswith(ql):
        return 75 - min(10, len(nl) - len(ql))
    if len(ql) >= 3 and ql in nl:
        return 65
    if len(ql) >= 3 and ql in qualL:
        return 60
    if qtoks:
        # Words in any order, matched against the name and against the first
        # line of the docstring: 'size string to bytes' finds parse_size through
        # "Bytes from a plain number or a unit-suffixed string".
        nf = _overlap(qtoks, _tokens(qual))
        df = _overlap(qtoks, _tokens(s.doc)) if getattr(s, "doc", "") else 0.0
        if nf >= 0.99:
            name_sc = 55
        elif nf >= 0.5 and len(qtoks) > 1:
            name_sc = int(30 + 20 * nf)
        elif nf > 0:
            name_sc = int(12 + 16 * nf)
        else:
            name_sc = 0
        if df >= 0.99:
            doc_sc = 45
        elif df >= 0.5:
            doc_sc = int(20 + 20 * df)
        else:
            doc_sc = 0
        if name_sc or doc_sc:
            return min(58, max(name_sc, doc_sc) + (6 if name_sc and doc_sc else 0))
    if len(ql) >= 3 and " " not in ql:
        it = iter(nl)
        if all(c in it for c in ql.replace("_", "")):
            return 20
        if ql in s.signature.lower():
            return 15
    return 0


def _overlap(qtoks: tuple[str, ...], stoks: tuple[str, ...]) -> float:
    """Fraction of query words found among a name's (or doc line's) words; a
    prefix either way ('conf' / 'config') counts three quarters."""
    if not stoks:
        return 0.0
    hit = 0.0
    for t in qtoks:
        if t in stoks:
            hit += 1
        elif len(t) >= 3 and any(st.startswith(t) or (len(st) >= 3 and t.startswith(st))
                                 for st in stoks):
            hit += 0.75
    return hit / len(qtoks)


_PATHLIKE = re.compile(r"[/\\]|\.[A-Za-z0-9]{1,6}$")


def _file_score(rel: str, q: str) -> int:
    ql, rl = q.lower().lstrip("./"), rel.lower()
    base = rl.rsplit("/", 1)[-1]
    if rl == ql:
        return 100
    if rl.endswith("/" + ql):
        return 95
    if base == ql or base.rsplit(".", 1)[0] == ql:
        return 90
    if len(ql) >= 3 and ql in rl:
        return 60
    return 0


# ── executors ────────────────────────────────────────────────────────────────

_MAX_SEARCH = 30
_MAX_TEXT_HITS = 200
_MAX_TEXT_LINE = 200
_MAX_CALLER_GROUPS = 25     # places listed at level 1
_MAX_LINES_PER_GROUP = 3
_MAX_EXPAND = 8             # level-1 definitions followed to level 2 (and so on)
_MAX_EXPAND_GROUPS = 5      # places listed per followed definition
_MAX_CALLERS_CHARS = 9000   # the whole answer; a small model's context is 16k tokens
_MAX_EXPAND_FILES = 300     # a name used in more files than this is too common to expand


def index_search(query: str, kind: str | None = None, path: str | None = None,
                 lang: str | None = None, *, workdir: Path, index: ProjectIndex | None = None,
                 cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    q = (query or "").strip().replace("::", ".")
    if not q:
        return "ERROR: query is empty — pass a name or part of one, e.g. 'parse_config'"
    line = code_nav.line_query(q)
    if line is not None:
        return _line_owner(index, line, path, workdir)
    if " " in q and not code_nav._is_pattern(q) and len(_tokens(q)) == 1:
        q = q.replace(" ", "")
    ql = q.lower()
    qtoks = _query_tokens(q)
    pattern = code_nav._is_pattern(q)
    keep = _path_filter(path)
    want_files = kind == "file" or (kind is None and bool(_PATHLIKE.search(q)))
    hits = []
    file_hits = []
    with index._lock:
        for fid, e in index._alive():
            if (lang and e.lang != lang) or not keep(e.path):
                continue
            if want_files:
                fs = _file_score(e.path, q)
                if fs:
                    file_hits.append((-fs, len(e.path), e.path, e))
            if kind == "file":
                continue
            for s in e.symbols:
                if kind and s.kind != kind:
                    continue
                sc = _score(s, q, ql, qtoks, pattern)
                if sc:
                    hits.append((-sc, _CODE_KIND_ORDER.get(s.kind, 3), len(s.qualname),
                                 e.path, s.start, s, e.lang))
        total_files = len(index._by_path)
    file_hits.sort(key=lambda h: h[:3])
    if file_hits and (not hits or -file_hits[0][0] >= -min(hits)[0]):
        # The query names a file: say so, and point at the tool that describes one.
        lines = [f"{h[2]}  (file, {h[3].lang or 'text'}, {h[3].nlines} lines, "
                 f"{len(h[3].symbols)} definitions)" for h in file_hits[:_MAX_SEARCH]]
        head = f"{len(file_hits)} file{'s' if len(file_hits) != 1 else ''} matching '{query}':"
        more = [f"... ({_MAX_SEARCH} of {len(file_hits)})"] if len(file_hits) > _MAX_SEARCH else []
        return _with_note("\n".join([head] + lines + more + [
            f'(next: index_file("{file_hits[0][2]}") shows its outline, imports and the files '
            f'that import it)']), index)
    if not hits:
        return _with_note(
            f"(no definition, key, selector or table matches '{query}' in {total_files} indexed "
            f"files — index_text('{query}') searches the file contents instead)", index)
    hits.sort(key=lambda h: h[:5])
    out = []
    exact_block = hits[0][0] <= -90
    shown_sep = False
    for h in hits[:_MAX_SEARCH]:
        sc, s, rel = -h[0], h[5], h[3]
        if exact_block and sc < 90 and not shown_sep:
            out.append("— approximate matches —")
            shown_sep = True
        out.append(f"{rel}:L{s.start}-{s.end}  {s.kind} {s.qualname}  | {s.signature}")
    head = f"{len(hits)} match{'es' if len(hits) != 1 else ''} for '{query}'"
    if not exact_block:
        head += " (no exact name match — closest first)"
    trailer = []
    if len(hits) > _MAX_SEARCH:
        trailer.append(f"... ({_MAX_SEARCH} of {len(hits)} — narrow with kind=, lang= or path=)")
    best = hits[0][5]
    trailer.append(f'(next: read_symbol("{hits[0][3]}", "{best.qualname}") reads the best match'
                   + (f"; index_callers(\"{best.name}\") shows who uses it)"
                      if hits[0][6] not in _DATA_LANGS else ")"))
    return _with_note("\n".join([head + ":"] + out + trailer), index)


def _line_owner(index: ProjectIndex, line: int, path: str | None, workdir: Path) -> str:
    """'Which definition is line N of this file in?' — one line of output."""
    from .tools import _safe_path
    if not path:
        return (f"ERROR: looking up line {line} needs the file it is in — pass it as path, "
                f"e.g. index_search(\"{line}\", path=\"src/app.py\")")
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    rel = index._rel(p)
    with index._lock:
        fid = index._by_path.get(rel) if rel else None
        e = index.files[fid] if fid is not None else None
    if e is None:
        return (f"ERROR: {path} is not an indexed file" + (" — pass the file, not a directory"
                                                          if p.is_dir() else ""))
    if not 1 <= line <= max(e.nlines, 1):
        return f"ERROR: line {line} is outside {path} (1-{e.nlines})"
    owner = code_nav._enclosing(e.symbols, line)
    if owner is None:
        return f"(line {line} of {e.path} is not inside any definition — it is at module level)"
    return (f"{e.path}:L{owner.start}-{owner.end}  {owner.kind} {owner.qualname}  | {owner.signature}\n"
            f'(next: read_symbol("{e.path}", "{owner.qualname}") reads it)')


def index_text(query: str, path: str | None = None, regex: bool = False, *, workdir: Path,
               index: ProjectIndex | None = None, cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    if not query:
        return "ERROR: query is empty"
    if regex:
        try:
            rx = re.compile(query)
        except re.error as e:
            return f"ERROR: invalid regex: {e}"
        lits = _regex_literals(query)
    else:
        rx = re.compile(re.escape(query), re.IGNORECASE)
        lits = _WORD_STR.findall(query.lower())
    ids = _trigram_ids(lits)
    keep = _path_filter(path)
    with index._lock:
        use_sig = bool(ids) and index.components["trigrams"]
        cands = []
        masks: dict[int, int] = {}
        for fid, e in index._alive():
            if not keep(e.path):
                continue
            if use_sig and e.sig_bits:
                m = masks.get(e.sig_bits)
                if m is None:
                    m = masks[e.sig_bits] = _mask(ids, e.sig_bits)
                if e.sig & m != m:
                    continue
            elif use_sig and not e.sig_bits:
                continue     # no [a-z0-9_] trigram at all in this file
            cands.append((e.path, e.symbols))
        scanned_all = not use_sig
    hits: list[tuple[str, int, str, object]] = []
    total = 0
    files_hit: dict[str, int] = {}
    for i, (rel, symbols) in enumerate(cands):
        if cancel is not None and i % 50 == 0 and cancel.is_set():
            return "ERROR: cancelled"
        try:
            text = (index.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for ln, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                total += 1
                files_hit[rel] = files_hit.get(rel, 0) + 1
                if len(hits) < _MAX_TEXT_HITS:
                    hits.append((rel, ln, line, symbols))
    if not hits:
        how = "every indexed file was scanned" if scanned_all else \
            f"{len(cands)} candidate files checked"
        return _with_note(f"(no matches for {query!r} — {how}; index_search finds definitions "
                          f"by approximate name)", index, "trigrams")
    out = [f"{total} match{'es' if total != 1 else ''} in {len(files_hit)} file"
           f"{'s' if len(files_hit) != 1 else ''} for {query!r}:"]
    cur = None
    for rel, ln, line, symbols in hits:
        if rel != cur:
            out.append(f"{rel} ({files_hit[rel]})")
            cur = rel
        owner = code_nav._enclosing(symbols, ln) if symbols else None
        where = f" [in {owner.qualname}]" if owner else ""
        text = line.strip()
        if len(text) > _MAX_TEXT_LINE:
            text = text[:_MAX_TEXT_LINE] + " …"
        out.append(f"  L{ln}{where}: {text}")
    if total > len(hits):
        out.append(f"... (first {len(hits)} of {total} matches — narrow with path= or a longer query)")
    if not ids:
        out.append("(note: the query has no 3-letter word, so every indexed file was scanned)")
    first = hits[0]
    out.append(f'(next: read_symbol("{first[0]}", "L{first[1]}") reads the definition around a hit)')
    return _with_note("\n".join(out), index, "trigrams")


def _uses(index: ProjectIndex, bare: str, want_recv: str | None, cancel) -> tuple[list, list]:
    """All uses of `bare`: ([(rel, row1, role, recv, owner, line_text)], defs [(rel, Symbol)])."""
    with index._lock:
        if index.components["idents"]:
            occ = index._occurrences(bare)
            targets = [(index.files[fid].path, set(lines)) for fid, lines in occ.items()
                       if index.files[fid] is not None and index.files[fid].lang]
        else:
            targets = [(e.path, None) for _, e in index._alive()
                       if e.lang and e.lang not in _DATA_LANGS]
    uses, defs = [], []
    for i, (rel, rows) in enumerate(sorted(targets, key=lambda t: t[0])):
        if cancel is not None and i % 20 == 0 and cancel.is_set():
            break
        try:
            parsed = code_nav.parse(index.root / rel)
        except (OSError, ImportError):
            continue
        if parsed is None:
            continue
        found = code_nav.references_in(parsed, bare, {r - 1 for r in rows} if rows else None)
        for r in sorted(found):
            role, recv = found[r]
            if role == "def":
                sym = next((s for s in parsed.symbols if s.name == bare and s.name_line == r + 1), None)
                if sym is not None:
                    defs.append((rel, sym))
                continue
            if want_recv and recv != want_recv:
                continue
            owner = code_nav._enclosing(parsed.symbols, r + 1)
            line = parsed.lines[r].strip() if r < len(parsed.lines) else ""
            uses.append((rel, r + 1, role, recv, owner, line))
    return uses, defs


def _group(uses) -> "dict[tuple[str, str | None], list]":
    groups: dict = {}
    for u in uses:
        key = (u[0], u[4].qualname if u[4] else None)
        groups.setdefault(key, []).append(u)
    return groups


def _fmt_group(key, items, indent: str, max_lines: int = _MAX_LINES_PER_GROUP) -> list[str]:
    rel, qual = key
    owner = items[0][4]
    head = (f"{indent}{rel}:L{owner.start}-{owner.end}  {owner.kind} {owner.qualname}"
            if owner else f"{indent}{rel} (module level)")
    out = [head]
    for u in items[:max_lines]:
        tag = u[2] + (f", recv {u[3]}" if u[3] else "")
        text = u[5] if len(u[5]) <= 120 else u[5][:120] + " …"
        out.append(f"{indent}    L{u[1]} ({tag}) {text}")
    if len(items) > max_lines:
        out.append(f"{indent}    ... {len(items) - max_lines} more in this definition")
    return out


def index_callers(name: str, depth: int = 1, role: str | None = None, *, workdir: Path,
                  index: ProjectIndex | None = None, cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    try:
        depth = max(1, min(3, int(depth)))
    except (TypeError, ValueError):
        depth = 1
    if role and role not in code_nav.REFERENCE_ROLES:
        return f"ERROR: unknown role '{role}' (use one of: {', '.join(code_nav.REFERENCE_ROLES)})"
    dotted = (name or "").strip().replace("::", ".")
    if not dotted:
        return "ERROR: name is empty"
    bare = dotted.rsplit(".", 1)[-1]
    qualifier = dotted.rsplit(".", 1)[0] if "." in dotted else None
    want_recv = None
    if qualifier:
        # 'Harness.send' names a method: its callers call it on self/harness/...,
        # so the class is not a receiver filter.  'JSON.parse' is.
        with index._lock:
            is_container = any(index.files[fid] is not None
                               and index.files[fid].symbols[i].kind in code_nav._CONTAINER_KINDS
                               for fid, i in index._sym_by_name.get(qualifier.rsplit(".", 1)[-1].lower(), []))
        if not is_container:
            want_recv = qualifier
    uses, defs = _uses(index, bare, want_recv, cancel)
    if cancel is not None and cancel.is_set():
        return "ERROR: cancelled"
    if qualifier and want_recv is None:
        defs = [d for d in defs if d[1].qualname == dotted or d[1].qualname.endswith("." + dotted)] or defs
    if role:
        uses = [u for u in uses if u[2] == role]
    out = []
    if defs:
        where = "; ".join(f"{rel}:L{s.start}-{s.end} ({s.kind} {s.qualname})" for rel, s in defs[:5])
        out.append(f"'{dotted}' is defined at {where}" + (f" and {len(defs) - 5} more" if len(defs) > 5 else "") + ".")
    else:
        out.append(f"'{dotted}' has no definition in the indexed files (external, dynamic or builtin).")
    if not uses:
        out.append(f"No {role + ' ' if role else ''}uses found"
                   + (f" on receiver '{want_recv}'" if want_recv else "") + ".")
        return _with_note("\n".join(out), index, "idents")
    counts: dict[str, int] = {}
    for u in uses:
        counts[u[2]] = counts.get(u[2], 0) + 1
    groups = _group(uses)
    split = ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    out.append(f"Level 1 — {len(uses)} use{'s' if len(uses) != 1 else ''} in {len(groups)} "
               f"place{'s' if len(groups) != 1 else ''} ({split}):")
    for key in list(groups)[:_MAX_CALLER_GROUPS]:
        out.extend(_fmt_group(key, groups[key], "  "))
    if len(groups) > _MAX_CALLER_GROUPS:
        out.append(f"  ... {len(groups) - _MAX_CALLER_GROUPS} more places")
    impact_defs = {(k[0], k[1]) for k in groups if k[1]}
    impact_files = {k[0] for k in groups}
    frontier = [(items[0][4], key) for key, items in groups.items()
                if items[0][4] is not None and any(u[2] != "import" for u in items)]
    visited = {dotted} | {o.qualname for o, _ in frontier}
    for level in range(2, depth + 1):
        if not frontier:
            break
        lines, nxt = [], []
        if len(frontier) > _MAX_EXPAND:
            lines.append(f"  (following the first {_MAX_EXPAND} of {len(frontier)} definitions — "
                         f"call index_callers on one of the others to follow it)")
        for owner, _key in frontier[:_MAX_EXPAND]:
            if cancel is not None and cancel.is_set():
                return "ERROR: cancelled"
            with index._lock:
                common = (index.components["idents"]
                          and len(index._ref_files(owner.name)) > _MAX_EXPAND_FILES)
            if common or owner.name == bare:
                lines.append(f"  via {owner.qualname}: (name too common to follow)")
                continue
            u2, _ = _uses(index, owner.name, None, cancel)
            u2 = [u for u in u2 if u[2] in ("call", "other", "type")]
            if not u2:
                continue
            g2 = _group(u2)
            lines.append(f"  via {owner.qualname} ← {len(u2)} use{'s' if len(u2) != 1 else ''}:")
            for key in list(g2)[:_MAX_EXPAND_GROUPS]:
                lines.extend(_fmt_group(key, g2[key], "    ", 1))
                impact_files.add(key[0])
                if key[1]:
                    impact_defs.add(key)
                o2 = g2[key][0][4]
                if o2 is not None and o2.qualname not in visited:
                    visited.add(o2.qualname)
                    nxt.append((o2, key))
            for key in list(g2)[_MAX_EXPAND_GROUPS:]:
                impact_files.add(key[0])
                if key[1]:
                    impact_defs.add(key)
            if len(g2) > _MAX_EXPAND_GROUPS:
                lines.append(f"    ... {len(g2) - _MAX_EXPAND_GROUPS} more places")
        if lines:
            out.append(f"Level {level} — who uses those definitions (matched by name, not resolved):")
            out.extend(lines)
        frontier = nxt
    out.append(f"Impact: {len(impact_defs)} definition{'s' if len(impact_defs) != 1 else ''} "
               f"in {len(impact_files)} file{'s' if len(impact_files) != 1 else ''}"
               + (f" over {depth} levels." if depth > 1 else "."))
    first = next(iter(groups.values()))[0]
    hint = f'(next: read_symbol("{first[0]}", "L{first[1]}") reads a caller whole'
    if depth == 1 and frontier:
        hint += "; depth=2 shows who calls those"
    tail = [out.pop(), hint + ")"]     # the Impact line and the hint always survive
    text, size = [], sum(len(t) + 1 for t in tail)
    for line in out:
        if size + len(line) + 1 > _MAX_CALLERS_CHARS:
            text.append(f"... (output capped at {_MAX_CALLERS_CHARS} chars — narrow with "
                        f"role='call', a qualified name, or depth=1)")
            break
        text.append(line)
        size += len(line) + 1
    return _with_note("\n".join(text + tail), index, "idents")


def _ranks(index: ProjectIndex):
    """(file rank {fid: score}, used-by {fid: n files}, symbol use {(fid, i): n files}),
    cached per index version."""
    cached = index._rank_cache
    if cached is not None and cached[0] == index.version:
        return cached[1]
    alive = [fid for fid, _ in index._alive()]
    edges: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    used_by: dict[int, set] = defaultdict(set)
    sym_use: dict[tuple[int, int], int] = {}
    if index.components["idents"]:
        defs: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for fid in alive:
            e = index.files[fid]
            if e.lang in _DATA_LANGS:
                continue
            for i, s in enumerate(e.symbols):
                if s.depth <= 1 and len(s.name) >= 3:
                    defs[s.name].append((fid, i))
        for name, where in defs.items():
            def_fids = {fid for fid, _ in where}
            if len(def_fids) > 3:
                continue        # too generic to say who uses which
            refs = index._ref_files(name) - def_fids
            if not refs:
                continue
            w = 1.0 / len(def_fids)
            for fid, i in where:
                sym_use[(fid, i)] = len(refs)
            for r in refs:
                for d in def_fids:
                    edges[r][d] += w
                    used_by[d].add(r)
    n = len(alive) or 1
    rank = {fid: 1.0 / n for fid in alive}
    for _ in range(20):
        nxt = {fid: 0.15 / n for fid in alive}
        dangling = 0.0
        for fid in alive:
            out = edges.get(fid)
            if not out:
                dangling += rank[fid]
                continue
            tot = sum(out.values())
            for d, w in out.items():
                if d in nxt:
                    nxt[d] += 0.85 * rank[fid] * w / tot
        share = 0.85 * dangling / n
        for fid in nxt:
            nxt[fid] += share
        rank = nxt
    result = (rank, {k: len(v) for k, v in used_by.items()}, sym_use)
    index._rank_cache = (index.version, result)
    return result


def index_map(path: str | None = None, budget: int = 1500, *, workdir: Path,
              index: ProjectIndex | None = None, cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    try:
        budget = max(300, min(8000, int(budget)))
    except (TypeError, ValueError):
        budget = 1500
    keep = _path_filter(path)
    with index._lock:
        rank, used_by, sym_use = _ranks(index)
        entries = [(fid, e) for fid, e in index._alive() if keep(e.path)]
        entries.sort(key=lambda fe: (-rank.get(fe[0], 0), -used_by.get(fe[0], 0), fe[1].path))
        limit = budget * 4
        out = []
        head = (f"Project map{f' of {path}' if path and path not in ('.', './') else ''} — "
                f"{len(entries)} files, most-used first (used by = files that reference its "
                f"definitions):")
        size = len(head)
        shown = 0
        for fid, e in entries:
            lang = e.lang or "text"
            ub = used_by.get(fid, 0)
            line = f"{e.path} ({lang}, {e.nlines} lines)" + (f" — used by {ub} files" if ub else "")
            block = [line]
            if e.lang in _DATA_LANGS:
                tops = [s.name for s in e.symbols if s.depth == 0][:8]
                if tops:
                    block.append("  " + ", ".join(tops))
            elif e.symbols:
                tops = [(sym_use.get((fid, i), 0), i, s) for i, s in enumerate(e.symbols) if s.depth <= 1]
                tops.sort(key=lambda t: (-t[0], t[1]))
                for n_use, _i, s in tops[:5]:
                    block.append(f"  {s.kind} {s.qualname} (L{s.start})"
                                 + (f" · used in {n_use} files" if n_use else ""))
            cost = sum(len(b) + 1 for b in block)
            if size + cost > limit and shown:
                break
            out.extend(block)
            size += cost
            shown += 1
    trailer = []
    if shown < len(entries):
        trailer.append(f"... {len(entries) - shown} more files — index_map(path=\"<dir>\") zooms "
                       f"in, or pass a larger budget")
    trailer.append("(next: index_file(\"<path>\") shows one file's outline, imports and users)")
    return _with_note("\n".join([head] + out + trailer), index)


def index_file(path: str, *, workdir: Path, index: ProjectIndex | None = None, cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    from .tools import _safe_path
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    rel = index._rel(p)
    with index._lock:
        fid = index._by_path.get(rel) if rel else None
        if fid is None:
            if p.is_dir():
                return f"ERROR: {path} is a directory — use index_map(path=\"{path}\")"
            if not p.exists():
                return f"ERROR: file not found: {path}"
            return (f"ERROR: {path} is not in the code index (binary, over 2 MB, git-ignored or "
                    f"outside the budget) — use read_file")
        e = index.files[fid]
        rank, used_by, sym_use = _ranks(index)
        out = [f"{e.path} ({e.lang or 'text'}, {e.nlines} lines, {len(e.symbols)} definitions"
               + (f", used by {used_by[fid]} files" if used_by.get(fid) else "") + ")"]
        if e.has_error:
            out.append(code_nav._ERROR_NOTE)
        if e.symbols:
            out.append("Outline:")
            shown = [s for s in e.symbols if s.depth <= 1]
            for s in shown[:60]:
                out.append(f"  {'  ' * s.depth}L{s.start}-{s.end}  {code_nav._line_label(s)}")
            if len(shown) > 60 or len(shown) < len(e.symbols):
                out.append(f"  ... ({len(e.symbols)} definitions in all — code_outline(\"{e.path}\") "
                           f"lists every one)")
        if e.imports:
            mods = []
            for imp in e.imports[:30]:
                mods.append(imp.module + (f" ({', '.join(imp.names[:6])}"
                                          f"{', ...' if len(imp.names) > 6 else ''})" if imp.names else ""))
            out.append(f"Imports ({len(e.imports)}): " + "; ".join(mods)
                       + (" ..." if len(e.imports) > 30 else ""))
        if e.lang and e.lang not in _DATA_LANGS:
            stem, cands = code_nav._module_candidates(p, workdir)
            importers = []
            for ofid, other in index._alive():
                if ofid == fid:
                    continue
                for imp in other.imports:
                    norm = code_nav._norm_module(imp.module)
                    if norm in cands or norm.endswith("." + stem) or stem in imp.names:
                        importers.append(f"{other.path}:L{imp.line}")
                        break
            out.append(f"Imported by ({len(importers)}): "
                       + (", ".join(importers[:20]) + (" ..." if len(importers) > 20 else "")
                          if importers else "(none found — matched on import text)"))
            used = sorted(((n, i) for (f, i), n in sym_use.items() if f == fid), reverse=True)[:8]
            if used:
                out.append("Most used definitions (files that reference the name):")
                for n, i in used:
                    s = e.symbols[i]
                    out.append(f"  {s.kind} {s.qualname} (L{s.start}) — {n} files")
        example = e.symbols[0].qualname if e.symbols else None
    if example:
        out.append(f'(next: read_symbol("{e.path}", "{example}") reads one definition'
                   + (f"; index_callers(\"{example.rsplit('.', 1)[-1]}\") shows its users)"
                      if e.lang not in _DATA_LANGS else ")"))
    return _with_note("\n".join(out), index)


def index_status(*, workdir: Path, index: ProjectIndex | None = None, cancel=None) -> str:
    if index is None:
        return "Code index: off (the user can turn it on with /index on)"
    return status_text(index)


# (key, label) of the composition categories, in stacking order.
BREAKDOWN_CATEGORIES = (
    ("files",    "File records"),
    ("symbols",  "Definitions"),
    ("imports",  "Imports"),
    ("idents",   "Identifiers"),
    ("trigrams", "Text signatures"),
)


def breakdown(index: ProjectIndex) -> dict:
    """What the index's memory is spent on — backs /index and the web UI's
    INDEX popover, the way context_breakdown backs /context.  Bytes are the
    same estimates the budget uses, so the categories add up to `used`."""
    with index._lock:
        n = nsym = nimp = nocc = 0
        by_lang: dict[str, list[int]] = {}
        for _, e in index._alive():
            n += 1
            nsym += len(e.symbols)
            nimp += len(e.imports)
            lang = e.lang or "text"
            row = by_lang.setdefault(lang, [0, 0])
            row[0] += 1
            row[1] += e.cost + e.cost_idents + e.cost_sig
        nocc = sum(len(a) for a in index._idents.values())
        cats = {
            "files":    (n * _C_FILE, f"{n:,} files"),
            "symbols":  (nsym * _C_SYMBOL, f"{nsym:,} definitions"),
            "imports":  (nimp * _C_IMPORT, f"{nimp:,} imports"),
            "idents":   (index.mem["idents"], f"{len(index._idents):,} names, {nocc:,} uses"),
            "trigrams": (index.mem["trigrams"], "per-file bitmaps"),
        }
        enabled = {"idents": index.components["idents"], "trigrams": index.components["trigrams"]}
        used = index.mem_used()
        return {
            "enabled": True,
            "state": index.state,
            "progress": f"{index.done}/{index.total}" if index.state in ("building", "refreshing")
                        and index.total else "",
            "files": n,
            "used": used,
            "limit": index.max_bytes,
            "pct": min(100, round(used / index.max_bytes * 100)) if index.max_bytes else 0,
            "partial": index.partial,
            "degraded": index.degraded(),
            "categories": [{"key": k, "label": label, "bytes": max(0, cats[k][0]),
                            "detail": cats[k][1], "enabled": enabled.get(k, True)}
                           for k, label in BREAKDOWN_CATEGORIES],
            "languages": [{"lang": lang, "files": f, "bytes": b}
                          for lang, (f, b) in sorted(by_lang.items(), key=lambda kv: -kv[1][1])],
            # Distinct identifier names belong to the project, not to one file.
            "shared": max(0, used - sum(b for _, b in by_lang.values())),
            "skipped": {k: v for k, v in index.skipped.items() if v},
            "error": index.last_error,
        }


def status_text(index: ProjectIndex) -> str:
    with index._lock:
        langs: dict[str, int] = {}
        nsym = 0
        for _, e in index._alive():
            langs[e.lang or "text"] = langs.get(e.lang or "text", 0) + 1
            nsym += len(e.symbols)
        nfiles = len(index._by_path)
        state = index.state
        if state != "idle" and index.total:
            state += f" {index.done}/{index.total}"
        comp = ", ".join(f"{k} {'on' if v else 'DROPPED'}" for k, v in index.components.items())
        lines = [
            f"Code index: {state} — {nfiles} files, {nsym} definitions, "
            f"{len(index._idents)} distinct identifiers",
            "By language: " + ", ".join(f"{k} {v}" for k, v in sorted(langs.items(), key=lambda kv: -kv[1])),
            f"Memory: ~{_fmt_size(index.mem_used())} of {_fmt_size(index.max_bytes)} "
            f"(files {_fmt_size(index.mem['files'])}, identifiers {_fmt_size(index.mem['idents'])}, "
            f"text signatures {_fmt_size(index.mem['trigrams'])}; estimated)",
            f"Components: {comp}" + ("; PARTIAL (budget reached)" if index.partial else ""),
        ]
        sk = {k: v for k, v in index.skipped.items() if v}
        if sk:
            lines.append("Skipped: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sk.items()))
        if index.last_error:
            lines.append(f"Last error: {index.last_error}")
    return "\n".join(lines)


# Tools that must see an up-to-date index before they run.
WAIT_TOOLS = {"index_search", "index_text", "index_callers", "index_map", "index_file",
              "code_outline", "find_symbol"}
