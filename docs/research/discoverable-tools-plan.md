# Plan: discoverable tools

> **Implemented** as `/tools discover` (see the README's "Loading tools on demand"). Differences from this draft: the meta-tool is `find_tools`; `index_map` and `index_changes` stay core because the prompt's index rules name them; auto-load uses a declared `Tool.suggests` list rather than scanning every result; the flag is `--tools-discover`/`--no-tools-discover`.
>
> Follows finding 1 in [momo-vs-pi.md](momo-vs-pi.md): keep a small core declared, and let the model load the rest on demand. Planned 2026-10-27 against `7529146`.

## Why

Tools cost tokens in **two** places. The first is the schemas sent with every request. The second is the compact tool reference that `render_tool_reference()` puts in the system prompt (`harness/tools/reference.py:58`, used at `harness/harness.py:1242`). Measured with the 4-chars-per-token estimate:

| Role / toggles | Tools | Schemas | Reference | Total |
|---|---|---|---|---|
| design | 16 | 3.6k | 1.1k | **4.7k** |
| coding | 24 | 5.2k | 1.6k | **6.8k** |
| coding + index (the default) | 29 | 6.1k | 2.0k | **8.1k** |
| coding + index + net | 32 | 7.4k | 2.3k | **9.8k** |

On a 16–32k local model that is 25–60% of the window before the first message. MCP servers add to it without limit, because `_mcp_tools()` declares every MCP tool directly.

## Design

### 1. Each tool is either core or on demand
- Add `Tool.discoverable: bool = False` in `harness/tools/registry.py`. Core tools are declared as they are today. On-demand tools are left out of the schemas and the reference while discovery is on, and appear only as one line in a catalog.
- **Never on demand:** the tools in `LOCKED_TOOLS` (plan tools), `ask_user`, and `complete_step`/`revise_plan` (plan-exec).
- **Initial on-demand set** (to be confirmed by phase 0): `append_to_file`, `delete_file`, `move_file`, `edit_files`, `replace_symbol`, `file_info`, `grep_extract`, `scratch_write`, `command_output`, `stop_command`, `file_dependencies`, `index_map`, `index_changes`, `index_status`, `add_search_source`. Their schemas come to about 2.3k tokens, plus about 1k of reference lines.
- `fetch_url` and `web_search` stay core while `/net` is on, because the user turned the network on to use them.
- Custom and workspace tools: honour an optional `"discoverable": true` key. The default is core, so nothing changes for existing tools.
- MCP tools: on demand by default while discovery is on, with a per-server override `"exposure": "direct" | "discover"` in `mcp.json`. MCP is where this scales best, as it does in pi.

### 2. One meta-tool: `find_tools`
Its name follows momo's `find_*` convention. It is declared whenever discovery is on and something on-demand exists for the role.

```json
{"name": "find_tools",
 "description": "Load more tools. The catalog in the system prompt lists them; pass their names, or describe what you need.",
 "parameters": {"names": {"type": "array", "items": {"type": "string"}},
                "query": {"type": "string"}}}
```

- **`names`** (exact) loads those tools. **`query`** ranks the catalog by a small stdlib BM25 over name, description and parameter names, then loads the top 3. Each call loads at most 5 tools.
- **The result** gives each loaded tool's signature, its first sentence and its `<tool_call>` example, built with the existing `example_args()` and `_first_sentence()` in `reference.py`. A model that writes text-format calls gets its example from here, without the system prompt changing.
- **Why an explicit tool** rather than letting the model call undeclared tools directly: with `tool_choice: "auto"` (`harness/llm/llamacpp_client.py:170`), llama.cpp's tool grammar can restrict calls to the declared names. A direct call to an undeclared tool would then never come through.

### 3. The loaded set
- Store `Harness.loaded_tools: set[str]`. It is sticky for the session: a loaded tool is never unloaded automatically. It is saved in the session next to `disabled_tools` (`harness/harness.py:2455`, `session.save`) and cleared by `/new`.
- **Why sticky:** chat templates (Qwen, Hermes) render the tool list inside the system turn. Every change to the tool list therefore throws away the server's prompt cache once, so loads should be rare and should never reverse.
- `schema_for()` gating still applies. A loaded index tool disappears while the index is off; a loaded write tool is not offered in design mode.
- A tool the user turned off (`disabled_tools`) is not in the catalog and cannot be loaded.

### 4. Loading without being asked (momo-specific, phase 2)
Many tool results already name the tool that should come next:
- `run_command` names `command_output` and `stop_command` for logs and background runs.
- The `read_file` footer names `read_symbol` and `code_outline`.
- The editing hints name `replace_symbol` and `edit_files` (`harness/editing.py`).

After each result, if the text names an on-demand tool, load it. The model then never needs `find_tools` for the tool that was just suggested to it. Build the name set from `registry`, not a hard-coded list.

### 5. Prompt
- The reference covers declared tools only, as it does now, because it renders `_current_tools()`.
- Add a new **"More tools"** section of about 15–20 tokens per tool, e.g. `move_file(src, dst): Move or rename a file.`, plus one line saying to load them with `find_tools`. Generate it from the catalog, and render the whole catalog every time, loaded tools included, so loading never changes it.
- **Do not call `rebuild_system_prompt()` on load.** The system prompt stays byte-identical, and only the `tools` field grows.
- `_nav_rules`, `_net_rules` and `_index_banner` take the declared names. Check that none of them tells the model to use a tool it doesn't have; the phase 0 data should keep nav tools core anyway.

### 6. Loop
- `_run_loop(tools, …)` (`harness/harness.py:1720`) fixes `tools` for the whole run. After any tool batch that changed `loaded_tools`, recompute `tools = self._current_tools()`. Do the same in `plan_runner.py:439`.
- `_extract_text_tool_calls(content, tools)` (`harness.py` around line 1849) must accept on-demand names too. Pass the declared tools plus the catalog, so a text-format call to a known on-demand tool is recovered, loaded and run instead of rejected.
- The dispatch path is unchanged, because `registry.get()` already resolves every tool.

### 7. Settings and UI
- Add `/tools discover on|off` and `--tools-discover`, saved in prefs like `/run-mode`. The default stays **off** until phase 3 decides.
- Add `/tools load <name>` and `/tools core|demand <name>` (a per-user override, kept in prefs; workspace overrides go in `.momo/tools.json`).
- In `tool_choices()` (`harness.py:765`), add a `state` field of `core | loaded | on demand` to each row. The TUI picker and `GET /api/tools` show it.
- In `/context` and `system_prompt_view()`, show the catalog section and the meta-tool's schema separately. `_schema_tokens()` already keys its cache by tool names.

## Phases

**Phase 0: measure (no code changes to momo).** Run `evals/run_evals.py --suite all --runs 3 --json baseline.json` on the reference model, Qwen3.5-9B on llama.cpp. Count how often each tool is called (the run records carry `"tools"`, `evals/run_evals.py:228`). Any tool used in more than about 5% of runs, or that is any task's `ideal` tool, stays core. Fix the initial on-demand set from that data.

**Phase 1: the mechanism** (behind `/tools discover`, default off)
- `harness/tools/registry.py`: the `discoverable` field; `offered()` takes the loaded set and returns declared tools; a new `catalog(ctx, loaded)`.
- `harness/tools/discover.py` (new): ranking, the `find_tools` executor, and rendering the result.
- `harness/tools/builtin/find_tools.py` (new): the `Tool` declaration (`harness=True`, handled in the loop like `ask_user`, because it changes harness state).
- `harness/tools/reference.py`: render the catalog section.
- `harness/harness.py`: `loaded_tools`, `_current_tools()`, refreshing `tools` in the loop, text-call recovery over the catalog, session save and restore, `tool_choices()`.
- `harness/plan_runner.py`, `harness/commands.py`, `harness/main.py`, `harness/session.py`, `harness/web/` (picker state).
- Mark the phase 0 set with `discoverable=True` in their `builtin/*.py` files.

**Phase 2: loading without being asked, and MCP**
- Load on mention (§4), and MCP `exposure` (`harness/mcp/config.py`, `mcp_mixin.py`).

**Phase 3: decide with evals**
- Add `--tools-discover on|off` to `evals/run_evals.py`, in the same style as `--edit-features`, and a `discover` column: `find_tools` calls, loads, and prompt tokens at the first request.
- Add 3 tasks to `evals/tasks.py` that need an on-demand tool (rename a file with `move_file`; start a server in the background and read its log with `command_output`; delete generated files), plus one control task where the core set is enough.
- **Adopt** (make it the default) if the ideal-tool rate and the malformed-call count are no worse, and the first-request token count drops by 25% or more. Also run the `edit` suite, because `edit_files` and `replace_symbol` move to on demand.

## Tests (`tests/test_tool_discovery.py`, unittest like the rest)
- Gating: with discovery on, a discoverable tool is missing from `offered()` and present in `catalog()`. After loading, it is in `offered()`. Role and index gating still apply after loading. Locked and plan tools are never discoverable.
- `find_tools`: exact names load; an unknown name gives an `ERROR:` that lists the catalog; a query ranks `move_file` first for "rename a file"; the 5-tool cap holds; a disabled tool can't be loaded.
- Prompt stability: the system prompt text is identical before and after a load. The catalog lists loaded tools too.
- Loop: a scripted fake client (the existing test doubles) calls `find_tools` and then the loaded tool in the next turn, and the second request's `tools` contains it.
- Text recovery: `<tool_call>{"name": "move_file", …}` with `move_file` on demand is recovered and loads the tool.
- Persistence: `loaded_tools` survives save and restore and is cleared by `/new`.
- Auto-load (phase 2): a `run_command` background result loads `stop_command`.
- With discovery off, everything is byte-identical to today. Regression-test this against the current `offered()` and reference output.

## Verification
1. `python -m unittest discover tests` passes.
2. `/system-prompt` and `/context` with discovery on show the smaller reference, the catalog and the lower schema tokens. Compare with the table above.
3. Manually, in the TUI: ask "rename foo.py to bar.py". The model calls `find_tools` (or `move_file` directly via the catalog), the tool loads, and the rename runs with its diff shown.
4. The phase 3 A/B numbers decide the default.

## Risks
- **Small models may not reach for `find_tools`.** This is the main risk. The catalog-in-prompt approach and loading on mention are the mitigations, and the A/B decides.
- **Prompt cache invalidation on load.** Mitigated by sticky loads and a stable system prompt. Measure the time to first token after a load on llama.cpp.
- **Hints in tool results that name on-demand tools.** These are covered by loading on mention. Until phase 2, they should mention `find_tools`.
