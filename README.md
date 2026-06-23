# momo-coding-harness

A local AI coding assistant that connects to a running [Ollama](https://ollama.com) instance. It runs in your terminal as a split-pane TUI and can read, edit, and manage files in your project via tool calls.

## Requirements

- Python 3.14+ (uses `/opt/homebrew/bin/python3.14` by default)
- A running Ollama instance with at least one tool-calling capable model (e.g. `qwen3.5:9b`, `qwen2.5-coder`, `mistral-nemo`)

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
| `--workspace` / `--workdir` | `.` (current directory) | Root for all file operations |
| `--context` | auto-detected | Override context token limit (default: half the model's maximum) |
| `--mode` | `design` | Starting mode (`design` or `coding`) |
| `--max-tool-result` | `0` (unlimited) | Max chars returned by a single tool call |
| `--no-think` | off | Disable model thinking/reasoning mode (on by default) |

Example targeting a specific project and remote instance:

```bash
python momo-coding-harness.py --workdir ~/projects/myapp --model qwen3-coder:30b --host http://192.168.1.10:11434
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
│  [thinking]                                           │
│    I should read login.py first...                    │
│  [assistant] I can see the auth module consists of... │
│                                                       │
├──────────────────────────────────────────────────────┤
│  MODE: design | MODEL: qwen3.5:9b | CTX: 12% | DIR: .│
├──────────────────────────────────────────────────────┤
│  > _                                                  │
└──────────────────────────────────────────────────────┘
```

- **Chat pane** — conversation history including inline tool calls (yellow), results, and thinking blocks (orange). Scroll with `↑`/`↓` or `PgUp`/`PgDn`.
- **Status bar** — current mode, model, context usage %, and working directory.
  - CTX turns yellow at ≥ 75%, red at ≥ 90%.
  - Shows `⠋ thinking` (spinner) while the model is working.
  - Shows `? waiting for input` when the model has called `ask_user` and is waiting for your reply. Type your answer and press Enter — the model resumes from where it paused.
- **Input bar** — 4-row multi-line input area. Paste or type multi-line text freely.
  - **Enter** — submit the message.
  - **Ctrl+J** — insert a newline at the cursor without submitting (works in every terminal, including macOS Terminal.app and iTerm2 with default settings).
  - **Option+Enter** — also inserts a newline on iTerm2 when Left Option Key is set to "+Esc" (Profiles → Keys), or on Terminal.app with "Use Option as Meta key" enabled.
  - **Shift+Enter** — inserts a newline on terminals with CSI-u mode enabled (iTerm2 → Profiles → Keys → "Report modifiers using CSI u").
  - Multi-line text can always be **pasted** regardless of terminal settings.
  - **↑/↓** — move the cursor between lines when the input has multiple lines; at the top or bottom edge, navigates command history.
  - **Shift+↑ / Shift+↓** — navigate command history regardless of cursor position.
  - When the model is waiting for input (after `ask_user`), the prefix changes from `›` to `?`.
- **Focus** — Press `Tab` to toggle focus between Chat and Input. The active pane border highlights green; the input `›` prefix turns green when the input pane is focused.
- **Tool call visibility** — `/toggle-tool-output` or `T` (when chat focused) switches between full tool output and abbreviated mode (first 50 chars + `…`).
- **Thinking output** — `[thinking]` blocks show the model's internal reasoning in orange/yellow. Toggle display with `/toggle-think-output` or `Shift+T` (when chat focused). Thinking content is never re-injected as context.

## Modes

### Design mode (default)

The assistant acts as a design partner. It explores your codebase to understand existing code, asks informed questions grounded in what it finds, and builds a spec. It writes the design document autonomously once it has gathered enough information — no explicit trigger required. It will **not** create or modify code files. It can pause mid-exploration to ask targeted questions via `ask_user`.

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
| `write_file` | Write content to a file. In design mode, used to save the finished spec. In coding mode, used to create or overwrite any file. |
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

## Skills

Skills are `.md` files in the `skills/` folder that get appended to the active role's system prompt. Use them to add domain-specific instructions — language conventions, coding paradigms, tool preferences, framework patterns, etc.

```
/list-skills              # show all available skills and which are active
/load-skill python        # append skills/python.md to the system prompt
/unload-skill python      # remove it
```

Active skills are saved with the session and restored on restart. Skills stack — multiple can be active at once. On a mode switch (`/code`, `/design`) the role prompt is rebuilt with all currently active skills still included.

To add a new skill, create a `.md` file in `skills/`. The filename (without extension) is the skill name used in commands.

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
| `/think` | Show thinking mode state (on/off) |
| `/think on\|off` | Enable or disable model thinking/reasoning mode |
| `/list-skills` | List available skills and show which are active |
| `/load-skill <name>` | Append a skill's instructions to the system prompt |
| `/unload-skill <name>` | Remove a skill from the system prompt |
| `/toggle-think-output` | Toggle display of model thinking/reasoning blocks |
| `/toggle-tool-output` | Toggle the tool calls pane on/off |
| `/context` | Show context limit and current token usage |
| `/context <n>` | Set the context token limit (e.g. `/context 16384`) |
| `/tool-result` | Show the current tool result character cap |
| `/tool-result <n>` | Set the cap (e.g. `/tool-result 8000`); `0` = unlimited |
| `/compact` | Manually compact the context (remove old messages) |
| `/clear` | Clear conversation history |
| `/cost` | Show token usage for this session by mode and model |
| `/session` | Show the current session file path |
| `/session <name>` | Load a saved session by name or prefix |
| `/exit` or `/quit` | Save session and exit |

## Context Management

The harness automatically detects the model's native context window on startup and uses half of it as the working limit (e.g. a 262,144-token model gets a 131,072-token limit). The startup message confirms what was detected. Use `--context N` or `/context N` to override.

Compaction fires automatically when token usage exceeds the limit:

1. **Pass 1** — removes old tool call and thinking messages (oldest first) until usage drops to 50% of the limit
2. **Pass 2** — if still over limit, removes old user/assistant message pairs (oldest first)

The system prompt is never removed. A notice is shown in the chat pane whenever compaction occurs.

## Tool Result Cap

By default tool results are passed to the model without truncation. Set a cap to prevent a single large result from consuming the context window:

```
/tool-result          # show current cap (0 = unlimited)
/tool-result 8000     # cap results at 8000 chars
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
