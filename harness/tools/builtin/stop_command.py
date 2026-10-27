"""stop_command: stop a background run_command and everything it started."""
from __future__ import annotations

from pathlib import Path

from ... import run_store as run_store_mod
from ..registry import WRITE_ROLES, Tool, tool_schema
from ..shell import new_run_mode, no_log, read_log


def stop_command(path: str, *, workdir: Path,
                 run_store: "run_store_mod.RunStore | None" = None,
                 run_output_limit: int = run_store_mod.DEFAULT_LIMIT) -> str:
    if run_store is None:
        return "ERROR: stop_command is only available in /run-mode new"
    log = run_store.resolve(path)
    if log is None:
        return no_log(path, run_store)
    stopped = run_store.stop(log)
    status = run_store.status(log)
    head = (f"Stopped {log.stem}." if stopped else
            f"{log.stem} is not running in the background ({status or 'it has ended'}).")
    text = read_log(run_store, log)
    body = run_store_mod.view(text, run_output_limit, tail_n=20) if text else "(no output)"
    return f"{head}\n{body}\n" + run_store_mod.footer(
        log, text, status, False, run_store.meta(log).get("command"))


TOOL = Tool(
    tool_schema("stop_command",
        "Stop a background run_command and all it started.",
        {"path": {"type": "string", "description": "Log path or id (e.g. r3)"}},
        ["path"]),
    discoverable=True,
    run=stop_command, order=520, group="edit & shell", modes=WRITE_ROLES | {"plan"},
    needs_run_mode="new", inject=("run_store", "run_output_limit"), keeps_green=True,
    windows_itself=new_run_mode, example={"path": "r2"})
