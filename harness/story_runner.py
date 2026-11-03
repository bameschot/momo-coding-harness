"""Running a stories plan (/plan stories): the Harness methods behind
plan_stories / write_story / finish_stories / complete_story and the phase
loop (see stories.py).

Each phase runs in three parts, each in a fresh context (the history before
it goes to the compaction archive, marked by="story", so /export full keeps
it): the story writer breaks the phase into stories against the code as it is
now (an outline first, then one story per turn, so no single turn has to work
out the whole set); the stories run one at a time, in dependency order, each
accepted once an independent verifier finds its expected behaviour; then the
phase gate verifies the phase's own expectations and re-runs every saved
acceptance test, and a phase report is written for the next phase's writer.
A story that fails is re-split by the writer (_amend_stories); one that fails
again pauses the plan for the user."""
from __future__ import annotations

import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

from . import code_nav
from . import tdd as tdd_mod
from . import transcript as transcript_mod
from .events import ChatEvent, ErrorEvent, ToolCallEvent, ToolResultEvent
from .loop_guard import _owners
from .plan import PLAN_FILENAME, Step, delete_plan_file, request_commands, suite_command
from .stories import (Criterion, Story, StorySet, _cycle, criterion_notes, drop_suite_runs,
                      library_modules, new_stories_dir, outline_entries, read_story_file,
                      validate_outline, write_story_file)
from .toolcall_text import text_call_error

# _run_loop iterations per story (/plan stories cap): accepted stories in runs
# 3–4 took 4–23 turns; one that had finished its work ran out at 30.
DEFAULT_STORIES_CAP = 40
STORIES_CAP_RANGE = (5, 100)
APPROVE_MODES = ("each", "auto")  # /plan stories approve: every phase's stories, or none
APPROVE_ALIASES = {"once": "auto"}  # the old name of auto
WRITER_CAP = 60                   # iterations of one story-writing pass
STORY_MAX_REFUSALS = 3            # complete_story refusals before the story counts as failed
STORY_NUDGES = 3                  # notes for a story or writing reply without a tool call
MAX_ATTEMPTS = 2                  # failures of a story (and its replacements) before pausing
MAX_COVERS = 3                    # requirements one acceptance criterion may claim to prove
# _run_loop iterations of one verifier pass, by the expectations it checks:
# passes that verified 1–2 expectations took 7–12 turns in run 4.
VERIFY_BASE, VERIFY_PER_EXPECTATION, VERIFY_MAX = 8, 5, 40
ACCEPTANCE_DIR = "tests/acceptance"   # where the verifier's acceptance tests are kept
TURNS_NOTE_AT = 2 / 3             # share of the story cap after which the story gets a turns note
GATE_FIXES = 1                    # fix passes after failing phase checks before pausing
GATE_BATCH = 4                    # expectations per verifier pass at a phase gate
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
    "Claim a behaviour only when a verified expectation, a passed test or a test name below "
    "shows it — 'planned to provide' is what a story was asked for, not what it proved. Do not invent anything that "
    "is not in the notes.")
_YES = ("y", "yes", "ok", "go", "run", "approve", "approved")
# Run 6: phase 1's test of a placeholder reply failed once phase 2 put the real
# engine behind it, and the fix pass brought the placeholder back.
_RETIRE_HINT = (
    "If a failing test checks behaviour this phase replaced on purpose (a placeholder, a dummy "
    "reply), do not bring the old behaviour back: retire it with finish_stories' `retire` "
    "({\"expectation\": \"P1.S1.4\", \"why\": \"what replaced it\"}). Otherwise fix the product.")
_RX_TEST_FILE = re.compile(r"^(?:test_.*|.*_test)\.py$")
_RX_SCRATCH = re.compile(r"^(?:(?:verify|check|debug|scratch|tmp|temp|try)_\w*\.\w+|.*\.(?:pid|log))$")
_RX_NO_TESTS = re.compile(r"^Ran 0 tests|NO TESTS RAN|no tests ran", re.M)
_RX_NO_MODULE = re.compile(r"ModuleNotFoundError: No module named '([A-Za-z_]\w*)'")


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
                    f"needed, then /plan resume, or send a message: it goes to the next story writer or "
                    f"story; /plan cancel to discard."))
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
        fixes, reverified = 0, False
        while True:
            sset = plan.story_sets.get(n)
            if sset is not None:
                edited = read_story_file(self._stories_path(), n)
                if edited is not None:          # the user's edits since it was written
                    sset.merge_from_file(edited)
            if sset is None or not sset.approved:
                # An unapproved draft of an adding pass (a re-split, a fix) resumes
                # that pass from its snapshot: a whole-phase pass would drop the
                # phase's finished stories.
                outcome = self._write_stories(idx, amend=sset.amend if sset is not None else None)
                if outcome != "done":
                    return outcome
                continue
            story = sset.next_ready()
            if story is not None:
                outcome = self._run_story(n, story)
                if outcome == "cancelled":
                    return outcome
                if outcome in ("blocked", "bad_check"):
                    outcome = (self._fix_check(idx, story) if outcome == "bad_check"
                               else self._fix_defect(idx, story))
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
                self.event_queue.put(ChatEvent("system",
                    f"Phase {n} cannot go on: {_stuck_reason(sset)}. Edit {self._stories_rel(n)} "
                    "(mark a story [ ] to retry it), then /plan resume."))
                return "paused"
            passed, report = self._phase_checks(step, sset, last=n == len(plan.steps))
            if passed:
                step.report = sset.report = self._phase_report(n)
                self._write_stories_file(sset)
                self.event_queue.put(ChatEvent("system", f"Phase {n} report:\n{step.report}"))
                return "done"
            kinds = set(sset.gate.get("kinds") or ())
            if kinds == {"ran_out"}:
                # Nothing shows the product is wrong: verify again with more turns
                # once; new stories cannot help a verifier that ran out.
                if not reverified:
                    reverified, self._verify_bonus = True, VERIFY_PER_EXPECTATION
                    continue
                self.event_queue.put(ChatEvent("system",
                    f"Phase {n}: the verifier ran out of turns twice before it got these tests "
                    f"working:\n{_tail(report)}\nEdit the expectations or the code, then /plan resume."))
                return "paused"
            if fixes >= GATE_FIXES:
                self.event_queue.put(ChatEvent("system",
                    f"Phase {n}'s checks still fail after a fix pass:\n{_tail(report)}"))
                return "paused"
            fixes += 1
            outcome = self._amend_stories(idx, (
                "All stories of this phase are done, but the phase's checks fail:\n\n"
                f"{report}\n\nOutline and write NEW stories that fix this (ids after the existing "
                "ones, depending on the stories they build on). Do not repeat the finished stories. "
                "If nothing needs building (only an earlier test of replaced behaviour, or an "
                "unclear expectation, must go), call finish_stories with just `retire`."),
                may_be_empty=kinds <= {"regression", "unclear", "ran_out"})
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

    def _amend_stories(self, idx: int, why: str, replaces: tuple | list = (),
                       remap: str | None = None, blocks: str | None = None,
                       may_be_empty: bool = False) -> str:
        """A writing pass that adds to the phase's stories (a re-split, a fix
        story, a corrected story, a gate fix) instead of writing the set.
        replaces: the stories it retires; remap: the one whose dependents move
        to the new stories; blocks: the one that waits for them; may_be_empty:
        finish_stories may add nothing (only `retire`)."""
        sset = self.plan.story_sets.get(idx + 1)
        return self._write_stories(idx, amend={
            "extra": why, "replaces": list(replaces), "remap": remap, "blocks": blocks,
            "may_be_empty": may_be_empty,
            "base": [s.to_dict() for s in sset.stories] if sset is not None else []})

    def _write_stories(self, idx: int, extra: str = "", amend: dict | None = None) -> str:
        """One story-writing pass for phase idx: the writer investigates,
        outlines the stories (plan_stories), writes them one per turn
        (write_story) and submits them (finish_stories) until a set is valid
        and approved.  amend (from _amend_stories, or a stored draft's): the
        pass adds to the phase's stories as they were when it began.  "done"
        when the phase has an approved set, else why not."""
        n = idx + 1
        self._fresh_context()
        self._enter_role("story-writer")
        # An adding pass works on a snapshot of the stories, kept with its draft:
        # a submitted set is stored before approval, and its drafts must not become
        # "existing" stories when the user sends it back (the story-mud run
        # tripled its stories that way), nor be all there is on /plan resume.
        amend = dict(amend) if amend else None
        w = self._writing = {"phase": n, "amend": amend,
                             "replaces": list(amend["replaces"]) if amend else [],
                             "base": [Story.from_dict(d) for d in amend["base"]] if amend else None,
                             "stored": False, "outline": [], "drafts": {}, "story_turn": -1}
        self._append_user(self._writer_brief(idx, (amend or {}).get("extra") or extra))
        self._turn_user = self.messages[-1]
        before = self._background_runs()
        try:
            outcome = self._run_loop(self._current_tools() if self.tools_enabled else [], WRITER_CAP)
        finally:
            self._writing = None
            self._enter_role(None)
            self._stop_background_runs(before, f"phase {n}'s story writer")
        if w["stored"]:
            return "done"
        if outcome == "cancelled":
            return outcome
        self.event_queue.put(ChatEvent("system",
            f"The story writer stopped without stories for phase {n}: {self._writer_stopped(outcome, w)}. "
            "/plan resume asks it again; a message you send is passed to it."))
        return "paused" if outcome == "done" else outcome

    def _story_nudge(self, content: str, tools: list[dict]) -> str:
        """The note for a story or writing pass that replied without a tool call."""
        err = text_call_error(content, tools)
        if getattr(self, "_verifying", None):
            what = ("Call the tool itself now. Write and run the test for the next expectation, "
                    "save it with save_acceptance_test, then call finish_verification.")
        elif self._writing:
            what = ("Call the tool itself now, with JSON arguments. " + self._writer_next(self._writing))
        else:
            what = ("A story ends only with complete_story. Make the change with a tool call "
                    "(edit_file / write_file), try the story's examples, then call complete_story.")
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
            lines += ["", "Each needs at least one expected behaviour whose examples show it, "
                          "naming it in covers (e.g. covers: [\"R3\"]); finish_stories refuses "
                          "the set until every one is covered. Write the examples the way the "
                          "user meets the product."]
        suite = self._suite_proofs()
        mine = [r for r in plan.requirements if r["phase"] == n and r["id"] in suite]
        if mine:
            lines += [""] + [f"{r['id']} ({r['text']}) is proven by `{suite[r['id']]}`, which the "
                             "harness runs at the last phase's gate: give it no expected behaviour, "
                             "and have the stories that write the code write its unit tests."
                             for r in mine]
        if plan.interfaces:
            lines += ["", "## How the product is used (its interfaces)"]
            lines += [f"- {x}" for x in plan.interfaces]
        lines += ["", "## Phases"]
        for i, s in enumerate(plan.steps, 1):
            mark = "▶" if i == n else ("✓" if s.status == "done" else " ")
            lines.append(f"{mark} {i}. {s.title}" + (f" — {s.outcome}" if s.outcome else ""))
        lines += ["", f"## This phase: {step.title}", step.details or "(no details)"]
        if step.outcome:
            lines += ["", f"Outcome: {step.outcome}"]
        if step.acceptance:
            lines += ["Expected behaviour a verifier tests when all stories are done:"]
            for c in step.acceptance:
                lines.append(f"- {c.text}")
                lines += [f"  - e.g. {e['given']} → {e['expect']}" for e in c.examples]
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
        w = getattr(self, "_writing", None)
        current = plan.story_sets.get(n)
        if w and w.get("base"):
            shown = StorySet(phase=n, title=step.title, stories=w["base"], approved=True)
            lines += ["", "## Stories of this phase so far", shown.to_markdown()]
        elif current is not None and current.stories and not current.approved:
            lines += ["", "## Your earlier draft of this phase (not approved: write the set again)",
                      current.to_markdown()]
        elif current is not None and current.stories:
            lines += ["", "## Stories of this phase so far", current.to_markdown()]
        if extra:
            lines += ["", "## What to do now", extra]
        lines += self._take_resume_note()
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
        if not w.get("amend") or w.get("base") is None:
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
                    "with plan_stories."
                    + (" If no new story is needed, call finish_stories with just `retire`."
                       if (w.get("amend") or {}).get("may_be_empty") else ""))
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
        if not w.get("amend"):
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
        acc = raw.get("acceptance") if isinstance(raw.get("acceptance"), list) else []
        if any(isinstance(c, dict) and str(c.get("check") or "").strip() for c in acc):
            # run 3's checks named classes and test files the code did not have
            return ("ERROR: acceptance criteria are expected behaviour with examples, not "
                    "commands: drop 'check' and give 'examples' — an action through the "
                    "product's interface → the output it must give. An independent verifier "
                    "writes and runs the tests after the story is implemented.")
        story = Story.from_dict(raw)
        # an expectation that only runs the tests is the last gate's job (as for a phase)
        dropped = []
        if self._test_commands():
            story.acceptance, dropped = drop_suite_runs(story.acceptance)
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
        errors = (validate_outline(outline, kept, earlier)
                  + story.problems(ids, earlier, self.workdir, library_modules(self.plan.interfaces)))
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
        notes = [f"Dropped \"{c.text[:60]}\": it only runs the tests, which the last phase's gate "
                 f"does (`{self._test_commands()[0]}`)." for c in dropped]
        notes += [f"Note: {x}." for c in story.acceptance for x in criterion_notes(c)]
        return (f"{'Replaced the draft of' if rewritten else 'Stored'} {story.id} "
                f"({k}/{len(outline)} written).{kept_note}{after} "
                + "".join(f"{x} " for x in notes) + self._writer_next(w)
                + self._feedback_reminder(w))

    def _handle_finish_stories(self, args: dict) -> str:
        w = self._writer()
        if not w:
            return "ERROR: finish_stories is only available while a phase's stories are written."
        retire, problems = self._retire_problems(w["phase"], args.get("retire"))
        if problems:
            return ("ERROR: " + "\n".join(problems) + "\nFix `retire` (or leave it out), then call "
                    "finish_stories again.")
        # An adding pass may add nothing: a gate failure that only needs a test of
        # replaced behaviour retired, or a verifier that ran out of turns.
        empty_ok = bool(w.get("amend")) and (bool(retire) or w["amend"].get("may_be_empty"))
        if not w["outline"] and not empty_ok:
            return ("ERROR: nothing to submit yet: outline the stories with plan_stories, then "
                    "write each one with write_story.")
        missing = [e["id"] for e in w["outline"] if e["id"] not in w["drafts"]]
        if missing:
            return (f"ERROR: not every story is written yet: {', '.join(missing)} still "
                    f"need{'s' if len(missing) == 1 else ''} write_story (or leave "
                    f"{'it' if len(missing) == 1 else 'them'} out of the outline with plan_stories). "
                    + self._writer_next(w))
        w["retire"] = retire
        new = [Story.from_dict(w["drafts"][e["id"]].to_dict()) for e in w["outline"]]
        return self._submit_stories(w, new, args.get("phase_revision"))

    def _submit_stories(self, w: dict, new: list[Story], revision) -> str:
        """Validate the written stories as the phase's set (or, for a re-split
        or a fix pass, added to the phase's stories so far), store it and ask
        for approval."""
        plan = self.plan
        n = w["phase"]
        kept = self._writer_kept(w)
        amend = w.get("amend") or {}
        if kept:
            # Replacements inherit the failed story's attempts, so failing
            # again pauses the plan.
            first = (amend.get("replaces") or [None])[0]
            failed = next((s for s in kept if s.id == first), None)
            for s in new:
                s.attempts = max(s.attempts, failed.attempts if failed else 0)
            if amend.get("remap"):      # a corrected story stands in for the one it replaces
                for s in kept:
                    if not s.finished and amend["remap"] in s.depends_on:
                        s.depends_on = [d for d in s.depends_on if d != amend["remap"]] + [
                            x.id for x in new if x.id not in s.depends_on]
            blocked = next((s for s in kept if s.id == amend.get("blocks")), None)
            if blocked is not None:     # the story that reported a defect waits for its fix
                blocked.depends_on += [x.id for x in new if x.id not in blocked.depends_on]
        sset = StorySet(phase=n, title=plan.steps[n - 1].title, stories=kept + new)
        errors = (sset.validate(plan.earlier_stories(n), self.workdir, library_modules(plan.interfaces))
                  + self._coverage_problems(n, sset))
        if errors:
            return ("ERROR: the stories need changes before they can run:\n"
                    + "\n".join(f"- {e}" for e in errors)
                    + "\nFix them with write_story (or the outline with plan_stories), then call "
                      "finish_stories again.")
        sset.approved = False
        sset.amend = amend              # resumed as this pass if it is never approved
        plan.story_sets[n] = sset
        err = self._write_stories_file(sset)
        if err:
            return f"ERROR: {err}"
        answer = self._approve_stories(n, sset, revision, w.get("retire") or [])
        if answer:
            return answer                     # feedback, or an invalid hand edit
        self._apply_retire(n, w.get("retire") or [])
        w["stored"] = True
        self._step_summary = f"{len([s for s in sset.stories if not s.finished])} stories for phase {n}"
        return (f"Stories approved for phase {n} ({sset.progress()} done). The harness runs them "
                "now, each in its own conversation. Do not call any more tools.")

    def _approve_stories(self, n: int, sset: StorySet, revision,
                         retire: list[tuple[str, str]] = ()) -> str:
        """Ask the user per the setting; '' once approved, else the tool result
        that sends the writer back (feedback, errors in a hand edit or in the
        phase revision).  A phase revision is asked after the stories, and
        applied only with them."""
        phases = self._revision_phases(n, revision)
        if isinstance(phases, str):
            return phases
        self.event_queue.put(ChatEvent("assistant", sset.to_markdown()))
        if retire:
            by = {cid: c for cid, c, _n in self._criteria(n)}
            self.event_queue.put(ChatEvent("system",
                "With these stories, the writer retires these earlier acceptance tests (their "
                "behaviour was replaced on purpose):\n"
                + "\n".join(f"- {cid}: {by[cid].text if cid in by else ''} — {why}"
                             for cid, why in retire)))
        if self.stories_approve == "each":
            todo = len([s for s in sset.stories if not s.finished])
            answer = self._ask_user(
                f"Run these {todo} stor{'y' if todo == 1 else 'ies'} for phase {n}? y = run them · "
                f"anything else = feedback for the story writer. You can edit {self._stories_rel(n)} "
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
                errors = sset.validate(self.plan.earlier_stories(n), self.workdir,
                                       library_modules(self.plan.interfaces))
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
        sset.approved, sset.amend = True, {}
        self._write_stories_file(sset)
        if phases and not self._apply_phase_revision(n, phases, revision):
            self.event_queue.put(ChatEvent("system",
                "The user declined the change to the later phases; they stay as they were."))
        self._sync_plan_file()
        return ""

    def _revision_phases(self, n: int, revision) -> list[Step] | str | None:
        """finish_stories' phase_revision as the new later phases, checked the
        way write_phase checks a phase; an ERROR for the writer; None when
        there is none."""
        if not (isinstance(revision, dict) and isinstance(revision.get("phases"), list)):
            return None
        from .plan_runner import _phase_problems
        phases = [Step.from_dict(p) for p in revision["phases"] if isinstance(p, dict)]
        if not phases:
            return None
        problems = [f"phase {n + i}: {e}" for i, st in enumerate(phases, 1)
                    for e in _phase_problems(st, n + i, self.plan.interfaces)]
        if problems:
            return ("ERROR: phase_revision needs changes:\n" + "\n".join(f"- {p}" for p in problems)
                    + "\nFix phase_revision (or leave it out), then call finish_stories again.")
        return phases

    def _apply_phase_revision(self, n: int, phases: list[Step], revision: dict) -> bool:
        """Ask the user; on yes replace the phases after n and move the
        requirements the revision names, then any left past the last phase
        onto it (they would otherwise never be due or checked)."""
        shown = "\n".join(f"{n + i}. {p.title} — {p.outcome}" for i, p in enumerate(phases, 1))
        answer = self._ask_user(
            f"The story writer proposes changing the phases after phase {n}:\n"
            f"{revision.get('reason', '')}\n\n{shown}\n\nApply this change? y / n").strip()
        if answer.lower().rstrip(".!") not in _YES:
            return False
        plan = self.plan
        plan.steps = plan.steps[:n] + phases
        moves = {str(r.get("id") or "").strip().upper(): r.get("phase")
                 for r in revision.get("requirements") or [] if isinstance(r, dict)}
        last, moved = len(plan.steps), []
        for r in plan.requirements:
            to = moves.get(r["id"])
            if isinstance(to, int) and n < to <= last:
                r["phase"] = to
            elif r["phase"] > last:
                r["phase"] = last
                moved.append(r["id"])
        if moved:
            self.event_queue.put(ChatEvent("system",
                f"{', '.join(moved)} now belong{'s' if len(moved) == 1 else ''} to phase {last}, "
                "the last one: the revision removed the phase they were in."))
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
                       "failure": "", "defect": "", "bad_check": False, "submitted": False,
                       "base_turns": 0, "turns": 0, "start": len(self.messages),
                       "files_before": set(_project_files(self.workdir, limit=5000)),
                       "scratch_asked": False}
        self._append_user(self._story_brief(n, story))
        self._turn_user = self.messages[-1]
        before = self._background_runs()
        try:
            outcome = self._implement_and_verify(n, story)
        finally:
            state = self._story
            self._story = None
            story.finished_at = _now()
            self._enter_role(None)
            self._stop_background_runs(before, f"P{n}.{story.id}")
        if state["done"]:
            return "done"
        if state["defect"]:
            story.status, story.defect = "pending", state["defect"]
            self._write_stories_file(sset)
            return "bad_check" if state["bad_check"] else "blocked"
        if outcome == "cancelled":
            story.status = "pending"
            self._write_stories_file(sset)
            return outcome
        if (outcome in ("done", "limit") and not state["failure"]
                and self._accept_text_end(n, story, state, at_cap=outcome == "limit")):
            return "done"
        self._story_failure = state["failure"] or self._last_words(outcome)
        changed = self._story_facts(state["start"], [])["files"]
        if changed:
            # run 3's P3.S2 left a test player in world.json behind its re-split
            self._story_failure += ("\n\nIts changes are still in the code: "
                                    + ", ".join(changed[:12]) + ".")
        return outcome if outcome != "done" else "not_done"

    def _implement_and_verify(self, n: int, story: Story) -> str:
        """The implementer's loop, and after each complete_story that submits the
        story, an independent verifier pass; a failed expectation goes back to
        the implementer, in its own context, as a refusal.  Returns the last
        loop's outcome; the story's state (self._story) says how it ended."""
        st = self._story
        budget, outcome = self.stories_cap, "done"
        while True:
            outcome = self._run_loop(self._current_tools() if self.tools_enabled else [],
                                     max(1, budget - st["base_turns"]))
            st["base_turns"] += st["turns"]
            st["turns"] = 0
            if not st["submitted"] or outcome == "cancelled":
                return outcome
            st["submitted"] = False
            found = self._verify_story(n, story)
            if not found["fail"]:
                # An expectation the verifier finds unclear says nothing against
                # the work: the gate verifies it again, where it can be restated.
                self._accept_story(n, story, st, story.delivered)
                return outcome
            st["refusals"] += 1
            story.refusals = st["refusals"]
            report = _fail_report(found["fail"])
            if st["refusals"] >= STORY_MAX_REFUSALS or st["base_turns"] >= budget:
                st["failure"] = f"The verifier found expectations not met:\n{report}"
                return outcome
            self._append_user("[Verifier report, from the harness] These expectations are not "
                              f"met yet:\n{report}\nFix the product (not the tests), run the "
                              "tests named above to see it pass, then call complete_story again.")

    def _accept_story(self, n: int, story: Story, st: dict, summary: str) -> None:
        sset = self.plan.story_sets[n]
        story.status = "done"
        story.delivered = summary
        story.facts = self._story_facts(st["start"], [c.test["command"] for c in story.acceptance
                                                      if c.test and c.proven] + st.get("passed", []))
        self._write_stories_file(sset)
        self._sync_plan_file()
        st["done"] = True

    def _verify_story(self, n: int, story: Story, label: str = "",
                      criteria: list[Criterion] | None = None, at_gate: bool = False) -> dict:
        """The independent verifier pass: a fresh `story-verifier` context gets
        the expectations (criteria with examples), writes and runs a black-box
        test for each through the product's interfaces, saves it, and gives a
        verdict.  Saved tests that still pass are not verified again.  Returns
        {"fail": [(criterion, evidence)], "unclear": [(criterion, reason)],
        "deferred": [(criterion, why)]}; the verdicts and tests are kept on the
        criteria.

        One pass per batch (all of a story's expectations; GATE_BATCH at a
        gate), then one pass alone for each it did not get to (no verdict, no
        saved test): a verifier that ran out of turns says nothing about the
        expectation (run 4's engine stories were rewritten twice for it).  What
        is still unverified is deferred to the gate, or at the gate reported as
        "ran_out", apart from the verifier's own "unclear".  `not_yet` (the
        interface is not built yet) defers to the gate; at the gate it counts
        as unclear."""
        criteria = story.acceptance if criteria is None else criteria
        label = label or f"P{n}.{story.id}"
        todo = []
        for i, c in enumerate(criteria):
            if not c.examples:
                continue
            if c.test and c.proven:
                ok, _out = self._run_acceptance_test(c.test["command"])
                if ok:
                    continue
            todo.append(i)
        fail, unclear, deferred = [], [], []
        if not todo:
            return {"fail": fail, "unclear": unclear, "deferred": deferred, "ran_out": []}
        absent = [] if at_gate else _absent_interfaces(self.workdir, self.plan.interfaces)
        if absent:
            # run 4a spent two 20-turn passes, twice, on an engine whose server
            # did not exist yet: nothing to test it through
            why = f"{', '.join(absent)} does not exist yet"
            for i in todo:
                criteria[i].verdict = {"passed": None, "deferred": True, "evidence": why,
                                       "at": _now()}
                deferred.append((criteria[i], why))
            self.event_queue.put(ChatEvent("system",
                f"🔎 {label}: {len(todo)} expectation{'s' if len(todo) != 1 else ''} deferred to "
                f"the phase gate — {why}."))
            return {"fail": fail, "unclear": unclear, "deferred": deferred, "ran_out": []}
        # At the gate, a few expectations per verifier pass: run 6's phase 2 gate
        # gave one pass 20 (about 2 turns each), it ran out twice, and the 10
        # "unclear" sent the story writer off to write test-only stories.
        batches = ([todo[i:i + GATE_BATCH] for i in range(0, len(todo), GATE_BATCH)]
                   if at_gate else [todo])
        if len(batches) > 1:
            self.event_queue.put(ChatEvent("system",
                f"🔎 Verifying {label} — {len(todo)} expectations in {len(batches)} batches"))
        found = {"fail": fail, "unclear": unclear, "deferred": deferred, "ran_out": []}
        left = []
        for b, batch in enumerate(batches, 1):
            part = f"batch {b}/{len(batches)}" if len(batches) > 1 else ""
            rest = self._verify_batch(n, story, label, criteria, batch, at_gate, found, part)
            # one more pass each, alone, for what the pass did not get to
            left += [i for i in rest
                     if self._verify_batch(n, story, label, criteria, [i], at_gate, found,
                                           "alone" if len(batch) > 1 else part, attempt=2)]
        why = "the verifier did not get a test for it working"
        for i in left:
            c = criteria[i]
            # still deferred: the next gate verifies it again
            c.verdict = {"passed": None, "deferred": True, "evidence": why, "at": _now()}
            (found["ran_out"] if at_gate else deferred).append((c, why))
        ran_out = found["ran_out"]
        parts = [f"{len(fail)} not met"] * bool(fail) + [f"{len(unclear)} unclear"] * bool(unclear) \
            + [f"{len(ran_out)} not verified (the verifier ran out of turns)"] * bool(ran_out) \
            + [f"{len(deferred)} deferred to the phase gate"] * bool(deferred)
        self.event_queue.put(ChatEvent("system",
            f"🔎 {label}: expectations {', '.join(parts) if parts else 'all met'}."))
        return found

    def _verify_batch(self, n: int, story: Story, label: str, criteria: list[Criterion],
                      todo: list[int], at_gate: bool, found: dict, part: str = "",
                      attempt: int = 1) -> list[int]:
        """One verifier pass over the expectations `todo`, adding the verdicts
        to `found`; returns the ones it did not get to (no verdict, no saved
        test)."""
        fail, unclear, deferred = found["fail"], found["unclear"], found["deferred"]
        results, runs = self._verifier_pass(n, story, label, criteria, todo, attempt, part, at_gate)
        left = []
        for i in todo:
            c = criteria[i]
            verdict, evidence = results.get(i, (None, ""))
            if verdict is None:          # no verdict: what its saved test showed
                run = runs.get(i)
                if run is None:
                    left.append(i)
                    continue
                verdict, evidence = (("pass", "") if run[0] else
                                     ("fail", "its saved test fails:\n" + _tail(run[1], 12)))
            if verdict == "not_yet":
                if at_gate:
                    verdict, evidence = "unclear", ("the verifier found no way to exercise it "
                                                    f"through the product's interfaces: {evidence}")
                else:
                    c.verdict = {"passed": None, "deferred": True, "evidence": evidence, "at": _now()}
                    deferred.append((c, evidence))
                    continue
            if verdict == "unclear":
                # kept for the gate (again): it says nothing against the work
                c.verdict = {"passed": None, "deferred": True, "evidence": f"unclear: {evidence}",
                             "at": _now()}
                unclear.append((c, evidence))
                continue
            c.verdict = {"passed": verdict == "pass", "evidence": evidence, "at": _now()}
            if verdict == "fail":
                fail.append((c, evidence))
        return left

    def _verifier_pass(self, n: int, story: Story, label: str, criteria: list[Criterion],
                       todo: list[int], attempt: int, part: str = "",
                       at_gate: bool = False) -> tuple[dict, dict]:
        """One verifier context: (verdicts by index, saved test runs by index)."""
        saved = (self.messages, self.story_role, self._story, self._turn_user)
        self._story = None
        self.messages = [dict(self.messages[0])]
        v = self._verifying = {"phase": n, "story": story, "label": label, "criteria": criteria,
                               "todo": todo, "saved": {}, "results": None, "turns": 0}
        self._enter_role("story-verifier")
        self.event_queue.put(ChatEvent("system",
            f"🔎 Verifying {label} — {len(todo)} expectation{'s' if len(todo) != 1 else ''}"
            + (f" ({', '.join(x for x in (part, 'second pass' * (attempt == 2)) if x)})"
               if part or attempt == 2 else "")))
        brief = self._verify_brief(n, story, label, criteria, todo, at_gate)
        if attempt > 1:
            brief += ("\n\nA first verifier pass ran out of turns before it saved a test for these. "
                      "Start by finding how the product is run (its entry point and how it is "
                      "started), then write each test right away.")
        self._append_user(brief)
        self._turn_user = self.messages[-1]
        before = self._background_runs()
        try:
            self._run_loop(self._current_tools() if self.tools_enabled else [],
                           verify_cap(len(todo)) + getattr(self, "_verify_bonus", 0) * len(todo))
        finally:
            self._drop_unsaved_tests(v)
            self._archive_context(by="verify")
            self._verifying = None
            self._stop_background_runs(before, f"the verifier of {label}")
            self.messages, role, self._story, self._turn_user = saved
            self._enter_role(role)
        return v["results"] or {}, v["saved"]

    def _drop_unsaved_tests(self, v: dict) -> None:
        """Remove the files this verifier pass wrote in the acceptance folder
        but did not save as a test (run 4b left p2_s8_c1_go_north_v2.py): the
        folder holds the acceptance tests and nothing else."""
        kept = {c.test["path"] for c in v["criteria"] if c.test}
        for path in self._story_facts(1, [])["files"]:
            if path.startswith(ACCEPTANCE_DIR + "/") and path not in kept:
                try:
                    (self.workdir / path).unlink()
                except OSError:
                    pass

    def _verify_brief(self, n: int, story: Story, label: str, criteria: list[Criterion],
                      todo: list[int], at_gate: bool = False) -> str:
        plan = self.plan
        tag = label.replace(".", "_").replace(" ", "_").lower()
        lines = [f"# Verify {label}: {story.title}", "",
                 f"Plan: {plan.title} — phase {n}/{len(plan.steps)}: {plan.steps[n - 1].title}"]
        if plan.interfaces:
            lines += ["", "## How the product is used (its interfaces)"]
            lines += [f"- {x}" for x in plan.interfaces]
        if story.delivered:
            lines += ["", "## What the implementer says it delivered", story.delivered]
        files = _project_files(self.workdir)
        if files:
            lines += ["", "## Files in the project", ", ".join(files)]
        lines += ["", "## The expectations to verify"]
        for i in todo:
            c = criteria[i]
            lines += ["", f"### Expectation {i + 1}: {c.text}"]
            lines += [f"- Example: {e['given']} → {e['expect']}" for e in c.examples]
            lines.append(f"- Save its test as `{ACCEPTANCE_DIR}/{tag}_c{i + 1}_<short-name>.<ext>`")
        lines += ["", "For each expectation: find out from the code how to exercise it through "
                      "the interface, write the test, run it, save it with save_acceptance_test; "
                      "then call finish_verification with a verdict for every expectation. "
                      + ("This is the end of the phase: every interface it needs should exist now, "
                         "so there is no `not_yet`. If one truly cannot be exercised, give it "
                         "`unclear` with what is missing." if at_gate else
                         "If the interface an expectation acts through does not exist yet (e.g. the "
                         "server it needs is not written), give it `not_yet` at once, with what is "
                         "missing: it is verified at the end of the phase, when the path is "
                         "complete.")]
        return "\n".join(lines)

    def _archive_context(self, by: str) -> None:
        """Put the current conversation (bar the system prompt) in the
        compaction archive, so /export full still shows it."""
        msgs = self.messages
        if len(msgs) > 1:
            self.compacted = transcript_mod.archive_compaction(
                self.compacted, msgs, set(range(1, len(msgs))), {}, [0],
                max((r["n"] for r in self.compacted), default=0) + 1,
                datetime.now().isoformat(timespec="seconds"), by=by)

    def _run_acceptance_test(self, command: str) -> tuple[bool, str]:
        self.event_queue.put(ToolCallEvent("acceptance_test", {"command": command}))
        ok, out = tdd_mod.run_tests(command, self.workdir, self._cancel)
        self.event_queue.put(ToolResultEvent("acceptance_test", out))
        return ok and not _RX_NO_TESTS.search(out), out

    def _verifier(self) -> dict | None:
        v = getattr(self, "_verifying", None)
        return v if self.story_role == "story-verifier" and v else None

    def _verifier_criterion(self, v: dict, raw) -> tuple[int, Criterion] | str:
        try:
            i = int(raw) - 1
        except (TypeError, ValueError):
            return "ERROR: 'expectation' is the expectation's number in the first message (1, 2, …)."
        if i not in v["todo"]:
            nums = ", ".join(str(t + 1) for t in v["todo"])
            return f"ERROR: there is no expectation {raw} to verify; the expectations are {nums}."
        return i, v["criteria"][i]

    def _handle_save_acceptance_test(self, args: dict) -> str:
        v = self._verifier()
        if not v:
            return "ERROR: save_acceptance_test is only available while a story is verified."
        got = self._verifier_criterion(v, args.get("expectation"))
        if isinstance(got, str):
            return got
        i, c = got
        path = str(args.get("path") or "").strip().lstrip("./")
        command = str(args.get("command") or "").strip()
        if not path.startswith(ACCEPTANCE_DIR + "/") or ".." in Path(path).parts:
            return f"ERROR: acceptance tests go in {ACCEPTANCE_DIR}/; save it there."
        f = self.workdir / path
        if not f.is_file():
            return f"ERROR: {path} does not exist — write the test first, then save it."
        if not command or Path(path).name not in command:
            return f"ERROR: 'command' must run {path}, e.g. `sh {path}` or `python3 {path}`."
        problems = _test_command_problems(command)
        if problems:
            return "ERROR: this command cannot be an acceptance test: " + "; ".join(problems)
        text = f.read_text(encoding="utf-8", errors="replace")
        inside = _project_imports(self.workdir, f, text, self.plan.interfaces)
        if inside:
            # run 4: P1.S2's tests called GameWorld.execute_command and broke on
            # the next story's refactor while the API still behaved
            return (f"ERROR: the test imports {', '.join(inside)} — the project's own code. Test "
                    "the product through its interfaces (start it with the command they give, "
                    "and send the request or run the command line), never its modules: a test "
                    "of internals breaks when the "
                    "code is reorganised, though the product still behaves.")
        missing = [x for x in expected_values(c) if x not in text]
        if missing:
            return ("ERROR: the test does not check the example's expected value"
                    + ("s " if len(missing) > 1 else " ") + ", ".join(f"`{x}`" for x in missing)
                    + " — check exactly what the example expects; never weaken it.")
        ok, out = self._run_acceptance_test(command)
        old = (c.test or {}).get("path") or ""
        if old and old != path and old.startswith(ACCEPTANCE_DIR + "/"):
            try:                            # one test per expectation: the folder keeps no strays
                (self.workdir / old).unlink()
            except OSError:
                pass
        c.test = {"path": path, "command": command}
        v["saved"][i] = (ok, out)
        if ok:
            return f"Saved and run: it passes. Expectation {i + 1} is met."
        return (f"Saved and run: it FAILS.\n{_tail(out, 20)}\nIf the product does not behave as "
                "expected, keep the test and report 'fail'. If the test itself is wrong (path, "
                "start command, a typo), fix it and save it again.")

    def _handle_finish_verification(self, args: dict) -> str:
        v = self._verifier()
        if not v:
            return "ERROR: finish_verification is only available while a story is verified."
        raw = args.get("results") if isinstance(args.get("results"), list) else []
        results, errors = {}, []
        for r in raw:
            if not isinstance(r, dict):
                continue
            got = self._verifier_criterion(v, r.get("expectation"))
            if isinstance(got, str):
                errors.append(got.removeprefix("ERROR: "))
                continue
            i, _c = got
            verdict = str(r.get("verdict") or "").strip().lower()
            evidence = str(r.get("evidence") or "").strip()
            run = v["saved"].get(i)
            if verdict not in ("pass", "fail", "unclear", "not_yet"):
                errors.append(f"expectation {i + 1}: the verdict is pass, fail, not_yet or unclear")
            elif verdict == "pass" and not (run and run[0]):
                errors.append(f"expectation {i + 1}: 'pass' needs a saved test that passes"
                              + (" — its saved test fails" if run else " — none is saved"))
            elif verdict == "fail" and not run:
                errors.append(f"expectation {i + 1}: 'fail' needs a saved test that shows it")
            elif verdict == "fail" and run[0]:
                errors.append(f"expectation {i + 1}: its saved test passes, so it is not a fail")
            elif verdict in ("unclear", "not_yet") and not evidence:
                errors.append(f"expectation {i + 1}: say why it cannot be checked"
                              + (" yet (which part of the interface is missing)"
                                 if verdict == "not_yet" else ""))
            else:
                results[i] = (verdict, evidence or (_tail(run[1], 12) if run and not run[0] else ""))
        missing = [str(i + 1) for i in v["todo"] if i not in results]
        if missing and not errors:
            errors.append(f"no verdict for expectation {', '.join(missing)}")
        if errors:
            return ("ERROR: verification not finished:\n" + "\n".join(f"- {e}" for e in errors)
                    + "\nFix this and call finish_verification again.")
        v["results"] = results
        self._step_summary = ""
        return "Verification recorded. Do not call any more tools."

    def _verifier_guard(self, name: str, args: dict) -> str:
        """The verifier writes only its acceptance tests: never product code."""
        if self.story_role != "story-verifier" or not isinstance(args, dict):
            return ""
        from .tools import registry
        from .tools.dispatcher import written_paths
        tool = registry.get(name)
        paths = written_paths(tool, args) if tool is not None else []
        bad = [p for p in paths if not str(p).lstrip("./").startswith(ACCEPTANCE_DIR + "/")]
        if bad:
            return (f"ERROR: the verifier writes only in {ACCEPTANCE_DIR}/ — {', '.join(map(str, bad))} "
                    "is product code or data; never change it. Test the product as it is.")
        return ""

    def _background_runs(self) -> set[str]:
        store = getattr(self, "_run_store", None)
        return set(store.running()) if store is not None else set()

    def _stop_background_runs(self, before: set[str], who: str) -> None:
        """Stop the background commands a story or a writing pass started: run
        3's P3.S2 left `python3 server.py` holding :8000 after it failed, and
        later stories met "Address already in use"."""
        store = getattr(self, "_run_store", None)
        if store is None:
            return
        stopped = [r for r in store.running() if r not in before and store.stop(Path(r))]
        if stopped:
            self.event_queue.put(ChatEvent("system",
                f"Stopped the background command{'s' if len(stopped) > 1 else ''} {who} "
                f"left running: {', '.join(stopped)}."))

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
        if plan.interfaces:
            lines += ["", "## How the product is used (its interfaces)"]
            lines += [f"- {x}" for x in plan.interfaces]
        if any(c.examples for c in story.acceptance):
            lines += ["", "An independent verifier tests the acceptance examples after you call "
                          "complete_story, with its own tests through these interfaces."]
        cmds = self._test_commands()
        if cmds:
            lines += ["", "## How the project's tests run",
                      f"The last phase's gate runs `{cmds[0]}`: it must find and pass your unit tests. "
                      + _test_hint(cmds[0]) + "."]
        lines += self._take_resume_note()
        lines += ["", "Implement this story, try its examples yourself, then call complete_story."]
        return "\n".join(lines)

    def _take_resume_note(self) -> list[str]:
        """The user's message that resumed the paused plan, for the brief being
        built (once): story roles start from a fresh context, so it would
        otherwise reach no one."""
        note, self._resume_note = getattr(self, "_resume_note", ""), ""
        if not note:
            return []
        return ["", "## The user's message (sent when resuming the plan)", note, "",
                "Follow it: where it differs from what this brief says, it wins."]

    def _dependency(self, n: int, ref: str) -> Story | None:
        if ref.startswith("P"):
            return self.plan.earlier_stories(n).get(ref)
        sset = self.plan.story_sets.get(n)
        return sset.get(ref) if sset else None

    def _accept_text_end(self, n: int, story: Story, state: dict, at_cap: bool = False) -> bool:
        """A story that ended with text instead of complete_story: run its unit
        tests and the verifier, and accept it when they pass (the story-mud run
        failed a story that had done its work, only because it summed up in
        text)."""
        if not story.acceptance or any(not c.examples for c in story.acceptance):
            return False
        failures, passed = self._run_own_tests(story, state["start"])
        if failures:
            return False
        last = next((m.get("content") for m in reversed(self.messages[state["start"]:])
                     if m.get("role") == "assistant" and m.get("content")), "")
        story.delivered = " ".join(str(last).split())[:600]
        found = self._verify_story(n, story)
        if found["fail"]:
            state["failure"] = (("It reached the turn limit" if at_cap
                                 else "It ended without complete_story")
                                + ", and the verifier found:\n" + _fail_report(found["fail"]))
            return False
        state["passed"] = passed
        self._accept_story(n, story, state, story.delivered)
        self._step_summary = story.delivered or f"Story {story.id} done."
        self.event_queue.put(ChatEvent("system",
            f"Story {story.id} " + ("reached the turn limit" if at_cap
                                    else "ended without complete_story")
            + "; its expectations are verified — accepted."))
        return True

    def _run_own_tests(self, story: Story, start: int) -> tuple[list[str], list[str]]:
        """Run the test files the story wrote (_own_tests), and the project's
        test command when it wrote any (_suite_finds_tests): (failure reports,
        the commands that passed)."""
        failures, passed = [], []
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
                failures.append(f"✗ the story's own tests in {path} pass\n"
                                f"  `{cmd}`\n{_indent(_tail(out))}" + self._import_hint(out))
        return failures + self._suite_finds_tests(story, start), passed

    def _import_hint(self, out: str) -> str:
        """run 4's P2.S1 looped on `No module named 'server'` until its turns ran out."""
        m = _RX_NO_MODULE.search(out)
        if m and ((self.workdir / f"{m.group(1)}.py").exists() or (self.workdir / m.group(1)).is_dir()):
            return (f"\n  ({m.group(1)} is the project's own module: run the tests from the project "
                    "root with `python3 -m unittest discover`; tests/ needs an __init__.py)")
        return ""

    def _suite_finds_tests(self, story: Story, start: int) -> list[str]:
        """When the story wrote test files and the request names a test command,
        that command must find tests: plain `python -m unittest` found none in
        runs 2–4 (no tests/__init__.py), and only the last gate would say so."""
        wrote = [f for f in self._story_facts(start, [])["files"] if _RX_TEST_FILE.match(Path(f).name)]
        cmds = self._test_commands()
        out = []
        for cmd in cmds if wrote else []:
            self.event_queue.put(ToolCallEvent("story_check", {"command": cmd}))
            _ok, text = tdd_mod.run_tests(cmd, self.workdir, self._cancel)
            self.event_queue.put(ToolResultEvent("story_check", text))
            if _RX_NO_TESTS.search(text):
                out.append(f"✗ `{cmd}` (the project's test command) finds no tests, though this "
                           f"story wrote {', '.join(wrote)}\n{_indent(_tail(text, 6))}\n  ({_test_hint(cmd)})")
        return out

    def _own_tests(self, story: Story, start: int) -> list[tuple[str, str, str]]:
        """The Python test files this story wrote, as (path, the command that
        runs it, ""), or (path, "", the problem) for a test file outside the
        story's files and any tests folder (a stray root test_server.py stayed
        behind in run 2)."""
        pytest = any("pytest" in c for c in self._test_commands())
        out = []
        for path in self._story_facts(start, [])["files"]:
            pp = Path(path)
            if not _RX_TEST_FILE.match(pp.name) or pp.suffix != ".py" \
                    or not (self.workdir / pp).is_file():
                continue
            if not {"test", "tests"} & set(pp.parts[:-1]) and path not in story.files:
                out.append((path, "", "a test file outside the story's files and the tests "
                                      "folder — move it into tests/ or delete it"))
            else:
                out.append((path, f"python3 -m pytest -q {path}" if pytest
                            else f"python3 -m unittest {path}", ""))
        return out

    def _handle_complete_story(self, args: dict) -> str:
        st = getattr(self, "_story", None)
        if self.story_role != "story-exec" or not st:
            return "ERROR: complete_story is only available while a story runs."
        sset = self.plan.story_sets[st["phase"]]
        story = sset.get(st["id"])
        scratch = self._scratch_files(st, story)
        if scratch and not st["scratch_asked"]:
            st["scratch_asked"] = True
            return ("ERROR: not submitted yet: you created files that look like scratch work and are "
                    f"not among the story's files: {', '.join(scratch)}. Delete them, or call "
                    "complete_story again to keep them.")
        failures, passed = self._run_own_tests(story, st["start"])
        if failures:
            st["refusals"] += 1
            story.refusals = st["refusals"]
            self.emit_status()
            report = "\n".join(failures)
            if st["refusals"] >= STORY_MAX_REFUSALS:
                st["failure"] = f"Its tests failed {st['refusals']} times:\n{report}"
                self._step_summary = ""
                return ("ERROR: story not complete — its tests failed again:\n" + report
                        + "\nThe harness stops this story here. Do not call any more tools.")
            return ("ERROR: story not complete — these fail:\n" + report
                    + "\nFix this, then call complete_story again.")
        summary = str(args.get("summary") or "").strip()
        st["passed"] = passed
        if any(c.examples for c in story.acceptance):
            story.delivered = summary
            st["submitted"] = True
            self._step_summary = ""
            return ("Submitted: an independent verifier now tests the expectations against the "
                    "product. If one is not met, you get its report here. Do not call any more "
                    "tools.")
        self._accept_story(st["phase"], story, st, summary)
        self._step_summary = story.delivered or f"Story {story.id} done."
        return "Story accepted: its tests pass."

    def _scratch_files(self, st: dict, story: Story) -> list[str]:
        """Files the story created that look like leftovers of trying things out
        (run 6 left verify_api.py, server.pid and tests/verify_s5.py), bar
        the story's own files and the acceptance folder."""
        before = st.get("files_before")
        if before is None:
            return []
        return [f for f in _project_files(self.workdir, limit=5000)
                if f not in before and f not in story.files
                and not f.startswith(ACCEPTANCE_DIR + "/") and _RX_SCRATCH.match(f.rsplit("/", 1)[-1])]

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
        check = str(args.get("criterion") or "").strip()
        if check:
            # run 3's P1.S2 had `assert Room.__issubclass__`, which no class has;
            # the model added `__issubclass__ = True` to every class to pass it.
            st["defect"], st["bad_check"] = (f"The acceptance criterion \"{check}\" is wrong: "
                                             f"{report}"), True
            return ("Reported: the harness has the story writer correct this expectation; "
                    "a replacement story then runs. Do not call any more tools.")
        st["defect"] = report
        return ("Defect reported. The harness has a story written that fixes it; this story "
                "runs again after that, from the start. Do not call any more tools.")

    def _fix_defect(self, idx: int, story: Story) -> str:
        """A story reported a defect in earlier code: a writer pass adds one fix
        story, and the blocked story waits for it."""
        sset = self.plan.story_sets[idx + 1]
        self.event_queue.put(ChatEvent("system",
            f"Story {story.id} is blocked by a defect in earlier code; asking the story writer "
            f"for a fix story:\n{_tail(story.defect)}"))
        nxt = max((s.number for s in sset.stories), default=0) + 1
        return self._amend_stories(idx, (
            f"Story {story.id} ({story.title}) is blocked by a defect in code an earlier story "
            f"wrote:\n\n{story.defect}\n\nLook at that code and write ONE story, S{nxt}, that "
            f"fixes it and adds a regression test proving the fix. Do not change {story.id} or "
            f"the finished stories: {story.id} runs again once S{nxt} is done (it will depend "
            f"on S{nxt}, so S{nxt} must not depend on {story.id})."), blocks=story.id)

    def _fix_check(self, idx: int, story: Story) -> str:
        """A story reported that one of its own expectations is wrong: the writer
        replaces it with one story doing the same work under correct ones,
        and stories that depended on it depend on the replacement."""
        n = idx + 1
        sset = self.plan.story_sets[n]
        nxt = max((s.number for s in sset.stories), default=0) + 1
        self.event_queue.put(ChatEvent("system",
            f"Story {story.id} reports a wrong expectation; asking the story writer to correct "
            f"it:\n"
            f"{_tail(story.defect)}"))
        return self._amend_stories(idx, replaces=[story.id], remap=story.id, why=(
            f"Story {story.id} ({story.title}) reports that one of its acceptance criteria is "
            f"wrong:\n\n{story.defect}\n\nLook at the criterion and the code. Write ONE story, "
            f"S{nxt}, that replaces {story.id}: the same work (what {story.id} already wrote "
            f"stays in the code) with expectations whose examples are right and checkable. If the "
            f"criterion was right after all, keep it and say in the description what the product "
            f"must do to meet it. Stories that depended on {story.id} will depend on S{nxt}."))

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
            "wrote is what keeps this story from working, fix it when it is small, or call "
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
        return self._amend_stories(idx, replaces=ids, why=(
            f"Story {story.id} ({story.title}) failed:\n\n{story.failure or 'no reason recorded'}\n\n"
            f"Look at the code as it is now and replace {', '.join(ids)} with smaller or "
            f"corrected stories that avoid this. Outline and write ONLY the replacement "
            f"stories, with new ids from S{nxt} on — the finished stories stay as they are. "
            f"What the failed story changed stays in the code: build on what is right, and "
            f"have a replacement undo what is wrong (test data it added to project files). "
            f"Keep the pending stories that do not depend on {story.id}; retire only "
            f"duplicates, or work the replacements now cover, with plan_stories' replaces."))

    # ── the phase gate ────────────────────────────────────────────────────────

    def _phase_checks(self, step: Step, sset: StorySet, last: bool = False) -> tuple[bool, str]:
        """The phase gate (_gate); a re-verification's extra turns last one gate."""
        try:
            return self._gate(step, sset, last)
        finally:
            self._verify_bonus = 0

    def _gate(self, step: Step, sset: StorySet, last: bool) -> tuple[bool, str]:
        """The phase gate, stopping at the first failure; the result is kept on
        the story set for the progress view, with the kinds of failure
        (fail, unclear, ran_out, regression, command, gap) that decide what
        _run_phase does next.
        1. The phase's own expectations, verified like a story's (a verifier
           pass writes and saves their acceptance tests).
        2. Every acceptance test saved so far, in this phase and earlier ones:
           a later story must not break what an earlier one delivered.
        3. At the last phase, the project's test commands: the ones the
           request says must pass, as written (run 2 ended "complete" with
           plain `python -m unittest` finding no tests), and those proving
           its test-suite requirements.
        4. At the last phase, every requirement must be proven."""
        n = self.plan.steps.index(step) + 1
        out = []

        def fail(report: str, kinds: list[str], command: str = "",
                 output: str = "") -> tuple[bool, str]:
            sset.gate = {"passed": False, "command": command, "output": (output or report)[-_RESULT_CHARS:],
                         "kinds": kinds, "at": _now()}
            return False, report

        # the phase's own expectations, and the stories' that waited for the
        # complete path (deferred: their interface did not exist yet)
        waiting = [c for s in sset.stories if s.status == "done" for c in s.acceptance
                   if c.verdict and c.verdict.get("deferred") and not c.retired]
        criteria = [c for c in step.acceptance if c.examples and not c.retired] + waiting
        if criteria:
            pseudo = Story(id="phase", title=step.title, description=step.outcome,
                           delivered=step.outcome, acceptance=criteria)
            found = self._verify_story(n, pseudo, label=f"phase {n}", criteria=criteria,
                                       at_gate=True)
            kinds = [k for k in ("fail", "unclear", "ran_out") if found[k]]
            if kinds:
                ids = {id(c): cid for cid, c, _n in self._criteria(n)}
                return fail("\n\n".join(x for x in (
                    _fail_report(found["fail"]) and
                    "The phase's expected behaviour is not there yet:\n" + _fail_report(found["fail"]),
                    _unclear_report(found["unclear"], ids),
                    _ran_out_report(found["ran_out"])) if x), kinds)
            out.append(f"{len(criteria)} expectation(s) verified"
                       + (f", {len(waiting)} of them deferred by stories" if waiting else ""))
        saved = self._saved_tests(n)
        broken = []
        for cid, c in saved:
            ok, text = self._run_acceptance_test(c.test["command"])
            if not ok:
                broken.append((cid, c, text))
        if broken:
            # all of them, so one fix pass sees every one (run 6 stopped at the first)
            return fail("\n\n".join(
                f"An acceptance test that passed before fails now ({cid}: {c.text}):\n"
                f"`{c.test['command']}`\n{_tail(text, 12)}" for cid, c, text in broken)
                + "\n\n" + _RETIRE_HINT, ["regression"], broken[0][1].test["command"], broken[0][2])
        if saved:
            out.append(f"{len(saved)} acceptance test(s) still pass")
        cmds = self._test_commands() if last else []
        for cmd in cmds:
            self.event_queue.put(ToolCallEvent("phase_check", {"command": cmd}))
            ok, text = tdd_mod.run_tests(cmd, self.workdir, self._cancel)
            self.event_queue.put(ToolResultEvent("phase_check", text))
            if _RX_NO_TESTS.search(text):
                ok, text = False, (text + "\n(no tests ran: a test command that finds no tests "
                                          "proves nothing — unittest discovers tests/ only when "
                                          "it has an __init__.py)")
            if not ok:
                return fail(f"`{cmd}` failed:\n{_tail(text)}", ["command"], cmd, text)
            out.append(f"`{cmd}` passes")
        gaps = self._requirement_gaps(len(self.plan.steps) + 1) if last else []
        if gaps:
            return fail("These requirements of the user's request are not proven by a passing "
                        "acceptance test:\n" + "\n".join(f"- {r['id']}: {r['text']}" for r in gaps),
                        ["gap"])
        if not out:
            out.append("(the phase has nothing to verify)")
        sset.gate = {"passed": True, "output": "\n".join(out), "at": _now()}
        return True, sset.gate["output"]

    def _saved_tests(self, upto: int) -> list[tuple[str, Criterion]]:
        """The proven criteria with a saved acceptance test, in phases up to
        `upto`, bar retired ones: (id, criterion), the id as finish_stories'
        `retire` takes it (P1.S1.4: story S1 of phase 1, expectation 4)."""
        return [(cid, c) for cid, c, _n in self._criteria(upto)
                if c.test and c.proven and not c.retired]

    def _criteria(self, upto: int) -> list[tuple[str, Criterion, int]]:
        """Every criterion of the done stories and of the phases up to `upto`:
        (id, criterion, phase)."""
        out = []
        for n, sset in sorted(self.plan.story_sets.items()):
            if n > upto:
                continue
            for s in sset.stories:
                if s.status == "done":
                    out += [(f"P{n}.{s.id}.{k}", c, n) for k, c in enumerate(s.acceptance, 1)]
        for n, step in enumerate(self.plan.steps[:upto], 1):
            out += [(f"phase {n}.{k}", c, n) for k, c in enumerate(step.acceptance, 1)]
        return out

    def _retire_problems(self, n: int, raw) -> tuple[list[tuple[str, str]], list[str]]:
        """finish_stories' `retire`: ([(id, why)], problems).  An expectation of
        a done story or of a phase up to this one can be retired, with a reason:
        one with a saved test of replaced behaviour, or one the verifier found
        unclear and a new story restates."""
        items, problems = [], []
        if raw in (None, "", []):
            return items, problems
        if not isinstance(raw, list):
            return items, ["retire must be a list of {expectation, why}"]
        known = {cid.lower(): cid for cid, c, _n in self._criteria(n) if not c.retired}
        for x in raw:
            cid = str((x.get("expectation") or x.get("id") or "") if isinstance(x, dict) else x).strip()
            why = str(x.get("why") or x.get("reason") or "").strip() if isinstance(x, dict) else ""
            if cid.lower() not in known:
                shown = list(known.values())
                problems.append(f"retire: {cid or '(no id)'} is not an expectation that can be retired "
                                "(ids look like P1.S1.4 — story S1 of phase 1, expectation 4 — "
                                f"or phase 1.2); these can: {', '.join(shown[:20]) or 'none'}"
                                + (" …" if len(shown) > 20 else ""))
            elif not why:
                problems.append(f"retire: {cid} needs a `why`: what replaced the behaviour it tests")
            else:
                items.append((known[cid.lower()], why))
        return items, problems

    def _apply_retire(self, n: int, items: list[tuple[str, str]]) -> None:
        by = {cid: c for cid, c, _n in self._criteria(n)}
        for cid, why in items:
            c = by.get(cid)
            if c is not None:
                c.retired = {"by": f"phase {n}", "why": why, "at": _now()}
                self.event_queue.put(ChatEvent("system", f"Retired {cid}: {c.text} — {why}"))
        for k, sset in self.plan.story_sets.items():
            if k <= n:
                self._write_stories_file(sset)

    # ── requirements ──────────────────────────────────────────────────────────

    def _coverage(self, upto: int | None = None) -> dict[str, list[tuple[str, Criterion, bool | None]]]:
        """Each requirement id → the verifiable criteria that cover it, as
        (where, criterion, passed: True/False/None = not verified), in phases
        before `upto` (all when None): stories' criteria (replaced stories do
        not count) and the phases' own, each proven by its verifier verdict."""
        out: dict[str, list] = {}
        for n, sset in sorted(self.plan.story_sets.items()):
            if upto is not None and n >= upto:
                continue
            for s in sset.stories:
                if s.status == "replaced":
                    continue
                for c in s.acceptance:
                    if not c.examples or c.retired:
                        continue
                    passed = c.proven if s.status == "done" else None
                    for rid in c.covers:
                        out.setdefault(rid, []).append((f"P{n}.{s.id}", c, passed))
        for n, step in enumerate(self.plan.steps, 1):
            if upto is not None and n >= upto:
                continue
            for c in step.acceptance:
                for rid in c.covers if not c.retired else ():
                    out.setdefault(rid, []).append((f"phase {n}", c, c.proven if c.verdict else None))
        return out

    def _suite_proofs(self) -> dict[str, str]:
        """Requirement id → the test command that proves it, for requirements
        about the test suite (plan.suite_command): the last gate runs it, and no
        expectation covers them."""
        return {r["id"]: cmd for r in self.plan.requirements
                for cmd in [suite_command(r["text"], self.plan.request, self.plan.interfaces)] if cmd}

    def _test_commands(self) -> list[str]:
        """The commands the last gate runs as the project's tests: the request's
        own, then those that prove its test-suite requirements."""
        if self.plan is None:
            return []
        return list(dict.fromkeys(request_commands(self.plan.request)
                                  + list(self._suite_proofs().values())))

    def _requirement_gaps(self, upto: int) -> list[dict]:
        """The requirements of phases before `upto` that no passing check proves
        (bar those the test command proves at the last gate)."""
        cov, suite = self._coverage(upto), self._suite_proofs()
        return [r for r in self.plan.requirements if r["phase"] < upto and r["id"] not in suite
                and not any(passed for _w, _c, passed in cov.get(r["id"], []))]

    def _due_requirements(self, n: int) -> list[dict]:
        """What phase n's stories must cover: its own requirements, and earlier
        phases' that no passing check proves yet."""
        gaps, suite = {r["id"] for r in self._requirement_gaps(n)}, self._suite_proofs()
        return [r for r in self.plan.requirements
                if (r["phase"] == n and r["id"] not in suite) or r["id"] in gaps]

    def _coverage_problems(self, n: int, sset: StorySet) -> list[str]:
        """A submitted set's requirement errors: a due requirement no live
        expectation covers, or a covers id that is no requirement."""
        if not self.plan.requirements:
            return []
        ids = {r["id"] for r in self.plan.requirements}
        covered, errors = set(), []
        for s in sset.stories:
            if s.status == "replaced":
                continue
            for c in s.acceptance:
                if c.retired:                   # proves nothing any more (_coverage)
                    continue
                unknown = [x for x in c.covers if x not in ids]
                if len(c.covers) > MAX_COVERS and not s.finished:
                    # run 3's phase 2: every criterion covered R5–R13, all nine
                    # commands, and parse_command('go north') "proved" quit.
                    errors.append(f"{s.id}: '{c.text[:60]}' covers {len(c.covers)} requirements — "
                                  f"name only what its own examples show (at most {MAX_COVERS}); "
                                  "give each requirement a criterion whose examples exercise it")
                if unknown and not s.finished:
                    errors.append(f"{s.id}: covers {', '.join(unknown)}, which "
                                  f"{'is' if len(unknown) == 1 else 'are'} not among the "
                                  f"requirements ({', '.join(sorted(ids, key=_rnum))})")
                if c.examples:
                    covered |= set(c.covers)
        step = self.plan.steps[n - 1] if n <= len(self.plan.steps) else None
        for c in (step.acceptance if step else []):    # the phase's own expectations
            if not c.retired:
                covered |= set(c.covers)
        for r in self._due_requirements(n):
            if r["id"] not in covered:
                errors.append(f"{r['id']} ({r['text']}) is not covered — give a story an "
                              f"expected behaviour with examples that show it, and covers: "
                              f"[\"{r['id']}\"]")
        return errors

    def _requirement_table(self, upto: int) -> str:
        """The requirements of phases up to `upto` and what proves them, for the
        phase report: written by the harness, not the model."""
        cov, suite = self._coverage(upto + 1), self._suite_proofs()
        lines = []
        for r in self.plan.requirements:
            if r["phase"] > upto:
                continue
            proof = [(w, c) for w, c, passed in cov.get(r["id"], []) if passed]
            if r["id"] in suite and not proof:
                lines.append(f"- · {r['id']} {r['text']} — `{suite[r['id']]}`, run at the last "
                             "phase's gate")
            elif proof:
                w, c = proof[0]
                how = f"`{c.test['command']}`" if c.test else f"\"{c.text}\""
                lines.append(f"- ✓ {r['id']} {r['text']} — {w}: {how}")
            else:
                lines.append(f"- ✗ {r['id']} {r['text']} — not proven by a passing test")
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
                         f"tests passed: {'; '.join(s.facts.get('checks') or []) or '-'}\n"
                         f"verified expectations: "
                         f"{'; '.join(c.text for c in s.acceptance if c.proven) or '-'}"
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
                story.turns = self._story.get("base_turns", 0) + iteration
            self._turns_note(self._story.get("base_turns", 0) + iteration)
        elif getattr(self, "_verifying", None):
            self._verifying["turns"] = iteration
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
                "tests": step.tests, "outcome": step.outcome,
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
            "criteria": [_criterion_view(c) for c in s.acceptance]}


def _criterion_view(c: Criterion) -> dict:
    """A criterion as the plan view shows it: its behaviour with the examples,
    the verifier's saved test ("check") and its verdict."""
    text = c.text + "".join(f"\n  e.g. {e['given']} → {e['expect']}" for e in c.examples)
    result = ({"passed": c.verdict.get("passed"), "output": c.verdict.get("evidence") or "",
               "at": c.verdict.get("at", "")} if c.verdict else None)
    return {"text": text, "check": c.test["command"] if c.test else None, "result": result}


def _dependents_any(sset: StorySet, story_id: str) -> list[str]:
    """Stories of the set that list story_id among their dependencies (any status)."""
    return [s.id for s in sset.stories if story_id in s.depends_on]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _tail(text: str, n: int = 25) -> str:
    return "\n".join(text.splitlines()[-n:])


def _indent(text: str) -> str:
    return "\n".join(f"  {ln}" for ln in text.splitlines())


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


def expected_values(c: Criterion) -> list[str]:
    """The concrete values a criterion's examples expect — `quoted`, "quoted"
    and 'quoted' fragments and numbers — which its acceptance test must check."""
    out = []
    for e in c.examples:
        # an apostrophe inside a word (Ann's) is not a quote
        for m in re.finditer(r"`([^`]+)`|\"([^\"]+)\"|(?<!\w)'([^'\n]+)'(?!\w)|\b(\d+)\b",
                             e.get("expect", "")):
            v = next(g for g in m.groups() if g)
            if v not in out:
                out.append(v)
    return out


def _fail_report(fail: list) -> str:
    lines = []
    for c, evidence in fail:
        lines.append(f"✗ {c.text}")
        lines += [f"  example: {e['given']} → {e['expect']}" for e in c.examples]
        if evidence:
            lines.append(_indent(_tail(evidence, 12)))
        if c.test:
            lines.append(f"  run its test: `{c.test['command']}`")
    return "\n".join(lines)


def _unclear_report(unclear: list, ids: dict | None = None) -> str:
    """Expectations the verifier could not check as written; with ids (by
    id(criterion)), how the writer gets rid of them."""
    if not unclear:
        return ""
    lines = [f"\"{c.text}\"" + (f" ({ids[id(c)]})" if ids and id(c) in ids else "")
             + f" cannot be verified as written: {why}" for c, why in unclear]
    if ids:
        lines.append("Restate each as an expectation of a new story, with examples that can be "
                     "checked, and retire the unclear one with finish_stories' `retire` (its id is "
                     "in brackets).")
    return "\n".join(lines)


def _stuck_reason(sset: StorySet) -> str:
    """Why no story of the set can run, for the pause message."""
    failed = [s.id for s in sset.stories if s.status == "failed"]
    waiting = [s.id for s in sset.blocked()]
    cycle = _cycle([s for s in sset.stories if s.status == "pending"])
    parts = ([f"{', '.join(failed)} failed {MAX_ATTEMPTS} times"] if failed else []) \
        + ([f"{', '.join(waiting)} wait on {'it' if len(failed) == 1 else 'them'}"]
           if waiting and failed else [f"{', '.join(waiting)} cannot run"] if waiting else []) \
        + ([f"a dependency cycle: {' → '.join(cycle)}"] if cycle else [])
    return " and ".join(parts) or "no story is ready to run"


def _ran_out_report(ran_out: list) -> str:
    """Expectations the verifier did not get to: no sign the product is wrong
    (run 6's fix pass answered them with stories that only tested)."""
    if not ran_out:
        return ""
    return ("The verifier ran out of turns before it got a test working for these:\n"
            + "\n".join(f"- {c.text}" + "".join(f"\n  example: {e['given']} → {e['expect']}"
                                                 for e in c.examples) for c, _why in ran_out)
            + "\nThat does not show the product is wrong. Write a story for one only where the "
              "product lacks what its example acts through (an endpoint, a command, a page). Never "
              "write a story whose only work is testing or verifying: the verifier tests them "
              "again at the gate.")


def _project_files(workdir: Path, limit: int = 60) -> list[str]:
    """The project's files (not .git, .momo, caches), for the verifier's brief:
    run 4's verifier spent its turns listing directories."""
    out = []
    for p in sorted(workdir.rglob("*")):
        rel = p.relative_to(workdir)
        if (p.is_file() and not any(part.startswith(".") or part == "__pycache__"
                                    for part in rel.parts)):
            out.append(rel.as_posix())
            if len(out) >= limit:
                out.append("…")
                break
    return out



# `server.py & sleep 2 && curl …`: a lone & leaves the process running (run 3's
# server held :8000), and a kill stops what the test did not start (run 2).
_RX_BACKGROUND = re.compile(r"(?<![&>|])&(?![&>])")
_RX_KILL = re.compile(r"(?:^|[\s;&|(])(?:pkill|killall|kill)\s")


def _test_command_problems(cmd: str) -> list[str]:
    """What is wrong with the command that runs a saved acceptance test."""
    try:
        shlex.split(cmd)
    except ValueError as e:
        return [f"it does not parse as a command ({e})"]
    return ((["it starts a process in the background (&) and leaves it running; the test "
              "starts and stops what it needs itself"] if _RX_BACKGROUND.search(cmd) else [])
            + (["it stops processes it did not start"] if _RX_KILL.search(cmd) else []))


def verify_cap(expectations: int) -> int:
    return min(VERIFY_MAX, VERIFY_BASE + VERIFY_PER_EXPECTATION * expectations)


def _project_imports(workdir: Path, f: Path, text: str, interfaces: list[str]) -> list[str]:
    """The project's own modules a test imports, bar a declared Library
    interface: an acceptance test drives the product, not its code."""
    import ast
    names: set[str] = set()
    if f.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        for node in ast.walk(tree) if tree is not None else ():
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names.add(node.module.split(".")[0])
    else:
        names |= {a or b for a, b in re.findall(r"\bfrom\s+([A-Za-z_]\w*)\s+import|\bimport\s+([A-Za-z_]\w*)", text)}
    libraries = library_modules(interfaces)
    import sys
    return sorted(n for n in names
                  if n not in sys.stdlib_module_names and n not in libraries and n != "tests"
                  and ((workdir / f"{n}.py").exists() or (workdir / n / "__init__.py").exists()
                       or ((workdir / n).is_dir() and any((workdir / n).glob("*.py")))))


_RX_INTERFACE_FILE = re.compile(r"(?<![\w/.:-])((?:[\w-]+/)*[\w-]+\.(?:py|js|mjs|ts|sh|rb|go|rs|java|php|pl))\b")


_NOT_PRODUCT_MODULES = set(sys.stdlib_module_names) | {"pytest", "pip", "coverage", "flask", "uvicorn"}


def _absent_interfaces(workdir: Path, interfaces: list[str]) -> list[str]:
    """The files the declared interfaces run (`python3 server.py` → server.py,
    `python3 -m src.api` → src/api.py) when none of them exists yet; [] when
    one does or none is named."""
    named, paths = [], []
    for x in interfaces:
        for code in re.findall(r"`([^`]+)`", x):
            for m in _RX_INTERFACE_FILE.findall(code):
                if m not in named:
                    named.append(m)
                    paths.append([m])
            for mod in re.findall(r"(?:^|\s)-m\s+([A-Za-z_][\w.]*)", code):
                base = mod.replace(".", "/")
                # `python3 -m unittest` / `-m http.server` run a tool, not the product
                # (run 8 deferred every expectation of phase 1 on "unittest")
                if mod.split(".")[0] in _NOT_PRODUCT_MODULES:
                    continue
                if mod not in named:
                    named.append(mod)
                    paths.append([f"{base}.py", f"{base}/__init__.py", f"{base}/__main__.py"])
    if not named or any((workdir / f).exists() for alts in paths for f in alts):
        return []
    return named


def _test_hint(cmd: str) -> str:
    """How tests must look for the user's test command to find them."""
    if "unittest" in cmd:
        return ("unit tests are unittest.TestCase classes in tests/test_*.py, tests/ has an "
                "__init__.py, and they run from the project root")
    if "pytest" in cmd:
        return "unit tests are test_* functions in tests/test_*.py, run from the project root"
    return "put the unit tests where that command looks for them"
