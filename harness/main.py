import argparse
import curses
import sys
from pathlib import Path

from . import session as session_mod
from .harness import Harness
from .tui import run_tui


def main():
    parser = argparse.ArgumentParser(
        prog="momo-coding-harness",
        description="AI coding harness powered by Ollama",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host",    default="http://localhost:11434", metavar="URL",
                        help="Ollama base URL")
    parser.add_argument("--model",   default=None, metavar="NAME",
                        help="Ollama model name (default: last-used model or qwen3.5:9b)")
    parser.add_argument("--workspace", "--workdir", default=".", metavar="PATH",
                        dest="workdir", help="Root directory for all file operations")
    parser.add_argument("--context", default=None, type=int, metavar="N",
                        help="Override context token limit (default: read from model)")
    parser.add_argument("--mode",    default="design", choices=["design", "coding"],
                        help="Starting mode (ignored when restoring a session)")
    parser.add_argument("--max-tool-result", default=0, type=int, metavar="N",
                        help="Max chars returned by a single tool call (0 = unlimited)")
    parser.add_argument("--fresh", action="store_true", default=False,
                        help="Start a new session instead of restoring the last one")
    parser.add_argument("--no-think", action="store_true", default=False,
                        help="Disable model thinking/reasoning mode (default: on)")
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        print(f"error: --workdir is not a directory: {workdir}", file=sys.stderr)
        sys.exit(1)

    model = args.model or session_mod.load_prefs().get("model") or "qwen3.5:9b"
    harness = Harness(host=args.host, model=model, workdir=workdir)
    if args.context is not None:
        harness.context_limit = args.context
    harness.max_tool_result = args.max_tool_result
    if args.no_think:
        harness.think = False

    # Restore last session unless --fresh
    sessions = session_mod.list_sessions()
    if not args.fresh and sessions:
        harness.load_session(sessions[0])
    else:
        harness.set_mode(args.mode)

    try:
        curses.wrapper(run_tui, harness)
    except SystemExit:
        pass
    except KeyboardInterrupt:
        harness._autosave()
        harness.logger.close()
    finally:
        harness.logger.close()


if __name__ == "__main__":
    main()
