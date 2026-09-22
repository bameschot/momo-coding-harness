import argparse
import curses
import secrets
import signal
import sys
import time
from pathlib import Path

from . import net as net_mod
from . import session as session_mod
from .controller import Controller
from .harness import Harness, ChatEvent
from .tui import run_tui
from .web.server import is_loopback, start_web_server


# Default base URL per backend when --host is not given.
_DEFAULT_HOSTS = {
    "ollama":   "http://localhost:11434",
    "llamacpp": "http://localhost:8080",
}


def main():
    parser = argparse.ArgumentParser(
        prog="momo-coding-harness",
        description="AI coding harness for local LLMs (Ollama or llama.cpp)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--provider", default=None, choices=["ollama", "llamacpp"],
                        help="LLM backend (default: last-used or ollama)")
    parser.add_argument("--host",    default=None, metavar="URL",
                        help="Backend base URL (default: 11434 for ollama, 8080 for llamacpp)")
    parser.add_argument("--model",   default=None, metavar="NAME",
                        help="Model name (default: last-used model or qwen3.5:9b)")
    parser.add_argument("--workspace", "--workdir", default=".", metavar="PATH",
                        dest="workdir", help="Root directory for all file operations")
    parser.add_argument("--context", default=None, type=int, metavar="N",
                        help="Override context token limit (default: read from model)")
    parser.add_argument("--mode",    default="design", choices=["design", "chat", "plan", "coding", "momo"],
                        help="Starting mode (ignored when restoring a session)")
    parser.add_argument("--max-tool-result", default=0, type=int, metavar="N",
                        help="Max chars returned by a single tool call (0 = unlimited)")
    parser.add_argument("--fresh", action="store_true", default=False,
                        help="Start a new session instead of restoring the last one")
    parser.add_argument("--no-think", action="store_true", default=False,
                        help="Disable model thinking/reasoning mode (default: on)")
    parser.add_argument("--net", choices=("off", "on", "local"), default="off",
                        help="Let the model fetch URLs with fetch_url: 'on' reaches the "
                             "public internet, 'local' also allows localhost and the LAN "
                             "(default: off; toggle at runtime with /net)")
    parser.add_argument("--net-confirm", choices=("on", "off"), default="on",
                        help="Ask y/N before each fetch_url POST/PUT/PATCH/DELETE "
                             "(default: on). Writes to private addresses always ask.")
    parser.add_argument("--net-max-bytes", default=None, metavar="SIZE",
                        help="Ceiling on a single fetch_url response, in bytes or with a "
                             "unit: 200000, 500kb, 2mb (default: 100kb)")
    parser.add_argument("--no-stream", action="store_true", default=False,
                        help="Wait for complete replies instead of streaming them as they are generated")
    parser.add_argument("--companion-idle-recap", action=argparse.BooleanOptionalAction, default=None,
                        help="momo recaps the last turns in its speech bubble when you're idle "
                             "(default: last /companion-idle-recap setting, else off)")
    parser.add_argument("--companion-idle-recap-secs", default=None, type=int, metavar="N",
                        help="Seconds of inactivity before momo recaps (default 90)")
    parser.add_argument("--web", action=argparse.BooleanOptionalAction, default=True,
                        help="Serve the browser chat UI alongside the TUI")
    parser.add_argument("--web-host", default="127.0.0.1", metavar="HOST",
                        help="Interface for the web UI (non-loopback hosts require an access token)")
    parser.add_argument("--web-port", default=8765, type=int, metavar="PORT",
                        help="Port for the web UI")
    parser.add_argument("--web-token", default=None, metavar="TOKEN",
                        help="Access token for the web UI (default: generated when --web-host is not loopback)")
    parser.add_argument("--headless", action="store_true", default=False,
                        help="Run only the web UI (no terminal UI)")
    args = parser.parse_args()
    if args.headless and not args.web:
        parser.error("--headless needs the web UI; drop --no-web")

    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        print(f"error: --workdir is not a directory: {workdir}", file=sys.stderr)
        sys.exit(1)

    prefs = session_mod.load_prefs()
    provider = args.provider or prefs.get("provider") or "ollama"
    host = args.host or _DEFAULT_HOSTS.get(provider, _DEFAULT_HOSTS["ollama"])
    model = args.model or prefs.get("model") or "qwen3.5:9b"
    harness = Harness(host=host, model=model, workdir=workdir, provider=provider)
    if args.context is not None:
        harness.context_limit = args.context
    harness.max_tool_result = args.max_tool_result
    if args.no_think:
        harness.think = False
    if args.no_stream:
        harness.stream = False
    harness.net_access = args.net
    harness.net_confirm = args.net_confirm == "on"
    if args.net_max_bytes:
        if (size := net_mod.parse_size(args.net_max_bytes)):
            harness.net_max_bytes = min(size, net_mod.HARD_MAX_BYTES)
        else:
            parser.error(f"--net-max-bytes: not a size: {args.net_max_bytes}")
    harness.idle_recap = bool(args.companion_idle_recap if args.companion_idle_recap is not None
                              else prefs.get("idle_recap", False))
    harness.idle_recap_secs = max(10, args.companion_idle_recap_secs or prefs.get("idle_recap_secs") or 90)

    # Restore last session unless --fresh
    sessions = session_mod.list_sessions()
    if not args.fresh and sessions:
        harness.load_session(sessions[0])
        # Backend flags given on the command line beat the backend the restored
        # session was saved with — otherwise `--provider ollama` is silently undone.
        if args.provider or args.host or args.model:
            new_provider = args.provider or harness.provider
            switched = new_provider != harness.provider
            harness.switch_backend(
                new_provider,
                host=args.host or (_DEFAULT_HOSTS[new_provider] if switched else harness.client.host),
                model=args.model or (model if switched else harness.client.model))
    else:
        harness.set_mode(args.mode)

    # One Controller drives the harness for every frontend (TUI and web).
    controller = Controller(harness)

    web = None
    if args.web:
        token = args.web_token or (None if is_loopback(args.web_host) else secrets.token_urlsafe(24))
        try:
            web = start_web_server(controller, args.web_host, args.web_port, token)
            controller.web_url = web.url
            harness.event_queue.put(ChatEvent("system", f"Web UI: {web.url}"))
        except OSError as e:
            msg = f"Web UI failed to start on {args.web_host}:{args.web_port}: {e}"
            if args.headless:
                print(f"error: {msg}", file=sys.stderr)
                sys.exit(1)
            harness.event_queue.put(ChatEvent("system", msg))

    try:
        if args.headless:
            # Treat SIGTERM (service managers, `kill`) like Ctrl+C so the session is saved.
            def _on_term(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, _on_term)
            print(f"momo web UI: {web.url}\nPress Ctrl+C to stop.", flush=True)
            while True:
                time.sleep(3600)
        else:
            curses.wrapper(run_tui, harness, controller)
    except SystemExit:
        pass
    except KeyboardInterrupt:
        harness._autosave()
    finally:
        if web is not None:
            web.close()
        harness.logger.close()


if __name__ == "__main__":
    main()
