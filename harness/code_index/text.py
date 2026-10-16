"""index_text — full-text search, prefiltered by the per-file trigram
signatures — and find_line_text for edit_file's "this text is elsewhere"."""
from __future__ import annotations

import re
from pathlib import Path

from .. import code_nav
from ..paths import split_lines
from .records import _WORD_STR, _mask, _trigram_ids
from .store import ProjectIndex
from .common import (_MAX_TEXT_CHARS, _MAX_TEXT_HITS, _MAX_TEXT_LINE, _MAX_TEXT_PER_FILE,
                     _path_filter, _read_source, _ready, _scope, _with_note)

_UNSAFE_REGEX = re.compile(r"\)[?*{]")
_VERBOSE_FLAG = re.compile(r"\(\?[aiLmsu]*x")          # (?x): spaces and # comments are not text
# Groups whose text a match does not contain: lookarounds and comments.
_UNMATCHED_GROUP = re.compile(r"\(\?(?:<?[=!]|#)")
# Syntax that looks like a word but is not text: group names and backreferences
# by name; and counted quantifiers ({3}, {3,100}), which become "*" so the
# character before one still counts as optional ({0,3}).
_NOT_TEXT = re.compile(r"\(\?P<\w+>|\(\?P=\w+\)|\(\?<\w+>")
_COUNTED = re.compile(r"\{\d*(?:,\d*)?\}")


def _drop_groups(pattern: str, start: re.Pattern) -> str | None:
    """`pattern` with every group `start` opens removed whole, or None when a
    group's end cannot be found."""
    out, i = [], 0
    while (m := start.search(pattern, i)) is not None:
        out.append(pattern[i:m.start()])
        depth, k, in_class = 0, m.start(), False
        while k < len(pattern):
            c = pattern[k]
            if c == "\\":
                k += 2
                continue
            if in_class:
                in_class = c != "]"
            elif c == "[":
                in_class = True
                if pattern[k + 1:k + 2] == "]":
                    k += 1             # "[]...]": the first ] is a literal
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if depth:
            return None
        out.append(" ")
        i = k + 1
    return "".join(out) + pattern[i:]


def _regex_literals(pattern: str) -> list[str]:
    """Lowercased [a-z0-9_] runs every match of `pattern` must contain, or []
    when that cannot be decided cheaply (alternation, optional groups)."""
    if "|" in pattern or _UNSAFE_REGEX.search(pattern) or _VERBOSE_FLAG.search(pattern):
        return []
    pattern = _drop_groups(pattern, _UNMATCHED_GROUP)
    if pattern is None:
        return []
    pattern = _COUNTED.sub("*", _NOT_TEXT.sub(" ", pattern))
    s = re.sub(r"\\x[0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}|\\N\{[^}]*\}|\\[0-7]{1,3}", " ", pattern)
    s = re.sub(r"\[\^?\]?(?:\\.|[^\]])*\]", " ", s)    # "[]x]": the first ] is a literal
    s = re.sub(r"\\.", " ", s)
    out = []
    for m in re.finditer(r"[A-Za-z0-9_]+", s):
        run = m.group()
        if s[m.end():m.end() + 1] in ("?", "*", "{"):
            run = run[:-1]           # the last character is optional
        if len(run) >= 3:
            out.append(run.lower())
    return out


def _text_mentions(index: ProjectIndex, query: str, keep, limit: int = 5) -> str:
    """'README.md ×7, harness/commands.py ×3' — the files whose text contains
    `query` (case-insensitive), most first; '' when none or the query is short."""
    lits = _WORD_STR.findall(query.lower())
    ids = _trigram_ids(lits)
    if not ids or len(query) < 3:
        return ""
    needle = query.lower()
    cands = [rel for rel, _ in _text_candidates(index, ids, keep)[0]]
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


def _text_candidates(index: ProjectIndex, ids, keep) -> tuple[list[tuple[str, list]], bool]:
    """([(path, symbols)] of the files that can contain every trigram in ids,
    whether every file had to be taken)."""
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
    return cands, not use_sig


def find_line_text(index: ProjectIndex, text: str, skip: str | None = None, limit: int = 3,
                   cancel=None) -> list[tuple[str, int]]:
    """[(path, line)] of indexed files with a line containing `text` exactly,
    other than `skip` — for edit_file's "this text is in another file".  The
    caller has waited for the index to be fresh."""
    ids = _trigram_ids(_WORD_STR.findall(text.lower()))
    if not ids:
        return []           # without a trigram every file would be read
    cands, _ = _text_candidates(index, ids, lambda p: p != skip)
    out: list[tuple[str, int]] = []
    for i, (rel, _symbols) in enumerate(cands):
        if cancel is not None and i % 50 == 0 and cancel.is_set():
            break
        try:
            src = _read_source(index.root / rel)
        except OSError:
            continue
        if text not in src:
            continue
        ln = next((n for n, line in enumerate(split_lines(src), 1) if text in line), None)
        if ln is None:
            continue
        out.append((rel, ln))
        if len(out) >= limit:
            break
    return out


def index_text(query: str, path: str | None = None, regex: bool = False, *, workdir: Path,
               index: ProjectIndex | None = None, cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    if not query:
        return "ERROR: query is empty"
    path, err = _scope(path, workdir, index)
    if err:
        return err
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
    cands, scanned_all = _text_candidates(index, ids, _path_filter(path))
    hits: list[tuple[str, int, str, object]] = []
    total = 0
    files_hit: dict[str, int] = {}
    for i, (rel, symbols) in enumerate(cands):
        if cancel is not None and i % 50 == 0 and cancel.is_set():
            return "ERROR: cancelled"
        try:
            text = _read_source(index.root / rel)
        except OSError:
            continue
        for ln, line in enumerate(split_lines(text), 1):
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
    in_file = 0
    listed: set[str] = set()
    for rel, ln, line, symbols in hits:
        if rel == cur and in_file >= _MAX_TEXT_PER_FILE:
            continue                # breadth first: the other files matter more
        owner = code_nav.enclosing_symbol(symbols, ln) if symbols else None
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
        in_file = 1 if rel != cur else in_file + 1
        cur = rel
        listed.add(rel)
        if in_file == _MAX_TEXT_PER_FILE and files_hit[rel] > in_file:
            more_here = files_hit[rel] - in_file
            out.append(f"  ... {more_here} more in this file")
            size += len(out[-1]) + 1
    if total > shown:
        rest = sorted((f for f in files_hit if f not in listed), key=lambda f: -files_hit[f])
        more = (" — also in: " + ", ".join(f"{f} ({files_hit[f]})" for f in rest[:8])
                + (" ..." if len(rest) > 8 else "")) if rest else ""
        out.append(f"... (first {shown} of {total} matches{more} — narrow with path= or a "
                   f"longer query)")
    if not ids:
        out.append("(note: the query has no 3-letter word, so every indexed file was scanned)")
    first = hits[0]
    out.append(f'(next: read_symbol("{first[0]}", "L{first[1]}") reads the definition around a hit)')
    return _with_note("\n".join(out), index, "trigrams")
