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
| `--model` | `devstral-small-2:latest` | Model name |
| `--workdir` | `.` (current directory) | Root for all file operations |
| `--context` | `8192` | Initial context token limit |
| `--mode` | `design` | Starting mode (`design` or `coding`) |
| `--max-tool-result` | `4000` | Max chars returned by a single tool call (`0` = unlimited) |

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
│                                                       │
├──────────────────────────────────────────────────────┤
│  Tool calls pane                                      │
│  ▶ find_files("*.py","src/auth")                      │
│    → src/auth/login.py                                │
│    → src/auth/tokens.py                               │
│  ▶ read_file("src/auth/login.py")                     │
│    →    1: import hashlib                             │
│    →    2: ...                                        │
├──────────────────────────────────────────────────────┤
│  MODE: design | MODEL: llama3.1 | CTX: 12% | DIR: ./ │
├──────────────────────────────────────────────────────┤
│  > _                                                  │
└──────────────────────────────────────────────────────┘
```

- **Chat pane** — conversation history. Scroll with `↑`/`↓` or `PgUp`/`PgDn`.
- **Tool calls pane** — live tool call requests and results as the model works.
- **Status bar** — current mode, model, context usage %, and working directory.
  - CTX turns yellow at ≥ 75%, red at ≥ 90%.
- **Input bar** — 4-row area (long messages word-wrap). Press Enter to send. `↑`/`↓` navigate command history when input is focused.
- **Focus** — Press `Tab` to cycle focus between Chat, Tool Calls, and Input. The focused pane's right edge highlights (green = chat, yellow = tool calls, cyan prefix = input). `↑`/`↓` and `PgUp`/`PgDn` scroll whichever pane is focused.

## Modes

### Design mode (default)

The assistant acts as a design partner. It can explore your codebase (read-only) to understand existing code and asks clarifying questions to build a spec. It will **not** modify any files.

Available tools in design mode: `find_files`, `read_file`, `grep_file`, `grep_files`

### Coding mode

The assistant acts as an engineer. It uses the full tool suite to implement changes: reading files, making targeted edits, running commands, and working with git.

Available tools in coding mode: all design tools + `edit_file`, `create_file`, `delete_file`, `git_command`, `run_command`

Switch modes with `/design` and `/code`.

## Available Tools

| Tool | Description |
|---|---|
| `find_files` | Find files by glob pattern under a directory |
| `read_file` | Read a file, optionally a specific line range |
| `grep_file` | Regex search in a single file |
| `grep_files` | Recursive regex search across a directory |
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

Default cap: **4000 chars**. Set to `0` to disable truncation entirely.

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
