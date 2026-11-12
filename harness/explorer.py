"""explore: a sub-agent with a fresh context, called as a tool from inside a
turn (fable-feature-review.md C3).

The explorer gets its own system prompt (roles/explorer.md) and tool set (the
"explorer" role: the read, code-nav, index and net tools, and run_command
behind the read-only shell guard), the question as its first message, and the
shared loop (_run_loop) with a round cap.  Its final reply is the report,
returned as the explore call's result; the caller's context keeps only the
call and the report.  Its own context goes to the compaction archive at the
call's place (by="explore"), so /export full shows it.

The nested loop runs on the same Harness, so everything _run_loop reads or
resets for the caller's turn is saved and restored here, and the paths that
belong to the caller's role (the planner, a plan step, steering) check
self._explore and stand aside while it is set."""
from __future__ import annotations

import time
from datetime import datetime

from . import transcript as transcript_mod
from .events import ChatEvent
from .syntax_watch import SyntaxWatch

EXPLORE_CAP = 30            # _run_loop rounds of one explorer
REPORT_CHARS = 6000         # the report returned to the caller, at most
_LOOKED_AT_MAX = 25         # files listed under "Files it looked at"
# The fields _run_loop and the tools set for the caller's turn, saved around the
# explorer and put back after it.
_SAVED = ("messages", "compacted", "_turn_user", "_step_summary", "_last_run",
          "_last_failed_call", "_last_run_exit", "_measured_tokens", "_token_estimate",
          "_syntax_watch")


class ExploreMixin:
    """The explore tool for Harness."""

    def _sub_role(self) -> str | None:
        """The role that replaces the mode's for a while: the explorer, or a
        stories plan's part (story_role); None otherwise."""
        if getattr(self, "_explore", None) is not None:
            return "explorer"
        return getattr(self, "story_role", None)

    def _loop_role(self) -> str:
        """The role the loop runs as: a sub-role, an executing plan, or the mode."""
        return self._sub_role() or ("plan-exec" if self._plan_executing() else self.mode)

    def _emit_reply(self, text: str) -> None:
        """The model's text as a chat message, except an explorer's: its report
        reaches the chat as the explore call's result."""
        if getattr(self, "_explore", None) is None:
            self.event_queue.put(ChatEvent("assistant", text))

    def _handle_explore(self, args: dict) -> str:
        question = str(args.get("question") or "").strip()
        if not question:
            return "ERROR: explore needs a question."
        if self._explore is not None:
            return "ERROR: an explorer cannot start another explorer; investigate it yourself."
        raw_paths = args.get("paths") or []
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        paths = [str(p).strip() for p in raw_paths if str(p).strip()] if isinstance(raw_paths, list) else []

        saved = {k: getattr(self, k, None) for k in _SAVED}
        saved_loaded = set(self.loaded_tools)
        t0 = time.monotonic()
        self._explore = {"question": question, "paths": paths}
        self.messages = [{"role": "system", "content": ""}]
        self.compacted = []           # its own archive, merged in at the end
        self._syntax_watch = SyntaxWatch()
        outcome, report = "error", ""
        try:
            self.rebuild_system_prompt()
            self.emit_status()
            self.event_queue.put(ChatEvent("system", f"🔍 Exploring: {_clip(question, 200)}"))
            self._append_user(_brief(question, paths))
            before = self._background_runs()
            try:
                outcome = self._run_loop(self._current_tools() if self.tools_enabled else [],
                                         EXPLORE_CAP)
            finally:
                self._stop_background_runs(before, "the explorer")
            report = _final_reply(self.messages)
        finally:
            sub = self.messages
            sub_archive = self.compacted
            for k, v in saved.items():
                setattr(self, k, v)
            self.loaded_tools.clear()
            self.loaded_tools.update(saved_loaded)
            self._explore = None
            self._archive_explorer(sub, sub_archive)
            self.rebuild_system_prompt()
            self.emit_status()
        calls = _calls(sub)
        secs = time.monotonic() - t0
        looked = _looked_at(calls)
        self.event_queue.put(ChatEvent("system",
            f"🔍 Explorer {'done' if outcome in ('done', 'limit') and report else 'stopped'} — "
            f"{len(calls)} tool call{'s' if len(calls) != 1 else ''}, {secs:.0f} s"))
        files = ("\n\nFiles it looked at: " + ", ".join(looked)) if looked else ""
        if outcome == "cancelled":
            return "ERROR: the explorer did not finish — the user interrupted the turn." + files
        if not report:
            why = {"limit": f"it ran out of rounds ({EXPLORE_CAP})",
                   "loop": "the loop guard stopped it"}.get(outcome, f"it stopped ({outcome})")
            return (f"ERROR: the explorer gave no report: {why}. Investigate the rest yourself, "
                    "or call explore again with a narrower question." + files)
        if len(report) > REPORT_CHARS:
            cut = report.rfind("\n", 0, REPORT_CHARS)
            cut = cut if cut > REPORT_CHARS // 2 else REPORT_CHARS
            report = report[:cut] + f"\n... (report cut after {cut} of {len(report)} chars)"
        head = ("The explorer's report (its round cap ended it; it may be incomplete):"
                if outcome == "limit" else "The explorer's report:")
        return f"{head}\n\n{report}{files}"

    def _archive_explorer(self, sub: list[dict], sub_archive: list[dict]) -> None:
        """Put the explorer's context (with what its own compactions removed) in
        the caller's archive, just before the explore call's result, which is
        the next message the caller's history gets."""
        if len(sub) <= 1 and not sub_archive:
            return
        pos = len(self.messages)
        n = max((r["n"] for r in self.compacted), default=0) + 1
        when = datetime.now().isoformat(timespec="seconds")
        for msg, _note in transcript_mod.full_transcript(sub, sub_archive)[1:]:
            self.compacted.append({"pos": pos, "kind": "removed", "n": n, "time": when,
                                   "message": msg, "by": "explore"})


def _brief(question: str, paths: list[str]) -> str:
    lines = ["# Question", "", question]
    if paths:
        lines += ["", "Start from: " + ", ".join(f"`{p}`" for p in paths)]
    lines += ["", "Investigate, then reply with your report (the answer, the evidence with "
                  "path:line, what you did not check) and no tool call."]
    return "\n".join(lines)


def _final_reply(msgs: list[dict]) -> str:
    """The explorer's report: its last message, when that is a reply without
    tool calls (a text-only reply, the round cap's summary, an answer taken
    from its reasoning)."""
    last = msgs[-1] if len(msgs) > 1 else {}
    if last.get("role") != "assistant" or last.get("tool_calls"):
        return ""
    return str(last.get("content") or "").strip()


def _calls(msgs: list[dict]) -> list[tuple[str, dict]]:
    out = []
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for c in m.get("tool_calls") or ():
            fn = c.get("function") or {}
            a = fn.get("arguments")
            out.append((fn.get("name") or "", a if isinstance(a, dict) else {}))
    return out


def _looked_at(calls: list[tuple[str, dict]]) -> list[str]:
    """The paths the explorer's calls named, in first-seen order."""
    seen: dict[str, None] = {}
    for _name, a in calls:
        for key in ("path", "paths", "file", "files"):
            v = a.get(key)
            for p in (v if isinstance(v, list) else [v]):
                if isinstance(p, str) and p.strip() and p.strip() not in (".", "./"):
                    seen.setdefault(p.strip(), None)
    out = list(seen)
    if len(out) > _LOOKED_AT_MAX:
        out = out[:_LOOKED_AT_MAX] + [f"… {len(seen) - _LOOKED_AT_MAX} more"]
    return out


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "…"
