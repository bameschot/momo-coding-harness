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
- All other modes (writing, coding, chat, momo): 100 iterations maximum.

On hitting the limit an `ErrorEvent` is emitted and the loop exits.

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

Some models emit tool calls as plain text rather than using the API's structured tool-call field. `_extract_text_tool_calls` applies successive fallbacks before giving up (each later tier only runs when the earlier ones found nothing):

1. **Tagged / structured formats**: Recognises model-specific wrappers — Qwen3/Hermes `<tool_call>`, Functionary `<functioncall>` / `<function_call>`, Phi `<|tool_call|>`, DeepSeek `<｜tool▁call▁begin｜>`, Mistral `[TOOL_CALLS]` array, Command-R `Action:/Action Input:`.
2. **Nameless `<tool_call>` payloads**: some models (notably gemma) put the argument object directly inside the tag with no `{"name":…,"arguments":…}` wrapper. A payload carrying a `content` key is attributed to `write_file`; `raw_decode` reads the full object so braces inside `content` do not truncate parsing.
3. **Python-call syntax**: gemma-family models often emit calls as Python source — `edit_file(path='a.py', old_string='x', new_string='y')` or positionally `edit_file("a.py", "x", "y")`, sometimes wrapped in `print(...)`. `_match_paren` carves out the balanced `name(...)` (skipping quoted strings), then `ast` parses it and `ast.literal_eval` recovers each argument. Positional args are mapped to the tool's declared parameter order; keyword args override. Bare mentions in prose are rejected because their values are not literals.
4. **Bare JSON anchored to tool names**: pattern built dynamically from the current tool set; scans for `{"name": "<known_tool>", "arguments": {…}}` or `known_tool({…})` anywhere in the response.

If a text tool call is recovered it is executed normally; if every tier fails the text is treated as a regular assistant response.

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

Sessions are auto-saved at the end of every `send()` call. On startup the most recent session (by filename, sorted newest-first) is restored unless `--fresh` is passed. The TUI replays the message history into the chat buffer at startup via `_replay_session()`.

`/session <name>` resolves sessions by exact filename stem first, then partial prefix — so `2026-06-22` matches the first session whose stem starts with that string.

---

## Roles and Skills

The system prompt is built from `roles/<mode>.md` plus any active skill files from `skills/`. Skills are appended in load order separated by `---`. Switching mode or loading/unloading a skill rebuilds `messages[0]` in place. Active skills are persisted with the session.

### Modes

| Mode | Role file | Tools | Purpose |
|------|-----------|-------|---------|
| `design` | `roles/designer.md` | read-only + `write_file` + `ask_user` | Interview-based design partner; explores codebase, writes specs |
| `writing` | `roles/writer.md` | design tools + `append_to_file` + `edit_file` | Document editor; targeted edits, matches existing voice |
| `coding` | `roles/coder.md` | all tools | Engineer; full read/write/exec/git access |
| `chat` | `roles/chat.md` | read-only + `ask_user` | Conversational Q&A over code and documents; never writes files |
| `momo` | `roles/momo.md` | all tools | Cat companion; full tool access with a warm, curious persona |

`{workdir}` in role files is substituted with the actual working directory path at load time (currently only `coder.md` uses this token).
