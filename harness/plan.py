from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# The plan is mirrored to this file in the working directory while it exists.
# It is re-parsed when the user approves the plan (so hand edits are honoured)
# and deleted once every step has been executed.
PLAN_FILENAME = ".momo-plan.md"

# Checkbox marker per status, used both when rendering and parsing the file.
_STATUS_MARK = {"pending": " ", "in_progress": "~", "done": "x", "skipped": "-"}
_MARK_STATUS = {" ": "pending", "~": "in_progress", "x": "done", "X": "done", "-": "skipped"}

_RX_STEP  = re.compile(r'^\s*[-*]\s*\[([ xX~-])\]\s*(?:\d+[.)]\s*)?(.*?)\s*$')
_RX_TITLE = re.compile(r'^#\s+(?:Plan:\s*)?(.+?)\s*$')


@dataclass
class Step:
    title: str
    details: str = ""
    files: list[str] = field(default_factory=list)
    status: str = "pending"   # "pending" | "in_progress" | "done" | "skipped"
    note: str = ""

    @property
    def finished(self) -> bool:
        return self.status in ("done", "skipped")

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        files = d.get("files") or []
        if isinstance(files, str):
            files = [f.strip() for f in files.split(",") if f.strip()]
        status = d.get("status", "pending")
        return cls(
            title=str(d.get("title", "")).strip() or "(untitled step)",
            details=str(d.get("details", "") or "").strip(),
            files=[str(f) for f in files],
            status=status if status in _STATUS_MARK else "pending",
            note=str(d.get("note", "") or "").strip(),
        )

    def to_dict(self) -> dict:
        return {"title": self.title, "details": self.details, "files": self.files,
                "status": self.status, "note": self.note}


@dataclass
class Plan:
    title: str
    goal: str = ""
    steps: list[Step] = field(default_factory=list)

    # ── serialisation ─────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(
            title=str(d.get("title", "")).strip() or "Implementation plan",
            goal=str(d.get("goal", "") or "").strip(),
            steps=[Step.from_dict(s) for s in (d.get("steps") or []) if isinstance(s, dict)],
        )

    def to_dict(self) -> dict:
        return {"title": self.title, "goal": self.goal,
                "steps": [s.to_dict() for s in self.steps]}

    def to_markdown(self) -> str:
        lines = [f"# Plan: {self.title}", ""]
        if self.goal:
            lines += ["## Goal", "", self.goal, ""]
        lines += ["## Steps", ""]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"- [{_STATUS_MARK[s.status]}] {i}. **{s.title}**")
            for dl in s.details.splitlines():
                lines.append(f"  {dl}" if dl.strip() else "")
            if s.files:
                lines.append(f"  Files: {', '.join(s.files)}")
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
                else:
                    detail_lines.append(line[2:] if line.startswith("  ") else line)
            elif section == "goal":
                goal_lines.append(line)
        _flush()
        return cls(title=title or "Implementation plan",
                   goal="\n".join(goal_lines).strip(), steps=steps)

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

    def is_complete(self) -> bool:
        return all(s.finished for s in self.steps)

    def progress(self) -> str:
        """'<current step>/<total>' — the step number being (or next to be) worked on."""
        idx = self.current_index()
        n = len(self.steps)
        return f"{n}/{n}" if idx is None else f"{idx + 1}/{n}"

    def replace_remaining(self, new_steps: list[Step]):
        """Keep finished steps, replace everything else with `new_steps`."""
        self.steps = [s for s in self.steps if s.finished] + new_steps

    def render_for_prompt(self) -> str:
        """Compact status list for the system prompt, with ▶ on the current step."""
        cur = self.current_index()
        out = [f"Plan: {self.title}"]
        if self.goal:
            out.append(f"Goal: {self.goal}")
        for i, s in enumerate(self.steps):
            marker = "▶" if i == cur else " "
            out.append(f"{marker} [{_STATUS_MARK[s.status]}] {i + 1}. {s.title}")
            if i == cur:
                for dl in s.details.splitlines():
                    out.append(f"      {dl}")
                if s.files:
                    out.append(f"      Files: {', '.join(s.files)}")
            elif s.note and s.finished:
                out.append(f"      Done: {s.note}")
        return "\n".join(out)


# ── plan file ─────────────────────────────────────────────────────────────────

def plan_path(workdir: Path) -> Path:
    return workdir / PLAN_FILENAME


def write_plan_file(plan: Plan, workdir: Path) -> str:
    """Write the plan markdown. Returns '' on success or an error message."""
    try:
        plan_path(workdir).write_text(plan.to_markdown(), encoding="utf-8")
        return ""
    except OSError as e:
        return f"could not write {PLAN_FILENAME}: {e}"


def read_plan_file(workdir: Path) -> Plan | None:
    """Parse the plan file; None if it is missing or contains no steps."""
    try:
        text = plan_path(workdir).read_text(encoding="utf-8")
    except OSError:
        return None
    plan = Plan.from_markdown(text)
    return plan if plan.steps else None


def delete_plan_file(workdir: Path):
    try:
        plan_path(workdir).unlink()
    except OSError:
        pass  # already gone (or unremovable) — nothing more to do
