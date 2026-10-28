# Skill: Writing momo workspace tools

A workspace tool is a Python file in `.momo/tools/` of a project. momo offers it to its model as a tool, next to the built-in ones. The file is committed with the project, so everyone who opens the project in momo gets it. Follow these rules exactly: momo refuses a file that breaks them, and says why.

## How momo treats the file

- momo reads the file's `TOOL = {...}` literal **without running the file**. It runs `run()` only when the model calls the tool.
- **A tool never runs before the user approves it.** After you write or change any file in `.momo/tools/`, every tool there needs approval again. You cannot approve a tool yourself, and you must not try to work around the approval. End your work by telling the user: "Review `.momo/tools/<name>.py`, then run `/tools approve <name>`."
- Every call runs in a **separate Python process**: `python -I -B`, with the project folder as the current directory. The harness is not importable there. Nothing is kept between calls.
- momo never indexes `.momo/tools/` and never searches it.

## The file

One tool per file. The file name is the tool name: `.momo/tools/<name>.py`. Files starting with `_` are helpers: tools may `import` them, and momo never loads them as tools. Do not import one tool file from another; share code through a `_helper.py`.

```python
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
```

### `TOOL`: a plain literal

`TOOL` must be a dict literal at module level: strings, numbers, booleans, lists and dicts only. Names, calls, f-strings and constants defined elsewhere are not allowed, because momo reads the dict without running the file. These are the only keys:

| Key | Type | Required | Meaning |
|---|---|---|---|
| `name` | str | yes | The tool name: a-z, 0-9 and `_`, starting with a letter, 2-64 characters, equal to the file name. It must not start with `mcp_` or be a name momo already has. |
| `description` | str | yes | What the tool is for, **in the first sentence**, because a small model picks tools by it. Then, briefly, what it returns. |
| `parameters` | dict | yes | JSON-schema properties: `{"arg": {"type": "string", "description": "..."}}`. Types: string, integer, number, boolean, array, object. Use `{}` for no arguments. |
| `required` | list | no | The parameter names the model must pass. Default: none. |
| `category` | str | no | `read`, `write`, `run` or `net` (see below). Missing or unknown counts as `write`. |
| `example` | dict | no | One realistic call's arguments. They appear in the model's tool reference. |
| `order` | int | no | Its position in the tool list. Default 9500. |
| `timeout` | int | no | Seconds before the call is killed, 1-900. Default 120. |
| `confirm` | bool | no | `true`: momo asks the user before every call. Use it for anything irreversible or external. |
| `discoverable` | bool | no | With `/tools discover extensions` or `all` (off by default), a workspace tool is listed in the prompt's "More tools" catalog and declared only once the model loads it (`find_tools`). `false` keeps it declared even then: use that for a tool most tasks in this project need. |

### `run`

```python
def run(<the parameters, with defaults for the optional ones>, *, workdir: Path) -> str:
```

- It takes exactly the names in `parameters`, as keyword arguments. Give optional ones a default.
- `workdir` is the project folder (a `pathlib.Path`) and also the current directory. `os.environ["MOMO_SCRATCH"]` is the scratchpad for temporary files.
- It returns a **str**, which is all the model sees. Returning anything else is an error.
- An exception becomes an `ERROR:` result with the last lines of the traceback. Return a clear `ERROR: ...` for expected failures instead.

## Categories decide where the tool is offered

| Category | Use it when the tool… | Offered in |
|---|---|---|
| `read` | only reads (files, git, metadata) | every mode, including chat and planning |
| `write` | creates, changes or deletes files | coding, momo, a running plan |
| `run` | runs programs, tests or builds | coding, momo, a running plan, plan investigation |
| `net` | talks to the network | every mode, but only while the user has `/net on` |

Declare the **narrowest true** category. A tool that writes even one file is `write`, even if it mostly reads. Network access goes with `net`, never with `read`. The user can change roles per tool; that is not your job. Their settings live in `.momo/tools.json`: never edit that file to widen a tool's roles.

## Rules

1. **No harness imports.** `import harness` fails. Use the standard library, the packages in `.momo/tools/requirements.txt`, and your own `_helper.py` files.
2. **Stay in the project.** Resolve every path against `workdir`, and refuse paths that end up outside it (as `run` does above). Never touch `~/.momo-harness/` or momo's own files.
3. **No input.** No stdin is connected. Never prompt; take everything as parameters.
4. **Short, bounded results.** Cap lists and output, say what was left out, and say how to get more. A small model has a small context.
5. **Errors the model can act on.** Start with `ERROR:` and say what to do instead, e.g. `ERROR: src/x.py does not exist — pass a file in the project`.
6. **Finish in time.** Stay well inside `timeout`. On Esc momo kills the process and everything it started, so don't rely on cleanup code. Use `MOMO_SCRATCH` for temporary files.
7. **`print()` is not the result.** Printed text goes to a log, not to the model. Return the text instead.
8. **No secrets in the file.** It is committed. Read credentials from the environment, and only in a `net` tool.

## Dependencies

1. Add the package to `.momo/tools/requirements.txt` (pip format, one per line).
2. Ask the user to run `/tools venv`. It creates `.momo/tools/.venv`, which is not committed, and installs the requirements into it. Tools then run with that Python, and the user approves them again.
3. If the project already has a virtualenv with what the tool needs, the user can set `"python": ".venv/bin/python"` in `.momo/tools.json` instead.

A missing package comes back to the model as an `ERROR:` that names it and points at `/tools venv`.

## Check before you finish

1. `/tools check <name>` (or ask the user to run it) validates the file and the interpreter without running anything.
2. Try `run()` directly from the project folder:

   ```sh
   python3 -I -B -c "import sys; from pathlib import Path; sys.path.insert(0, '.momo/tools'); import todo_count; print(todo_count.run(path='src', workdir=Path.cwd()))"
   ```

3. Go through the checklist:
   - the file name equals `TOOL["name"]`;
   - `TOOL` is a literal using only the keys above;
   - the description's first sentence says what the tool is for;
   - the category is the narrowest true one;
   - `run` takes the parameters plus `*, workdir` and returns a str;
   - paths are checked against `workdir`;
   - the output is capped;
   - errors start with `ERROR:`;
   - new packages are in `requirements.txt`.
4. Tell the user to review the file and run `/tools approve <name>`. If you added packages, tell them to run `/tools venv` first.
