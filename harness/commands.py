from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import code_index
from . import net as net_mod
from . import session as session_mod
from .harness import Harness, ChatEvent
from .tools import dispatch


def _format_index(harness) -> str:
    """/index: a meter of memory used against the budget, then what the index is
    made of — per category and per language, each bar a share of the index —
    in the same layout as /context."""
    b = harness.index_breakdown()
    if not b["enabled"]:
        return ("Code index: off\nTurn it on with /index on — the model then gets the "
                "index_* tools (search, text, callers, map, file).")
    fmt = net_mod.format_size
    width = 20

    def bar(frac: float) -> str:
        fill = min(width, round(frac * width))
        return "█" * fill + "░" * (width - fill)

    state = b["state"] + (f" {b['progress']}" if b["progress"] else "")
    used = b["used"] or 1
    lines = [f"Code index: {state} — {b['files']:,} files"
             + (" | PARTIAL: budget reached" if b["partial"] else ""),
             f"  {'Budget':<16}{fmt(b['used']):>9}  {b['pct']:>3}%  {bar(b['used'] / (b['limit'] or 1))}"
             f"  of {fmt(b['limit'])} (/index-max-mem)",
             "Made of:"]

    def share_row(label: str, n: int, detail: str) -> str:
        return (f"  {label:<16}{fmt(n):>9}  {n / used * 100:>3.0f}%  {bar(n / used)}"
                + (f"  {detail}" if detail else ""))
    for c in b["categories"]:
        if not c["enabled"]:
            lines.append(f"  {c['label']:<16}{'dropped':>9}     —  (over budget — /index-max-mem to restore)")
        else:
            lines.append(share_row(c["label"], c["bytes"], c["detail"]))
    if b["languages"]:
        lines.append("By language:")
        for row in b["languages"][:10]:
            lines.append(share_row(row["lang"], row["bytes"],
                                   f"{row['files']:,} file{'s' if row['files'] != 1 else ''}"))
        if len(b["languages"]) > 10:
            rest = b["languages"][10:]
            lines.append(f"  … {len(rest)} more ({sum(r['files'] for r in rest):,} files, "
                         f"{fmt(sum(r['bytes'] for r in rest))})")
        if b["shared"]:
            lines.append(share_row("shared names", b["shared"], "identifier names used across files"))
    if b["skipped"]:
        lines.append("Skipped: " + ", ".join(f"{v:,} {k.replace('_', ' ')}"
                                             for k, v in b["skipped"].items()))
    if b["error"]:
        lines.append(f"Last error: {b['error']}")
    lines.append(f"  (sizes are estimates of the index's own data; save/load to disk: "
                 f"{'on' if b['persist'] else 'off'} — {code_index.pickle_path(harness.workdir)})")
    lines.append(f"  Route grep/find to the index: {'on' if b.get('route', False) else 'off'} "
                 f"(/index-route)")
    return "\n".join(lines)


@dataclass
class CommandResult:
    handled: bool
    output: str | None = None
    exit_app: bool = False
    confirm_prompt: str | None = None       # if set, TUI asks this before proceeding
    confirm_action: "callable | None" = None  # called with no args when user answers y
    tool_output: bool | None = None         # TUI sets tool-pane visibility (True=show, False=hide)
    think_output: bool | None = None        # TUI sets thinking-output visibility
    md_render: bool | None = None           # TUI sets markdown rendering
    diff_output: bool | None = None         # TUI shows/hides edit diffs
    diff_style: str | None = None           # TUI sets diff style ("compact"|"git")
    companion: bool | None = None           # TUI shows/hides momo companion bar
    replay_session: bool = False            # TUI replays loaded session messages into chat buffer
    run_compact: bool = False               # TUI runs compact on worker thread
    compact_summarise: bool = True          # passed to compact_threaded()
    run_plan: bool = False                  # TUI runs execute_plan_threaded() on a worker thread
    retry: bool = False                     # controller re-sends the last user message



def _format_context(harness) -> str:
    """/context: usage against the limit, then tokens per category with a bar."""
    b = harness.context_breakdown()
    limit = b["limit"]
    head = f"Context: ~{b['used']:,} / {limit:,} tokens ({b['pct']}%)"
    if harness.context_pct is not None:
        head += f" | limit is {harness.context_pct}% of model max"
    if b["model_max"]:
        head += f" | model max {b['model_max']:,}"
    if b["measured"] is not None:
        head += f" | last measured {b['measured']:,}"
    lines = [head]
    width = 20
    for c in b["categories"]:
        if c["key"] == "generating" and not b["streaming"]:
            continue
        if not c["sent"]:
            lines.append(f"  {c['label']:<14}{c['tokens']:>8,}     —  (kept in transcript, not sent to the model)")
            continue
        frac = c["tokens"] / limit if limit else 0
        fill = min(width, round(frac * width))
        lines.append(f"  {c['label']:<14}{c['tokens']:>8,}  {frac * 100:>3.0f}%  "
                     + "█" * fill + "░" * (width - fill))
    lines.append(f"  (per-category figures are estimates, ~4 chars per token; total ~{b['estimated']:,})")
    return "\n".join(lines)

def handle(line: str, harness: Harness) -> CommandResult:
    """Parse and execute a /command. Returns CommandResult."""
    parts = line.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/exit", "/quit"):
        harness._autosave()
        harness.logger.close()
        return CommandResult(handled=True, exit_app=True)

    if cmd == "/help":
        return CommandResult(handled=True, output=_HELP)

    if cmd == "/model":
        if not arg:
            models = harness.client.list_models()
            if not models:
                return CommandResult(handled=True,
                                     output=f"No models found — is {harness.client.provider_name} reachable at {harness.client.host}?")
            current = harness.client.model
            lines = [f"  {'*' if m == current else ' '} {m}" for m in models]
            return CommandResult(handled=True, output="Models:\n" + "\n".join(lines))
        if not harness.client.can_switch_model:
            return CommandResult(handled=True, output=(
                f"{harness.client.provider_name} serves one model per server and cannot switch "
                f"models at runtime (currently: {harness.client.model}). "
                f"Restart the server with the model you want."))
        harness.set_model(arg)
        return CommandResult(handled=True, output=f"Model set to: {arg} (ctx: {harness.context_limit})")

    if cmd == "/host":
        if not arg:
            return CommandResult(handled=True,
                                 output=f"{harness.client.provider_name} host: {harness.client.host}")
        harness.client.set_host(arg)
        # A new host may be a different server serving a different model and context
        # window — adopt the server's loaded model (fixed-model backends) and re-read
        # its context size so the display matches reality.
        harness._reconcile_fixed_model()
        harness._sync_context_limit(emit=True)
        harness._emit_status()
        return CommandResult(handled=True, output=f"Host set to: {arg}")

    if cmd == "/token":
        if not arg:
            token = harness.client._auth_token
            if token:
                return CommandResult(handled=True, output=f"Auth token: {_mask_token(token)}")
            return CommandResult(handled=True, output="Auth token: not set")
        harness.client.set_auth_token(arg)
        return CommandResult(handled=True, output=f"Auth token set: {_mask_token(arg)}")

    if cmd == "/clear-token":
        harness.client.set_auth_token(None)
        return CommandResult(handled=True, output="Auth token cleared")

    if cmd == "/code":
        harness.set_mode("coding")
        return CommandResult(handled=True, output="Switched to coding mode")

    if cmd == "/design":
        harness.set_mode("design")
        return CommandResult(handled=True, output="Switched to design mode")

    if cmd == "/plan":
        sub = arg.lower()
        if not sub:
            harness.set_mode("plan")
            state = ""
            if harness.plan is not None:
                state = f" — current plan: {harness.plan.title} ({harness._plan_progress()}); /plan show to view"
            return CommandResult(handled=True, output=(
                "Switched to plan mode: describe a feature or bug; I'll investigate, ask "
                "questions, and propose a plan to approve before executing it" + state))
        if sub == "show":
            if harness.plan is None:
                return CommandResult(handled=True, output="No active plan.")
            return CommandResult(handled=True, output=harness.plan.to_markdown())
        if sub in ("run", "resume"):
            if harness.plan is None:
                return CommandResult(handled=True, output="No active plan. Use /plan and describe a feature or bug first.")
            if harness.plan_phase != "executing":
                err = harness.approve_plan()  # re-reads the plan file for hand edits
                if err:
                    return CommandResult(handled=True, output=f"ERROR: {err}")
            harness.set_mode("plan")
            return CommandResult(handled=True, run_plan=True)
        if sub == "cancel":
            return CommandResult(handled=True, output=harness.cancel_plan())
        return CommandResult(handled=True, output="Usage: /plan [show|run|resume|cancel]")

    if cmd == "/chat":
        harness.set_mode("chat")
        return CommandResult(handled=True, output="Switched to chat mode")

    if cmd == "/momo":
        harness.set_mode("momo")
        return CommandResult(handled=True, output="Switched to momo mode")

    if cmd == "/new":
        return CommandResult(handled=True, output=harness.new_session(), replay_session=True)

    if cmd == "/retry":
        return CommandResult(handled=True, retry=True)

    if cmd == "/clear":
        # An active plan goes too: it refers to a conversation that no longer exists.
        # (Safe here — the Controller rejects /clear while a plan step is running.)
        plan_note = ""
        if harness.plan is not None:
            plan_note = "; " + harness.cancel_plan()  # also refreshes messages[0]
        system_msg = harness.messages[0]
        harness.messages = [system_msg]
        guides_note = harness.reload_guides()
        harness._token_estimate = harness._estimate(schemas=True)
        harness._emit_status()
        return CommandResult(handled=True, output="Conversation cleared" + plan_note
                             + (f"\n{guides_note}" if guides_note else ""))

    if cmd in ("/workspace", "/workdir"):  # /workdir kept as a backward-compatible alias
        if not arg:
            return CommandResult(handled=True, output=f"Working directory: {harness.workdir}")
        p = Path(arg).expanduser().resolve()
        if not p.is_dir():
            def _create():
                try:
                    p.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    return f"ERROR: could not create directory: {e}"
                harness.workdir = p
                harness.set_mode(harness.mode)
                guides_note = harness.reload_guides()
                index_note = harness.restart_index()
                return (f"Created and set working directory: {p}"
                        + "".join(f"\n{n}" for n in (guides_note, index_note) if n))
            return CommandResult(
                handled=True,
                confirm_prompt=f"Directory does not exist: {p}\nCreate it?",
                confirm_action=_create,
            )
        harness.workdir = p
        harness.set_mode(harness.mode)  # always refresh system prompt with new workdir
        guides_note = harness.reload_guides()
        index_note = harness.restart_index()
        harness._emit_status()
        return CommandResult(handled=True, output=f"Working directory set to: {p}"
                             + "".join(f"\n{n}" for n in (guides_note, index_note) if n))

    if cmd == "/tool-output":
        if arg.lower() in ("on", "true", "1", "yes"):
            return CommandResult(handled=True, tool_output=True)
        if arg.lower() in ("off", "false", "0", "no"):
            return CommandResult(handled=True, tool_output=False)
        return CommandResult(handled=True, output=f"ERROR: expected 'on' or 'off', got: {arg!r}" if arg else "ERROR: expected 'on' or 'off'")

    if cmd == "/think-output":
        if arg.lower() in ("on", "true", "1", "yes"):
            return CommandResult(handled=True, think_output=True)
        if arg.lower() in ("off", "false", "0", "no"):
            return CommandResult(handled=True, think_output=False)
        return CommandResult(handled=True, output=f"ERROR: expected 'on' or 'off', got: {arg!r}" if arg else "ERROR: expected 'on' or 'off'")

    if cmd == "/markdown":
        if arg.lower() in ("on", "true", "1", "yes"):
            return CommandResult(handled=True, md_render=True)
        if arg.lower() in ("off", "false", "0", "no"):
            return CommandResult(handled=True, md_render=False)
        return CommandResult(handled=True, output=f"ERROR: expected 'on' or 'off', got: {arg!r}" if arg else "ERROR: expected 'on' or 'off'")

    if cmd == "/diff":
        if arg.lower() in ("on", "true", "1", "yes"):
            return CommandResult(handled=True, diff_output=True)
        if arg.lower() in ("off", "false", "0", "no"):
            return CommandResult(handled=True, diff_output=False)
        return CommandResult(handled=True, output=f"ERROR: expected 'on' or 'off', got: {arg!r}" if arg else "ERROR: expected 'on' or 'off'")

    if cmd == "/diff-style":
        if arg.lower() in ("git", "compact"):
            return CommandResult(handled=True, diff_style=arg.lower())
        return CommandResult(handled=True, output=f"ERROR: expected 'git' or 'compact', got: {arg!r}" if arg else "ERROR: expected 'git' or 'compact'")

    if cmd == "/compact":
        return CommandResult(handled=True, run_compact=True)

    if cmd == "/fast-compact":
        return CommandResult(handled=True, run_compact=True, compact_summarise=False)

    if cmd == "/context":
        if not arg:
            return CommandResult(handled=True, output=_format_context(harness))
        if arg.endswith("%"):
            try:
                n = int(arg[:-1])
            except ValueError:
                return CommandResult(handled=True, output=f"ERROR: invalid percentage: {arg}")
            if not 1 <= n <= 100:
                return CommandResult(handled=True, output="ERROR: percentage must be 1–100")
            reported = harness.client.context_length()
            if not reported:
                return CommandResult(handled=True,
                                     output="ERROR: model did not report a context size — use /context <n> to set an absolute limit")
            harness.context_pct = n
            harness._sync_context_limit(emit=False)
            harness._emit_status()
            return CommandResult(handled=True,
                                 output=f"Context limit set to {n}% of {reported:,} max: {harness.context_limit:,} tokens")
        try:
            n = int(arg)
            if n < 256:
                return CommandResult(handled=True, output="ERROR: context limit must be >= 256")
            harness.context_pct = None
            harness.context_limit = n
            harness._emit_status()
            return CommandResult(handled=True, output=f"Context limit set to: {n}")
        except ValueError:
            return CommandResult(handled=True, output=f"ERROR: invalid number: {arg}")

    if cmd == "/tool-result":
        if not arg:
            cap = harness.max_tool_result
            return CommandResult(handled=True,
                                 output=f"Tool result cap: {cap} chars (0 = unlimited)")
        try:
            n = int(arg)
            if n < 0:
                return CommandResult(handled=True, output="ERROR: value must be >= 0")
            harness.max_tool_result = n
            label = f"{n} chars" if n > 0 else "unlimited"
            return CommandResult(handled=True, output=f"Tool result cap set to: {label}")
        except ValueError:
            return CommandResult(handled=True, output=f"ERROR: invalid number: {arg}")

    if cmd == "/think":
        if not arg:
            state = "on" if harness.think else "off"
            return CommandResult(handled=True, output=f"Thinking mode: {state}")
        if arg.lower() in ("on", "true", "1", "yes"):
            harness.think = True
            return CommandResult(handled=True, output="Thinking mode: on")
        if arg.lower() in ("off", "false", "0", "no"):
            harness.think = False
            return CommandResult(handled=True, output="Thinking mode: off")
        return CommandResult(handled=True, output=f"ERROR: expected 'on' or 'off', got: {arg}")

    if cmd == "/companion-idle-recap":
        def _state() -> str:
            state = "on" if harness.idle_recap else "off"
            return f"Idle recap: {state} (after {harness.idle_recap_secs}s idle)"
        if not arg:
            return CommandResult(handled=True, output=_state())
        if arg.lower() in ("on", "true", "1", "yes"):
            harness.idle_recap = True
        elif arg.lower() in ("off", "false", "0", "no"):
            harness.idle_recap = False
        else:
            try:
                secs = int(arg)
            except ValueError:
                return CommandResult(handled=True, output=f"ERROR: expected 'on', 'off' or seconds, got: {arg}")
            if secs < 10:
                return CommandResult(handled=True, output="ERROR: idle time must be at least 10 seconds")
            harness.idle_recap_secs = secs
        session_mod.save_prefs(idle_recap=harness.idle_recap, idle_recap_secs=harness.idle_recap_secs)
        return CommandResult(handled=True, output=_state())

    if cmd == "/guides":
        sub = arg.lower()
        if sub in ("on", "true", "1", "yes", "off", "false", "0", "no"):
            harness.guides = sub in ("on", "true", "1", "yes")
            session_mod.save_prefs(guides=harness.guides)
        elif sub and sub != "reload":
            return CommandResult(handled=True, output=f"ERROR: expected 'on', 'off' or 'reload', got: {arg}")
        if sub:
            note = harness.reload_guides()
            harness._token_estimate = harness._estimate(schemas=True)
            harness._emit_status()
        else:
            note = harness.guides_summary()
        if not harness.guides:
            return CommandResult(handled=True, output="Project guides: off")
        return CommandResult(handled=True, output=f"Project guides: on\n{note}")

    if cmd == "/tools":
        if not arg:
            state = "on" if harness.tools_enabled else "off"
            return CommandResult(handled=True, output=f"Tool calls: {state}")
        if arg.lower() in ("on", "true", "1", "yes"):
            harness.tools_enabled = True
            harness._emit_status()
            return CommandResult(handled=True, output="Tool calls: on")
        if arg.lower() in ("off", "false", "0", "no"):
            harness.tools_enabled = False
            harness._emit_status()
            return CommandResult(handled=True, output="Tool calls: off")
        return CommandResult(handled=True, output=f"ERROR: expected 'on' or 'off', got: {arg}")

    if cmd == "/run-confirm":
        if not arg:
            state = "on" if harness.run_confirm else "off"
            return CommandResult(handled=True, output=f"run_command confirmation: {state}")
        if arg.lower() in ("on", "true", "1", "yes"):
            harness.run_confirm = True
            harness._emit_status()
            return CommandResult(handled=True, output="run_command confirmation: on (you will be asked y/N before each command)")
        if arg.lower() in ("off", "false", "0", "no"):
            harness.run_confirm = False
            harness._emit_status()
            return CommandResult(handled=True, output="run_command confirmation: off (commands run automatically)")
        return CommandResult(handled=True, output=f"ERROR: expected 'on' or 'off', got: {arg}")

    if cmd == "/net":
        def _net_state() -> str:
            label = {"off": "off", "on": "on (public internet only)",
                     "local": "on, including localhost and the LAN"}[harness.net_access]
            writes = "ask y/N" if harness.net_confirm else "run without asking"
            return f"Internet access: {label}\nWrite requests (POST/PUT/PATCH/DELETE): {writes}"
        if not arg:
            return CommandResult(handled=True, output=_net_state())
        a = arg.lower()
        if a in ("on", "true", "1", "yes", "public"):
            harness.net_access = "on"
        elif a in ("off", "false", "0", "no"):
            harness.net_access = "off"
        elif a == "local":
            harness.net_access = "local"
        else:
            return CommandResult(handled=True,
                                 output=f"ERROR: expected 'on', 'off' or 'local', got: {arg}")
        # fetch_url enters/leaves the tool set, so the generated tool reference
        # in the system prompt has to be re-rendered.
        harness.rebuild_system_prompt()
        harness._emit_status()
        extra = ""
        if harness.net_access != "off":
            extra = ("\nFetched pages are untrusted input — consider '/run-confirm on' "
                     "so shell commands need approval too.")
        if harness.net_access == "local":
            extra += ("\nlocalhost and the LAN are now reachable, including this harness's "
                      "own web UI. Writes to private addresses always ask first.")
        return CommandResult(handled=True, output=_net_state() + extra)

    if cmd == "/net-confirm":
        if not arg:
            state = "on" if harness.net_confirm else "off"
            return CommandResult(handled=True, output=f"fetch_url write confirmation: {state}")
        if arg.lower() in ("on", "true", "1", "yes"):
            harness.net_confirm = True
            harness._emit_status()
            return CommandResult(handled=True,
                                 output="fetch_url write confirmation: on (you will be asked "
                                        "y/N before each POST/PUT/PATCH/DELETE)")
        if arg.lower() in ("off", "false", "0", "no"):
            harness.net_confirm = False
            harness._emit_status()
            return CommandResult(handled=True,
                                 output="fetch_url write confirmation: off (write requests run "
                                        "automatically; requests to local and private addresses "
                                        "are still confirmed)")
        return CommandResult(handled=True, output=f"ERROR: expected 'on' or 'off', got: {arg}")

    if cmd == "/net-max-bytes":
        cur = net_mod.format_size(harness.net_max_bytes)
        if not arg:
            return CommandResult(handled=True, output=(
                f"fetch_url download cap: {cur} ({harness.net_max_bytes} bytes)\n"
                f"Set it with a size, e.g. /net-max-bytes 500kb or /net-max-bytes 8mb. "
                f"This bounds the download only; /net-max-chars sets how much text "
                f"reaches the context."))
        size = net_mod.parse_size(arg)
        if size is None:
            return CommandResult(handled=True, output=(
                f"ERROR: not a size: {arg}. Use bytes or a unit, e.g. 200000, 500kb, 2mb."))
        if size > net_mod.HARD_MAX_BYTES:
            return CommandResult(handled=True, output=(
                f"ERROR: {net_mod.format_size(size)} is above the "
                f"{net_mod.format_size(net_mod.HARD_MAX_BYTES)} hard limit."))
        harness.net_max_bytes = size
        harness._emit_status()
        return CommandResult(handled=True, output=(
            f"fetch_url download cap: {net_mod.format_size(size)} ({size} bytes)"))

    if cmd == "/net-max-chars":
        if not arg:
            return CommandResult(handled=True, output=(
                f"fetch_url text per call: {harness.net_max_chars:,} characters "
                f"(~{harness.net_max_chars // 4:,} tokens); the model pages through the "
                f"rest with offset=/find=.\nSet it with a number, e.g. /net-max-chars 12000."))
        try:
            n = int(arg.replace(",", "").replace("_", ""))
        except ValueError:
            n = 0
        if n < 1000 or n > net_mod.HARD_MAX_CHARS:
            return CommandResult(handled=True, output=(
                f"ERROR: expected a number of characters between 1000 and "
                f"{net_mod.HARD_MAX_CHARS:,}, got: {arg}"))
        harness.net_max_chars = n
        harness._emit_status()
        warn = ""
        if n // 4 > harness.context_limit // 2:
            warn = (f"\nThat is large: one fetch (~{n // 4:,} tokens) can fill over half "
                    f"the context ({harness.context_limit:,} tokens).")
        return CommandResult(handled=True, output=(
            f"fetch_url text per call: {n:,} characters" + warn))

    if cmd == "/index":
        sub = arg.lower()
        if not sub or sub == "status":
            return CommandResult(handled=True, output=_format_index(harness))
        if sub in ("on", "true", "1", "yes", "off", "false", "0", "no"):
            on = sub in ("on", "true", "1", "yes")
            session_mod.save_prefs(index=on)
            return CommandResult(handled=True, output=harness.set_index(on))
        if harness.index is None:
            return CommandResult(handled=True, output=f"ERROR: the code index is off — /index on first")
        if sub == "rebuild":
            harness.index.rebuild()
            harness._emit_status()
            return CommandResult(handled=True, output="Code index: rebuilding from scratch")
        if sub == "save":
            return CommandResult(handled=True, output=harness.index.save())
        if sub == "load":
            msg = harness.index.load() or (f"No saved code index for this workdir "
                                           f"({code_index.pickle_path(harness.workdir)}).")
            harness.index.mark_dirty()
            harness._emit_status()
            return CommandResult(handled=True, output=msg)
        return CommandResult(handled=True, output=(
            f"ERROR: expected on, off, status, rebuild, save or load, got: {arg}"))

    if cmd == "/index-max-mem":
        cur = net_mod.format_size(harness.index_max_bytes)
        if not arg:
            return CommandResult(handled=True, output=(
                f"Code index memory budget: {cur}\nSet it with a size, e.g. /index-max-mem 200mb. "
                f"Over budget the index drops text-search signatures first, then the identifier "
                f"index, then stops adding files."))
        size = net_mod.parse_size(arg)
        if size is None:
            return CommandResult(handled=True, output=(
                f"ERROR: not a size: {arg}. Use bytes or a unit, e.g. 100mb, 512kb, 1gb."))
        if size < code_index.MIN_MAX_BYTES:
            return CommandResult(handled=True, output=(
                f"ERROR: the minimum is {net_mod.format_size(code_index.MIN_MAX_BYTES)}"))
        harness.index_max_bytes = size
        session_mod.save_prefs(index_max_mem=size)
        if harness.index is not None:
            harness.index.set_max_bytes(size)
        harness._emit_status()
        return CommandResult(handled=True, output=f"Code index memory budget: {net_mod.format_size(size)}")

    if cmd == "/index-route":
        if not arg:
            return CommandResult(handled=True, output=(
                f"Answer grep_files / find_files from the code index: "
                f"{'on' if harness.index_route else 'off'}"))
        if arg.lower() in ("on", "true", "1", "yes", "off", "false", "0", "no"):
            harness.index_route = arg.lower() in ("on", "true", "1", "yes")
            session_mod.save_prefs(index_route=harness.index_route)
            harness.rebuild_system_prompt()     # the prompt and grep/find descriptions change
            harness._emit_status()
            extra = (" — a plain-text grep_files and a file-name find_files are answered by "
                     "index_text / the index's file list; regex searches and files the index "
                     "does not cover still go to the disk") if harness.index_route else \
                " — grep_files / find_files always search the disk"
            return CommandResult(handled=True, output=(
                f"Answer grep/find from the code index: {'on' if harness.index_route else 'off'}"
                f"{extra}"))
        return CommandResult(handled=True, output=f"ERROR: expected 'on' or 'off', got: {arg}")

    if cmd == "/index-persist":
        if not arg:
            state = "on" if harness.index_persist else "off"
            return CommandResult(handled=True, output=(
                f"Code index save/load to disk: {state} ({code_index.pickle_path(harness.workdir)})"))
        if arg.lower() in ("on", "true", "1", "yes", "off", "false", "0", "no"):
            harness.index_persist = arg.lower() in ("on", "true", "1", "yes")
            session_mod.save_prefs(index_persist=harness.index_persist)
            harness._emit_status()
            extra = (" — saved on exit, on a workdir change and after the first build; loaded "
                     "when the index starts") if harness.index_persist else ""
            return CommandResult(handled=True, output=(
                f"Code index save/load to disk: {'on' if harness.index_persist else 'off'}{extra}"))
        return CommandResult(handled=True, output=f"ERROR: expected 'on' or 'off', got: {arg}")

    if cmd == "/cost":
        return CommandResult(handled=True, output=harness.logger.cost_summary())

    if cmd == "/session":
        if not arg:
            return CommandResult(handled=True,
                                 output=f"Current session: {harness.session_path()}")
        p = session_mod.find_session(arg)
        if p is None:
            sessions = session_mod.list_sessions()
            names = [s.stem for s in sessions[:10]]
            hint = "\nRecent sessions:\n" + "\n".join(f"  {n}" for n in names) if names else ""
            return CommandResult(handled=True, output=f"Session not found: {arg}{hint}")
        msg = harness.load_session(p)
        return CommandResult(handled=True, output=msg, replay_session=True)

    if cmd == "/list-skills":
        available = harness.list_available_skills()
        if not available:
            return CommandResult(handled=True, output="No skills found in skills/ folder.")
        active = set(harness.active_skills)
        lines = [f"  {'[on] ' if s in active else '[off]'} {s}" for s in available]
        return CommandResult(handled=True, output="Skills:\n" + "\n".join(lines))

    if cmd == "/load-skill":
        if not arg:
            return CommandResult(handled=True, output="Usage: /load-skill <name>")
        return CommandResult(handled=True, output=harness.load_skill(arg.strip()))

    if cmd == "/unload-skill":
        if not arg:
            return CommandResult(handled=True, output="Usage: /unload-skill <name>")
        return CommandResult(handled=True, output=harness.unload_skill(arg.strip()))

    if cmd == "/sessions":
        sessions = session_mod.list_sessions()
        if not sessions:
            return CommandResult(handled=True, output="No saved sessions.")
        lines = []
        for p in sessions[:20]:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                mode  = data.get("mode", "?")
                model = data.get("model", "?")
            except Exception:
                mode = model = "?"
            lines.append(f"  {p.stem}  [{mode}]  {model}")
        return CommandResult(handled=True, output="Recent sessions:\n" + "\n".join(lines))

    if cmd == "/export":
        filename = arg.strip() or f"conversation-{harness._ts}.md"
        rendered = _render_markdown(harness.messages)
        out = harness.workdir / filename
        try:
            out.write_text(rendered, encoding="utf-8")
        except OSError as e:
            return CommandResult(handled=True, output=f"ERROR: {e}")
        return CommandResult(handled=True, output=f"Exported to: {out}")

    if cmd == "/copy":
        if arg.strip() == "all":
            text = _render_markdown(harness.messages)
        else:
            text = _last_assistant_text(harness.messages)
        if not text:
            return CommandResult(handled=True, output="Nothing to copy.")
        err = _copy_to_clipboard(text)
        if err:
            return CommandResult(handled=True, output=err)
        return CommandResult(handled=True, output="Copied to clipboard.")

    if cmd == "/companion":
        if not arg:
            return CommandResult(handled=True, output="Usage: /companion on|off  (Shift+Q to toggle)")
        if arg.lower() in ("on", "true", "1", "yes"):
            return CommandResult(handled=True, companion=True)
        if arg.lower() in ("off", "false", "0", "no"):
            return CommandResult(handled=True, companion=False)
        return CommandResult(handled=True, output=f"ERROR: expected 'on' or 'off', got: {arg!r}")

    if cmd == "/read":
        if not arg:
            return CommandResult(handled=True, output="Usage: /read <path> [start_line] [end_line]")
        parts = arg.split()
        path = parts[0]
        try:
            start = int(parts[1]) if len(parts) > 1 else 1
            end   = int(parts[2]) if len(parts) > 2 else None
        except ValueError:
            return CommandResult(handled=True, output="ERROR: start_line and end_line must be integers")
        result = dispatch("read_file", {"path": path, "start_line": start, "end_line": end}, harness.workdir)
        return CommandResult(handled=True, output=result)

    if cmd == "/ls":
        result = dispatch("list_directory", {"path": arg or "."}, harness.workdir)
        return CommandResult(handled=True, output=result)

    if cmd == "/grep":
        if not arg:
            return CommandResult(handled=True, output="Usage: /grep <pattern> [path_or_dir]")
        parts = arg.split(None, 1)
        pattern = parts[0]
        target  = parts[1] if len(parts) > 1 else "."
        p = (harness.workdir / target).resolve()
        if p.is_file():
            result = dispatch("grep_file", {"pattern": pattern, "path": target}, harness.workdir)
        else:
            result = dispatch("grep_files", {"pattern": pattern, "directory": target}, harness.workdir)
        return CommandResult(handled=True, output=result)

    return CommandResult(handled=False)


def _mask_token(t: str) -> str:
    n = len(t)
    if n <= 5:
        return "*" * n
    return t[:2] + "*" * (n - 5) + t[-3:]


def _render_markdown(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        if role in ("system", "thinking"):
            continue
        if role == "user":
            parts.append(f"**You:**\n\n{content}")
        elif role == "assistant":
            if content:
                parts.append(f"**Assistant:**\n\n{content}")
        elif role == "tool":
            name = m.get("name", "tool")
            parts.append(f"**Tool result ({name}):**\n\n```\n{content}\n```")
    return "\n\n---\n\n".join(parts) + "\n"


def _last_assistant_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "assistant":
            content = m.get("content") or ""
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            if content:
                return content
    return ""


def _copy_to_clipboard(text: str) -> str:
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        try:
            subprocess.run(cmd, input=text.encode(), check=True, timeout=5,
                           capture_output=True)
            return ""
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            continue
    return "ERROR: no clipboard tool found (tried pbcopy, xclip, xsel)"


_HELP = """
Available commands:
  /model              List available models on the current backend
  /model <name>       Switch to a different model
  /host               Show current backend host (ollama or llama.cpp)
  /host <url>         Connect to a different backend instance
  (backend is chosen at startup: --provider ollama|llamacpp)
  /token              Show whether an auth token is set (masked)
  /token <key>        Set a Bearer token for authenticated remote hosts
  /clear-token        Remove the current auth token
  /code               Switch to coding mode (full tools)
  /design             Switch to design mode (read-only tools)
  /plan               Switch to plan mode (investigate → plan → approve → execute)
  /plan show          Show the current plan and its progress
  /plan run           Approve and execute the plan (re-reads .momo-plan.md edits)
  /plan resume        Continue a paused plan from its current step
  /plan cancel        Discard the plan and delete .momo-plan.md
  /chat               Switch to chat mode (read files, ask questions)
  /momo               Switch to momo companion mode (talk to the cat)
  /clear              Clear conversation history and discard any active plan
  /new                Save this session and start a new, empty one
  /retry              Re-send your last message (drops the reply it got)
  /workspace          Show current working directory (alias: /workdir)
  /workspace <path>   Set working directory for file operations
  /tool-output on|off   Show or hide the tool calls pane
  /think-output on|off  Show or hide model thinking/reasoning output  (Shift+T)
  /markdown on|off      Enable or disable markdown rendering  (Shift+M)
  /diff on|off          Show or hide diffs of file edits  (Shift+D)
  /diff-style git|compact  Choose diff presentation (git-style or compact)
  /companion on|off     Show or hide the momo companion bar  (Shift+Q)
  /companion-idle-recap         Show whether momo recaps recent turns when you're idle
  /companion-idle-recap on|off  momo recaps the last turns in its bubble after you've been idle
  /companion-idle-recap <secs>  Set how long you must be idle first (default 90)
  Shift+C               Interrupt a running LLM response
  /compact            Compact context with LLM summary of dropped history
  /fast-compact       Compact context without LLM summarisation (instant)
  /context            Show context limit and current usage
  /context <n>        Set context token limit (e.g. /context 8192)
  /context <n>%       Set context limit as % of model max (e.g. /context 75%); saved in session
  /tool-result        Show current tool result character cap
  /tool-result <n>    Set cap (e.g. /tool-result 8000); 0 = unlimited
  /think              Show thinking mode state (on/off)
  /think on|off       Enable or disable model thinking/reasoning mode
  /run-confirm        Show run_command confirmation state (on/off)
  /run-confirm on|off Ask y/N before each run_command  (Shift+P toggles)
  /tools              Show whether tool calls are enabled (on/off)
  /tools on|off       Enable or disable tool calls entirely
  /net                Show internet access state (off/on/local)
  /net on|off         Allow or block fetch_url reaching the public internet
  /net local          Also allow localhost and the LAN (off by default)
  /net-confirm        Show whether fetch_url writes ask for confirmation
  /net-confirm on|off Ask y/N before each POST/PUT/PATCH/DELETE (default on)
  /net-max-bytes      Show the fetch_url download size cap
  /net-max-bytes <n>  Set it, in bytes or with a unit: 200000, 500kb, 2mb
  /net-max-chars      Show how much page text one fetch_url call returns
  /net-max-chars <n>  Set it in characters (default 24000); the rest is paged
  /guides             Show whether project guide files are loaded, and which
  /guides on|off      Put AGENTS.md / CLAUDE.md / ... from the workdir in the system prompt
                      (re-read on new session, /clear and compaction)
  /guides reload      Re-read the guide files now
  /index              Show the code index: memory per category and language, state
  /index on|off       Index the workdir in memory and give the model the index_* search tools
  /index rebuild      Forget the index and build it again
  /index save|load    Write the index to disk now, or load the saved one
  /index-max-mem <n>  Memory budget for the index (default 100mb)
  /index-persist on|off  Load the saved index at start, save it on exit
  /index-route on|off  Answer plain-text grep_files / find_files from the index (default off)
  /list-skills        List available skills and show which are active
  /load-skill <name>  Append a skill's instructions to the system prompt
  /unload-skill <name> Remove a skill from the system prompt
  /sessions           List recent sessions with mode and model info
  /export [filename]  Export conversation to a Markdown file
  /copy               Copy last assistant message to clipboard
  /copy all           Copy full conversation to clipboard
  /cost               Show token usage for this session by mode and model
  /session            Show current session file
  /session <name>     Load a saved session by name or prefix
  /help               Show this help
  /exit | /quit       Save session and exit

File inspection (no model round-trip):
  /ls [path]                       List directory contents
  /read <path> [start] [end]       Read a file (optional line range)
  /grep <pattern> [path_or_dir]    Regex search in a file or directory
""".strip()


def help_commands() -> list[dict]:
    """Parse the /help text into [{cmd, usage, desc}] for autocomplete."""
    out = []
    for line in _HELP.splitlines():
        m = re.match(r"\s{2}(/.+?)\s{2,}(\S.*)$", line)
        if m:
            usage = m.group(1).strip()
            out.append({"cmd": usage.split()[0], "usage": usage, "desc": m.group(2).strip()})
    return out

