"""One call of a workspace tool, in its own process.

momo never imports this module to run a tool: tools/workspace.py passes its
source to `python -I -B -c`, with the workdir as current directory.  -I
leaves the current directory, the script's folder, PYTHONPATH and the user
site off sys.path, so nothing of the harness can be imported; -B writes no
__pycache__.  It uses the standard library only.

The request comes as JSON on stdin: {"source": the tool file's text, as
approved, "path": its path, "args": the call's arguments}.  The source is run
from the request, not read from disk again, so what runs is exactly what the
user approved.  The tool's folder goes on sys.path for its `_helper` imports.

The reply goes to the process's real stdout as JSON: {"result": text} or
{"error": text, "traceback": text, "missing_module": name or null}.  While the
tool runs, stdout (also its child processes' fd 1) points at stderr, so a
stray print() cannot corrupt the reply.
"""
import json
import os
import sys
import traceback
import types
from pathlib import Path


def main() -> None:
    request = json.loads(sys.stdin.read() or "{}")
    reply_to = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def reply(obj: dict) -> None:
        reply_to.write(json.dumps(obj))
        reply_to.flush()

    path = Path(request["path"])
    sys.path.insert(0, str(path.parent))
    module = types.ModuleType(path.stem)
    module.__file__ = str(path)
    sys.modules[path.stem] = module
    try:
        exec(compile(request["source"], str(path), "exec"), module.__dict__)
        result = module.run(**request.get("args", {}), workdir=Path.cwd())
    except BaseException as e:          # KeyboardInterrupt / SystemExit too: report, don't vanish
        reply({"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc(),
               "missing_module": e.name if isinstance(e, ModuleNotFoundError) else None})
        return
    if not isinstance(result, str):
        reply({"error": f"run() returned {type(result).__name__}, not a str", "traceback": "",
               "missing_module": None})
        return
    reply({"result": result})


if __name__ == "__main__":
    main()
