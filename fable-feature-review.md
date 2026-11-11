# Review of momo-coding-harness: bugs, inconsistencies, improvements, missing features

## Context

A review of the harness and its tools (2026-11-11), with a comparison against other
coding harnesses. Section A lists defects confirmed by reading the code, section B
structural inconsistencies, section C features other harnesses have that momo lacks,
section D the recommended order. Status: D1 (the A fixes) and D2 (the B1 settings
table) are implemented; everything else is a proposal.

**Coverage, so you know what the findings rest on.** Read in full: `harness.py`,
`controller.py`, `session.py`, `checkpoints.py`, `main.py`, `paths.py`,
`llm/{base,http,llamacpp_client}.py`, `web/server.py`, `tools/{dispatcher,shell,common}.py`,
`run_command`, the file tools, `workspace_runner.py`. Skimmed: `editing.py`, `prompts.py`,
`toolcall_text.py`, `mcp/stdio.py`, `workspace.py`, the web JS (only for `innerHTML`
and cancel handling). **Not read:** `code_nav.py`, `code_index/`, `story_runner.py`,
`plan_runner.py`, `stories.py`, `tui.py`, `commands.py`, `net.py`, `search.py`,
`html_text.py`, `compaction.py`, `loop_guard.py`, `mcp/` core. The test suite was
not run (plan mode is read-only). Overall the code is careful and well-tested; the
defects below are in the seams between features, not in the tools themselves.

---

## A. Confirmed bugs (proposed fixes)

### A1. `/retry` and "edit last message" can act on a harness note instead of your message
Harness nudges are stored as plain `role: "user"` messages with no marker
(`harness.py:2433, 2487, 2517, 2530, 2546, 2559, 2900, 2943, 3149, 3157`).
`truncate_at_last_user` (`harness.py:3698`), `Controller.edit_last` /
`last_user_message` (`controller.py:423, 456`) and `transcript_events`
(`controller.py:91`) all take "the last `user` message".
- After a turn that got any note (idle note, next-step note, think note, churn nudge),
  `/retry` truncates only back to the note and re-sends the note text as your message.
- A reloaded session shows every note as something you typed.
- `load_session`'s `turn_count` fallback and the session drawer's message count include them.

**Fix:** tag injected messages `{"role": "user", "harness": True}` through one helper
(`_append_note`), strip the key in `_api_messages()` (llama.cpp passes unknown keys
through, `llamacpp_client.py:140`), and make the four readers above skip tagged
messages (transcript replay renders them as system lines, as the live view does).
Old sessions: also treat content starting `[Note from the harness, not the user]` as tagged.

### A2. An exception in a worker thread prints a traceback into the curses screen and reports nothing
`Controller._run_bg` (`controller.py:318-327`) is `try: fn() finally: _finish_bg()` with
no `except`; `Harness.send`, `execute_plan_threaded` and `compact_threaded` are also
`try/finally`. There is no `threading.excepthook`. Any bug in the loop therefore goes
to stderr (corrupting the TUI, which the code elsewhere takes pains to avoid) and the
user sees the turn just stop. The assistant message with its tool calls may be left
without results.
**Fix:** catch in `_run`, emit `ErrorEvent(f"{type(e).__name__}: {e}")`, write the
traceback to the session log (`Logger`), and settle unanswered tool calls
(`settle.settled_result("interrupted", …)`) so the next request is well-formed.
Install a `threading.excepthook` in `main.py` that logs instead of printing.

### A3. `load_session` leaves the previous session's plan-draft state in place
`new_session` and `clear_conversation` reset `_plan_draft`, `_plan_asked`,
`_plan_gate_refused`, `_plan_first_request`, `_plan_restarts`, `_plan_qa`,
`_resume_note`, `_plan_heuristics`, `_plan_refused` (`harness.py:3639-3642, 3674-3677`,
two hand-copied lists); `load_session` (`harness.py:3509`) resets none of them, nor
`_steer`. Start a stories outline, `/session <other>`, and the other session's planner
gets "The plan is not finished" notes (`harness.py:2536`). `_plan_heuristics` is also
never set in `__init__` (only via `setdefault`, `plan_runner.py:711`).
**Fix:** one `_reset_plan_state()` called from `__init__`, `new_session`,
`clear_conversation` and `load_session`.

### A4. A turn waiting at `ask_user` cannot be interrupted
`Controller.cancel` returns False while waiting (`controller.py:462`); the TUI
(`tui.py:1812`) and web (`app.js:1581, 1779`) skip Esc in that state, and `_ask_user`
blocks on `queue.get()` with no cancel check (`harness.py:677`). The only way out of a
question is to answer it, and the model then carries on.
**Fix:** let cancel work while waiting: set `_cancel`, then unblock the queue with a
sentinel that `_ask_user` turns into "the user interrupted" — the loop's existing
`_cancel.is_set()` check at the top ends the turn.

### A5. Auto-compaction ignores the server's own token count
The trigger is `self._estimate() > self.context_limit` (`harness.py:2237`), the chars/4
estimate, which your notes record as ~2.5× low on digit-heavy output. The measured
`prompt_tokens + eval_tokens` of the last reply is already stored
(`_measured_tokens`, `harness.py:2340`) but only drives the status bar. Today the miss
is caught late, by the "prompt too long" error path — one wasted request.
**Fix:** keep a per-session ratio `measured / estimate` from each reply (clamped
1.0–4.0) and compare `estimate × ratio` with the limit; `_read_budget` uses it too.

### A6. `read_file` and the grep tools read a file of any size into memory
`common.read_text` (`tools/common.py:23`) does `p.read_bytes()` before any limit; the
read budget is applied to the rendered text afterwards. `read_file` on a multi-GB log
allocates it all (twice, with the decode).
**Fix:** stat first; above `MAX_SCAN_FILE_BYTES` for grep, or a larger read ceiling
for `read_file`, return the existing style of error naming `start_line`/`grep_file`
— or read ranged for negative `start_line` (seek from the end).

### A7. Smaller defects
- `session.new_timestamp()` has one-second resolution (`session.py:58`): two sessions
  started in the same second (two momo processes, `/new` twice) share a file. Add a
  short suffix when the path exists.
- `session.mark_current` reads prefs outside `_prefs_lock` and writes back a derived
  dict (`session.py:146-156`): a concurrent update to `workspace_sessions` is lost.
- `write_file`'s "No change" check uses `read_text`, which translates `\r\n`
  (`editing.py:1487`): LF content over a CRLF file reports no change. Compare bytes.
- The generic truncation note says "use read_file with start_line/end_line"
  for every tool (`harness.py:2783`), including grep and list results where that is wrong.
- `.DS_Store` is tracked in git; `.gitignore` should cover it.

---

## B. Inconsistencies and structural improvements

1. **One setting, seven places.** Each option is hand-wired in `Harness.__init__`,
   a `/command`, prefs load in `main.py` (≈170 lines of near-identical parsing,
   `main.py:226-373`), a CLI flag, `StatusEvent`, the web settings drawer and README.
   The drift shows: `think_repeat` and `session_titles` have no flag while
   `reply_repeat` has; `--mode` and `--tool-ref` hard-code their choices instead of
   `MODES` / `TOOL_REF_STYLES`; `plan_questions`/`progress_note` accept both bool and
   "on"/"off" prefs. A declarative table (name, type, default, validator, persisted?)
   driving all seven would remove this class of bug.
2. **`harness.py` is 3,710 lines and `_run_loop` ~770.** The per-call dispatch chain
   (`harness.py:2670-2769`) is a 20-branch `elif` on tool names although the registry
   already has `Tool.harness`; a `handler` field on the Tool would let the loop call
   `tool.handler(self, args)`. The loop's ~20 local counters would sit better in a
   small `TurnState` object, which also makes A2's cleanup straightforward.
3. **Defensive `isinstance(args, dict)` checks are applied to some branches only**
   (`harness.py:2678-2735`), while both sources of calls guarantee dicts
   (`llm/base.py:74`, `toolcall_text.py:323`). Either drop them or check once.
4. **Sessions and logs are never pruned** (`session.py`, `logger.py`): run logs and
   checkpoints age out after 3 days, sessions grow forever. Add `/sessions prune <days>`
   and an optional retention pref.
5. **Autosave rewrites the whole session with `indent=2` every turn**, including the
   `compacted` archive (`harness.py:3503`, `session.py:116`). For long sessions, write
   compact JSON and move the archive to a sidecar file appended on compaction.
6. **`run_command` uses `/bin/sh`** (`shell.py:166`) while models write bash
   (`[[ ]]`, `source`, `&>`), and `split_view_pipe` itself recognises `PIPESTATUS`.
   On Linux `sh` is dash. Use bash when present and say which shell in the
   Environment section.
7. **Streams have no idle timeout** (`llm/http.py:220`): a server that accepts and
   then hangs holds the turn until Esc. A generous idle limit (e.g. 10 min without a
   byte) with the existing transient-retry path would cover it.
8. **Web hardening.** No token on loopback means any local user or process can drive
   the harness (run commands as you) on a shared machine; POSTs without `Origin` are
   accepted (`server.py:589`). Options: token by default (as Jupyter does), and a
   `Content-Security-Policy` header as a second line behind the markdown escaper
   (needs a hash for the one inline script in `index.html`).
9. **Checkpoints do not cover `run_command`, MCP or write-category workspace tools**
   (documented for commands). See C5 for a way to close this.
10. **Packaging.** No `pyproject.toml`, entry point, `--version`, changelog or CI
    workflow; tests need a hand-typed command with `HOME` and `PYTHONPATH` set.

---

## C. Missing features compared with other harnesses

Compared against Claude Code, Codex CLI, OpenCode, Aider, Cline, Goose, Crush and pi.
momo is ahead of most of them on small-model steering (loop guard, reasoning watch,
text tool-call recovery, tool discovery, in-memory index, TDD/story plans) and on
measurement. The gaps, most valuable first:

| # | Feature | Who has it | Fit for momo |
|---|---------|-----------|--------------|
| 1 | **Permission rules**: allow / ask / deny per tool and per command pattern, "always allow `pytest`", persisted per project | Claude Code, Codex, OpenCode, Cline | High. Today it is `/run-confirm` on or off, and file writes never ask. `shell.shell_writes` already extracts write targets and could flag writes outside the workdir or destructive commands (`rm -rf`, `git reset --hard`, `git push --force`). |
| 2 | **OS sandbox for `run_command`** (macOS `sandbox-exec`, Linux bwrap/Landlock): writes confined to the workdir, network off when `/net off` | Codex, Claude Code | High, and stdlib-only (it shells out). Turns two "steering, not a sandbox" notes into real guarantees. |
| 3 | **Sub-agent with a fresh context** for search/exploration that returns a summary | Claude Code, OpenCode, Goose | High for small windows. The story roles already run fresh contexts; a general `explore(question)` tool would reuse that machinery. |
| 4 | **Hooks / post-edit checks**: run a configured lint, format or test command after edits and feed failures back | Aider (auto-lint/test), Claude Code hooks, OpenCode | High. Only `SyntaxWatch` exists now. A per-project `check_command` is the smallest version. |
| 5 | **Git-backed snapshots** (shadow repo per turn) so undo also covers what commands changed; `/diff`, `/commit`, optional auto-commit | Cline, OpenCode, Aider | Medium-high. Closes B9. |
| 6 | **Non-interactive mode**: `momo -p "…"` with text/JSON output and exit code | Claude Code, Codex `exec`, Aider `-m` | Medium. The evals already drive `Harness` directly; this is that driver made public. Must auto-answer or fail on `ask_user`. |
| 7 | **Generic OpenAI-compatible provider** (LM Studio, vLLM, MLX, OpenRouter) and **per-role models** (large planner, small executor, cheap title/summary model) | OpenCode, Aider architect mode, Crush | Medium. `LlamaCppClient` is already OpenAI-shaped; only `/props` and `/tokenize` are specific. |
| 8 | **Image input** for vision models (paste/attach a screenshot) | Claude Code, Codex, Cline | Medium. Attachments reject binaries today; the Ollama wire format already carries `images`. |
| 9 | **User and project skills, custom commands**: skills load only from the repo's `skills/`; no `~/.momo-harness/skills/`, `.momo/skills/`, model-triggered loading by description, or prompt-template commands | Claude Code, OpenCode, Goose recipes | Medium. `/tools discover`'s catalog mechanism would carry skills too. |
| 10 | **Project memory and richer guides**: guides are off by default, root-only, no nested `AGENTS.md`, no `/init` to write one, no model-writable memory that is put back in the prompt | Claude Code, Codex, Gemini CLI | Medium. |
| 11 | **Rewind to any earlier message** (conversation and files together) and **fork a session** | Claude Code, OpenCode, pi | Medium. Checkpoints are already per turn; `/retry` only reaches the last message. |
| 12 | **MCP gaps** already listed in the README: OAuth, prompts, sampling, elicitation, image results | Claude Code, Goose | Low-medium. |
| 13 | **Parallel read-only tool calls** in one batch; **several sessions at once** in the web UI | Claude Code, OpenCode | Low for a single local model. |
| 14 | **Windows support** (curses, process groups) | most | Low unless wanted. |

Not proposed, because you declined them before: an LSP backend, `rename_symbol`,
the test-command env line.

---

## D. Recommended order

1. **Fix A1–A7.** A1 and A2 first: one corrupts
   what `/retry` sends, the other hides every other bug.
2. **B1 settings table** before adding features — every feature in C adds settings.
3. **C1 + C2** together (rules decide, the sandbox enforces), then **C4**, **C3**.
4. The rest by appetite.

## Implemented (2026-11-11)

**A1–A7**, with regressions in `tests/test_review_fixes_1111.py`:

- A1: `harness.note_message()` / `is_note()` / `NOTE_KEY`; every injected note is
  tagged, `_api_messages` strips the key, `truncate_at_last_user`, `edit_last`,
  `last_user_message` and the `load_session` turn count skip notes, and
  `transcript_events` replays them as system lines. Old sessions' notes are
  recognised by the `[Note from the harness, not the user]` prefix. A user
  message merged into a trailing note makes it the user's.
- A2: `Harness.report_crash()` logs the traceback (`Logger.log_error`), settles the
  newest batch's unanswered calls, saves, and emits an `ErrorEvent`;
  `Controller._run_bg` and the steer re-send call it; `main.py` installs a
  `threading.excepthook` that logs instead of printing.
- A3: `Harness._reset_plan_state()` replaces the two copied reset lists and also
  runs in `load_session` (plus the steer queue); `_plan_heuristics` is set in `__init__`.
- A4: `Harness.cancel_question()` + a `_CANCELLED` sentinel; `Controller.cancel`
  works while waiting; Esc in the TUI and web, and the web stop button, too.
  An `ask_user` / `ask_choice` cut short returns "not answered — the user interrupted".
- A5: `_est_ratio` (server prompt tokens per estimated token, clamped 1–4, reset on
  a model switch) scales `_compact_limit()`, used by the compaction trigger, the
  compaction target and `_read_budget`.
- A6: `tools/common.read_text` refuses files over `MAX_READ_BYTES` (64 MB) before reading.
- A7: `session.new_timestamp` adds `-2`, `-3`… on a clash; `session.update_prefs`
  does read-modify-write under the lock (`mark_current` uses it); `write_file`
  compares bytes; the `/tool-result` cut note is tool-specific; `.DS_Store` untracked.

**B1 settings table**, `harness/settings.py` (tests: `tests/test_settings.py`):
33 saved settings, each with its Harness attribute, prefs key, flag, help and
one parser for flag and pref alike. `main.py` builds the flags from it
(`add_arguments`) and applies them (`apply`): flag, else a valid pref, else the
Harness default; a bad flag is a usage error. That replaced ~150 lines of
per-setting parsing. New flags that were missing: `--think-repeat`,
`--session-titles`. `--mode` takes its choices from `MODES`. The test fails if
code saves a prefs key that is not in the table (or the table lists one nothing saves).
Still per-setting: the `/commands` keep their own parsing and messages (they save
with the table's pref keys, which the test checks), and `StatusEvent` / the web
settings drawer are unchanged. Moving the commands onto the table's parsers is
the natural next step.

**Found while testing, not fixed:** `tests/test_tool_registry_golden.py` already
fails on HEAD (216 configs, all system-prompt hashes). The role prompts changed in
the recent story-planning commits without `MOMO_REGEN_GOLDEN=1`. These changes
leave it exactly as it was (same 216, prompt text only). Regenerate it once the
role edits are confirmed intended.

## Verification

`HOME=$PWD/.scratch/home-test PYTHONPATH=.:tests .venv/bin/python -m pytest tests -q`:
1679 passed (the golden test deselected, see above). `app.js` syntax checked with
`osascript -l JavaScript`. A headless start (`--headless --fresh --think-repeat off
--loop-guard off --undo-max 7`) showed the values in `/api/state`, and
`--loop-guard 99` exits with a usage error.
