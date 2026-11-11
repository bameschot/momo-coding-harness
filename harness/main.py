import argparse
import curses
import secrets
import signal
import sys
import threading
import time
from pathlib import Path

from . import session as session_mod
from . import settings as settings_mod
from .controller import Controller
from .events import ChatEvent
from .harness import Harness
from .prompts import MODES
from .tui import run_tui
from .web import tls as tls_mod
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
    parser.add_argument("--workspace", "--workdir", default=None, metavar="PATH",
                        dest="workdir", help="Root directory for all file operations "
                                             "(default: the last session's workspace, else "
                                             "the current directory; with PATH, that "
                                             "workspace's last session is reopened)")
    parser.add_argument("--context", default=None, type=int, metavar="N",
                        help="Override context token limit (default: read from model)")
    parser.add_argument("--mode",    default=None, choices=list(MODES),
                        help="Starting mode (default: last-selected mode, else design; "
                             "ignored when restoring a session)")
    parser.add_argument("--fresh", action="store_true", default=False,
                        help="Start a new session instead of restoring the last one")
    parser.add_argument("--no-think", action="store_true", default=False,
                        help="Same as --think off")
    parser.add_argument("--net", choices=("off", "on", "local"), default="off",
                        help="Let the model fetch URLs with fetch_url: 'on' reaches the "
                             "public internet, 'local' also allows localhost and the LAN "
                             "(default: off; toggle at runtime with /net)")
    parser.add_argument("--net-confirm", choices=("on", "off"), default="on",
                        help="Ask y/N before each fetch_url POST/PUT/PATCH/DELETE "
                             "(default: on). Writes to private addresses always ask.")
    parser.add_argument("--no-stream", action="store_true", default=False,
                        help="Wait for complete replies instead of streaming them as they are generated")
    parser.add_argument("--mcp-config", action="append", default=[], metavar="PATH",
                        help="An extra MCP config file (mcpServers format); may be repeated")
    parser.add_argument("--web", action=argparse.BooleanOptionalAction, default=True,
                        help="Serve the browser chat UI alongside the TUI")
    parser.add_argument("--web-host", default="127.0.0.1", metavar="HOST",
                        help="Interface for the web UI (non-loopback hosts require an access token)")
    parser.add_argument("--web-port", default=8765, type=int, metavar="PORT",
                        help="Port for the web UI")
    parser.add_argument("--web-token", default=None, metavar="TOKEN",
                        help="Access token for the web UI (default: generated when --web-host is not loopback)")
    parser.add_argument("--web-tls", choices=("off", "auto"), default="off",
                        help="Serve the web UI over HTTPS: 'auto' creates momo's own local CA and a "
                             "certificate for this machine (needs the openssl command)")
    parser.add_argument("--web-cert", default=None, metavar="PEM",
                        help="Serve the web UI over HTTPS with this certificate")
    parser.add_argument("--web-key", default=None, metavar="PEM",
                        help="Private key for --web-cert (if not inside the certificate file)")
    parser.add_argument("--web-tls-name", action="append", default=[], metavar="NAME",
                        help="Extra DNS name or IP for the --web-tls auto certificate (repeatable)")
    parser.add_argument("--web-insecure", action="store_true", default=False,
                        help="Plain HTTP without an access token, even off loopback. "
                             "Anyone who can reach the address can run commands as you")
    parser.add_argument("--web-allow-host", action="append", default=[], metavar="NAME",
                        help="Extra name accepted in the Host header with --web-insecure (repeatable)")
    parser.add_argument("--headless", action="store_true", default=False,
                        help="Run only the web UI (no terminal UI)")
    settings_mod.add_arguments(parser)    # the saved settings' flags (settings.py)
    args = parser.parse_args()
    if args.headless and not args.web:
        parser.error("--headless needs the web UI; drop --no-web")
    if args.web_tls == "auto" and args.web_cert:
        parser.error("--web-tls auto makes its own certificate; drop --web-cert or --web-tls auto")
    if args.web_key and not args.web_cert:
        parser.error("--web-key needs --web-cert")
    if args.web_tls_name and args.web_tls != "auto":
        parser.error("--web-tls-name only applies to --web-tls auto")
    if args.web_insecure and (args.web_token or args.web_cert or args.web_tls == "auto"):
        parser.error("--web-insecure is plain HTTP without a token; "
                     "it can't be combined with --web-token, --web-cert or --web-tls auto")
    if args.web_allow_host and not args.web_insecure:
        parser.error("--web-allow-host only applies to --web-insecure "
                     "(with a token, the Host header is not restricted)")

    given = Path(args.workdir).expanduser().resolve() if args.workdir else None
    if given is not None and not given.is_dir():
        print(f"error: --workdir is not a directory: {given}", file=sys.stderr)
        sys.exit(1)
    # What to reopen: the last session in its workspace, or with --workspace that
    # workspace's last session (session.restore_target); --fresh reopens nothing.
    last, remembered = (None, None) if args.fresh else session_mod.restore_target(given)
    workdir = given or remembered or Path(".").resolve()

    if args.context is not None and args.context < 256:
        parser.error("--context: expected at least 256")

    prefs = session_mod.load_prefs()
    provider = args.provider or prefs.get("provider") or "ollama"
    host = args.host or _DEFAULT_HOSTS.get(provider, _DEFAULT_HOSTS["ollama"])
    model = args.model or prefs.get("model") or "qwen3.5:9b"
    try:
        harness = Harness(host=host, model=model, workdir=workdir, provider=provider)
    except ValueError as e:        # an unusable --host (or saved provider)
        parser.error(str(e))

    def _thread_crash(hook_args):
        # Any thread's uncaught exception: into the session log, never onto the
        # terminal (the default hook prints a traceback over the curses screen).
        if hook_args.exc_type is SystemExit:
            return
        try:
            harness.logger.log_error(f"thread {getattr(hook_args.thread, 'name', '?')}",
                                     hook_args.exc_value)
        except Exception:
            pass
    threading.excepthook = _thread_crash

    # Every saved setting: its flag, else its pref, else the Harness default.
    if args.no_think:
        args.think = "off"
    returned = settings_mod.apply(harness, args, prefs, parser.error)
    index_on = returned["index"]
    if args.no_stream:
        harness.stream = False
    # Session-only by design: never saved, so a flag or the safe default.
    harness.net_access = args.net
    harness.net_confirm = args.net_confirm == "on"
    harness.rebuild_system_prompt()     # the run_command schema depends on run_mode
    harness.reload_guides()   # load_session re-reads them for a restored workdir

    # Restore last session unless --fresh
    if last is None:
        harness._mark_current()
    if last is not None:
        harness.load_session(last)
        # Backend flags given on the command line beat the backend the restored
        # session was saved with — otherwise `--provider ollama` is silently undone.
        if args.provider or args.host or args.model:
            new_provider = args.provider or harness.provider
            switched = new_provider != harness.provider
            harness.switch_backend(
                new_provider,
                host=args.host or (_DEFAULT_HOSTS[new_provider] if switched else harness.client.host),
                model=args.model or (model if switched else harness.client.model))
        # Likewise an explicit --workdir beats the restored session's, so does the
        # remembered one (newer: /workspace in a session not saved since), and a
        # restored workdir that no longer exists falls back to this one.
        if args.workdir is not None or remembered is not None or not harness.workdir.is_dir():
            if not harness.workdir.is_dir():
                harness.event_queue.put(ChatEvent(
                    "system", f"The session's working directory {harness.workdir} no longer "
                              f"exists; using {workdir}."))
            if harness.workdir.resolve() != workdir:
                harness.workdir = workdir
                harness._mark_current()
                harness.set_mode(harness.mode)   # the system prompt names the workdir
                harness.reload_guides()
    else:
        mode = args.mode or prefs.get("mode")
        harness.set_mode(mode if mode in MODES else "design")
    if args.context is not None:      # after the restore, which would overwrite it
        harness.context_pct = None
        harness.context_fixed = True
        harness.context_limit = args.context
    # The saved model and context size may be stale: the server was relaunched
    # with another model or context, or the model was removed.
    harness.event_queue.put(ChatEvent("system", harness.check_backend()))

    # Start the index only now, so a restored session's workdir is the one indexed.
    if index_on:
        harness.event_queue.put(ChatEvent("system", harness.set_index(True, clear_choices=False)))

    # After the restore, so project servers come from the session's workdir.
    if (mcp_note := harness.start_mcp([Path(p).expanduser() for p in args.mcp_config])):
        harness.event_queue.put(ChatEvent("system", mcp_note))

    # One Controller drives the harness for every frontend (TUI and web).
    controller = Controller(harness)
    if (guides_note := harness.guides_summary()):
        harness.event_queue.put(ChatEvent("system", guides_note))
    for tools_note in (harness.custom_tools.summary(), harness.workspace_tools.summary()):
        if tools_note:
            harness.event_queue.put(ChatEvent("system", tools_note))

    web = None
    web_notes: list[str] = []
    if args.web:
        if args.web_insecure:
            token = None
        else:
            token = args.web_token or (None if is_loopback(args.web_host) else secrets.token_urlsafe(24))
        try:
            cert = key = ca = None
            auto = None
            if args.web_tls == "auto":
                auto = tls_mod.ensure(args.web_host, args.web_tls_name)
                cert, key, ca = auto.cert, auto.key, auto.ca
            elif args.web_cert:
                cert, key = args.web_cert, args.web_key
            extra_hosts = ()
            if args.web_insecure and not is_loopback(args.web_host):
                extra_hosts = tls_mod.local_names(args.web_host, args.web_allow_host)
            web = start_web_server(controller, args.web_host, args.web_port, token,
                                   cert=cert, key=key, ca_pem=ca, extra_hosts=extra_hosts)
            controller.web_url = web.url
            line = f"Web UI: {web.url}"
            if args.web_insecure and not is_loopback(args.web_host):
                line += (" — no access token: anyone who can reach this address can read "
                         "the conversation and run commands as you.")
            web_notes.append(line)
            if auto is not None:
                web_notes.append(
                    f"HTTPS: trust momo's local CA once per device — {auto.ca} "
                    f"(also at {web.url.split('?')[0]}momo-ca.pem), "
                    f"SHA-256 {auto.ca_fingerprint}")
            for note in web_notes:
                harness.event_queue.put(ChatEvent("system", note))
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
            print(f"momo {web_notes[0]}", *web_notes[1:], "Press Ctrl+C to stop.",
                  sep="\n", flush=True)
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
        harness.shutdown_index()
        harness.shutdown_mcp()
        harness.logger.close()


if __name__ == "__main__":
    main()
