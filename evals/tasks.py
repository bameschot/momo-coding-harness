"""Task set for the code-navigation evals.

Each task is a question with a known answer in THIS repository, so the ground
truth is checked-in data rather than something regenerated per run.  If the repo
changes shape, fix the expectations here — a failing task is then a real signal.

`ideal` is the tool that should answer the task in one call.  `must` are
substrings that have to appear in the model's final reply; keep them to facts
the answer cannot avoid stating (a file name, a symbol name), not to phrasing,
and never to a heading level or other formatting detail.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Task:
    id: str
    prompt: str
    ideal: frozenset[str]      # tools that answer it in one call
    must: tuple[str, ...]      # substrings required in the final answer
    note: str = ""
    max_calls: int = 3         # over this and the run is inefficient, not wrong
    modes: tuple[str, ...] = ("chat",)


TASKS: list[Task] = [
    Task(
        id="find-definition",
        prompt="Which file and line defines the EventBus class?",
        ideal=frozenset({"find_symbol", "index_search"}),
        must=("events.py", "76"),
        note="find_symbol returns the line range directly; a follow-up read is waste.",
        max_calls=1,
    ),
    Task(
        id="enumerate-classes",
        prompt="List every class defined in harness/events.py.",
        ideal=frozenset({"code_outline", "find_symbol", "index_file", "index_search"}),
        must=("UserEvent", "ResetEvent", "DeltaEvent", "StreamEndEvent",
              "BusyEvent", "CompanionEvent", "Subscription", "EventBus"),
        note="One code_outline call, or find_symbol('*', kind='class').",
        max_calls=1,
    ),
    Task(
        id="callers",
        prompt="Which functions call _safe_path? List the calling functions.",
        ideal=frozenset({"find_references", "index_callers"}),
        must=("code_outline", "_read_file", "_edit_file"),
        note="find_references(role='call') is complete in one call. The known "
             "failure is re-deriving it with shell after already getting it.",
        max_calls=1,
        modes=("chat", "coding"),
    ),
    Task(
        id="importers",
        prompt="Which files import harness/events.py?",
        ideal=frozenset({"file_dependencies", "index_file"}),
        must=("controller.py", "harness.py", "tui.py", "server.py"),
        max_calls=1,
    ),
    Task(
        id="line-to-definition",
        prompt="What function or method contains line 1600 of harness/harness.py?",
        ideal=frozenset({"find_symbol", "read_symbol", "index_search"}),
        must=("_run_loop",),
        note="The headline regression case. Before the read_file footer hints the "
             "model binary-searched with read_file (7 calls avg, worst 30) or "
             "reimplemented the lookup with sed/grep.",
        max_calls=1,
        modes=("chat", "coding"),
    ),
    Task(
        id="blast-radius",
        prompt="I want to rename the function `dispatch` in harness/tools.py. "
               "Which files and lines must I update?",
        ideal=frozenset({"find_references", "index_callers"}),
        must=("commands.py", "harness.py", "tools.py"),
        note="Watch for a whole-file read of tools.py (~12k tokens) before the "
             "find_references call that actually answers it.",
        max_calls=2,
    ),
    Task(
        id="repo-overview",
        prompt="Give me a short overview of which modules exist under harness/ "
               "and what each one is responsible for.",
        ideal=frozenset({"code_outline", "index_map"}),
        must=("tui", "tools", "session"),
        note="code_outline on the directory answers this in one call.",
        max_calls=2,
    ),
    Task(
        id="string-literal",
        prompt="Where is the literal string 'CTX' used in the source? Give file and line.",
        ideal=frozenset({"grep_files", "grep_file", "index_text"}),
        must=("tui.py",),
        note="Control: code nav cannot match string contents, so grep is correct "
             "here. Catches over-applying the syntax tools.",
        max_calls=1,
    ),
    Task(
        id="non-source-file",
        prompt="What are the main section headings in design.md?",
        ideal=frozenset({"read_file", "grep_file", "grep_files", "index_text"}),
        must=("Overview", "Key Files"),
        note="Control: .md is not a code_nav language, so read_file is correct.",
        max_calls=2,
    ),
    Task(
        id="config-lookup",
        prompt="Which config file in this repo sets LOG_LEVEL, and to what value?",
        ideal=frozenset({"index_search", "index_text", "grep_files"}),
        must=("sample.yaml", "debug"),
        note="A key in a YAML file: index_search finds it as a key path, grep_files "
             "as text. Nothing structural answers it without the index.",
        max_calls=2,
    ),
    Task(
        id="paraphrased-name",
        prompt="Where is the function that turns a size string like '2mb' into a number "
               "of bytes defined?",
        ideal=frozenset({"index_search", "find_symbol", "grep_files"}),
        must=("net.py", "parse_size"),
        note="The model has to guess the name. index_search takes the words in any "
             "order; find_symbol needs the exact name or a glob.",
        max_calls=2,
    ),
]
