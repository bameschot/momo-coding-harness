"""scratch_write: write a file in the workspace's scratchpad, .momo/scratch/
(harness/momo_dir.py).  Every mode has it, the read-only ones included: the
scratchpad is not the project, and the tool cannot write anywhere else."""
from __future__ import annotations

from pathlib import Path

from ... import momo_dir
from ..registry import ALL_ROLES, Tool, tool_schema


def scratch_write(path: str, content: str, append: bool = False, *, workdir: Path) -> str:
    folder = momo_dir.ensure(workdir, momo_dir.SCRATCH)
    if isinstance(folder, str):
        return folder
    p = momo_dir.resolve_scratch(path, workdir)
    if isinstance(p, str):
        return p
    shown = f"{momo_dir.SCRATCH_REL}/{p.relative_to(folder.resolve()).as_posix()}"
    if p.is_dir():
        return f"ERROR: {shown} is a folder — name a file in it"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"ERROR: could not write {shown}: {e.strerror or e}"
    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return (f"{'Appended to' if append else 'Written:'} {shown} ({lines} line"
            f"{'s' if lines != 1 else ''}, {len(content.encode('utf-8'))} bytes)")


TOOL = Tool(
    tool_schema("scratch_write",
        f"Write a file in your scratchpad ({momo_dir.SCRATCH_REL}/) — for temporary scripts, notes "
        "and intermediate results that must not land in the project. path is relative to the "
        "scratchpad and cannot leave it; folders are created. Overwrites the file, or adds to its "
        "end with append=true. Read it back with read_file "
        f"(path \"{momo_dir.SCRATCH_REL}/<name>\").",
        {"path":    {"type": "string",  "description": "File inside the scratchpad, e.g. 'probe.py' or 'out/results.txt'"},
         "content": {"type": "string",  "description": "Raw file content (no markdown fences around code)."},
         "append":  {"type": "boolean", "description": "Add to the end of the file instead of overwriting it (default false)"}},
        ["path", "content"]),
    run=scratch_write, order=610, group="edit & shell", modes=ALL_ROLES, keeps_green=True,
    body_arg="content",
    example={"path": "probe.py", "content": "import json\nprint(json.load(open('config.json')).keys())\n"})
