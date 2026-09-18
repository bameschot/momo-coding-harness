# momo-coding-harness

A local AI coding assistant that connects to a running [Ollama](https://ollama.com) or [llama.cpp](https://github.com/ggml-org/llama.cpp) server. It runs in your terminal as a split-pane TUI and can read, edit, and manage files in your project via tool calls.

## Requirements

- Python 3.14+ (uses `/opt/homebrew/bin/python3.14` by default)
- One of:
  - A running **Ollama** instance with at least one tool-calling capable model (e.g. `qwen3.5:9b`, `qwen2.5-coder`, `mistral-nemo`), or
  - A running **llama.cpp** server started with `--jinja` (required for tool calling), e.g. `llama-server -m model.gguf -c 8192 --jinja --port 8080`. Add `--reasoning-format ...` if you want the model's reasoning surfaced as thinking output.

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
| `--provider` | last-used or `ollama` | LLM backend: `ollama` or `llamacpp` |
| `--host` | `11434` (ollama) / `8080` (llamacpp) | Backend base URL |
| `--model` | `qwen3.5:9b` | Model name (llama.cpp serves whatever model it was launched with) |
| `--workspace` / `--workdir` | `.` (current directory) | Root for all file operations |
| `--context` | auto-detected | Override context token limit (default: half the model's maximum) |
| `--mode` | `design` | Starting mode (`design`, `chat`, `plan`, `coding`, or `momo`) |
| `--max-tool-result` | `0` (unlimited) | Max chars returned by a single tool call |
| `--no-think` | off | Disable model thinking/reasoning mode (on by default) |

The chosen provider is saved and reused on the next launch (and restored per session). The status bar shows the active backend as `VIA: <provider>`.

Example targeting a specific project and remote Ollama instance:

```bash
python momo-coding-harness.py --workdir ~/projects/myapp --model qwen3-coder:30b --host http://192.168.1.10:11434
```

Example targeting a local llama.cpp server:

```bash
python momo-coding-harness.py --provider llamacpp --host http://localhost:8080 --workdir ~/projects/myapp
```

## Connecting to a remote Ollama host

If your Ollama instance is hosted remotely and protected by an API key, set the host and token once the harness is running:

```
/host https://my-remote-ollama.example.com
/token sk-your-api-key-here
```

The token is sent as a `Authorization: Bearer <token>` header on every request.

**Security notes:**

- The token is **never written to disk** — it is not saved to the session JSON, the log file, or the input history. It disappears when the harness exits.
- When you type `/token <key>`, the key is masked in the chat display immediately: only the first 2 and last 3 characters are shown (e.g. `sk***ere`). The raw value is never displayed or echoed.
- The **host** *is* saved in the session, so it is restored automatically when the harness restarts and reloads your last session. Only the token must be re-entered (because it is never persisted); add a shell alias or a startup `/token` step if you connect to the same authenticated remote frequently.
- `/token` with no argument shows the current masked token (or "not set").
- `/clear-token` removes the token for the current session.

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
│  MODE: design | VIA: ollama | MODEL: qwen3.5:9b | HOST: localhost:11434 | CTX: 12% | DIR: .│
├──────────────────────────────────────────────────────┤
│  > _                                                  │
└──────────────────────────────────────────────────────┘
```

- **Chat pane** — conversation history including inline tool calls (yellow), results, and thinking blocks (orange). Scroll with `↑`/`↓` or `PgUp`/`PgDn`.
- **Status bar** — current mode, model, Ollama host, context usage %, and working directory. When the line is too narrow to fit, the working directory is shortened from the front (`…/tail`) so its most specific part stays visible.
  - CTX turns yellow at ≥ 75%, red at ≥ 90%.
  - Shows `⠋ thinking` (spinner) while the model is working.
  - Shows `? waiting for input` when the model has called `ask_user` and is waiting for your reply. Type your answer and press Enter — the model resumes from where it paused.
  - Top rule turns green when the chat pane has focus; bottom rule turns green when the input pane has focus.
- **Input bar** — 5-row multi-line input area. Paste or type multi-line text freely.
  - **Enter** — submit the message.
  - **Ctrl+J** — insert a newline at the cursor without submitting (works in every terminal).
  - **Option+Enter** — also inserts a newline on iTerm2 when Left Option Key is set to "+Esc" (Profiles → Keys), or on Terminal.app with "Use Option as Meta key" enabled.
  - **Shift+Enter** — inserts a newline on terminals with CSI-u mode enabled (iTerm2 → Profiles → Keys → "Report modifiers using CSI u").
  - Multi-line text can always be **pasted** regardless of terminal settings.
  - **↑/↓** — move the cursor between lines in multi-line input; at the top or bottom edge, navigates command history.
  - **Shift+↑ / Shift+↓** — navigate command history regardless of cursor position.
  - **Ctrl+Left / Ctrl+Right** — move cursor one word left or right (requires xterm-compatible terminal; also available as **Option+b / Option+f** on macOS with "+Esc" Option key).
  - **Ctrl+A / Home** — move cursor to start of the current line.
  - **Ctrl+E / End** — move cursor to end of the current line.
  - **Ctrl+K** — delete from cursor to end of the current line (kills the newline itself when the cursor is right before one).
  - **Ctrl+U** — delete from start of the current line to the cursor.
  - When the model is waiting for input (after `ask_user`), the prefix changes from `›` to `?`.
- **Chat pane scrolling** — `↑`/`↓`, `PgUp`/`PgDn`. When a table or other wide content is present, `←`/`→` scrolls horizontally (chat focus required).
- **Focus** — Press `Tab` to toggle focus between Chat and Input. The active pane border highlights green.
- **Tool call visibility** — `/tool-output on|off` switches between full tool output and abbreviated mode (first 50 chars + `…`).
- **Thinking output** — `[thinking]` blocks show the model's internal reasoning in orange/yellow. Toggle display with `/think-output on|off` or `Shift+T` (when chat focused). Thinking content is never re-injected as context.
- **Markdown rendering** — assistant responses are rendered as formatted markdown by default. Headings use box-drawing decorations, lists use `•`/numbered prefixes, code blocks are prefixed with `│`, tables render with full box-drawing characters. Toggle with `/markdown on|off` or `Shift+M` (when chat focused). When a table is wider than the terminal, a horizontal scrollbar appears at the bottom of the chat pane; scroll it with `←`/`→` while the chat pane is focused.
- **Edit diffs** — whenever the model changes a file (`edit_file`, `append_to_file`, `write_file`, `delete_file`, `move_file`), the chat pane shows a colored diff of exactly what changed on disk instead of a terse `OK` line. Added lines are green, removed lines red, hunk headers cyan. Each line has a two-column line-number gutter (old | new): context lines show both numbers, removed lines only the old, added lines only the new — so every change is anchored to its position in the file. `write_file` to a new path shows as a new file, `delete_file` shows every line removed, and `move_file` shows a rename notice. Shown by default; toggle with `/diff on|off` or `Shift+D` (when chat focused). Choose the presentation with `/diff-style compact` (default — a `± path (+N -M)` header with hunks) or `/diff-style git` (full `git diff` layout with `diff --git`/`---`/`+++` headers). Diffs are display-only and reconstructed from disk at edit time, so a reloaded session shows the plain tool result rather than the diff.

## Modes

Press `Shift+Tab` to cycle through modes: **design → chat → plan → coding → momo → design**.

### Design mode (default)

The assistant acts as a design partner. It explores your codebase to understand existing code, asks informed questions grounded in what it finds, and builds a spec. It writes the design document autonomously once it has gathered enough information — no explicit trigger required. It will **not** create or modify code files.

Available tools: `list_directory`, `file_info`, `find_files`, `read_file`, `grep_file`, `grep_files`, `write_file`, `ask_user`

### Coding mode

The assistant acts as an engineer. It uses the full tool suite to implement changes: reading files, making targeted edits, running commands, and working with git.

Available tools: all design tools + `edit_file`, `delete_file`, `move_file`, `append_to_file`, `run_command`

### Plan mode

Plan mode is for features and bugfixes you want thought through before any code changes. The assistant investigates first, asks about genuine uncertainties, and writes a concrete implementation plan. Nothing is edited until you approve that plan. It then carries the plan out step by step with the same prompt, tools, and loop as coding mode, keeping track of which step it is on.

Switch to it with `/plan` (or `Shift+Tab`) and describe the feature or bug.

#### 1. Investigate

The assistant explores the code (`find_files`, `grep_files`, `read_file`, …) and follows the code path the change touches. For a bug it tries to reproduce the problem with `run_command` (run the failing test, the script, or a one-liner), and it finds how the project is tested. `run_command` is available so it can observe the system, not so it can change it; `/run-confirm on` makes every command ask first.

When the code can't answer a question that changes the plan (two valid designs, unclear scope, a destructive step), it asks you with `ask_user`, one question at a time.

#### 2. Plan

The assistant calls `create_plan` with a title, a goal (the root cause, key decisions, and your answers), and an ordered list of steps. Each step names the file and function it changes, what the change is, and how to verify it. The last step runs the project's tests.

The harness writes the plan to **`.momo-plan.md`** in the working directory and shows it in chat:

```markdown
# Plan: Fix paginate() dropping the last partial page

## Goal

paginate() stops its range before the final partial page; page_count() floors instead of rounding up.

## Steps

- [ ] 1. **Fix paginate() loop bound**
  In pager.py paginate(), iterate while start < len(items) so the trailing partial page is kept.
  Files: pager.py
- [ ] 2. **Use ceiling division in page_count()**
  In pager.py page_count(), return (n_items + page_size - 1) // page_size.
  Files: pager.py
- [ ] 3. **Add regression tests and run the suite**
  Add partial-page cases to test_pager.py, then run python -m unittest -v.
  Files: test_pager.py
```

#### 3. Confirm

The harness then asks: *Execute this plan?*

| Answer | Effect |
|---|---|
| `y` | Approve and start executing immediately |
| `n` (or empty) | Keep the plan for later. The status bar shows `plan [awaiting approval]`. Start it with `/plan run` |
| anything else | Treated as feedback. The assistant investigates further if needed and submits a revised plan |

You can also **edit `.momo-plan.md` by hand** before answering (or before `/plan run`). You can reword, reorder, add, or delete steps, or mark a step `[-]` to skip it. The file is re-read when you approve, so your edits are what gets executed.

#### 4. Execute

The harness drives the plan one step at a time:

- Before each step, the plan with its progress is rendered into the system prompt, with `▶` marking the current step. The model always knows where it is, even after context compaction.
- Each step arrives as a `Plan step i/N` message and runs the full coding loop. The prompt is the coding agent's prompt, the tools are the full coding tool set, and each step gets its own 100-iteration budget, with the same retries and nudges.
- The model ends a step by calling `complete_step(summary)`, or by replying with text only. Tool calls after `complete_step` in the same turn are not run, so the model can't wander into the next step.
- If the model discovers the rest of the plan is wrong, it calls `revise_plan` with the corrected remaining steps. Finished steps are kept. Any step left out of the revision is reported in chat, and the model is told, so nothing gets dropped silently.
- Progress is visible in the status bar (`MODE: plan [exec 3/7]`) and in `.momo-plan.md`, where the checkboxes update as steps start (`[~]`) and finish (`[x]`, with a short note).

When the last step is done, the harness reports *Plan complete* and **deletes `.momo-plan.md`**.

#### Pausing and resuming

If a step is interrupted (`Shift+C`), hits a backend error, or runs out of iterations, execution **pauses** at that step. The plan and its progress stay in `.momo-plan.md` and in the saved session. To continue:

- `/plan resume` picks up at the paused step, or
- type any message: it is passed to the model as extra context for the resumed step (e.g. *"the test runner is `make test`"*).

This also works after restarting the harness and reloading the session with `/session`. `/plan cancel` discards the plan and deletes the file.

#### Plan commands

| Command | Description |
|---|---|
| `/plan` | Switch to plan mode (shows the current plan's state if there is one) |
| `/plan show` | Show the current plan with step statuses |
| `/plan run` | Approve a plan kept for later and execute it (re-reads `.momo-plan.md`) |
| `/plan resume` | Continue a paused plan from its current step |
| `/plan cancel` | Discard the plan and delete `.momo-plan.md` |

Investigation tools: read-only tools + `run_command`, `ask_user`, `create_plan`. Execution tools: same as coding mode + `complete_step`, `revise_plan`.

### Chat mode

The assistant acts as a conversation partner for exploring code and documents. Point it at a file or module and it will read it, explain what it found, and ask follow-up questions to deepen the discussion. It never writes or modifies files — it is a read-only dialogue mode designed for understanding rather than implementation.

Available tools: `list_directory`, `file_info`, `find_files`, `read_file`, `grep_file`, `grep_files`, `ask_user`

### Momo mode

Momo is a small black cat who lives in the harness and keeps you company. This mode is a companion first and a capable helper second: it chats, vents, and celebrates wins with genuine (slightly excessive) enthusiasm, but it also has the **full tool suite** and will read, edit, run, and write things when asked — or when its curiosity takes over and it wanders off to sniff at a suspicious filename. Replies are short, warm, lowercase, and entirely cat; reactions come *after* a tool runs, not before. Good for long grinding sessions when you want something alive in the terminal alongside you.

The animated companion in the bar between the chat pane and status bar *is* Momo — in this mode you are talking to it directly. (The companion walks around and mews in every mode; toggle it with `/companion on|off` or `Shift+Q`.)

Available tools: same as coding mode (all read-only + `write_file`, `edit_file`, `delete_file`, `move_file`, `append_to_file`, `run_command`, `ask_user`)

Switch modes with `/design`, `/chat`, `/plan`, `/code`, `/momo`, or `Shift+Tab`.

## Available Tools

### Read-only (all modes)

| Tool | Description |
|---|---|
| `list_directory` | List the contents of a directory — dirs first, then files with sizes |
| `file_info` | File/directory metadata: exists, type, size, mtime, line count |
| `find_files` | Find files using glob patterns (`*.py`, `src/**/*.ts`). Simple patterns are recursive automatically. |
| `read_file` | Read a file, optionally a specific line range |
| `grep_file` | Regex search in a single file — returns matching lines |
| `grep_files` | Recursive regex search across a directory — returns matching lines |
### Shared (design, coding, momo modes)

| Tool | Description |
|---|---|
| `write_file` | Write content to a file |
| `ask_user` | Pause mid-task and ask the user a focused clarifying question. The worker thread blocks until the answer is submitted; the status bar shows `? waiting for input`. |

Chat mode also has `ask_user` but not `write_file`.

### Coding mode only

| Tool | Description |
|---|---|
| `move_file` | Move or rename a file (parent dirs created automatically) |
| `append_to_file` | Append text to a file (creates if missing) |
| `edit_file` | Change text inside a file: replace `old_string` with `new_string`. One occurrence by default (errors if it matches zero or multiple times); pass `replace_all: true` to replace every occurrence. |
| `delete_file` | Delete a file |
| `run_command` | Run any shell command — scripts, tests, build tools, etc. Times out after 15 minutes (900s) by default; the model may request a shorter timeout, and 900s is the hard cap. |

All file operations are sandboxed to the working directory. Paths that attempt to escape via `..` are rejected.

`grep_files` returns at most 200 matches; `find_files` returns at most 100 files. Results over the cap include a trailer explaining how many were omitted.

## Skills

Skills are `.md` files in the `skills/` folder that get appended to the active role's system prompt. Use them to add domain-specific instructions — language conventions, coding paradigms, tool preferences, framework patterns, etc.

```
/list-skills              # show all available skills and which are active
/load-skill python        # append skills/python.md to the system prompt
/unload-skill python      # remove it
```

Active skills are saved with the session and restored on restart. Skills stack — multiple can be active at once. On a mode switch (`/code`, `/plan`, etc.) the role prompt is rebuilt with all currently active skills still included.

### Built-in skills

| Skill | Description |
|---|---|
| `bash` | Shell scripting idioms, safety defaults, portability notes |
| `c` | C programming patterns |
| `code-review` | Code review approach and checklist |
| `data-analysis` | pandas/polars patterns, jq idioms, CSV/JSON wrangling in Python |
| `designer` | Software design patterns |
| `document-editing` | Document structure, clarity editing, Markdown conventions |
| `html-javascript` | Semantic HTML, accessibility, modern JS patterns and pitfalls |
| `java` | Java patterns |
| `kotlin` | Kotlin patterns |
| `python` | Python patterns |
| `rust` | Ownership, borrowing, error handling, idiomatic Rust |
| `sql` | Query patterns, indexing, safe updates, migrations |
| `testing` | TDD and testing patterns |
| `text-based-game-design` | World design, puzzles, parser conventions |
| `typescript` | TypeScript type system, strict mode, common patterns |

To add a new skill, create a `.md` file in `skills/`. The filename (without extension) is the skill name used in commands.

## Prompt Construction

Each call to the model sends a messages array assembled from three sources: the system prompt, the conversation history, and the tool definitions.

### System prompt

The system message is built from the active role file plus any loaded skills:

```
┌─ system ──────────────────────────────────────────────────────────────┐
│                                                                       │
│  <role base text>                          ← roles/<mode>.md         │
│  (designer / coder / planner / chat / momo)                           │
│  {workdir} substituted with the actual working directory              │
│                                                                       │
│  ---                      (only present when skills are active)       │
│                                                                       │
│  <skill text>                              ← skills/<name>.md        │
│                                                                       │
│  ---                      (repeated for each additional skill)        │
│                                                                       │
│  <skill text>                              ← skills/<name>.md        │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

The system message is rebuilt in-place whenever the mode changes or a skill is loaded or unloaded. Sessions save the active skill list and reconstruct the system prompt from the current files on disk when loaded, so edits to role or skill files take effect immediately on next load.

### Conversation history

User messages, assistant replies, and tool results are appended as the session progresses. Thinking blocks are stored locally for display but are stripped before every API call:

```
┌─ user ────────────────────────────────────────────────────────────────┐
│  <message text>                                                       │
└───────────────────────────────────────────────────────────────────────┘
┌─ assistant ───────────────────────────────────────────────────────────┐
│  <text>   +   tool_calls: [{name, arguments}, …]                      │
└───────────────────────────────────────────────────────────────────────┘
┌─ tool ────────────────────────────────────────────────────────────────┐
│  <tool result text>                                                   │
└───────────────────────────────────────────────────────────────────────┘
  thinking: "..."   ← stored for display; never sent to the model
┌─ assistant ───────────────────────────────────────────────────────────┐
│  <text>                                                               │
└───────────────────────────────────────────────────────────────────────┘
┌─ user ────────────────────────────────────────────────────────────────┐
│  <next message>                                                       │
└───────────────────────────────────────────────────────────────────────┘
  ...
```

For Qwen-family models, `<`, `>`, and `&` in tool result content are XML-escaped in the copy sent to the API (unescaped content is kept in the stored history).

### Tool definitions

The set of tools included in the call depends on the current mode:

```
design  → list_directory  file_info  find_files  read_file
          grep_file  grep_files  write_file  ask_user

coding  → all design tools + edit_file  delete_file  move_file
          append_to_file  run_command

chat    → list_directory  file_info  find_files  read_file
          grep_file  grep_files  ask_user

plan    → investigating: read-only tools + run_command  ask_user  create_plan
          executing:     same as coding + complete_step  revise_plan

momo    → same as coding (full tool suite)
```

## Slash Commands

Type any command in the input bar:

| Command | Description |
|---|---|
| `/help` | Show all available commands |
| `/code` | Switch to coding mode |
| `/design` | Switch to design mode |
| `/plan` | Switch to plan mode (investigate → plan → approve → execute step by step) |
| `/plan show\|run\|resume\|cancel` | Manage the current plan — see [Plan mode](#plan-mode) |
| `/chat` | Switch to chat mode (read files, ask questions — no file writes) |
| `/momo` | Switch to momo companion mode (full tools; talk to the cat) |
| `/model` | List available models on the current backend |
| `/model <name>` | Switch to a different model |
| `/host` | Show current backend host URL |
| `/host <url>` | Connect to a different backend instance at runtime |
| `/token` | Show whether an auth token is set (masked display) |
| `/token <key>` | Set a Bearer token for authenticated remote hosts (never saved to disk or history) |
| `/clear-token` | Remove the current auth token |
| `/workspace` | Show the current working directory (alias: `/workdir`) |
| `/workspace <path>` | Change the working directory. If the path does not exist, prompts for confirmation before creating it. |
| `/think` | Show thinking mode state (on/off) |
| `/think on\|off` | Enable or disable model thinking/reasoning mode |
| `/tools on\|off` | Enable or disable tool calls (off = model receives no tool schemas) |
| `/tool-output on\|off` | Show or hide the tool calls pane |
| `/think-output on\|off` | Show or hide model thinking/reasoning blocks (also `Shift+T`) |
| `/markdown on\|off` | Enable or disable markdown rendering for assistant output (also `Shift+M`) |
| `/diff on\|off` | Show or hide colored diffs of file edits (also `Shift+D`) |
| `/diff-style git\|compact` | Choose diff presentation: full `git diff` layout or compact (default) |
| `/list-skills` | List available skills and show which are active |
| `/load-skill <name>` | Append a skill's instructions to the system prompt |
| `/unload-skill <name>` | Remove a skill from the system prompt |
| `/context` | Show context limit and current token usage |
| `/context <n>` | Set an absolute context token limit (e.g. `/context 16384`); minimum 256; clears any percentage scale |
| `/context <n>%` | Set the context limit as a percentage of the model's native maximum (e.g. `/context 75%`); saved in session and reapplied on model switch |
| `/tool-result` | Show the current tool result character cap |
| `/tool-result <n>` | Set the cap (e.g. `/tool-result 8000`); `0` = unlimited |
| `/compact` | Compact context — removes old messages and summarises them with the LLM |
| `/fast-compact` | Compact context without LLM summarisation (instant) |
| `/clear` | Clear conversation history |
| `/cost` | Show token usage for this session, aggregated by mode and model (in/out/total tokens per combination) |
| `/sessions` | List up to 20 recent sessions with mode and model |
| `/session` | Show the current session file path |
| `/session <name>` | Load a saved session by exact name or partial prefix (e.g. `2026-06-22` matches the first session from that date) |
| `/export` | Export the conversation to a Markdown file in the working directory (filename: `conversation-<timestamp>.md`) |
| `/export <filename>` | Export to a specific filename |
| `/copy` | Copy the last assistant message to the clipboard (tries `pbcopy`, then `xclip`, then `xsel`) |
| `/copy all` | Copy the full conversation to the clipboard |
| `/ls [path]` | List directory contents directly (no model round-trip) |
| `/read <path> [start] [end]` | Read a file directly, with optional line range |
| `/grep <pattern> [path_or_dir]` | Regex search in a file or across a directory directly |
| `/exit` or `/quit` | Save session and exit |

## Context Management

The harness automatically detects the model's native context window on startup and uses half of it as the working limit (e.g. a 262,144-token model gets a 131,072-token limit). The startup message confirms what was detected. Use `--context N` or `/context N` to override.

Token usage is estimated as `sum(len(content) // 4)` across all messages (a fast approximation). Tool call argument content is not counted, so real usage in tool-heavy sessions can be higher than the displayed percentage.

Compaction fires automatically when token usage exceeds the limit:

1. **Pass 1** — removes tool-call groups (oldest first): the assistant message that issued tool calls plus all its tool/thinking result messages are removed as a unit until usage drops to 33% of the limit.
2. **Pass 2** — if still over limit, removes the oldest user/assistant message pairs until the target is met.

After removing messages the harness makes a one-shot LLM call to summarise what was dropped and prepends the summary to the oldest remaining user message so the model retains key context. The notice in the chat pane says "summarised N messages" when this succeeds, or "removed N messages" if summarisation was skipped or failed.

The system prompt is never removed. Compaction runs on the worker thread — the TUI stays responsive and shows the spinner while the summary is being generated.

Use `/fast-compact` to skip LLM summarisation and compact immediately.

## Tool Result Cap

By default tool results are passed to the model without truncation. Set a cap to prevent a single large result from consuming the context window:

```
/tool-result          # show current cap (0 = unlimited)
/tool-result 8000     # cap results at 8000 chars
/tool-result 0        # disable cap (unlimited)
```

Truncated results are cut at the last line boundary before the cap and display a notice: `... (truncated after N chars of M — use read_file with start_line/end_line for specific sections)` so the model knows more content exists and gets an actionable hint for how to retrieve the rest.

## Sessions

Each session is automatically saved after every assistant response to:

```
~/.momo-harness/sessions/<timestamp>.json
```

Each session stores the model, Ollama host, mode, working directory, context settings, active skills, input history, and the full message history — all restored when the session is reloaded (the auth token is the only thing deliberately left out).

A log file (`.log`) is written alongside the JSON, recording every request, response, tool call, and token count in newline-delimited JSON format. Use it to audit what the model did or analyse token usage.

```
/sessions             # list recent sessions with mode and model
/session 2026-06-22   # load by partial prefix
/export               # save the current conversation as a Markdown file
/copy                 # copy the last assistant response to the clipboard
```
