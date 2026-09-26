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
    workdir: str = ""          # project to run in, relative to the repo (default: the repo)
    # Run in a fresh temp copy of workdir: in coding mode the model edits files
    # (it fixed the fixture's planted bugs, and every later run then chased a
    # failure that no longer existed), and runs must not see each other's changes.
    isolate: bool = False


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
        prompt="Which functions call safe_path? List the calling functions.",
        ideal=frozenset({"find_references", "index_callers"}),
        must=("code_outline", "_read_text", "_edit_file"),
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


# Use cases for the code index (/index on): config/markup lookups, constants,
# exported JS, transitive callers, orientation, file cards and indexed text
# search.  Each also has an index-off route (grep_files, find_symbol,
# file_dependencies, read_file), so running the suite with and without --index
# measures what the index adds.  `ideal` lists the tools that answer in one
# call with the index on OR off.
INDEX_TASKS: list[Task] = [
    Task(
        id="sql-columns",
        prompt="What columns does the users table have, and in which file is the table defined?",
        ideal=frozenset({"index_search", "grep_files"}),
        must=("sample.sql", "id", "email"),
        note="SQL table + columns are symbols with the index: users.* in one call.",
        max_calls=2,
    ),
    Task(
        id="css-variable",
        prompt="In the web UI stylesheet, what is the light-theme value of the --warn color?",
        ideal=frozenset({"index_search", "grep_files", "grep_file"}),
        must=("#b07900",),
        note="A CSS custom property; the dark-theme value #e2b640 is the trap.",
        max_calls=2,
    ),
    Task(
        id="html-element",
        prompt="What kind of element has the id index-badge in the web UI's HTML, and what "
               "text does it show initially?",
        ideal=frozenset({"index_search", "index_text", "grep_files"}),
        must=("button", "INDEX: off"),
        max_calls=2,
    ),
    Task(
        id="docker-stage",
        prompt="Which Dockerfile in this repo defines a build stage named builder, and which "
               "base image does that stage use?",
        ideal=frozenset({"index_search", "find_symbol", "grep_files", "find_files"}),
        must=("tests/fixtures/Dockerfile", "python:3.12"),
        max_calls=2,
    ),
    Task(
        id="yaml-paraphrase",
        prompt="Which config file sets the image used by the database service, and what image is it?",
        ideal=frozenset({"index_search", "grep_files"}),
        must=("sample.yaml", "postgres:16"),
        note="The key is services.db.image; 'database' never appears in the file.",
        max_calls=2,
    ),
    Task(
        id="module-constant",
        prompt="What is the default memory limit of the code index (the value /index-max-mem "
               "starts at), and in which file and constant is that default defined?",
        ideal=frozenset({"index_search", "find_symbol", "grep_files"}),
        must=("DEFAULT_MAX_BYTES", "code_index.py", "100"),
        note="A module-level constant; net.py has a same-named 2 MB one (the trap). Reworded "
             "2026-09-23: 'memory budget' alone also matched index_map's budget=1500 parameter.",
        max_calls=2,
    ),
    Task(
        id="exported-js",
        prompt="Where is the JavaScript function that formats byte sizes for the web UI defined?",
        ideal=frozenset({"index_search", "find_symbol", "grep_files"}),
        must=("app.js", "formatSize"),
        max_calls=2,
    ),
    Task(
        id="indirect-callers",
        prompt="What calls references_in, and what calls those callers in turn? Name the functions.",
        ideal=frozenset({"index_callers", "find_references"}),
        must=("find_references", "_uses", "index_callers"),
        note="Two levels: index_callers(depth=2) is one call; find_references needs one per level.",
        max_calls=2,
    ),
    Task(
        id="central-modules",
        prompt="Which three modules under harness/ are imported by the most other files?",
        ideal=frozenset({"index_map", "file_dependencies"}),
        must=("harness.py", "tools.py", "base.py"),
        note="index_map ranks by importers; without it this is a file-by-file survey.",
        max_calls=2,
    ),
    Task(
        id="file-card",
        prompt="Which files import harness/net.py, and which of its functions do they use?",
        ideal=frozenset({"index_file", "file_dependencies"}),
        must=("commands.py", "main.py", "tools.py", "format_size"),
        max_calls=2,
    ),
    Task(
        id="docs-text",
        prompt="Which documentation file explains the /net-max-bytes command?",
        ideal=frozenset({"index_text", "grep_files"}),
        must=("README.md",),
        max_calls=1,
    ),
    Task(
        id="regex-text",
        prompt="Which files under harness/ start a thread with threading.Thread(...)? List the files.",
        ideal=frozenset({"index_text", "grep_files"}),
        must=("code_index.py", "controller.py", "harness.py", "server.py"),
        max_calls=1,
    ),
]


# One synthetic project per language (evals/lang/<lang>/project, ground truth in
# expect.py — see evals/lang_bench.py for the model-free scorecard).  Three tasks
# each: a paraphrased lookup, a two-level caller chain, and one language trap.
_LOOKUP = frozenset({"index_search", "find_symbol", "grep_files"})
_CHAIN = frozenset({"index_callers", "find_references"})
_IMPORTS = frozenset({"index_file", "file_dependencies", "grep_files"})


def _lang(lang: str, *specs) -> list[Task]:
    return [Task(id=f"{lang}-{kind}", prompt=prompt, ideal=ideal, must=must, max_calls=budget,
                 workdir=f"evals/lang/{lang}/project")
            for kind, prompt, ideal, must, budget in specs]


LANG_TASKS: list[Task] = [
    *_lang("python",
        ("lookup", "Where is the function that downloads tax rates defined?", _LOOKUP,
         ("pricing.py", "fetch_rates"), 2),
        ("chain", "What calls apply_discount, and what calls those callers in turn?", _CHAIN,
         ("total", "value", "checkout"), 2),
        ("trap", "Which code calls Cart.clear — the cart's own clear method, not other clear methods?",
         _CHAIN, ("reset", "checkout.py"), 2)),
    *_lang("c",
        ("lookup", "Where is the macro that squares a number defined, and what is it called?", _LOOKUP,
         ("SQUARE", "util.h"), 2),
        ("chain", "What calls clamp, and what calls those functions?", _CHAIN,
         ("play_round", "total_score", "best"), 2),
        ("trap", "There are two functions named helper. Which files are they in?", _LOOKUP,
         ("util.c", "game.c"), 2)),
    *_lang("cpp",
        ("lookup", "Where is Circle's area method implemented — the definition with a body?", _LOOKUP,
         ("shape.cpp",), 2),
        ("chain", "What calls clamp_to, and what calls those callers?", _CHAIN,
         ("scale", "main"), 2),
        ("trap", "In which file are the overloads of geo::scale defined (with bodies)?", _LOOKUP,
         ("shape.cpp",), 2)),
    *_lang("java",
        ("lookup", "Which method applies a discount twice, and in which file is it defined?", _LOOKUP,
         ("applyTwice", "Discount.java"), 2),
        ("chain", "What calls applyTwice, and what calls those methods?", _CHAIN,
         ("pay", "settle"), 2),
        ("trap", "Which files import com.acme.shop.Money with an explicit import statement?", _IMPORTS,
         ("Cart.java",), 2)),
    *_lang("kotlin",
        ("lookup", "Where is the extension function that turns a string into a slug defined?", _LOOKUP,
         ("Text.kt", "slug"), 2),
        ("chain", "What uses percentOf, and what calls that function in turn?", _CHAIN,
         ("total", "receipt"), 2),
        ("trap", "Which files import the Product class?", _IMPORTS,
         ("Checkout.kt", "Main.kt"), 2)),
    *_lang("rust",
        ("lookup", "Where is the constructor Item::new defined?", _LOOKUP,
         ("model.rs",), 2),
        ("chain", "What calls apply_discount, and what calls those functions?", _CHAIN,
         ("total", "receipt"), 2),
        ("trap", "Which types implement the Priced trait, and in which file?", _LOOKUP,
         ("Item", "model.rs"), 2)),
    *_lang("javascript",
        ("lookup", "Where is the function that formats a price defined?", _LOOKUP,
         ("format.js", "formatPrice"), 2),
        ("chain", "Who uses formatPrice, including CommonJS code, and what calls those functions?",
         _CHAIN, ("legacy.js", "cart.js", "lazyReport"), 2),
        ("trap", "Which files import src/format.js, directly, via require() or via a re-export?",
         _IMPORTS, ("cart.js", "index.js", "legacy.js"), 2)),
    *_lang("typescript",
        ("lookup", "Where is the currency conversion function implemented?", _LOOKUP,
         ("pricing.ts", "convert"), 2),
        ("chain", "What calls Rates.lookup, and what calls that in turn?", _CHAIN,
         ("convert", "run"), 2),
        ("trap", "Which file re-exports convert, and which file imports it from there?", _IMPORTS,
         ("index.ts", "main.ts"), 2)),
    *_lang("tsx",
        ("lookup", "Where is the custom React hook for the cart defined?", _LOOKUP,
         ("useCart.tsx", "useCart"), 2),
        ("chain", "Which component renders LineItem, and which component renders that one?", _CHAIN,
         ("CartView", "App"), 2),
        ("trap", "What does the App component render, and with which title text?", _LOOKUP,
         ("CartView", "Shop"), 2)),
]


# Shell output (/run-mode new vs classic): evals/shell/project has a 300-test
# suite with two failures, a build that prints ~57 KB with one warning in the
# middle, and a quiet linter.  Every answer needs run_command; what differs is
# how much output reaches the model and how many calls it takes to get the fact.
# Run with --suite shell --run-mode new|classic and compare.
_SHELL = "evals/shell/project"
_RUN = frozenset({"run_command"})

SHELL_TASKS: list[Task] = [
    Task(id="shell-which-fail", prompt="Run ./run_tests.sh and tell me which tests fail. Don't change any files.",
         ideal=_RUN, must=("test_restock_047", "test_price_203"), max_calls=1,
         modes=("coding",), workdir=_SHELL, isolate=True,
         note="failures and summary are at the end: the default view or tail= has them"),
    Task(id="shell-count", prompt="Run ./run_tests.sh. How many tests ran in total? Don't change any files.",
         ideal=_RUN, must=("300",), max_calls=1, modes=("coding",), workdir=_SHELL, isolate=True,
         note="the 'Ran 300 tests' line is near the end"),
    Task(id="shell-warning",
         prompt="Run ./build.sh. Did the build print any warnings? Quote them exactly. "
                "Don't change any files.",
         ideal=_RUN, must=("max_conn", "max_connections"), max_calls=2,
         modes=("coding",), workdir=_SHELL, isolate=True,
         note="the warning is in the middle of ~57 KB: grep= (or a command_output "
              "follow-up) finds it, the default head+tail view does not"),
    Task(id="shell-small", prompt="Run ./lint.sh and list every issue it reports. Don't change any files.",
         ideal=_RUN, must=("E501", "W291"), max_calls=1, modes=("coding",), workdir=_SHELL, isolate=True,
         note="short output: must come back whole, no follow-up call"),
    Task(id="shell-cause",
         prompt="Run ./run_tests.sh and find out why test_price_203 fails: which function "
                "in inventory.py is wrong, and what does it do wrong? Don't change any files.",
         ideal=_RUN, must=("value",), max_calls=3, modes=("coding",), workdir=_SHELL, isolate=True,
         note="run, then read the function; a rerun of the suite is waste"),
]
