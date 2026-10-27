# momo-coding-harness vs pi: review

> Reviewed 2026-10-27 against momo `7529146` and pi `db14038bd`. Line references may drift as either codebase changes.

Paths are relative to each repo: momo is `~/projects/momo-coding-harness`, and pi is `~/projects/pi` (`pc/` = `packages/coding-agent/`).

## Summary

The two harnesses are built on opposite philosophies. **momo is batteries-included and tuned for small local models.** It has 37 built-in tools, role-gated modes, a tree-sitter code index, heavy safety rails around editing and shell, recovery for text-format tool calls, and evals that measure whether the model actually picks the right tool. **pi is a minimal core plus a platform.** It starts with 4 tools by default (8 built in). Everything else comes through a large TypeScript extension API, Agent-Skills-spec skills, npm/git packages, around 30 lifecycle hooks, an SDK, RPC/print/JSON modes, and a tree-structured session format. It also adds two clever context-saving mechanisms, `codemode` and `tool_search` with deferred exposure.

momo wins on **depth of the coding loop**: edits, shell runs, code navigation, undo and local-model robustness. pi wins on **architecture and reach**: extensibility, embedding, sessions, providers, parallel tool execution and prompt economy. The tools pi has that momo lacks are few (`codemode`, `tool_search`, plus things that ship only as extension examples). The real gap is the platform around the tools.

## 1. Tool matrix

**Default tool-schema cost.** momo spends **~3.7k tokens** of tool schemas in design mode and **~5.3k** in coding mode, rising to ~6.3k with the index on (measured from `harness/tools/registry.py` with the 4-chars-per-token estimate). pi's 4 default tools come to roughly **~1k tokens**, and all 8 to about 1.5k. That pi figure is estimated from the description strings in `pc/src/core/tools/*.ts`; pi is not built here. On an 8–32k-context local model this is the largest structural difference between the two.

| Capability | momo | pi core | pi extension / example |
|---|---|---|---|
| Read file | `read_file` (with a code-aware footer that points at `read_symbol`/`code_outline`) | `read` (text and **images**; 2000 lines / 50 KB) | |
| Write / append | `write_file`, `append_to_file` | `write` | |
| Edit | `edit_file`, `edit_files` (multi-file), `replace_symbol` | `edit` (several disjoint edits in one file per call) | |
| Delete / move | `delete_file`, `move_file` | none (via `bash`) | |
| List / find | `list_directory`, `find_files`, `file_info` | `ls`, `find` (fd, .gitignore-aware) | |
| Grep | `grep_file`, `grep_files`, `grep_extract` | `grep` (ripgrep) | `rg` (truncated-tool example) |
| Shell | `run_command` (background, `wait_for`, grep/tail views, saved logs), `command_output`, `stop_command` | `bash`, `powershell` (tail truncation, full output saved to a temp file) | `ssh`, `bash-spawn-hook`, `sandbox`, `gondolin` |
| Code navigation | `code_outline`, `find_symbol`, `read_symbol`, `find_references`, `file_dependencies` | **none** | |
| Code index | `index_map/file/search/text/callers/changes/status` | **none** | |
| Plan / workflow | `create_plan`, `complete_step`, `revise_plan` (plan-exec role), TDD flow | **none (by design)** | `plan-mode/`, `todo`, `handoff` |
| Ask the user | `ask_user` | none | `question`, `questionnaire` |
| Scratchpad | `scratch_write` | none | |
| Web | `fetch_url`, `web_search`, `add_search_source` (off by default) | none | none shipped |
| Sub-agents | none | **none (by design)** | `subagent/` |
| **Script-composed tool calls** | **none** | | **`codemode`** (built-in extension: JS in a QuickJS sandbox calls other tools; only the output reaches context) |
| **On-demand tool loading** | none (role gating instead) | | **`tool_search`** (built-in extension: BM25 over deferred tools, which are loaded into the next call) |
| MCP | client with approval, tools exposed directly | | built-in `mcp` extension, **default exposure `codemode`/deferred** (`pc/src/core/mcp-servers.ts:241`) |
| Structured output | none | | `structured_output` example |
| Non-LLM models | none | | `models.classify()` / `generateImages()` inside codemode |

**Tools pi exposes that momo does not:** `codemode`, `tool_search`, images in `read`, and `powershell`. Extension examples add `subagent`, `structured_output`, `todo`, `questionnaire` and `ssh`. Going the other way, momo has about 25 tools pi lacks, mainly code navigation and the index, plan/TDD, delete/move, `ask_user`, scratch, web, and background shell.

## 2. Scorecard by dimension

| # | Dimension | Verdict | Why (evidence) |
|---|---|---|---|
| 1 | Tool design | **Split** | momo: a rich `Tool` dataclass with role, index, net and run-mode gating, guards, diff and TDD flags (`harness/tools/registry.py:66-110`). pi: tools carry `promptSnippet`/`promptGuidelines`, `executionMode`, `constrainedSampling: json_schema`, output schemas for programmatic callers, and pluggable `operations` for remote execution (`pc/src/core/tools/edit.ts:149-160`, `bash.ts:56-62`). |
| 2 | Editing | **momo stronger** | momo: line-number-prefix stripping, indent-shift tolerance, quote/whitespace matching, a closest-block hint on a miss, a post-edit syntax check, escape repair, and "uses left to update" after a signature change (`harness/editing.py:1-24`, `:144-400`, `:919`), all measured by `evals/edit_bench.py`. pi: exact match, then NFKC/whitespace/quote fuzzy match (`pc/src/core/tools/edit-diff.ts:207`), plus a per-file mutation queue for safe parallel edits (`file-mutation-queue.ts`). |
| 3 | Shell | **momo stronger** | momo: background runs with `wait_for`, grep/tail views, saved logs re-queryable via `command_output`, and `stop_command` (`harness/tools/builtin/run_command.py:217-235`). pi: a foreground `bash` with optional timeout, tail truncation and the full output in a temp file (`pc/src/core/tools/bash.ts:255`). |
| 4 | Loop and context | **pi stronger** | pi: **parallel tool execution** by default (`packages/agent/src/agent.ts:261`, `agent-loop.ts:527`), steering and follow-up queues mid-run, a structured summary (Goal / Constraints / Progress / Decisions / Next steps, `pc/docs/compaction.md:234`), cumulative file tracking, overflow/length recovery, and cache warm/stats. momo: sequential execution (`harness/harness.py:1979`), a 3-pass drop-then-summarise compaction, and a 4-chars-per-token estimate. momo's thinking handling (`think_trim.py`, `/think-history`, `/think-keep`) and `cavecat` are genuinely unique. |
| 5 | Extensibility | **pi much stronger** | pi: `registerTool/Command/Shortcut/Flag/Provider/McpServer/VirtualModel/*Renderer`, about 30 events (`tool_call`, `context`, `before_provider_headers`, `session_compact`…), `setActiveTools`, `appendEntry` (`pc/src/core/extensions/types.ts:1681-1921`), packages via npm/git, and about 60 examples. momo: personal and workspace tools (`.momo/tools/`, well sandboxed per process with fingerprint approval) and skills as plain `.md` files, but no hook or event API. |
| 6 | Providers | **pi stronger** (expected) | pi: about 30 providers plus OAuth subscriptions, a model catalog, virtual models, a llama.cpp router extension, and Ollama through OpenAI-compatible `models.json`. momo: Ollama and llama.cpp only, with zero dependencies, by design. |
| 7 | Sessions | **Split** | pi: a JSONL **tree** with `/tree`, `/fork`, `/clone`, branch summaries, HTML export and share (`pc/docs/sessions.md`). momo: linear JSON sessions, but **file checkpoints and `/undo`** with conflict preview (`harness/checkpoints.py`), which pi lacks; pi only has a `git-checkpoint` example. |
| 8 | Interfaces | **pi stronger for embedding, momo for UX** | pi: interactive, print, JSON event stream, JSONL RPC and an in-process SDK, all on one session engine. momo: a TUI and a **web UI sharing one session** (SSE, an HTTP API, changes panel, attachments, TLS), which is great for a person but has no SDK, RPC or one-shot print mode. |
| 9 | Safety | **momo stronger** | momo: file tools confined to the workdir (`harness/paths.py:59`), network off by default and reset at each launch, confirm modes for run/net/MCP, and approved-by-fingerprint workspace tools and MCP servers (`harness/approvals.py`). pi: "does not ask for approval before every tool call", only project trust, and points to containers for isolation (`pc/docs/security.md:3`). Approval gates exist only as examples (`permission-gate`, `protected-paths`). |
| 10 | Code intelligence | **momo much stronger** | A tree-sitter code index, symbols, callers, references and dependency graphs (`harness/code_index/`, `code_nav.py`). pi has only ripgrep and fd. |
| 11 | Workflow | **momo has it, pi doesn't (by design)** | Modes and roles, plan to plan-exec with step tracking, TDD green-state tracking, `ask_user`. pi pushes these to extensions. |
| 12 | Codemode and MCP | **pi stronger** | Codemode keeps nested results out of context and runs calls in parallel. MCP tools are deferred, so schemas don't bloat the prompt while servers connect. momo exposes MCP tools directly, so every server adds schema tokens. |
| 13 | Engineering | **Both good, different emphasis** | pi: about 5.5k tests across packages and Docker-based doc-lift evals. momo: about 1.2k tests, but **better behavioural evals for tool choice** (tool-reach, edit bench, replay A/B, think-trim, web-extract). momo's `harness.py` (2.6k lines) and `code_nav.py` (2.6k) are hotspots. |

## 3. Strengths and weaknesses of momo compared with pi

**Strengths**
- Robustness for small local models: recovering text-format tool calls across Qwen, Hermes, Phi, DeepSeek, Mistral, Command-R and more (`harness/toolcall_text.py`), write-intent nudges, and rescuing missing-path writes. pi assumes the provider returns proper tool calls.
- Editing quality, with measured evidence behind it.
- Code navigation and an index that cut read tokens roughly 20× on large files.
- Shell runs that suit real dev loops: background servers and saved logs.
- Undo/checkpoints for files.
- Secure defaults: workdir confinement, network off, approvals.
- Thinking-history management and cavecat token saving.
- Zero third-party dependencies, plus a web UI.

**Weaknesses**
- Large default prompt: 3.7–6.3k tokens of tool schemas against about 1k for pi. That hurts small-context models, which are exactly momo's target.
- Sequential tool execution.
- No extension or hook API, so every feature lands in core. `harness.py` and `commands.py` (70+ slash commands) keep growing.
- No SDK, RPC or print/JSON mode, so it is hard to script, embed or run in CI.
- Linear sessions with no branching or fork.
- MCP tools inflate the prompt with no deferred or search mechanism.
- Skills are whole-file prompt appends loaded by hand, where pi uses lazy load-on-demand per the Agent Skills spec with `SKILL.md` and bundled scripts.
- Compaction summaries are less structured, and token counting is a chars/4 estimate until the server reports.

## 4. What pi does differently (key ideas)
1. **A minimal default toolset**, with more tools activated on demand (`defaultTools`, `+name`/`-name`, `setActiveTools`).
2. **Exposure levels** per tool: `direct | model-only | codemode | deferred | hidden` (`pc/src/core/extensions/types.ts:522`).
3. **Codemode:** the model writes a script that orchestrates tools, and only the distilled output enters context.
4. **Everything is an extension**, even codemode, MCP, tool_search and llama.cpp (`pc/src/extensions/index.ts`).
5. **Sessions are an append-only tree**, and compaction is an entry rather than a rewrite, so history is never lost.
6. **One engine with many front-ends** (TUI, print, JSON, RPC, SDK).
7. **Security by environment** (containers) rather than in-process rails.

## 5. Worth adopting from pi (ranked by value to effort, for a local-model harness)

| Rank | Idea | Value for momo | Effort |
|---|---|---|---|
| 1 | **Deferred tools + `tool_search`.** Keep about 6–8 core tools declared and load the index, nav, web and MCP tools on demand. | Cuts 2–5k prompt tokens, which directly helps small models. You can A/B it with your existing evals. | M |
| 2 | **Parallel execution of read-only tools** (momo already knows which tools are read-only from `modes`/`writes`) | Fewer round-trips. Note that many local models emit one call per turn, so measure first. | S–M |
| 3 | **Structured compaction summary** (Goal / Progress / Decisions / Next steps / files touched) | Better continuity after compaction. A prompt change only. | S |
| 4 | **Print/JSON mode** (`momo -p "task" --json`) | Lets you script momo, run it in CI, and run evals headless. Most of the plumbing exists via `controller.py`/`events.py`. | S–M |
| 5 | **Lazy skills** (name and description in the prompt; the model reads the file on demand) + `SKILL.md` directories | Lower prompt cost, compatible with the Agent Skills ecosystem. | S |
| 6 | **Lifecycle hooks** (`tool_call` veto/modify, `tool_result` transform, `context`) as a small Python plugin API | Stops core growth, since features like cavecat, net rules and TDD could move out. | M–L |
| 7 | **Session tree / fork** from an earlier message (pairs naturally with momo's checkpoints) | Lets you explore alternatives, and rewind code and chat together. | M–L |
| 8 | **A codemode equivalent** (a sandboxed Python subset or restricted JS calling tools) | Big savings on multi-step lookups, but small models write scripts poorly. Eval before building. | L |
| 9 | Images in `read` for multimodal local models (Qwen-VL, Gemma 3) | Cheap win where the model supports it. | S |

**momo advantages worth keeping (don't "pi-ify" these away):** text tool-call recovery, the editing extras, code index and navigation, background runs, undo, secure defaults, think trimming, and the eval harness. pi has nothing comparable to any of them.

## 6. Out of scope (README-level only)
pi's `durable` (crash-safe resumable agent runs), `chord` (hot-reloadable distributed objects), `env` (remote execution over SSH via a Rust daemon) and `telemetry` show pi heading towards a hosted, long-running agent platform. momo has no counterpart, and none is needed for a local assistant.

## Optional next step
Run both harnesses on the same llama.cpp model against `evals/tasks.py` and the edit suite (pi talks to llama.cpp through its built-in extension). That would test whether pi's small toolset beats momo's rich one on small models. Item 1 above can be tested more cheaply inside momo by gating its tools behind a `tool_search`.
