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
       ├─ filter thinking messages out of API call
       ├─ call ollama_client.chat(messages, tools)
       ├─ extract thinking from msg.thinking field or <think>…</think> tags
       │    └─ stored as role:"thinking", emitted as ThinkEvent, never re-sent to API
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

Tool results are appended as `{"role": "tool", ...}` messages. Thinking content (`role: "thinking"`) is appended but filtered out before every API call — it is display-only and never re-injected as context.

### Iteration limits

- Design mode: 40 iterations maximum.
- All other modes (writing, data, coding, chat): 100 iterations maximum.

On hitting the limit an `ErrorEvent` is emitted and the loop exits.

### Nudge logic

When the model makes 10 or more consecutive tool-only turns (assistant + tool results, no text response), a user-role message is injected asking it to stop and summarise its findings. This fires at most once per `send()` call. Design mode receives a different nudge from other modes.

### Write-intent recovery

If the assistant response contains phrases like "let me write", "i will write", "write the complete", etc., but no `write_file` / `create_file` tool call was made, a targeted recovery nudge is injected — "You said you would write X — please call write_file now." One recovery nudge per `send()`.

### Empty response handling

If the model returns no text and no tool calls:
1. **First retry** — inject an explicit prompt to respond. If `done_reason == "length"` (context cutoff), suppress thinking for the next turn to avoid XML malformation. If only thinking was returned (no output), also suppress thinking on retry.
2. **Second empty in a row** — emit `ErrorEvent("No response from model")`, pop the injected retry message, and exit.

### Thinking suppression (`_suppress_think_next`)

Thinking is disabled for one turn after: (a) the context window was cut off (`done_reason == "length"`), or (b) the model produced only thinking with no output. This prevents Qwen3's `<think>…</think>` XML template from appearing in the next turn's content.

### Text tool-call recovery

Some models emit tool calls as plain text rather than using the API's structured tool-call field. The harness applies a two-pass fallback before giving up:

1. **Tagged / structured formats**: Recognises model-specific wrappers — Qwen3/Hermes `<tool_call>`, Functionary `<functioncall>` / `<function_call>`, Phi `<|tool_call|>`, DeepSeek `<｜tool▁call▁begin｜>`, Mistral `[TOOL_CALLS]` array, Command-R `Action:/Action Input:`.
2. **Bare JSON anchored to tool names**: Pattern built dynamically from the current tool set; scans for `{"name": "<known_tool>", "arguments": {…}}` or `known_tool({…})` anywhere in the response. Only runs if pass 1 found nothing.

If a text tool call is recovered it is executed normally; if both passes fail the text is treated as a regular assistant response.

### Tool content sanitization

For `write_file`, `create_file`, and `append_to_file` tool results stored in the message history, the `content` field of the *assistant* message (which contains the file content that was written) is replaced with a short placeholder `[written to <path>]`. This prevents large file content from accumulating in the context and avoids Qwen3 template corruption on subsequent turns.

### `ask_user` blocking

When the model calls the `ask_user` tool, the worker thread blocks on `_user_input_queue.get()` and emits an `AskUserEvent`. The TUI sets `_waiting_for_input = True`, updates the status bar to `? waiting for input`, and routes the next submitted line back to the harness via `harness.provide_user_input()`. The worker thread then unblocks and continues the agentic loop with the answer injected as a tool result.

---

## Qwen3-Specific Handling

Qwen3 uses an XML chat template that wraps tool results in `<tool_response>…</tool_response>`. Special handling applied when a Qwen model is active:

- **Tool result escaping** — `&`, `<`, `>` in tool result content are HTML-escaped before the API call (unescaped content is stored in messages; escaping happens only in the API payload copy).
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
│  mode | model | ctx% | workdir | spinner when busy     │
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

Toggle with `/toggle-markdown` or `Shift+M`. On by default.

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

After both passes, removed messages are formatted and passed to the model in a one-shot call (no tools). The resulting summary is prepended to the oldest remaining `user` message as `[Earlier context summary:\n...\n]`. If the call fails the summary is silently skipped.

### Manual compaction

- `/compact` — runs with LLM summarisation; non-blocking (worker thread, spinner shown).
- `/fast-compact` — skips summarisation; instant.

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

Session fields: `created_at`, `model`, `mode`, `workdir`, `context_limit`, `active_skills`, `input_history`, `messages`.

Sessions are auto-saved at the end of every `send()` call. On startup the most recent session (by filename, sorted newest-first) is restored unless `--fresh` is passed. The TUI replays the message history into the chat buffer at startup via `_replay_session()`.

`/session <name>` resolves sessions by exact filename stem first, then partial prefix — so `2026-06-22` matches the first session whose stem starts with that string.

---

## Roles and Skills

The system prompt is built from `roles/<mode>.md` plus any active skill files from `skills/`. Skills are appended in load order separated by `---`. Switching mode or loading/unloading a skill rebuilds `messages[0]` in place. Active skills are persisted with the session.

### Modes

| Mode | Role file | Tools | Purpose |
|------|-----------|-------|---------|
| `design` | `roles/designer.md` | read-only + `write_file` + `ask_user` | Interview-based design partner; explores codebase, writes specs |
| `writing` | `roles/writer.md` | design tools + `append_to_file` + `replace_all_in_file` | Document editor; targeted edits, matches existing voice |
| `data` | `roles/data.md` | design tools + `run_command` | Data analyst; inspects, processes, reports |
| `coding` | `roles/coder.md` | all tools | Engineer; full read/write/exec/git access |
| `chat` | `roles/chat.md` | read-only + `ask_user` | Conversational Q&A over code and documents; never writes files |

`{workdir}` in role files is substituted with the actual working directory path at load time (currently only `coder.md` and `data.md` use this token).
