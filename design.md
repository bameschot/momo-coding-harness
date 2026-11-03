# momo-coding-harness — Architecture

## Overview

A terminal-based AI coding assistant. The harness wraps a local Ollama model in a curses TUI and exposes file, shell, and git tools to the model via Ollama's native tool-calling API. All file operations are sandboxed to the configured working directory.

---

## Process and Threading Model

A single Python process with two threads:

- **Main thread** — runs the curses TUI event loop (`tui.py`). Handles keyboard input, renders panes, and drains the event queue on each iteration.
- **Worker thread** — calls `harness.send(text)` (or `harness.compact_threaded()` for manual compaction). Sends API requests, executes tool calls, and pushes typed events back to the TUI via `harness.event_queue`.

Communication is one-way: the worker pushes events; the TUI only reads them. The worker thread is a daemon, so it exits with the process. The TUI sets `_busy = True` before starting the thread and waits for a `DoneEvent` to clear it.

---

## Key Files

| File | Role |
|---|---|
| `harness/main.py` | Entry point. Parses args, constructs `Harness`, restores last session, starts curses wrapper. |
| `harness/harness.py` | `Harness` class — owns `messages`, `event_queue`, context limit, compaction, and the `send()` agentic loop. |
| `harness/tui.py` | `TUI` class — curses layout, event drain, keyboard handling, `_LineBuffer`. |
| `harness/commands.py` | `/command` parsing and dispatch. Returns `CommandResult` flags; the TUI acts on them. |
| `harness/tools.py` | Tool definitions and implementations (file, shell, git). |
| `harness/ollama_client.py` | Thin wrapper around the `ollama` SDK. |
| `harness/md_render.py` | Standalone markdown-to-curses renderer. No TUI imports. |
| `harness/session.py` | JSON session persistence to `~/.momo-harness/sessions/`. |
| `harness/logger.py` | NDJSON request/response log alongside each session file. |
| `roles/` | System prompt markdown files, one per mode. |
| `skills/` | Optional skill overlays appended to the active role prompt. |

---

## Agentic Loop (`harness.send`)

```
user message
  └─ append to messages
  └─ loop (up to 100 iterations, 40 in design mode):
       ├─ auto-compact if token estimate > context_limit
       ├─ fold stored thinking into the next assistant message (/think-history)
       ├─ call ollama_client.chat(messages, tools)
       ├─ extract thinking from msg.thinking field or <think>…</think> tags
       │    └─ stored as role:"thinking", emitted as ThinkEvent; sent back per /think-history
       ├─ if response has tool_calls:
       │    ├─ emit ToolCallEvent to TUI
       │    ├─ execute each tool, apply result cap if set
       │    ├─ emit ToolResultEvent to TUI
       │    ├─ sanitize large file-write content before storing in messages
       │    └─ append tool results to messages, continue loop
       ├─ if response has text content:
       │    ├─ emit ChatEvent("assistant", text)
       │    └─ if no tool calls → emit DoneEvent, return
       └─ safety valves (see below)
```

Tool results are appended as `{"role": "tool", ...}` messages. Thinking content (`role: "thinking"`) is stored as its own message and never sent as one. `Harness._api_messages()` attaches it to the following assistant message as `reasoning` (llama.cpp `reasoning_content`, Ollama `thinking`) when `/think-history` covers it: `turn` = since the last user message, `all` = every turn, `off` = never. A cut-off reply's reasoning and reasoning shown as the answer are marked `unsent`.

### Iteration limits

- Design mode: 40 iterations maximum.
- All other modes (coding, chat, plan, momo): 100 iterations maximum. Plan execution gets 100 iterations *per step*.

On hitting the limit `_wrap_up` makes one request with no tools (`_no_tool_reply`, shared with the loop analysis) asking what is done, what is left and the next step (`_WRAP_UP_PROMPT`). The reply becomes the turn's assistant answer; an empty one falls back to the `ErrorEvent`. The loop then returns `"limit"`.

### Steering

`Controller.submit` hands a message typed while a turn runs (busy, not waiting for an answer) to `Harness.steer`, which queues it under a lock. At the top of each round `_run_loop` drains the queue with `_append_user` (merging, bridging tool → user, new turn anchor) and moves the loop guard's epoch. A message queued after the turn's last request is run as one more `send` by `Controller._finish_bg`, which checks the queue and clears busy under the controller lock so nothing is left unsent. On a cancel, `send` drops the queue with a note.

### Server errors

The `except` around `client.chat` classifies the error (`_server_error_kind`):
- **transient:** status 408/429/5xx, or a refused or dropped connection, a timeout or a model loading, without a status. Retried after `_SERVER_RETRY_DELAYS` (2, 4, 8 s) with `self._cancel.wait`, so Esc stops it. The counter resets after a successful request, and a retry does not count as a round.
- **overflow:** `_RX_OVERFLOW` (llama.cpp "exceeds the available context size", "prompt is too long", …). `_compact()` runs once per turn, then the request is sent again.

Anything else ends the turn with an `ErrorEvent`.

### Nudge logic

When the model makes 10 or more consecutive tool-only turns (assistant + tool results, no text response), a user-role message is injected asking it to stop and summarise its findings. This fires at most once per `send()` call. Design mode receives a different nudge from other modes.

### Write-intent recovery

If the assistant response contains phrases like "let me write", "i will write", "write the complete", etc., but no `write_file` tool call was made, a targeted recovery nudge is injected — "You said you would write X — please call write_file now." One recovery nudge per `send()`.

### Empty response handling

If the model returns no text and no tool calls:
1. **First retry** — inject an explicit prompt to respond. If `done_reason == "length"` (context cutoff), suppress thinking for the next turn to avoid XML malformation. If only thinking was returned (no output), also suppress thinking on retry.
2. **Second empty in a row** — emit `ErrorEvent("No response from model")`, pop the injected retry message, and exit.

### Thinking suppression (`_suppress_think_next`)

Thinking is disabled for one turn after: (a) the context window was cut off (`done_reason == "length"`), or (b) the model produced only thinking with no output. This prevents Qwen3's `<think>…</think>` XML template from appearing in the next turn's content.

### Text tool-call recovery

Some models emit tool calls as plain text rather than using the API's structured tool-call field. `recover_text_calls` (and `_extract_text_tool_calls`, its calls only) applies successive fallbacks before giving up (each later tier only runs when the earlier ones found nothing):

1. **Tagged / structured formats**: Recognises model-specific wrappers — Qwen3/Hermes `<tool_call>`, Functionary `<functioncall>` / `<function_call>`, Phi `<|tool_call|>`, DeepSeek `<｜tool▁call▁begin｜>`, Mistral `[TOOL_CALLS]` array, Command-R `Action:/Action Input:`.
2. **`<tool_call>` payloads**, read in the order written, up to `</tool_call>`, gemma's `<tool_call|>` or the next call. `_lenient_json` reads the JSON slips gemma makes there: raw newlines in strings, a backslash before a line break, and (only when a closing tag shows the model ended the call, never for a reply cut off by the output limit) a dropped closing brace. Story-mud run 2 lost a story to four such `write_file` calls. Some models put the argument object directly inside the tag with no `{"name":…,"arguments":…}` wrapper: a payload with a `content` key is attributed to `write_file`, any other to the one offered tool whose parameters it fits (`{"command": …}` → `run_command`); an ambiguous one is dropped.
3. **Python-call syntax**: gemma-family models often emit calls as Python source — `edit_file(path='a.py', old_string='x', new_string='y')` or positionally `edit_file("a.py", "x", "y")`, sometimes wrapped in `print(...)`. `_match_paren` carves out the balanced `name(...)` (skipping quoted strings), then `ast` parses it and `ast.literal_eval` recovers each argument. Positional args are mapped to the tool's declared parameter order; keyword args override. Bare mentions in prose are rejected because their values are not literals.
4. **Bare JSON anchored to tool names**: pattern built dynamically from the current tool set; scans for `{"name": "<known_tool>", "arguments": {…}}` or `known_tool({…})`.

Tiers 3 and 4 count a call only when it **ends the reply**. Walking back from the end, each call may be followed only by closing syntax (`)`, `;`, backticks, a code fence, leaked turn markers) or by another accepted call (`_trailing`). A model that writes a call as text stops after it. An answer that mentions one goes on: a code example in a chat answer, or "used as `write_file('out.txt', 'hi')` in main.py" in a summary. Recovering that mention ran the call (writing the file) and dropped the answer. Other details:
- A `print(…)` around a call belongs to it.
- A single dict-literal argument (`write_file({...})`) is the arguments object.
- Tagged formats (tiers 1–2) are unambiguous and count anywhere.

`recover_text_calls` also returns the text left around the calls: the markup cut out, and an emptied code fence removed. `_run_loop` keeps it as the call's preamble, as with a native call, rather than dropping it.

If a text tool call is recovered it is executed normally; if every tier fails the text is treated as a regular assistant response, unless the reply was cut off in the middle of it (see below).

A call (native or recovered) whose path, command or other short argument contains gemma's chat-template tokens (`<|tool_call>`, `<tool_call|>`, `<|channel>`, …) is not run (`template_leak`, in `Harness._dispatch`). When gemma's call goes wrong, llama.cpp's parser can end a string argument only at the next quote, so the argument carries the rest of the turn: story-mud run 2 listed the directory `.}<tool_call|><tool_call|>ep<|channel>thought…`. File text and prose arguments (`content`, `new_string`, …) may quote the tokens on purpose and are not checked.

Reasoning in the content (`_extract_and_strip_thinking`): every `<think>…</think>` block, an unclosed `<think>…` at the end, and text before a `</think>` with no opening tag. Templates that open the block in the prompt (Qwen3 thinking models, llama.cpp `--reasoning-format none`) produce that last form; it used to be shown as the answer, tag and all.

A `write_file` / `append_to_file` with `content` but no `path` gets a name only for a Markdown document (its first H1, not in code) or `design.md` in design mode (`_derive_write_path`). Otherwise the required-argument error goes back to the model; code used to land in `untitled.md`.

### Keeping the anchor (`editing._keep_check`, edit feature `keep`)

`edit_file`, `edit_files`, `replace_symbol` and `write_file` (over an existing file) refuse, before writing, a change that removes a definition while it adds a different one: the anchor mistake, where old code located the place for new code and was left out of the replacement. With a parser it's `defdiff.anchor_loss`: `_gone` against the added names, with a rename (the same source under the new name) excused. For `write_file`, any removed definition counts. Without a parser, or for a file with no definitions, it's `editing._header_swap`: the first lines of `old_string` and `new_string` are headers (a name before `(`, or after `class`/`struct`/`fn`-style keywords) with the same indentation and first word but a different name, and the old one is gone from `new_string`. A refusal is remembered in `_REFUSED` (32 entries, keyed by resolved path, tool and a hash of the call); the identical call sent again is applied, with a note. Any write to the path forgets its refusals. Tests: `tests/test_anchor_loss.py`, covering every `evals/lang` project with and without the parser, and the recorded bench edits in `tests/golden/anchor_edits.json`.

### Input completion (`harness/complete.py`)

Both frontends complete the input box with the same code: the TUI calls `complete.complete(..., wait=False)` on its key loop (the workspace file list and the model list come from caches refreshed in the background, and `waiting` asks it to retry), the web UI calls `GET /api/complete?text=&cursor=` (`wait=True`). "/" completes every word from the `/help` usages, parsed into literals, choices, optional words (`[force]`) and placeholders; a placeholder takes live values from `Sources.values` (models, sessions, MCP servers, tools, skills, thinking levels) or completes a path: inside the workspace through `safe_path` for `/read`, `/ls`, `/grep`, `/undo file`, `/export`, any folder for `/workspace`. "@" is a fuzzy search over files and their folders, or, once the query has a "/", that folder's entries. An item's `more` keeps the list open after a pick (a folder, a word with words after it).

### End-of-turn check (an announced next step)

In coding and momo mode (`Harness._run_loop`, `_ends_turn_checked`: no plan step or story running), a reply without a tool call that only announces its next step (`toolcall_text.announces_next_step`: first-person intent in the last 400 characters, or a last line that is an imperative plan step; not a question) gets `_NEXT_STEP_NOTE`, at most `_NEXT_STEP_NOTES` (2) per turn. The reply and the note are appended as a user turn and the loop continues, like the design-mode write-intent nudge. The detector is checked against labelled final replies from the multi-part bench (`evals/multipart_bench.py`, `tests/golden/next_step_replies.json`). A `todos` checklist tool with an open-items note was tried alongside it and removed: a 26B called it once in 27 runs, and this note alone stopped the early endings it was meant for (`evals/README.md`, "The todo list").

The detector looks at the sentences in the last 400 characters. One counts when it has an intent ("I will", "let me", "my next step is", …) followed within six words by an action verb, and isn't an offer or deferral ("if you", "let me know", "would you like", "next time", …). Without that last rule, finished summaries such as "Let me know if you want more, and I'll add them." got the note and the model went on with work nobody asked for. A last line that is a list item starting with a verb still counts.

### Reasoning watch (`/think-budget`)

`Harness._delta_sink(watch, reply_watch)` feeds every streamed reasoning delta (the adapter's reasoning field, or `<think>` text in the content stream) to a `think_watch.ThinkWatch`, and every reply-text delta to a second one (`stream="reply"`). The reasoning watch raises `think_watch.Overrun` out of the adapter's stream when:
- the reasoning passes `think_budget` tokens (chars / 4);
- one line of `MIN_LINE`+ characters, after case and whitespace normalisation, appears `REPEATS` (3) times in the last `WINDOW` (6,000) characters;
- the last `RATIO_WINDOW` (24,000) characters compress below `MIN_RATIO` (0.10) once `RATIO_MIN_CHARS` (8,000) have streamed. This is checked every `RATIO_STEP` characters with `think_watch.looping`.

The reply watch has only the compression trip, since code repeats lines legitimately. `_run_loop` catches the overrun at the chat call, and `_think_overrun` handles it:
- `client.abort()` closes the stream, so the server stops generating;
- the reasoning is stored as `thinking` marked `unsent`, followed by an empty assistant turn (role alternation). A reply overrun stores nothing of the looping text;
- `_THINK_NOTE` follows as a user turn, with the role's next action and either `think_trim.trim(..., "lean")` of the reasoning, the repeated line, or (for a reply) `_REPLY_REPEAT_TAIL`;
- the next request runs with thinking suppressed.

At most `STOPS_PER_TURN` (2) in a row: `think_stops` resets when a reply has a tool call. After 2 stops without progress, later replies get no watch until the next call. Without streaming there are no deltas, so only `/max-output-tokens` applies.

A reply the cap cuts off without a call keeps only `settle.trim_capped_reply` (its first 1,500 characters) in the history. `settle.cut_off_cause(..., text)` returns `LOOP_CAUSE` for looping text, and `retry_advice` then says to call the tool directly instead of splitting the call. `/think-budget repeat off` turns the line and compression trips off for reasoning, `/think-budget reply-repeat off` turns off the reply watch, and `/think-budget off` turns the budget off.

### Progress note (`/progress-note`)

`_run_loop` counts `idle_calls`: consecutive executed tool calls that wrote nothing (`DIFF_TOOLS` or a tool's `written_paths`), outside `_PROGRESS_TOOLS` (`ask_user`, `ask_choice`, the plan and story tools). Text alongside a call does not reset it, unlike `tool_only_turns`. A loop reset does. At `_IDLE_AFTER[role]` (15; momo 12; plan and story-writer 25) and again at twice that, `_IDLE_NOTE` with `_IDLE_ACT[role]` is appended as a user turn (story-writer: `_writer_next`). The role key is `story_role`, else `plan-exec` while a plan runs, else the mode.

### Ask before planning (`/plan questions`)

`roles/planner.md` has the planner investigate the options for each decision that shapes the plan, then ask about them with `ask_choice` before `create_plan`. `_run_loop` counts the `ask_user` / `ask_choice` answers given while investigating in `_plan_asked`. `PlanRunnerMixin._handle_create_plan` refuses a create_plan with `_ASK_FIRST` when `plan_questions` is on, there is no draft (`self.plan is None`) and nothing was asked. It refuses once per plan (`_plan_gate_refused`): the next create_plan goes through. `approve_plan`, `cancel_plan` and a new session reset both. `create_plan` is exempt from the loop guard, so the refusal is not counted as a repeat. `/plan questions` is allowed while a turn runs (`controller._blocked_while_busy`).

### Settled results (calls not run)

A call the harness does not run still gets an answer that says why and what a retry should change (`harness/settle.py`). Every result starts with `ERROR: not run`, so the loop guard counts the same cut-off made again like any repeated failure.

- **Cut off or malformed arguments.** The adapters keep the JSON error on `ToolCall.args_error` (arguments stay `{}`). With `done_reason == "length"` the call is settled as *truncated* (the cause is the `/max-output-tokens` cap when the reply used 95% of it, else the context window), with advice per tool: `write_file` then `append_to_file`, smaller edits, or smaller arguments. Otherwise it is *malformed*. Complete calls earlier in the batch run normally. Thinking is suppressed for the next turn when the cut-off reply had reasoning.
- **Esc during a batch.** The remaining calls are settled as *interrupted*, and the loop then returns `cancelled`.
- **A text call cut off.** `partial_text_call` finds an opening tag, an unparseable `{"name": "<tool>"`, or an unbalanced `<tool>(`. The text before it is kept as the assistant turn, and a harness note with the same advice follows. At most 2 per `send()`.
- **An empty cut-off with no reasoning** retries with `_RETRY_TEXT["cut_off_call", …]`, which asks for a smaller call.

### Missing-path write rescue

Some models emit `content` first and drop the trailing `path` on large writes. Before dispatch, a `write_file`/`append_to_file` call that has `content` but no `path` is rescued: `_derive_write_path` infers a filename from the document's first Markdown `# H1` (kebab-cased), falling back to `design.md` in design mode or `untitled.md` otherwise. A system note reports the inferred path.

### Argument validation and tool auto-routing (`dispatch`)

Small models frequently confuse the file-writing tools — calling `write_file` while passing `edit_file`'s `old_string`/`new_string`. `dispatch` (`tools.py`) guards against this from schema-derived maps (`_REQUIRED_ARGS`, `_KNOWN_ARGS`):

- **Auto-route the unambiguous case** — `write_file` with `old_string`+`new_string` and no `content` clearly means an edit, so it is re-dispatched as `edit_file` (the result is prefixed with a routing note).
- **Reject unknown arguments** with a clear message listing the valid parameters and a "did you mean" hint (e.g. *"write_file does not accept old_string … use edit_file"*), instead of letting Python raise a `TypeError` that leaks the internal function name.

The `edit_file` tool absorbed the former `replace_all_in_file`: it changes one occurrence by default and every occurrence when `replace_all=true`.

### Resilient edit matching (`_edit_file`)

Small models frequently fail an exact `old_string` match — usually by copying `read_file`'s `  12: ` line-number prefix or by getting the leading whitespace wrong. When the exact substring match fails, `edit_file` tries, in order:

1. **Prefix strip** — remove the `^\s*\d+:\s` read_file line-number prefix from `old_string`/`new_string` and retry the exact match.
2. **Whitespace-tolerant unique match** (single-edit path only) — match the block line-by-line ignoring each line's leading/trailing whitespace; only acts when exactly one block matches, and transfers the file's real per-line indentation to the replacement (requires a 1:1 line edit so indentation transfer is unambiguous). Never edits an ambiguous or non-unique match.
3. **Closest-match hint** — if all matching fails, the "not found" error appends the file lines most similar to `old_string` (via `difflib`) so the model can copy the exact text on its next attempt.
4. **Already-applied detection** — if `old_string` is gone but a substantial `new_string` is already present, the edit was very likely made on an earlier turn; `edit_file` returns a non-error "No change needed" message so the model stops re-issuing the same edit (a common source of repeated "not found" errors when small models loop). Successful edits also return `"OK — 1 change applied"` rather than a bare `OK`, which further discourages redundant retries.

### File-edit diffs

When a mutating tool (`edit_file`, `append_to_file`, `write_file`, `delete_file`, `move_file`) succeeds, the harness emits a `DiffEvent` instead of the terse `ToolResultEvent`: it snapshots the target file before and after the call and builds a unified diff (`diff.py`). The TUI renders it with a two-column old/new line-number gutter, colored additions/deletions, in compact or git style. On error or a no-op the plain `ToolResultEvent` is emitted instead.

### Tool content sanitization

For `write_file` and `append_to_file` tool results stored in the message history, the `content` field of the *assistant* message (which contains the file content that was written) is replaced with a short placeholder `[written to <path>]`. This prevents large file content from accumulating in the context and avoids Qwen3 template corruption on subsequent turns.

### `ask_user` blocking

When the model calls the `ask_user` tool, the worker thread blocks on `_user_input_queue.get()` and emits an `AskUserEvent`. The TUI sets `_waiting_for_input = True`, updates the status bar to `? waiting for input`, and routes the next submitted line back to the harness via `harness.provide_user_input()`. The worker thread then unblocks and continues the agentic loop with the answer injected as a tool result.

### Tools on demand (`/tools discover`)

Which tools are on demand is `Tool.on_demand(mode)` (`tools/registry.py`): off by default; in `extensions` every custom and workspace tool unless `discoverable=False`, in `all` also the built-ins marked `discoverable=True`; MCP tools follow their server's `exposure`. `_run_loop` receives its tool list once, but with discovery on a batch can load tools: `find_tools` (answered in the loop, like `ask_user`), a call to a catalog tool that is not loaded yet, or a result that names a tool its tool lists in `Tool.suggests` (`_load_suggested`). After such a batch the loop re-reads `_current_tools()`, so the next request declares them. Text tool-call recovery matches the catalog as well as the declared tools. The system prompt is built from the core tools plus the full catalog (`tools/discover.py`), never from what is loaded, so a load leaves it unchanged; the loaded set is saved with the session.

---

## Qwen3-Specific Handling

Qwen3 uses an XML chat template that wraps tool results in `<tool_response>…</tool_response>`. Special handling applied when a Qwen model is active:

- **Tool result escaping, on demand** — `&`, `<`, `>` in tool results and past tool-call arguments are HTML-escaped in the API payload copy (stored messages keep the real text). This happens only for a model whose Ollama server answered a request with an "XML" error: the first such error resends the request escaped, and later requests for that model are escaped from the start (`OllamaClient._xml_escape_models`). The model reads the escaped copy and copied `&lt;` into its edits, so unescaped is the default.
- **Tool call markup stripping** — `<tool_call>…</tool_call>` XML left in the assistant content field is stripped before storage.
- **Gap bridging** — if a `role: "tool"` message appears in history with no preceding assistant, a dummy `{"role": "assistant", "content": ""}` is inserted to satisfy Qwen3's strict alternation requirement.

---

## TUI Layout and Rendering

Three panes stacked vertically:

```
┌─ chat pane (scrollable) ──────────────────────────────┐
│  conversation, tool calls, thinking blocks             │
│  [horizontal scrollbar when content > terminal width]  │
├─ status bar ──────────────────────────────────────────┤
│  mode | model | host | ctx% | workdir | spinner when busy │
│  (workdir shortened from the front with … when it does  │
│   not fit; an animated companion bar sits above this)   │
├─ input area (5 rows) ─────────────────────────────────┤
│  multi-line input; command history via ↑/↓             │
└───────────────────────────────────────────────────────┘
```

### `_LineBuffer`

The chat pane is backed by `_LineBuffer` — a list of `(text, color_pair_id, curses_attrs)` tuples. Each line is a single color (curses constraint). `render()` handles both vertical and horizontal scrolling:

- Vertical: `_scroll` offset into the line list; v-scrollbar drawn in the last column.
- Horizontal: `_hscroll` column offset; when `max(line_width) > pane_width` a horizontal scrollbar (`◀───█████───▶`) occupies the last row of the pane. Left/right arrow keys scroll it when the chat pane is focused.

### Markdown Renderer (`md_render.py`)

A pure-Python, single-pass line-by-line state machine. Takes a markdown string and returns `list[tuple[str, int, int]]` (text, color_id, attrs) ready for `_LineBuffer.append()`. No external libraries.

Supported elements:

| Element | Rendering |
|---|---|
| `# H1` | `═══ HEADING ═══` bold cyan |
| `## H2` | `─── Heading ───` bold cyan |
| `### H3` | `▸ Heading` white |
| `- / * / +` list | `  •` prefix, word-wrapped |
| `1. 2.` ordered | `  1.` prefix, word-wrapped |
| ` ``` ` code block | `│ code` line-by-line, white |
| `> blockquote` | `  ▌ text` yellow |
| `---` / `***` rule | `────────────────` |
| `\|table\|` | box-drawing table, full-width, h-scroll compatible |
| `**bold**` | `A_BOLD`, white |
| `_italic_` | white (word-boundary only — snake_case not affected) |
| `` `code` `` | `‹code›` |

Table columns are aligned per separator row (`:---` left, `---:` right, `:---:` centre). Headers are always centred. Numeric columns auto-right-align when no separator row is present. Tables render at full content width; the `_LineBuffer` h-scrollbar handles viewing when wider than the terminal.

Toggle with `/markdown on|off` or `Shift+M`. On by default.

---

## Context Management

Token usage is estimated as `sum(len(content) // 4)` across all messages. This is a fast approximation — `tool_calls` fields are not counted, so real usage can be higher in tool-heavy sessions.

### Context limit

Set to `model_reported_context // 2` at startup. Using half the model's maximum leaves room for the response itself. Override with `--context N` or `/context N`.

### Auto-compaction

Triggered at the top of each `send()` iteration when `_token_estimate > context_limit`. Target: reduce to `context_limit // 3`.

### Compaction algorithm (`compact()`)

**Pass 1 — delete tool-call groups (oldest first)**

Walks `messages[1:]` looking for assistant messages followed by tool/thinking messages. When found, the entire group (assistant + all consecutive tool/thinking messages) is deleted atomically. Orphaned tool/thinking messages (no preceding assistant) are also cleaned up.

**Pass 2 — delete oldest user/assistant pairs**

Walks again. Each `user` message found is deleted, plus the following `assistant` if present.

The system prompt at index 0 is never touched.

**LLM summarisation**

The summary is rendered from `Harness.summary_state` (`compaction.new_summary_state`, saved with the session), never parsed back from the context. `merge_facts` reads the dropped messages for the user's requests, file operations (tool calls paired with their results by order; `ERROR` and `No change` results don't count) and `run_command` exit codes. `serialise_removed` gives the summariser a labelled transcript with tool-call arguments clipped and tool results kept head and tail. `summary_prompt` asks it to update the previous LLM sections (Goal, Constraints & preferences, Progress, Decisions, Failed attempts / gotchas, Next steps). `parse_sections` validates the reply: no Goal, or fewer than 3 sections, means it is ignored, and so does a failed call. `render_summary` shrinks the block until it fits its budget, and the result is prepended to the oldest remaining `user` message as `<context-summary>\n...\n</context-summary>`. A legacy `[Earlier context summary:` block is kept as the `legacy` section until a summary succeeds. The state is committed only after the summary is in, so a cancel changes nothing.

### Manual compaction

- `/compact` — runs with LLM summarisation; non-blocking (worker thread, spinner shown).
- `/fast-compact` — skips the LLM summary (the harness-built sections are still updated); instant.

---

## Command Dispatch

`commands.handle(line, harness)` parses `/commands` and returns a `CommandResult` dataclass. Flags tell the TUI what to do:

| Flag | Effect |
|---|---|
| `exit_app` | Save and quit |
| `output` | Print string to chat pane |
| `toggle_tools` | Toggle tool-call pane visibility |
| `toggle_think` | Toggle thinking-block visibility |
| `toggle_md` | Toggle markdown rendering |
| `diff_output` / `diff_style` | Toggle edit-diff visibility; switch compact/git diff style |
| `companion` | Show/hide the animated companion bar |
| `replay_session` | Re-render loaded session messages into chat buffer |
| `run_compact` | Start compact on worker thread (with `compact_summarise` flag) |
| `confirm_prompt` / `confirm_action` | Show yes/no prompt; call action on confirm |

Commands that need a worker thread (currently `run_compact`) follow the same pattern as `send()`: set `_busy = True`, start a daemon thread, let events come back via the queue.

---

## Logging (`logger.py`)

A NDJSON log file is written alongside each session at `~/.momo-harness/sessions/<timestamp>.log`. Every record includes an ISO 8601 UTC `timestamp` and a `type` field:

| type | Fields |
|---|---|
| `request` | mode, model, message_count, prompt_tokens (estimated) |
| `response` | mode, model, prompt_tokens (API), eval_tokens (API), total_tokens, has_tool_calls |
| `tool_call` | mode, model, tool (name), args (dict) |
| `tool_result` | mode, model, tool (name), result_length (char count, not content) |
| `compact` | mode, model, removed_messages, tokens_before, tokens_after |

`/cost` reads the log and aggregates `response` records by `(mode, model)` pair, summing in/out/total tokens and call count. Missing or malformed records are silently skipped.

---

## Sessions

JSON files at `~/.momo-harness/sessions/<timestamp>.json`. The timestamp format is `YYYY-MM-DDTHH-MM-SS` (hyphens in the time portion, not colons, for filesystem compatibility).

Session fields: `created_at`, `model`, `host`, `mode`, `workdir`, `context_limit`, `context_pct`, `active_skills`, `input_history`, `messages`. The Ollama `host` is restored on load (before the model context query runs against it); the auth token is deliberately never persisted.

Sessions are auto-saved at the end of every `send()` call. `Harness._mark_current` records the session in use with its workspace (`session.mark_current`): the prefs `last_session` and `last_workdir`, and `workspace_sessions[workdir] = ts` (50 kept; another workspace's entry for the same session is dropped, since it moved). It runs from `new_session`, `load_session`, `clear_conversation`, `/workspace` and a launch that restores nothing. `main` asks `session.restore_target(given)` before it builds the Harness:
- With no `--workspace`, it gets `last_session` (if it has a conversation) and `last_workdir` (if it is still a directory). The remembered workspace also beats the one saved with the session, since a `/workspace` in an unsaved session is newer.
- With `--workspace PATH`, it gets that workspace's entry, only if it has a conversation and was saved in PATH.
- An entry that is cleared or moved means nothing is restored, never an older session and its plan.
- Without the prefs (older prefs files), it gets the newest session with a user message, in PATH when one is given.

`--fresh` skips all of this. `Harness.clear_conversation` (`/clear`) deletes the saved JSON (`session.forget`) and resets the plan, `compacted`, `summary_state` (otherwise the next compaction would write the old requests into the new first message), recap lines and an unlocked title. The TUI replays the message history into the chat buffer at startup via `_replay_session()`.

`/session <name>` resolves sessions by exact filename stem first, then partial prefix — so `2026-06-22` matches the first session whose stem starts with that string.

---

## Roles and Skills

The system prompt is built from `roles/<mode>.md` plus any active skill files from `skills/`. Skills are appended in load order separated by `---`. Switching mode or loading/unloading a skill rebuilds `messages[0]` in place. Active skills are persisted with the session.

### Modes

| Mode | Role file | Tools | Purpose |
|------|-----------|-------|---------|
| `design` | `roles/designer.md` | read-only + code nav + `write_file` + `ask_user` | Interview-based design partner; explores codebase, writes specs |
| `plan` | `roles/planner.md`, then `roles/coder.md` + plan state | investigating: read-only + code nav + `run_command` + `ask_user` + `create_plan`; executing: all tools + `complete_step` + `revise_plan` | Investigates, asks, writes `.momo-plan.md`; after approval executes it step by step (`Harness._execute_plan`), deleting the file when done. A stories plan (`/plan stories`, `harness/story_runner.py`) runs each phase as a story-writer pass (`roles/story-writer.md`, role `story-writer`: read-only + `run_command` + `plan_stories` / `write_story` (one per turn) / `finish_stories`) and then one story at a time in a fresh context (role `story-exec`: coding tools + `complete_story` / `report_defect`) |
| `coding` | `roles/coder.md` | all tools | Engineer; full read/write/exec/git access |
| `chat` | `roles/chat.md` | read-only + code nav + `ask_user` | Conversational Q&A over code and documents; never writes files |
| `momo` | `roles/momo.md` | all tools | Cat companion; full tool access with a warm, curious persona |

### Stories plans (`/plan stories`)

What each rule answers is recorded run by run in `evals/README.md` ("Story mode runs"); this section keeps the mechanisms.

**Planning.** A stories plan is written in steps (`PlanRunnerMixin._outline_plan`, `_handle_write_phase`, `_handle_finish_plan`, then `_submit_plan`, the one-call path normal and TDD plans keep): one large nested plan could not be re-sent intact after a refusal. `create_plan` takes the frame (title, goal, `interfaces`, `requirements`) and an outline; each phase comes alone through `write_phase` and is checked by `_phase_problems`. A refusal lists every problem with a valid phase to copy (`_refuse_plan`); the first in a row is answered in place, a second restarts the planner (`_plan_restart` → `_restart_planning`): the planning messages go to the archive (`by="plan-retry"`) and the context becomes the system prompt plus `_plan_summary`, written from the harness's records — the request, the questions and answers (`_plan_qa`), the goal, the draft with each phase ✓/✗, the reasons, and `_plan_next`. Whether it tells the planner not to send `create_plan` again depends on `draft["frame_refused"]`. `PLAN_MAX_RESTARTS` (5) bounds restarts without progress; then planning pauses with the summary in `.momo/plan-draft.md`. A re-sent outline keeps written phases by normalised title first (`_kept_phases`), by position only for the unmatched rest. Checks that read the planner's wording (`_HEURISTIC`: a request list, the phase-1 slice) yield after two refusals in a row per scope (`_yield_heuristics`); a request list counts only when its label names what the product offers (`_RX_LIST_LABEL`). `Plan.from_markdown` ends Goal and Request only at its own headings (`plan._SECTIONS`), and `approve_plan` keeps the request it had. A ```json block holding a tool's bare arguments that ends a reply is recovered as that tool's call when exactly one offered tool fits it (`_nameless_call`).

**Expectations.** A criterion is a behaviour with examples — what is done through one of the plan's `interfaces` → what must come out, with exact values — and never code. `stories.criterion_problems` refuses missing examples, code (`_RX_CODE`: an import statement, `assert`, dunders, `obj.method(` unless `obj` is a declared library, `library_modules`) and examples that only run the tests (`runs_the_suite`; `drop_suite_runs` takes them out with a note while other expectations remain). A missing exact value is advice, not a refusal (`criterion_notes`). The text the planner and writer follow is one constant, `prompts.EXPECTATIONS` (`{expectations}` in `roles/story-writer.md`). Shell `check`s written up front were the older model; they named code that did not exist and the implementer bent the code to them, so they are gone (an older plan's `Check:` lines are dropped on load).

**Running a phase.** `_execute_stories_plan` runs the phases in order; `_run_phase` loops: write stories if the phase has no approved set, run the next ready story (`StorySet.next_ready`), and when all are done, the gate. Each role starts from a fresh context (`_fresh_context`, archived `by="story"`). The writer outlines (`plan_stories`), writes one story per turn (`write_story`, refused twice in one turn), and submits (`finish_stories`, `_submit_stories`): the set is validated (`StorySet.validate`, `_coverage_problems`), stored unapproved, shown, and approved per `/plan stories approve each|auto`. A `phase_revision` is checked with `_phase_problems`, asked after the stories are approved and applied only then; it moves the requirements it names, and any left past the last phase go to it (`_apply_phase_revision`). A story's brief (`_story_brief`) carries the story, what its dependencies provide and delivered (with a code outline of their files), the interfaces and the test command. Stories and writer passes stop the background commands they started (`_stop_background_runs`), and in a story role `run_command` reaps what a finished foreground command left running (`run_reap`). A user message sent while the plan is paused goes to the next brief (`_resume_note`, `_take_resume_note`), since every story role starts fresh.

**Verification.** `complete_story` runs the story's own test files (`_run_own_tests`: `_own_tests`, and `_suite_finds_tests` when it wrote any) and then `_implement_and_verify` runs the `story-verifier` role (`_verify_story`) in a fresh context: it writes a black-box test per expectation in `tests/acceptance/` and saves it with `save_acceptance_test`; `finish_verification` gives the verdicts. The harness checks what it cannot leave to the model: the verifier writes nowhere else (`_verifier_guard`), a saved test must contain every expected value (`expected_values`; an apostrophe inside a word is not a quote), may not import the project's modules unless a Library interface is declared (`_project_imports`), and is run by the harness; `pass` needs a saved test that passed. An unmet expectation goes back to the implementer in its own context. One pass per batch (`GATE_BATCH` at a gate), then one pass alone for each it did not get to (`_verify_batch`); a pass is capped at `verify_cap`. `not_yet`, `unclear`, or not verified defers the expectation to the gate (verdict `deferred`), which keeps that mark until a gate verifies it; when none of the interfaces' files exist yet (`_absent_interfaces`, including `-m pkg.mod`), the deferral needs no pass. A story at its turn cap or ending in text goes through `_accept_text_end` and is accepted when its tests and the verifier pass.

**Failure and amend passes.** Every pass that adds to a phase's stories goes through `_amend_stories`: a re-split of a failed story and its pending dependents (`_resplit`), a fix story for a defect `report_defect` found in earlier code (`_fix_defect`, `blocks`: the blocked story depends on the fix, so a cycle is refused), a corrected story for a wrong expectation (`_fix_check`, `remap`), and a gate fix. The pass works on a snapshot (`StorySet.amend`, stored on its unapproved draft), so feedback never turns drafts into existing stories and `/plan resume` continues the same pass instead of rewriting the phase. Replacements inherit the failed story's attempts; `MAX_ATTEMPTS` (2) failures pause the plan. An amend pass may add nothing when retiring is all it needs (`finish_stories` with `retire`, or `may_be_empty`).

**The gate.** `_phase_checks`/`_gate` verifies the phase's own expectations and the deferred ones, re-runs every saved acceptance test (`_saved_tests`: they double as regression tests), runs the project's test commands at the last phase (`_test_commands`: the request's own, `plan.request_commands`, and those proving test-suite requirements), and at the last phase fails on any requirement without a passing test. It records its failure kinds (`sset.gate["kinds"]`): a gate that only ran out is verified once more with more turns (`_verify_bonus`), then pauses without a writer pass; otherwise one fix pass (`GATE_FIXES`). A test of behaviour the phase replaced on purpose, or an expectation still unclear, is retired with `finish_stories`' `retire` (`_retire_problems`, `_apply_retire`; ids like `P1.S1.4` or `phase 1.2`) — an unclear one restated in a new story. When the gate passes, a no-tools call writes the phase report; its `## Requirements` table is the harness's (`_requirement_table`).

**Requirements.** `create_plan` splits the request into `requirements` (R1, … each with a phase); criteria name the ones they prove (`covers`, at most `MAX_COVERS`). `_coverage` maps each to its criteria and verdicts (retired ones prove nothing). The writer must cover its phase's requirements plus earlier unproven ones (`_due_requirements`). A requirement about the test suite is proven by a test command (`_suite_proofs`, `plan.suite_command`, falling back to `plan.default_test_command`), not by an expectation.

`read_file`'s trailer is deliberately load-bearing: on a file `code_nav` can parse it names the
definition a ranged read landed in, and on a whole-file read it points at `code_outline`. Measured
against the local 9B, that one hint moved `read_file` from 55% of all tool calls to 20% and the
navigation tools from 19% to 63%; see `evals/`.

"Code nav" is the tree-sitter tool set in `harness/code_nav.py` (`code_outline`, `find_symbol`,
`read_symbol`, `find_references`, `file_dependencies`), gated on `code_nav.AVAILABLE` so a missing
tree-sitter install simply removes it from every mode. It keeps two LRU caches: a small one holding
parse trees (~0.65 MB per file, so it stays small) for the tools that walk nodes, and a large one
holding only symbols and imports, which is what the project-wide scans read — that split is what
makes a repeated `find_symbol` over a large repo cheap.

`{workdir}` in role files is substituted with the actual working directory path at load time (currently only `coder.md` uses this token).
