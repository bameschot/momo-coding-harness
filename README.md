# momo-coding-harness

A local AI coding assistant that connects to a running [Ollama](https://ollama.com) instance. It runs in your terminal as a split-pane TUI and can read, edit, and manage files in your project via tool calls.

## Requirements

- Python 3.14+ (uses `/opt/homebrew/bin/python3.14` by default)
- A running Ollama instance with at least one tool-calling capable model (e.g. `llama3.1`, `qwen2.5-coder`, `mistral-nemo`)

## Setup

```bash
/opt/homebrew/bin/python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running

```bash
python momo-coding-harness.py
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--host` | `http://localhost:11434` | Ollama base URL |
| `--model` | `qwen3.5:9b` | Model name |
| `--workdir` | `.` (current directory) | Root for all file operations |
| `--context` | `100000` | Initial context token limit |
| `--mode` | `design` | Starting mode (`design` or `coding`) |
| `--max-tool-result` | `20000` | Max chars returned by a single tool call (`0` = unlimited) |

Example targeting a specific project and remote instance:

```bash
python momo-coding-harness.py --workdir ~/projects/myapp --model qwen3-coder:30b --host http://192.168.1.10:11434 --max-tool-result 0
```

## TUI Layout

```
┌──────────────────────────────────────────────────────┐
│  Chat pane                                            │
│  [user] describe what the auth module does            │
│  [assistant] The auth module handles...               │
│  ▶ find_files({"pattern":"*.py","directory":"src"})   │
│    → src/auth/login.py                                │
│    → src/auth/tokens.py                               │
│  [assistant] I can see the auth module consists of... │
│                                                       │
├──────────────────────────────────────────────────────┤
│  MODE: design | MODEL: qwen3.5:9b | CTX: 12% | DIR: .│
├──────────────────────────────────────────────────────┤
│  > _                                                  │
└──────────────────────────────────────────────────────┘
```

- **Chat pane** — conversation history including inline tool calls (yellow) and results. Scroll with `↑`/`↓` or `PgUp`/`PgDn`.
- **Status bar** — current mode, model, context usage %, and working directory.
  - CTX turns yellow at ≥ 75%, red at ≥ 90%.
  - Shows `⠋ thinking` (black-on-yellow spinner) while the model is working.
  - Shows `? waiting for input` (yellow) when the model has called `ask_user` and is waiting for your reply. Type your answer and press Enter — the model resumes from where it paused.
- **Input bar** — 4-row area (long messages word-wrap). Press Enter to send. `↑`/`↓` navigate command history when input is focused. When the model is waiting for input, the prefix changes from `›` to `?`.
- **Focus** — Press `Tab` to toggle focus between Chat and Input. The chat pane's right edge highlights green when focused; the input shows a cyan `›` prefix when focused.
- **Tool call visibility** — `/toggle-tool-output` switches between full tool output (call + result) and abbreviated mode (first 50 chars + `…`, no result).

## Modes

### Design mode (default)

The assistant acts as a design partner. It explores your codebase to understand existing code, asks informed questions grounded in what it finds, and builds a spec. When you explicitly ask it to write or save the design (e.g. "write design", "save spec"), it writes a Markdown document. It will **not** create or modify code files. It can pause mid-exploration to ask targeted questions via `ask_user`.

Available tools in design mode: `list_directory`, `file_info`, `find_files`, `read_file`, `grep_file`, `grep_files`, `write_file`, `ask_user`

### Coding mode

The assistant acts as an engineer. It uses the full tool suite to implement changes: reading files, making targeted edits, running commands, and working with git. It can pause mid-task to ask the user for clarification before proceeding with ambiguous or destructive changes via `ask_user`.

Available tools in coding mode: all design tools + `edit_file`, `create_file`, `delete_file`, `git_command`, `run_command`

Switch modes with `/design` and `/code`.

## Available Tools

### Read-only (available in both modes)

| Tool | Description |
|---|---|
| `list_directory` | List the contents of a directory — dirs first, then files with sizes |
| `file_info` | File/directory metadata: exists, type, size, mtime, line count |
| `find_files` | Find files using standard shell glob patterns (`*.py`, `src/**/*.ts`). Simple patterns (no `/`) are recursive automatically; path patterns are anchored to the search directory. |
| `read_file` | Read a file, optionally a specific line range |
| `grep_file` | Regex search in a single file |
| `grep_files` | Recursive regex search across a directory |

### Shared (available in both modes)

| Tool | Description |
|---|---|
| `write_file` | Write content to a `.md` file. Only invoked when the user explicitly asks to write or save the design (e.g. "write design", "save spec"). |
| `ask_user` | Pause mid-task and ask the user a focused clarifying question. The harness blocks the model and waits for your reply before continuing. |

### Coding mode only

| Tool | Description |
|---|---|
| `move_file` | Move or rename a file (parent dirs created automatically) |
| `append_to_file` | Append text to a file (creates if missing) |
| `replace_all_in_file` | Replace every occurrence of a string in a file; returns count |
| `edit_file` | Replace an exact string in a file (must match exactly once) |
| `create_file` | Create or overwrite a file |
| `delete_file` | Delete a file |
| `git_command` | Run a git command (e.g. `status`, `diff`, `add src/foo.py`) |
| `run_command` | Run any shell command — scripts, tests, build tools, etc. |

All file operations are sandboxed to the working directory. Paths that attempt to escape via `..` are rejected.

## Slash Commands

Type any command in the input bar:

| Command | Description |
|---|---|
| `/help` | Show all available commands |
| `/code` | Switch to coding mode |
| `/design` | Switch to design mode |
| `/model` | List available Ollama models |
| `/model <name>` | Switch to a different model |
| `/host` | Show current Ollama host URL |
| `/host <url>` | Connect to a different Ollama instance at runtime |
| `/workdir` | Show the current working directory |
| `/workdir <path>` | Change the working directory |
| `/context` | Show context limit and current token usage |
| `/context <n>` | Set the context token limit (e.g. `/context 16384`) |
| `/tool-result` | Show the current tool result character cap |
| `/tool-result <n>` | Set the cap (e.g. `/tool-result 8000`); `0` = unlimited |
| `/compact` | Manually compact the context (remove old messages) |
| `/clear` | Clear conversation history |
| `/session` | Show the current session file path |
| `/session <name>` | Load a saved session by name or prefix |
| `/exit` or `/quit` | Save session and exit |

## Context Management

The harness tracks token usage and compacts the context automatically when it exceeds the limit (default: 8192 tokens).

Compaction strategy:
1. **Pass 1** — removes old tool call messages (oldest first) until usage drops to 50% of the limit
2. **Pass 2** — if still over limit, removes old user/assistant message pairs (oldest first)

The system prompt is never removed. A notice is shown in the chat pane whenever compaction occurs.

Use `/context <n>` to raise the limit for models with larger context windows.

## Tool Result Cap

Each tool call result is capped at a maximum number of characters before being added to the conversation context. This prevents a single large result (e.g. a directory listing or long file) from consuming the entire context window.

Default cap: **20000 chars**. Set to `0` to disable truncation entirely.

```
/tool-result          # show current cap
/tool-result 8000     # raise cap to 8000 chars
/tool-result 0        # disable cap (unlimited)
```

Truncated results display a notice: `... (truncated, N chars total)` so the model knows more content exists and can request a narrower read (e.g. with `start_line`/`end_line` on `read_file`).

## Sessions

Each session is automatically saved after every assistant response to:

```
~/.momo-harness/sessions/<timestamp>.json
```

A log file (`.log`) is written alongside the JSON, recording every request, response, tool call, and token count in newline-delimited JSON format. Use it to audit what the model did or analyse token usage.

To resume a previous session:

```bash
/session 2026-06-22   # partial prefix match works
```
