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

    _fn("grep_file", "Search for a regex pattern in a single file, returns matching lines",
        {"pattern": {"type": "string"}, "path": {"type": "string"}},
        ["pattern", "path"]),

    _fn("grep_files", "Recursively search for a regex pattern across all files in a directory",
        {"pattern": {"type": "string"},
         "directory": {"type": "string", "description": "Directory to search (default: .)"}},
        ["pattern"]),
]

CODING_ONLY_TOOLS = [
    _fn("move_file",
        "Move or rename a file. Both source and destination must be inside the working directory. "
        "Parent directories of the destination are created automatically.",
        {"src": {"type": "string", "description": "Current path"},
         "dst": {"type": "string", "description": "Target path"}},
        ["src", "dst"]),

    _fn("append_to_file",
        "Append text to the end of a file. Creates the file if it does not exist.",
        {"path":    {"type": "string"},
         "content": {"type": "string"}},
        ["path", "content"]),

    _fn("replace_all_in_file",
        "Replace every occurrence of old_string with new_string in a file. "
        "Returns the number of replacements made. "
        "Use for renaming a variable or symbol throughout a file; "
        "use edit_file instead when the change should apply to exactly one location.",
        {"path":       {"type": "string"},
         "old_string": {"type": "string"},
         "new_string": {"type": "string"}},
        ["path", "old_string", "new_string"]),

    _fn("edit_file",
        "Replace an exact occurrence of old_string with new_string in a file. "
        "Fails if old_string is not found exactly once.",
        {"path": {"type": "string"},
         "old_string": {"type": "string", "description": "Exact text to find (must match exactly once)"},
         "new_string": {"type": "string", "description": "Replacement text"}},
        ["path", "old_string", "new_string"]),

    _fn("create_file", "Create or overwrite a file with given content",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"]),

    _fn("delete_file", "Delete a file",
        {"path": {"type": "string"}},
        ["path"]),

    _fn("git_command", "Run a git command. Provide args as a string, e.g. 'status' or 'add src/foo.py'",
        {"args": {"type": "string", "description": "git subcommand and arguments"}},
        ["args"]),

    _fn("run_command",
        "Run an arbitrary shell command. Returns stdout and stderr. "
        "Use for running scripts, tests, build tools, etc.",
        {"command": {"type": "string", "description": "Shell command to execute"},
         "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30)"}},
        ["command"]),
]

ALL_TOOLS = READ_ONLY_TOOLS + CODING_ONLY_TOOLS


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

def _find_files(pattern: str, directory: str = ".", *, workdir: Path) -> str:
    root = _safe_path(directory, workdir)
    if isinstance(root, str):
        return root
    # Simple filename patterns (no path separator, no **) are made recursive
    # so "*.py" behaves the same as "**/*.py" — finds all matches in the tree.
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
    return "\n".join(sorted(matches)) if matches else "(no matches)"


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
    s = max(0, start_line - 1)
    e = end_line if end_line is not None else len(lines)
    chunk = lines[s:e]
    numbered = "".join(f"{s + i + 1:4}: {l}" for i, l in enumerate(chunk))
    return numbered or "(empty)"


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
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    try:
                        rel = fpath.relative_to(workdir)
                    except ValueError:
                        rel = fpath
                    results.append(f"{rel}:{i}: {line}")
    return "\n".join(results) if results else "(no matches)"


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


def _replace_all_in_file(path: str, old_string: str, new_string: str, *, workdir: Path) -> str:
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
    p.write_text(content.replace(old_string, new_string), encoding="utf-8")
    return f"Replaced {count} occurrence(s)"


def _edit_file(path: str, old_string: str, new_string: str, *, workdir: Path) -> str:
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
    if count > 1:
        return f"ERROR: old_string found {count} times; must match exactly once"
    p.write_text(content.replace(old_string, new_string, 1), encoding="utf-8")
    return "OK"


def _create_file(path: str, content: str, *, workdir: Path) -> str:
    p = _safe_path(path, workdir)
    if isinstance(p, str):
        return p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"ERROR: {e}"
    return "OK"


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


def _git_command(args: str, *, workdir: Path) -> str:
    try:
        cmd = ["git"] + shlex.split(args)
    except ValueError as e:
        return f"ERROR: could not parse args: {e}"
    try:
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=30)
        out = r.stdout + r.stderr
        return out.strip() or "(no output)"
    except FileNotFoundError:
        return "ERROR: git not found"
    except subprocess.TimeoutExpired:
        return "ERROR: git command timed out"
    except OSError as e:
        return f"ERROR: {e}"


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

_EXECUTORS = {
    "list_directory":     _list_directory,
    "file_info":          _file_info,
    "find_files":         _find_files,
    "read_file":          _read_file,
    "grep_file":          _grep_file,
    "grep_files":         _grep_files,
    "move_file":          _move_file,
    "append_to_file":     _append_to_file,
    "replace_all_in_file": _replace_all_in_file,
    "edit_file":          _edit_file,
    "create_file":        _create_file,
    "delete_file":        _delete_file,
    "git_command":        _git_command,
    "run_command":        _run_command,
}


def dispatch(name: str, args: dict, workdir: Path) -> str:
    fn = _EXECUTORS.get(name)
    if fn is None:
        return f"ERROR: unknown tool '{name}'"
    try:
        return fn(**args, workdir=workdir)
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
