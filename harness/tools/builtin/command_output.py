"""command_output: re-query a saved run_command log (/run-mode new) without
running the command again."""
from __future__ import annotations

from pathlib import Path

from ... import run_store as run_store_mod
from ..registry import INVESTIGATE_ROLES, WRITE_ROLES, Tool, tool_schema
from ..shell import log_view, new_run_mode, no_log, read_log


def command_output(path: str, grep: str | None = None, context: int = 0,
                   tail: int | None = None, start_line: int | None = None,
                   end_line: int | None = None, *, workdir: Path,
                   run_store: "run_store_mod.RunStore | None" = None,
                   run_output_limit: int = run_store_mod.DEFAULT_LIMIT) -> str:
    if run_store is None:
        return "ERROR: command_output is only available in /run-mode new"
    log = run_store.resolve(path)
    if log is None:
        return no_log(path, run_store)
    status = run_store.status(log)        # first: a command that just ended has written all
    text = read_log(run_store, log)
    body, hint = log_view(text, run_output_limit, tail, grep, context, start_line, end_line,
                          how="pass start_line/end_line, grep= or tail= to see them")
    if body.startswith("ERROR"):
        return body
    return f"{body or '(no output)'}\n" + run_store_mod.footer(
        log, text, status, hint, run_store.meta(log).get("command"))


TOOL = Tool(
    tool_schema("command_output",
        "Search the saved output of an earlier run_command without running it again.",
        {"path":       {"type": "string", "description": "Log path from the run_command result, or its id (e.g. r3)"},
         "grep":       {"type": "string", "description": "Case-insensitive regex"},
         "context":    {"type": "integer", "description": "With grep: lines around each match"},
         "tail":       {"type": "integer", "description": "Last N lines (with grep: last N matches)"},
         "start_line": {"type": "integer", "description": "First line (negative: from the end)"},
         "end_line":   {"type": "integer", "description": "Last line (inclusive)"}},
        ["path"]),
    # It follows run_command: wherever that is offered, in /run-mode new.
    suggests=("stop_command",),
    run=command_output, order=510, group="edit & shell", modes=WRITE_ROLES | INVESTIGATE_ROLES,
    needs_run_mode="new", inject=("run_store", "run_output_limit"), keeps_green=True,
    windows_itself=new_run_mode, example={"path": "r1", "grep": "FAILED|Error"})
