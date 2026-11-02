"""Running a stories plan (/plan stories): the Harness methods behind
plan_stories / write_story / finish_stories / complete_story and the phase
loop (see stories.py).

Each phase runs in three parts, each in a fresh context (the history before
it goes to the compaction archive, marked by="story", so /export full keeps
it): the story writer breaks the phase into stories against the code as it is
now (an outline first, then one story per turn, so no single turn has to work
out the whole set); the stories run one at a time, in dependency order, each accepted only
when its acceptance checks pass; then the phase's own checks run and a phase
report is written for the next phase's writer.  A story that fails is re-split
by the writer; one that fails again pauses the plan for the user."""
from __future__ import annotations

import re
import shlex
from datetime import datetime
from pathlib import Path

from . import code_nav
from . import tdd as tdd_mod
from . import transcript as transcript_mod
from .events import ChatEvent, ErrorEvent, ToolCallEvent, ToolResultEvent
from .loop_guard import _owners
from .plan import PLAN_FILENAME, Step, delete_plan_file, request_commands
from .stories import (Criterion, Story, StorySet, new_stories_dir, outline_entries, read_story_file,
                      validate_outline, write_story_file)
from .toolcall_text import text_call_error

DEFAULT_STORIES_CAP = 30          # _run_loop iterations per story (/plan stories cap)
STORIES_CAP_RANGE = (5, 100)
APPROVE_MODES = ("each", "once")  # /plan stories approve: every phase's stories, or none
WRITER_CAP = 60                   # iterations of one story-writing pass
STORY_MAX_REFUSALS = 3            # complete_story refusals before the story counts as failed
STORY_NUDGES = 3                  # notes for a story or writing reply without a tool call
MAX_ATTEMPTS = 2                  # failures of a story (and its replacements) before pausing
TURNS_NOTE_AT = 2 / 3             # share of the story cap after which the story gets a turns note
GATE_FIXES = 1                    # fix passes after failing phase checks before pausing
_OUTLINE_CHARS = 1500             # code outline shown per file a dependency changed
_RESULT_CHARS = 1500              # check output kept per criterion for the progress view
_REPORT_PROMPT = (
    "Write the report of a finished phase of a phased plan, for whoever breaks the next phase "
    "into stories. From the stories below and what they delivered, write under these headings, "
    "briefly and concretely, naming files and exact signatures:\n\n"
    "## What exists now\n## Interfaces later phases can use\n## Deviations from the plan\n"
    "## Known gaps\n\nUnder Known gaps, also list the parts of the user's request (when given) "
    "that this phase should have covered and the notes do not show as done. The harness's "
    "record of requirements is the truth: every requirement it marks ✗ goes under Known gaps. "
    "Claim a behaviour only when a passed check or a test name below shows it — 'planned to "
    "provide' is what a story was asked for, not what it proved. Do not invent anything that "
    "is not in the notes.")
_YES = ("y", "yes", "ok", "go", "run", "approve", "approved")
# A check that failed because the server it talks to is not running (run 2's
# P2.S4): the story cannot fix that by changing its code.
_RX_TEST_FILE = re.compile(r"^(?:test_.*|.*_test)\.py$")
_RX_PY = re.compile(r"^(?:\S*/)?python(?:3(?:\.\d+)?)?$")
_RX_NO_TESTS = re.compile(r"^Ran 0 tests|NO TESTS RAN|no tests ran", re.M)
_RX_NO_SERVER = re.compile(r"Connection refused|ConnectionRefusedError|Address already in use")
_SERVER_HINT = ("\n  (the check needs a server: a test starts the server itself, in setUpClass, "
                "and stops it in tearDownClass)")


class StoryRunnerMixin:
    """The stories half of plan execution, used by PlanRunnerMixin._execute_plan."""

    # ── the phase loop ────────────────────────────────────────────────────────

    def _execute_stories_plan(self) -> str:
        """Run the phases of a stories plan in order.  Returns "done" when the
        plan is complete, else why it stopped (the plan stays, paused)."""
        plan = self.plan
        if not plan.stories_dir:
            d = new_stories_dir(self.workdir, plan.title)
            if isinstance(d, str):
                self.event_queue.put(ErrorEvent(d))
                return "error"
            plan.stories_dir = d.relative_to(self.workdir.resolve()).as_posix()
        while (idx := plan.current_index()) is not None:
            step = plan.steps[idx]
            step.status = "in_progress"
            if not step.started_at:
                step.started_at = _now()
            self._sync_plan_file()
            self.emit_status()
            n = len(plan.steps)
            self.event_queue.put(ChatEvent("system", f"▶ Phase {idx + 1}/{n}: {step.title}"))
            outcome = self._run_phase(idx)
            if outcome != "done":
                self._sync_plan_file()
                self.event_queue.put(ChatEvent("system",
                    f"Plan paused in phase {idx + 1}/{n} — edit {self._stories_rel(idx + 1)} if "
                    f"needed, then /plan resume (or any message); /plan cancel to discard."))
                return outcome
            step.status = "done"
            step.finished_at = _now()
            step.note = " ".join(step.report.split())[:200]
            self._sync_plan_file()
            self._autosave()
        self._finish_stories_plan()
        return "done"

    def _finish_stories_plan(self) -> None:
        """Keep the plan file with its stories (the folder stays as history),
        then end the plan as _execute_plan does."""
        plan = self.plan
        folder = self.workdir / plan.stories_dir
        try:
            (folder / "plan.md").write_text(plan.to_markdown(), encoding="utf-8")
        except OSError as e:
            self.event_queue.put(ErrorEvent(f"could not keep the plan in {plan.stories_dir}: {e}"))
        total = len(plan.steps)
        self.plan = None
        self.plan_phase = "investigating"
        self.story_role = None
        delete_plan_file(self.workdir)
        self.rebuild_system_prompt()
        self.event_queue.put(ChatEvent("system",
            f"Plan complete: {plan.title} ({total} phase{'s' if total != 1 else ''}). "
            f"{PLAN_FILENAME} removed; the stories are kept in {plan.stories_dir}/. "
            "Still in coding mode — /plan to plan the next change."))

    def _run_phase(self, idx: int) -> str:
        plan = self.plan
        n = idx + 1
        step = plan.steps[idx]
        fixes = 0
        while True:
            sset = plan.story_sets.get(n)
            if sset is not None:
                edited = read_story_file(self._stories_path(), n)
                if edited is not None:          # the user's edits since it was written
                    sset.merge_from_file(edited)
            if sset is None or not sset.approved:
                outcome = self._write_stories(idx)
                if outcome != "done":
                    return outcome
                continue
            story = sset.next_ready()
            if story is not None:
                outcome = self._run_story(n, story)
                if outcome == "cancelled":
                    return outcome
                if outcome == "blocked":
                    outcome = self._fix_defect(idx, story)
                    if outcome != "done":
                        return outcome
                    continue
                if outcome != "done":
                    outcome = self._story_failed(idx, story, outcome)
                    if outcome != "done":
                        return outcome
                continue
            if not sset.done:
                retry = next((s for s in sset.stories
                              if s.status == "failed" and s.attempts < MAX_ATTEMPTS), None)
                if retry is not None:
                    # Its re-split never got written (e.g. the writer replied with text): again.
                    self.event_queue.put(ChatEvent("system",
                        f"Story {retry.id} failed and was not re-split yet; asking the story "
                        "writer again."))
                    outcome = self._resplit(idx, retry)
                    if outcome != "done":
                        return outcome
                    continue
                failed = [s for s in sset.stories if s.status == "failed"]
                waiting = [s.id for s in sset.blocked()]
                self.event_queue.put(ChatEvent("system",
                    f"Phase {n} cannot go on: {', '.join(s.id for s in failed)} failed "
                    f"{MAX_ATTEMPTS} times" + (f" and {', '.join(waiting)} wait on it" if waiting else "")
                    + f". Edit {self._stories_rel(n)} (mark a story [ ] to retry it), then "
                    "/plan resume."))
                return "paused"
            passed, report = self._phase_checks(step, sset, last=n == len(plan.steps))
            if passed:
                step.report = sset.report = self._phase_report(n)
                self._write_stories_file(sset)
                self.event_queue.put(ChatEvent("system", f"Phase {n} report:\n{step.report}"))
                return "done"
            if fixes >= GATE_FIXES:
                self.event_queue.put(ChatEvent("system",
                    f"Phase {n}'s checks still fail after a fix pass:\n{_tail(report)}"))
                return "paused"
            fixes += 1
            outcome = self._write_stories(idx, extra=(
                "All stories of this phase are done, but the phase's checks fail:\n\n"
                f"{report}\n\nOutline and write NEW stories that fix this (ids after the existing "
                "ones, depending on the stories they build on). Do not repeat the finished stories."),
                append=True)
            if outcome != "done":
                return outcome

    # ── fresh contexts ────────────────────────────────────────────────────────

    def _fresh_context(self) -> None:
        """Start over from the system prompt; the history so far goes to the
        compaction archive (by="story"), so /export full still shows it."""
        msgs = self.messages
        if len(msgs) > 1:
            self.compacted = transcript_mod.archive_compaction(
                self.compacted, msgs, set(range(1, len(msgs))), {}, [0],
                max((r["n"] for r in self.compacted), default=0) + 1,
                datetime.now().isoformat(timespec="seconds"), by="story")
            del msgs[1:]
        self._turn_user = None

    def _enter_role(self, role: str | None) -> None:
        self.story_role = role
        self.rebuild_system_prompt()
        self.emit_status()

    # ── writing stories ───────────────────────────────────────────────────────

    def _write_stories(self, idx: int, extra: str = "", replaces: list[str] | None = None,
                       append: bool = False) -> str:
        """One story-writing pass for phase idx: the writer investigates,
        outlines the stories (plan_stories), writes them one per turn
        (write_story) and submits them (finish_stories) until a set is valid
        and approved.  "done" when the phase has an approved set, else why not."""
        n = idx + 1
        self._fresh_context()
        self._enter_role("story-writer")
        # A re-split or fix pass adds to the phase's stories as they are now: a
        # snapshot, since a submitted set is stored before approval and its
        # drafts must not become "existing" stories when the user sends it
        # back (the story-mud run tripled its stories that way).
        base = self.plan.story_sets.get(n) if (replaces or append) else None
        w = self._writing = {"phase": n, "replaces": replaces or [], "append": append,
                             "adds": bool(replaces or append),
                             "base": [Story.from_dict(s.to_dict()) for s in base.stories]
                                     if base is not None else None,
                             "stored": False, "outline": [], "drafts": {}, "story_turn": -1}
        self._append_user(self._writer_brief(idx, extra))
        self._turn_user = self.messages[-1]
        try:
            outcome = self._run_loop(self._current_tools() if self.tools_enabled else [], WRITER_CAP)
        finally:
            self._writing = None
            self._enter_role(None)
        if w["stored"]:
            return "done"
        if outcome == "cancelled":
            return outcome
        self.event_queue.put(ChatEvent("system",
            f"The story writer stopped without stories for phase {n}: {self._writer_stopped(outcome, w)}. "
            "/plan resume (or any message) asks it again."))
        return "paused" if outcome == "done" else outcome

    def _story_nudge(self, content: str, tools: list[dict]) -> str:
        """The note for a story or writing pass that replied without a tool call."""
        err = text_call_error(content, tools)
        if self._writing:
            what = ("Call the tool itself now, with JSON arguments. " + self._writer_next(self._writing))
        else:
            what = ("A story ends only with complete_story. Make the change with a tool call "
                    "(edit_file / write_file), run the acceptance checks, then call complete_story.")
        return ("[Note from the harness, not the user] Your reply had no tool call, so nothing "
                "happened" + (f": {err}" if err else "") + ". " + what)

    def _writer_stopped(self, outcome: str, w: dict) -> str:
        """Why a writing pass ended without stories, in a few words."""
        if outcome != "done":
            return {"loop": "the loop guard stopped it", "limit": "it ran out of turns"}.get(outcome, outcome)
        last = next((m.get("content") or "" for m in reversed(self.messages)
                     if m.get("role") == "assistant"), "")
        from .tools import registry, ToolContext
        err = text_call_error(last, registry.offered(ToolContext(role="story-writer")))
        what = f"it replied with text instead of calling {_writer_due(w)}"
        return what + " — " + err if err else what

    def _writer_brief(self, idx: int, extra: str = "") -> str:
        plan = self.plan
        n = idx + 1
        step = plan.steps[idx]
        lines = [f"# Write the stories for phase {n}/{len(plan.steps)}: {step.title}", "",
                 f"Plan: {plan.title}"]
        if plan.goal:
            lines += ["", "## Goal of the plan", plan.goal]
        if plan.request:
            # The story-mud run's writers worked from the phase text alone and
            # left out half the requested commands and the join flow.
            lines += ["", "## The user's request", plan.request, "",
                      "Cover every requirement of the request that belongs to this phase. If an "
                      "earlier phase left one unmet that this phase needs, add a story for it, "
                      "or propose a phase_revision when it belongs to a later phase."]
        due = self._due_requirements(n)
        if due:
            lines += ["", "## Requirements this phase must cover"]
            lines += [f"- {r['id']}: {r['text']}" + (f" (phase {r['phase']}, not proven yet)"
                                                    if r["phase"] != n else "") for r in due]
            lines += ["", "Each needs at least one acceptance criterion whose check proves it, "
                          "naming it in covers (e.g. covers: [\"R3\"]); finish_stories refuses "
                          "the set until every one is covered. Test it the way the user meets it: "
                          "a command typed in the page is sent as the page sends it."]
        lines += ["", "## Phases"]
        for i, s in enumerate(plan.steps, 1):
            mark = "▶" if i == n else ("✓" if s.status == "done" else " ")
            lines.append(f"{mark} {i}. {s.title}" + (f" — {s.outcome}" if s.outcome else ""))
        lines += ["", f"## This phase: {step.title}", step.details or "(no details)"]
        if step.outcome:
            lines += ["", f"Outcome: {step.outcome}"]
        if step.checks:
            lines += ["Checks the harness runs when all stories are done:"]
            lines += [f"- `{c}`" for c in step.checks]
        if step.files:
            lines += [f"Files: {', '.join(step.files)}"]
        for i, s in enumerate(plan.steps[:idx], 1):
            if s.report:
                lines += ["", f"## Report of phase {i}: {s.title}", s.report]
        earlier = [(k, s) for k, s in plan.earlier_stories(n).items() if s.status == "done"]
        if earlier:
            lines += ["", "## Stories delivered in earlier phases (depend on them as P<n>.S<m>)"]
            for key, s in earlier:
                lines.append(f"- {key} {s.title}")
                for p in s.provides:
                    lines.append(f"  provides: {p}")
                if s.delivered:
                    lines.append(f"  delivered: {' '.join(s.delivered.split())[:300]}")
        current = plan.story_sets.get(n)
        if current is not None and current.stories:
            lines += ["", "## Stories of this phase so far", current.to_markdown()]
        if extra:
            lines += ["", "## What to do now", extra]
        lines += ["", "Investigate the code this phase builds on, then outline the stories with "
                      "plan_stories, write them one per turn with write_story, and call "
                      "finish_stories."]
        return "\n".join(lines)

    def _writer(self) -> dict | None:
        w = getattr(self, "_writing", None)
        return w if self.story_role == "story-writer" and w else None

    def _writer_kept(self, w: dict) -> list[Story]:
        """The phase's stories a re-split or a fix pass adds to (copies, the
        ones it replaces marked so); [] when the writer writes the whole phase."""
        if not w.get("adds") or w.get("base") is None:
            return []
        kept = [Story.from_dict(s.to_dict()) for s in w["base"]]
        for s in kept:
            if s.id in w["replaces"] and s.status != "done":
                s.status = "replaced"
        return kept

    def _writer_next(self, w: dict) -> str:
        """What the writer is due to do next, for tool results and nudges."""
        outline = w["outline"]
        if not outline:
            return ("Next: once you know the code this phase builds on, outline the stories "
                    "with plan_stories.")
        missing = [e for e in outline if e["id"] not in w["drafts"]]
        if missing:
            e = missing[0]
            deps = f" (depends on {', '.join(e['depends_on'])})" if e["depends_on"] else ""
            return (f"Next: write {e['id']} — {e['title']}{deps} with write_story. Work out only "
                    "this story's details.")
        return (f"All {len(outline)} stories are written. Rewrite any that need it with "
                "write_story, then call finish_stories.")

    def _handle_plan_stories(self, args: dict) -> str:
        w = self._writer()
        if not w:
            return "ERROR: plan_stories is only available while a phase's stories are written."
        n = w["phase"]
        outline = outline_entries(args.get("outline"))
        before = list(w["replaces"])
        err = self._take_replaces(w, args.get("replaces"))
        if err:
            return err
        retired = [k for k in w["replaces"] if k not in before]
        kept = self._writer_kept(w)
        # Finished stories outlined again (a model re-listing the whole phase)
        # are dropped: delivered work is not rewritten.
        done = {s.id for s in kept if s.status == "done"}
        dropped = [e["id"] for e in outline if e["id"] in done]
        outline = [e for e in outline if e["id"] not in done]
        kept_note = (f" ({', '.join(dropped)} {'is' if len(dropped) == 1 else 'are'} done and "
                     "kept as delivered; outline only new stories.)" if dropped else "")
        errors = validate_outline(outline, kept, self.plan.earlier_stories(n))
        if errors:
            w["replaces"] = before              # a refused outline retires nothing
            return ("ERROR: the outline needs changes" + kept_note + ":\n"
                    + "\n".join(f"- {e}" for e in errors)
                    + "\nCall plan_stories again with the corrected outline.")
        w["outline"] = outline
        ids = {e["id"] for e in outline}
        gone = [k for k in w["drafts"] if k not in ids]
        w["drafts"] = {k: v for k, v in w["drafts"].items() if k in ids}
        self.event_queue.put(ChatEvent("system", f"✎ Phase {n} outline: " + ", ".join(
            f"{e['id']} {e['title']}" for e in outline)))
        return (f"Outline stored ({len(outline)} {'story' if len(outline) == 1 else 'stories'})."
                + kept_note
                + (f" Retired {', '.join(retired)} (marked replaced)." if retired else "")
                + (f" Dropped your unsaved drafts of {', '.join(gone)}; stories already in the "
                   "set stay until you retire them with replaces." if gone else "")
                + " " + self._writer_next(w) + self._feedback_reminder(w))

    def _take_replaces(self, w: dict, raw) -> str:
        """plan_stories' replaces: the phase's pending or failed stories the
        writer retires (a feedback round or a re-split could only add stories,
        so the story-mud run ended up with the same three stories three
        times).  "" when taken, else the error for the tool result."""
        ids = [str(x).strip().upper() for x in (raw if isinstance(raw, list) else
                                                 [raw] if isinstance(raw, str) and raw else [])
               if str(x).strip()]
        if not ids:
            return ""
        if not w.get("adds"):
            return ("ERROR: nothing to retire: this pass writes the phase's whole set, so leave "
                    "the stories that should go out of the outline instead of using replaces.")
        stories = {s.id: s for s in (w.get("base") or [])}
        bad = []
        for k in ids:
            s = stories.get(k)
            if s is None:
                bad.append(f"{k}: not a story of this phase yet — leave it out of the outline instead")
            elif s.status in ("done", "in_progress"):
                bad.append(f"{k}: {s.status.replace('_', ' ')} — only pending or failed stories "
                           "can be retired")
        if bad:
            return ("ERROR: replaces needs changes:\n" + "\n".join(f"- {b}" for b in bad)
                    + "\nCall plan_stories again with the corrected replaces.")
        w["replaces"] = w["replaces"] + [k for k in ids if k not in w["replaces"]]
        return ""

    def _feedback_reminder(self, w: dict) -> str:
        """The user's feedback on this pass's stories, repeated on every result
        until finish_stories (the story-mud writer ignored most of five points)."""
        fb = w.get("feedback")
        return f"\nUser feedback to address: {fb}" if fb else ""

    def _handle_write_story(self, args: dict) -> str:
        w = self._writer()
        if not w:
            return "ERROR: write_story is only available while a phase's stories are written."
        if not w["outline"]:
            return ("ERROR: outline the phase first: call plan_stories with one line per story, "
                    "then write them one at a time with write_story.")
        raw = args.get("story")
        if not isinstance(raw, dict) and "id" in args:
            raw = args                          # the story's fields sent at the top level
        if not isinstance(raw, dict):
            return "ERROR: write_story needs a 'story' object. Call it again with the story."
        story = Story.from_dict(raw)
        if w["story_turn"] == w.get("turns"):
            return (f"ERROR: one story per turn — {story.id or 'this story'} was not stored. "
                    "Send it with write_story in your next turn.")
        n = w["phase"]
        kept = self._writer_kept(w)
        if story.id in {s.id for s in kept if s.status == "done"}:
            return (f"ERROR: {story.id} is done and kept as delivered; write only the new stories. "
                    + self._writer_next(w))
        # A title or dependencies left out keep the outline's: the story-mud run's
        # writer outlined "S2 depends on S1" and titles, then sent each story
        # without them, and the set ran untitled with no dependencies.
        inherited = []
        was = next((e for e in w["outline"] if e["id"] == story.id), None)
        if was is not None:
            if not str(raw.get("title") or "").strip() and was["title"] != "(untitled story)":
                story.title = was["title"]
                inherited.append("title")
            if not story.depends_on and was["depends_on"]:
                story.depends_on = list(was["depends_on"])
                inherited.append(f"dependencies ({', '.join(was['depends_on'])})")
        # The story's own title and dependencies update its outline line (or add one).
        entry = {"id": story.id, "title": story.title, "depends_on": story.depends_on,
                 "provides": "; ".join(story.provides)}
        outline = [entry if e["id"] == story.id else e for e in w["outline"]]
        if all(e["id"] != story.id for e in outline):
            outline.append(entry)
        earlier = self.plan.earlier_stories(n)
        ids = {e["id"] for e in outline} | {s.id for s in kept if s.status != "replaced"}
        planned = ({f for d in w["drafts"].values() for f in d.files}
                   | {f for k in kept if k.status != "replaced" for f in k.files} | set(story.files))
        errors = (validate_outline(outline, kept, earlier)
                  + story.problems(ids, earlier, self.workdir, planned))
        if errors:
            return (f"ERROR: {story.id or 'the story'} needs changes before it is stored:\n"
                    + "\n".join(f"- {e}" for e in errors)
                    + "\nCall write_story again with the corrected story.")
        rewritten = story.id in w["drafts"]
        w["story_turn"] = w.get("turns")
        w["outline"] = outline
        w["drafts"][story.id] = story
        k = sum(e["id"] in w["drafts"] for e in outline)
        self.event_queue.put(ChatEvent("system",
            f"✎ P{n}.{story.id} {'rewritten' if rewritten else 'drafted'} — {story.title} "
            f"({k}/{len(outline)})"))
        after = ("" if k == len(outline) else
                 " If writing it showed that an earlier story must change (what it provides, its "
                 "dependencies, its scope), rewrite that one with write_story first.")
        kept_note = (f" Kept the outline's {' and '.join(inherited)} for {story.id}."
                     if inherited else "")
        return (f"{'Replaced the draft of' if rewritten else 'Stored'} {story.id} "
                f"({k}/{len(outline)} written).{kept_note}{after} {self._writer_next(w)}"
                + self._feedback_reminder(w))

    def _handle_finish_stories(self, args: dict) -> str:
        w = self._writer()
        if not w:
            return "ERROR: finish_stories is only available while a phase's stories are written."
        if not w["outline"]:
            return ("ERROR: nothing to submit yet: outline the stories with plan_stories, then "
                    "write each one with write_story.")
        missing = [e["id"] for e in w["outline"] if e["id"] not in w["drafts"]]
        if missing:
            return (f"ERROR: not every story is written yet: {', '.join(missing)} still "
                    f"need{'s' if len(missing) == 1 else ''} write_story (or leave "
                    f"{'it' if len(missing) == 1 else 'them'} out of the outline with plan_stories). "
                    + self._writer_next(w))
        new = [Story.from_dict(w["drafts"][e["id"]].to_dict()) for e in w["outline"]]
        return self._submit_stories(w, new, args.get("phase_revision"))

    def _submit_stories(self, w: dict, new: list[Story], revision) -> str:
        """Validate the written stories as the phase's set (or, for a re-split
        or a fix pass, added to the phase's stories so far), store it and ask
        for approval."""
        plan = self.plan
        n = w["phase"]
        kept = self._writer_kept(w)
        if kept:
            # Replacements inherit the failed story's attempts, so failing
            # again pauses the plan.
            failed = next((s for s in kept if w["replaces"] and s.id == w["replaces"][0]), None)
            for s in new:
                s.attempts = max(s.attempts, failed.attempts if failed else 0)
        sset = StorySet(phase=n, title=plan.steps[n - 1].title, stories=kept + new)
        errors = sset.validate(plan.earlier_stories(n), self.workdir) + self._coverage_problems(n, sset)
        if errors:
            return ("ERROR: the stories need changes before they can run:\n"
                    + "\n".join(f"- {e}" for e in errors)
                    + "\nFix them with write_story (or the outline with plan_stories), then call "
                      "finish_stories again.")
        sset.approved = False
        plan.story_sets[n] = sset
        err = self._write_stories_file(sset)
        if err:
            return f"ERROR: {err}"
        answer = self._approve_stories(n, sset, revision)
        if answer:
            return answer                     # feedback, or an invalid hand edit
        w["stored"] = True
        self._step_summary = f"{len([s for s in sset.stories if not s.finished])} stories for phase {n}"
        return (f"Stories approved for phase {n} ({sset.progress()} done). The harness runs them "
                "now, each in its own conversation. Do not call any more tools.")

    def _approve_stories(self, n: int, sset: StorySet, revision) -> str:
        """Ask the user per the setting; '' once approved, else the tool result
        that sends the writer back (feedback, or errors in a hand edit)."""
        self.event_queue.put(ChatEvent("assistant", sset.to_markdown()))
        if isinstance(revision, dict) and isinstance(revision.get("phases"), list):
            if not self._apply_phase_revision(n, revision):
                note = "The user declined the change to the later phases; they stay as they were."
                self.event_queue.put(ChatEvent("system", note))
        if self.stories_approve == "each":
            answer = self._ask_user(
                f"Run these {len(sset.stories)} stories for phase {n}? y = run them · anything "
                f"else = feedback for the story writer. You can edit {self._stories_rel(n)} "
                "before answering.").strip()
            if answer.lower().rstrip(".!") not in _YES:
                w = self._writer()
                if w is not None:
                    w["feedback"] = answer
                return (f"User requested changes to the stories: {answer}\nInvestigate further if "
                        "needed, rewrite the stories it concerns with write_story (or the outline "
                        "with plan_stories; retire stories that must go with its replaces), then "
                        "call finish_stories again.")
            edited = read_story_file(self._stories_path(), n)
            if edited is not None:
                sset.merge_from_file(edited)
                errors = sset.validate(self.plan.earlier_stories(n), self.workdir)
                if errors:
                    w = self._writer()
                    if w:                       # the drafts take the user's edits
                        for s in sset.stories:
                            if s.id in w["drafts"]:
                                w["drafts"][s.id] = Story.from_dict(s.to_dict())
                    return ("ERROR: the user's edits to the stories file left problems:\n"
                            + "\n".join(f"- {e}" for e in errors)
                            + "\nFix them with write_story, keeping the user's changes, then call "
                              "finish_stories again.")
        sset.approved = True
        self._write_stories_file(sset)
        self._sync_plan_file()
        return ""

    def _apply_phase_revision(self, n: int, revision: dict) -> bool:
        from .plan import Step as _Step
        phases = [_Step.from_dict(p) for p in revision["phases"] if isinstance(p, dict)]
        if not phases:
            return False
        shown = "\n".join(f"{n + i}. {p.title} — {p.outcome}" for i, p in enumerate(phases, 1))
        answer = self._ask_user(
            f"The story writer proposes changing the phases after phase {n}:\n"
            f"{revision.get('reason', '')}\n\n{shown}\n\nApply this change? y / n").strip()
        if answer.lower().rstrip(".!") not in _YES:
            return False
        self.plan.steps = self.plan.steps[:n] + phases
        self._sync_plan_file()
        return True

    # ── running one story ─────────────────────────────────────────────────────

    def _run_story(self, n: int, story: Story) -> str:
        """Run one story in a fresh context.  "done" when complete_story
        accepted it, else why not (the failure reason is in _story_failure)."""
        sset = self.plan.story_sets[n]
        story.status = "in_progress"
        story.started_at, story.finished_at = _now(), ""
        story.turns = story.refusals = 0
        self._write_stories_file(sset)
        self._fresh_context()
        self._enter_role("story-exec")
        # "▶ P1.S3 — …": the web UI links this line and the story's row both ways.
        self.event_queue.put(ChatEvent("system",
            f"▶ P{n}.{story.id} — {story.title} ({sset.progress()} done in phase {n})"))
        self.checkpoints.begin_turn(self._turn_count, story.title, step=f"P{n} {story.id}",
                                    plan={"plan": self.plan.title, "phase": n, "story": story.id})
        self._story = {"phase": n, "id": story.id, "refusals": 0, "done": False,
                       "failure": "", "defect": "", "start": len(self.messages)}
        self._append_user(self._story_brief(n, story))
        self._turn_user = self.messages[-1]
        try:
            outcome = self._run_loop(self._current_tools() if self.tools_enabled else [],
                                     self.stories_cap)
        finally:
            state = self._story
            self._story = None
            story.finished_at = _now()
            self._enter_role(None)
        if state["done"]:
            return "done"
        if state["defect"]:
            story.status, story.defect = "pending", state["defect"]
            self._write_stories_file(sset)
            return "blocked"
        if outcome == "cancelled":
            story.status = "pending"
            self._write_stories_file(sset)
            return outcome
        if outcome == "done" and not state["failure"] and self._accept_text_end(n, story, state):
            return "done"
        self._story_failure = state["failure"] or self._last_words(outcome)
        return outcome if outcome != "done" else "not_done"

    def _last_words(self, outcome: str) -> str:
        last = next((m.get("content") for m in reversed(self.messages)
                     if m.get("role") == "assistant" and m.get("content")), "")
        why = {"loop": "the loop guard stopped it", "limit": "it ran out of iterations",
               "done": "it ended without complete_story"}.get(outcome, outcome)
        return f"The story did not finish: {why}." + (f" Its last words:\n{last}" if last else "")

    def _story_brief(self, n: int, story: Story) -> str:
        plan = self.plan
        step = plan.steps[n - 1]
        lines = [f"# Story {story.id} of phase {n}: {story.title}", "",
                 f"Plan: {plan.title} — phase {n}/{len(plan.steps)}: {step.title}"]
        if step.outcome:
            lines.append(f"The phase delivers: {step.outcome}")
        brief = Story.from_dict({**story.to_dict(), "delivered": "", "facts": {}, "attempts": 0})
        lines += ["", brief.to_markdown().split("\n", 1)[1]]
        deps = [(d, self._dependency(n, d)) for d in story.depends_on]
        deps = [(d, s) for d, s in deps if s is not None]
        if deps:
            lines += ["", "## What the stories this builds on delivered"]
            for key, s in deps:
                lines += ["", f"### {key}: {s.title}"]
                lines += [f"- provides: {p}" for p in s.provides]
                if s.delivered:
                    lines.append(f"- delivered: {s.delivered}")
                for f in (s.facts.get("files") or [])[:4]:
                    outline = code_nav.code_outline(f, workdir=self.workdir)
                    if not outline.startswith("ERROR"):
                        if len(outline) > _OUTLINE_CHARS:
                            outline = outline[:_OUTLINE_CHARS] + "\n…"
                        lines += ["", f"Current outline of {f}:", "```", outline, "```"]
        lines += ["", "Implement this story, run its acceptance checks, then call complete_story."]
        return "\n".join(lines)

    def _dependency(self, n: int, ref: str) -> Story | None:
        if ref.startswith("P"):
            return self.plan.earlier_stories(n).get(ref)
        sset = self.plan.story_sets.get(n)
        return sset.get(ref) if sset else None

    def _accept_text_end(self, n: int, story: Story, state: dict) -> bool:
        """A story that ended with text instead of complete_story: run its checks
        and accept it when every criterion has a check and all of them pass (the
        story-mud run failed a story whose own run of its check had passed, only
        because it summed up in text).  A criterion backed by evidence alone
        still needs complete_story."""
        if not story.acceptance or any(not c.check for c in story.acceptance):
            return False
        failures, passed, results = self._run_story_checks(story, [], state["start"])
        story.check_results = results
        if failures:
            return False
        last = next((m.get("content") for m in reversed(self.messages[state["start"]:])
                     if m.get("role") == "assistant" and m.get("content")), "")
        story.status = "done"
        story.delivered = " ".join(str(last).split())[:600]
        story.facts = self._story_facts(state["start"], passed)
        self._write_stories_file(self.plan.story_sets[n])
        self._sync_plan_file()
        self._step_summary = story.delivered or f"Story {story.id} done."
        self.event_queue.put(ChatEvent("system",
            f"Story {story.id} ended without complete_story; its checks pass — accepted."))
        return True

    def _run_story_checks(self, story: Story, evidence: list,
                          start: int | None = None) -> tuple[list[str], list[str], list[dict]]:
        """Run a story's acceptance checks: (failure reports, the checks that
        passed, a result per criterion).  A criterion without a check passes on
        matching evidence ({criterion, how}), else its result is None.  With
        start (where the story's messages begin), the test files the story
        wrote that no check runs are run too (_own_tests)."""
        failures, passed, results = [], [], []
        for c in story.acceptance:
            if c.check:
                self.event_queue.put(ToolCallEvent("story_check", {"command": c.check}))
                ok, out = tdd_mod.run_tests(c.check, self.workdir, self._cancel)
                self.event_queue.put(ToolResultEvent("story_check", out))
                results.append({"passed": ok, "output": out[-_RESULT_CHARS:], "at": _now()})
                if ok:
                    passed.append(c.check)
                else:
                    failures.append(f"✗ {c.text}\n  `{c.check}`\n{_indent(_tail(out))}"
                                    + (_SERVER_HINT if _RX_NO_SERVER.search(out) else ""))
            else:
                how = next((str(e.get("how") or "").strip() for e in evidence if isinstance(e, dict)
                            and str(e.get("how") or "").strip()
                            and _same(str(e.get("criterion") or ""), c.text)), "")
                results.append({"passed": True if how else None, "output": how, "at": _now()})
        if start is not None:
            for path, cmd, problem in self._own_tests(story, start):
                if problem:
                    failures.append(f"✗ {path}: {problem}")
                    continue
                self.event_queue.put(ToolCallEvent("story_check", {"command": cmd}))
                ok, out = tdd_mod.run_tests(cmd, self.workdir, self._cancel)
                self.event_queue.put(ToolResultEvent("story_check", out))
                if ok and not _RX_NO_TESTS.search(out):
                    passed.append(cmd)
                else:
                    failures.append(f"✗ the story's own tests in {path} pass (no check ran them)\n"
                                    f"  `{cmd}`\n{_indent(_tail(out))}")
        return failures, passed, results

    def _own_tests(self, story: Story, start: int) -> list[tuple[str, str, str]]:
        """The test files this story wrote that none of its checks runs, as
        (path, the command that runs it, ""), or (path, "", the problem) for a
        test file outside the story's files and any tests folder (run 2's P2.S1
        put its tests in a file its check never ran; a stray root test_server.py
        stayed behind)."""
        checks = [c.check for c in story.acceptance if c.check]
        runner = next((a for c in checks for a in _argv(c) if _RX_PY.match(a)), "python3")
        pytest = any("pytest" in c for c in checks)
        out = []
        for path in self._story_facts(start, [])["files"]:
            pp = Path(path)
            if not _RX_TEST_FILE.match(pp.name) or pp.suffix != ".py" \
                    or not (self.workdir / pp).is_file():
                continue
            if not {"test", "tests"} & set(pp.parts[:-1]) and path not in story.files:
                out.append((path, "", "a test file outside the story's files and the tests "
                                      "folder — move it into tests/ or delete it"))
            elif not any(_runs(c, path) for c in checks):
                out.append((path, f"{runner} -m pytest -q {path}" if pytest
                            else f"{runner} -m unittest {path}", ""))
        return out

    def _handle_complete_story(self, args: dict) -> str:
        st = getattr(self, "_story", None)
        if self.story_role != "story-exec" or not st:
            return "ERROR: complete_story is only available while a story runs."
        sset = self.plan.story_sets[st["phase"]]
        story = sset.get(st["id"])
        evidence = args.get("evidence") if isinstance(args.get("evidence"), list) else []
        failures, passed, results = self._run_story_checks(story, evidence, st["start"])
        story.check_results = results
        unbacked = [c.text for c, r in zip(story.acceptance, results)
                    if not c.check and r["passed"] is None]
        if unbacked and not failures:
            failures += [f"✗ {t}\n  no evidence: add {{criterion, how}} to 'evidence'" for t in unbacked]
        if failures:
            st["refusals"] += 1
            story.refusals = st["refusals"]
            self.emit_status()
            report = "\n".join(failures)
            if st["refusals"] >= STORY_MAX_REFUSALS:
                st["failure"] = f"Its acceptance checks failed {st['refusals']} times:\n{report}"
                self._step_summary = ""
                return ("ERROR: story not complete — the checks failed again:\n" + report
                        + "\nThe harness stops this story here. Do not call any more tools.")
            return ("ERROR: story not complete — these acceptance criteria fail:\n" + report
                    + "\nFix this, then call complete_story again.")
        story.status = "done"
        story.delivered = str(args.get("summary") or "").strip()
        story.facts = self._story_facts(st["start"], passed)
        self._write_stories_file(sset)
        self._sync_plan_file()
        st["done"] = True
        self._step_summary = story.delivered or f"Story {story.id} done."
        return "Story accepted: its checks pass."

    def _story_facts(self, start: int, passed: list[str]) -> dict:
        """What the story changed, as far as the harness knows it."""
        from .compaction import _paired_calls
        from .tools import registry
        from .tools.dispatcher import written_paths
        files, definitions = [], []
        for name, args, result in _paired_calls(self.messages[start:]):
            if result is None or result.startswith("ERROR") or not isinstance(args, dict):
                continue
            tool = registry.get(name)
            if name == "edit_files":
                from .tools import edit_paths
                paths = edit_paths(args)
            else:
                paths = written_paths(tool, args) if tool is not None else []
            files += [p for p in paths if p not in files and not p.startswith(".momo/")]
            definitions += [q for p, q in _owners(name, args, result)
                            if q and q not in definitions]
        return {"files": files, "definitions": definitions, "checks": passed}

    def _handle_report_defect(self, args: dict) -> str:
        """The story's checks show a bug in code an earlier story wrote, too big
        to fix within this story: end the story without failing it; the harness
        has a fix story written (run 2's P2.S9 used up its 30 turns on a login
        bug in engine.py it took to be out of its scope)."""
        st = getattr(self, "_story", None)
        if self.story_role != "story-exec" or not st:
            return "ERROR: report_defect is only available while a story runs."
        problem = " ".join(str(args.get("problem") or "").split())
        if not problem:
            return ("ERROR: report_defect needs 'problem': what is wrong in the earlier code "
                    "and how it shows.")
        story = self.plan.story_sets[st["phase"]].get(st["id"])
        where = str(args.get("file") or "").strip()
        evidence = str(args.get("evidence") or "").strip()
        report = (f"{where}: " if where else "") + problem + (
            f"\nEvidence:\n{_tail(evidence, 15)}" if evidence else "")
        self._step_summary = ""                 # ends this story's loop
        if story.defect:                        # one fix story per story; then it fails as usual
            st["failure"] = (f"It reported a second defect after a fix story for the first:\n"
                             f"{report}")
            return ("This story already had a fix story written for a defect; the harness "
                    "stops it here as not done. Do not call any more tools.")
        st["defect"] = report
        return ("Defect reported. The harness has a story written that fixes it; this story "
                "runs again after that, from the start. Do not call any more tools.")

    def _fix_defect(self, idx: int, story: Story) -> str:
        """A story reported a defect in earlier code: a writer pass adds one fix
        story, and the blocked story waits for it."""
        n = idx + 1
        sset = self.plan.story_sets[n]
        before = {s.id for s in sset.stories}
        self.event_queue.put(ChatEvent("system",
            f"Story {story.id} is blocked by a defect in earlier code; asking the story writer "
            f"for a fix story:\n{_tail(story.defect)}"))
        nxt = max((s.number for s in sset.stories), default=0) + 1
        outcome = self._write_stories(idx, append=True, extra=(
            f"Story {story.id} ({story.title}) is blocked by a defect in code an earlier story "
            f"wrote:\n\n{story.defect}\n\nLook at that code and write ONE story, S{nxt}, that "
            f"fixes it and adds a regression test proving the fix. Do not change {story.id} or "
            f"the finished stories: {story.id} runs again once S{nxt} is done."))
        if outcome != "done":
            return outcome
        sset = self.plan.story_sets[n]
        added = [s.id for s in sset.stories if s.id not in before and not s.finished]
        blocked = sset.get(story.id)
        if blocked is not None and added:
            blocked.depends_on += [a for a in added if a not in blocked.depends_on]
            self._write_stories_file(sset)
        return "done"

    def _turns_note(self, iteration: int) -> None:
        """Once a story has used TURNS_NOTE_AT of its turns: tell it how many
        are left and that a bug outside the story is in scope, on the last tool
        result (no extra turn)."""
        st = self._story
        cap = self.stories_cap
        if st.get("turns_noted") or iteration < max(2, int(cap * TURNS_NOTE_AT)):
            return
        if not self.messages or self.messages[-1].get("role") != "tool":
            return
        st["turns_noted"] = True
        self.messages[-1]["content"] = (
            f"{self.messages[-1].get('content') or ''}\n\n[Note from the harness, not the user] "
            f"{cap - iteration + 1} turns left for this story. If a bug in code an earlier story "
            "wrote is what keeps the checks failing, fix it when it is small, or call "
            "report_defect.")

    def _story_failed(self, idx: int, story: Story, outcome: str) -> str:
        """A story did not finish: re-split it once, else pause for the user."""
        n = idx + 1
        sset = self.plan.story_sets[n]
        story.attempts += 1
        story.status = "failed"
        story.failure = getattr(self, "_story_failure", "") or outcome
        self._write_stories_file(sset)
        if story.attempts >= MAX_ATTEMPTS:
            self.event_queue.put(ChatEvent("system",
                f"Story {story.id} failed again ({story.attempts} attempts):\n{_tail(story.failure)}\n"
                f"Edit it in {self._stories_rel(n)} (mark it [ ] to retry), or /plan cancel."))
            return "paused"
        self.event_queue.put(ChatEvent("system",
            f"Story {story.id} failed; asking the story writer to re-split it."))
        return self._resplit(idx, story)

    def _resplit(self, idx: int, story: Story) -> str:
        """Have the writer replace a failed story and its pending dependents."""
        sset = self.plan.story_sets[idx + 1]
        ids = [story.id] + _dependents(sset, story.id)
        nxt = max((s.number for s in sset.stories), default=0) + 1
        return self._write_stories(idx, replaces=ids, extra=(
            f"Story {story.id} ({story.title}) failed:\n\n{story.failure or 'no reason recorded'}\n\n"
            f"Look at the code as it is now and replace {', '.join(ids)} with smaller or "
            f"corrected stories that avoid this. Outline and write ONLY the replacement "
            f"stories, with new ids from S{nxt} on — the finished stories stay as they are. "
            f"If other pending stories must go too (duplicates, or work the replacements "
            f"now cover), retire them with plan_stories' replaces."))

    # ── the phase gate ────────────────────────────────────────────────────────

    def _phase_checks(self, step: Step, sset: StorySet, last: bool = False) -> tuple[bool, str]:
        """Run the phase's checks (stopping at the first that fails); the result
        is kept on the story set for the progress view.  The last phase also
        runs the commands the user's request says must pass, as written, and
        fails while a requirement has no passing check (run 2 ended "complete"
        with plain `python -m unittest` finding no tests, and n/s/e/w missing)."""
        cmds = list(step.checks)
        if last:
            cmds += [c for c in request_commands(self.plan.request) if c not in cmds]
        out = []
        for cmd in cmds:
            self.event_queue.put(ToolCallEvent("phase_check", {"command": cmd}))
            ok, text = tdd_mod.run_tests(cmd, self.workdir, self._cancel)
            self.event_queue.put(ToolResultEvent("phase_check", text))
            if _RX_NO_TESTS.search(text):
                ok, text = False, (text + "\n(no tests ran: a test command that finds no tests "
                                          "proves nothing — unittest discovers tests/ only when "
                                          "it has an __init__.py)")
            if not ok:
                report = f"`{cmd}` failed:\n{_tail(text)}"
                sset.gate = {"passed": False, "command": cmd, "output": text[-_RESULT_CHARS:],
                             "at": _now()}
                return False, report
            out.append(f"`{cmd}` passes")
        gaps = self._requirement_gaps(len(self.plan.steps) + 1) if last else []
        if gaps:
            report = ("These requirements of the user's request have no passing check:\n"
                      + "\n".join(f"- {r['id']}: {r['text']}" for r in gaps))
            sset.gate = {"passed": False, "command": "", "output": report, "at": _now()}
            return False, report
        if not out:
            out.append("(the phase has no checks)")
        sset.gate = {"passed": True, "output": "\n".join(out), "at": _now()}
        return True, sset.gate["output"]

    # ── requirements ──────────────────────────────────────────────────────────

    def _coverage(self, upto: int | None = None) -> dict[str, list[tuple[str, Criterion, bool | None]]]:
        """Each requirement id → the criteria with a check that cover it, as
        (P<n>.S<m>, criterion, passed: True/False/None = not run), in phases
        before `upto` (all when None); replaced stories do not count."""
        out: dict[str, list] = {}
        for n, sset in sorted(self.plan.story_sets.items()):
            if upto is not None and n >= upto:
                continue
            for s in sset.stories:
                if s.status == "replaced":
                    continue
                for i, c in enumerate(s.acceptance):
                    if not c.check:
                        continue
                    r = s.check_results[i] if i < len(s.check_results) else {}
                    passed = r.get("passed") if s.status == "done" else None
                    for rid in c.covers:
                        out.setdefault(rid, []).append((f"P{n}.{s.id}", c, passed))
        return out

    def _requirement_gaps(self, upto: int) -> list[dict]:
        """The requirements of phases before `upto` that no passing check proves."""
        cov = self._coverage(upto)
        return [r for r in self.plan.requirements if r["phase"] < upto
                and not any(passed for _w, _c, passed in cov.get(r["id"], []))]

    def _due_requirements(self, n: int) -> list[dict]:
        """What phase n's stories must cover: its own requirements, and earlier
        phases' that no passing check proves yet."""
        gaps = {r["id"] for r in self._requirement_gaps(n)}
        return [r for r in self.plan.requirements if r["phase"] == n or r["id"] in gaps]

    def _coverage_problems(self, n: int, sset: StorySet) -> list[str]:
        """A submitted set's requirement errors: a due requirement no live
        criterion with a check covers, or a covers id that is no requirement."""
        if not self.plan.requirements:
            return []
        ids = {r["id"] for r in self.plan.requirements}
        covered, errors = set(), []
        for s in sset.stories:
            if s.status == "replaced":
                continue
            for c in s.acceptance:
                unknown = [x for x in c.covers if x not in ids]
                if unknown and not s.finished:
                    errors.append(f"{s.id}: covers {', '.join(unknown)}, which "
                                  f"{'is' if len(unknown) == 1 else 'are'} not among the "
                                  f"requirements ({', '.join(sorted(ids, key=_rnum))})")
                if c.check:
                    covered |= set(c.covers)
        for r in self._due_requirements(n):
            if r["id"] not in covered:
                errors.append(f"{r['id']} ({r['text']}) is not covered — give a story an "
                              f"acceptance criterion with a check that proves it and covers: "
                              f"[\"{r['id']}\"]")
        return errors

    def _requirement_table(self, upto: int) -> str:
        """The requirements of phases up to `upto` and what proves them, for the
        phase report: written by the harness, not the model."""
        cov = self._coverage(upto + 1)
        lines = []
        for r in self.plan.requirements:
            if r["phase"] > upto:
                continue
            proof = [(w, c) for w, c, passed in cov.get(r["id"], []) if passed]
            if proof:
                w, c = proof[0]
                lines.append(f"- ✓ {r['id']} {r['text']} — {w}: `{c.check}`")
            else:
                lines.append(f"- ✗ {r['id']} {r['text']} — no passing check")
        return "\n".join(lines)

    def _phase_report(self, n: int) -> str:
        """The phase's report for the next writer: written by the model from
        the stories' notes, or put together from them when that fails."""
        sset = self.plan.story_sets[n]
        notes = []
        for s in sset.stories:
            if s.status != "done":
                continue
            tests = _test_names(self.workdir, s.facts.get("files") or [])
            notes.append(f"### {s.id}: {s.title}\nplanned to provide: {'; '.join(s.provides) or '-'}\n"
                         f"delivered: {s.delivered or '-'}\n"
                         f"files: {', '.join(s.facts.get('files') or []) or '-'}\n"
                         f"definitions: {', '.join(s.facts.get('definitions') or []) or '-'}\n"
                         f"checks passed: {'; '.join(s.facts.get('checks') or []) or '-'}"
                         + (f"\ntests: {', '.join(tests)}" if tests else ""))
        step = self.plan.steps[n - 1]
        body = f"Phase {n}: {step.title}\nOutcome: {step.outcome}\n\n" + "\n\n".join(notes)
        table = self._requirement_table(n)
        if table:
            body = f"Requirements and what proves them (the harness's record):\n{table}\n\n" + body
        if self.plan.request:
            body = f"The user's request:\n{self.plan.request}\n\n" + body
        try:
            self.client.max_output_tokens = self.max_output_tokens or None
            response = self.client.chat(
                [{"role": "system", "content": _REPORT_PROMPT}, {"role": "user", "content": body}],
                [], think=False, num_ctx=self.model_max_ctx or self.context_limit)
            self._turn_stats["eval"] += response.eval_tokens or 0
            from .toolcall_text import _extract_and_strip_thinking
            text, _ = _extract_and_strip_thinking(response.content or "")
            if text and text.strip():
                return text.strip() + (f"\n\n## Requirements\n{table}" if table else "")
        except Exception:
            pass
        return "\n\n".join(notes) + (f"\n\n## Requirements\n{table}" if table else "")

    # ── the progress view ─────────────────────────────────────────────────────

    def _story_tick(self, iteration: int) -> None:
        """One _run_loop turn of a story or a story-writing pass: count it and
        let the frontends refresh (the status line shows the turn)."""
        if self._story:
            self._story["turns"] = iteration
            sset = self.plan.story_sets.get(self._story["phase"])
            story = sset.get(self._story["id"]) if sset else None
            if story is not None:
                story.turns = iteration
            self._turns_note(iteration)
        elif self._writing:
            self._writing["turns"] = iteration
        self.emit_status()

    def plan_view(self) -> dict | None:
        """The plan as the web UI's timeline shows it: every step (a stories
        plan's phases, with their stories), how far each got, and the node
        being worked on.  None without a plan."""
        plan = self.plan
        if plan is None:
            return None
        current = None
        if self._story:
            current = {"step": self._story["phase"], "story": self._story["id"],
                       "turns": self._story.get("turns", 0)}
        elif self._writing:
            current = {"step": self._writing["phase"], "writing": True,
                       "turns": self._writing.get("turns", 0)}
        elif self.plan_phase == "executing" and (idx := plan.current_index()) is not None:
            current = {"step": idx + 1, "turns": plan.steps[idx].turns}
        steps = []
        for n, step in enumerate(plan.steps, 1):
            sset = plan.story_sets.get(n) if plan.stories else None
            steps.append({
                "n": n, "title": step.title, "status": step.status, "stage": step.stage,
                "tag": step.tag, "details": step.details, "files": step.files, "note": step.note,
                "tests": step.tests, "outcome": step.outcome, "checks": step.checks,
                "report": step.report, "started_at": step.started_at,
                "finished_at": step.finished_at, "turns": step.turns,
                "gate": sset.gate if sset else {},
                "approved": bool(sset and sset.approved),
                "writing": bool(current and current.get("writing") and current["step"] == n),
                "progress": sset.progress() if sset else "",
                "stories": [_story_view(sset, s) for s in sset.stories] if sset else []})
        return {"kind": plan.kind, "title": plan.title, "goal": plan.goal,
                "phase": self.plan_phase, "current": current, "steps": steps,
                "stories_dir": plan.stories_dir, "approve": self.stories_approve,
                "cap": self.stories_cap}

    # ── files ─────────────────────────────────────────────────────────────────

    def _stories_path(self) -> Path:
        return self.workdir / self.plan.stories_dir

    def _stories_rel(self, n: int) -> str:
        return f"{self.plan.stories_dir}/phase-{n}.md"

    def _write_stories_file(self, sset: StorySet) -> str:
        err = write_story_file(self._stories_path(), sset)
        if err:
            self.event_queue.put(ErrorEvent(err))
        return err


def _writer_due(w: dict) -> str:
    """The tool a writing pass was due to call next."""
    if not w["outline"]:
        return "plan_stories"
    if any(e["id"] not in w["drafts"] for e in w["outline"]):
        return "write_story"
    return "finish_stories"


def _dependents(sset: StorySet, story_id: str) -> list[str]:
    """Pending stories that depend on story_id, directly or not."""
    out: list[str] = []
    frontier = [story_id]
    while frontier:
        cur = frontier.pop()
        for s in sset.stories:
            if s.status == "pending" and cur in s.depends_on and s.id not in out:
                out.append(s.id)
                frontier.append(s.id)
    return out


def _story_view(sset: StorySet, s: Story) -> dict:
    """One story for the plan view: what it is, and how its last run went."""
    return {"id": s.id, "title": s.title, "status": s.status, "description": s.description,
            "technical_details": s.technical_details, "hints": s.hints, "pitfalls": s.pitfalls,
            "provides": s.provides, "files": s.files, "delivered": s.delivered,
            "facts": s.facts, "depends_on": s.depends_on,
            "dependents": _dependents_any(sset, s.id), "attempts": s.attempts,
            "turns": s.turns, "refusals": s.refusals, "started_at": s.started_at,
            "finished_at": s.finished_at,
            "criteria": [{"text": c.text, "check": c.check,
                          "result": s.check_results[i] if i < len(s.check_results) else None}
                         for i, c in enumerate(s.acceptance)]}


def _dependents_any(sset: StorySet, story_id: str) -> list[str]:
    """Stories of the set that list story_id among their dependencies (any status)."""
    return [s.id for s in sset.stories if story_id in s.depends_on]


def _same(a: str, b: str) -> bool:
    a, b = " ".join(a.lower().split()), " ".join(b.lower().split())
    return bool(a) and (a == b or a in b or b in a)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _tail(text: str, n: int = 25) -> str:
    return "\n".join(text.splitlines()[-n:])


def _indent(text: str) -> str:
    return "\n".join(f"  {ln}" for ln in text.splitlines())


def _argv(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _runs(check: str, path: str) -> bool:
    """Whether a check command runs the tests in path: it names the file, its
    module or its folder, discovers tests, or runs the whole suite."""
    argv = _argv(check)
    module = path.removesuffix(".py").replace("/", ".")
    folder = str(Path(path).parent)
    if path in check or module in argv or "discover" in argv:
        return True
    if any(a.rstrip("/") == folder for a in argv if folder != "."):
        return True
    for i, a in enumerate(argv[:-1]):
        if a == "-m" and argv[i + 1] in ("unittest", "pytest"):
            return not [x for x in argv[i + 2:] if not x.startswith("-")]
    return argv[:1] == ["pytest"] and not [x for x in argv[1:] if not x.startswith("-")]


def _rnum(rid: str) -> int:
    return int(rid[1:]) if rid[1:].isdigit() else 0


def _test_names(workdir: Path, files: list[str], limit: int = 20) -> list[str]:
    """The test functions and methods (test_*) in the test files among files."""
    import ast
    names = []
    for f in files:
        if not _RX_TEST_FILE.match(Path(f).name):
            continue
        try:
            tree = ast.parse((workdir / f).read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        names += [node.name for node in ast.walk(tree)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.name.startswith("test")]
    return names[:limit] + ([f"… {len(names) - limit} more"] if len(names) > limit else [])
