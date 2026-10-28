"""Limits and helpers shared by several tool files (the file and search tools,
index hints, edit_files' argument parsing)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..paths import BINARY_SNIFF_BYTES, MAX_SCAN_FILE_BYTES, safe_path

# ── limits (quoted by the schema descriptions) ───────────────────────────────

MAX_FIND_RESULTS = 100
MAX_GREP_RESULTS = 200
MAX_GREP_LINE_CHARS = 300        # a longer hit line (minified code) is clipped around the match
MAX_SCAN_MB = MAX_SCAN_FILE_BYTES // 1_000_000

# Syntax-aware navigation (tree-sitter): the languages the code-nav tools name.
CODE_NAV_LANGS = ("Python, Java, C/C++, Kotlin, Rust, JS/TS; also YAML/TOML/JSON keys, HTML, "
                  "CSS, SQL, shell, Dockerfiles")


def read_text(path: str, workdir: Path, *, refuse_binary: bool = False) -> tuple[Path, str] | str:
    """(resolved path, text) of a file in the workdir, or an ERROR string.
    With refuse_binary, a file with a NUL byte near the start is an ERROR too."""
    p = safe_path(path, workdir)
    if isinstance(p, str):
        return p
    if p.is_dir():
        return f"ERROR: {path} is a directory — use list_directory to see what it contains"
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except OSError as e:
        return f"ERROR: {e}"
    if refuse_binary and b"\x00" in raw[:BINARY_SNIFF_BYTES]:
        return (f"ERROR: {path} is a binary file ({len(raw):,} bytes), not text — "
                f"file_info shows its size and type")
    return p, raw.decode("utf-8", errors="replace")


# ── search results ───────────────────────────────────────────────────────────

def clip_hit(line: str, m: re.Match) -> str:
    """A grep hit line, clipped to a window around the match when it is long."""
    if len(line) <= MAX_GREP_LINE_CHARS:
        return line
    mid = (m.start() + m.end()) // 2
    start = max(0, min(mid - MAX_GREP_LINE_CHARS // 2, len(line) - MAX_GREP_LINE_CHARS))
    end = start + MAX_GREP_LINE_CHARS
    return (("…" if start else "") + line[start:end] + ("…" if end < len(line) else "")
            + f" [line is {len(line):,} chars]")


def capped(hits: list[str], total: int, advice: str) -> str:
    """Hit lines, with a note when the cap cut some off."""
    if not hits:
        return "(no matches)"
    more = f"\n... (first {len(hits)} of {total} matches — {advice})" if total > len(hits) else ""
    return "\n".join(hits) + more


def grep_one(pattern: str, path: str, workdir: Path):
    """(text, compiled regex) for the single-file greps, or an ERROR string."""
    got = read_text(path, workdir, refuse_binary=True)
    if isinstance(got, str):
        return got
    try:
        return got[1], re.compile(pattern)
    except re.error as e:
        return f"ERROR: invalid regex: {e}"


# ── the code index, from file-based tools ────────────────────────────────────

INDEX_GREP_TIP = ("\n(index_text runs this search from the code index and names the definition "
                  "each hit is in)")
_REGEX_ONLY = set("^$*+?{}[]|()")


def literal(pattern: str) -> str | None:
    r"""The text a grep pattern searches for when it is plain text, else None.
    `\.` and a dot between words count as a literal dot (`os.path`)."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            if i + 1 < len(pattern) and not pattern[i + 1].isalnum():
                out.append(pattern[i + 1])
                i += 2
                continue
            return None                       # \d, \w, \b ...
        if c in _REGEX_ONLY:
            return None
        if c == "." and (i + 1 < len(pattern) and pattern[i + 1] in "*+?{"):
            return None
        out.append(c)
        i += 1
    return "".join(out) or None


def index_target(raw, workdir: Path, index) -> tuple[Path, str] | None:
    """(resolved path, path relative to the index root) of a tool's path
    argument, or None when it is outside the workdir or the index."""
    p = safe_path(str(raw or "."), workdir)
    if isinstance(p, str):
        return None
    rel = index.rel(p)
    return None if rel is None else (p, rel)


def dir_map_hint(result: str, args: dict, workdir: Path, index) -> str:
    """list_directory / code_outline on an indexed folder: point at index_map,
    which ranks the files and names their main definitions (the same in-situ
    hint mechanism as read_file's footer)."""
    if index is None or result.startswith("ERROR"):
        return result
    got = index_target(args.get("path"), workdir, index)
    if got is None:
        return result
    p, rel = got
    if not p.is_dir() or not index.has_files_under(rel):
        return result
    arg = "" if rel == "." else f'path="{rel}"'
    return result + (f"\n(code index: index_map({arg}) lists these files ranked by how much the "
                     f"project uses them, with their main definitions)")


# ── edit_files' argument ─────────────────────────────────────────────────────

def edit_list(edits) -> list:
    """edit_files' edits as a list, also when they came as a JSON string."""
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except ValueError:
            return []
    return edits if isinstance(edits, list) else [edits] if isinstance(edits, dict) else []


def edit_paths(args: dict) -> list[str]:
    return [e["path"] for e in edit_list(args.get("edits"))
            if isinstance(e, dict) and isinstance(e.get("path"), str)]
