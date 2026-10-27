# momo-coding-harness vs crush (and pi): review

> Reviewed 2026-10-27 against momo `7529146` **plus the uncommitted working tree**, which includes the `find_tools`/`discover.py` deferred-tool work, and crush `eded979b`. The pi column reuses [momo-vs-pi.md](momo-vs-pi.md) (pi `db14038bd`). Line references may drift.

Paths are relative to each repo. momo is `~/projects/momo-coding-harness`. crush is `~/projects/crush/internal/`, so `agent/…` means `internal/agent/…`.

## Summary

**crush is a polished, provider-agnostic product shell.** It's written in Go, about 108k lines excluding tests, and ships as a single binary with a Bubble Tea TUI. Its features include:
- an emulated `mvdan/sh` shell
- LSP as the code-intelligence layer
- SQLite sessions
- Claude-Code-compatible hooks and lazily loaded `SKILL.md` skills
- a plan agent and a read-only sub-agent
- an optional HTTP client/server split

It's built for frontier hosted models first. Local servers (Ollama, LM Studio, llama.cpp) are auto-discovered, but the prompt and tools assume a capable model: the `bash` description alone is about 7.5 KB.

**momo is a local-model workbench.** It has more, narrower tools, a tree-sitter index instead of LSP, tool-call recovery for weak models, safety rails and behavioural evals.

**pi** (see the pi doc) is the opposite of both: a 4-tool core plus a large extension platform.

Where each leads:
- **crush beats momo** on product plumbing: hooks, lazy skills, LSP diagnostics after edits, sub-agents, loop detection, unfinished-call settling, compaction triggers, sessions in a database, and a client/server API.
- **momo beats crush** on editing robustness, shell-run ergonomics, code navigation without external servers, undo, safety defaults, small-model tolerance, measured tool choice, and a smaller prompt.

## 1. Tool matrix

**Default tool-schema cost** (chars ÷ 4, on the same basis for both):

| Toolset | Cost |
|---|---|
| momo, design | ~3.6k |
| momo, coding | ~4.7k |
| momo, coding + index | ~5.7k |
| momo, coding + index + net | ~7.1k |
| momo, coding with `/tools discover` on | ~3.8k |
| crush, `coder` without LSP or MCP | **~6.2k** |
| crush, `coder` with LSP (auto-on by default) | ~7.1k |
| crush, `task` sub-agent | ~0.5k |
| pi, default | ~1k |

- momo's numbers come from JSON-serialising `registry.offered(ctx)`. Crush's are description files plus parameter strings plus structure, so they're an estimate.
- In crush, `bash` (7.5 KB) and `question` (4.4 KB) make up 71% of the description bytes.
- So crush is not lighter than momo, and momo with `/tools discover` is the leanest of the two.

| Capability | momo | crush | pi (core / ext) |
|---|---|---|---|
| Read | `read_file` (code-aware footer) | `view` (line numbers, **images**, LSP diagnostics appended, filename suggestions on a miss; `agent/tools/view.go:172,205,247`) | `read` (+ images) |
| Write | `write_file`, `append_to_file` | `write` (refuses if the file changed since last read; `write.go:75`) | `write` |
| Edit | `edit_file`, `edit_files` (multi-file), `replace_symbol` | `edit` (also creates and deletes), `multiedit` (one file), `lsp_replace_symbol` (replace, insert before or after, delete) | `edit` |
| Delete / move | `delete_file`, `move_file` | `edit` with an empty new string deletes; no move | none |
| List / find | `list_directory`, `find_files`, `file_info` | `ls` (tree, max 1000), `glob` (mtime-sorted, max 100) | `ls`, `find` |
| Grep | `grep_file`, `grep_files`, `grep_extract` | `grep` (rg, max 100, 500-char lines) | `grep` |
| Shell | `run_command` (background, `wait_for`, grep and tail views, saved logs), `command_output`, `stop_command` | `bash` (emulated sh, **auto-backgrounds after 60 s**, head+tail truncation with a spill file), `job_output` (`wait`), `job_kill` | `bash` |
| Code nav | `code_outline`, `find_symbol`, `read_symbol`, `find_references`, `file_dependencies` (tree-sitter) | `lsp_symbols`, `lsp_definition`, `lsp_references`, `lsp_call_hierarchy`, `lsp_diagnostics`, `lsp_restart` (LSP) | none |
| Refactor | `replace_symbol`, `move_file` | **`lsp_rename`** (semantic, across files) | none |
| Code index | `index_map/file/search/text/callers/changes/status` | none | none |
| Plan / tasks | `create_plan`, `complete_step`, `revise_plan`, TDD flow | **`todos`** (whole-list replace); a separate `plan` agent | ext: `todo`, `plan-mode` |
| Ask the user | `ask_user` (free text) | `question` (structured: up to 5 questions with choices, plus a confirm tab) | ext: `questionnaire` |
| Sub-agents | none | **`agent`** (read-only `task` agent, child session), **`agentic_fetch`** (small-model web researcher) | ext: `subagent` |
| Web | `fetch_url`, `web_search`, `add_search_source` (net off by default) | `fetch` (raw, 100 KB), **`download`** (to disk, binary-safe), `web_search`/`web_fetch` only inside `agentic_fetch` | none |
| Remote code search | none | **`sourcegraph`** (public GitHub) | none |
| Self-introspection | none (`/` commands only) | **`crush_info`** (model, LSP, MCP, skills, hooks, permissions), **`crush_logs`** | none |
| MCP | tools exposed directly | tools exposed directly as `mcp_<srv>_<tool>` and filterable per agent, plus **`list_mcp_resources`/`read_mcp_resource`**; prompts become slash commands; `claude/channel` push | deferred / codemode |
| Tool discovery | `find_tools` (with `/tools discover`) | none | `tool_search` |
| Scratchpad | `scratch_write` | none (bash spill files) | none |

### Tools crush exposes that momo does not
1. **`agent`**: a read-only sub-agent that runs in its own child session, so the parent context gets only the answer (`agent/agent_tool.go`, `session/session.go:116`).
2. **`agentic_fetch`**: a sub-agent on the small model that searches, fetches and distils web pages. Raw pages never enter the main context (`agent/agentic_fetch_tool.go:141-196`).
3. **`lsp_rename`**: a semantic rename across files.
4. **`lsp_diagnostics`**, plus diagnostics automatically appended to `view`, `edit`, `multiedit` and `write` results (`tools/diagnostics.go:86-130`).
5. **`lsp_call_hierarchy`**, which shows incoming and outgoing calls. momo's `index_callers` covers incoming calls only.
6. **`todos`**: a lightweight task list that also gets folded into compaction summaries (`agent/agent.go:2337`).
7. **`question`**: structured multiple-choice questions to the user.
8. **`download`**: saves a URL to disk.
9. **`sourcegraph`**: public code search.
10. **`list_mcp_resources` and `read_mcp_resource`**.
11. **`crush_info` and `crush_logs`**: the agent can inspect its own configuration and logs.
12. Images in `view`. pi has these too.

### Tools momo has that crush lacks
- The tree-sitter index (7 tools) and `file_dependencies`.
- `grep_extract` and `grep_file`.
- `move_file`, `append_to_file`, `scratch_write`.
- The plan-exec step tools and the TDD flow.
- `command_output` (re-querying saved run logs with grep and tail views).
- `edit_files` (atomic multi-file edits).
- `find_tools`.
- `add_search_source` and the source-scoped `web_search` in the main loop.

## 2. Scorecard

| # | Dimension | momo vs crush | Evidence | pi (from pi doc) |
|---|---|---|---|---|
| 1 | Tool design | **Split** | crush: tools are gated by `AllowedTools` per agent, every tool is hook-wrapped, and parallel execution is opt-in per tool via `NewParallelAgentTool` (`agent/coordinator.go:865-980`, `agent/hooked_tool.go:28-107`). Users can't define custom agents (`SetupAgents` overwrites them, `config/config.go:1088`). momo: a richer `Tool` dataclass (role, index, net and run-mode gating, guards, diff and TDD flags, discoverable) (`harness/tools/registry.py:66-110`). | exposure levels, `setActiveTools` |
| 2 | Editing | **momo stronger, but crush has two ideas momo lacks** | crush: exact match, then a whole-line whitespace-normalised fallback that re-indents to the file's indent unit, with a closest-lines hint on a miss (`tools/edit_whitespace.go:24-345`). It also **requires a read before editing and rejects stale edits** (`tools/edit.go:264-295`, `filetracker/`) and appends **LSP diagnostics** after the edit. momo: more matchers (line-number-prefix stripping, quote and escape repair, indent shift), a tree-sitter syntax check, and "uses left to update", all measured in the edit bench (`harness/editing.py`). momo has no read-before-edit or stale-file check. | weaker |
| 3 | Shell | **Split** | crush: an emulated `mvdan/sh` with exec passthrough, setsid and process-group kill (`shell/exec_unix.go:20-31`). It **auto-backgrounds after 60 s**, truncates head+tail at 2000 lines / 50 KB, and keeps the full log with 7-day retention (`shell/truncate.go`). A banned-command list and argument blockers (`tools/bash.go:76-195`) and safe commands that skip the prompt when nothing is chained (`tools/safe.go:58-72`). momo: `wait_for`, grep and tail views on logs, `command_output` and the re-run note (`harness/harness.py:2385`). momo's views are richer, crush's defaults are safer for a model that runs `npm run dev` in the foreground. | weaker |
| 4 | Loop and context | **crush stronger** | crush: auto-compaction triggers at **20% of the window remaining, or a 20k buffer when the window is over 200k, and is skipped if the window is unknown** (`agent/agent.go:56-59,1114-1134`). It has a structured summary (state, files with lines, commands that worked or failed, next steps; `templates/summary.md`) and **re-queues the original request** after compacting (`agent.go:1240`). **Loop detection** stops a turn when the same (tool, input, output) repeats more than 5 times in 10 steps (`agent/loop_detection.go:19`). **Settled results** give unanswered calls explanatory wording (`agent/settle.go:15-58`), and there's an idle stream timeout with a longer first-token wait (`request_timeout.go`). Prompts sent mid-turn fold into the next step (`agent.go:886`). momo: compacts only once the estimate passes the limit, with no headroom (`harness/harness.py:1883`), and only warns on an exact repeat of a failed call (`harness.py:2385`). momo still leads on thinking trimming and cavecat. | parallel, structured compaction |
| 5 | Extensibility | **crush much stronger** | crush: 13 hook events, including Pre/PostToolUse, PermissionRequest, Pre/PostCompact and TurnStart/End, with allow/deny/halt, input rewriting and output replacement (`hooks/events.go:6-37`, `hooks/hooks.go:46-110`). They're **Claude Code compatible** in payload and exit code 2 (`hooks/input.go`). Skills follow `SKILL.md` **and load lazily** (only name and description in the prompt; `skills/skills.go:299-322`), and crush also reads `~/.claude/skills`. Custom markdown commands with `$ARGS`. Config can be written as a bash script (`crushrc`). momo: custom and workspace tools (better sandboxed and fingerprint-approved), whole-file skills, no hooks. | pi strongest |
| 6 | Providers | **crush much stronger** | crush: a catwalk catalog of hosted providers plus `discover/` for ollama, lmstudio, llamacpp, litellm and omlx. It **reads the real context window** from `/api/show` and from `n_ctx` on `/v1/models` (`discover/llamacpp.go:38-75`), sends `top_k` via `extra_body`, and estimates usage when the provider reports none (`usage_fallback.go`). It also repairs malformed tool-call JSON (`agent.go:2372`). momo: Ollama and llama.cpp only, with zero dependencies, by design. momo's **text-format tool-call recovery** (`harness/toolcall_text.py`) goes well beyond crush's JSON repair. | pi strongest |
| 7 | Sessions | **Split** | crush: SQLite with migrations, parent/child sessions, cost and token tracking, and a **full snapshot of every file version** (`history/file.go`). There's **no revert or undo in the UI** (only diffs). momo: linear JSON sessions, but real **checkpoints with `/undo`** and conflict preview (`harness/checkpoints.py`). | tree / fork |
| 8 | Interfaces | **Split** | crush: TUI, `crush run` (non-interactive, stdin, plain text with no JSON stream; `cmd/run.go`), `session list/show --json`, and an **HTTP API over a unix socket, named pipe or TCP with SSE events and OpenAPI** (`server/endpoints.go`). momo: TUI plus a **web UI** on one session (SSE, changes panel, attachments), but no one-shot or headless mode. | print/JSON/RPC/SDK |
| 9 | Safety | **momo stronger** | crush: a prompt per call, with "allow for session" grants held **in memory only** (`permission/permission.go:115-199`). `--yolo` exists. Reads outside the cwd prompt (`tools/view.go:113-145`) and writes always prompt. Network tools are banned in bash, but there's no egress control. momo: **file tools are confined to the workdir** (`harness/paths.py:59`), net is off by default and reset each launch, workspace tools and MCP are fingerprint-approved, and custom tools run in an isolated `python -I` subprocess. crush's safe-command and banned-argument lists are worth borrowing. | weakest |
| 10 | Code intelligence | **Split** | crush: real LSP (definitions, references, rename, call hierarchy, diagnostics), auto-started when the binary and root markers exist (`lsp/manager.go:104-285`). It's type-accurate, but depends on installed servers and their start-up time. momo: a tree-sitter index that is zero-setup, offline, fast and works in any repo, plus a repo map. It has no type resolution and no diagnostics. | none |
| 11 | Workflow | **Split** | crush: `plan` agent with write tools removed and UI-detected `CRUSH_PLAN_READY` markers (`templates/plan.md.tpl`), `todos`, sub-agents, `/init` writing AGENTS.md (`templates/initialize.md.tpl`). momo: plan → plan-exec with step tracking, TDD green state, modes and roles. momo's flow is more structured, crush's is lighter and has delegation. | by extension |
| 12 | MCP | **crush stronger** | Both expose MCP tools directly. crush adds resources, prompts as commands, the `claude/channel` push, per-agent `AllowedMCP`, `ToolsListChanged` refresh and an async connect gate (`coordinator.go:314-340`). momo has approval and the `/mcp` UI. Neither defers MCP schemas the way pi does. | pi strongest (deferred) |
| 13 | Engineering | **Split** | crush: 325 test files, **VCR cassettes** for LLM tests (`agent/testdata/TestCoderAgent/*`), fake-provider integration tests (`agenttest`), race tests, golden TUI tests. **No evals.** momo: about 1.2k tests plus **behavioural evals** (tool reach, edit bench, replay A/B). That eval loop is momo's biggest process advantage. | many tests, doc-lift evals |

## 3. momo's strengths and weaknesses compared with crush

**Strengths**
- **Small-model tolerance:** text tool-call recovery, write-intent nudges and rescues, and the re-run and repeat notes. crush assumes proper tool calls and only repairs JSON.
- **Smaller prompt:** coding mode costs ~4.7k against crush's ~6.2–7.1k, and ~3.8k with `/tools discover`. crush's `bash` description alone is about 1.9k tokens.
- **Editing depth**, measured with the edit bench.
- **Code navigation with no external servers:** the tree-sitter index works offline in any repo.
- **Run ergonomics:** `wait_for`, grep and tail views, `command_output` over saved logs.
- **Real undo** via checkpoints. crush stores every file version but has no revert.
- **Safety:** workdir confinement, net off by default, fingerprint approvals.
- **Behavioural evals** for tool choice and edits. crush has none.
- **Web UI** with a changes panel.

**Weaknesses**
- **No read-before-edit or stale-file guard.** An edit can land on a file changed by `run_command` (formatter, codegen) or by the user.
- **No diagnostics feedback after edits.** momo has a syntax check but no type or lint errors. crush appends LSP diagnostics to every edit result.
- **No sub-agents**, so exploration and web research stay in the main context. That matters most at 8–32k contexts.
- **Weaker loop control:** no repeated-step detection, and compaction fires only at the limit with no headroom. momo already reads the real window (llama.cpp `/props`, Ollama `context_length`), so this is only about when compaction triggers.
- **No hooks**, so features land in core. `harness.py` is 2.75k lines and `commands.py` 1.9k.
- **Skills are whole-file appends.** crush and pi both load `SKILL.md` lazily, and crush even reads `~/.claude/skills`.
- **No headless mode** (`crush run` / `pi -p`) for CI and scripting.
- **No auto-backgrounding:** a model that runs a server in the foreground blocks until the timeout.
- No images in reads, no MCP resources, no structured `question`.

## 4. What each does differently

**crush**
1. **LSP is the code-intelligence layer**, and diagnostics are part of every edit's feedback loop.
2. **Delegation through sub-agents** (`agent`, `agentic_fetch`) that run in child sessions, with the cheap "small model" doing web work.
3. **Hooks and skills follow Claude Code's formats**, so users bring their existing setup.
4. **An emulated shell** (`mvdan/sh`) with in-process builtins (`jq`) and block-functions. Policy is enforced inside the interpreter, not by string matching alone.
5. **Product-grade failure handling:** settled tool results, idle timeouts, loop detection, `empty_media`, and exactly one RunComplete per run.
6. **One engine behind an HTTP API**, so the TUI can be a client.

**pi** (details in [momo-vs-pi.md](momo-vs-pi.md) §4)
1. A minimal core of 4 tools, with everything else as an extension.
2. **Tool exposure levels**, `tool_search` and deferred MCP. crush and momo both put MCP schemas in the prompt directly.
3. **Codemode**: scripts orchestrate tools, and only their output enters context.
4. Sessions are an append-only tree with fork.
5. Security comes from the environment (containers). It's the weakest in-process rails of the three.
6. Parallel tool execution by default. crush makes it opt-in per tool (network and sub-agent tools only), and momo runs tools sequentially.

**Where crush sits between them:** it's like pi on providers, hooks and skills, and like momo on batteries included (LSP, plan agent, todos). It's unlike both in betting on LSP and sub-agents.

## 5. Worth adopting from crush (value ÷ effort, for a local-model harness)

| Rank | Idea | Why for momo | Effort | Also in pi list? |
|---|---|---|---|---|
| 1 | **Read-before-edit plus a stale-file check** (track the mtime or hash of the last read per path; refuse `edit_file` if the file is unread or changed) | Stops edits against stale views after formatters, codegen or user edits. Small models are especially prone to this. | S | no |
| 2 | **Headroom compaction**: compact at 20% of the window remaining (momo already knows the real window from `/props` or `context_length`), not at the limit; summary with Files (path:line), Commands that worked or failed, and Next steps; re-queue the original request | Compacting earlier leaves room for the summary call itself and keeps more recent turns. | S | yes (#3) |
| 3 | **Loop detection**: stop or nudge when the same (tool, args, result) hash appears more than N times in the last K steps | A cheap guard against the classic 9B loop. It extends the existing `_annotate_result`. | S | no |
| 4 | **Settled results for unanswered or truncated calls**, worded with what a retry should change ("output tokens ran out, split the write") | Better recovery after a max-tokens cut, which is common on local servers. | S | no |
| 5 | **Auto-backgrounding** `run_command` after N seconds, which then becomes a background run queryable via `command_output` | momo already has background runs, so this is mostly a timer. | S | no |
| 6 | **Lazy skills** (`SKILL.md`, name and description in the prompt, read on demand; also scan `~/.claude/skills`) | Lower prompt cost and ecosystem compatibility. | S | yes (#5) |
| 7 | **A read-only exploration sub-agent** (`agent`) and a **web sub-agent** (`agentic_fetch`), each returning only a summary | Keeps exploration out of a small main context. momo's plan runner and controller already orchestrate model calls. Eval it, since small models summarise lossily. | M | partly (ext) |
| 8 | **Post-edit diagnostics**: run an opt-in configured linter or typechecker (`ruff`, `tsc --noEmit`, `go vet`) on the touched file and append at most 10 findings. This is a cheaper stand-in for LSP. | Most of LSP's value with no server lifecycle, and it complements the tree-sitter syntax check. | M | no |
| 9 | **Claude-Code-compatible hooks** (PreToolUse/PostToolUse via shell commands, exit code 2 = block) | A minimal hook API that reuses an existing format. It's the first step toward moving features out of core. | M | yes (#6, different format) |
| 10 | **Headless `momo run "task"`** with stdin and a plain or JSON stream | CI and scripting, and headless evals. | S–M | yes (#4) |
| 11 | Safe-command list (no chaining) for auto-approving `run_command` in confirm mode; banned-argument blockers (`pip install --user`, `npm i -g`) | Fewer prompts, and a safer YOLO. | S | no |
| 12 | `todos` as a light alternative to plan mode, folded into compaction | A cheaper workflow for small tasks. | S | ext |
| 13 | MCP resources tools; images in `read_file` | Completeness. | S | yes (#9, images) |

**Don't import:** crush's ~7.5 KB `bash` description and 434-line system prompt, since they're too heavy for momo's target models. Also skip the hard dependency on LSP; keep tree-sitter and add diagnostics as an option (#8).

## 6. Optional next step
Run a same-model A/B. crush can point at llama-server through its `llamacpp` discover provider, which reads `n_ctx`. Run both harnesses on `evals/tasks.py` and the edit suite with the same Qwen/Gemma model. That would show whether crush's heavier prompt and fewer, broader tools cost it accuracy on a 9B model. Items 1–5 above can each be A/B-tested inside momo with the existing replay and eval tooling.
