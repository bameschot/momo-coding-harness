from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import momo_dir

# The plan is mirrored to this file in the working directory while it exists.
# It is re-parsed when the user approves the plan (so hand edits are honoured)
# and deleted once every step has been executed.
PLAN_FILENAME = momo_dir.PLAN_REL
# Where it lived before .momo/: still read when the new file is missing, and
# removed on the next write or delete.
LEGACY_PLAN_FILENAME = ".momo-plan.md"

# Checkbox marker per status, used both when rendering and parsing the file.
_STATUS_MARK = {"pending": " ", "in_progress": "~", "done": "x", "skipped": "-"}
_MARK_STATUS = {" ": "pending", "~": "in_progress", "x": "done", "X": "done", "-": "skipped"}

_RX_STEP  = re.compile(r'^\s*[-*]\s*\[([ xX~-])\]\s*(?:\d+[.)]\s*)?(.*?)\s*$')
_RX_TITLE = re.compile(r'^#\s+(?:Plan:\s*)?(.+?)\s*$')

# TDD plans: the harness wraps the model's steps in a test-writing stage and an
# acceptance stage.  Pending tests carry their step's tag in the skip reason.
STAGES = ("tests", "impl", "acceptance")
ACCEPTANCE_TAG = "tdd-acceptance"
_RX_STEP_TAG = re.compile(r'^tdd-step-(\d+)$')


def str_list(v) -> list[str]:
    """A list of non-empty strings from a list, or a newline-separated string."""
    if isinstance(v, str):
        v = v.splitlines()
    if not isinstance(v, list):
        return []
    return [str(x).strip().lstrip("-* ").strip() for x in v if str(x).strip()]


@dataclass
class Step:
    title: str
    details: str = ""
    files: list[str] = field(default_factory=list)
    status: str = "pending"   # "pending" | "in_progress" | "done" | "skipped"
    note: str = ""
    # TDD plans only: expected outcomes, the tag its pending tests carry, and
    # which stage it is ("tests" and "acceptance" are added by the harness).
    tests: list[str] = field(default_factory=list)
    tag: str = ""
    stage: str = "impl"

    @property
    def finished(self) -> bool:
        return self.status in ("done", "skipped")

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        files = d.get("files") or []
        if isinstance(files, str):
            files = [f.strip() for f in files.split(",") if f.strip()]
        status = d.get("status", "pending")
        stage = d.get("stage", "impl")
        return cls(
            title=str(d.get("title", "")).strip() or "(untitled step)",
            details=str(d.get("details", "") or "").strip(),
            files=[str(f) for f in files],
            status=status if status in _STATUS_MARK else "pending",
            note=str(d.get("note", "") or "").strip(),
            tests=str_list(d.get("tests")),
            tag=str(d.get("tag", "") or "").strip(),
            stage=stage if stage in STAGES else "impl",
        )

    def to_dict(self) -> dict:
        d = {"title": self.title, "details": self.details, "files": self.files,
             "status": self.status, "note": self.note}
        if self.tests or self.tag or self.stage != "impl":
            d.update(tests=self.tests, tag=self.tag, stage=self.stage)
        return d


@dataclass
class Plan:
    title: str
    goal: str = ""
    steps: list[Step] = field(default_factory=list)
    kind: str = "normal"      # "normal" | "tdd"
    test_command: str = ""    # tdd: the full suite the harness runs at every gate
    # tdd, optional: a template that runs only some test files ({files}, {dirs},
    # {stems}), for the red check and the model's own runs (tdd.step_command)
    step_test_command: str = ""
    # tdd: {path: {test key: tag}}, recorded when the tests are written (tdd.test_owners)
    test_owners: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def tdd(self) -> bool:
        return self.kind == "tdd"

    # ── serialisation ─────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(
            title=str(d.get("title", "")).strip() or "Implementation plan",
            goal=str(d.get("goal", "") or "").strip(),
            steps=[Step.from_dict(s) for s in (d.get("steps") or []) if isinstance(s, dict)],
            kind="tdd" if d.get("kind") == "tdd" else "normal",
            test_command=str(d.get("test_command", "") or "").strip(),
            step_test_command=str(d.get("step_test_command", "") or "").strip(),
            test_owners={str(k): {str(t): str(g) for t, g in v.items()}
                         for k, v in (d.get("test_owners") or {}).items() if isinstance(v, dict)},
        )

    def to_dict(self) -> dict:
        d = {"title": self.title, "goal": self.goal,
             "steps": [s.to_dict() for s in self.steps]}
        if self.tdd:
            d.update(kind=self.kind, test_command=self.test_command,
                     step_test_command=self.step_test_command, test_owners=self.test_owners)
        return d

    def to_markdown(self) -> str:
        lines = [f"# Plan: {self.title}", ""]
        if self.tdd:
            lines += ["Type: TDD", ""]
        if self.goal:
            lines += ["## Goal", "", self.goal, ""]
        if self.tdd:
            lines += ["## Test command", "", f"`{self.test_command}`", ""]
            if self.step_test_command:
                lines += ["## Step test command", "", f"`{self.step_test_command}`", ""]
        lines += ["## Steps", ""]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"- [{_STATUS_MARK[s.status]}] {i}. **{s.title}**")
            for dl in s.details.splitlines():
                lines.append(f"  {dl}" if dl.strip() else "")
            if s.files:
                lines.append(f"  Files: {', '.join(s.files)}")
            for t in s.tests:
                lines.append(f"  Expect: {t}")
            if s.tag:
                lines.append(f"  Tag: {s.tag}")
            if s.stage != "impl":
                lines.append(f"  Stage: {s.stage}")
            if s.note:
                lines.append(f"  Note: {s.note}")
        lines += ["", "<!-- Legend: [ ] pending · [~] in progress · [x] done · [-] skipped. "
                  "Edit steps freely before approving; this file is deleted when the plan completes. -->"]
        return "\n".join(lines) + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "Plan":
        """Parse the format written by to_markdown().  Tolerant of hand edits:
        any checkbox list item starts a step, and the lines that follow it (up to
        the next step or heading) become its details, with 'Files:' and 'Note:'
        lines split out."""
        title, goal_lines, steps = "", [], []
        kind, command_lines, step_command_lines = "normal", [], []
        section = ""
        cur: Step | None = None
        detail_lines: list[str] = []

        def _flush():
            if cur is not None:
                cur.details = "\n".join(detail_lines).strip()
                steps.append(cur)

        for line in text.splitlines():
            if line.strip().startswith("<!--"):
                continue
            if line.startswith("# ") and not title:
                m = _RX_TITLE.match(line)
                title = m.group(1) if m else line[2:].strip()
                continue
            if line.startswith("## "):
                _flush()
                cur, detail_lines = None, []
                section = line[3:].strip().lower()
                continue
            if not section and line.strip().lower() == "type: tdd":
                kind = "tdd"
                continue
            if section in ("test command", "step test command"):
                if line.strip():
                    (command_lines if section == "test command" else step_command_lines).append(
                        line.strip().strip("`").strip())
                continue
            m = _RX_STEP.match(line)
            if m and section != "goal":
                _flush()
                step_title = m.group(2).strip()
                if step_title.startswith("**") and step_title.endswith("**") and len(step_title) > 4:
                    step_title = step_title[2:-2].strip()
                cur = Step(title=step_title or "(untitled step)",
                           status=_MARK_STATUS.get(m.group(1), "pending"))
                detail_lines = []
                continue
            if cur is not None:
                s = line.strip()
                if s.lower().startswith("files:"):
                    cur.files = [f.strip().strip("`") for f in s[6:].split(",") if f.strip()]
                elif s.lower().startswith("note:"):
                    cur.note = s[5:].strip()
                elif s.lower().startswith("expect:"):
                    if s[7:].strip():
                        cur.tests.append(s[7:].strip())
                elif s.lower().startswith("tag:"):
                    cur.tag = s[4:].strip().strip("`")
                elif s.lower().startswith("stage:") and s[6:].strip().lower() in STAGES:
                    cur.stage = s[6:].strip().lower()
                else:
                    detail_lines.append(line[2:] if line.startswith("  ") else line)
            elif section == "goal":
                goal_lines.append(line)
        _flush()
        return cls(title=title or "Implementation plan",
                   goal="\n".join(goal_lines).strip(), steps=steps, kind=kind,
                   test_command=" ".join(command_lines),
                   step_test_command=" ".join(step_command_lines))

    # ── progress ──────────────────────────────────────────────────────────────

    def current_index(self) -> int | None:
        """Index of the step to work on next (in progress first, else first pending)."""
        for i, s in enumerate(self.steps):
            if s.status == "in_progress":
                return i
        for i, s in enumerate(self.steps):
            if not s.finished:
                return i
        return None

    def progress(self) -> str:
        """'<current step>/<total>' — the step number being (or next to be) worked on."""
        idx = self.current_index()
        n = len(self.steps)
        return f"{n}/{n}" if idx is None else f"{idx + 1}/{n}"

    def replace_remaining(self, new_steps: list[Step]):
        """Keep finished steps, replace everything else with `new_steps`.

        A TDD plan keeps its harness-made stages: an unfinished test-writing
        stage stays first and the acceptance stage stays last.  A new step whose
        title matches an unfinished one keeps that step's tag (its tests may
        already be written); any other new step gets a fresh tag."""
        if not self.tdd:
            self.steps = [s for s in self.steps if s.finished] + new_steps
            return
        old_tags = {s.title.strip().lower(): s.tag for s in self.steps
                    if not s.finished and s.stage == "impl" and s.tag}
        keep_first = [s for s in self.steps if not s.finished and s.stage == "tests"]
        keep_last = [s for s in self.steps if not s.finished and s.stage == "acceptance"]
        stage_titles = {s.title.strip().lower() for s in keep_first + keep_last}
        new_steps = [s for s in new_steps
                     if s.stage == "impl" and s.title.strip().lower() not in stage_titles]
        self.steps = ([s for s in self.steps if s.finished] + keep_first
                      + new_steps + keep_last)
        for s in new_steps:
            s.tag = old_tags.get(s.title.strip().lower(), "")
        for s in new_steps:
            if not s.tag:
                s.tag = self.next_tag()

    def next_tag(self) -> str:
        """A step tag no step of this plan uses yet."""
        used = [int(m.group(1)) for s in self.steps if (m := _RX_STEP_TAG.match(s.tag))]
        return f"tdd-step-{max(used, default=0) + 1}"

    def tags(self) -> list[str]:
        """Every tag of this plan, in step order."""
        return [s.tag for s in self.steps if s.tag]

    def render_for_prompt(self) -> str:
        """Compact status list for the system prompt, with ▶ on the current step."""
        cur = self.current_index()
        out = [f"Plan: {self.title}"]
        if self.goal:
            out.append(f"Goal: {self.goal}")
        if self.tdd:
            out.append(f"Test command (run by the harness at every gate): {self.test_command}")
        writing_tests = cur is not None and self.steps[cur].stage == "tests"
        for i, s in enumerate(self.steps):
            marker = "▶" if i == cur else " "
            tag = f"  [{s.tag}]" if s.tag else ""
            out.append(f"{marker} [{_STATUS_MARK[s.status]}] {i + 1}. {s.title}{tag}")
            if i == cur:
                for dl in s.details.splitlines():
                    out.append(f"      {dl}")
                if s.files:
                    out.append(f"      Files: {', '.join(s.files)}")
            if (i == cur or writing_tests) and s.tests:
                for t in s.tests:
                    out.append(f"      Expect: {t}")
            elif i != cur and s.note and s.finished:
                out.append(f"      Done: {s.note}")
        return "\n".join(out)


# ── plan file ─────────────────────────────────────────────────────────────────

def plan_path(workdir: Path) -> Path:
    return momo_dir.plan_path(workdir)


def _drop_legacy(workdir: Path):
    try:
        (workdir / LEGACY_PLAN_FILENAME).unlink()
    except OSError:
        pass  # never there (or unremovable) — nothing more to do


def write_plan_file(plan: Plan, workdir: Path) -> str:
    """Write the plan markdown. Returns '' on success or an error message."""
    folder = momo_dir.ensure(workdir)
    if isinstance(folder, str):
        return f"could not write {PLAN_FILENAME}: {folder.removeprefix('ERROR: ')}"
    try:
        plan_path(workdir).write_text(plan.to_markdown(), encoding="utf-8")
    except OSError as e:
        return f"could not write {PLAN_FILENAME}: {e}"
    _drop_legacy(workdir)
    return ""


def read_plan_file(workdir: Path) -> Plan | None:
    """Parse the plan file (or the pre-.momo one); None if it is missing or
    contains no steps."""
    for p in (plan_path(workdir), workdir / LEGACY_PLAN_FILENAME):
        if p.is_symlink():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        plan = Plan.from_markdown(text)
        return plan if plan.steps else None
    return None


def delete_plan_file(workdir: Path):
    p = plan_path(workdir)
    try:
        if not p.parent.is_symlink():
            p.unlink()
    except OSError:
        pass  # already gone (or unremovable) — nothing more to do
    _drop_legacy(workdir)
