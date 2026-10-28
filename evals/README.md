# Code-navigation evals

Measures whether the local model actually *uses* the code-navigation tools, as
opposed to whether they work. `tests/` covers the second question; this covers
the first.

Tool descriptions are load-bearing in this harness — the system prompt's tool
reference is generated from the tool schemas (one file per tool in
`harness/tools/builtin/`; `harness/tools.py` before 2026-10-21), so a wording
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
read `harness/tools/dispatcher.py` (`harness/tools.py` before the split below),
whose source contains `dispatch`'s own error strings ("does not accept
argument(s)"), so an unanchored match scores a successful file read as a
malformed call. That bug inflated the first coding-mode report by 3.

## The tool split (2026-10-21)

`harness/tools.py` became the `harness/tools/` package, one file per tool. What
the model sees did not change (`tests/test_tool_registry_golden.py` pins every
schema, the tool reference and the system prompt across 192 configurations),
but this repo, which several tasks are about, did. Three tasks' ground truth
moved with it and their numbers before that date are not comparable:
`blast-radius` (now about `harness/tools/dispatcher.py`; must name
`tools/__init__.py`, which re-exports `dispatch`), `central-modules` (every tool
file imports `tools/registry.py`, now the most imported module) and `file-card`
(`net.py` is imported by `tools/builtin/fetch_url.py` instead of `tools.py`).
Numbers in the tables above that mention `tools.py` are from before.

## Reusing runs (`--cache`)

```bash
python evals/run_evals.py --suite lang --no-think --mode chat --runs 5 --cache evals/.cache/runs.jsonl
python evals/run_evals.py --suite lang --no-think --mode chat --runs 5 --cache evals/.cache/runs.jsonl --index
```

Each run is stored under a fingerprint of everything that can change its outcome:
the task (prompt, expected facts, ideal tools), the files of the project it runs
in, model / mode / think / index, the rendered system prompt and tool schemas,
and the harness code behind the tool output (the `tools/` package, `code_nav.py`,
`harness.py`, `net.py`, plus the `code_index/` package with `--index`). A matching run is
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

### Resolution across a project (`--langs python-resolve`, 2026-10-16)

The oracles above check what each file contains. This one checks what the index
*makes of it* over a whole project: which file each import means, and which
project function each call reaches — the layer index_file's "Imported by",
index_map's ranks and module-qualified `index_callers` stand on. Ground truth is
`ast` plus Python's own import rules (`evals/oracle/py_resolve.py`): import roots
(the project root, `src/`, the folder above each top-level package, a script's
own folder), relative levels, `from pkg import mod` meaning the module, and a
call resolved through the calling scope's bindings. A call on an object the
oracle cannot type (`obj.f()`, `self.json.dumps()`) is not scored.

| | import edges | re-export → defining file | calls (200 functions) |
|---|---|---|---|
| this repo, before | 99.7% / 92.4% | 0% | 91.5% / 99.0% |
| this repo, after | 100% / 99.7% | 100% | 100% / 100% |
| CPython `Lib/` (1,867 files), before | 99.5% / 82.5% | 8.1% | 98.5% / 96.5% |
| CPython `Lib/`, after | 100% / 98.2% | 100% | 98.5% / 99.7% |

(recall / precision.)  What it found, all fixed:

- **Same-named modules.** Imports were matched on text, so `import abc` linked
  every `abc.py` in the tree. Python imports now resolve through the import
  roots, a nested project (`evals/x/project/shop`) through its own root first, and
  a standard-library name is never a project file.
- **`from .web import tls`** linked only the package's `__init__.py`, not the
  submodule `tls.py`.
- **Re-exports.** `from pkg import f`, where `pkg/__init__.py` got `f` from
  `.impl`, did not count for `impl.py`; neither did `code_index.index_search()`
  through the package.
- **Aliases.** `from .commands import handle as handle_command` — every call of
  `handle_command` was invisible to `index_callers("handle")`.
- **Imports inside a function** (`def k(): from m import f; f()`) made `f` a local.
- **A bare call in a file that merely imports the module** (`import inspect`, then
  its own `iscoroutine()`) was credited to `inspect.iscoroutine`.
- **`harness.tools.read_text` became a method query** because a test class
  `Tools` exists: a module that defines the name now wins over a same-named
  class, and a class counts only if it defines the member (any letter case:
  `harness.send` is still `Harness.send`).

What remains on `Lib/`: a few hundred edges to modules the oracle cannot see
loaded (`sys.path` set at run time, `_frozen_importlib_external` imported as
`_bootstrap_external`), and calls the oracle cannot resolve either way.
JavaScript/TypeScript (tsconfig paths, package names) and Rust (crate paths,
`mod foo;`) resolution have no independent oracle here; they are covered by
fixtures in `tests/test_code_index.py` (`ConfigResolution`).

## Command output (`/run-mode`, `run_output_bench.py`, `--suite shell`)

`/run-mode new` (the default) saves `run_command` output to
`~/.momo-harness/runs/<session>/rN.log` and returns a view: `tail=N`, `grep=`,
or by default the whole output up to `/run-output-limit` (off by default; the
bench runs used 5000 chars), else its first 25% and last 75%. Every result ends with the log's full path, which
`command_output` takes to search it again. `/run-mode classic` is the old
whole-output behaviour, kept for comparison.

**Without a model**: `python evals/run_output_bench.py` replays six generated
outputs (pytest with failures, a make/gcc build, git log, pip, find, a short run)
through the real `run_command` and checks which facts survive each view:

| view | chars | facts |
|---|---|---|
| classic | 195,510 | 10/10 |
| new, default view | 26,816 (14%) | 7/10 — misses what sits mid-output |
| new, best tail=/grep= | 2,032 (1%) | 10/10 |
| new, default + one command_output | 27,791 (14%) | 10/10 |

The new schema costs ≈ +640 tokens in every request (tool reference + JSON).

**Failure-aware default view (2026-10-16).** When the default view has to cut, the left-out
lines that mention error/fail/warning are listed with their line numbers (beginning 15%, those
lines ≤35%, end 50%). The cut note now names the missing lines (`lines 18-320`) and the
`command_output(path="rN", start_line=…, end_line=…)` call for them, and the note counts
against the limit:

| view | chars | facts |
|---|---|---|
| new, default view (before) | 26,816 | 7/10 |
| new, default view (after) | 25,265 | **8/10**: the make warning mid-build is now in the first result |
| new, default + one command_output | 25,948 | 10/10 |

The remaining misses (a commit deep in `git log`, one path among 3,200 `find` lines) are not
trouble lines; only a grep finds them. The schema is now ≈ +800 tokens over classic: +167
for `background`/`wait_for` and `stop_command`, partly paid for by a shorter
`command_output` description.

**With the model** (Ornith-1.5-9B-Q8_0, llama.cpp, `--suite shell --run-mode new --runs 3`,
HEAD vs the change, back to back on one server, 2026-10-16):

| | before | after |
|---|---|---|
| facts found | 15/15 runs | 15/15 runs |
| command output sent to the model | 3.9 KB/run (max 11.0) | **2.5 KB/run (max 5.5)** |
| `command_output` calls | 13 | **3** |
| tail=/grep= views · self-piped · re-runs | 11 · 2 · 0 | 9 · 2 · 0 |
| tool calls | 50 | 62 |
| wall time | ≈480 s | ≈500 s |

- **Fewer follow-up looks.** Before, which-fail and warning runs took the tail and then
  grepped the log 2–4 times. After, a plain `./run_tests.sh` got both failing tests listed
  by line number in the first result (`48: test_restock_047 … FAIL`, `204: test_price_203 …
  FAIL`, out of 15 KB of passing output), with 0 or 1 follow-ups.
- **The model now passes the short id** (`r1`, `r1.log`) that the hint shows. Before, it
  passed the full path, and twice a wrong, workdir-relative one.
- **The extra calls are not from the change.** All 11 extra `read_file` calls come before the
  first `run_command` (reading `run_tests.sh`, `build.sh`, `inventory.py` first), plus one
  10-call shell-cause outlier. n=3 per task on a sampled model, so the call count is noise.
- **Background commands are not covered:** no shell task starts a server.

**With the model** (Qwen3.5-9B-Q4_0, llama.cpp, 3 runs × 5 tasks per mode, 2026-09-26):

```bash
python evals/run_evals.py --suite shell --run-mode classic --runs 3
python evals/run_evals.py --suite shell --run-mode new --runs 3
```

| | classic | new |
|---|---|---|
| facts found | 15/15 runs | 15/15 runs |
| tool calls | 71 | 66 |
| command output sent to the model | 436 KB (28.4 KB/run, max 76) | 62 KB (4.1 KB/run, max 7) |
| all tool output | 460 KB | 74 KB |
| wall time | 1,060 s | 686 s (−35%) |
| self-piped commands (`\| grep`, `\| tail`) | 16 | 3 |
| same command run again | 9 | 3 |

Per task (mean seconds): which-fail 67→38, count 44→29, warning 90→43,
small 35→34, cause 118→86. Call counts did not drop: the build warning sits in
the middle of 57 KB, so new mode needs a second look (4–6 calls vs 1–4), but
each look is ~5 KB instead of ~57 KB. The model used `command_output` 13 times,
passing the full printed path in 11. It also ran shell `grep` on the log file
itself, which works because it is a plain file. It used the native
`tail=`/`grep=` args 10 times. Once it garbled the long session directory in the
path, so `command_output` now matches on the `rN.log` name (it only ever opens
its own store's file).

**Keep shell tasks `isolate=True`**: in coding mode the model edits the project.
In the first attempt it fixed the fixture's planted bugs, and every later run
chased failures that no longer existed (100 and 81 calls). Isolated tasks run in
a fresh temp copy of their workdir.

## Editing (`harness/editing.py`, `edit_bench.py`, `--suite edit`)

The edit tools add four things to a plain replace, each named in
`Harness.edit_features` so it can be switched off (`--edit-features none|all|a,b`):
**feedback** (the changed lines with their new numbers, a syntax warning, "No change"
for an edit that changes nothing), **match** (tolerant matching for a changed line count, quotes and spacing;
errors that list each occurrence or show the closest block), **symbol** (`replace_symbol`,
and a pointer to it when an edit_file copied most of a definition) and **checks** (uses
left behind by a rename or signature change, from the index or a bounded scan;
old_string found in another file; definitions dropped by `write_file`).

**Without a model**: `python evals/edit_bench.py` puts 8 real edits through 10
copying slips and checks 4 must-refuse cases (gate: `tests/test_edit_bench.py`):

| | features off (= before) | on |
|---|---|---|
| correct | 45/66 | 63/66 |
| written wrongly | 0 | 0 |
| refused (all with the block to copy) | 21 | 3 |
| must-refuse cases edited | 0 | 0 |

The first version wrote 6 edits wrongly. In those cases the model's strings had lost
their indentation (every line flat), or it indented in 2-space steps in a 4-space or
tab file. Now the indentation ratio is read from the matched lines (or each side's
indent unit), and strings with no indentation structure are refused rather than guessed.

**With the model** (Qwen3.5-9B-Q4_0, llama.cpp, `--think-history turn`, 3 runs per
cell, 2026-10-07). Both arms ran in clean worktrees of HEAD; *new* = HEAD + this change.
Six change requests on `evals/edit/project`, each checked by hidden tests
(`evals/edit/checks.py`). The index only matters for the cross-file tasks, so they
also ran with `--index`:

```bash
python evals/run_evals.py --suite edit --runs 3 --think-history turn
python evals/run_evals.py --suite edit --index --tasks rename-across signature-change --runs 3 --think-history turn
```

| task | index | before: pass, calls (min-max) | after: pass, calls (min-max) |
|---|---|---|---|
| ambiguous-edit | off | 3/3, 9.0 (8-10) | 3/3, 6.0 (5-7) |
| nested-branch | off | 3/3, 9.3 (9-10) | 2/3, 7.3 (5-9) |
| two-edits | off | 3/3, 10.7 (8-12) | 3/3, 12.0 (11-14) |
| rewrite-function | off | **0/3**, 19.3 (9-29) | **2/3**, 19.7 (9-33) |
| rename-across | off | 3/3, 25.3 (24-26) | 3/3, 29.3 (23-36) |
| signature-change | off | 3/3, 22.7 (16-32) | 3/3, 23.7 (17-34) |
| rename-across | on | 3/3, 26.7 (25-30) | 3/3, 22.7 (20-25) |
| signature-change | on | 2/3, 26.7 (17-46) | 3/3, 22.7 (18-27) |

| totals | index off: before → after | index on: before → after |
|---|---|---|
| passed | 15/18 → 16/18 | 5/6 → 6/6 |
| tool calls | 289 → 294 | 160 → 136 |
| wall time | 1,704 s → 1,574 s | 726 s → 550 s |
| tool output | 121 KB → 127 KB | 74 KB → 59 KB |
| edit errors | 2 → 0 | 0 → 0 |
| re-reads of a file after editing it | 40 → 36 | 33 → 16 |
| whole-file rewrites of an existing file | 0 → 0 | 4 → 1 |
| collateral changes / broken .py left | 0 / 0 → 0 / 0 | 0 / 0 → 0 / 0 |

What the sessions show (every message, result and reasoning was kept in the eval HOME)
— more modest than the totals suggest:
- **replace_symbol did not cause the rewrite passes.** The failing runs also replaced
  the whole function, through a 28-line edit_file old_string. Whether `'h'` raised
  ValueError was the model's logic, i.e. sampling. What replace_symbol saves is
  copying the old text (output tokens): 5 of the 6 edit_file rewrites did that copy.
- **The index-on "−15% calls" is one outlier.** A 46-call baseline run (which
  failed) is a third of that arm's total; without it both arms average ≈22.7
  calls/run. What does hold with the index on: whole-file read_file 59 → 33 and
  read_symbol 3 → 11, and the leftover-use list fired in every rename/signature run.
- **Edit errors 2 → 0** (both arms index off), and no collateral changes anywhere.
- **The index-off warning was misleading.** It listed only the edited file's own uses
  (`gross` in pricing.py), and because that list was non-empty the find_references
  pointer was dropped. In one rename run the model then updated only the imports
  and found the missed call sites through failing tests (29 calls vs 23–25).
- **The index-on list included a false positive**: `ledger.calc_tax()` — the
  `Ledger.calc_tax` method the task says to leave alone — was listed as a use of the
  removed function in all three runs. The model left it alone only because the
  prompt said so.
- **Re-reads after an edit are a final check after the tests pass** ("Let me verify
  the final file content"). The post-edit snippet does not replace that.
- **`within=` was never used, and no edit hit "found N times"**: the model always
  copied enough context. (Removed in the follow-up.)
- **21% of all run_command calls (40/191) were `python: command not found`**, about
  one per run, in both arms: this Mac has only `python3`.
- The one new failure (nested-branch) was an exact-match edit whose new_string dropped
  the `else:` branch; the result showed only the new lines, so nothing said so.

Three runs per cell moves a pass rate by a third with one run, so read the per-run
tool use above, not just the pass counts. Collateral excludes tool caches
(`.pytest_cache`, `__pycache__`). The model runs pytest and leaves them behind.

### Follow-up (2026-10-07): fixes from the sessions, measured by replay

Changes:
- The leftover-use list without the index comes from a bounded scan of the project
  (≤1 s / 5,000 files; it says when it stopped early). It is never the edited file's
  uses alone.
- A removed top-level function no longer counts `obj.name()` calls on an object; a
  call on an imported module (`pricing.calc_tax()`, aliases included) still counts.
- The environment section says `run it as python3` when there is no `python`, and a
  `python: command not found` result gets a one-line note.
- Edit results listed the old lines the edit removed — dropped again after R1 below
  showed no effect.
- run_command's default timeout is 120 s (was the 900 s maximum); a command the default
  stops is told it can pass `timeout=` up to 900.
- An edit_file that copied most of a definition points at replace_symbol.
- `within=` was removed.

**Replay A/B** (`python evals/replay.py`). A real session is cut right after one tool
result. The project is rebuilt as it was at that point, and the model continues
from the same prefix with the recorded result (A) or the current code's result (B).
Scored by the hidden check; 10 samples per variant:

| replay | A | B |
|---|---|---|
| R1: the edit result lists the removed `else:` branch | 0/10 pass, 2.5 calls | 0/10 pass, 3.1 calls |
| R2: index off, leftover uses from the whole project (not just the file) | 10/10, 15.5 calls (9-18) | 10/10, 13.5 calls (11-18) |

R1: no effect. The model runs the visible tests (which do not cover the other zones),
sees them pass and stops, whether or not the result names the deleted lines. R2:
about 2 calls fewer after the rename; the bigger gain is that the list is complete
and no longer includes the `Ledger.calc_tax` trap.

**Short rerun** (current code, 3 runs per cell; compare with the A/B above):

| | baseline | A/B new arm | follow-up |
|---|---|---|---|
| index off: passed | 15/18 | 16/18 | 17/18 |
| index off: calls | 289 | 294 | 277 |
| index off: `python ...` commands (command not found) | 18 | 16 | **2** |
| index off: run_command calls | 64 | 83 | 53 |
| index on: passed | 5/6 | 6/6 | 6/6 |
| index on: calls | 160 | 136 | 160 |
| index on: `python ...` commands | 6 | 6 | **0** |
| collateral changes / broken .py left | 0 | 0 | 0 |

The index-on calls went back up because of a bug the rerun exposed. Two
signature-change runs sent an edit_file whose new_string equalled old_string (the
model meant to add a missing `}`). The tool answered "OK — 1 change applied — lines
removed", and with the file already broken there was no syntax warning. The model
concluded its edits were not landing and fell back to write_file (6 whole-file
rewrites, 29 and 35 calls). Fixed: an identical edit answers "No change … Line N still
reads: …", and an error still sitting next to the changed lines is reported as
"still does not parse — Nothing here fixed it yet". Replay R3
(`evals/edit/replays/signature-noop-edit.json`, cut at that edit, 10 samples per variant):

| R3 | A: "OK — lines removed" | B: "No change" + "still does not parse" |
|---|---|---|
| passed | 9/10 | 9/10 |
| calls | mean 23.2, median 18.5 (13-41) | mean 22.5, median 19 (9-41) |
| fell back to write_file | 7/10 samples (20 calls) | **3/10 samples (12 calls)** |

The truthful result cuts the write_file fallback it was aimed at by more than half. Pass
rate and calls are unchanged: what remains is the model's own trouble with nested quotes
in `f"Pay {format_price(total, "EUR")}"`, which no tool message fixes.

**Runaway replies.** A replay sample stalled for 80 minutes: llama.cpp was
generating one reply at 125,641 tokens and counting. The harness sent no output limit
and waits on a non-streamed reply with no read timeout. Every request now carries a
reply cap, `/max-output-tokens` (default 25,000, `max_tokens` for llama.cpp,
`num_predict` for Ollama). A capped reply ends with `done_reason: length`, which the
loop already retries without thinking.

Wall time is not compared here. The Mac slept twice during the run, freezing the
model server mid-request (600–1,700 s "responses" with 64 output tokens). One
rewrite run also spent 15 minutes on its own infinite loop: run_command's default
timeout is its maximum, 900 s, and the model never passes `timeout=`.

### Fewer turns per edit (2026-10-07): edit_files, rename_symbol, the test command

Sessions showed one edit per model turn: 662 of 746 turns carried a single tool call. A
rename took 8 sequential edit_file calls and a signature change 5–8, and 2.8 calls per
run went to finding the tests. Added:
- `edit_files`: many edits, in any files, in one call, all or nothing
- `rename_symbol`: a definition and all its uses in one call, syntax-aware
- a `Tests:` line in the Environment section with the project's test command

**Same-code A/B** (Qwen3.5-9B, N=3, `--think-history turn`, under caffeinate):
A = `--edit-features feedback,match,symbol,checks --no-test-command`, B = `--edit-features all`.

| index off | A turns / calls | B turns / calls |
|---|---|---|
| ambiguous-edit | 6.0 / 6.0 | 3.7 / 3.7 |
| nested-branch | 7.0 / 7.7 | 6.0 / 6.0 |
| two-edits | 8.3 / 8.3 | 7.7 / 7.7 |
| rewrite-function | 11.3 / 11.3 | 16.0 / 16.3 (one 26-call run) |
| rename-across | 18.3 / 22.3 | **10.7 / 13.0** |
| signature-change | 12.0 / 18.3 | **7.0 / 10.7** |

| totals | index off: A → B | index on (rename + signature): A → B |
|---|---|---|
| passed | 16/18 → 15/18 | 6/6 → 6/6 |
| model turns | 189 → **153 (−19%)** | 85 → **62 (−27%)** |
| tool calls | 222 → **172 (−23%)** | 122 → **89 (−27%)** |
| edit calls | 54 → 28 | 43 → 18 |
| re-reads of an edited file | 25 → 7 | 15 → 10 |
| wall time | 1,350 s → 1,440 s | 597 s → 522 s |
| collateral / broken files / edit errors | 0 / 0 / 0 → 0 / 0 / 0 | 0 / 0 / 0 → 0 / 0 / 0 |

- **edit_files did the work.** It was used in 11 of the 12 B rename/signature runs.
  rename_symbol was used once: the model renames with edit_files (the leftover-use
  warning points there).
- **Every B failure is the model's own logic.** Two rewrite-function runs left `'h'`
  parseable (as in A); one signature run gave `currency` a default although the task
  says required — the same failure as a baseline run earlier.
- **The test line was adopted but did not cut discovery.** 31 of 33 B test runs used
  the suggested `python3 -m unittest discover -s tests -t .` (A ran pytest). But calls
  that look at the tests barely moved (47 → 41 index off, 6 → 0 index on): the model
  still lists `tests/` and reads the test file to see what is covered.
- Wall time with the index off went up despite fewer turns. One 26-call rewrite run
  accounts for it.

After this A/B **rename_symbol and the test-command line were removed** (user's call).
rename_symbol was used once in 12 runs, for a job edit_files already did; the test line
changed which command ran, not how many calls it took. edit_files stays.

## TDD plans (`tdd_bench.py`)

`/plan tdd` end to end against a live model: one feature request (add `remove()` and
`low_stock()` to the `Inventory` class in `evals/tdd/project/`), from investigation to the
finished plan. The approval is answered `y`, any other question "Use your recommendation.".
A hidden checker the model never sees (`evals/tdd/check.py`, 8 cases incl. the strict
threshold and "a failed remove changes nothing") grades the result. `tests/test_tdd_bench.py`
keeps the checker honest without a model: it fails every case on the untouched fixture and
passes a reference solution.

```bash
python evals/tdd_bench.py --runs 3                            # TDD plans
python evals/tdd_bench.py --runs 3 --kind both                # and normal plans, same task
python evals/tdd_bench.py --host http://127.0.0.1:9931 --json out.json --keep runs/
```

`--keep` leaves each run's project, `events.txt` and `messages.json`, which is where every
finding below came from. The columns: model calls, output/prompt tokens, stages, gate refusals
by reason (`missing tag`, `hollow`, `red suite`, `deleted check`, `finished test changed`,
`marker left`), edits after the test-writing stage, the model's own suite run right before
`complete_step`, and whether the closing "run the tests" step was dropped.

Ornith-1.5-9B-Q8_0 on `llama serve`, 2026-10-14 (3 runs per row; all 12 TDD runs and all 3
normal runs passed the hidden check 8/8):

| version | seconds | model calls | what went wrong |
|---|---|---|---|
| normal plan (baseline) | 345 (302–386) | 20 (17–24) | — |
| TDD, first version | 519 (450–569) | 24 (19–29) | **2/3 runs wrote hollow tests** (body = only `self.skipTest("tdd-step-1")`; assertions written after the code); 22 marker-removal edits in one run |
| + harness switches tests on and runs them (red first), hollow gate | 795 (592–1110) | 30 (25–38) | the planner added a closing "run the full test suite" step in **3/3** runs → a tests-stage refusal (no test for it) and a confused stage |
| + that step dropped (`_only_runs_tests`) | 464 (410–513) | 22 (19–25) | planner still wrote it 2/3 (dropped) |
| + planner rule 6 rewritten for TDD, test-change audit, refusal cap, diffs | 520 (411–627) | 21 (18–24) | none; verify step written 0/3; audit notes in 2/3 runs, both real test fixes the model named in its summary |

What the runs taught:

- **A gate that checks markers and a green suite cannot see a hollow test.** Only the red
  check (switch the step's tests on, run them before any code) and the body check catch it.
- **Role text wins over an appended override.** "Do not add a final step that only runs the
  tests" after a role rule saying "the final step runs the project's tests" lost 3/3; removing
  the rule from the TDD prompt (`prompts.planner_for_tdd`) fixed it.
- **"You don't need to run the suite before complete_step" changed nothing** — the 9B runs it
  in nearly every stage. It costs a test run, not a model round trip, so it is left alone.
- Real TDD has a real cost: the model writes wrong expectations into its tests (an off-by-one
  threshold, insertion vs sorted order) and fixes them when they go red. The audit shows each
  such change to the user; deleting a check, or changing a finished step's test, is refused.

### Slow suites (`--slow-suite 20`, 2026-10-14)

A planted test that sleeps 20 s makes every full run cost 20 s. Before = the tree without
`step_test_command` and gate reuse, after = with them; 3 runs each, all 8/8 on the hidden check.

| | harness test time per run | gates reused | step command planned |
|---|---|---|---|
| before | 141 s (141–141): 7 full runs | – | – |
| after | 94 s (60–141) | 4 of 13 | 1/3 |
| + tail pipe → `tail=N`, no-op steps dropped | 80 s (60–121) | **9 of 12** | 0/3 |

- **The template works when the planner gives one** (1/3): the red checks ran one file, and
  harness test time fell to 60 s. With no template it stays at 141 s.
- **Reuse depends on the model running the bare command.** In one run it did (`… -v` is
  accepted) and 4 of 4 passing gates were reused. In the others it piped every run into
  `tail`, whose exit code says nothing about the suite, so nothing could be reused — despite the
  step message asking for the exact command.
- **Wall time went up (807 s → 1751 s), but not from the harness.** The planner's plans were
  worse in these runs: one merged both features into one step; another invented a "keep the
  existing tests green" step and its own "Acceptance" step, spent 708 s in the first (its tests
  already passed) and hit the 45-minute limit. Output tokens tripled (12k → 38k). With 3 runs a
  side and this variance, the wall-clock numbers don't measure item 7.

**Follow-up: real exit codes and no-op steps.** `run_command` now turns a trailing `| tail -N`
(with `2>&1`, `|&`, or a `; echo …${PIPESTATUS[0]}` after it) into `tail=N`, so the exit code is
the command's own; gate reuse accepts such a run. A TDD plan drops every step that touches no
production file and only runs or keeps the tests green, or is its own acceptance step — anywhere
in the plan, not just the last one. And a step whose tests already pass is told it may say so
and finish. Three runs with `--slow-suite 20`: **597 s (582–616)** against 807 s (629–1137)
before item 7 and 1751 s (901–2700) after it; 9 of 12 gates reused; the drop caught
"Verify full suite passes" and "Acceptance: end-to-end behavior"; all 8/8. Where a gate ran
fresh, the model had run a narrower command (`-p 'test_inventory.py'`, which skips the slow
file), which is rightly not the full suite.


### Grep/head pipes, re-run hint, repeated failures (2026-10-14)

From a long 9B normal-plan session (a Wikipedia scraper, 2 h 18 min, 174 tool calls). The suite
ran 14 times; 11 of those runs ended in `| grep …` or `| head -N`, so failing suites were
reported as "exit code 0". 8 runs repeated the previous command, with nothing written in
between, just to grep another slice. `command_output` was never called. One failing `edit_file`
was sent twice, unchanged. Fixes:

- `| grep [-E|-i|-n|-e] [-A/-B/-C N] PATTERN [| head -N]` and `| head -N` now become views, the
  way `| tail -N` already did, in both run modes.
- A new-mode run that repeats the last command with nothing written since starts with a pointer
  to the earlier log. It still runs.
- An ERROR result from the exact call that just failed says so.
- A one-line `edit_file` miss shows where it first differs.

**Live check:** `--kind normal`, 2 runs per side, interleaved, clean worktrees, a 30-minute
budget.

| | time | calls | hidden | suite runs piped | unchanged re-runs in a step | repeated failures |
|---|---|---|---|---|---|---|
| before | 312 s, 390 s | 23, 23 | 8/8, 8/8 | 0, 0 | 0, 0 | 0, 0 |
| after | 377 s, 288 s | 25, 23 | 8/8, 8/8 | 1, 4 (all `tail`) | 0, 0 | 0, 0 |

- **No regression, but this task doesn't exercise the fixes.** The suite is small and stays
  green, so there was no grep pipe, no re-run within a step and no edit miss on either side.
  The four `tail` pipes in one "after" run were split as before, so their exit codes were the
  real ones.
- One run typed `| tail - 20`. That isn't a tail pipe the harness recognises, so it went to the
  shell and failed.
- Measuring these fixes needs a task where the first suite runs fail: a replay of the scraper
  session, or a bench whose plan has to debug.

## Measuring `/cavecat` (`--cavecat`)

`/cavecat on` compresses the model's plan steps and tool text, asks for no narration between
tool calls, keeps its reply short, and has the harness compact command output; `off` is the
baseline. Rounds 1–2 below ran under earlier levels (`lite`, then `text` and `full`, with a
thinking rule at `full` since removed, and before the short-reply rule). See the main
README's *Cavecat mode*. Whether it pays off depends
on how far the model lets a style rule into its reasoning, and whether that costs it passes
or extra rounds. Every run records, from the turn's `DoneEvent` and `ThinkEvent`s:

| Field | What |
|---|---|
| `eval_tokens` | output tokens over the whole turn, thinking included |
| `prompt_tokens` | the last request's prompt |
| `prompt_total` | every request's prompt summed: what the turn re-read. A run that loops costs far more here than in output tokens. It counts the whole prompt each time, so with a server's prefix cache most of it is not recomputed: compare it across levels, not with output tokens |
| `think_tokens` | the thinking: counted with the server's tokenizer (llama.cpp `/tokenize`), else estimated with `text_tokens` |
| `preamble_tokens`, `args_tokens`, `reply_tokens` | the rest of the output, the same way: text alongside tool calls, the calls themselves (name and arguments), and text without calls (the reply). What the four leave of `eval_tokens` is call framing and template tokens. `split_exact` says whether they were counted or estimated; the summary prints them as shares of the output |
| `think_sample` | the first ~400 characters of the first and last thought, to check the model follows the rule |

The summary prints mean output tokens, seconds, prompt read and thinking per run next to the
pass rate. Plan mode, where compressed plan text is re-sent on every step, is measured with
`tdd_bench.py --cavecat`.

The testbench, cheapest first:

```bash
# 0. offline, seconds: what compacted command output keeps (the `compact` column)
python evals/run_output_bench.py                 # and --limit 3000
# 1. live, ~12 min per level: --suite cavecat = the 5 shell tasks (long command output,
#    the reply a third of the output: graded on facts in it) + 3 light edits (many quick
#    calls: narration between them; a hidden check guards the passes)
for lv in off on; do
  python evals/run_evals.py --suite cavecat --runs 2 --cavecat $lv --max-run-secs 300 \
    --cache cave.jsonl --json cave-$lv.json
done
# optional, slow: plan mode, where compressed plan text is re-sent on every step
python evals/tdd_bench.py --runs 3 --cavecat on --json tdd-on.json   # and off
```

The heavy edits (`rewrite-function`, `signature-change`) take 5–10× as long and mostly
measure thinking, which cavecat leaves alone; rounds 1–2 used them because the first
thinking rule backfired there. `tests/test_run_output.py` checks the compaction itself on the
shell project's real output, with no model.

`--cavecat` is part of the `--cache` fingerprint (runs cached before it existed count as
`off`); cached rows from before a field existed show no line for it.

A run is cancelled after `--max-run-secs` (default 600, `0` = no limit) and graded as it is,
marked `(capped)` and counted in the summary. `--timeout` only catches a model that goes
silent; a 9B once spent 40+ tool calls disassembling bytecode to "prove" a stale cache, which
`--timeout` never ends.

### Round 1: the first rules (Ornith-1.5-9B Q8, edit suite, 3 runs per task)

The first version had two levels that both compressed thinking: `lite` ("plain, short
sentences; don't restate, hedge or re-check what is settled") and `full` ("drafts, not prose;
fragments, conclusions only"). The run was cut short, so `full` has no numbers for the two
heaviest tasks. Mean output tokens per run, and passes:

| Task | off | lite | full |
|---|---|---|---|
| ambiguous-edit | 575, 3/3 | 749, 3/3 | 634, 3/3 |
| nested-branch | 1,255, 3/3 | 1,276, 3/3 | 850, 3/3 |
| two-edits | 2,175, 3/3 | 1,770, 3/3 | 1,586, 3/3 |
| rewrite-function | 5,928, 2/3 | 4,185, 3/3 | 8,415, 2/2 |
| rename-across | 3,252, 3/3 | 2,538, 3/3 | — |
| signature-change | 3,745, 3/3 | 7,183, 3/3 | — |

- **No pass was lost** (off 17/18, lite 18/18, full 11/11).
- **Light edits got cheaper:** `full` used 23% fewer output tokens and 17% less time on the
  three light tasks; `lite` about 5%.
- **Hard edits backfired, after the first edit.** The first edit landed at the same call at
  every level; what followed grew. `lite` on signature-change made 7–17 edits and 4–21 shell
  runs (off: 5–8 and 1–8), one run with 12 scratch writes and 5 repeated commands; `full` on
  rewrite-function made 4–6 edits and 7–8 runs with 3 repeated commands each. Shorter
  reasoning moved the checking into tool calls, and every extra round re-reads the context,
  which these output-token numbers don't show (hence `prompt_total`).
- Over all six tasks `lite` came out flat (+5% tokens, same time).

What changed after it: `lite` became `text`, which leaves thinking alone, and `full`'s
thinking rule became "cut the narration, keep the reasoning; reason it through fully before an
edit and after a failed run; don't re-run a command unchanged".

### Round 2: `text`, and `full` with "keep the reasoning" (same model, 3 heavy tasks, 5 runs each)

Means per run: output tokens, prompt read over all rounds (`prompt_total`), estimated
thinking, tool calls, seconds, and passes.

| Task | Level | Output | Prompt read | Thinking | Calls | Secs | Passed |
|---|---|---|---|---|---|---|---|
| rewrite-function | off | 8,476 | 245,692 | 4,522 | 19.4 | 324 | 4/5 (1 capped) |
| | text | 7,594 | 254,167 | 3,239 | 21.0 | 297 | 4/5 (1 capped) |
| | full | 7,099 | 199,378 | 3,695 | 15.2 | 265 | 5/5 |
| rename-across | off | 2,441 | 75,498 | 929 | 13.4 | 100 | 5/5 |
| | text | 2,353 | 81,026 | 863 | 14.8 | 97 | 5/5 |
| | full | 2,379 | 97,832 | 937 | 13.0 | 99 | 5/5 |
| signature-change | off | 2,999 | 184,260 | 796 | 20.8 | 128 | 5/5 |
| | text | 2,672 | 154,289 | 805 | 19.2 | 111 | 4/5 |
| | full | 4,210 | 242,528 | 1,676 | 25.2 | 172 | 4/5 |
| **Sum of the three** | off | 13,916 | 505,450 | 6,246 | | 551 | 14/15 |
| | text | 12,619 (−9%) | 489,482 (−3%) | 4,907 | | 505 (−8%) | 13/15 |
| | full | 13,689 (−2%) | 539,738 (+7%) | 6,307 | | 536 (−3%) | 14/15 |

- **The round-1 backfire is gone.** Neither level loops like `lite` did on signature-change.
  `full` was the best level on rewrite-function, and worst on signature-change, where it
  dropped to 4/5 and used 40% more output tokens.
- **No clear win either.** Every difference in the totals is smaller than the spread between
  runs of one level (rewrite-function `off` alone ranged 2.5k–16k output tokens), and each
  level lost one or two passes. `text` is slightly cheaper; `full` re-read more prompt.
- **The thinking rule is barely followed.** The `think_sample`s at `full` open the same way as
  at `off` ("Let me start by exploring the project structure to understand…") and stay full
  prose. This 9B's reasoning style hardly takes a system-prompt rule, so on it `full` mostly
  changes behaviour (when to reason and re-run), not length.
- So `off` stays the default. `text` is the safe level to try, and plan mode, where its
  compressed plan text is re-sent every step, is still to be measured (`tdd_bench.py --cavecat`).
- Since the thinking rule bought nothing measurable, it was removed afterwards: `full` is
  now `text` plus the short-reply rule.

### Round 3: `off` / `on`, on the testbench (same model, `--suite cavecat --runs 2`, 2026-10-25)

Before this round the eval only estimated thinking (`len // 4`), which hid how the output
splits up. Now the `DoneEvent` counts every part with the server's tokenizer. On two heavy
`off` runs of rewrite-function, thinking was 56–82% of the output and text between calls only
3–8%. That is why the testbench uses lighter tasks, where the parts cavecat can reach are
larger. `on` is everything at once: the short reply, no narration between calls, telegraphic
tool text, and compacted command output. Means per run:

| Tasks | Level | Output | Reply | Between calls | Calls | Secs | Prompt read | Command output | Passed |
|---|---|---|---|---|---|---|---|---|---|
| shell (10 runs) | off | 601 | 196 | 44 | 3.5 | 34.1 | 39,903 | 5.7 KB | 10/10 facts |
| | on | 406 (−32%) | 129 (−34%) | 23 (−48%) | 2.8 | 25.6 (−25%) | 34,880 (−13%) | 2.5 KB (−56%) | 10/10 facts |
| light edits (6 runs) | off | 1,233 | 198 | 103 | 7.2 | 54.9 | 75,029 | 0.8 KB | 6/6 |
| | on | 990 (−20%) | 123 (−38%) | 85 (−17%) | 6.8 | 46.6 (−15%) | 73,914 (−1%) | 0.8 KB | 6/6 |
| **all 16** | off | 838 | | | | 41.9 | 53,075 | 3.9 KB (max 19.7) | |
| | on | 625 (−25%) | | | | 33.5 (−20%) | 49,518 (−7%) | 1.9 KB (max 7.4) | |

Output split over all 16 runs, exact: `off` is thinking 35%, between calls 8%, tool arguments
25%, reply 24% and framing 8%. `on` is 31% / 7% / 31% / 20% / 10%.

- **The first clear saving.** Every column moves the same way on both task groups, and no
  pass or fact was lost. Paired run for run, `on` was faster on all 10 shell runs and on 5 of the 6 edits.
  At 2 runs per task this is still a small sample, but unlike rounds 1–2 the gap is larger
  than the spread.
- **The short reply is the biggest single lever.** The reply shrank about 35% on both groups,
  and on shell tasks it is a third of the output. Text between calls halved on shell tasks
  but fell only 17% on the edits, so the rule is followed only partly.
- **Compaction pays in calls, not only in bytes.** On shell-warning, `on` found the warning
  in 2 calls both times; `off` took 3–4. The plain head-and-tail view hides the warning,
  while the compacted log keeps it in view. The largest command result fell from 19.7 KB to
  7.4 KB. On the edits, command output is too small to compact.
- **Thinking fell too** (296 → 195 tokens/run), with no rule asking for it. Shorter turns
  give less to reason about.
- So `text` and `full` became one level, `on`. The testbench above is the measurement to
  repeat on another model.

### Round 4: trimming the reasoning sent back (same model, `--suite cavecat --runs 2`, 2026-10-25)

*The fixed-size trim measured here was replaced by the adaptive one of [Round 6](#round-6-a-budget-that-grows-with-the-reasoning-and-think-keep-same-model---suite-cavecat---runs-2-2026-10-26).*

With `/think-history turn` the model re-reads its earlier reasoning on every later request.
`harness/think_trim.py` sends back only the decision instead: the plan steps and the closing
paragraph, with a note saying how much was left out.

- **Offline,** `evals/think_trim_bench.py` ran over 279 recorded thoughts. The trim would not
  send 83% of the reasoning (84% on Ornith), and it is stable on every thought.
- **A lossless trim was tried first:** only what the context already holds, plus "Let me…"
  narration. It took 1–9% off, because 68% of the reasoning is sentences carrying a path,
  name, number or quote.

Live, all at `--cavecat on`. Means per run:

| Reasoning sent back | Tasks | Secs | Thinking | Calls | Prompt read | Cached | Sent / full chars | Passed |
|---|---|---|---|---|---|---|---|---|
| none (`--think-history off`, round 3) | shell | **25.6** | 134 | 2.8 | 34,880 | 68% | 0 | 10/10 facts |
| | edits | **46.6** | 297 | 6.8 | 73,914 | 84% | 0 | 6/6 |
| full (`turn`, `--think-trim off`) | shell | 39.1 | 442 | 3.5 | 42,335 | 73% | 1,394 / 1,394 | 10/10 facts |
| | edits | 58.5 | 558 | 7.5 | 87,682 | 87% | 2,000 / 2,000 | 6/6 |
| trimmed (`turn`, `--think-trim on`) | shell | 30.6 | 291 | 2.5 | 33,743 | 68% | 506 / 928 | 10/10 facts |
| | edits | 53.2 | 498 | 7.3 | 80,538 | 86% | 1,108 / 1,780 | 6/6 |

- **Sending reasoning back made this model reason more.** Against sending none, thinking
  roughly tripled on shell tasks and doubled on edits, and runs were 25–50% slower. It copies
  the long reasoning it sees. This contradicts the Qwen3.5 code-navigation eval, where `turn`
  made a quarter fewer calls than `off`, so the effect depends on the model or the task.
- **The trim recovers part of it:** 22% faster on shell and 9% on edits than sending it all,
  with less thinking and no pass or fact lost. Live it cut 45% of the sent reasoning, not 83%:
  many thoughts within a turn are short or a single paragraph, and those are sent as they are.
- **The prompt cache held:** the cached share and prefill time were unchanged with the trim,
  because a trimmed step is byte-identical on later requests.
- **On this model, sending nothing was still fastest,** with nothing lost. The ordering is
  none < trimmed < full in both task groups, though each cell is 2 runs per task.

### Round 5: a tighter trim, and Environment after the tool reference (same model, `--suite cavecat --runs 2`, 2026-10-26)

*The fixed-size trim measured here was replaced by the adaptive one of [Round 6](#round-6-a-budget-that-grows-with-the-reasoning-and-think-keep-same-model---suite-cavecat---runs-2-2026-10-26).*

Two changes, measured together:

- **The trim got tighter.** It now cuts thoughts from 200 chars (was 400). A one-paragraph
  thought keeps its plan steps and last two sentences, where before it was sent whole. The
  closing prose is capped at 400 chars of whole sentences (was 800, cut at a word). Offline it
  would not send 85% of the 279 recorded thoughts' reasoning (was 83%), and 167 of them are now
  trimmed.
- **Environment moved behind the fixed prompt.** The working directory, date and git state now
  sit after the tool reference and internet rules, just before the navigation rules. Two
  working directories' prompts, rendered by the server, now first differ 93% of the way in
  (was 76%). Ornith is a hybrid with recurrent layers, and llama.cpp reuses its cache only
  from checkpoints at the ends of earlier prompts. A prompt that diverges mid-way reuses 0
  tokens, before and after the move, so on this model the move can't show. On models that
  reuse any shared prefix (pure attention, such as Qwen3, gpt-oss, or Gemma 4 with
  `--swa-full`), the system text up to the tool reference now stays cached across directories
  and days. That only helps a session's first request.

Live, `--cavecat on --think-history turn --think-trim on`, against round 4's trim. Means per
run:

| Trim | Tasks | Secs | Thinking | Calls | Prompt read | Cached | Sent / full chars | Passed |
|---|---|---|---|---|---|---|---|---|
| round 4 | shell | 30.6 | 291 | 2.5 | 33,743 | 68% | 506 / 928 | all facts |
| | edits | 53.2 | 498 | 7.3 | 80,538 | 86% | 1,108 / 1,780 | 6/6 |
| tighter | shell | **29.1** | **219** | 2.8 | 37,937 | 71% | 410 / 754 | all facts |
| | edits | 59.1 | 625 | 7.7 | 87,961 | 87% | 1,087 / 2,192 | 6/6 |

- **Live, it sent 51% of the reasoning, against 59%.** That is less than the offline cut,
  because most thoughts within a turn are still short.
- **Shell tasks were a little faster with less thinking; edits a little slower with more.**
  Both are within run-to-run noise. On edits, one `two-edits` run (96 s, 1,270 thinking tokens)
  and one 9-call `ambiguous-edit` run account for the gap. The same tasks spread as widely
  between runs within any one setting (`two-edits` with the full reasoning: 74 s and 114 s).
  At 2 runs per task, the tighter trim neither helped nor hurt measurably. It was kept: it
  sends less, loses nothing, and the cached share held.
- **The ordering from round 4 stands.** On this model, `--think-history off` (25.6 s / 46.6 s)
  is still the fastest.

### Round 6: a budget that grows with the reasoning, and `/think-keep` (same model, `--suite cavecat --runs 2`, 2026-10-26)

*Since then `/think-keep` is a setting of its own: `off|lean|more|most`, default `lean`, on
with or without cavecat. The evals' `--think-trim on|off` became `--think-keep LEVEL|off`; the
runs below are `--think-keep lean` and `more` at `--cavecat on`.*

The trims of rounds 4–5 kept the same few hundred chars whatever the thought's length:

- a 10k-char thought kept about as much as a 1k one;
- the cap was tuned on a 9B model, though the harness also serves larger ones.

`harness/think_trim.py` now keeps `budget = max(300, 12·√chars) × level`, at `/think-keep`
`lean` ×1 (the default), `more` ×2 or `most` ×4. The budget is filled in this order:
- plan steps first, up to half of it;
- then backwards from the end: whole paragraphs, the last one cut at sentence boundaries;
- code is kept whole or not at all, and code the call repeats becomes a pointer.

A thought is sent as it is when the trim would save under a quarter of it, so anything under
~400 chars at lean. This replaced the earlier rules (the 200-char minimum, the 400-char
decision, the two sentences of a one-paragraph thought, the 1,200-char step cap). `--think-keep`
is now an eval flag, and `harness/think_trim.py` is in the eval cache's fingerprint.

`evals/think_trim_bench.py --keep LEVEL`, over the same 279 recorded thoughts (518k chars).
Share not sent:

| Thought length | Chars | `lean` | `more` | `most` |
|---|---|---|---|---|
| < 500 | 35,959 | 7% | 0% | 0% |
| 500–1,000 | 28,005 | 56% | 0% | 0% |
| 1,000–2,000 | 26,315 | 68% | 40% | 0% |
| 2,000–4,000 | 54,505 | 78% | 57% | 0% |
| 4,000–8,000 | 100,336 | 84% | 70% | 35% |
| 8,000+ | 272,798 | 91% | 83% | 66% |
| **all** | 517,918 | **80%** | **65%** | **42%** |

- **`lean` sends a fifth of the reasoning** (round 5's fixed trim: 15%). It sends more of long
  thoughts and leaves short ones whole.
- **The trim is stable** on every thought at every level.
Live, `--cavecat on --think-history turn` (then `--think-trim on`, now `--think-keep lean|more`), against the trims of rounds 4–5.
Means per run:

| Trim | Tasks | Secs | Thinking | Calls | Prompt read | Cached | Sent / full chars | Passed |
|---|---|---|---|---|---|---|---|---|
| round 4 | shell | 30.6 | 291 | 2.5 | 33,743 | 68% | 506 / 928 | all facts |
| | edits | 53.2 | 498 | 7.3 | 80,538 | 86% | 1,108 / 1,780 | 6/6 |
| round 5 | shell | 29.1 | 219 | 2.8 | 37,937 | 71% | 410 / 754 | all facts |
| | edits | 59.1 | 625 | 7.7 | 87,961 | 87% | 1,087 / 2,192 | 6/6 |
| adaptive, `lean` | shell | 32.6 | 297 | 2.8 | 38,691 | 71% | 622 / 1,013 | all facts |
| | edits | 54.1 | 499 | 7.5 | 82,332 | 86% | 1,122 / 1,759 | 6/6 |
| adaptive, `more` | shell | 29.7 | 240 | 2.5 | 34,033 | 68% | 688 / 797 | all facts |
| | edits | 54.3 | 489 | 6.7 | 82,422 | 86% | 1,526 / 1,747 | 6/6 |

- **Nothing was lost at either level.** Every fact was found and every edit passed, with no
  capped runs.
- **Live, `lean` sent 63% of the reasoning and `more` 87%.** Offline, the shares were 20% and
  35%. Most thoughts within a turn are short, and the floor now sends those whole.
- **The four trims are within noise of each other.**
  - At `lean`, the 32.6 s on shell tasks is mostly `shell-cause`: 59 s and 61 s, against
    49 s in round 5. That task's spread across rounds is larger than any trim's effect.
  - Edits sit at 53–55 s for every trim but round 5's.
  - Sending more back (`more`) did not make Ornith think more here, unlike sending all of it
    (round 4: 39.1 s / 58.5 s).
- **The prompt cache held:** 79% cached and about 11–12 s of prefill per run at both levels.
- **Kept:** the adaptive trim, with `lean` as the default.
  - On Ornith, `--think-history off` is still fastest (25.6 s / 46.6 s); among the trims,
    neither level is measurably better.
  - `more` and `most` cost little here, so they are a safe choice for larger models that make
    use of their earlier reasoning. Measure them there.

## Round 3: verify guidance and narration (Ornith-1.5-9B Q8, `--cavecat off`, 2026-10-25)

A census of the 1,378 tool calls in rounds 1–2 found rounds the coder role itself caused: 60
`python -m py_compile` runs in 35 of 92 runs (`roles/coder.md` suggested it, though the edit
result already reports syntax errors), 61 re-reads of a file just edited (the edit result shows
the changed lines), and on average 3.5 calls after the last edit. Two prompt changes were
measured one at a time, each with the heavy tasks ×5 and the light tasks ×3.

New per-run fields: `prompt_cached` (of `prompt_total`, what llama.cpp's prompt cache held:
`usage.prompt_tokens_details.cached_tokens`, else `timings.cache_n`) and `prompt_secs` (the
server's time on the rest). In round 3, **89% of the prompt read was cached and prefill took
about 15 s of a 150 s run**: generation, not re-reading the context, is where the time goes.
So a round costs its output tokens plus a little prefill, and fewer rounds still matter most.

`max_calls` for the edit tasks is now the fewest calls of a passing `off` run (4, 7, 8, 10, 10,
14); before, "over call budget" fired on 62 of 65 heavy runs.

**Stage 1, verify guidance (kept).** Principle 6 now says: run the one test or short command
that covers the change; the edit result already reports syntax errors and shows the changed
lines, so don't run a syntax check or re-read the file; once the check passes, stop. Against the
24 `off` heavy runs of rounds 1–2: `py_compile` 0.54 → 0.13 per run (both in one broken run),
re-reads after an edit 0.71 → 0.33, calls −7%, prompt read −10%, passes 13/15 (was 22/24),
output tokens +18% (mostly one capped run). The habits it named went; the total moved within
the noise.

**Stage 2, lighter narration (reverted).** "Every response MUST contain text" and "Before each
tool call: one sentence" became "your final response must contain text" and "one sentence before
an edit or when your plan changes". Against stage 1:

| | Stage 1 | Stage 2 |
|---|---|---|
| Light tasks (3×3) | 23.3 calls, 4,313 out, 185 s, 9/9 | 20.7 calls (−11%), 3,374 out (−22%), 153 s (−17%), 9/9 |
| Heavy tasks (3×5) | 47.6 calls, 15,993 out, 611 s, 13/15 | 58.4 calls (+23%), 17,693 out (+11%), 678 s (+11%), 13/15 |
| rewrite-function | 13.2 calls, median 8,259 out | 20.6 calls, median 12,315 out, 1 capped |
| All six | 70.9 calls, 20,306 out, 22/24 | 79.1 calls (+12%), 21,066 out (+4%), 22/24 |

Sums of per-task means. Lighter narration made the easy edits cheaper and the hardest one
dearer (its median run too, not just an outlier): without a sentence before each call the 9B
acts with less of a plan when the change is large. Reverted, as overall calls and output went up.

**A recurring failure.** Both capped heavy-task failures across the two stages were the same:
the model mangled nested quotes in an f-string inside `edit_file`'s `new_string`
(`f"Pay {format_price(total, "EUR")"}"`), got the syntax warning, and then spent 25–50 calls on
`od`, `ast.parse` and scratch probes of a file it believed valid, until `--max-run-secs`. That
spiral, not verification habits, is now the largest single waste in this suite.

## Round 4: escape repair and syntax watch (Ornith-1.5-9B Q8, `--cavecat off`, 2026-10-25)

Three changes aimed at that spiral, all language-neutral and behind the existing edit features:

- **The caret mark.** A syntax warning now gives line *and* column, and shows the line with a
  caret under the spot, naming the character when it is a single one (`^ here: '\' (backslash)`).
- **Escape repair** (`match`). When the text a write tool got contains a backslash, breaks the
  file's syntax as given, and parses with one level of escaping removed (`\"` → `"`, then `\\`,
  then `\n \t \r`), it is written unescaped and the result says so. Legitimate escapes parse as
  given and are left alone. The tool descriptions now say text is written "exactly as it should
  appear in the file — quotes and backslashes are not escaped again".
- **Syntax watch** (`feedback`). While a file this turn wrote has more syntax errors than before
  the turn, every later tool result carries "NOTE: <file> still does not parse since your edit"
  with the mark; after two calls that don't edit it, also "Inspecting the file further won't
  change it". It goes quiet once the file parses again.

Offline, replaying the recorded failing calls: stage 2's three over-escaped edits are repaired
on the first write; stage 1's moved quote gets a caret at the unclosed `{`.

Live, heavy tasks ×8 and the other four ×3, against stages 1–2 (both narration variants):

| Task | Stages 1–2 | Round 4 |
|---|---|---|
| signature-change | 8/10, **2 capped**, 24.0 calls, 5,184 out, 207 s | **8/8, 0 capped**, 23.0 calls, 4,011 out, 160 s |
| rewrite-function | 8/10, 1 capped, 16.9 calls, 9,155 out, 338 s | 5/8, 0 capped, 13.8 calls, 7,635 out, 282 s |
| ambiguous-edit | 6/6, 5.2 calls | 3/3, 6.0 calls |
| nested-branch | 6/6, 6.7 calls | 3/3, 9.0 calls |
| two-edits | 6/6, 10.2 calls | 3/3, 8.7 calls |
| rename-across | 10/10, 12.1 calls | 3/3, 10.3 calls |

- **No run hit the cap.** In 5 of the 8 signature-change runs a file stayed broken across calls
  and got the "still does not parse" note; all 5 fixed it and passed.
- **The repair never fired live**: no run over-escaped this time, so only the replay exercises it.
- **rewrite-function's 3 failures are logic, not syntax**: `''` returned 0, `'1h-5m'` was
  accepted, valid `'1h30m'` rejected. None had a syntax warning or note. Earlier rounds varied
  as much on this task; 8 runs can't tell that apart from chance.
- The light tasks are within the noise of 3 runs each.

## Tools on demand (`/tools discover`, `--suite discover`)

`/tools discover all` declares only the commonly needed built-ins and lists the
rest in the system prompt until the model loads them (`find_tools`, a call by name,
or a result naming one; see the README's "Loading tools on demand"). It saves
6–13% of the prompt spent on tools (coding with the index on: ~8,100 → ~7,400
tokens, after round 2 below), more with MCP servers, and it stays **off by default** until this A/B
shows the model still reaches the tools it needs.

```bash
python evals/run_evals.py --suite discover --runs 3 --tools-discover off --json off.json
python evals/run_evals.py --suite discover --runs 3 --tools-discover all --json on.json
# and the nav and edit suites, which must not get worse with it on:
python evals/run_evals.py --suite all --runs 3 --tools-discover all --json all-on.json
```

The `discover` suite: `discover-rename` (`move_file`, core again since round 3),
`discover-background` (`stop_command`, on demand, named by the background
`run_command` result), `discover-delete` (`delete_file`, core again since
round 1, kept as a check) and `discover-control`, a plain `edit_file` change
that needs only core tools.
The checks are in `evals/edit/checks.py` (`rename_module`, `delete_module`,
`slug_clean`).

The summary adds `first request` (the estimated prompt plus tool schemas of
the first request, the cost the setting cuts) and, for runs with it on,
`find_tools calls` and the tools loaded. Per run, the JSON has
`first_request_tokens`, `find_tools_calls` and `loaded_tools`.

**Turn it on by default when**, against the reference model: the ideal tool is
used no less often than with it off (the `discover` suite and `--suite all`),
rejected and coached calls do not rise, and the first request is at least
~20% smaller.

### Round 1 (Ornith-1.5-9B Q8 on llama.cpp, 2026-10-27): stays off

Off and on side by side, the same build. discover/shell/edit 3 runs per task
(index off), nav 2 runs with `--index`; 79 runs per condition.

| suite | result off → on | ideal tool | tool calls | prompt read/run (all rounds) |
|---|---|---|---|---|
| discover | checks 9/9 → 9/9, facts 3/3 → 3/3 | 12/12 → **9/12** | 96 → 92 | 75.5k → 56.8k (−25%) |
| shell | facts 100% → 100% | 15/15 → 15/15 | 51 → **58** | 41.0k → 34.9k (−15%) |
| edit | checks 17/18 → 18/18 | — | 192 → **216** | 93.2k → 88.1k (−5%) |
| nav (index) | recall about even | 26/26 → 26/26 | 98 → 76 | 30.1k → 28.4k (−6%) |

First request: 9,421 → 7,274 tokens (−23%) in coding with the index off;
7,335 → 6,645 (−9%) on nav (mostly chat mode, index on). Over all 79 runs
the prompt read fell 11% and the wall time was the same (6,499 s vs 6,497 s);
the server's prompt cache hit 85% → 77% on discover, since each load
re-sends the tool list.

What worked: `move_file` was loaded with `find_tools` in all three renames,
and `stop_command` loaded itself from the background `run_command` result in
all three server runs. What did not, all three tools the model has a habit
for:

- **`delete_file`**: 0/3 loaded; every run deleted with `rm` through
  `run_command` instead (3/3 used `delete_file` with it off). That bypasses
  `/undo` and the diff.
- **`command_output`**: used 5 times off, 0 on; three commands were re-run
  instead (0 off). A foreground `run_command` result does not name it, yet
  `run_command`'s own description points at it.
- **`edit_files`**: off, every `rename-across` run did the rename in one
  `edit_files` call (9–12 calls); on, it was loaded from a leftover-use hint
  only after the model had started editing file by file, and two of three
  runs made 8 `edit_file` calls (20–22 calls).

So the first criterion fails and the default stays off. Next: make
`delete_file`, `command_output` and `edit_files` core (about 750 tokens back
into the prompt) and rerun discover-delete, shell and edit.

(The nav run first crashed copying the repo for a coding-mode task:
`.scratch/` holds a browser profile with sockets; the copy now skips it.)

### Round 2 (same model and setup, 2026-10-27): closer, still off

`delete_file`, `command_output` and `edit_files` declared again (coding with
the index on: ~8,100 → ~7,000 tokens on demand, −13%). discover, shell and
edit rerun, 3 runs per task, off and on side by side again.

| suite | result off → on | ideal tool | tool calls | prompt read/run |
|---|---|---|---|---|
| discover | checks 9/9 → 9/9 | 12/12 → 11/12 | 98 → 111 | 73.5k → 66.2k (−10%) |
| shell | facts 24/24 → 24/24 | 15/15 → 15/15 | 46 → 46 | 36.2k → 32.5k (−10%) |
| edit, without rewrite-function | checks 14/15 → 15/15 | — | 161 → 159 | 109k → 81k (an off outlier) |
| rewrite-function | checks 3/3 → 2/3 | — | 39 → **86** | 3 of 3 runs on hit the 600 s cap |

First request 9,421 → 8,025 tokens (−15%, coding, index off).

Fixed: every delete used `delete_file`; no reruns, `command_output` used
(2 off, 4 on); `rename-across` 12/10/12 calls on against 13/10/12 off.
`stop_command` loaded itself from the result in 3/3, two of them done in
2 calls.

Still wrong:

- **`scratch_write`**: in all three capped `rewrite-function` runs the model
  wrote throwaway check scripts with `write_file`, first to `/tmp` (refused,
  outside the workdir), then into the project (`shop/_verify_duration.py`,
  `private_check.py`), later `.momo/scratch/`, and spent its time on that. The
  Environment section's scratchpad rule is only there while `scratch_write`
  is declared (`prompts._environment`), so on demand the model lost it. No
  run off, and none in round 1, wrote such scripts, so it is partly chance,
  but the missing rule is what turns it into a loop.
- **`move_file`**: one rename of three used `mv` through `run_command`
  (5/6 loaded it over both rounds); like `rm`, that bypasses `/undo`.

Next: declare `scratch_write` (or keep its rule while it is on demand) and
consider `move_file`, then rerun discover and edit.

### Round 3 (same model and setup, 2026-10-27): no regressions left

`move_file` and `scratch_write` declared again too. On demand in coding with
the index on: `file_info`, `grep_extract`, `index_status`, `append_to_file`,
`replace_symbol`, `stop_command` (~8,100 → ~7,400 tokens, −8%; −13% with the
index off). discover and edit rerun, 3 runs per task.

| suite | result off → on | ideal tool | tool calls | prompt read/run | s/run |
|---|---|---|---|---|---|
| discover | checks 9/9 → 9/9 | 12/12 → 12/12 | 107 → 100 | 76.8k → 68.4k (−11%) | 80 → 79 |
| edit | checks 17/18 → 18/18 | — | 209 → 192 | 112k → 89k (−21%) | 138 → 120 |

First request 9,421 → 8,507 tokens (−10%). No run was capped; rejected calls
0 in both. `rewrite-function`, capped three times on in round 2, took 6–10
calls on against 15–18 off. Coached calls on edit rose 3 → 12, but 8 are one
`signature-change` run (32 calls) stuck on quote escaping in `edit_file`,
with nothing loaded and no on-demand tool involved; round 2 had the same kind
of run off (28 calls).

Decision: the model now reaches what it needs with discovery on, but what
is left on demand saves 8–13% of the first request, short of the ~20% this
section set as the bar. So the built-ins stay declared: the setting became
three modes, and `extensions` lists only custom,
workspace and MCP tools, whose cost grows with every server and tool. The
default is `off`, every tool declared, until loading extension tools is
measured. The
rounds above are `all` against `off` (they ran as `--tools-discover on`,
which meant `all`); none of these tasks has an extension tool, so
`extensions` and `off` give them the same prompt. Not measured yet: how
reliably the model loads an MCP tool it needs.

## Tool definitions in the prompt (`tool_cost.py`, `/tool-ref minimal`, shorter descriptions, 2026-10-27)

`python evals/tool_cost.py --host <llama.cpp>` measures real tokens through the
server's chat template. Before: coding with the index on, 11,139 tokens in the
first request, 82% of it tools. Three changes: `/tool-ref minimal` (no
per-tool lines, −2,000), redundant parameter descriptions and empty
`required` dropped (−90), and the ten longest descriptions shortened (−780).

Ornith-1.5-9B Q8, four arms side by side (old/new descriptions × compact/minimal):

| suite | old+compact (today) | old+minimal | new+compact | new+minimal |
|---|---|---|---|---|
| discover: checks, ideal, calls, prompt/run | 9/9, 12/12, 115, 82.4k | 9/9, 11/12, 105, 64.9k | 9/9, 12/12, 87, 64.3k | 9/9, 12/12, 99, 56.3k |
| shell: facts, ideal, calls, prompt/run | 24/24, 15/15, 60, 46.1k | 24/24, 15/15, 51, 37.7k | 24/24, 15/15, 45, 34.5k | 24/24, 15/15, 54, 37.2k |
| nav --index: recall, ideal, calls, prompt/run | 84%, 26/26, 70, 26.6k | 84%, 26/26, 86, 31.6k | 84%, 26/26, 70, 26.1k | 89%, 26/26, 54, 19.1k |

| edit: checks, calls, coached, prompt/run | 17/18, 209, 2, 119k | 17/18, 209, 5, 96k | 17/18, 186, 5, 90k | 17/18, 194, 4, 86k |

Rejected calls 0 in every arm; text-format calls 1 in two discover arms (recovered
also with `minimal`). Every arm's one edit failure is a `rewrite-function` run,
the task that also fails under today's setup. Coached calls over all four
suites: 9 for old+compact, 7 for new+minimal.

Decision: new descriptions + `minimal` meet the bar (no suite worse, nav recall
84% → 89%, fewer calls on shell, nav and edit) and are the default. First
request, real tokens, before → after:

| role | index off | index on | index + net |
|---|---|---|---|
| design | 8,764 → 7,141 (−19%) | 10,280 → 8,194 (−20%) | 12,330 → 9,893 (−20%) |
| chat | 5,453 → 4,208 (−23%) | 6,971 → 5,263 (−25%) | 9,021 → 6,962 (−23%) |
| plan | 7,308 → 5,593 (−23%) | 8,826 → 6,648 (−25%) | 10,896 → 8,367 (−23%) |
| coding | 9,608 → 7,257 (−24%) | 11,139 → 8,325 (−25%) | 13,189 → 10,024 (−24%) |

Not done (smaller or riskier, see the plan): trimming the "Navigating code"
rules, hiding grep/find while the index covers them, merging overlapping tools.
