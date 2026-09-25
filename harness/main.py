import argparse
import curses
import secrets
import signal
import sys
import time
from pathlib import Path

from . import code_index
from . import net as net_mod
from . import session as session_mod
from .controller import Controller
from .harness import Harness, ChatEvent
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
                        help="Ceiling on a single fetch_url download, in bytes or with a "
                             "unit: 200000, 500kb, 2mb (default: 2mb)")
    parser.add_argument("--net-max-chars", type=int, default=None, metavar="N",
                        help="Characters of page text one fetch_url call returns; the model "
                             "pages through the rest (default: 24000)")
    parser.add_argument("--no-stream", action="store_true", default=False,
                        help="Wait for complete replies instead of streaming them as they are generated")
    parser.add_argument("--companion-idle-recap", action=argparse.BooleanOptionalAction, default=None,
                        help="momo recaps the last turns in its speech bubble when you're idle "
                             "(default: last /companion-idle-recap setting, else off)")
    parser.add_argument("--companion-idle-recap-secs", default=None, type=int, metavar="N",
                        help="Seconds of inactivity before momo recaps (default 90)")
    parser.add_argument("--guides", action=argparse.BooleanOptionalAction, default=None,
                        help="Put the workdir's AGENTS.md / CLAUDE.md / ... in the system prompt "
                             "(default: last /guides setting, else off)")
    parser.add_argument("--index", action=argparse.BooleanOptionalAction, default=None,
                        help="Index the workdir in memory and give the model the index_* search "
                             "tools (default: last /index setting, else on)")
    parser.add_argument("--index-max-mem", default=None, metavar="SIZE",
                        help="Memory budget for the code index: 100mb, 512kb, 1gb (default: last "
                             "/index-max-mem setting, else 100mb)")
    parser.add_argument("--index-max-files", type=int, default=None, metavar="N",
                        help="Most files the code index covers (default: last /index-max-files "
                             "setting, else 100000)")
    parser.add_argument("--index-workers", default=None, metavar="N",
                        help="Processes the code index builds in: auto or 1-"
                             f"{code_index.MAX_WORKERS} (default: last /index-workers setting, "
                             "else auto)")
    parser.add_argument("--index-persist", action=argparse.BooleanOptionalAction, default=None,
                        help="Load the saved code index at start and save it on exit, in "
                             "~/.momo-harness/index/ (default: last /index-persist setting, else on)")
    parser.add_argument("--index-route", action=argparse.BooleanOptionalAction, default=None,
                        help="With the code index on, answer plain-text grep_files and file-name "
                             "find_files from the index (default: last /index-route setting, else on)")
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

    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        print(f"error: --workdir is not a directory: {workdir}", file=sys.stderr)
        sys.exit(1)

    prefs = session_mod.load_prefs()
    provider = args.provider or prefs.get("provider") or "ollama"
    host = args.host or _DEFAULT_HOSTS.get(provider, _DEFAULT_HOSTS["ollama"])
    model = args.model or prefs.get("model") or "qwen3.5:9b"
    harness = Harness(host=host, model=model, workdir=workdir, provider=provider)
    if args.context is not None and args.context < 256:
        parser.error("--context: expected at least 256")
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
    if args.net_max_chars:
        if not 1000 <= args.net_max_chars <= net_mod.HARD_MAX_CHARS:
            parser.error(f"--net-max-chars: expected 1000..{net_mod.HARD_MAX_CHARS}")
        harness.net_max_chars = args.net_max_chars
    harness.idle_recap = bool(args.companion_idle_recap if args.companion_idle_recap is not None
                              else prefs.get("idle_recap", False))
    harness.idle_recap_secs = max(10, args.companion_idle_recap_secs or prefs.get("idle_recap_secs") or 90)
    harness.guides = bool(args.guides if args.guides is not None else prefs.get("guides", False))
    if args.index_max_mem:
        size = net_mod.parse_size(args.index_max_mem)
        if not size or size < code_index.MIN_MAX_BYTES:
            parser.error(f"--index-max-mem: not a size of at least 1mb: {args.index_max_mem}")
        harness.index_max_bytes = size
    elif isinstance(prefs.get("index_max_mem"), int) and prefs["index_max_mem"] >= code_index.MIN_MAX_BYTES:
        harness.index_max_bytes = prefs["index_max_mem"]
    if args.index_max_files is not None:
        if args.index_max_files < code_index.MIN_MAX_FILES:
            parser.error(f"--index-max-files: expected at least {code_index.MIN_MAX_FILES}")
        harness.index_max_files = args.index_max_files
    elif (isinstance(prefs.get("index_max_files"), int)
          and prefs["index_max_files"] >= code_index.MIN_MAX_FILES):
        harness.index_max_files = prefs["index_max_files"]
    if args.index_workers is not None:
        w = args.index_workers.strip().lower()
        if w == "auto":
            harness.index_workers = 0
        elif w.isdigit() and 1 <= int(w) <= code_index.MAX_WORKERS:
            harness.index_workers = int(w)
        else:
            parser.error(f"--index-workers: expected auto or 1-{code_index.MAX_WORKERS}")
    elif (isinstance(prefs.get("index_workers"), int)
          and 0 <= prefs["index_workers"] <= code_index.MAX_WORKERS):
        harness.index_workers = prefs["index_workers"]
    harness.index_persist = bool(args.index_persist if args.index_persist is not None
                                 else prefs.get("index_persist", True))
    harness.index_route = bool(args.index_route if args.index_route is not None
                               else prefs.get("index_route", True))
    index_on = bool(args.index if args.index is not None else prefs.get("index", True))
    harness.reload_guides()   # load_session re-reads them for a restored workdir

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
    if args.context is not None:      # after the restore, which would overwrite it
        harness.context_pct = None
        harness.context_fixed = True
        harness.context_limit = args.context
    # The saved model and context size may be stale: the server was relaunched
    # with another model or context, or the model was removed.
    harness.event_queue.put(ChatEvent("system", harness.check_backend()))

    # Start the index only now, so a restored session's workdir is the one indexed.
    if index_on:
        harness.event_queue.put(ChatEvent("system", harness.set_index(True)))

    # One Controller drives the harness for every frontend (TUI and web).
    controller = Controller(harness)
    if (guides_note := harness.guides_summary()):
        harness.event_queue.put(ChatEvent("system", guides_note))

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
        harness.logger.close()


if __name__ == "__main__":
    main()
