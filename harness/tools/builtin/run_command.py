"""run_command: a shell command in the workdir.

Two variants, switched by /run-mode: "new" (the default) saves the whole
output to a log (harness/run_store.py) and returns a view of it — the tail, a
grep, or its beginning and end — that command_output can re-query; "classic"
returns the output itself."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ... import run_store as run_store_mod
from ..registry import WRITE_ROLES, Tool, tool_schema
from ..shell import (DEFAULT_COMMAND_TIMEOUT, MAX_COMMAND_TIMEOUT, STOPPED_TAIL_CHARS,
                     clamp_timeout, command_env, exec_command, log_view, longer, new_run_mode,
                     output_tail, read_log, split_view_pipe, web_refusal)

_TRAILING_AMP = re.compile(r"\s*(?<![&>|])&\s*$")
_NO_PYTHON_RE = re.compile(r"\bpython: (?:command )?not found")

_BACKGROUND_SETTLE = 3     # seconds a background command gets to start, without wait_for
_BACKGROUND_WAIT = 30      # seconds to wait for wait_for when no timeout is given


def _view_note(taken: str, view: dict) -> str:
    if "grep" in view:
        what = f"the lines matching `{view['grep']}` (case-insensitive)"
        if view["context"]:
            what += f" with {view['context']} lines of context"
    elif "head" in view:
        what = f"the first {view['head']} lines"
    else:
        what = f"the last {view['tail']} lines"
    return f"(ran without `{taken}` so the exit code is the command's own; showing {what})\n"


def run_command(command: str, timeout: int | None = None, tail: int | None = None,
                grep: str | None = None, context: int = 0, background: bool = False,
                wait_for: str | None = None, *, workdir: Path,
                net_access: str = "off", cancel=None, run_mode: str = "classic",
                run_store: "run_store_mod.RunStore | None" = None,
                run_output_limit: int = run_store_mod.DEFAULT_LIMIT,
                run_exit: dict | None = None, run_compact: bool = False) -> str:
    refusal, note = web_refusal(command, net_access)
    if refusal:
        return refusal
    command, piped, taken = split_view_pipe(command)
    head = None
    if piped:
        if "tail" in piped:
            tail = piped["tail"] = min(tail, piped["tail"]) if tail else piped["tail"]
        elif "head" in piped:
            head = piped["head"]
        elif grep:                    # the model's own grep= wins over the piped one
            piped = None
        else:
            grep, context = piped["grep"], max(context or 0, piped["context"])
        if piped:
            note += _view_note(taken, piped)
    defaulted = timeout is None
    timeout = clamp_timeout(timeout)
    new_mode = run_mode != "classic" and run_store is not None
    if new_mode and _TRAILING_AMP.search(command):
        # "npm run dev &": the shell returns at once and the server runs on,
        # untracked and unstoppable.  Run it as the background command it is.
        command, background = _TRAILING_AMP.sub("", command), True
        note += "(ran without the trailing `&`, as background=true)\n"
    if background and not new_mode:
        return "ERROR: background=true needs /run-mode new"
    if new_mode:
        return note + _run_to_log(command, timeout, workdir, cancel, run_store,
                                  run_output_limit, tail, grep, context, defaulted, run_exit,
                                  end_line=head, background=bool(background), wait_for=wait_for,
                                  compact=run_compact)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc, stopped = exec_command(command, timeout, workdir, cancel, out, err)
        if proc is None:
            return stopped
        out_text, err_text = output_tail(out), output_tail(err)
    if run_exit is not None and not stopped:
        run_exit["code"] = proc.returncode

    parts = []
    if out_text:
        parts.append(out_text)
    if err_text:
        parts.append(f"[stderr]\n{err_text}")
    if stopped:
        tail_text = "\n".join(parts)
        if len(tail_text) > STOPPED_TAIL_CHARS:
            tail_text = "…" + tail_text[-STOPPED_TAIL_CHARS:]
        return (f"ERROR: {stopped}" + longer(stopped, defaulted)
                + (f"\n[output before it stopped]\n{tail_text}" if tail_text else ""))
    if piped and parts:     # classic mode returns all output; the pipe asked for less
        lines = "\n".join(parts).splitlines()
        if "tail" in piped:
            parts = ["\n".join(lines[-tail:])]
        elif "head" in piped:
            parts = ["\n".join(lines[:head])]
        else:
            parts = [run_store_mod.grep(lines, grep, context)]
    if proc.returncode != 0:
        parts.append(f"[exit code: {proc.returncode}]")
    return note + ("\n".join(parts) or "(no output)")


def _start_background(command, timeout, defaulted, wait_for, workdir, cancel, log, store, path):
    """Start a command that is meant to keep running (a server, a watcher) and
    wait until it is ready: wait_for appears in its output, or a few seconds
    pass.  Returns (proc, stopped, notice) like exec_command, plus a line saying how
    the wait ended; proc None: it could not start (stopped is the error)."""
    # Python block-buffers stdout that is not a terminal: "Serving on …" would
    # sit in the buffer and wait_for never see it (command_env sets PYTHONUNBUFFERED).
    try:
        proc = subprocess.Popen(command, shell=True, cwd=workdir, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                env=command_env(workdir))
    except OSError as e:
        return None, f"ERROR: {e}", ""
    rx = run_store_mod.compile_pattern(wait_for)[0] if wait_for else None
    wait = _BACKGROUND_SETTLE if rx is None else (_BACKGROUND_WAIT if defaulted else timeout)
    deadline = time.monotonic() + wait
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            run_store_mod.kill_group(proc)
            return proc, "command interrupted by the user", ""
        if rx is not None and rx.search(read_log(store, path)):
            return proc, None, f"[ready: its output matched `{wait_for}`]\n"
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    if proc.poll() is not None:
        return proc, None, "[it exited during start-up, so it is not running in the background]\n"
    if rx is not None:
        return proc, None, (f"[`{wait_for}` did not appear in its output within {wait}s — it is "
                            f"still running; check the output below and stop_command it if it "
                            f"is stuck]\n")
    return proc, None, ""


def _run_to_log(command, timeout, workdir, cancel, store, limit, tail, grep, context,
                defaulted=False, run_exit: dict | None = None, end_line: int | None = None,
                background: bool = False, wait_for: str | None = None,
                compact: bool = False) -> str:
    """/run-mode new: stdout and stderr in order in one log file, and a bounded
    view of it back, ending with the log's full path for command_output.
    background: start it, wait until it is ready, and leave it running.
    compact: collapse redundant lines of the default view (/cavecat)."""
    if background and len(running := store.running()) >= run_store_mod.MAX_BACKGROUND:
        return (f"ERROR: {len(running)} background commands are already running "
                f"({', '.join(running)}) — stop_command one of them first")
    run_id, path = store.new_log()
    with open(path, "wb") as log:
        if background:
            proc, stopped, notice = _start_background(command, timeout, defaulted, wait_for,
                                                      workdir, cancel, log, store, path)
        else:
            (proc, stopped), notice = exec_command(command, timeout, workdir, cancel, log,
                                            subprocess.STDOUT), ""
    if proc is None:
        path.unlink(missing_ok=True)
        return stopped
    live = background and not stopped and proc.poll() is None
    if run_exit is not None and not stopped and not live:
        run_exit["code"] = proc.returncode
    status = stopped or ("still running" if live else f"exit code {proc.returncode}")
    store.record(run_id, command, status)
    if live:
        store.add_background(run_id, proc)
    text = read_log(store, path)
    if stopped and not limit:      # limit off: a runaway or timed-out command still gets a bounded view
        limit = STOPPED_TAIL_CHARS
    body, hint = log_view(text, limit, tail, grep, context, 1 if end_line else None, end_line,
                          how=f'command_output(path="{run_id}", start_line=…, end_line=…) '
                              f'shows them', compact=run_id if compact else None)
    foot = run_store_mod.footer(path, text, status, hint)
    if live:
        foot += (f'\n[running in the background — command_output(path="{run_id}") shows its '
                 f'output so far, stop_command(path="{run_id}") stops it]')
    if stopped:
        head = f"ERROR: {stopped}" + longer(stopped, defaulted)    # the log path stays last
        return f"{head}\n[output before it stopped]\n{body}\n{foot}" if text else f"{head}\n{foot}"
    return f"{notice}{body or '(no output)'}\n{foot}"


def _after(result: str, args: dict, workdir: Path, index) -> str:
    if _NO_PYTHON_RE.search(result) and shutil.which("python3"):
        result += "\n(note: there is no `python` command here — use python3)"
    if index is not None:
        index.mark_dirty()           # a command may have changed any file
    return result


_RUN_COMMAND_NOTES = (
    "To read a web page or call an API use fetch_url, not curl or wget; to download a "
    "file to disk (a jar, archive, image) curl -o or wget is fine. The command runs with "
    "the working directory as its current directory. It is NON-INTERACTIVE: no stdin is "
    "connected, so a command that waits for input (e.g. 'git commit' with no -m, "
    "'npm init', a prompt for a password) will hang until it times out — always pass "
    "flags that avoid prompts.")

_CLASSIC = tool_schema("run_command",
    "Run a shell command from the working directory. Returns stdout and stderr. "
    "Use for running scripts, tests, build tools, etc. To read a web page or call an "
    "API use fetch_url, not curl or wget; to download a file to disk (a jar, "
    "archive, image) curl -o or wget is fine. The command runs with the "
    "working directory as its current directory. It is NON-INTERACTIVE: no stdin is "
    "connected, so a command that waits for input (e.g. 'git commit' with no -m, "
    "'npm init', a prompt for a password) will hang until it times out — always pass "
    "flags that avoid prompts. Long output is returned in full (up to the last 4 MB of "
    "each stream); exit code is appended when non-zero. A command that times out or is "
    "interrupted returns the end of its output so far.",
    {"command": {"type": "string", "description": "Shell command to execute (runs in the working directory)"},
     "timeout": {"type": "integer", "description": f"Timeout in seconds (default {DEFAULT_COMMAND_TIMEOUT}, maximum {MAX_COMMAND_TIMEOUT} = {MAX_COMMAND_TIMEOUT // 60} minutes) — pass a longer one for a full build or test suite"}},
    ["command"])

TOOL = Tool(
    tool_schema("run_command",
        "Run a shell command in the working directory. Pass tail=N for only the last N lines "
        "of output, or grep=\"regex\" for only the matching lines (both: the last N matches); "
        "with neither, short output comes back whole and long output as its beginning and end. "
        "The full output (stdout and stderr) is saved to a log whose path ends the result: "
        "pass it to command_output to search it again instead of rerunning the command. Do "
        "not pipe into tail/head/grep yourself (that hides the exit code). " + _RUN_COMMAND_NOTES,
        {"command": {"type": "string", "description": "Shell command to execute"},
         "tail":    {"type": "integer", "description": "Only the last N lines (e.g. 30 for a test summary)"},
         "grep":    {"type": "string", "description": "Case-insensitive regex; only matching lines, numbered (e.g. \"error|fail\")"},
         "context": {"type": "integer", "description": "With grep: lines around each match"},
         "timeout": {"type": "integer", "description": f"Seconds (default {DEFAULT_COMMAND_TIMEOUT}, maximum {MAX_COMMAND_TIMEOUT}); longer for a full build or test suite"},
         "background": {"type": "boolean", "description": "Keep it running (a server, a watcher); stop_command ends it"},
         "wait_for": {"type": "string", "description": "With background: regex printed once ready; waits up to timeout (default 30s)"}},
        ["command"]),
    variants={"classic": _CLASSIC}, examples={"classic": {"command": "python -m pytest"}},
    select=lambda ctx: "classic" if ctx.run_mode == "classic" else "",
    # Plan investigation may run commands (reproduce a bug, check the test
    # baseline) but not edit files until the plan is approved.
    run=run_command, order=500, group="edit & shell", modes=WRITE_ROLES | {"plan"},
    inject=("net_access", "cancel", "run_mode", "run_store", "run_output_limit", "run_exit",
            "run_compact"),
    windows_itself=new_run_mode, after=_after,
    example={"command": "python -m pytest", "tail": 30})
