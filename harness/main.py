import argparse
import curses
import sys
from pathlib import Path

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
    parser.add_argument("--model",   default="devstral-small-2:latest", metavar="NAME",
                        help="Ollama model name")
    parser.add_argument("--workdir", default=".", metavar="PATH",
                        help="Root directory for all file operations")
    parser.add_argument("--context", default=8192, type=int, metavar="N",
                        help="Initial context token limit")
    parser.add_argument("--mode",    default="design", choices=["design", "coding"],
                        help="Starting mode")
    parser.add_argument("--max-tool-result", default=4000, type=int, metavar="N",
                        help="Max chars returned by a single tool call (0 = unlimited)")
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        print(f"error: --workdir is not a directory: {workdir}", file=sys.stderr)
        sys.exit(1)

    harness = Harness(host=args.host, model=args.model, workdir=workdir)
    harness.context_limit = args.context
    harness.max_tool_result = args.max_tool_result
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
