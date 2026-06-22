from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import session as session_mod
from .harness import Harness, ChatEvent


@dataclass
class CommandResult:
    handled: bool
    output: str | None = None
    exit_app: bool = False


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
        harness.client.set_model(arg)
        harness._emit_status()
        return CommandResult(handled=True, output=f"Model set to: {arg}")

    if cmd == "/host":
        if not arg:
            return CommandResult(handled=True, output=f"Ollama host: {harness.client.host}")
        harness.client.set_host(arg)
        harness._emit_status()
        return CommandResult(handled=True, output=f"Host set to: {arg}")

    if cmd == "/code":
        harness.set_mode("coding")
        return CommandResult(handled=True, output="Switched to coding mode")

    if cmd == "/design":
        harness.set_mode("design")
        return CommandResult(handled=True, output="Switched to design mode")

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
            return CommandResult(handled=True, output=f"ERROR: not a directory: {arg}")
        harness.workdir = p
        if harness.mode == "coding":
            harness.set_mode("coding")  # refresh system prompt with new workdir
        harness._emit_status()
        return CommandResult(handled=True, output=f"Working directory set to: {p}")

    if cmd == "/compact":
        notice = harness.compact()
        harness._emit_status()
        return CommandResult(handled=True, output=notice)

    if cmd == "/context":
        if not arg:
            pct = harness._ctx_pct()
            est = harness._token_estimate
            return CommandResult(handled=True,
                                 output=(f"Context limit: {harness.context_limit} tokens | "
                                         f"usage: ~{est} tokens ({pct}%)"))
        try:
            n = int(arg)
            if n < 256:
                return CommandResult(handled=True, output="ERROR: context limit must be >= 256")
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
        return CommandResult(handled=True, output=msg)

    return CommandResult(handled=False)


_HELP = """
Available commands:
  /model              List available Ollama models
  /model <name>       Switch to a different model
  /host               Show current Ollama host
  /host <url>         Connect to a different Ollama instance
  /code               Switch to coding mode (full tools)
  /design             Switch to design mode (read-only tools)
  /clear              Clear conversation history
  /workdir            Show current working directory
  /workdir <path>     Set working directory for file operations
  /compact            Compact context (remove old tool calls / messages)
  /context            Show context limit and current usage
  /context <n>        Set context token limit (e.g. /context 8192)
  /tool-result        Show current tool result character cap
  /tool-result <n>    Set cap (e.g. /tool-result 8000); 0 = unlimited
  /session            Show current session file
  /session <name>     Load a saved session by name or prefix
  /help               Show this help
  /exit | /quit       Save session and exit
""".strip()
