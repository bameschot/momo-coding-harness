# Technical Design

## Overview

momo-coding-harness is a single-process Python application. The TUI runs on the main thread using Python's `curses` module. All Ollama API calls and tool execution happen on a background daemon thread. The two threads communicate exclusively via a `queue.Queue` of typed event objects — the background thread never touches curses directly.

## Module Structure

```
harness/
├── main.py          # Entry point
├── tui.py           # Curses TUI
├── harness.py       # Core conversation loop + event types
├── ollama_client.py # Ollama API wrapper
├── tools.py         # Tool schemas and executors
├── commands.py      # Slash command dispatch
├── session.py       # Session persistence
└── logger.py        # Per-session NDJSON log
```

---

## Threading Model

```
Main thread (curses)                Background thread (Harness.send)
─────────────────────               ────────────────────────────────
TUI.run()                           Harness.send(text)
  getch() with halfdelay(1)  ←──     event_queue.put(ChatEvent(...))
  _drain_events()             ←──     event_queue.put(ToolCallEvent(...))
  _redraw()                   ←──     event_queue.put(ToolResultEvent(...))
                              ←──     event_queue.put(StatusEvent(...))
```

`curses.halfdelay(1)` makes `getch()` return `curses.ERR` after 100 ms if no key is pressed, which lets the main loop drain the event queue and refresh the display while the model is thinking.

The `Harness` instance is created before `curses.wrapper` is called, so both threads share the same object. No locks are needed: the background thread only writes to `messages` and `event_queue`; the main thread only reads `event_queue`. The `_lock` on `Harness` is reserved for future use.

---

## harness.py

### Event types

Defined as `@dataclass` classes at module level so `tui.py` can `isinstance`-check them:

| Class | Fields | Consumer |
|---|---|---|
| `ChatEvent` | `role, text` | ChatPane |
| `ToolCallEvent` | `name, args` | ToolPane |
| `ToolResultEvent` | `name, result` | ToolPane |
| `StatusEvent` | `mode, model, workdir, ctx_pct, ctx_color` | StatusBar |
| `ErrorEvent` | `text` | ChatPane |
| `DoneEvent` | _(no fields)_ | Clears `_busy` in TUI |

### Conversation loop (`Harness.send`)

```
append user message
loop:
    check context → auto-compact if over limit
    log_request
    client.chat(messages, tools)   ← blocks until Ollama responds
    log_response
    emit ChatEvent for text content
    if no tool_calls → append assistant message, break
    append assistant message with tool_calls field
    for each tool_call:
        emit ToolCallEvent
        dispatch(name, args, workdir) → result string
        if len(result) > max_tool_result (and max_tool_result > 0):
            truncate result + append notice
        emit ToolResultEvent
        append tool message
    (loop continues, calls Ollama again with tool results)
    if iteration >= 20: emit ErrorEvent, emit DoneEvent, return
emit StatusEvent
autosave session
emit DoneEvent
```

The loop terminates when Ollama returns a response with no `tool_calls`, or after a hard limit of **20 iterations** to guard against infinite tool-call cycles. `DoneEvent` is always emitted on exit (including the error path) so the TUI can clear its `_busy` flag.

### System prompts

The system prompt is always `messages[0]`. Switching modes replaces it in-place; the rest of the conversation history is preserved. The coding prompt embeds the current `workdir` path so the model knows where it is operating.

### Token estimation

After each Ollama response, `response.usage.prompt_eval_count + eval_count` is used when available. Between responses (and for the compaction decision), a character-based heuristic is used: `sum(len(msg["content"]) // 4 for msg in messages)`. This is intentionally approximate — roughly 4 characters per token for English/code.

### Context compaction

Triggered when `_token_estimate > context_limit`, either automatically before a chat call or manually via `/compact`.

**Pass 1** (tool message removal): Walks `messages` from index 1 (never touches `messages[0]`, the system prompt). When a `role: "tool"` message is found, it and the preceding `role: "assistant"` message (which contained the `tool_calls` that triggered it) are deleted together. This keeps the conversation structurally valid. Stops when estimate drops below `context_limit // 2`.

**Pass 2** (old turn removal): If still over target after pass 1, walks from index 1 again removing `role: "user"` messages and their following `role: "assistant"` reply. Stops when estimate drops below `context_limit // 2`.

After compaction, a `ChatEvent("system", ...)` notice is emitted and a `compact` record is written to the log.

---

## tui.py

### Layout

`_compute_layout(rows, cols)` derives pixel-free window geometry:

```
chat_h  = max(4, int(rows * 0.60))
tool_h  = max(3, rows - chat_h - 2)   # 1 status row + 1 input row
status  = chat_h + tool_h             # row index
input   = chat_h + tool_h + 1         # row index
```

Four `curses.newwin` objects are created: `_chat_win`, `_tool_win`, `_status_win`, `_input_win`. On `KEY_RESIZE` all four are rebuilt and the screen is redrawn from the in-memory `_LineBuffer` objects.

### `_LineBuffer`

Stores rendered display lines as `(text: str, color_pair: int)` tuples. Maintains a `_scroll` offset (0-based index of the last visible line). `render(win, height, width)` slices `_lines[scroll+1-height : scroll+1]` and writes them with `addnstr`. Auto-scrolls to the bottom whenever a line is appended.

Scroll keys in the main loop manipulate `_chat_buf._scroll` directly; `_tool_buf` scrolls are not bound to keys (the tool pane auto-scrolls to show the latest).

### Color pairs

| Pair ID | Foreground | Use |
|---|---|---|
| 1 (`_C_USER`) | Cyan | `[user]` prefix |
| 2 (`_C_ASSISTANT`) | Green | `[assistant]` prefix |
| 3 (`_C_SYSTEM`) | Magenta | `[system]` notices |
| 4 (`_C_TOOL_NAME`) | Yellow | Tool call lines (`▶ name(args)`) |
| 5 (`_C_TOOL_RES`) | Default | Tool result lines (`→ ...`) |
| 6 (`_C_STATUS`) | Black on White | Status bar (normal) |
| 7 (`_C_WARN`) | Yellow | Status bar CTX ≥ 75% |
| 8 (`_C_DANGER`) | Red | Status bar CTX ≥ 90% |

`curses.use_default_colors()` is called so `-1` means the terminal's default background, preserving transparency.

### Input handling

The input area is 4 rows tall. State is `_input_lines: list[str]` (one entry per logical line). Characters in printable ASCII (32–126) are appended to the last element. Backspace removes the last character of the last element, or pops the element if it is empty and there is more than one. Shift+Enter appends a new empty element (new line). Enter calls `_submit()`.

`_submit()` joins `_input_lines` with `\n`, strips the result, resets the buffer, and saves the text to `_history` (deduplicates consecutive identical submissions). Slash commands are handled synchronously on the main thread. Regular messages are dispatched to a daemon thread via `harness.send(text)`. The `_busy` flag prevents double-submit.

**Command history** (`_history: list[str]`) is in-memory for the session. `↑`/`↓` navigate it. On first `↑` the current buffer is stashed in `_history_stash`; `↓` past the newest entry restores the stash. `_history_idx = -1` means "not browsing."

**Chat scroll** uses `PgUp`/`PgDn`. `↑`/`↓` are reserved for history navigation when the input pane is focused, or scroll the focused pane when chat/tool is focused (cycled with `Tab`).

**Shift+Tab** toggles between design and coding mode without using a slash command.

---

## tools.py

### Schema format

Each tool is an OpenAI-compatible function schema dict:

```python
{
    "type": "function",
    "function": {
        "name": str,
        "description": str,
        "parameters": {
            "type": "object",
            "properties": { ... },
            "required": [ ... ],
        },
    },
}
```

`READ_ONLY_TOOLS` and `CODING_ONLY_TOOLS` are separate lists. `ALL_TOOLS = READ_ONLY_TOOLS + CODING_ONLY_TOOLS`. `Harness.send` selects which list to pass based on the current mode.

### Path safety

`_safe_path(raw, workdir)` resolves the raw path against `workdir` and calls `Path.relative_to(workdir.resolve())`. If the resolved path escapes the working directory, it returns the error string `"ERROR: path outside working directory"` rather than raising, so the model receives the error as a tool result and can respond accordingly.

### Executors

Each executor function takes keyword-only `workdir: Path` in addition to its declared tool parameters. `dispatch(name, args, workdir)` looks up the function in `_EXECUTORS` and calls it via `**args, workdir=workdir`.

Tool results are always strings. Errors are returned as `"ERROR: ..."` strings — never raised as exceptions — so the model can read them and decide how to proceed.

**Read-only tools:**

| Tool | Notable behaviour |
|---|---|
| `list_directory` | `os.scandir` result sorted dirs-first then files alphabetically; hidden entries (`.`) skipped unless `show_hidden=True` |
| `file_info` | Uses `Path.lstat()` to handle symlinks; attempts UTF-8 read for line count, reports `(binary)` on decode error |
| `find_files` | Uses `Path.glob()` for standard shell glob semantics. Simple patterns (no `/`, no `**`) are auto-prefixed with `**/` to stay recursive. Path patterns (`src/*.py`) are left-anchored to `directory`, so they match only within that exact subdirectory — not any deeper directory with the same name. Skip-dirs filtered from results by checking path components. |
| `read_file` | Lines prefixed with 1-based numbers (`   1: content`) |
| `grep_file` | Returns matching lines with numbers; validates regex before scanning |
| `grep_files` | Skips unreadable/binary files silently via `OSError` catch |

**Coding-only tools:**

| Tool | Notable behaviour |
|---|---|
| `move_file` | `_safe_path` on both src and dst; `dst.parent.mkdir(parents=True)` before `src.rename(dst)` |
| `append_to_file` | Opens in `"a"` mode; creates file and parent dirs if missing |
| `replace_all_in_file` | Returns error if `old_string` not found; replaces all occurrences via `str.replace`; reports count |
| `edit_file` | Counts occurrences before replacing; errors if count ≠ 1 (exact-once guarantee) |
| `create_file` | `parent.mkdir(parents=True, exist_ok=True)` before write; overwrites silently |
| `delete_file` | `Path.unlink()`; errors if not found |
| `git_command` | `shlex.split` for arg parsing; 30 s timeout; stdout + stderr combined |
| `run_command` | `shell=True`; captures stdout and stderr separately, appends `[exit code: N]` if non-zero |

`_SKIP_DIRS` = `{".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", "dist", "build", ".mypy_cache", ".pytest_cache"}` — applied in all three directory-traversal tools, but via different mechanisms:
- `grep_files` and `list_directory`: `os.walk` with in-place `dirnames[:] = [...]` pruning (prevents descent)
- `find_files`: post-filter on `Path.glob()` results — checks `any(part in _SKIP_DIRS for part in p.relative_to(root).parts)`

### Tool result cap

After `dispatch()` returns, `Harness.send` checks `len(result) > harness.max_tool_result` (when `max_tool_result > 0`). Oversized results are truncated to `max_tool_result` chars and a suffix `\n... (truncated, N chars total)` is appended so the model knows more content exists.

`max_tool_result` defaults to **4000**, is set at startup via `--max-tool-result`, and can be changed at runtime with `/tool-result <n>`. Setting it to `0` disables truncation.

---

## ollama_client.py

Wraps `ollama.Client(host=host)`. The `chat` method passes `tools=tools` only when the list is non-empty (some older Ollama versions reject an empty `tools` field). The library returns a response object; `Harness.send` accesses it via attribute access (`response.message`, `response.usage`).

`list_models()` calls `client.list()` and extracts `.model` from each `Model` object in `.models`. Returns a sorted list of strings.

`set_host(url)` reinitialises `self._client = ollama.Client(host=url)`, which is the mechanism behind `/host <url>`.

---

## session.py

Sessions are stored in `~/.momo-harness/sessions/` as `<ISO-timestamp>.json`. The timestamp format is `%Y-%m-%dT%H-%M-%S` (colons replaced with hyphens for filesystem compatibility).

Schema:

```json
{
  "created_at": "2026-06-22T10-30-00",
  "model": "llama3.1",
  "mode": "coding",
  "workdir": "/Users/foo/project",
  "context_limit": 8192,
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user",   "content": "..."},
    ...
  ]
}
```

`autosave` rewrites the whole file after each assistant turn (sessions are small enough that incremental writes are not necessary).

`find_session(name)` first tries exact stem/filename match, then falls back to prefix match on the stem. This lets `/session 2026-06` load the most recent June session.

---

## logger.py

Writes newline-delimited JSON (NDJSON) to `~/.momo-harness/sessions/<timestamp>.log`. The file handle is opened in append mode and kept open for the session lifetime. Each `_write` call flushes immediately so the log is readable mid-session.

Record types:

| `type` | Key fields |
|---|---|
| `request` | `mode, model, message_count, prompt_tokens` |
| `response` | `mode, model, prompt_tokens, eval_tokens, total_tokens, has_tool_calls` |
| `tool_call` | `mode, tool, args` |
| `tool_result` | `mode, tool, result_length` |
| `compact` | `mode, removed_messages, tokens_before, tokens_after` |

Token fields are `null` when the model does not report usage.

---

## commands.py

`handle(line, harness)` parses the slash command and returns a `CommandResult(handled, output, exit_app)`. Commands that need to communicate with the TUI (e.g. listing models) return their output as a string; `tui.py` renders it as a `[system]` chat message.

Commands that change harness state (`/code`, `/design`, `/workdir`, `/model`, `/host`, `/context`, `/tool-result`) call the relevant `Harness` methods which in turn call `_emit_status()` so the status bar updates immediately on the main thread via the event queue. `/tool-result` sets `harness.max_tool_result` directly; no status event is needed since the cap is not displayed in the status bar.

`/exit` and `/quit` call `_autosave()` and `logger.close()` before returning `exit_app=True`. The TUI catches `SystemExit(0)` from `_submit()`.

---

## Data Flow Summary

```
User types text → TUI._submit()
    → text starts with "/" → commands.handle() → CommandResult
                                                 output rendered in ChatPane
    → plain text → threading.Thread(harness.send, text)
                       → append to messages
                       → [auto-compact if needed]
                       → ollama_client.chat()
                           → Ollama HTTP API
                       ← response
                       → if tool_calls:
                           → tools.dispatch() → filesystem / git / shell
                           → append tool result to messages
                           → ollama_client.chat() [loop]
                       → emit events to event_queue
                       → session.save()
                       → logger.write()
TUI main loop → _drain_events() → render ChatPane / ToolPane / StatusBar
```
