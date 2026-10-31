"""Completion for the input box, shared by the TUI and the web UI (/api/complete):
"/" completes every word of a command from the usages in /help, "@" completes
workspace paths.

A path completes folder by folder: "@harness/llm/" lists that folder, and
picking a folder fills it in and keeps the list open (Item.more).  An "@"
without a "/" is a fuzzy search over the whole workspace, folders included.
Path arguments of commands (/read, /ls, /grep, /undo file, /export) complete
inside the workspace; /workspace completes any folder on disk.

complete() looks only at the text before the cursor.  With wait=False (the
TUI's key loop) it never blocks: the workspace file list and the model list
come from caches refreshed in the background, and `waiting` asks the caller to
try again once the first walk is done."""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .file_search import fuzzy_search, workspace_files, workspace_files_nowait
from .momo_dir import MOMO_DIR
from .paths import SKIP_DIRS, safe_path

AT_RX = re.compile(r"(?:^|\s)@([\w./~+-]*)$")
MAX_ITEMS = 30
_SCAN_MAX = 5000            # directory entries read for one listing
_CACHE_TTL_S = 10.0


@dataclass
class Item:
    value: str            # replaces text[start:cursor]
    label: str            # what the list shows
    desc: str = ""
    more: bool = False    # picking it goes on completing (a folder, a word with words after it)
    kind: str = "arg"     # "cmd" | "arg" | "path" | "note"


@dataclass
class Completion:
    start: int
    items: list[Item] = field(default_factory=list)
    waiting: bool = False   # the workspace walk is still running: ask again when it is done

    def to_dict(self) -> dict:
        return {"start": self.start, "items": [asdict(i) for i in self.items],
                "waiting": self.waiting}


# ── paths ─────────────────────────────────────────────────────────────────────

def path_items(partial: str, base: Path, *, confine: bool, dirs_only: bool = False,
               limit: int = MAX_ITEMS) -> list[Item]:
    """The entries of the folder `partial` names, matching the part after its
    last "/": folders first (ending in "/", more=True), then files.  confine:
    only inside `base` (the workspace); else any folder, "~" and absolute paths
    too.  The value keeps what was typed before the name ("~/pro" → "~/projects/")."""
    head, slash, name = partial.rpartition("/")
    shown_dir = head + slash
    if confine:
        target = safe_path(shown_dir or ".", base)
        if isinstance(target, str):
            return []
    else:
        target = Path(shown_dir or ".").expanduser()
        if not target.is_absolute():
            target = base / target
    entries = []
    try:
        with os.scandir(target) as it:
            for i, e in enumerate(it):
                if i >= _SCAN_MAX:
                    break
                entries.append(e)
    except OSError:
        return []
    low = name.lower()
    rows = []
    for e in entries:
        n = e.name
        if n.startswith(".") and not name.startswith("."):
            continue
        if n in SKIP_DIRS and n != MOMO_DIR:
            continue
        nl = n.lower()
        if low and low not in nl:
            continue
        try:
            is_dir = e.is_dir()
        except OSError:
            continue
        if dirs_only and not is_dir:
            continue
        rows.append((not is_dir, not nl.startswith(low), nl, n, is_dir))
    rows.sort()
    return [Item(value=shown_dir + n + ("/" if d else ""), label=shown_dir + n + ("/" if d else ""),
                 desc="folder" if d else "", more=d, kind="path")
            for *_, n, d in rows[:limit]]


def _with_dirs(files: list[str]) -> tuple[list[str], set[str]]:
    """The workspace files plus every folder that holds one (no trailing "/")."""
    dirs = set()
    for f in files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return files + sorted(dirs), dirs


def _at_items(q: str, src: "Sources", wait: bool) -> tuple[list[Item], bool]:
    if "/" in q:
        items = path_items(q, src.workdir, confine=True)
        return [Item(value=f"@{i.value}" if i.more else f"`{i.value}` ", label=f"@ {i.value}",
                     desc=i.desc, more=i.more, kind="path") for i in items], False
    files = workspace_files(src.workdir) if wait else workspace_files_nowait(src.workdir)
    if files is None:
        return [Item(value="", label="searching the workspace…", kind="note")], True
    pool, dirs = _with_dirs(files)
    out = []
    for p in fuzzy_search(pool, q):
        if p in dirs:
            out.append(Item(value=f"@{p}/", label=f"@ {p}/", desc="folder", more=True, kind="path"))
        else:
            out.append(Item(value=f"`{p}` ", label=f"@ {p}", kind="path"))
    return out, False


# ── command usages ───────────────────────────────────────────────────────────

@dataclass
class _Tok:
    kind: str                 # "lit" | "choice" | "opt" (an optional literal) | "hole"
    words: tuple = ()         # the literal(s) / choices
    name: str = ""            # a hole's placeholder name ("path", "server", "name")


def _token(raw: str) -> _Tok:
    opt = raw.startswith("[") and raw.endswith("]")
    inner = raw[1:-1] if opt else raw
    if "|" in inner:
        words = tuple(w for w in inner.split("|")
                      if w and not re.search(r"[<>\[\]…]", w) and w not in ("n", "N"))
        holes = [w for w in inner.split("|") if w.startswith("<")]
        if words:
            return _Tok("choice", words, name=holes[0].strip("<>…") if holes else "")
        return _Tok("hole", name=(holes[0] if holes else inner).strip("<>…"))
    if inner.startswith("<") or inner in ("n", "N") or "…" in inner:
        return _Tok("hole", name=inner.strip("<>…"))
    if opt:
        # "[force]" is an optional word; "[path]", "[filename]", "[server]" are optional holes.
        return (_Tok("opt", (inner,)) if inner in _OPTIONAL_WORDS
                else _Tok("hole", name=inner))
    return _Tok("lit", (inner,))


# Bracketed words in /help usages that are literal flags, not placeholders.
_OPTIONAL_WORDS = {"force", "--force", "stories", "full", "update"}


def _usage_tokens(usage: str) -> list[_Tok] | None:
    """A /help usage as tokens after the command name; None for one that takes
    no arguments ("/exit | /quit")."""
    if " | " in usage:
        return None
    return [_token(t) for t in usage.split()[1:]]


def _fits(tok: _Tok, word: str) -> bool:
    if tok.kind in ("lit", "opt"):
        return word in tok.words
    if tok.kind == "choice":
        return word in tok.words or bool(tok.name)
    return True


def _positions(tokens: list[_Tok], words: list[str]) -> list[tuple[int, int]]:
    """Where the next word can go in `tokens` after `words` matched, as
    (index, placeholders used): one per way of matching (an optional word may
    be skipped).  Fewer placeholders is the closer match: "/tools discover"
    is the subcommand, not a tool named "discover"."""
    states = {(0, 0)}
    for w in words:
        nxt = set()
        for i, holes in states:
            j = i
            while j < len(tokens):
                if _fits(tokens[j], w):
                    literal = tokens[j].kind in ("lit", "opt") or w in tokens[j].words
                    nxt.add((j + 1, holes + (0 if literal else 1)))
                if tokens[j].kind != "opt":
                    break
                j += 1          # skip the optional word and try the next token
        states = nxt
        if not states:
            return []
    out = set()
    for i, holes in states:
        j = i
        while j < len(tokens):
            out.add((j, holes))
            if tokens[j].kind != "opt":
                break
            j += 1
        if j >= len(tokens):
            out.add((len(tokens), holes))
    return sorted(out)


def _rank(cands: list[tuple[str, str, bool]], partial: str) -> list[tuple[str, str, bool]]:
    """Candidates matching `partial`, prefix matches first; a word that some
    usage continues after keeps more=True ("/plan stories" and
    "/plan stories approve …")."""
    low = partial.lower()
    best: dict[str, list] = {}
    for v, d, more in cands:
        if v == partial or low not in v.lower():
            continue
        if v in best:
            best[v][1] = best[v][1] or more
        else:
            best[v] = [d, more]
    out = sorted((not v.lower().startswith(low), v.lower(), v) for v in best)
    return [(v, best[v][0], best[v][1]) for *_, v in out[:MAX_ITEMS]]


# Placeholders that are paths: (command, name) first, then the name alone.
_WORKSPACE_PATH = {"path", "path_or_dir", "filename", "file"}


def _command_items(line: str, src: "Sources", wait: bool) -> tuple[int, list[Item]] | None:
    words = line.split(" ")
    cmd = words[0].lower()
    usages = [c for c in src.commands if c["cmd"] == cmd]
    if len(words) == 1:
        found = [c for c in src.commands if c["cmd"].startswith(cmd) or c["usage"].startswith(line)]
        # A command typed in full is not offered again (its longer relatives are).
        found = [c for c in found if c["usage"] != line.strip()]
        if not found:
            return None
        # A command with any usage that takes words goes on to complete them.
        takes = {c["cmd"] for c in src.commands if _usage_tokens(c["usage"])}
        return 0, [Item(value=c["cmd"] + (" " if c["cmd"] in takes else ""), label=c["usage"],
                        desc=c["desc"], more=c["cmd"] in takes, kind="cmd")
                   for c in found[:MAX_ITEMS * 2]]
    if not usages:
        return None
    done, partial = [w for w in words[1:-1] if w], words[-1]
    start = len(line) - len(partial)
    cands: list[tuple[str, str, bool]] = []
    path_holes: list[tuple[bool, bool]] = []   # (confine, dirs_only)
    matches = [(c, toks, pos, holes) for c in usages if (toks := _usage_tokens(c["usage"]))
               for pos, holes in _positions(toks, done)]
    fewest = min((h for *_, h in matches), default=0)
    for c, toks, pos, holes in matches:
        if holes != fewest or pos >= len(toks):
            continue
        tok = toks[pos]
        more = pos + 1 < len(toks)
        if tok.kind in ("lit", "opt", "choice"):
            cands += [(w, c["desc"], more) for w in tok.words]
        hole = tok.name if tok.kind in ("hole", "choice") else ""
        if not hole:
            continue
        if cmd in ("/workspace", "/workdir") and hole == "path":
            path_holes.append((False, True))
        elif hole in _WORKSPACE_PATH:
            path_holes.append((True, False))
        else:
            cands += [(v, d, more) for v, d in src.values(cmd, hole, wait)]
    items = [Item(value=v + (" " if more else ""), label=v, desc=d, more=more)
             for v, d, more in _rank(cands, partial)]
    for confine, dirs_only in dict.fromkeys(path_holes):
        items += path_items(partial, src.workdir, confine=confine, dirs_only=dirs_only)
    if not items:
        return None
    return start, items[:MAX_ITEMS * 2]


# ── entry point ──────────────────────────────────────────────────────────────

def complete(text: str, cursor: int, src: "Sources", *, wait: bool) -> Completion | None:
    """What to offer for the text before the cursor; None for nothing."""
    before = text[:cursor]
    m = AT_RX.search(before)
    if m:
        items, waiting = _at_items(m.group(1), src, wait)
        start = m.end() - len(m.group(1)) - 1     # the "@"
        return Completion(start, items, waiting) if items else None
    if not before.startswith("/") or "\n" in before:
        return None
    got = _command_items(before, src, wait)
    return Completion(*got) if got else None


# ── sources ──────────────────────────────────────────────────────────────────

class Sources:
    """What completion reads from the running harness: the workdir, the /help
    usages, and the live values of placeholders (models, sessions, servers…)."""

    def __init__(self, harness, commands: list[dict]):
        self.h = harness
        self.commands = commands

    @property
    def workdir(self) -> Path:
        return self.h.workdir

    def values(self, cmd: str, name: str, wait: bool) -> list[tuple[str, str]]:
        h = self.h
        try:
            if name == "server" or (cmd == "/mcp" and name == "name"):
                return [(r["name"], r["state"]) for r in h.mcp.status_rows()]
            if cmd == "/model" and name == "name":
                cur = h.client.model
                return [(m, "the model in use" if m == cur else "")
                        for m in _cached(("models", h.client.host), h.client.list_models, wait) or []]
            if cmd == "/session" and name == "name":
                from . import session as session_mod
                return [(p.stem, time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)))
                        for p in session_mod.list_sessions()[:50]]
            if cmd == "/load-skill":
                active = set(h.active_skills)
                return [(s, "") for s in h.list_available_skills() if s not in active]
            if cmd == "/unload-skill":
                return [(s, "") for s in h.active_skills]
            if cmd == "/think":
                return [(v, f"{h.client.model} takes this") for v in h.think_choices()]
            if cmd == "/tools" and name == "name":
                return [(t["name"], t.get("state") or "") for t in h.tool_choices()]
        except Exception:
            return []           # a source that fails offers nothing; the fixed choices still show
        return []


# (key) -> (monotonic time, values); refreshed in the background for wait=False.
_cache: dict = {}
_cache_lock = threading.Lock()
_refreshing: set = set()


def _cached(key, load, wait: bool):
    hit = _cache.get(key)
    fresh = hit is not None and time.monotonic() - hit[0] < _CACHE_TTL_S
    if fresh:
        return hit[1]
    if wait:
        values = load()
        with _cache_lock:
            _cache[key] = (time.monotonic(), values)
        return values
    if key not in _refreshing:
        _refreshing.add(key)

        def refresh():
            try:
                values = load()
                with _cache_lock:
                    _cache[key] = (time.monotonic(), values)
            except Exception:
                pass
            finally:
                _refreshing.discard(key)
        threading.Thread(target=refresh, name="momo-complete", daemon=True).start()
    return hit[1] if hit else None
