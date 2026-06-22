import os
import re
import shlex
import subprocess
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
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune ignored directories in-place so os.walk won't descend into them
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            full = Path(dirpath) / fname
            # support both "*.py" (filename match) and "**/*.py" / "src/*.py" (path match)
            rel = str(full.relative_to(root))
            if full.match(pattern) or full.name == pattern or Path(rel).match(pattern):
                try:
                    matches.append(str(full.relative_to(workdir)))
                except ValueError:
                    matches.append(str(full))
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
    "find_files": _find_files,
    "read_file": _read_file,
    "grep_file": _grep_file,
    "grep_files": _grep_files,
    "edit_file": _edit_file,
    "create_file": _create_file,
    "delete_file": _delete_file,
    "git_command": _git_command,
    "run_command": _run_command,
}


def dispatch(name: str, args: dict, workdir: Path) -> str:
    fn = _EXECUTORS.get(name)
    if fn is None:
        return f"ERROR: unknown tool '{name}'"
    try:
        return fn(**args, workdir=workdir)
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
