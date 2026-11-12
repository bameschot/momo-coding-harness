"""The saved settings in one table: each one's Harness attribute, prefs key,
command-line flag, and the one parser that checks a flag and a saved value alike.

main.py builds its flags from SETTINGS (add_arguments) and applies them with
apply(): a flag beats the saved pref, a bad flag is a usage error, a bad or
missing pref leaves the Harness default.  The /commands check what you type
with the same parsers (parse) and set and save it with store(); the status
event carries every attribute here (status_fields), and the web settings
drawer shows them by name (data-setting).  tests/test_settings.py checks that
every key code saves is in this table (or in NOT_IN_TABLE), that every
attribute reaches the status event, and that the drawer names real fields.

Not here on purpose: settings that are session-only by design (/net,
/net-confirm, /run-confirm and the other security toggles reset to safe
defaults), the backend (provider/host/model, resolved with the restored
session), --mode and --context (applied around the session restore).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Callable

from . import code_index
from . import loop_guard as loop_guard_mod
from . import net as net_mod
from . import run_store as run_store_mod
from . import session as session_mod
from . import story_runner as story_runner_mod
from . import think_watch as think_watch_mod
from .harness import MIN_MAX_OUTPUT_TOKENS, THINK_HISTORY
from .llm.base import THINK_LEVELS
from .prompts import CAVECAT_ALIASES, CAVECAT_LEVELS
from .think_trim import KEEP_LEVELS
from .tools import TOOL_REF_STYLES
from .tools import registry as registry_mod

Parse = Callable[[Any], "tuple[Any, str]"]   # (value, "") or (None, why not)


# ── parsers: each takes a flag's string or a saved JSON value ──────────────

# on/off words that /think and /cavecat take for their on and off levels.
ON_OFF_WORDS = {"true": "on", "yes": "on", "1": "on", "false": "off", "no": "off", "0": "off"}
_OFF_WORDS = ("off", "none")


def choice(values, aliases: dict | None = None) -> Parse:
    def parse(v):
        if isinstance(v, str):
            v = v.strip().lower()
        v = (aliases or {}).get(v, v)
        if v in values:
            return v, ""
        return None, f"expected one of {', '.join(map(str, values))}, got: {v}"
    return parse


def on_off(v) -> tuple[bool | None, str]:
    """A bool (as saved) or on/off (as typed)."""
    if isinstance(v, bool):
        return v, ""
    s = str(v).strip().lower()
    if s in ("on", "true", "yes", "1"):
        return True, ""
    if s in ("off", "false", "no", "0"):
        return False, ""
    return None, f"expected on or off, got: {v}"


def integer(lo: int | None = None, hi: int | None = None, *, zero: bool = False,
            what: str = "off") -> Parse:
    """An int in lo..hi (25,000 and 25_000 read as 25000); with zero, 0 or
    off / none (= `what`: off, no cap) is allowed too."""
    def parse(v):
        if isinstance(v, bool):
            return None, f"not a number: {v}"
        if zero and isinstance(v, str) and v.strip().lower() in _OFF_WORDS:
            return 0, ""
        try:
            n = v if isinstance(v, int) else int(str(v).strip().replace(",", "").replace("_", ""))
        except ValueError:
            return None, f"not a number: {v}"
        if zero and n == 0:
            return 0, ""
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            rng = (f"{lo:,}-{hi:,}" if lo is not None and hi is not None
                   else f"at least {lo:,}" if lo is not None else f"at most {hi:,}")
            off = f" or off ({what})" if zero and what != "off" else " or off" if zero else ""
            return None, f"expected {rng}{off}, got: {v}"
        return n, ""
    return parse


def _net_bytes(v):
    """A typed size above the hard limit is refused; a saved one (an int, from
    before the limit came down) is cut to it."""
    saved = isinstance(v, int) and not isinstance(v, bool)
    size = v if saved and v > 0 else net_mod.parse_size(v)
    if not size:
        return None, f"not a size: {v}. Use bytes or a unit, e.g. 200000, 500kb, 2mb."
    if size > net_mod.HARD_MAX_BYTES and not saved:
        return None, (f"{net_mod.format_size(size)} is above the "
                      f"{net_mod.format_size(net_mod.HARD_MAX_BYTES)} hard limit")
    return min(size, net_mod.HARD_MAX_BYTES), ""


def _recap_secs(v):
    n, err = integer(1)(v)
    return (None, err) if n is None else (max(10, n), "")


def _discover(v):
    if isinstance(v, str):
        v = v.strip().lower()
    if isinstance(v, bool) or v in registry_mod.DISCOVER_MODES:
        return registry_mod.discover_mode(v), ""
    return None, f"expected one of {', '.join(registry_mod.DISCOVER_MODES)}, got: {v}"


# ── the table ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Setting:
    """attr: the Harness attribute (None: apply() returns the value instead).
    pref: its prefs.json key.  flag: its command-line flag, or None.
    cli: how argparse reads the flag — "str", "int", "choice" (choices from
    `choices`), "onoff" (on|off) or "bool" (--x / --no-x)."""
    attr: str | None
    pref: str
    parse: Parse
    flag: str | None = None
    cli: str = "str"
    choices: tuple = ()
    metavar: str | None = None
    help: str = ""


S = Setting
SETTINGS: tuple[Setting, ...] = (
    S("max_tool_result", "max_tool_result", integer(0), "--max-tool-result", "int", metavar="N",
      help="Max chars returned by a single tool call, 0 = unlimited "
           "(default: last /tool-result setting, else 0)"),
    S("run_mode", "run_mode", choice(("new", "classic"), {"on": "new", "off": "classic"}), "--run-mode", "choice", ("new", "classic"),
      help="run_command output: 'new' saves it to a log and returns a view "
           "(tail/grep, command_output); 'classic' returns all of it "
           "(default: last /run-mode setting, else new)"),
    S("tools_discover", "tools_discover", _discover, "--tools-discover", "choice",
      registry_mod.DISCOVER_MODES,
      help="Which tools are listed in the prompt and declared only once the model "
           "loads them (find_tools): 'extensions' = custom, workspace and MCP "
           "tools, 'all' = also rarely needed built-ins, 'off' = none "
           "(default: last /tools discover setting, else off)"),
    S("tool_ref", "tool_ref", choice(TOOL_REF_STYLES), "--tool-ref", "choice", TOOL_REF_STYLES,
      help="System prompt tool reference: 'full', 'compact' (one line per "
           "tool) or 'minimal' (only the call format) (default: last "
           "/tool-ref setting, else minimal)"),
    S("max_output_tokens", "max_output_tokens",
      integer(MIN_MAX_OUTPUT_TOKENS, zero=True, what="no cap"), "--max-output-tokens",
      metavar="N|off", help="most tokens one model reply may take, 0 = no cap "
                        "(default: last /max-output-tokens setting, else 25000)"),
    S("think_budget", "think_budget", integer(think_watch_mod.MIN_BUDGET, zero=True),
      "--think-budget", metavar="N|off",
      help="reasoning tokens one streamed reply may take before it is stopped "
           "and the model asked to act, 0 = off (default: last /think-budget "
           f"setting, else {think_watch_mod.DEFAULT_BUDGET})"),
    S("think_repeat", "think_repeat", on_off, "--think-repeat", "onoff",
      help="stop streamed reasoning that keeps repeating itself and ask the model to act "
           "(default: last /think-budget repeat setting, else on)"),
    S("reply_repeat", "reply_repeat", on_off, "--reply-repeat", "onoff",
      help="stop a streamed reply whose text keeps repeating itself and ask the "
           "model to act (default: last /think-budget reply-repeat setting, else on)"),
    S("progress_note", "progress_note", on_off, "--progress-note", "onoff",
      help="note to the model after many tool calls in a row with no change, "
           "question or plan (default: last /progress-note setting, else on)"),
    S("plan_questions", "plan_questions", on_off, "--plan-questions", "onoff",
      help="plan mode: refuse a first create_plan made before any question to "
           "the user, once (default: last /plan questions setting, else on)"),
    S("stories_approve", "stories_approve",
      choice(story_runner_mod.APPROVE_MODES, story_runner_mod.APPROVE_ALIASES)),
    S("stories_cap", "stories_cap", integer(*story_runner_mod.STORIES_CAP_RANGE)),
    S("loop_guard", "loop_guard",
      integer(loop_guard_mod.MIN_THRESHOLD, loop_guard_mod.MAX_THRESHOLD, zero=True),
      "--loop-guard", metavar="N|off",
      help="repeats of one tool call (or one error) that count as a loop, "
           "or off (default: last /loop-guard setting, else 3)"),
    S("loop_resets", "loop_resets", integer(0, loop_guard_mod.MAX_RESETS_LIMIT), "--loop-resets",
      "int", metavar="N",
      help="loop resets per turn before the turn stops, 0 = stop at the "
           "first loop (default: last /loop-guard resets setting, else 2)"),
    S("undo_max", "undo_max", integer(1, zero=True, what="no cap"), "--undo-max", metavar="N|off",
      help="turns of file checkpoints /undo can go back, 0 or off = no cap "
           "(default: last /undo-max setting, else 20)"),
    S("run_output_limit", "run_output_limit",
      integer(run_store_mod.MIN_LIMIT, zero=True), "--run-output-limit",
      metavar="N|off", help="Chars in run_command's default view in run mode new, 0 = off "
                        "(default: last /run-output-limit setting, else off)"),
    S("think_level", "think", choice(THINK_LEVELS, ON_OFF_WORDS), "--think", "choice", THINK_LEVELS,
      help="Thinking/reasoning level: off, on, or low/medium/high for "
           "models that take one (default: last /think setting, else on)"),
    S("think_history", "think_history", choice(THINK_HISTORY), "--think-history", "choice",
      THINK_HISTORY, help="Send the model's own reasoning back to it: off, turn (since "
                          "the last user message) or all (default: last setting, else turn)"),
    S("cavecat", "cavecat", choice(CAVECAT_LEVELS, {**ON_OFF_WORDS, **CAVECAT_ALIASES}), "--cavecat", "choice",
      CAVECAT_LEVELS + tuple(CAVECAT_ALIASES),
      help="on: compress plans, tool text and command output, and keep "
           "replies short, to save tokens (default: last /cavecat, else off)"),
    S("think_keep", "think_keep", choice(KEEP_LEVELS), "--think-keep", "choice", KEEP_LEVELS,
      help="How much of its earlier reasoning the model gets back: lean "
           "(small models), more (x2), most (x4), or off (all of it) "
           "(default: last /think-keep, else off)"),
    S("net_max_bytes", "net_max_bytes", _net_bytes, "--net-max-bytes", metavar="SIZE",
      help="Ceiling on a single fetch_url download, in bytes or with a "
           "unit: 200000, 500kb, 2mb (default: last /net-max-bytes "
           "setting, else 2mb)"),
    S("net_max_chars", "net_max_chars", integer(1000, net_mod.HARD_MAX_CHARS), "--net-max-chars",
      "int", metavar="N",
      help="Characters of page text one fetch_url call returns; the model "
           "pages through the rest (default: last /net-max-chars setting, "
           "else 24000)"),
    S("idle_recap", "idle_recap", on_off, "--companion-idle-recap", "bool",
      help="momo recaps the last turns in its speech bubble when you're idle "
           "(default: last /companion-idle-recap setting, else off)"),
    S("idle_recap_secs", "idle_recap_secs", _recap_secs, "--companion-idle-recap-secs", "int",
      metavar="N", help="Seconds of inactivity before momo recaps, at least 10 (default 90)"),
    S("session_titles", "session_titles", on_off, "--session-titles", "bool",
      help="Let the model title the session while you're idle "
           "(default: last /session-titles setting, else on)"),
    S("guides", "guides", on_off, "--guides", "bool",
      help="Put the workdir's AGENTS.md / CLAUDE.md / ... in the system prompt "
           "(default: last /guides setting, else off)"),
    S("mcp_enabled", "mcp", on_off, "--mcp", "bool",
      help="Connect the MCP servers in ~/.momo-harness/mcp.json and the "
           "workdir's .mcp.json (default: last /mcp setting, else on)"),
    S(None, "index", on_off, "--index", "bool",
      help="Index the workdir in memory and give the model the index_* search "
           "tools (default: last /index setting, else on)"),
    S("index_max_bytes", "index_max_mem", code_index.parse_max_mem, "--index-max-mem",
      metavar="SIZE", help="Memory budget for the code index: 100mb, 512kb, 1gb (default: last "
                           "/index-max-mem setting, else 100mb)"),
    S("index_max_files", "index_max_files", code_index.parse_max_files, "--index-max-files",
      metavar="N", help="Most files the code index covers (default: last /index-max-files "
                        "setting, else 100000)"),
    S("index_workers", "index_workers", code_index.parse_workers, "--index-workers", metavar="N",
      help="Processes the code index builds in: auto or 1-"
           f"{code_index.MAX_WORKERS} (default: last /index-workers setting, else auto)"),
    S("index_persist", "index_persist", on_off, "--index-persist", "bool",
      help="Load the saved code index at start and save it on exit, in "
           "~/.momo-harness/index/ (default: last /index-persist setting, else on)"),
    S("index_route", "index_route", on_off, "--index-route", "bool",
      help="With the code index on, answer plain-text grep_files and file-name "
           "find_files from the index (default: last /index-route setting, else on)"),
)
del S

# What apply() returns for settings with no Harness attribute, when neither a
# flag nor a valid pref gives one.
RETURN_DEFAULTS = {"index": True}

# Keys code saves to prefs.json that are not settings in this table: the
# backend and mode (restored with the session), MCP's per-server switches.
NOT_IN_TABLE = frozenset({"model", "provider", "mode", "mcp_disabled",
                          "last_session", "last_workdir", "workspace_sessions"})


_BY_PREF = {s.pref: s for s in SETTINGS}


def by_pref(pref: str) -> Setting | None:
    return _BY_PREF.get(pref)


def parse(pref: str, raw) -> tuple[Any, str]:
    """What a /command was given, read with the setting's own parser:
    (value, "") or (None, why not).  A KeyError for a pref not in the table."""
    return _BY_PREF[pref].parse(raw)


def store(harness, pref: str, value) -> None:
    """Set a parsed value on harness (when the setting has an attribute) and
    save it, so the next start begins with it."""
    s = _BY_PREF[pref]
    if s.attr is not None:
        setattr(harness, s.attr, value)
    session_mod.save_prefs(**{pref: value})


def status_fields(harness) -> dict:
    """{attribute: value} for every setting with one: StatusEvent's setting
    fields, named as the Harness attributes."""
    return {s.attr: getattr(harness, s.attr) for s in SETTINGS if s.attr is not None}


def dest(s: Setting) -> str:
    return s.flag.lstrip("-").replace("-", "_")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The table's flags, all defaulting to None (= not given)."""
    for s in SETTINGS:
        if s.flag is None:
            continue
        kw: dict = {"default": None, "help": s.help, "dest": dest(s)}
        if s.cli == "bool":
            kw["action"] = argparse.BooleanOptionalAction
        elif s.cli == "onoff":
            kw["choices"] = ("on", "off")
        elif s.cli == "choice":
            kw["choices"] = s.choices
        elif s.cli == "int":
            kw["type"] = int
        if s.metavar and s.cli not in ("bool", "onoff", "choice"):
            kw["metavar"] = s.metavar
        parser.add_argument(s.flag, **kw)


def apply(harness, args: argparse.Namespace | None, prefs: dict,
          error: Callable[[str], None]) -> dict:
    """Set every table setting on harness: the flag when given (a bad one
    calls error(message), which must not return), else a valid saved pref,
    else the Harness default stays.  Returns {pref: value} for the settings
    without an attribute (RETURN_DEFAULTS fills the ones nothing gave)."""
    out = dict(RETURN_DEFAULTS)
    for s in SETTINGS:
        given = getattr(args, dest(s), None) if (args is not None and s.flag) else None
        value = None
        if given is not None:
            value, err = s.parse(given)
            if value is None:
                error(f"{s.flag}: {err}")
                continue
        elif s.pref in prefs:
            value, _ = s.parse(prefs[s.pref])
        if value is None:
            continue
        if s.attr is None:
            out[s.pref] = value
        else:
            setattr(harness, s.attr, value)
    return out
