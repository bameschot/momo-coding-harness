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

Which files it covers is decided by a gitignore-syntax filter
(ignore_rules, /index-filter) kept next to the pickle in ~/.momo-harness/index/.
It is seeded once from the project's .gitignore files; after that only the
filter decides.

Bulk work (the first build, a rebuild, a branch switch) is read and parsed in
worker processes (/index-workers): tree-sitter holds the GIL, so threads would
not run in parallel.  Results are still applied by the one indexer thread.

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
import sys
import signal
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import code_nav, ignore_rules
from .net import format_size, parse_size
from .paths import BINARY_SNIFF_BYTES, MAX_SCAN_FILE_BYTES, safe_path
from .session import atomic_write

FORMAT_VERSION = 7   # 6: HTML inline-script JavaScript; 7: skipped files, source fingerprint
DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # default memory budget of the index (/index-max-mem)
MIN_MAX_BYTES = 1024 * 1024            # smallest budget /index-max-mem accepts
DEFAULT_MAX_FILES = 100_000            # a workdir of $HOME must not index the whole disk (/index-max-files)
MIN_MAX_FILES = 100                    # smallest file limit /index-max-files accepts
MAX_WORKERS = 64                       # largest worker count /index-workers accepts

# Every query re-checks the disk (a stat-diff: ~20 ms for 1,500 files).  Only a
# tree whose stat-diff is slow is throttled, to 10x what the last one took.
_STAT_THROTTLE_S = 0.0      # a query within this long of the last stat-diff trusts it
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
    package: str = ""         # Java/Kotlin `package a.b`: imports resolve through it
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


# Abbreviations a name uses where a question spells the word out ("database"
# for services.db).  Both directions: a query "cfg" also meets "config".
_ALIAS_GROUPS = (("database", "db"), ("config", "configuration", "cfg", "conf"),
                 ("environment", "env"), ("message", "msg"), ("button", "btn"),
                 ("authentication", "auth", "authorization"), ("repository", "repo"),
                 ("directory", "dir"), ("application", "app"), ("password", "pwd", "passwd"),
                 ("number", "num"), ("temporary", "tmp", "temp"), ("image", "img"),
                 ("parameter", "param"), ("argument", "arg"), ("request", "req"),
                 ("response", "resp", "res"), ("error", "err"), ("initialize", "init"),
                 ("maximum", "max"), ("minimum", "min"), ("attribute", "attr"),
                 ("document", "doc"), ("reference", "ref"), ("connection", "conn"),
                 ("service", "svc"), ("utility", "util"), ("library", "lib"))
_ALIASES: dict[str, frozenset[str]] = {}
for _g in _ALIAS_GROUPS:
    for _w in _g:
        _ALIASES[_stem(_w)] = frozenset(_stem(x) for x in _g if x != _w)


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


def filter_path(root: Path) -> Path:
    return pickle_path(root).with_suffix(".filter")


def _untrusted_reason(path: Path, st) -> str:
    if not stat_mod.S_ISREG(st.st_mode):
        return f"{path} is not a regular file"
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return f"{path} is not owned by you"
    if st.st_mode & 0o022:
        return f"{path} is writable by other users (chmod 600 it, or delete it)"
    return ""


def load_filter(root: Path) -> tuple[str, str]:
    """The project's index filter text, and a notice ('' when there is nothing
    to say).  A missing filter is seeded from the .gitignore files and saved;
    an untrusted or unreadable one is replaced by the seed for this run."""
    path = filter_path(root)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        text = ignore_rules.seed_text(root)
        try:
            atomic_write(path, text, private_dir=True)
        except OSError as e:
            return text, f"Code index filter could not be saved ({e}); using the .gitignore defaults."
        n = len(ignore_rules.Rules.parse(text))
        return text, (f"Code index filter created from .gitignore ({n} rules): {path} — "
                      f"/index-filter to view or edit it.")
    except OSError as e:
        return ignore_rules.seed_text(root), f"Code index filter not read ({e}); using the .gitignore defaults."
    why = _untrusted_reason(path, st)
    if why:
        return ignore_rules.seed_text(root), f"Code index filter not used: {why}. Using the .gitignore defaults."
    try:
        return path.read_text(encoding="utf-8", errors="replace"), ""
    except OSError as e:
        return ignore_rules.seed_text(root), f"Code index filter not read ({e}); using the .gitignore defaults."


def save_filter(root: Path, text: str) -> str:
    """Write the filter; returns '' or an ERROR string."""
    if not text.endswith("\n"):
        text += "\n"
    try:
        atomic_write(filter_path(root), text, private_dir=True)
    except OSError as e:
        return f"ERROR: could not save the code index filter: {e}"
    return ""


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


@lru_cache(maxsize=1)
def _source_fingerprint() -> str:
    """A hash of the extractor's own code: any change to what gets extracted
    discards saved indexes, without relying on FORMAT_VERSION being bumped."""
    h = hashlib.sha1()
    for mod in (code_nav, sys.modules[__name__]):
        try:
            h.update(Path(mod.__file__).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:16]


def _header(root: Path) -> dict:
    return {"format": FORMAT_VERSION, "root": str(root), "python": list(sys.version_info[:2]),
            "grammars": _grammar_versions(), "source": _source_fingerprint()}


def _fmt_size(n: int) -> str:
    from .net import format_size
    return format_size(max(0, int(n)))


# ── the index ────────────────────────────────────────────────────────────────

class ProjectIndex:
    """See the module docstring.  All public methods are thread-safe."""

    def __init__(self, root: Path, max_bytes: int = DEFAULT_MAX_BYTES, on_change=None,
                 max_files: int = DEFAULT_MAX_FILES, filter_text: str | None = None,
                 workers: int = 0):
        """filter_text None: load (or seed and save) the project's filter file,
        on the indexer thread — seeding walks the tree for .gitignore files.
        workers 0: auto_workers(); 1: build in the indexer thread only."""
        self.root = root.resolve()
        self.max_bytes = max(MIN_MAX_BYTES, int(max_bytes))
        self.max_files = max(MIN_MAX_FILES, int(max_files))
        self.workers = max(0, min(MAX_WORKERS, int(workers)))
        self._pool_broken = False               # worker processes failed: build serially
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
                # A filtered-out file is only queued to drop it (the worker skips it).
                if rel is not None and (rel in self._by_path or not self._rules.excluded(rel)):
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
        slow = self._diff_cost if self._diff_cost > _SLOW_DIFF_S else 0.0
        throttle = max(_STAT_THROTTLE_S, _THROTTLE_PER_DIFF * slow)
        if not self._dirty and time.monotonic() - self._last_diff < throttle:
            return
        self._diff()

    def _rel(self, p: Path) -> str | None:
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
            beyond.update(gone)     # the os.walk fallback stops listing at the limit
        with self._cond:
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
                text, note = load_filter(self.root)
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
                known = rel in self._by_path
                blocked = self.partial and not known
                beyond = rel in self._beyond or self._rules.excluded(rel)
            try:
                if beyond:
                    entry, rows, skip = None, None, None    # filtered out: as if gone
                elif blocked:
                    entry, rows, skip = None, None, self._skip_stat(rel, "budget")
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
            if rel in self._beyond or self._rules.excluded(rel):
                self._apply(rel, None, None)
            elif self.partial and rel not in self._by_path:
                self._apply(rel, None, None, self._skip_stat(rel, "budget"))
            else:
                rest.append(rel)
                continue
            self.done += 1
        return rest

    def _drain_parallel(self, rels: list[str], nworkers: int, want_idents: bool,
                        want_sig: bool) -> None:
        """Build rels in worker processes and apply each chunk's results under
        the lock as it arrives.  On stop or rescan the files not yet reached go
        back to the queue; if the pool breaks, they are built serially."""
        chunks = [rels[i:i + _PARALLEL_CHUNK] for i in range(0, len(rels), _PARALLEL_CHUNK)]
        todo = iter(chunks)
        pending: dict = {}
        pool = None
        try:
            pool = ProcessPoolExecutor(nworkers, initializer=_worker_init,
                                       mp_context=_mp_context())
            while True:
                while len(pending) < nworkers * 2 and not (self._stop or self._rescan):
                    chunk = next(todo, None)
                    if chunk is None:
                        break
                    fut = pool.submit(_build_many, str(self.root), chunk, want_idents, want_sig)
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
                if rel in self._beyond or self._rules.excluded(rel):
                    entry, rows, skip = None, None, None
                elif self.partial and rel not in self._by_path:
                    entry, rows, skip = None, None, self._skip_stat(rel, "budget")
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
        return _build_entry(self.root, rel, want_idents, want_sig)

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

    def paths_under(self, root: Path) -> "list[Path] | None":
        """The indexed source files under `root`, for code_nav's directory scans
        (find_symbol, code_outline) — the same files the index_* tools see.
        None when `root` is outside the project."""
        rel = self._rel(root)
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
                "skipped_files": dict(self._skipped),
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
            sk = payload.get("skipped_files", {})
            self._skipped = {r: v for r, v in sk.items()
                             if isinstance(r, str) and isinstance(v, tuple) and len(v) == 3} \
                if isinstance(sk, dict) else {}
            self._count_skipped()
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

# Definitions whose bare name says who uses them.  Methods are called through a
# receiver (`x.join(...)` is str.join as often as ProjectIndex.join) and module
# variables have names like `list` or `data`, so matching them by name ranks
# noise — index_map's graph leaves them out.
_RANK_KINDS = {"class", "interface", "struct", "enum", "trait", "type", "typedef", "union",
               "record", "object", "namespace", "module", "function", "constant", "macro"}

_CODE_KIND_ORDER = {"class": 0, "interface": 0, "struct": 0, "trait": 0, "enum": 1,
                    "function": 1, "method": 1, "table": 2, "stage": 2}
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
        parts.append(f"the project has more than {index.max_files:,} files; the ones past "
                     f"that limit are not indexed (/index-max-files)")
    return f"(note: {'; '.join(parts)})" if parts else ""


# Every index answer ends with this.  The 9B re-read the file an index result
# had just pointed at in most runs (evals, 2026-09-23) — "a correct result,
# distrusted" — and wait_fresh() really has re-synced with the disk, so say so
# where the decision to re-check gets made.
_CURRENT = ("(checked against the files on disk just now — this is current and complete: "
            "answer from it; re-reading files to double-check is not needed)")


def _with_note(text: str, index: ProjectIndex, component: str | None = None) -> str:
    note = _degraded_note(index, component)
    return text + (f"\n{note}" if note else "") + "\n" + _CURRENT


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
    if " " in ql and nl.replace("_", "") == ql.replace(" ", "").replace("_", ""):
        return 92       # "make counter" / "is entity" -> makeCounter / isEntity
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
        # The kind counts as a word of the name: "users table" is the table users,
        # not the class UserEvent that merely shares "user".
        nf = _overlap(qtoks, _tokens(qual) + (_stem(s.kind),))
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
        if t in stoks or not _ALIASES.get(t, frozenset()).isdisjoint(stoks):
            hit += 1
        elif len(t) >= 3 and any(st.startswith(t) or (len(st) >= 3 and t.startswith(st))
                                 for st in stoks):
            hit += 0.75
    return hit / len(qtoks)


# Test code: `tests/`, `test_x.py`, `x_test.go`, `x.test.ts`, `x.spec.js`, ...
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|specs?)/|(^|/)test_[^/]*$|_test\.[^/.]+$"
                        r"|\.(test|spec)\.[^/.]+$")


def _is_test(rel: str) -> bool:
    return bool(_TEST_PATH.search(rel))


_PATHLIKE = re.compile(r"[/\\]|\.[A-Za-z0-9]{1,6}$")


_CALLABLE_KINDS = code_nav.CALLABLE_KINDS
_kind_matches = code_nav.kind_matches

# What a model may pass as lang=, and the languages it also covers: JavaScript
# lives in HTML pages too, and TSX is TypeScript.
_LANG_ALIASES = {"js": "javascript", "jsx": "javascript", "node": "javascript", "mjs": "javascript",
                 "ts": "typescript", "py": "python", "python3": "python", "kt": "kotlin",
                 "kts": "kotlin", "rs": "rust", "c++": "cpp", "cxx": "cpp", "cc": "cpp",
                 "hpp": "cpp", "sh": "bash", "shell": "bash", "yml": "yaml", "htm": "html",
                 "docker": "dockerfile", "postgres": "sql", "postgresql": "sql", "mysql": "sql"}
_LANG_ALSO = {"javascript": ("html",), "typescript": ("tsx",)}
# Kinds the HTML extractor gives the page itself; everything else in an HTML
# file comes from its inline <script> JavaScript.
_HTML_KINDS = {"id", "script", "style", "element", "template"}


def _norm_lang(lang: str | None) -> str | None:
    if not lang:
        return None
    w = lang.strip().lower()
    return _LANG_ALIASES.get(w, w)


# Languages that can call or import each other's definitions.
_FAMILY = {"javascript": "js", "typescript": "js", "tsx": "js", "html": "js",
           "c": "c", "cpp": "c", "java": "jvm", "kotlin": "jvm"}


def _family(lang: str | None) -> str | None:
    return _FAMILY.get(lang, lang) if lang else None


def _is_code_symbol(lang: str | None, sym) -> bool:
    """A definition in code — not a config key, a CSS rule or an HTML id."""
    if not lang:
        return False
    if lang == "html":
        return sym.kind not in _HTML_KINDS
    return lang not in _DATA_LANGS


def _file_score(rel: str, q: str) -> int:
    ql, rl = q.lower().lstrip("./"), rel.lower()
    base = rl.rsplit("/", 1)[-1]
    if code_nav._is_pattern(ql):                 # a glob, like find_files: '*.py', 'test_*'
        return 100 if fnmatch.fnmatch(base, ql) or fnmatch.fnmatch(rl, ql) else 0
    if rl == ql:
        return 100
    if rl.endswith("/" + ql):
        return 95
    if base == ql or base.rsplit(".", 1)[0] == ql:
        return 90
    if len(ql) >= 3 and ql in rl:
        return 60
    return 0


# ── per-file build (indexer thread or worker process) ────────────────────────

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
        return None, None, None
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


# ── executors ────────────────────────────────────────────────────────────────

_MAX_SEARCH = 30
# Short definitions whose value IS the answer (a constant, a config key, a CSS
# rule, a SQL table's columns) are shown whole, so nothing is left to open.
_VALUE_KINDS = {"constant", "variable", "key", "list", "table", "var", "rule", "column",
                "env", "arg", "id", "keyframes", "media", "view", "index", "stage", "typedef",
                "type", "enum", "macro"}
_MAX_INLINE_LINES = 6
_MAX_INLINE_HITS = 8
_MAX_APPROX_AFTER_EXACT = 5   # an exact hit is the answer; a few near misses are context
_MAX_TEXT_HITS = 200
_MAX_TEXT_LINE = 200
_MAX_TEXT_CHARS = 9000      # the whole answer, like index_callers
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
    lang = _norm_lang(lang)
    langs = {lang, *_LANG_ALSO.get(lang, ())} if lang else None
    hits = []
    file_hits = []
    with index._lock:
        for fid, e in index._alive():
            if (langs and e.lang not in langs) or not keep(e.path):
                continue
            page_js = lang == "javascript" and e.lang == "html"   # only its scripts
            if want_files:
                fs = _file_score(e.path, q)
                if fs:
                    file_hits.append((-fs, len(e.path), e.path, e))
            if kind == "file":
                continue
            for s in e.symbols:
                if kind and not _kind_matches(s.kind, kind):
                    continue
                if page_js and s.kind in _HTML_KINDS:
                    continue
                sc = _score(s, q, ql, qtoks, pattern)
                if sc:
                    hits.append((-sc, _CODE_KIND_ORDER.get(s.kind, 3), len(s.qualname),
                                 e.path, s.start, s, e.lang, _is_test(e.path)))
        total_files = len(index._by_path)
    file_hits.sort(key=lambda h: h[:3])
    if not hits and kind and kind != "file" and not file_hits:
        # The name exists, just not as that kind: say so rather than "nothing".
        other = index_search(query, None, path, lang, workdir=workdir, index=index)
        if not other.startswith("(no definition"):
            return (f"(nothing of kind '{kind}' matches '{query}' — without kind= it matches "
                    f"these:)\n" + other)
    if file_hits and (not hits or -file_hits[0][0] >= -min(h[0] for h in hits)):
        # The query names a file: say so, and point at the tool that describes one.
        lines = [f"{h[2]}  (file, {h[3].lang or 'text'}, {h[3].nlines} lines, "
                 f"{len(h[3].symbols)} definitions)" for h in file_hits[:_MAX_SEARCH]]
        head = f"{len(file_hits)} file{'s' if len(file_hits) != 1 else ''} matching '{query}':"
        more = [f"... ({_MAX_SEARCH} of {len(file_hits)})"] if len(file_hits) > _MAX_SEARCH else []
        return _with_note("\n".join([head] + lines + more + [
            f'(next: index_file("{file_hits[0][2]}") shows its outline, imports and the files '
            f'that import it)']), index)
    if not hits:
        lines = [f"(no definition, key, selector or table matches '{query}' in {total_files} "
                 f"indexed files)"]
        mentions = _text_mentions(index, q, keep)
        if mentions:
            lines.append("The text appears in: " + mentions + " — index_text shows the lines")
        else:
            with index._lock:
                data = sorted({e.path for _, e in index._alive() if e.lang in _DATA_LANGS
                               and e.lang not in ("html", "css") and keep(e.path)})
            if data:
                lines.append(f"Config files in the index ({len(data)}): " + ", ".join(data[:12])
                             + (" ..." if len(data) > 12 else "")
                             + " — code_outline lists a file's keys")
        return _with_note("\n".join(lines), index)
    # Test code ranks below every real match in the source: a test class named
    # Budget must not shadow the constant a question about the budget is after.
    source_match = any(not h[7] and h[0] <= -40 for h in hits)
    hits.sort(key=lambda h: (source_match and h[7],) + h[:5])
    out = []
    exact_block = hits[0][0] <= -90
    if exact_block:
        n_exact = sum(1 for h in hits if h[0] <= -90)
        shown_hits = hits[:min(_MAX_SEARCH, n_exact + _MAX_APPROX_AFTER_EXACT)]
    else:
        shown_hits = hits[:_MAX_SEARCH]
    shown_sep = False
    file_lines: dict[str, list[str]] = {}
    inlined = 0
    best_whole = False
    for i, h in enumerate(shown_hits):
        sc, s, rel = -h[0], h[5], h[3]
        if exact_block and sc < 90 and not shown_sep:
            out.append("— approximate matches —")
            shown_sep = True
        out.append(f"{rel}:L{s.start}-{s.end}  {s.kind} {s.qualname}  | {s.signature}")
        span = s.end - s.start + 1
        if s.kind not in _VALUE_KINDS or span > _MAX_INLINE_LINES:
            continue
        if span == 1:
            best_whole = best_whole or i == 0     # the signature is the whole source
            continue
        if inlined >= _MAX_INLINE_HITS:
            continue
        if rel not in file_lines:
            try:
                file_lines[rel] = (index.root / rel).read_text(
                    encoding="utf-8", errors="replace").splitlines()
            except OSError:
                file_lines[rel] = []
        src = file_lines[rel][s.start - 1:s.end]
        if len(src) == span:
            out.extend(f"    {s.start + k}: {line.rstrip()[:160]}" for k, line in enumerate(src))
            inlined += 1
            best_whole = best_whole or i == 0
    head = f"{len(hits)} match{'es' if len(hits) != 1 else ''} for '{query}'"
    if not exact_block:
        head += " (no exact name match — closest first)"
    trailer = []
    if len(hits) > len(shown_hits):
        trailer.append(f"... ({len(shown_hits)} of {len(hits)} — narrow with kind=, lang= or path=)")
    best = hits[0][5]
    if -hits[0][0] < 90 or hits[0][6] in _DATA_LANGS:
        # No exact definition, or only a markup/config one: the answer may be
        # prose (docs, comments), which index_search cannot see.
        mentions = _text_mentions(index, q, keep)
        if mentions:
            trailer.append("(the text also appears in: " + mentions + " — index_text shows the lines)")
    if best_whole:
        trailer.append("(the best match's full source is shown above — nothing left to read)")
    else:
        # Callers make sense for code — including JavaScript inside an HTML page —
        # not for a config key or a CSS rule: decide by what the definition is.
        code_like = best.kind in _CALLABLE_KINDS or best.kind in code_nav._CONTAINER_KINDS
        trailer.append(f'(next: read_symbol("{hits[0][3]}", "{best.qualname}") reads the best match'
                       + (f"; index_callers(\"{best.name}\") shows who uses it)" if code_like else ")"))
    return _with_note("\n".join([head + ":"] + out + trailer), index)


def _text_mentions(index: ProjectIndex, query: str, keep, limit: int = 5) -> str:
    """'README.md ×7, harness/commands.py ×3' — the files whose text contains
    `query` (case-insensitive), most first; '' when none or the query is short."""
    lits = _WORD_STR.findall(query.lower())
    ids = _trigram_ids(lits)
    if not ids or len(query) < 3:
        return ""
    needle = query.lower()
    with index._lock:
        cands = []
        masks: dict[int, int] = {}
        use_sig = index.components["trigrams"]
        for _, e in index._alive():
            if not keep(e.path):
                continue
            if use_sig:
                if not e.sig_bits:
                    continue
                m = masks.get(e.sig_bits)
                if m is None:
                    m = masks[e.sig_bits] = _mask(ids, e.sig_bits)
                if e.sig & m != m:
                    continue
            cands.append(e.path)
    if len(cands) > 400:
        return ""       # too common a word to be a useful pointer
    counts = []
    for rel in cands:
        try:
            n = (index.root / rel).read_text(encoding="utf-8", errors="replace").lower().count(needle)
        except OSError:
            continue
        if n:
            counts.append((-n, rel))
    counts.sort()
    if not counts:
        return ""
    out = ", ".join(f"{rel} ×{-n}" for n, rel in counts[:limit])
    return out + (f" and {len(counts) - limit} more files" if len(counts) > limit else "")


def _line_owner(index: ProjectIndex, line: int, path: str | None, workdir: Path) -> str:
    """'Which definition is line N of this file in?' — one line of output."""
    if not path:
        return (f"ERROR: looking up line {line} needs the file it is in — pass it as path, "
                f"e.g. index_search(\"{line}\", path=\"src/app.py\")")
    p = safe_path(path, workdir)
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
    size = len(out[0])
    cur = None
    shown = 0
    for rel, ln, line, symbols in hits:
        owner = code_nav._enclosing(symbols, ln) if symbols else None
        where = f" [in {owner.qualname}]" if owner else ""
        text = line.strip()
        if len(text) > _MAX_TEXT_LINE:
            text = text[:_MAX_TEXT_LINE] + " …"
        block = ([f"{rel} ({files_hit[rel]})"] if rel != cur else []) + [f"  L{ln}{where}: {text}"]
        cost = sum(len(b) + 1 for b in block)
        if size + cost > _MAX_TEXT_CHARS and shown:
            break
        out.extend(block)
        size += cost
        shown += 1
        cur = rel
    if total > shown:
        rest = sorted((f for f in files_hit if f not in {h[0] for h in hits[:shown]}),
                      key=lambda f: -files_hit[f])
        more = (" — also in: " + ", ".join(f"{f} ({files_hit[f]})" for f in rest[:8])
                + (" ..." if len(rest) > 8 else "")) if rest else ""
        out.append(f"... (first {shown} of {total} matches{more} — narrow with path= or a "
                   f"longer query)")
    if not ids:
        out.append("(note: the query has no 3-letter word, so every indexed file was scanned)")
    first = hits[0]
    out.append(f'(next: read_symbol("{first[0]}", "L{first[1]}") reads the definition around a hit)')
    return _with_note("\n".join(out), index, "trigrams")


# Declared types that say nothing about the object: generics and "any".
_OPEN_TYPES = {"Any", "any", "object", "Object", "unknown", "dynamic", "Self", "self"}


def _other_type(index: ProjectIndex, rtype: str | None, cls: str) -> bool:
    """The receiver's declared type rules out `cls`.  Inheritance counts both
    ways: a `Circle c` is a Shape, and a `Shape s` / `Discount d` may BE the
    Circle / Half being asked about (virtual dispatch), so neither is dropped.
    Only an unrelated type rules it out.  Unknown, generic (`T`) and any-typed
    receivers never do."""
    if not rtype or rtype in _OPEN_TYPES or (len(rtype) <= 2 and rtype.isupper()):
        return False
    if _is_subtype(index, cls, rtype):
        return False                    # the receiver's type is a base of cls
    if _is_project_class(index, rtype):
        return not _is_subtype(index, rtype, cls)
    return True


def _is_project_class(index: ProjectIndex, t: str) -> bool:
    with index._lock:
        return any(index.files[f] is not None and index.files[f].symbols[i].name == t
                   and index.files[f].symbols[i].kind in code_nav._CONTAINER_KINDS
                   for f, i in index._sym_by_name.get(t.lower(), []))


def _is_subtype(index: ProjectIndex, t: str, target: str, depth: int = 4) -> bool:
    """`t` is `target` or (transitively) names it in its class declaration line:
    `class FlatPricer extends Pricer`, `class Circle : public Shape`,
    `class Line(Base):`.  A type not defined in the project is not a subtype."""
    if t == target:
        return True
    if depth == 0:
        return False
    with index._lock:
        sigs = [(index.files[f].lang, index.files[f].symbols[i].signature)
                for f, i in index._sym_by_name.get(t.lower(), [])
                if index.files[f] is not None and index.files[f].symbols[i].name == t
                and index.files[f].symbols[i].kind in code_nav._CONTAINER_KINDS]
    for lang, sig in sigs:
        rest = sig.split(t, 1)[-1]
        if lang != "python":
            rest = _drop_params(rest)       # Kotlin `class Box(val item: Circle) : Shape()`
        supers = [w for w in re.findall(r"[A-Za-z_]\w*", rest) if w != t]
        if target in supers or any(_is_subtype(index, w, target, depth - 1)
                                   for w in supers if w[:1].isupper()):
            return True
    return False


_CTOR_PARAMS = re.compile(r"\s*(?:<[^()]*>)?\s*(?:(?:private|protected|internal|public)\s+)?"
                          r"(?:@\w+\s+)*(?:constructor\s*)?\(")


def _drop_params(rest: str) -> str:
    """'(val item: Circle) : Shape()' -> ' : Shape()': the constructor parameters
    right after a class name (Kotlin, Java records) are not its bases.  Python's
    parentheses ARE the bases, so Python never comes here."""
    m = _CTOR_PARAMS.match(rest)
    if not m:
        return rest
    depth = 0
    for k in range(m.end() - 1, len(rest)):
        if rest[k] == "(":
            depth += 1
        elif rest[k] == ")":
            depth -= 1
            if depth == 0:
                return rest[:m.start()] + rest[k + 1:]
    return ""


def query_target(index: ProjectIndex, name: str) -> tuple[str, str | None, str | None]:
    """(bare name, receiver filter, class) for a callers query.  'Harness.send'
    names a method of the class Harness: its callers call it on self/harness/...,
    so the class is a scope, not a receiver filter.  'JSON.parse' is a filter."""
    dotted = name.strip().replace("::", ".")
    bare = dotted.rsplit(".", 1)[-1]
    if "." not in dotted:
        return bare, None, None
    qualifier = dotted.rsplit(".", 1)[0]
    with index._lock:
        is_class = any(index.files[fid] is not None
                       and index.files[fid].symbols[i].kind in code_nav._CONTAINER_KINDS
                       for fid, i in index._sym_by_name.get(qualifier.rsplit(".", 1)[-1].lower(), []))
    return (bare, None, qualifier) if is_class else (bare, qualifier, None)


def _uses(index: ProjectIndex, bare: str, want_recv: str | None, cancel,
          cls: str | None = None) -> tuple[list, list]:
    """All uses of `bare`: ([(rel, row1, role, recv, owner, line_text)], defs [(rel, Symbol)]).
    With `cls` (a query for cls.bare), drop what syntax shows is another object's
    method: a call on `self.attr` / `this.attr`, or a bare call inside a class
    that defines `bare` itself."""
    cls_last = cls.rsplit(".", 1)[-1] if cls else None
    with index._lock:
        named = [(index.files[f].lang, index.files[f].symbols[i])
                 for f, i in index._sym_by_name.get(bare.lower(), [])
                 if index.files[f] is not None and index.files[f].symbols[i].name == bare
                 and _is_code_symbol(index.files[f].lang, index.files[f].symbols[i])]
    # The definitions asked about: for Cart.total only Cart's, not Report.total.
    meant = [(lang, s) for lang, s in named
             if not cls_last or s.qualname == f"{cls_last}.{bare}"
             or s.qualname.endswith(f".{cls_last}.{bare}")] or named
    # A Python method is never called from JavaScript: when every definition
    # meant is in one language family, uses in other languages are other names.
    fams = {_family(lang) for lang, _ in meant}
    family = fams.pop() if len(fams) == 1 else None
    named = [s for _, s in named]
    owners_of_name = {s.qualname.rsplit(".", 1)[0] for s in named}
    # Every definition of the name is top-level (a function, not a method), so
    # `self.total` / `this.total` is some object's attribute, never a use of it.
    only_top_level = bool(named) and all(s.depth == 0 for s in named)
    with index._lock:
        if index.components["idents"]:
            occ = index._occurrences(bare)
            targets = [(e.path, set(lines)) for fid, lines in occ.items()
                       if (e := index.files[fid]) is not None and e.lang
                       and (family is None or _family(e.lang) == family)]
        else:
            targets = [(e.path, None) for _, e in index._alive()
                       if e.lang and (e.lang not in _DATA_LANGS or e.lang == "html")
                       and (family is None or _family(e.lang) == family)]
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
            role, recv, rtype = found[r]
            if role == "def":
                sym = next((s for s in parsed.symbols if s.name == bare and s.name_line == r + 1), None)
                if sym is not None:
                    defs.append((rel, sym))
                continue
            if role in code_nav.NOT_USES:
                continue
            if want_recv and recv != want_recv:
                continue
            if only_top_level and recv and recv.split(".", 1)[0] in ("self", "this", "cls"):
                continue
            owner = code_nav._enclosing(parsed.symbols, r + 1)
            if cls_last and role == "call":
                if recv and recv.startswith(("self.", "this.")):
                    continue            # an attribute of self is a different object
                if recv and recv.split("(", 1)[0] == "super" and owner is not None \
                        and cls_last in owner.qualname.split(".")[:-1]:
                    continue            # super().m() in C calls C's base, not C.m
                if _other_type(index, rtype, cls_last):
                    continue            # `Cart cart; cart.add()` is not Money.add
                if recv is None and owner is not None:
                    scopes = owner.qualname.split(".")[:-1]
                    here = next((".".join(scopes[:k]) for k in range(len(scopes), 0, -1)
                                 if ".".join(scopes[:k]) in owners_of_name), None)
                    if here and here.rsplit(".", 1)[-1] != cls_last:
                        continue        # a bare call to the enclosing class's own method
            line = parsed.lines[r].strip() if r < len(parsed.lines) else ""
            uses.append((rel, r + 1, role, recv, owner, line))
    return uses, defs


_USE_ROLES = ("call", "import", "type", "other")


def _data_defs(index: ProjectIndex, name: str) -> list:
    """[(rel, Symbol)] config keys, CSS rules, SQL tables, HTML ids... named `name`."""
    bare = name.rsplit(".", 1)[-1]
    with index._lock:
        return [(index.files[f].path, index.files[f].symbols[i])
                for f, i in index._sym_by_name.get(bare.lower(), [])
                if index.files[f] is not None
                and not _is_code_symbol(index.files[f].lang, index.files[f].symbols[i])
                and code_nav._matches(index.files[f].symbols[i], name)]


def _data_callers(index: ProjectIndex, name: str, bare: str, data: list) -> str:
    """index_callers on a config key / table / selector: code refers to those by
    string, so the answer is where it is defined and which files mention it."""
    where = "; ".join(f"{rel}:L{s.start}-{s.end} ({s.kind} {s.qualname})" for rel, s in data[:5])
    out = [f"'{name}' is not code — it is defined at {where}"
           + (f" and {len(data) - 5} more" if len(data) > 5 else "") + "."]
    homes = {rel for rel, _ in data}
    mentions = _text_mentions(index, bare, lambda rel: rel not in homes, limit=10)
    if mentions:
        out.append(f"Code refers to it by text; '{bare}' appears in: {mentions}.")
        out.append(f'(next: index_text("{bare}") shows those lines)')
    else:
        out.append(f"No other file mentions '{bare}'.")
    return _with_note("\n".join(out), index)


def _files_named(index: ProjectIndex, name: str) -> list[str]:
    """Indexed code files a module-style name refers to: 'format', 'src/format.js',
    'shop.pricing' (by stem, path or dotted path)."""
    q = name.strip().replace("\\", "/")
    dotted = q.replace("/", ".").rsplit(".", 1)[0] if "/" in q else q
    with index._lock:
        out = []
        for _, e in index._alive():
            if not e.lang or e.lang in _DATA_LANGS:
                continue
            stem_path = e.path.rsplit(".", 1)[0]
            if (e.path == q or stem_path == q or stem_path.replace("/", ".").endswith(dotted)
                    and (stem_path.rsplit("/", 1)[-1] == dotted.rsplit(".", 1)[-1])):
                out.append(e.path)
    return sorted(out)


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
    if role and role not in _USE_ROLES:
        return f"ERROR: unknown role '{role}' (use one of: {', '.join(_USE_ROLES)})"
    dotted = (name or "").strip().replace("::", ".")
    if not dotted:
        return "ERROR: name is empty"
    bare, want_recv, cls = query_target(index, dotted)
    uses, defs = _uses(index, bare, want_recv, cancel, cls)
    if cancel is not None and cancel.is_set():
        return "ERROR: cancelled"
    if cls:
        defs = [d for d in defs if d[1].qualname == dotted or d[1].qualname.endswith("." + dotted)] or defs
    if role:
        uses = [u for u in uses if u[2] == role]
    if not defs and not uses:
        data = _data_defs(index, dotted)
        if data:
            return _data_callers(index, dotted, bare, data)
        files = _files_named(index, dotted)
        if files:
            # `index_callers("format")` asks about a module, not a definition.
            return _with_note(
                f"'{dotted}' is not a definition — it names a file: {', '.join(files[:5])}.\n"
                f'(next: index_file("{files[0]}") lists the files that import it and what '
                f"each one uses)", index)
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
    importing = sorted({u[0] for u in uses if u[2] == "import"})
    if importing:
        # "Which files import X?" is answered here, before the use-by-use list.
        out.append(f"Imported in {len(importing)} file{'s' if len(importing) != 1 else ''}: "
                   + ", ".join(importing[:15]) + (" ..." if len(importing) > 15 else ""))
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
            # Scoped like level 1: Cart.total's users, not every `total`.
            b2, _recv, c2 = query_target(index, owner.qualname)
            u2, _ = _uses(index, b2, None, cancel, c2)
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


_HEADER_EXTS = (".h", ".hh", ".hpp", ".hxx", ".h++", ".inc")
# Languages whose imports name declarations (a class, a function), not files.
_DECL_IMPORT_LANGS = {"java", "kotlin"}


def _import_graph(index: ProjectIndex) -> dict[int, dict[int, int]]:
    """{file: {file it imports: line of the import}}, cached per index version.

    Resolution is textual, in order: the imported names as modules (`from pkg
    import mod`); the longest dotted prefix that names a file
    (`com.acme.Money.of` -> Money.java, `shop::model::Item` -> model.rs); for
    Java/Kotlin, a top-level declaration of the imported name (`import
    shop.Product` -> the file declaring Product).  A C/C++ header include only
    matches headers, never the same-stem .c/.cpp.  A same-named module elsewhere
    can still show up."""
    cached = index._graph_cache
    if cached is not None and cached[0] == index.version:
        return cached[1]
    alive = [fid for fid, _ in index._alive()]
    cands: dict[str, set[int]] = defaultdict(set)
    fqns: dict[str, set[int]] = defaultdict(set)    # "com.acme.Money" -> Money.java
    for fid in alive:
        e = index.files[fid]
        if not e.lang or e.lang in _DATA_LANGS:
            continue
        for c in code_nav._module_candidates(index.root / e.path, index.root)[1]:
            cands[c].add(fid)
        if e.package:
            fqns[e.package + ".*"].add(fid)
            for sym in e.symbols:
                if sym.depth == 0:
                    fqns[f"{e.package}.{sym.name}"].add(fid)
        elif e.lang in _DECL_IMPORT_LANGS:
            for sym in e.symbols:           # the default package: `import CdpClient`
                if sym.depth == 0:
                    fqns[sym.name].add(fid)
    out: dict[int, dict[int, int]] = defaultdict(dict)
    for fid in alive:
        e = index.files[fid]
        for imp in e.imports:
            targets = _relative_import(imp, e, index)
            if targets is None:
                targets = _resolve_import(imp, e.lang, cands, fqns, index)
            targets.discard(fid)
            for d in targets:
                out[fid].setdefault(d, imp.line)
    index._graph_cache = (index.version, out)
    return out


_JS_EXTS = ("", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx", ".d.ts",
            "/index.js", "/index.ts", "/index.tsx", "/index.jsx")


def _relative_import(imp, e: "FileEntry", index: ProjectIndex) -> set[int] | None:
    """Targets of an import written relative to the importing file — Python
    `from . import x` / `from ..pkg import y`, JS/TS `./x` / `../x`, C/C++
    `#include "x.h"` — resolved against that file's directory.  None when the
    import is not relative (or, for C, nothing sits next to the includer), so
    the textual resolver runs instead."""
    here = Path(e.path).parent
    by_path = index._by_path

    def find(*cands: str) -> set[int]:
        out = set()
        for c in cands:
            rel = Path(c).as_posix()
            parts = []
            for part in rel.split("/"):
                if part == "..":
                    if not parts:
                        return set()
                    parts.pop()
                elif part not in ("", "."):
                    parts.append(part)
            fid = by_path.get("/".join(parts))
            if fid is not None:
                out.add(fid)
        return out

    mod = imp.module
    if e.lang == "python" and mod.startswith("."):
        dots = len(mod) - len(mod.lstrip("."))
        base = here
        for _ in range(dots - 1):
            base = base.parent
        rest = mod.lstrip(".").replace(".", "/")
        if rest:
            return find(f"{base}/{rest}.py", f"{base}/{rest}/__init__.py")
        out = set()
        for n in imp.names:                     # from . import utils, models
            out |= find(f"{base}/{n}.py", f"{base}/{n}/__init__.py")
        return out or find(f"{base}/__init__.py")
    if e.lang in code_nav._JS_LANGS and mod.startswith((".", "/")):
        return find(*(f"{here}/{mod}{ext}" for ext in _JS_EXTS))
    if e.lang in ("c", "cpp") and '"' in imp.text:
        hit = find(f"{here}/{mod}")
        return hit or None                      # else: an -I include path, matched textually
    return None


def _resolve_import(imp, lang: str | None, cands, fqns, index: ProjectIndex) -> set[int]:
    norm = code_nav._norm_module(imp.module)
    parts = norm.split(".") if norm else []
    targets: set[int] = set()
    if lang in _DECL_IMPORT_LANGS:
        # `import com.acme.Money.of` / `import shop.Product` / `import a.b.*`:
        # the longest prefix that is a declared package.name wins.
        raw = imp.module.replace("::", ".")
        if raw.endswith(".*"):
            targets = set(fqns.get(raw, ()))
        for k in range(len(parts), 0, -1):
            if targets:
                break
            targets = set(fqns.get(".".join(parts[:k]), ()))
        if fqns:
            # The project declares packages, so an import none of them covers
            # is a library's (`java.util.List`), not a file named List or util
            # — unless a file's path spells it out: maestro/drivers/AndroidDriver.kt
            # for `maestro.drivers.AndroidDriver` (its class did not parse).
            for k in range(len(parts), 1, -1):
                if targets:
                    break
                targets = {d for d in cands.get(".".join(parts[:k]), ())
                           if index.files[d].lang in _DECL_IMPORT_LANGS}
            return targets
    # `from harness import tools` imports the module tools, not the package.
    for n in ([] if targets else imp.names):
        targets |= cands.get(f"{norm}.{n}" if norm else n, set())
    if not targets and parts:
        for k in range(len(parts), 0, -1):          # longest prefix first
            for s0 in range(k):                     # then its longest suffix
                hit = cands.get(".".join(parts[s0:k]))
                if hit:
                    targets = set(hit)
                    break
            if targets:
                break
    if lang in ("c", "cpp") and imp.module.lower().endswith(_HEADER_EXTS):
        targets = {d for d in targets if index.files[d].path.lower().endswith(_HEADER_EXTS)}
    # `import java.util.List` is not util.c; a Kotlin build script is not vite.config.ts.
    fam = _family(lang)
    return {d for d in targets if _family(index.files[d].lang) == fam
            or (fam == "js" and index.files[d].lang in ("css", "json"))}


def _ranks(index: ProjectIndex):
    """(file rank {fid: score}, used-by {fid: n files}, symbol use {(fid, i): n files}),
    cached per index version.

    A file is "used by" the files that import it; each of its top-level
    definitions those importers also mention adds weight.  Counting a bare name
    anywhere in the project instead ranked noise: `compile`, `split` or `parse`
    are mentioned everywhere through re.compile and str.split."""
    cached = index._rank_cache
    if cached is not None and cached[0] == index.version:
        return cached[1]
    alive = [fid for fid, _ in index._alive()]
    imports = _import_graph(index)
    importers: dict[int, set[int]] = defaultdict(set)
    for r, targets in imports.items():
        for d in targets:
            importers[d].add(r)
    edges: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    sym_use: dict[tuple[int, int], int] = {}
    for d, rs in importers.items():
        for r in rs:
            edges[r][d] += 1.0
        if not index.components["idents"]:
            continue
        for i, s in enumerate(index.files[d].symbols):
            if s.depth != 0 or s.kind not in _RANK_KINDS or len(s.name) < 3:
                continue
            users = index._ref_files(s.name) & rs
            if users:
                sym_use[(d, i)] = len(users)
                for r in users:
                    edges[r][d] += 1.0
    used_by = importers
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
    result = (rank, {k: len(v) for k, v in used_by.items() if v}, sym_use)
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
        # Most importers first — what the header promises; PageRank breaks ties,
        # so a file imported by central files beats one imported by leaves.
        entries.sort(key=lambda fe: (-used_by.get(fe[0], 0), -rank.get(fe[0], 0), fe[1].path))
        limit = budget * 4
        out = []
        head = (f"Project map{f' of {path}' if path and path not in ('.', './') else ''} — "
                f"{len(entries)} files, the most imported first:")
        size = len(head)
        shown = 0
        for fid, e in entries:
            lang = e.lang or "text"
            ub = used_by.get(fid, 0)
            line = f"{e.path} ({lang}, {e.nlines} lines)" + (f" — imported by {ub} files" if ub else "")
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
    p = safe_path(path, workdir)
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
               + (f", imported by {used_by[fid]} files" if used_by.get(fid) else "") + ")"]
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
            graph = _import_graph(index)
            importers = sorted(((ofid, f"{index.files[ofid].path}:L{targets[fid]}")
                                for ofid, targets in graph.items() if fid in targets),
                               key=lambda t: t[1])
            if not importers:
                out.append("Imported by: (none found — matched on import text)")
            else:
                # Which of this file's definitions each importer uses, so "who
                # uses what" is one call instead of a grep per importer.
                tops = [s for s in e.symbols if s.depth == 0 and len(s.name) >= 3]
                out.append(f"Imported by ({len(importers)}) — and the definitions each one uses:")
                for ofid, where in importers[:20]:
                    own = {s.name for s in index.files[ofid].symbols if s.depth == 0}
                    names = [s.name for s in tops if s.name not in own
                             and ofid in index._ref_files(s.name)] \
                        if index.components["idents"] else []
                    out.append(f"  {where}" + (f" — uses {', '.join(names[:10])}"
                                               + (" ..." if len(names) > 10 else "") if names else ""))
                if len(importers) > 20:
                    out.append(f"  ... {len(importers) - 20} more")
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


_MAX_FIND = 100     # same cap as find_files


def index_find_files(pattern: str, directory: str | None, *, index: ProjectIndex,
                     cancel=None) -> str | None:
    """find_files answered from the index's file list: a bare glob ('*.py',
    'config*') matched against file names under `directory`.  None when no
    indexed file matches, so the caller can look on disk (images, git-ignored
    files); an ERROR string when the wait for the index was cancelled."""
    if (err := _ready(index, cancel)):
        return err
    keep = _path_filter(directory)
    with index._lock:
        hits = sorted((e for _, e in index._alive()
                       if keep(e.path) and fnmatch.fnmatch(e.path.rsplit("/", 1)[-1], pattern)),
                      key=lambda e: e.path)
    if not hits:
        return None
    lines = [f"{e.path}  ({e.lang or 'text'}, {e.nlines} lines"
             + (f", {len(e.symbols)} definitions" if e.symbols else "") + ")"
             for e in hits[:_MAX_FIND]]
    if len(hits) > _MAX_FIND:
        lines.append(f"... (first {_MAX_FIND} of {len(hits)} files — use a more specific pattern "
                     f"or directory)")
    return "\n".join(lines)


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
            "max_files": index.max_files,
            "workers": index.workers,
            "workers_used": index.worker_count(),
            "filter": {"path": str(filter_path(index.root)), "rules": index.filter_rule_count()},
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


def _skip_label(reason: str, n: int, max_files: int) -> str:
    """One "Skipped:" entry; "limit" is a flag (the listing stops at the limit)."""
    if reason == "limit":
        return f"files past the {max_files:,}-file limit"
    return f"{n:,} {reason.replace('_', ' ')}"


def workers_label(workers: int) -> str:
    return f"auto ({auto_workers()})" if workers == 0 else str(workers)


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
            f"Workers: {workers_label(index.workers)}"
            + ("; worker processes failed, building in one process" if index._pool_broken else ""),
        ]
        sk = {k: v for k, v in index.skipped.items() if v}
        if sk:
            lines.append("Skipped: " + ", ".join(_skip_label(k, v, index.max_files)
                                                 for k, v in sk.items()))
        if index.last_error:
            lines.append(f"Last error: {index.last_error}")
    return "\n".join(lines)


# Tools that must see an up-to-date index before they run.
WAIT_TOOLS = {"index_search", "index_text", "index_callers", "index_map", "index_file",
              "code_outline", "find_symbol"}
