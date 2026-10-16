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

## Command output (`/run-mode`, `run_output_bench.py`, `--suite shell`)

`/run-mode new` (the default) saves `run_command` output to
`~/.momo-harness/runs/<session>/rN.log` and returns a view: `tail=N`, `grep=`,
or by default the whole output up to `/run-output-limit` (5000 chars), else its
first 25% and last 75%. Every result ends with the log's full path, which
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
