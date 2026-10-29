"""Running an approved plan: the Harness methods behind create_plan /
revise_plan / complete_step and the step-by-step execution loop, including a
TDD plan's stages and test gates (see tdd.py)."""
from __future__ import annotations

import json
import re
from datetime import datetime

from . import tdd as tdd_mod
from . import tools as tools_mod
from .tools import registry
from .events import ChatEvent, ErrorEvent, ToolCallEvent, ToolResultEvent
from .plan import (ACCEPTANCE_TAG, PLAN_FILENAME, Plan, Step, delete_plan_file, read_plan_file,
                   str_list, write_plan_file)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# A TDD step that ends with a text reply gets its gate failure back this often
# before the plan pauses.
_TDD_TEXT_RETRIES = 2

TDD_MAX_REFUSALS = 3   # gate refusals in a row before the user is asked

# Tools that cannot change files (Tool.keeps_green): a green run of the test
# command stays valid across them (tdd gate reuse).  Any other call,
# run_command included, ends it.  KEEPS_GREEN: the built-in ones.
KEEPS_GREEN = tools_mod.KEEPS_GREEN


def keeps_green(name: str) -> bool:
    tool = registry.get(name)
    return tool is not None and tool.keeps_green

# Steps a planner writes that a TDD plan does itself: running or keeping the
# suite green ("Run the full test suite", "Keep existing add tests green") and
# its own acceptance step.  The verb must open the title, so "Implement X and
# run tests" is real work.
_RUNS_TESTS = re.compile(r"(?i)^\W*(?:run|re-?run|verify|confirm|check|keep|ensure)\b"
                         r".*\b(?:tests?|suite|green|pass(?:es|ing)?)\b")

_ACCEPTANCE_TITLE = re.compile(r"(?i)^\W*(?:final\s+|end-to-end\s+)?acceptance\b")

_TEST_PATH = re.compile(r"(?i)(?:^|/)tests?/|(?:^|/)[^/]*(?:test|spec)[^/]*$")

def _no_work(step: Step, test_command: str, last: bool) -> bool:
    """A step with nothing to implement in a TDD plan: it touches no production
    file, and its title says it only runs or keeps the tests (or is acceptance,
    which the harness adds itself).  The closing step whose details just run
    the test command counts too."""
    if any(not _TEST_PATH.search(f) for f in step.files):
        return False
    return bool(_RUNS_TESTS.search(step.title) or _ACCEPTANCE_TITLE.search(step.title)
                or (last and test_command in f"{step.title}\n{step.details}"))


class PlanRunnerMixin:
    """Plan mode for Harness: plan state (plan, plan_phase, plan_kind) and the
    loop that executes it one step at a time through Harness._run_loop."""

    def _plan_investigating(self) -> bool:
        return self.mode == "plan" and self.plan_phase != "executing"

    def _plan_executing(self) -> bool:
        # An approved plan runs in coding mode: the coder's role, tools and tool
        # choices, and once it is done the next message is a coding turn, not a
        # new plan.  In any other mode the plan is dormant (paused).
        return self.mode == "coding" and self.plan_phase == "executing" and self.plan is not None

    def _plan_kind_shown(self) -> str:
        """The plan type in force: the running plan's own, else the next plan's."""
        if self.plan is not None and self.plan_phase == "executing":
            return self.plan.kind
        return self.plan_kind

    def set_plan_kind(self, kind: str) -> str:
        """Choose the type of the next plan ("normal" | "tdd" | "stories"); '' or an error."""
        if kind not in ("normal", "tdd", "stories"):
            return f"unknown plan type: {kind} (use normal, tdd or stories)"
        if self.plan is not None and self.plan_phase == "executing":
            return "a plan is executing — its type cannot change. /plan cancel first."
        if kind != self.plan_kind:
            self.plan_kind = kind
            self.rebuild_system_prompt()   # create_plan's schema and the role text change
            self.refresh_status()
            self._autosave()
        return ""

    def _sync_plan_file(self):
        """Mirror the in-memory plan to the plan file; report (not raise) on failure."""
        if self.plan is not None:
            err = write_plan_file(self.plan, self.workdir)
            if err:
                self.event_queue.put(ErrorEvent(err))

    # ── undo of a step's or story's checkpoint ───────────────────────────────

    def _undone_units(self, refs: list) -> tuple[list[int], dict[int, list[str]]]:
        """What undoing checkpoints with these plan refs (checkpoints.begin_turn)
        puts back to pending: step indexes, and per phase the story ids — the
        undone stories, then the done ones that depend on them.  Refs of another
        plan, or of a step revise_plan replaced since, are left out."""
        plan = self.plan
        steps: list[int] = []
        stories: dict[int, list[str]] = {}
        if plan is None:
            return steps, stories
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("plan") != plan.title:
                continue
            idx, n = ref.get("step"), ref.get("phase")
            if isinstance(idx, int) and 0 <= idx < len(plan.steps):
                st = plan.steps[idx]
                if (st.title == ref.get("title") and st.status in ("done", "in_progress")
                        and idx not in steps):
                    steps.append(idx)
            elif isinstance(n, int) and n in plan.story_sets:
                s = plan.story_sets[n].get(str(ref.get("story") or ""))
                ids = stories.setdefault(n, [])
                if s is not None and s.status in ("done", "in_progress") and s.id not in ids:
                    ids.append(s.id)
        # Done stories built on one that goes back, here or in a later phase, go back too.
        frontier = [(n, sid) for n, ids in stories.items() for sid in ids]
        while frontier:
            n, sid = frontier.pop()
            for m, sset in plan.story_sets.items():
                if m < n:
                    continue
                dep = sid if m == n else f"P{n}.{sid}"
                ids = stories.setdefault(m, [])
                for s in sset.stories:
                    if s.status == "done" and dep in s.depends_on and s.id not in ids:
                        ids.append(s.id)
                        frontier.append((m, s.id))
        return sorted(steps), {n: stories[n] for n in sorted(stories) if stories[n]}

    def _units_text(self, steps: list[int], stories: dict[int, list[str]]) -> str:
        """'step 2/5 "Parse args"' or 'P1 S2, S3; P2 S1'."""
        n = len(self.plan.steps)
        parts = [f'step {i + 1}/{n} "{self.plan.steps[i].title}"' for i in steps]
        parts += [f"P{p} " + ", ".join(ids) for p, ids in stories.items()]
        return "; ".join(parts)

    def plan_progress_undone(self, ids: list[int], partial: bool = False) -> str:
        """The confirm line for undoing checkpoints ids: the plan progress that
        goes back with them ('' when none).  partial: some files will be skipped,
        and a checkpoint not taken back completely keeps its progress."""
        refs = [t.get("plan") for t in self.checkpoints.turns() if t["id"] in ids]
        steps, stories = self._undone_units(refs)
        if not (steps or stories):
            return ""
        return (f"Plan progress goes back to pending: {self._units_text(steps, stories)}"
                + (" (for the checkpoints taken back completely)" if partial else ""))

    def _reset_plan_progress(self, refs: list) -> str:
        """Put the steps / stories whose checkpoints were undone back to pending,
        so /plan resume runs them again; the report line ('' when none)."""
        steps, stories = self._undone_units(refs)
        if not (steps or stories):
            return ""
        plan = self.plan
        for i in steps:
            st = plan.steps[i]
            st.status, st.note, st.started_at, st.finished_at, st.turns = "pending", "", "", "", 0
        for n, ids in stories.items():
            sset = plan.story_sets[n]
            for sid in ids:
                s = sset.get(sid)
                s.status, s.delivered, s.facts, s.check_results = "pending", "", {}, []
                s.started_at, s.finished_at, s.turns, s.refusals, s.failure = "", "", 0, 0, ""
            sset.gate = {}
            if n <= len(plan.steps) and plan.steps[n - 1].status == "done":
                phase = plan.steps[n - 1]
                phase.status, phase.finished_at, phase.note, phase.report = "pending", "", "", ""
            self._write_stories_file(sset)
        # current_index() takes a step in progress first: a later one waits now.
        first = min(steps + [n - 1 for n in stories])
        for st in plan.steps[first + 1:]:
            if st.status == "in_progress":
                st.status = "pending"
        self._sync_plan_file()
        self.rebuild_system_prompt()
        n = len(steps) + sum(len(ids) for ids in stories.values())
        return (f"Plan progress back to pending: {self._units_text(steps, stories)} — "
                f"/plan resume runs {'it' if n == 1 else 'them'} again.")

    @staticmethod
    def _parse_steps(raw) -> list[Step]:
        """Build steps from a tool's 'steps' argument.  Tolerates a JSON-encoded
        string and bare strings (title only), which small models sometimes send."""
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = [line for line in raw.splitlines() if line.strip()]
        steps = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict):
                # Status, tag and stage are the harness's to set, not the model's.
                steps.append(Step.from_dict({k: v for k, v in item.items()
                                             if k not in ("status", "tag", "stage")}))
            elif isinstance(item, str) and item.strip():
                steps.append(Step(title=item.strip()))
        return steps

    def approve_plan(self) -> str:
        """Approve the current plan for execution, re-reading the plan file first so
        the user's hand edits are honoured.  Returns '' on success or an error."""
        if self.plan is None:
            return "No plan to run. Switch to /plan and describe a feature or bug first."
        edited = read_plan_file(self.workdir)
        if edited is not None:
            if self.plan.stories and edited.stories:
                # The file holds the phases, not their stories or reports.
                edited.story_sets = self.plan.story_sets
                edited.stories_dir = edited.stories_dir or self.plan.stories_dir
                reports = {s.title: s.report for s in self.plan.steps if s.report}
                for s in edited.steps:
                    s.report = s.report or reports.get(s.title, "")
            self.plan = edited
        if not self.plan.steps:
            return "The plan has no steps."
        self.plan_phase = "executing"
        self._sync_plan_file()
        # The plan runs in coding mode (set_mode rebuilds the prompt and emits status).
        self.set_mode("coding")
        return ""

    def cancel_plan(self) -> str:
        if self.plan is None:
            return "No active plan."
        title = self.plan.title
        self.plan = None
        self.plan_phase = "investigating"
        delete_plan_file(self.workdir)
        self.rebuild_system_prompt()
        self.emit_status()
        return f"Plan discarded: {title} ({PLAN_FILENAME} removed)"

    def _handle_create_plan(self, args: dict) -> str:
        steps = self._parse_steps(args.get("steps"))
        if not steps:
            return ("ERROR: create_plan needs a non-empty 'steps' list of "
                    "{title, details, files} objects. Call it again with the steps.")
        plan = Plan(title=str(args.get("title") or "Implementation plan").strip(),
                    goal=str(args.get("goal") or "").strip(), steps=steps)
        approve_note = ""
        if self.plan_kind == "tdd":
            err = self._make_tdd_plan(plan, args)
            if err:
                return err
            approve_note = f" Approving also lets the harness run `{plan.test_command}` after every step."
        if self.plan_kind == "stories":
            incomplete = [s.title for s in steps if not s.outcome or not s.checks]
            if incomplete:
                return ("ERROR: every phase needs an 'outcome' and at least one command in "
                        f"'checks'; missing in: {', '.join(incomplete)}. Call create_plan again "
                        "with the complete plan.")
            plan.kind = "stories"
            approve_note = (" On approval each phase is broken into stories when it starts"
                            + (", which you approve before they run." if self.stories_approve == "each"
                               else ", which run without asking (/plan stories approve each to review them)."))
        self.plan = plan
        self._sync_plan_file()
        self.event_queue.put(ChatEvent("assistant", self.plan.to_markdown()))
        answer = self._ask_user(
            f"Execute this plan? y = run it · n = keep it for later (/plan run) · "
            f"anything else = feedback to revise it. You can edit {PLAN_FILENAME} before answering."
            + approve_note
        ).strip()
        low = answer.lower().rstrip(".!")
        if low in ("y", "yes", "ok", "go", "run", "approve", "approved"):
            err = self.approve_plan()
            if err:
                return f"ERROR: {err}"
            n = len(self.plan.steps)
            return (f"User approved the plan ({n} steps). The harness will now drive "
                    f"execution one step at a time.")
        if low in ("", "n", "no", "later", "not now"):
            self.plan_phase = "awaiting_approval"
            self.rebuild_system_prompt()
            self.emit_status()
            return (f"User chose to keep the plan for later (saved to {PLAN_FILENAME}). "
                    "Do not call any more tools; reply with one short sentence.")
        self.plan_phase = "investigating"
        self.rebuild_system_prompt()
        return (f"User requested changes to the plan: {answer}\n"
                "Investigate further if needed, then call create_plan again with the complete revised plan.")

    def _make_tdd_plan(self, plan: Plan, args: dict) -> str:
        """Turn plan into a TDD plan: tag its steps and wrap them in the
        test-writing and acceptance stages.  '' or an error for the model."""
        command = str(args.get("test_command") or "").strip()
        if not command:
            return ("ERROR: this is a TDD plan: create_plan needs 'test_command', the command "
                    "that runs the whole test suite. Call it again with it.")
        # The harness runs the suite at every gate and adds the acceptance stage;
        # a step that only does that has nothing to implement and no test to write.
        n = len(plan.steps)
        dropped = [st for i, st in enumerate(plan.steps) if _no_work(st, command, i == n - 1)]
        if dropped and len(dropped) == n:
            return ("ERROR: every step only runs or checks the tests, which a TDD plan does "
                    "itself at every step and in its acceptance stage. Call create_plan again "
                    "with the steps that change the code.")
        if dropped:
            plan.steps = [st for st in plan.steps if st not in dropped]
            self.event_queue.put(ChatEvent("system",
                "Dropped steps the TDD plan does itself (it runs the whole suite at every step "
                "and adds its own acceptance stage): "
                + ", ".join(f'"{st.title}"' for st in dropped)))
        missing = [st.title for st in plan.steps if not st.tests]
        if missing:
            return ("ERROR: every step of a TDD plan needs 'tests' (its expected outcomes). "
                    "Missing on: " + "; ".join(missing) + ". Call create_plan again.")
        acceptance = str_list(args.get("acceptance"))
        plan.kind, plan.test_command = "tdd", command
        plan.step_test_command = str(args.get("step_test_command") or "").strip()
        for i, st in enumerate(plan.steps, 1):
            st.tag = f"tdd-step-{i}"
        plan.steps = ([Step(title="Write the tests, all skipped", stage="tests",
                            details="Write the tests for every step and for acceptance, each "
                                    "skipped with its tag in the skip reason. The suite must "
                                    "still pass.")]
                      + plan.steps
                      + [Step(title="Acceptance: switch on the end-to-end tests", stage="acceptance",
                              tag=ACCEPTANCE_TAG, tests=acceptance,
                              details="Remove the tdd-acceptance skip markers and make the "
                                      "whole suite pass.")])
        return ""

    def _tdd_switch_on(self, step: Step) -> tuple[int, list[str], bool, str] | None:
        """Remove the skip markers of the stage about to start and run the suite
        once, so the model starts from the failures (red before green).
        (markers removed, tagged lines left to the model, passed, output);
        None when no marker could be removed."""
        self.event_queue.put(ToolCallEvent("tdd_switch_on", {"tag": step.tag}))
        removed, left, changed = tdd_mod.switch_on(self.workdir, step.tag)
        if changed and self.index is not None:
            self.index.invalidate(list(changed))
        for rel, old in changed.items():   # shown like any other edit
            self._emit_diff("edit_file", {"path": rel}, "OK", old, True)
        if not removed:
            note = ("no skip markers to remove" if not left else
                    "these tagged lines are not plain skip markers: " + ", ".join(left))
            self.event_queue.put(ToolResultEvent("tdd_switch_on", note))
            return None
        command = tdd_mod.step_command(self.plan, step.tag) or self.plan.test_command
        passed, output = tdd_mod.run_tests(command, self.workdir, self._cancel)
        self.event_queue.put(ToolResultEvent("tdd_switch_on",
            f"removed {removed} skip marker{'s' if removed != 1 else ''}; ran `{command}`: tests "
            f"{'pass' if passed else 'fail'}\n{output}"))
        return removed, left, passed, output

    def _tdd_step_text(self, idx: int, switched=None) -> str:
        """The instruction ending a TDD stage's step message.  switched: what
        _tdd_switch_on did when the stage started (None = nothing)."""
        plan = self.plan
        step = plan.steps[idx]
        step_cmd = tdd_mod.step_command(plan, step.tag) if step.tag else ""
        done = ((f"Run this stage's tests with `{step_cmd}`. " if step_cmd else "")
                + f"To check the whole suite, run exactly `{plan.test_command}` (no pipe): "
                "complete_step reuses that run if you change nothing after it. When the stage is "
                "done, call complete_step: it checks the skip markers and the whole suite, and the "
                "plan only moves on when that passes.")
        if step.stage == "tests":
            want = "\n".join(f"- [{st.tag}] {st.title}:\n" + "\n".join(f"    - {t}" for t in st.tests)
                             for st in plan.steps[idx + 1:] if st.tag)
            return ("Write the complete tests for these outcomes now: setup, the call and the "
                    "assertions. Put the test framework's own skip marker on its own line directly "
                    "above each test, with its [tag] in the skip reason, so the suite still passes. "
                    "A test whose body is only the skip marker checks nothing and is refused. Do not "
                    "implement anything yet (only stubs a skipped test needs to compile or "
                    "import).\n" + want + "\n\n" + done)
        expect = "\n".join(f"- {t}" for t in step.tests)
        acceptance = step.stage == "acceptance"
        what = "end-to-end tests" if acceptance else "tests for this step"
        if switched is None:
            # Resumed, nothing tagged (a step added after the tests were written),
            # or only markers the harness cannot remove.
            text = (f"Make sure the {what} (tagged {step.tag}) run — remove any skip marker with "
                    f"that tag that is left — then implement only this step until they pass:\n"
                    f"{expect}\n")
        else:
            removed, left, passed, output = switched
            text = f"The harness switched on the {removed} {what} tagged {step.tag} and ran them. "
            if not passed and acceptance:
                text += f"They fail:\n{output}\n\nFix what they show.\n"
            elif not passed:
                text += ("They fail, as they should before the implementation:\n"
                         f"{output}\n\nImplement only this step until they pass:\n{expect}\n")
            elif acceptance:
                text += ("They already pass. Check that they cover these outcomes, then call "
                         f"complete_step:\n{expect}\n")
            else:
                text += ("They already pass before any code change. If this step needs no code "
                         "change — what it asks for exists already — call complete_step and say "
                         "so. Otherwise make its tests check the new behaviour first (they must "
                         f"fail), then implement:\n{expect}\n")
            if left:
                text += ("These tagged lines are not plain skip markers — remove the skip from "
                         "them yourself: " + ", ".join(left) + "\n")
        return text + "Tests tagged for later steps stay skipped.\n\n" + done

    def _track_tdd_green(self, name: str, args: dict):
        """After each tool call of a running TDD stage: a passing run of exactly the
        plan's test command makes the suite green; anything that may write ends it."""
        if name == "run_command":
            code, self._last_run_exit = self._last_run_exit, None
            self._tdd_green = code == 0 and tdd_mod.same_command(
                str(args.get("command") or ""), self.plan.test_command)
        elif not keeps_green(name) and not self._mcp_keeps_green(name):
            self._tdd_green = False

    def _check_step_command(self, idx: int):
        """Once the tests are written: the step test command must pass on them
        while they are all skipped.  One that fails (matches no file, wrong
        runner) would show red at every stage, so it is dropped."""
        plan = self.plan
        first = next((st.tag for st in plan.steps[idx + 1:] if st.tag), "")
        command = tdd_mod.step_command(plan, first) if first and plan.step_test_command else ""
        if not command:
            return
        passed, output = tdd_mod.run_tests(command, self.workdir, self._cancel)
        if not passed:
            plan.step_test_command = ""
            tail = "\n".join(output.splitlines()[-8:])
            self.event_queue.put(ChatEvent("system",
                f"The step test command failed on the skipped tests (`{command}`); using the "
                f"full suite instead.\n{tail}"))

    def _tdd_ask_user(self, report: str) -> str:
        """After TDD_MAX_REFUSALS gate refusals in a row: ask the user for
        guidance or to pause.  Returns the text to add to the refusal."""
        n = self._tdd_refusals
        tail = "\n".join(report.splitlines()[-15:])
        answer = self._ask_user(
            f"Plan step {self.plan.progress()} still does not pass its gate after {n} attempts:\n"
            f"{tail}\n\nReply with guidance for the model, or 'pause' to stop the plan here."
        ).strip()
        self._tdd_refusals = 0
        if answer.lower().rstrip(".!") in ("", "pause", "stop", "p", "n", "no"):
            self._tdd_pause = True
            return "\nThe user paused the plan here. Do not call any more tools."
        return f"\nUser guidance after {n} failed attempts: {answer}"

    def _tdd_gate(self) -> tuple[bool, str]:
        """Check the current stage of a running TDD plan (markers + test command)."""
        idx = self.plan.current_index()
        if idx is None:
            return True, ""
        self.event_queue.put(ToolCallEvent("tdd_gate", {"command": self.plan.test_command}))
        step = self.plan.steps[idx]
        notes = []
        if self._tdd_snapshot is not None:
            finished = {st.tag for st in self.plan.steps[:idx] if st.tag and st.finished}
            refusal, notes = tdd_mod.audit(self._tdd_snapshot, self.workdir,
                                           self.plan.test_owners, finished)
            if refusal:
                self.event_queue.put(ToolResultEvent("tdd_gate", refusal))
                return False, refusal
        passed, report = tdd_mod.check_gate(self.plan, idx, self.workdir, self._cancel,
                                            reuse_green=self._tdd_green)
        if passed and step.stage == "tests":
            self.plan.test_owners = tdd_mod.test_owners(self.workdir, tdd_mod.find_tags(self.workdir))
            self._check_step_command(idx)
        if passed and notes:
            self.event_queue.put(ChatEvent("system",
                f"Test lines changed in step {idx + 1}:\n" + "\n".join(f"  {n}" for n in notes)))
        self.event_queue.put(ToolResultEvent("tdd_gate", report))
        self.logger.log_tool_result(self.mode, self.client.model, "tdd_gate", len(report))
        return passed, report

    def _handle_revise_plan(self, args: dict) -> str:
        if not self._plan_executing():
            return "ERROR: revise_plan is only available while executing an approved plan."
        steps = self._parse_steps(args.get("steps"))
        if not steps:
            return "ERROR: revise_plan needs a non-empty 'steps' list of the remaining steps."
        new_titles = {st.title.strip().lower() for st in steps}
        dropped = [st.title for st in self.plan.steps
                   if not st.finished and st.stage == "impl"
                   and st.title.strip().lower() not in new_titles]
        if self.plan.tdd:
            tests_written = any(st.stage == "tests" and st.finished for st in self.plan.steps)
            known = {st.tag for st in self.plan.steps if st.tag}
            self.plan.replace_remaining(steps)
            for st in steps:
                if tests_written and st.tag not in known:
                    st.details = (f"Its tests were not part of the test-writing stage: write "
                                  f"them first (the outcomes below), then implement.\n{st.details}")
            steps = [st for st in self.plan.steps if not st.finished]
        else:
            self.plan.replace_remaining(steps)
        steps[0].status = "in_progress"
        self._sync_plan_file()
        self.rebuild_system_prompt()
        self.emit_status()
        reason = str(args.get("reason") or "").strip()
        start = len(self.plan.steps) - len(steps) + 1
        listing = "\n".join(f"  {start + i}. {st.title}" for i, st in enumerate(steps))
        dropped_txt = ("\nDropped steps:\n" + "\n".join(f"  - {t}" for t in dropped)) if dropped else ""
        self.event_queue.put(ChatEvent("system",
            f"Plan revised{': ' + reason if reason else ''}\nRemaining steps:\n{listing}{dropped_txt}"))
        warn = ("\nThese steps were dropped because they are not in your list — if that was not "
                "intended (or they are not actually done), call revise_plan again including them:"
                + dropped_txt) if dropped else ""
        return (f"Plan revised. Remaining steps:\n{listing}{warn}\n"
                f"Continue with step {start} only: {steps[0].title}")

    def _execute_plan(self):
        """Drive an approved plan one step at a time.  Each step runs the same tool
        loop as coding mode; a text-only reply completes the step.  Any other loop
        outcome (interrupt, error, iteration cap) pauses execution so it can be
        resumed with /plan resume.  The plan file is deleted when all steps are done."""
        plan = self.plan
        if plan is None:
            self.event_queue.put(ChatEvent("system", "No plan to execute."))
            return
        self.plan_phase = "executing"
        if self.mode != "coding":
            self.set_mode("coding")
        if plan.stories:
            self._execute_stories_plan()
            return
        while (idx := plan.current_index()) is not None:
            step = plan.steps[idx]
            first_start = step.status == "pending"
            step.status = "in_progress"
            if first_start or not step.started_at:
                step.started_at, step.finished_at, step.turns = _now(), "", 0
            self._sync_plan_file()
            self.rebuild_system_prompt()
            self.emit_status()
            n = len(plan.steps)
            self.event_queue.put(ChatEvent("system", f"▶ Plan step {idx + 1}/{n}: {step.title}"))
            self.checkpoints.begin_turn(self._turn_count, step.title, step=f"step {idx + 1}/{n}",
                                        plan={"plan": plan.title, "step": idx, "title": step.title})
            # A TDD stage starts with its tests switched on and run once (red first),
            # and with the test files as they are then, to audit at its gate.
            switched = (self._tdd_switch_on(step)
                        if plan.tdd and first_start and step.stage != "tests" else None)
            self._tdd_snapshot = (tdd_mod.snapshot(self.workdir, plan.test_owners)
                                  if plan.tdd and first_start and step.stage != "tests" else None)
            self._tdd_refusals, self._tdd_pause, self._tdd_green = 0, False, False
            body = [f"Plan step {idx + 1}/{n}: {step.title}"]
            if step.details:
                body.append(step.details)
            if step.files:
                body.append(f"Files: {', '.join(step.files)}")
            body.append(self._tdd_step_text(idx, switched) if plan.tdd else
                        "Implement only this step and verify it, then call complete_step with a "
                        "short summary of what you changed and how you verified it.")
            self._append_user("\n\n".join(body))

            tools = self._current_tools() if self.tools_enabled else []
            outcome = self._run_loop(tools, 100)
            # A TDD step that ended with a text reply instead of complete_step
            # has not passed the gate yet: check it, and send failures back.
            retries = 0
            while (outcome == "done" and plan.tdd and self._step_summary is None
                   and plan.current_index() == idx):
                passed, report = self._tdd_gate()
                if passed:
                    break
                if retries >= _TDD_TEXT_RETRIES:
                    outcome = "gate"
                    break
                retries += 1
                self._append_user("The step is not complete — the TDD gate failed.\n" + report
                                  + "\nFix this, then call complete_step.")
                outcome = self._run_loop(tools, 100)
            if outcome != "done":
                self._sync_plan_file()
                if outcome == "gate":
                    self.event_queue.put(ChatEvent("system",
                        f"Step {plan.progress()} ended without passing its tests."))
                self.event_queue.put(ChatEvent("system",
                    f"Plan paused at step {plan.progress()} — type /plan resume (or any "
                    f"message) to continue, /plan cancel to discard. Progress is kept in {PLAN_FILENAME}."))
                return

            # The step that is in progress now may differ from `idx` if the model
            # called revise_plan during the step.
            cur = plan.current_index()
            if cur is not None and plan.steps[cur].status == "in_progress":
                if self._step_summary:
                    note = self._step_summary
                else:  # text-only reply ended the step
                    last = self.messages[-1] if self.messages else {}
                    note = (last.get("content") or "") if last.get("role") == "assistant" else ""
                note = " ".join(note.split())
                plan.steps[cur].status = "done"
                plan.steps[cur].finished_at = _now()
                plan.steps[cur].note = note[:200] + ("…" if len(note) > 200 else "")
            self._sync_plan_file()
            self._autosave()

        total = len(plan.steps)
        self.plan = None
        self.plan_phase = "investigating"
        delete_plan_file(self.workdir)
        self.rebuild_system_prompt()
        self.event_queue.put(ChatEvent("system",
            f"Plan complete: {plan.title} ({total} step{'s' if total != 1 else ''}). {PLAN_FILENAME} removed. "
            "Still in coding mode — /plan to plan the next change."))

    def execute_plan_threaded(self):
        """Run (or resume) plan execution on a worker thread for /plan run|resume."""
        self._cancel.clear()
        self._start_turn_stats()
        try:
            self._execute_plan()
        finally:
            self.emit_status()
            self._autosave()
            self.event_queue.put(self._done_event())

    def _step_tick(self) -> None:
        """One _run_loop turn of a plan step: count it for the plan view."""
        idx = self.plan.current_index()
        if idx is not None:
            self.plan.steps[idx].turns += 1
            self.emit_status()

    def _plan_progress(self) -> str:
        # A running plan shows in every mode (it runs in coding mode); a draft only in plan mode.
        if self.plan is None or (self.mode != "plan" and self.plan_phase != "executing"):
            return ""
        if self.plan_phase == "executing" and self.plan.stories:
            idx = self.plan.current_index()
            sset = self.plan.story_sets.get(idx + 1) if idx is not None else None
            story = getattr(self, "_story", None)
            where = (f" · {story['id']} ({sset.progress()}) · turn {story.get('turns', 0)}"
                     f"/{self.stories_cap}" if story and sset
                     else f" · stories {sset.progress()}" if sset and sset.approved
                     else " · writing stories" if getattr(self, "_writing", None) else "")
            return f"phase {self.plan.progress()}{where}"
        if self.plan_phase == "executing":
            idx = self.plan.current_index()
            turns = self.plan.steps[idx].turns if idx is not None else 0
            return f"exec {self.plan.progress()}" + (f" · turn {turns}" if turns else "")
        if self.plan_phase == "awaiting_approval":
            return "awaiting approval"
        return "draft"
