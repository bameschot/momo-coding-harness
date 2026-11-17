import argparse
import curses
import os
import secrets
import select
import signal
import stat
import sys
import threading
import time
from pathlib import Path

from . import attachments as attach_mod
from . import headless as headless_mod
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
    parser.add_argument("--web-public-url", default=None, metavar="URL",
                        help="The address to show for the web UI, when it is reached through a "
                             "port mapping or proxy (e.g. a container's published port)")
    parser.add_argument("--web-only", action="store_true", default=False,
                        help="Run only the web UI (no terminal UI)")
    # The old name of --web-only: "headless" now means -p.
    parser.add_argument("--headless", action="store_true", default=False,
                        help=argparse.SUPPRESS)
    headless = parser.add_argument_group(
        "headless run", "Run one prompt without a UI: the answer goes to stdout, progress "
        "to stderr. Questions get automatic answers (confirmations: no, unless --yes).")
    headless.add_argument("-p", "--prompt", default=None, metavar="TEXT",
                          help="Run this prompt and exit ('-' reads it from stdin; piped "
                               "stdin with TEXT is attached to it)")
    headless.add_argument("--yes", action="store_true", default=False,
                          help="With -p: answer yes to confirmations (run-confirm, network "
                               "writes, MCP and tool approvals)")
    headless.add_argument("--quiet", action="store_true", default=False,
                          help="With -p: no progress on stderr")
    headless.add_argument("--timeout", default=None, type=float, metavar="SECS",
                          help="With -p: stop the run after SECS seconds (exit code 124)")
    headless.add_argument("--continue", dest="continue_session", action="store_true",
                          default=False,
                          help="With -p: continue the workspace's last session instead of a new one")
    headless.add_argument("--session", default=None, metavar="NAME",
                          help="With -p: continue this saved session")
    parser.add_argument("--environment-note", action="append", default=[], metavar="TEXT",
                        help="An extra line for the system prompt's environment block, e.g. what "
                             "a container provides (repeatable; not saved)")
    parser.add_argument("--turn-marker", default=None, metavar="PATH",
                        help="Rewrite this JSON file after every finished turn, for a watcher "
                             "outside momo (momo-box uses it to sync the workspace)")
    parser.add_argument("--box-status", default=None, metavar="DIR",
                        help="The read-only folder where momo-box writes the box's status "
                             "(setup, sync, network) for the web UI's Setup panel")
    settings_mod.add_arguments(parser)    # the saved settings' flags (settings.py)
    args = parser.parse_args()
    if args.headless:
        print("note: --headless is now --web-only (momo -p runs headless)", file=sys.stderr)
        args.web_only = True
    one_shot = args.prompt is not None
    if not one_shot:
        given_only = [flag for flag, on in (("--yes", args.yes), ("--quiet", args.quiet),
                                            ("--timeout", args.timeout is not None),
                                            ("--continue", args.continue_session),
                                            ("--session", args.session is not None)) if on]
        if given_only:
            parser.error(f"{', '.join(given_only)} only applies with -p/--prompt")
    else:
        if args.web_only:
            parser.error("-p runs without any UI; drop --web-only")
        if args.continue_session and args.session:
            parser.error("--continue and --session both pick a session; use one")
        if args.fresh and (args.continue_session or args.session):
            parser.error("--fresh contradicts --continue/--session")
        if args.timeout is not None and args.timeout <= 0:
            parser.error("--timeout: expected a positive number of seconds")
    if args.web_only and not args.web:
        parser.error("--web-only needs the web UI; drop --no-web")
    if args.web_tls == "auto" and args.web_cert:
        parser.error("--web-tls auto makes its own certificate; drop --web-cert or --web-tls auto")
    if args.web_key and not args.web_cert:
        parser.error("--web-key needs --web-cert")
    if args.web_tls_name and args.web_tls != "auto":
        parser.error("--web-tls-name only applies to --web-tls auto")
    if args.web_insecure and (args.web_token or args.web_cert or args.web_tls == "auto"):
        parser.error("--web-insecure is plain HTTP without a token; "
                     "it can't be combined with --web-token, --web-cert or --web-tls auto")
    if args.web_public_url and not args.web:
        parser.error("--web-public-url needs the web UI; drop --no-web")
    if args.web_public_url and not args.web_public_url.startswith(("http://", "https://")):
        parser.error("--web-public-url: expected an http:// or https:// URL")
    if args.web_allow_host and not args.web_insecure:
        parser.error("--web-allow-host only applies to --web-insecure "
                     "(with a token, the Host header is not restricted)")

    given = Path(args.workdir).expanduser().resolve() if args.workdir else None
    if given is not None and not given.is_dir():
        print(f"error: --workdir is not a directory: {given}", file=sys.stderr)
        sys.exit(1)
    # What to reopen: the last session in its workspace, or with --workspace that
    # workspace's last session (session.restore_target); --fresh reopens nothing.
    # A -p run starts a new session unless --continue or --session says otherwise.
    if one_shot:
        prompt = _headless_prompt(args.prompt, parser)
        if args.session:
            last, remembered = session_mod.find_session(args.session), None
            if last is None:
                parser.error(f"--session: no saved session matches {args.session!r}")
        elif args.continue_session:
            last, remembered = session_mod.restore_target(given)
        else:
            last, remembered = None, None
    else:
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
    if one_shot:    # nobody will answer questions (the system prompt says so too)
        harness.unattended = "yes" if args.yes else "decline"
    harness.environment_notes = [n for n in args.environment_note if n.strip()]
    if args.turn_marker:
        harness.turn_marker = Path(args.turn_marker).expanduser()
    if args.box_status:
        harness.box_status_dir = Path(args.box_status).expanduser()
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

    if (guides_note := harness.guides_summary()):
        harness.event_queue.put(ChatEvent("system", guides_note))
    for tools_note in (harness.custom_tools.summary(), harness.workspace_tools.summary()):
        if tools_note:
            harness.event_queue.put(ChatEvent("system", tools_note))

    if one_shot:    # no frontend, so no Controller, web server or curses
        code = headless_mod.EXIT_ERROR
        try:
            code = headless_mod.run(harness, prompt, quiet=args.quiet, timeout=args.timeout)
        finally:
            harness.shutdown_index()
            harness.shutdown_mcp()
            harness.logger.close()
        sys.exit(code)

    # One Controller drives the harness for every frontend (TUI and web).
    controller = Controller(harness)

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
            shown = _public_url(args.web_public_url, token) if args.web_public_url else web.url
            controller.web_url = shown
            line = f"Web UI: {shown}"
            if args.web_insecure and not is_loopback(args.web_host):
                line += (" — no access token: anyone who can reach this address can read "
                         "the conversation and run commands as you.")
            web_notes.append(line)
            if auto is not None:
                web_notes.append(
                    f"HTTPS: trust momo's local CA once per device — {auto.ca} "
                    f"(also at {shown.split('?')[0]}momo-ca.pem), "
                    f"SHA-256 {auto.ca_fingerprint}")
            for note in web_notes:
                harness.event_queue.put(ChatEvent("system", note))
        except OSError as e:
            msg = f"Web UI failed to start on {args.web_host}:{args.web_port}: {e}"
            if args.web_only:
                print(f"error: {msg}", file=sys.stderr)
                sys.exit(1)
            harness.event_queue.put(ChatEvent("system", msg))

    try:
        if args.web_only:
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


def _public_url(base: str, token: str | None) -> str:
    """--web-public-url as the address to open: a trailing slash, plus the token."""
    url = base.split("?", 1)[0].rstrip("/") + "/"
    return url + (f"?token={token}" if token else "")


# How long `momo -p TEXT` waits for piped stdin to deliver anything (data or EOF)
# before it runs without it.  A non-TTY stdin that never closes (a CI runner, a
# parent process holding the pipe open) would otherwise block the run forever.
_STDIN_WAIT = 3.0


def _stdin_to_attach() -> str | None:
    """Piped stdin to attach to a -p TEXT prompt, or None.  Only a pipe or a
    regular file is read (`cmd | momo -p`, `momo -p … < file`): a TTY is the
    user's terminal, and a character device or socket is whatever the parent
    left there."""
    stdin = sys.stdin
    if stdin is None:
        return None
    try:
        mode = os.fstat(stdin.fileno()).st_mode
    except (OSError, ValueError, AttributeError):
        return None
    if stat.S_ISREG(mode):
        return stdin.read()
    if not stat.S_ISFIFO(mode):
        return None
    ready, _, _ = select.select([stdin], [], [], _STDIN_WAIT)
    if not ready:
        print(f"note: nothing arrived on stdin within {_STDIN_WAIT:g} s, so it is not "
              "attached (redirect it with `< file`, or use `-p -` to wait for it)",
              file=sys.stderr, flush=True)
        return None
    return stdin.read()


def _headless_prompt(text: str, parser: argparse.ArgumentParser) -> str:
    """The -p prompt: TEXT, stdin for '-', or TEXT with piped stdin attached
    (`pytest 2>&1 | momo -p "why does this fail?"`)."""
    try:
        if text == "-":
            piped = sys.stdin.read() if sys.stdin is not None else ""
            if not piped.strip():
                parser.error("-p -: stdin is empty")
            return piped
        piped = _stdin_to_attach()
    except (OSError, UnicodeDecodeError) as e:
        parser.error(f"-p: could not read stdin: {e}")
    if not text.strip() and not (piped or "").strip():
        parser.error("-p: the prompt is empty")
    if piped and piped.strip():
        return attach_mod.compose(text, [{"name": "stdin", "text": piped}])
    return text


if __name__ == "__main__":
    main()
