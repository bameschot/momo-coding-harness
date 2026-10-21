"""todo_count: an example workspace tool for momo (put it in .momo/tools/).

Count the TODO / FIXME comments per file under a folder of the project.
Standard library only, read-only, bounded output.
"""
import os
from pathlib import Path

TOOL = {
    "name": "todo_count",
    "description": "Count the TODO and FIXME comments per file under a folder of the project, "
                   "most first.",
    "parameters": {
        "path": {"type": "string", "description": "Folder or file to scan (default: the whole project)"},
        "limit": {"type": "integer", "description": "Files to list (default 20)"},
    },
    "required": [],
    "category": "read",
    "example": {"path": "src", "limit": 10},
    "timeout": 60,
}

_SKIP = {".git", ".momo", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
_MAX_BYTES = 2_000_000


def run(path: str = ".", limit: int = 20, *, workdir: Path) -> str:
    root = (workdir / path).resolve()
    if root != workdir.resolve() and workdir.resolve() not in root.parents:
        return f"ERROR: {path} is outside the project — pass a path inside it, e.g. 'src'"
    if not root.exists():
        return f"ERROR: {path} does not exist — pass a folder or file in the project"
    files = [root] if root.is_file() else _walk(root)
    counts = []
    for f in files:
        try:
            if f.stat().st_size > _MAX_BYTES:
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        n = sum(1 for line in text.splitlines() if "TODO" in line or "FIXME" in line)
        if n:
            counts.append((n, f.relative_to(workdir.resolve()).as_posix()))
    if not counts:
        return f"No TODO or FIXME comments under {path}."
    counts.sort(key=lambda c: (-c[0], c[1]))
    limit = max(1, min(limit, 200))
    lines = [f"{n:4}  {rel}" for n, rel in counts[:limit]]
    total = sum(n for n, _ in counts)
    more = f"\n… and {len(counts) - limit} more files (raise limit to see them)" if len(counts) > limit else ""
    return f"{total} TODO/FIXME comments in {len(counts)} files:\n" + "\n".join(lines) + more


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP)
        for name in sorted(filenames):
            yield Path(dirpath) / name
