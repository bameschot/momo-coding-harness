# Code-navigation evals

Measures whether the local model actually *uses* the code-navigation tools, as
opposed to whether they work. `tests/` covers the second question; this covers
the first.

Tool descriptions are load-bearing in this harness — the system prompt's tool
reference is generated from the schemas in `harness/tools.py`, so a wording
change alters behaviour. This makes that measurable.

```bash
source .venv/bin/activate
python evals/run_evals.py --runs 3                       # the whole set
python evals/run_evals.py --tasks line-to-definition      # one task
python evals/run_evals.py --mode coding --runs 3          # force a mode
python evals/run_evals.py --runs 3 --json baseline.json   # keep per-run records
```

It needs a model server running (`--host`, default `http://localhost:8080` for
llama.cpp; `--provider ollama --host http://localhost:11434` for Ollama). It is
**not** a unit test: it takes minutes and its output is statistical. It lives
outside `tests/` so `python -m unittest discover tests` never collects it.

`HOME` is redirected to a temp directory automatically, because each run writes
`~/.momo-harness/prefs.json` and a session file. Set `MOMO_EVAL_KEEP_HOME=1` to
keep your real one.

## Reading the output

Report the **spread, not just the mean**. The harness sends no sampling
parameters at all (`harness/llm/llamacpp_client.py`), so the server's defaults
apply — typically temperature 0.8 with a random seed. Runs are not reproducible,
and the same task in the same mode has ranged from 1 to 25 tool calls. Use
`--runs 3` at minimum and look at the `(min-max)` column.

Columns worth watching:

- **ideal** — did the run use the tool that answers the task in one call.
- **calls (min-max)** — the efficiency signal. `max_calls` per task in `tasks.py`
  is the budget; `over call budget` counts breaches.
- **toolKB** — tool output fed back into context. The cheapest way to make this
  explode is a whole-file `read_file`.
- **shell** — `run_command` calls. On a navigation task these are a red flag:
  the model reimplementing `find_symbol` with `grep`/`sed`.
- **malformed calls** — tool results starting with `ERROR`. This should stay at 0.

## Baseline

Measured against `Qwen3.5-9B-Q4_0` on llama.cpp, 54 runs, before the
`read_file` structural hints and the `find_symbol` line form were added:

| metric | before |
|---|---|
| ideal tool chosen | 24/27 |
| malformed / rejected calls | 0 |
| `read_file` share of all calls | 51% (139/270) |
| `run_command` share | 23% (61) — more than all five code-nav tools combined (49) |
| line→definition | ideal tool 7/15, mean 7.0 calls, worst 30, ~70s |
| `_safe_path` callers, chat vs coding | 1.0 vs 11.7 calls (worst 25 calls / 563s / ~35k tokens) |
| blast radius | read all of `tools.py` (~12k tokens) before `find_references`, 3/3 |

The two failure modes that motivated the change:

1. **Undiscoverable affordance.** `read_symbol(path, "1300")` answered
   line→definition in one call, but the model used it 7/15 unprompted — and 3/3
   when told to. The capability was fine; the line form was a trailing clause in
   a parameter description, invisible where the decision gets made.
2. **A correct result, distrusted.** In coding mode the model called
   `find_references(role="call")` first, got the complete answer, discarded it,
   and re-derived it over 24 more calls — full file reads plus fifteen
   `grep`/`sed`/`xargs` pipelines. Only happens where `run_command` exists.

So the fixes were in-situ rather than in the prompt: `read_file`'s footer now
names the enclosing definition on a ranged read and points at `code_outline` on a
whole-file read, which is the moment the model reaches past the structural tools.
`find_symbol` also took a line form, so "which definition is this line in" costs
one line of output instead of a 400-line function body.

## After

Same config as the baseline (`--no-think --mode chat --runs 3`, 27 runs):

| metric | before | after |
|---|---|---|
| ideal tool chosen | 24/27 | **27/27** |
| fact recall | 76/78 | **every task 100%** |
| `read_file` share | 51% | **20%** |
| `run_command` share | 23% | **0%** |
| code-nav share | 18% | **63%** |
| tool calls per run | 4.4 | **1.5** |
| line→definition | 7/15 ideal, 7.0 calls | **3/3 ideal, 1.0 calls, 3.8s** |
| find-definition | 3/3 ideal, 2.0 calls | **3/3 ideal, 1.0 calls** |

Coding mode (`--mode coding`, the three tasks where it was worst) closed the gap
with chat mode without any change to the tool set:

| task | coding before | coding after | chat after |
|---|---|---|---|
| callers | 11.7 calls, 228s | **1.3 calls, 25.5s** | 1.0 calls |
| line→definition | 13.0 calls | **1.7 calls, 8.6s** | 2.3 calls |

That is why the planned tool-set pruning was not carried out — the coding-vs-chat
gap it was meant to fix no longer exists.

Shell use dropped sharply but is **not** eliminated. The first 9-run coding sample
showed zero `run_command` calls; a later 2-run sample of `blast-radius` alone
showed three. Treat "no more shell" as unproven — which is the same warning this
file gives about means, applied to a claim made from a lucky sample.

## What is still imperfect

- **The blast-radius task in coding mode is still bad.** It is the one task the
  change did not fix: up to 7 calls, and a worst observed run of **665s with
  115 KB of tool output**, still mixing whole-file reads with shell pipelines.
- **Whole-file reads before the tool that answers the question.** 2 of 3 chat
  runs still `read_file` all of `harness/tools.py` (~12k tokens) before calling
  `find_references`. The footer hint arrives *after* the content, so it can only
  improve the next call, not the one that already paid. Fixing this would mean
  `read_file` refusing or replacing a large whole-file read, which would break
  legitimate use — worth deciding deliberately rather than by accident.
- **Only measured with `--no-think`.** The before and after numbers above share
  that setting, so the comparison is sound, but the harness's own default is
  `think=True` and the post-change behaviour has not been measured there. Thinking
  made runs 3-10x slower and, before the change, did not rescue the line task.

Two problems visible in the first post-change run were fixed and re-verified at
1.0 calls each: a redundant `read_symbol` after an already-complete `find_symbol`,
and `find_symbol` being given a line number plus a *directory* instead of the
file. The second was a schema-wording fix — the `directory` parameter now says
outright that a line query needs the one file, not the directory containing it.

## A trap when grading

Anchor error detection on the `ERROR` prefix, never a bare substring. These tasks
read `harness/tools.py`, whose source contains `dispatch`'s own error strings
("does not accept argument(s)"), so an unanchored match scores a successful file
read as a malformed call. That bug inflated the first coding-mode report by 3.

## Reusing runs (`--cache`)

```bash
python evals/run_evals.py --suite lang --no-think --mode chat --runs 5 --cache evals/.cache/runs.jsonl
python evals/run_evals.py --suite lang --no-think --mode chat --runs 5 --cache evals/.cache/runs.jsonl --index
```

Each run is stored under a fingerprint of everything that can change its outcome:
the task (prompt, expected facts, ideal tools), the files of the project it runs
in, model / mode / think / index, the rendered system prompt and tool schemas,
and the harness code behind the tool output (`tools.py`, `code_nav.py`,
`harness.py`, `net.py`, plus `code_index.py` with `--index`). A matching run is
reused; any change to those inputs makes the next invocation run it fresh.
Asking for more runs than are stored tops up (`--runs 5` after 3 cached runs
runs 2). `--refresh` ignores stored runs. Runs that ended in a transport error
are not stored. `evals/.cache/` is git-ignored.

Caching freezes noise as well as signal: with sampling unpinned, a 3-run
baseline keeps its outliers forever. Store at least 5 runs per task for a
baseline you intend to reuse.

For the model-free per-language scorecard (definitions, search, callers, imports
per language, no model), see `evals/lang_bench.py` — it runs in about a second.

## Checking the index against independent oracles (`oracle_bench.py`)

`lang_bench.py` scores fixtures against ground truth written by the same hand
that wrote the extractor. `oracle_bench.py` compares the index with sources it
has no say in, on real code:

- **Python:** the interpreter's `ast` (definitions, call sites, imports) and
  `symtable` (which names are local to each function, closures included).
- **C, C++, Java, JavaScript, TypeScript:** each grammar's own `tags.scm`
  (definitions; call references where the query has them — C/C++ have none).

```bash
python evals/oracle_bench.py                        # default corpora, 200 files per language
python evals/oracle_bench.py --langs python --limit 500 --show 10
```

Results on 2026-09-23 (Python: stdlib + this repo + badgeware; C/C++: badgeware;
Java: a 78-file Quarkus service; JS: badgeware + this web UI):

| | definitions | call sites | other |
|---|---|---|---|
| python (496 files) | 100% / 100% | 100% / 100% (2 of 51,760 missed) | imports 100/100, locals 100% recall / 99.7% precision |
| c (110) | 100% / 99.7% | no oracle | |
| cpp (82) | 100% / 91.7% | no oracle | extras: operators, destructors, fn-pointer typedefs (tags has no pattern) |
| java (78) | 100% / 98.1% | 100% / 100% | extras: constructors |
| javascript (49) | 83.5% / 98.6% | 100% / 100% | misses: `$("#x").onclick = () => ...` handlers — skipped by design |

C/C++ definitions are compared "with a body": tags.scm also tags prototypes and
forward declarations, which the index deliberately does not.

Two more oracles, added for the Maestro repo (~/projects/Maestro, Kotlin-heavy):

- **Kotlin:** the Kotlin 2.2 compiler's own parser (PSI), driven by
  `evals/oracle/KtOracle.java`. Needs a JDK; `--fetch-kotlin` downloads the
  compiler jars (~61 MB, Maven Central) into the git-ignored `evals/.cache/ktoracle`.
- **YAML:** Ruby's Psych (libyaml), `evals/oracle/yaml_keys.rb` — uses the
  system `ruby`, nothing to install.

| Maestro | definitions | call sites | other |
|---|---|---|---|
| kotlin (514 files) | 100% / 99.3% | 100% / 100% (28,416) | imports 100/100 (3,670); member properties not indexed by design |
| yaml (373) | key paths 100% / 100% (2,972) | | |
| javascript (19), tsx (2), ts (1), html inline scripts (2) | 100% recall | 100% / 100% | tsx extras: type aliases/interfaces |

Known Kotlin limits: 6 of 520 files (1.2%) hit tree-sitter-kotlin parse errors,
where the index recovers only ~55% of definitions and ~59% of calls (it flags
such files as incomplete); annotation arguments like `use = X.Id.NAME` before a
declaration can make the grammar misread it (1 of 4,848 definitions). In `.kts`
scripts the compiler treats top-level `val`s as script-local; the index lists
them as definitions. No real Rust code was available locally; Rust relies on
the fixtures.

### C against clang (mgba)

tags.scm has no call patterns for C, and the tags comparison only uses files
tree-sitter parses cleanly — which leaves out exactly the macro-heavy C where
the index is weakest. `--langs c-clang` uses **clang's own AST**
(`evals/oracle/clang_c.py`, `-ast-dump=json`; needs `clang` on PATH, nothing to
install) on every C file clang compiles without the project's build system
(include/, src/ and each bundled `third-party/*` library on the include path;
`*.h.prebuilt` config headers stand in for configure). Default corpus:
`~/projects/mgba` (Game Boy emulator, ~420 C files).

Comparing preprocessed C with source text needs these rules, all counted
separately in the report ("not comparable by design"), never silently:

- lines in `#if` branches the preprocessor does not take are dropped on both
  sides (found with marker lines that clang's own preprocessor keeps or removes);
- macros are not in the AST: index "calls" of function-like macros, and names in
  a macro argument the expansion drops (`PRF(printf(...))`), are excluded;
- calls and definitions that only exist after expansion — written inside a
  `#define` body, or as a macro argument that the macro turns into a call or a
  type (`DECL_BITFIELD(Flags, uint8_t)`) — are out of reach of any text parser.

| mgba, 265 C files | definitions | call sites |
|---|---|---|
| 128 files tree-sitter parses cleanly | 100% / 100% (1,179) | 100% / 100% (4,459) |
| 137 files with parse errors | 98.1% / 90.3% (2,446) | 99.1% / 99.4% (8,574) |
| cpp, 89 files (tags.scm; the Qt C++ needs Qt headers for clang) | 100% / 94.2% | no oracle |

Index bugs this found and fixed: in a region tree-sitter cannot parse (code
passed as a macro argument, a function split by `#if/#else`), keywords came out
as names — `if (x)` as a call of `if`, `else if (...) {...}` as a *function
named `if`* — and call statements came out as declarations inside a function
body, which the index read as prototypes and dropped. A first version of that
recovery also turned a local function-pointer variable into a call; the oracle
caught it.

Known C limits: in files with parse errors, locals of a mis-split function
land at file level and are listed as variables (most of the 9.7% extra
definitions); K&R-style definitions (`int f(a, b) int a; ...`, all of zlib) are
not parsed by tree-sitter-c at all.
