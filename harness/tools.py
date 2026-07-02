import os
import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path


# ── schema helpers ───────────────────────────────────────────────────────────

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


READ_ONLY_TOOLS = [
    _fn("list_directory",
        "List the contents of a directory. Returns '[D] name/' for subdirectories "
        "and '[F] name  (N bytes)' for files, sorted dirs first then files.",
        {"path":        {"type": "string",  "description": "Directory to list (default: .)"},
         "show_hidden": {"type": "boolean", "description": "Include hidden entries starting with '.' (default: false)"}},
        []),

    _fn("file_info",
        "Return metadata for a path: existence, type (file/directory/symlink), "
        "size in bytes, last-modified timestamp, and line count for text files.",
        {"path": {"type": "string"}},
        ["path"]),

    _fn("find_files", "Find files matching a glob pattern under a directory",
        {"pattern": {"type": "string", "description": "Glob pattern, e.g. '*.py'"},
         "directory": {"type": "string", "description": "Directory to search (default: .)"}},
        ["pattern"]),

    _fn("read_file", "Read a file, optionally restricting to a line range",
        {"path": {"type": "string"},
         "start_line": {"type": "integer", "description": "1-based start line (default: 1)"},
         "end_line": {"type": "integer", "description": "1-based end line inclusive (default: EOF)"}},
        ["path"]),

    _fn("grep_file", "Single-file regex search. Search for a regex pattern in one file, returns matching lines with line numbers.",
        {"pattern": {"type": "string"}, "path": {"type": "string"}},
        ["pattern", "path"]),

    _fn("grep_files", "Multi-file recursive search. Search for a regex pattern across all files in a directory, returns file:line:content hits.",
        {"pattern": {"type": "string"},
         "directory": {"type": "string", "description": "Directory to search (default: .)"}},
        ["pattern"]),

    _fn("grep_extract",
        "Single-file regex extraction. Like grep_file, but returns only the matching text "
        "(or a specific capture group) rather than the whole line. Use to pull values out of "
        "structured text, e.g. extract version strings, URLs, or identifiers.",
        {"pattern": {"type": "string", "description": "Regex; use a capture group to extract part of the match"},
         "path":    {"type": "string"},
         "group":   {"type": "integer", "description": "Capture group to return (default: 0 = whole match)"}},
        ["pattern", "path"]),

]

CODING_ONLY_TOOLS = [
    _fn("move_file",
        "Move or rename a file. Both source and destination must be inside the working directory. "
        "Parent directories of the destination are created automatically.",
        {"src": {"type": "string", "description": "Current path"},
         "dst": {"type": "string", "description": "Target path"}},
        ["src", "dst"]),

    _fn("append_to_file",
        "Add text to the END of an existing file (creates it if absent). "
        "Takes ONLY path and content. It cannot change existing text — to replace text "
        "inside a file use edit_file; to overwrite the whole file use write_file.",
        {"path":    {"type": "string"},
         "content": {"type": "string", "description": "Text to add at the end of the file."}},
        ["path", "content"]),

    _fn("edit_file",
        "Change text INSIDE an existing file: replace old_string with new_string. "
        "Use this tool (not write_file) whenever you are modifying part of a file you have read. "
        "By default it replaces exactly one occurrence and fails if old_string is not found "
        "exactly once; set replace_all=true to replace every occurrence (e.g. renaming a symbol "
        "throughout the file). "
        "Takes path, old_string, new_string, and optional replace_all — it does NOT take a "
        "'content' argument (that is write_file).",
        {"path": {"type": "string"},
         "old_string": {"type": "string", "description": "Exact text to find (copy it verbatim from read_file output)"},
         "new_string": {"type": "string", "description": "Replacement text"},
         "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring exactly one (default: false)"}},
        ["path", "old_string", "new_string"]),

    _fn("delete_file", "Delete a file",
        {"path": {"type": "string"}},
        ["path"]),

    _fn("run_command",
        "Run a shell command from the working directory. Returns stdout and stderr. "
        "Use for running scripts, tests, build tools, etc. The command runs with the "
        "working directory as its current directory.",
        {"command": {"type": "string", "description": "Shell command to execute (runs in the working directory)"},
         "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30)"}},
        ["command"]),
]

SHARED_TOOLS = [
    _fn("write_file",
        "Create a NEW file, or COMPLETELY overwrite an existing one, with the given content. "
        "Takes ONLY path and content. It cannot change part of a file and does NOT accept "
        "old_string/new_string — to replace specific text in an existing file, use edit_file instead. "
        "Use this to save any file: scripts (.py, .sh), data files (.csv, .json, .jsonl), "
        "documents (.md, .txt), or any other content. "
        "The content parameter must contain the raw file content exactly as it should appear on disk — "
        "do NOT wrap code in markdown code fences (no ```python or ``` markers) unless the target "
        "file is itself a Markdown document.",
        {"path":    {"type": "string", "description": "Destination file path, including the correct extension for the file type (e.g. 'process.py', 'output.csv', 'report.md')."},
         "content": {"type": "string", "description": "Raw file content to write. For scripts, this is source code only — no surrounding markdown."}},
        ["path", "content"]),

    _fn("ask_user",
        "Pause and ask the user a clarifying question mid-task. "
        "Use this when you genuinely cannot determine the answer from the code or context — "
        "e.g. which of two approaches to take, confirmation before a destructive action, "
        "or a preference the user has not expressed. "
        "Do NOT use this for things discoverable by reading files. "
        "Ask one focused question per call and continue after receiving the answer.",
        {"question": {"type": "string",
                      "description": "The question to present to the user"}},
        ["question"]),
]

DESIGN_TOOLS = READ_ONLY_TOOLS + SHARED_TOOLS
ALL_TOOLS    = READ_ONLY_TOOLS + SHARED_TOOLS + CODING_ONLY_TOOLS

# Derived subset for writer mode
_by_name = {t["function"]["name"]: t for t in CODING_ONLY_TOOLS}

WRITER_TOOLS = READ_ONLY_TOOLS + SHARED_TOOLS + [
    _by_name["append_to_file"],
    _by_name["edit_file"],
]

_shared_by_name = {t["function"]["name"]: t for t in SHARED_TOOLS}
CHAT_TOOLS = READ_ONLY_TOOLS + [_shared_by_name["ask_user"]]


# ── path safety ───────────────────────────────────────────────────────────────

def _safe_path(raw: str, workdir: Path) -> Path | str:
    p = (workdir / raw).resolve()
    try:
        p.relative_to(workdir.resolve())
    except ValueError:
        return "ERROR: path outside working directory"
    return p


# ── executors ────────────────────────────────────────────────────────────────

_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", "dist", "build", ".mypy_cache", ".pytest_cache"}

_MAX_FIND_RESULTS  = 100
_MAX_GREP_RESULTS  = 200
_READ_FOOTER_LINES = 200  # show footer when file exceeds this length and no range given
_MAX_GREP_FILE_BYTES = 2_000_000  # skip files larger than this in recursive grep
_BINARY_SNIFF_BYTES  = 4096       # bytes inspected for a NUL byte to detect binary files

def _find_files(pattern: str, directory: str = ".", *, workdir: Path) -> str:
    root = _safe_path(directory, workdir)
    if isinstance(root, str):
        return root
    # Bare filename patterns (no slash, no **) are promoted to recursive so
    # "*.py" behaves the same as "**/*.py" without the caller needing to know.
    glob_pat = pattern if ("/" in pattern or pattern.startswith("**")) else f"**/{pattern}"
    try:
        gen = root.glob(glob_pat)
    except ValueError as e:
        return f"ERROR: invalid pattern: {e}"
    matches = []
    for p in gen:
        if not p.is_file():
            continue
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        try:
            matches.append(str(p.relative_to(workdir)))
        except ValueError:
            matches.append(str(p))
    if not matches:
        return "(no matches)"
    total = len(matches)
    matches = sorted(matches)
    if total > _MAX_FIND_RESULTS:
        return "\n".join(matches[:_MAX_FIND_RESULTS]) + f"\n... (first {_MAX_FIND_RESULTS} of {total} files — use a more specific pattern or directory)"
    return "\n".join(matches)


def _read_file(path: str, start_line: int = 1, end_line: int | None = None, *, workdir: Path) -> str:
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except OSError as e:
        return f"ERROR: {e}"
    n = len(lines)
    s = max(0, start_line - 1)
    e = end_line if end_line is not None else n
    chunk = lines[s:e]
    numbered = "".join(f"{s + i + 1:4}: {l}" for i, l in enumerate(chunk))
    if not numbered:
        return "(empty)"
    if end_line is None and n > _READ_FOOTER_LINES:
        numbered += f"\n[{n} lines total — use start_line/end_line to read specific sections]"
    return numbered


def _grep_file(pattern: str, path: str, *, workdir: Path) -> str:
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except OSError as e:
        return f"ERROR: {e}"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: invalid regex: {e}"
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if rx.search(line):
            hits.append(f"{i:4}: {line}")
    return "\n".join(hits) if hits else "(no matches)"


def _grep_extract(pattern: str, path: str, group: int = 0, *, workdir: Path) -> str:
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except OSError as e:
        return f"ERROR: {e}"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: invalid regex: {e}"
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in rx.finditer(line):
            try:
                extracted = m.group(group)
            except IndexError:
                return f"ERROR: group {group} does not exist in pattern"
            if extracted is not None:
                hits.append(f"{i:4}: {extracted}")
    return "\n".join(hits) if hits else "(no matches)"


def _grep_files(pattern: str, directory: str = ".", *, workdir: Path) -> str:
    root = _safe_path(directory, workdir)
    if isinstance(root, str):
        return root
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: invalid regex: {e}"
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            # Skip oversized files: scanning them is slow and rarely useful.
            try:
                if fpath.stat().st_size > _MAX_GREP_FILE_BYTES:
                    continue
            except OSError:
                continue
            try:
                raw = fpath.read_bytes()
            except OSError:
                continue
            # Skip binaries: a NUL byte in the first chunk is a reliable, cheap
            # signal, and avoids polluting results with garbage decoded matches.
            if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
                continue
            text = raw.decode("utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    try:
                        rel = fpath.relative_to(workdir)
                    except ValueError:
                        rel = fpath
                    results.append(f"{rel}:{i}: {line}")
    if not results:
        return "(no matches)"
    total = len(results)
    if total > _MAX_GREP_RESULTS:
        return "\n".join(results[:_MAX_GREP_RESULTS]) + f"\n... (first {_MAX_GREP_RESULTS} of {total} matches — narrow the pattern or specify a directory)"
    return "\n".join(results)


def _list_directory(path: str = ".", show_hidden: bool = False, *, workdir: Path) -> str:
    root = _safe_path(path, workdir)
    if isinstance(root, str):
        return root
    if not root.is_dir():
        return f"ERROR: not a directory: {path}"
    try:
        entries = list(os.scandir(root))
    except OSError as e:
        return f"ERROR: {e}"
    dirs  = sorted([e for e in entries if e.is_dir(follow_symlinks=False)],  key=lambda e: e.name.lower())
    files = sorted([e for e in entries if not e.is_dir(follow_symlinks=False)], key=lambda e: e.name.lower())
    lines = []
    for e in dirs + files:
        if not show_hidden and e.name.startswith("."):
            continue
        if e.is_dir(follow_symlinks=False):
            lines.append(f"[D] {e.name}/")
        else:
            try:
                size = e.stat().st_size
            except OSError:
                size = 0
            lines.append(f"[F] {e.name}  ({size:,} bytes)")
    return "\n".join(lines) if lines else "(empty)"


def _file_info(path: str, *, workdir: Path) -> str:
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    if not p.exists() and not p.is_symlink():
        return f"exists:   no\npath:     {path}"
    try:
        st = p.lstat()
    except OSError as e:
        return f"ERROR: {e}"
    if p.is_symlink():
        kind = "symlink"
    elif p.is_dir():
        kind = "directory"
    else:
        kind = "file"
    mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"exists:   yes",
        f"type:     {kind}",
        f"size:     {st.st_size:,} bytes",
        f"modified: {mtime}",
    ]
    if kind == "file":
        try:
            text = p.read_text(encoding="utf-8", errors="strict")
            lines.append(f"lines:    {len(text.splitlines())}")
        except (UnicodeDecodeError, OSError):
            lines.append("lines:    (binary)")
    return "\n".join(lines)


def _move_file(src: str, dst: str, *, workdir: Path) -> str:
    sp = _safe_path(src, workdir)
    if isinstance(sp, str):
        return sp
    dp = _safe_path(dst, workdir)
    if isinstance(dp, str):
        return dp
    if not sp.exists():
        return f"ERROR: source not found: {src}"
    try:
        dp.parent.mkdir(parents=True, exist_ok=True)
        sp.rename(dp)
    except OSError as e:
        return f"ERROR: {e}"
    return "OK"


def _append_to_file(path: str, content: str, *, workdir: Path) -> str:
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"ERROR: {e}"
    return "OK"


def _edit_file(path: str, old_string: str, new_string: str,
               replace_all: bool = False, *, workdir: Path) -> str:
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except OSError as e:
        return f"ERROR: {e}"
    count = content.count(old_string)
    if count == 0:
        return "ERROR: old_string not found in file"
    if replace_all:
        p.write_text(content.replace(old_string, new_string), encoding="utf-8")
        return f"Replaced {count} occurrence(s)"
    # Default: require exactly one match so the model can't accidentally replace
    # the wrong occurrence when the same string appears multiple times.
    if count > 1:
        return f"ERROR: old_string found {count} times; must match exactly once (set replace_all=true to replace all)"
    p.write_text(content.replace(old_string, new_string, 1), encoding="utf-8")
    return "OK"


def _write_file(path: str, content: str, *, workdir: Path) -> str:
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"ERROR: {e}"
    # "Written: <path>" — the harness inspects last_tool by name (not return value)
    # but the model uses this confirmation to know the write succeeded.
    return f"Written: {path}"


def _delete_file(path: str, *, workdir: Path) -> str:
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    try:
        p.unlink()
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except OSError as e:
        return f"ERROR: {e}"
    return "OK"



def _run_command(command: str, timeout: int = 30, *, workdir: Path) -> str:
    try:
        r = subprocess.run(
            command, shell=True, cwd=workdir,
            capture_output=True, text=True, timeout=timeout,
        )
        parts = []
        if r.stdout.strip():
            parts.append(r.stdout.strip())
        if r.stderr.strip():
            parts.append(f"[stderr]\n{r.stderr.strip()}")
        if r.returncode != 0:
            parts.append(f"[exit code: {r.returncode}]")
        return "\n".join(parts) or "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except OSError as e:
        return f"ERROR: {e}"


# ── dispatch ─────────────────────────────────────────────────────────────────

# Required- and known-argument maps built from the tool schemas — used to generate
# clear error messages before Python's TypeError exposes internal function names.
_REQUIRED_ARGS: dict[str, list[str]] = {}
_KNOWN_ARGS: dict[str, set[str]] = {}
for _tl in (READ_ONLY_TOOLS, SHARED_TOOLS, CODING_ONLY_TOOLS, WRITER_TOOLS):
    for _t in _tl:
        _tname = _t["function"]["name"]
        _props = _t["function"]["parameters"].get("properties", {})
        _req   = _t["function"]["parameters"].get("required", [])
        if _tname not in _KNOWN_ARGS:
            _KNOWN_ARGS[_tname] = set(_props.keys())
        if _req and _tname not in _REQUIRED_ARGS:
            _REQUIRED_ARGS[_tname] = _req

_EXECUTORS = {
    "list_directory":     _list_directory,
    "file_info":          _file_info,
    "find_files":         _find_files,
    "read_file":          _read_file,
    "grep_file":          _grep_file,
    "grep_files":         _grep_files,
    "grep_extract":       _grep_extract,
    "write_file":          _write_file,
    "move_file":          _move_file,
    "append_to_file":     _append_to_file,
    "edit_file":          _edit_file,
    "delete_file":        _delete_file,
    "run_command":        _run_command,
}


def dispatch(name: str, args: dict, workdir: Path) -> str:
    fn = _EXECUTORS.get(name)
    if fn is None:
        return f"ERROR: unknown tool '{name}'"

    # Auto-route a common small-model confusion: write_file called with edit-style
    # old_string/new_string and no content clearly means an edit — run edit_file so
    # the model's intent succeeds instead of failing on an unexpected argument.
    if (name == "write_file" and "old_string" in args and "new_string" in args
            and "content" not in args and "path" in args):
        routed = {k: args[k] for k in ("path", "old_string", "new_string", "replace_all")
                  if k in args}
        return "(note: routed write_file to edit_file) " + dispatch("edit_file", routed, workdir)

    required = _REQUIRED_ARGS.get(name, [])
    missing = [r for r in required if r not in args]
    if missing:
        return (
            f"ERROR: {name} called without required argument(s): {', '.join(missing)}. "
            f"Required: {', '.join(required)}. Retry the call with all required arguments."
        )

    # Reject arguments the tool does not declare, with a clear message and a
    # "did you mean" hint — rather than letting Python raise a TypeError that
    # leaks the internal function name (e.g. write_file given old_string).
    known = _KNOWN_ARGS.get(name, set())
    unexpected = [a for a in args if a not in known]
    if unexpected:
        hint = ""
        if {"old_string", "new_string"} & set(unexpected):
            hint = " To change part of an existing file, use edit_file (old_string/new_string)."
        elif "content" in unexpected and name in ("edit_file",):
            hint = " To create or overwrite a whole file, use write_file (path/content)."
        return (
            f"ERROR: {name} does not accept argument(s): {', '.join(unexpected)}. "
            f"Valid arguments: {', '.join(sorted(known))}.{hint}"
        )

    try:
        return fn(**args, workdir=workdir)
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
