# momo-coding-harness vs pi and crush: combined review

> Combines [momo-vs-pi.md](momo-vs-pi.md) and [momo-vs-crush.md](momo-vs-crush.md) (both reviewed 2026-10-27). The "already done" status was checked on 2026-10-30 against momo `062beef`. Line references may drift.

## Summary

Both reviews reach the same conclusion. **momo wins on the coding loop itself**: editing, shell, code navigation, undo, safety, coping with weak models, and evals. **pi and crush win on what surrounds it**: hooks, lazy skills, headless mode, sub-agents and loop control.

- **momo** has lots of built-in tools and is tuned for small local models.
- **pi** is a small core (4 tools) plus a large extension system.
- **crush** is a polished Go app built for hosted models.

## 1. How the three differ

| | **momo** | **pi** | **crush** |
|---|---|---|---|
| Approach | Lots of built-in tools, tuned for small local models | Small core (4 tools) plus a large extension system | Polished Go app built for hosted models |
| Tool definitions in the prompt | ~3.6–5.7k tokens (~3.8k with `/tools discover`) | **~1k** | ~6.2–7.1k |
| Editing | **Strongest** (many fallback matchers, syntax check, measured by the edit benchmark) | Weakest | Checks the file was read first and hasn't changed, and shows linter/type errors after each edit |
| Shell | **Best output views** (`wait_for`, grep/tail, saved logs) | Basic | **Safest defaults** (long commands move to the background after 60s, blocked-command list) |
| Code intelligence | **tree-sitter index**: no setup, works offline | None | Language servers: accurate types and rename, but they must be installed |
| Loop and context | Thinking trimming, cavecat | **Runs tools in parallel**, structured compaction | Loop detection, compacts with room to spare, explains calls that never got an answer |
| Extensibility | Custom tools only | **Strongest** (~30 events, packages, SDK) | Hooks and skills in Claude Code's format |
| Sessions | Linear, but has **real `/undo`** | Tree with fork | SQLite and file snapshots, but no revert |
| Interfaces | TUI and web UI | Print, JSON, RPC and SDK | TUI, `crush run` and an HTTP API |
| Safety | **Strongest** (file tools limited to the workdir, network off, fingerprint approvals) | Weakest (relies on containers) | Asks every time; grants are lost on restart |
| Evals | **Only one with behavioural evals** | Doc-lift evals | None (VCR tests only) |

## 2. Already done since the reviews

- **Deferred tools:** `find_tools` and `--tools-discover` (`76542c0`). This was pi's #1.
- **Shorter tool descriptions** (`1f86263`).
- **Structured compaction** (`7fc7217`). Both reviews asked for this.
- **Loop detection:** `--loop-guard` and `/loop-guard` (`b825b8b`). This was crush's #3.
- **Compacting with room to spare:** compaction now starts at 50% of the model's real window by default (`harness/harness.py:638`), which covers crush's #2.
- **Calls that can't run say why** (#3 below, `aa0fa09`): cut-off, malformed and interrupted calls are not run and get an `ERROR: not run` result with what to change (`harness/settle.py`).
- **Already present before the reviews, missed by them** (part of #11): structured multiple-choice questions (`ask_choice`) and MCP resources (`mcp_list_resources`, `mcp_read_resource`).
- **`todos` checklist** (part of #11) in coding and momo modes, kept through compaction (`harness/todos.py`).

## 3. Remaining improvements (best value for effort first)

| # | Improvement | From | Effort |
|---|---|---|---|
| 1 | **Refuse edits to files the model hasn't read or that changed since** (track the hash of the last read per path). momo has nothing like this today. | crush | S |
| 2 | **Move `run_command` to the background after N seconds** so a dev server run in the foreground doesn't block. Background runs already exist, so this is mostly a timer. | crush | S |
| 3 | ~~**Explain cut-off or unanswered tool calls** with what a retry should change ("output ran out, split the write").~~ Done. | crush | S |
| 4 | **Load `SKILL.md` skills lazily** (only name and description in the prompt) and also read `~/.claude/skills`. | both | S |
| 5 | **Headless mode** (`momo run "task"` / `-p --json`). Also lets you run evals headless. | both | S–M |
| 6 | **Auto-approve safe commands** in confirm mode, and block risky arguments (`npm i -g`, `pip install --user`). | crush | S |
| 7 | **Run read-only tools in parallel.** Measure first, because many local models make one call per turn. | pi (crush makes it opt-in per tool) | S–M |
| 8 | **Show linter/type errors after edits** (`ruff`, `tsc --noEmit`, `go vet`, at most 10 findings) as a lighter substitute for language servers. | crush | M |
| 9 | **Sub-agents:** a read-only explorer and a web researcher that return only a summary. Run evals first, since small models summarise poorly. | crush (pi as an extension) | M |
| 10 | **Hooks in Claude Code's format** (PreToolUse/PostToolUse, exit code 2 blocks). This would stop `harness.py` and `commands.py` from growing. | both | M |
| 11 | Images in `read_file`. (`todos`, structured questions and MCP resources are done.) | crush / pi | S–M |
| 12 | Forking a session from an earlier message, paired with checkpoints | pi | M–L |
| 13 | Codemode (the model writes a script that calls tools; only the output enters context). Run evals before building. | pi | L |

## 4. Keep, and don't adopt

**Keep:** momo's tool-call recovery, editing matchers, tree-sitter index, background runs, undo and evals. Both reviews say neither pi nor crush has anything comparable.

**Don't adopt:** crush's 7.5 KB `bash` description and long system prompt, a hard dependency on language servers, or pi's "security by containers" approach.

## 5. Not verified

- Whether `--tools-discover all` defers MCP tool definitions (pi's deferred-MCP idea).
- Whether compaction re-sends the original request afterwards (crush does this).
