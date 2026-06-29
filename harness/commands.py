from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import session as session_mod
from .harness import Harness, ChatEvent
from .tools import dispatch


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
    companion: bool | None = None           # TUI shows/hides momo companion bar
    replay_session: bool = False            # TUI replays loaded session messages into chat buffer
    run_compact: bool = False               # TUI runs compact on worker thread
    compact_summarise: bool = True          # passed to compact_threaded()


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
            current = harness.client.model
            lines = [f"  {'*' if m == current else ' '} {m}" for m in models]
            return CommandResult(handled=True, output="Models:\n" + "\n".join(lines))
        harness.set_model(arg)
        return CommandResult(handled=True, output=f"Model set to: {arg} (ctx: {harness.context_limit})")

    if cmd == "/host":
        if not arg:
            return CommandResult(handled=True, output=f"Ollama host: {harness.client.host}")
        harness.client.set_host(arg)
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

    if cmd == "/write":
        harness.set_mode("writing")
        return CommandResult(handled=True, output="Switched to writing mode")

    if cmd == "/data":
        harness.set_mode("data")
        return CommandResult(handled=True, output="Switched to data mode")

    if cmd == "/chat":
        harness.set_mode("chat")
        return CommandResult(handled=True, output="Switched to chat mode")

    if cmd == "/momo":
        harness.set_mode("momo")
        return CommandResult(handled=True, output="Switched to momo mode")

    if cmd == "/clear":
        system_msg = harness.messages[0]
        harness.messages = [system_msg]
        harness._token_estimate = harness._estimate()
        harness._emit_status()
        return CommandResult(handled=True, output="Conversation cleared")

    if cmd == "/workdir":
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
                return f"Created and set working directory: {p}"
            return CommandResult(
                handled=True,
                confirm_prompt=f"Directory does not exist: {p}\nCreate it?",
                confirm_action=_create,
            )
        harness.workdir = p
        harness.set_mode(harness.mode)  # always refresh system prompt with new workdir
        return CommandResult(handled=True, output=f"Working directory set to: {p}")

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

    if cmd == "/compact":
        return CommandResult(handled=True, run_compact=True)

    if cmd == "/fast-compact":
        return CommandResult(handled=True, run_compact=True, compact_summarise=False)

    if cmd == "/context":
        if not arg:
            usage_pct = harness._ctx_pct()
            est = harness._token_estimate
            scale = f" | scale: {harness.context_pct}% of model max" if harness.context_pct is not None else ""
            return CommandResult(handled=True,
                                 output=(f"Context limit: {harness.context_limit} tokens | "
                                         f"usage: ~{est} tokens ({usage_pct}%){scale}"))
        if arg.endswith("%"):
            try:
                n = int(arg[:-1])
                if not 1 <= n <= 100:
                    return CommandResult(handled=True, output="ERROR: percentage must be 1–100")
                harness.context_pct = n
                harness._sync_context_limit(emit=False)
                harness._emit_status()
                return CommandResult(handled=True,
                                     output=f"Context limit set to {n}% of model max: {harness.context_limit:,} tokens")
            except ValueError:
                return CommandResult(handled=True, output=f"ERROR: invalid percentage: {arg}")
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
  /model              List available Ollama models
  /model <name>       Switch to a different model
  /host               Show current Ollama host
  /host <url>         Connect to a different Ollama instance
  /token              Show whether an auth token is set (masked)
  /token <key>        Set a Bearer token for authenticated remote hosts
  /clear-token        Remove the current auth token
  /code               Switch to coding mode (full tools)
  /design             Switch to design mode (read-only tools)
  /write              Switch to writing mode (document editing tools)
  /data               Switch to data analysis mode (run_command + read tools)
  /chat               Switch to chat mode (read files, ask questions)
  /momo               Switch to momo companion mode (talk to the cat)
  /clear              Clear conversation history
  /workdir            Show current working directory
  /workdir <path>     Set working directory for file operations
  /tool-output on|off   Show or hide the tool calls pane
  /think-output on|off  Show or hide model thinking/reasoning output  (Shift+T)
  /markdown on|off      Enable or disable markdown rendering  (Shift+M)
  /companion on|off     Show or hide the momo companion bar  (Shift+Q)
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
