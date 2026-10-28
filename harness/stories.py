"""Stories: the second planning layer of a /plan stories plan.

A stories plan's steps are phases.  When a phase starts, a story writer breaks
it into stories, written against the code as it is then: small technical tasks
(about one definition each) with a description, technical details, hints,
pitfalls and acceptance criteria, ordered by a dependency graph.  The harness
runs one ready story at a time, each in a fresh context, and accepts it only
when its checks pass; what it delivered is recorded for the stories that build
on it.

One phase's stories are a StorySet, mirrored to <stories dir>/phase-N.md: the
harness writes it, the user may edit it before approving it, and it is re-read
on approval (StorySet.from_markdown is tolerant of hand edits, like
Plan.from_markdown).  The stories dir is named after the plan and the time it
was made, so a new plan never reads or overwrites an old one's stories, and it
is kept when the plan completes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import momo_dir

STORIES = "stories"            # .momo/stories/<plan slug>-<YYYYMMDD-HHMM>/phase-N.md
MAX_STORIES = 12               # stories one phase may have
_STATUS_MARK = {"pending": " ", "in_progress": "~", "done": "x", "failed": "!", "replaced": "-"}
_MARK_STATUS = {v: k for k, v in _STATUS_MARK.items()} | {"X": "done"}
_RX_ID = re.compile(r"^S(\d+)$")
_RX_REF = re.compile(r"^P(\d+)\.S(\d+)$")          # a story of another phase
_RX_HEAD = re.compile(r"^##\s*\[([ xX~!-])\]\s*(S\d+)\s*[—:.-]*\s*(.*?)\s*$")
_RX_FIELD = re.compile(r"^(Depends|Files|Attempts):\s*(.*)$", re.I)
_SECTIONS = ("description", "technical details", "hints", "pitfalls", "provides",
             "acceptance", "delivered")


def _items(v) -> list[str]:
    """A list of non-empty strings from a list, or a newline-separated string."""
    if isinstance(v, str):
        v = v.splitlines()
    if not isinstance(v, list):
        return []
    return [str(x).strip().lstrip("-* ").strip() for x in v if str(x).strip()]


def _ids(v) -> list[str]:
    """Story ids from a list or a comma-separated string; "none" and "-" mean none."""
    if isinstance(v, str):
        v = re.split(r"[,\s]+", v)
    if not isinstance(v, list):
        return []
    out = [str(x).strip().upper() for x in v if str(x).strip()]
    return [x for x in out if x not in ("NONE", "-", "(NONE)")]


@dataclass
class Criterion:
    text: str
    check: str | None = None   # a command the harness runs; None: the model backs it with evidence

    @classmethod
    def from_dict(cls, d) -> "Criterion":
        if isinstance(d, str):
            return cls(text=d.strip())
        check = d.get("check")
        return cls(text=str(d.get("text", "") or "").strip(),
                   check=None if check is None else str(check).strip())

    def to_dict(self) -> dict:
        return {"text": self.text, **({"check": self.check} if self.check is not None else {})}


@dataclass
class Story:
    id: str
    title: str
    description: str = ""
    technical_details: str = ""
    hints: list[str] = field(default_factory=list)
    pitfalls: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    acceptance: list[Criterion] = field(default_factory=list)
    status: str = "pending"    # pending | in_progress | done | failed | replaced
    attempts: int = 0
    delivered: str = ""        # the model's note at complete_story
    facts: dict = field(default_factory=dict)   # the harness's: files, definitions, checks
    # How its latest run went, for the progress view (session only, not the file):
    # ISO times, _run_loop turns, complete_story refusals, and per acceptance
    # criterion its last result {"passed": bool | None, "output": str, "at": iso}.
    started_at: str = ""
    finished_at: str = ""
    turns: int = 0
    refusals: int = 0
    check_results: list = field(default_factory=list)
    failure: str = ""          # why its last run failed: the re-split (also on resume) uses it

    @property
    def number(self) -> int:
        m = _RX_ID.match(self.id)
        return int(m.group(1)) if m else 0

    @property
    def finished(self) -> bool:
        return self.status in ("done", "replaced")

    @classmethod
    def from_dict(cls, d: dict) -> "Story":
        status = d.get("status", "pending")
        attempts = _count(d.get("attempts"))
        acc = d.get("acceptance") or []
        return cls(
            id=str(d.get("id", "") or "").strip().upper(),
            title=str(d.get("title", "") or "").strip() or "(untitled story)",
            description=str(d.get("description", "") or "").strip(),
            technical_details=str(d.get("technical_details", "") or "").strip(),
            hints=_items(d.get("hints")),
            pitfalls=_items(d.get("pitfalls")),
            files=_items(d.get("files")),
            depends_on=_ids(d.get("depends_on")),
            provides=_items(d.get("provides")),
            acceptance=[Criterion.from_dict(c) for c in (acc if isinstance(acc, list) else [acc])
                        if isinstance(c, (dict, str))],
            status=status if status in _STATUS_MARK else "pending",
            attempts=attempts,
            delivered=str(d.get("delivered", "") or "").strip(),
            facts=d.get("facts") if isinstance(d.get("facts"), dict) else {},
            started_at=str(d.get("started_at", "") or ""),
            finished_at=str(d.get("finished_at", "") or ""),
            turns=_count(d.get("turns")),
            refusals=_count(d.get("refusals")),
            check_results=[r if isinstance(r, dict) else {} for r in (d.get("check_results") or [])]
                          if isinstance(d.get("check_results"), list) else [],
            failure=str(d.get("failure", "") or ""),
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "description": self.description,
                "technical_details": self.technical_details, "hints": self.hints,
                "pitfalls": self.pitfalls, "files": self.files, "depends_on": self.depends_on,
                "provides": self.provides, "acceptance": [c.to_dict() for c in self.acceptance],
                "status": self.status, "attempts": self.attempts, "delivered": self.delivered,
                "facts": self.facts, "started_at": self.started_at, "finished_at": self.finished_at,
                "turns": self.turns, "refusals": self.refusals, "check_results": self.check_results,
                "failure": self.failure}

    def to_markdown(self) -> str:
        lines = [f"## [{_STATUS_MARK[self.status]}] {self.id} — {self.title}",
                 f"Depends: {', '.join(self.depends_on) or 'none'}"]
        if self.files:
            lines.append(f"Files: {', '.join(self.files)}")
        if self.attempts:
            lines.append(f"Attempts: {self.attempts}")
        for name, body in (("Description", self.description),
                           ("Technical details", self.technical_details)):
            if body:
                lines += ["", f"### {name}", body]
        for name, items in (("Hints", self.hints), ("Pitfalls", self.pitfalls),
                            ("Provides", self.provides)):
            if items:
                lines += ["", f"### {name}"] + [f"- {x}" for x in items]
        lines += ["", "### Acceptance"]
        for c in self.acceptance:
            lines.append(f"- {c.text}")
            if c.check is not None:
                lines.append(f"  Check: `{c.check}`")
        if self.delivered or self.facts:
            lines += ["", "### Delivered"]
            if self.delivered:
                lines.append(self.delivered)
            for key in ("files", "definitions", "checks"):
                if self.facts.get(key):
                    lines.append(f"- {key}: {', '.join(map(str, self.facts[key]))}")
        return "\n".join(lines)


@dataclass
class StorySet:
    """One phase's stories."""
    phase: int                  # 1-based phase number
    title: str = ""             # the phase's title, for the file heading
    stories: list[Story] = field(default_factory=list)
    approved: bool = False
    gate: dict = field(default_factory=dict)   # the phase checks' last result: passed, output, at

    def get(self, story_id: str) -> Story | None:
        return next((s for s in self.stories if s.id == story_id.upper()), None)

    @property
    def done(self) -> bool:
        return bool(self.stories) and all(s.finished for s in self.stories)

    def progress(self) -> str:
        """'<done>/<total>' over the stories that count (replaced ones do not)."""
        live = [s for s in self.stories if s.status != "replaced"]
        return f"{sum(s.status == 'done' for s in live)}/{len(live)}"

    def next_ready(self) -> Story | None:
        """The story to run next: one in progress, else the lowest-numbered
        pending story whose dependencies in this phase are done (dependencies
        on earlier phases were checked done when the set was written)."""
        for s in self.stories:
            if s.status == "in_progress":
                return s
        done = {s.id for s in self.stories if s.status == "done"}
        for s in sorted(self.stories, key=lambda s: s.number):
            if s.status == "pending" and all(d in done or _RX_REF.match(d)
                                             for d in s.depends_on):
                return s
        return None

    def blocked(self) -> list[Story]:
        """Pending stories that can never run: a dependency failed, was
        replaced without a successor, or is itself blocked."""
        ready_or_done = {s.id for s in self.stories if s.status in ("done", "pending", "in_progress")}
        stuck: list[Story] = []
        changed = True
        while changed:
            changed = False
            for s in self.stories:
                if s.status == "pending" and s not in stuck and any(
                        not _RX_REF.match(d) and (d not in ready_or_done
                                                  or any(b.id == d for b in stuck))
                        for d in s.depends_on):
                    stuck.append(s)
                    changed = True
        return stuck

    def validate(self, earlier: dict[str, Story] | None = None,
                 workdir: Path | None = None) -> list[str]:
        """What is wrong with this set ([] when it can run).  earlier: the
        stories of finished phases by "P<n>.S<m>"."""
        earlier = earlier or {}
        errors: list[str] = []
        live = [s for s in self.stories if s.status != "replaced"]
        if not live:
            return ["no stories: write at least one"]
        if len(live) > MAX_STORIES:
            errors.append(f"{len(live)} stories — at most {MAX_STORIES} per phase; "
                          "merge the smallest or move work to a later phase")
        seen: set[str] = set()
        for s in self.stories:
            if not _RX_ID.match(s.id):
                errors.append(f"{s.id or '(no id)'}: ids are S1, S2, …")
            elif s.id in seen:
                errors.append(f"{s.id}: id used twice")
            seen.add(s.id)
        ids = {s.id for s in live}
        for s in live:
            where = f"{s.id} ({s.title})"
            if not s.description:
                errors.append(f"{where}: no description")
            if not s.acceptance:
                errors.append(f"{where}: no acceptance criteria — add at least one, with a "
                              "check command where it can be checked by running something")
            for c in s.acceptance:
                if not c.text:
                    errors.append(f"{where}: an acceptance criterion has no text")
                if c.check is not None and not c.check:
                    errors.append(f"{where}: empty check for \"{c.text}\" — give the command, "
                                  "or leave check out for a criterion backed by evidence")
            for d in s.depends_on:
                if d == s.id:
                    errors.append(f"{where}: depends on itself")
                elif _RX_REF.match(d):
                    dep = earlier.get(d)
                    if dep is None:
                        errors.append(f"{where}: depends on {d}, which is not a story of an "
                                      "earlier phase")
                    elif dep.status != "done":
                        errors.append(f"{where}: depends on {d}, which was not delivered")
                elif d not in ids:
                    errors.append(f"{where}: depends on {d}, which is not in this phase "
                                  "(earlier phases' stories are P<n>.S<m>)")
            for f in s.files:
                if _bad_path(f, workdir):
                    errors.append(f"{where}: file {f} is outside the working directory")
        cycle = _cycle(live)
        if cycle:
            errors.append("dependency cycle: " + " → ".join(cycle))
        return errors

    # ── serialisation ─────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: dict) -> "StorySet":
        return cls(phase=int(d.get("phase") or 1), title=str(d.get("title", "") or ""),
                   stories=[Story.from_dict(s) for s in (d.get("stories") or [])
                            if isinstance(s, dict)],
                   approved=bool(d.get("approved")),
                   gate=d.get("gate") if isinstance(d.get("gate"), dict) else {})

    def to_dict(self) -> dict:
        return {"phase": self.phase, "title": self.title, "approved": self.approved,
                "gate": self.gate, "stories": [s.to_dict() for s in self.stories]}

    def to_markdown(self) -> str:
        head = f"# Stories: phase {self.phase}" + (f" — {self.title}" if self.title else "")
        state = "approved" if self.approved else "draft — edit freely, then approve"
        parts = [head, "", f"Status: {state} · {self.progress()} done", "",
                 "<!-- One story per '## [ ] S<n> — title' heading. Legend: [ ] pending · "
                 "[~] in progress · [x] done · [!] failed · [-] replaced. 'Depends:' lists "
                 "story ids (P<n>.S<m> for earlier phases); 'Check:' under an acceptance "
                 "criterion is a command the harness runs. -->"]
        for s in self.stories:
            parts += ["", s.to_markdown()]
        return "\n".join(parts) + "\n"

    @classmethod
    def from_markdown(cls, text: str, phase: int = 1) -> "StorySet":
        """Parse the format to_markdown() writes, tolerant of hand edits: a
        '## [ ] S<n> — title' heading starts a story; the 'Depends:', 'Files:'
        and 'Attempts:' lines and '### <section>' headings fill it in."""
        title, stories, approved = "", [], False
        cur: Story | None = None
        section = ""
        buf: list[str] = []

        def flush_section():
            nonlocal buf
            if cur is None or not section:
                buf = []
                return
            body = "\n".join(buf).strip()
            if section == "description":
                cur.description = body
            elif section == "technical details":
                cur.technical_details = body
            elif section in ("hints", "pitfalls", "provides"):
                setattr(cur, section, _items(body))
            elif section == "acceptance":
                cur.acceptance = _parse_acceptance(buf)
            elif section == "delivered":
                cur.delivered = "\n".join(ln for ln in buf if not re.match(
                    r"^\s*-\s*(files|definitions|checks):", ln)).strip()
            buf = []

        for line in text.splitlines():
            if line.strip().startswith("<!--"):
                continue
            if line.startswith("# ") and not title:
                m = re.match(r"#\s*Stories:\s*phase\s*(\d+)\s*(?:[—:-]\s*(.*))?$", line.strip())
                if m:
                    phase = int(m.group(1))
                    title = (m.group(2) or "").strip()
                continue
            if line.lower().startswith("status:") and cur is None:
                approved = line.split(":", 1)[1].strip().lower().startswith("approved")
                continue
            m = _RX_HEAD.match(line)
            if m:
                flush_section()
                cur = Story(id=m.group(2).upper(), title=m.group(3) or "(untitled story)",
                            status=_MARK_STATUS.get(m.group(1), "pending"))
                stories.append(cur)
                section = ""
                continue
            if cur is None:
                continue
            if line.startswith("### "):
                flush_section()
                name = line[4:].strip().lower()
                section = name if name in _SECTIONS else "description"
                if name not in _SECTIONS:      # an unknown heading: keep its text
                    buf = [f"{line[4:].strip()}:"] if cur.description == "" else \
                          [cur.description, "", f"{line[4:].strip()}:"]
                continue
            f = _RX_FIELD.match(line.strip()) if not section else None
            if f:
                key, val = f.group(1).lower(), f.group(2).strip()
                if key == "depends":
                    cur.depends_on = _ids(val)
                elif key == "files":
                    cur.files = [x.strip().strip("`") for x in val.split(",") if x.strip()]
                else:
                    cur.attempts = int(val) if val.isdigit() else 0
                continue
            buf.append(line)
        flush_section()
        return cls(phase=phase, title=title, stories=stories, approved=approved)

    def merge_from_file(self, edited: "StorySet") -> None:
        """Take the user's edits from the file: every story that has not been
        delivered is replaced by the file's (a failed one marked [ ] again is
        retried); done and replaced ones keep what the harness recorded
        (delivered work is not rewritten)."""
        kept = {s.id: s for s in self.stories if s.status in ("done", "replaced")}
        before = {s.id: s for s in self.stories}
        merged = []
        for s in edited.stories:
            if s.id in kept:
                merged.append(kept.pop(s.id))
                continue
            old = before.get(s.id)
            if old is not None:          # how its last run went is not in the file
                s.started_at, s.finished_at, s.turns, s.refusals, s.failure = (
                    old.started_at, old.finished_at, old.turns, old.refusals, old.failure)
                if [c.check for c in s.acceptance] == [c.check for c in old.acceptance]:
                    s.check_results = old.check_results
            merged.append(s)
        merged += kept.values()
        merged.sort(key=lambda s: s.number)
        self.stories = merged


def _count(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _parse_acceptance(lines: list[str]) -> list[Criterion]:
    out: list[Criterion] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^check:\s*(.*)$", s, re.I)
        if m and out:
            out[-1].check = m.group(1).strip().strip("`").strip()
        elif s.startswith(("-", "*")):
            out.append(Criterion(text=s.lstrip("-* ").strip()))
        elif out:
            out[-1].text = f"{out[-1].text} {s}".strip()
    return out


def _bad_path(path: str, workdir: Path | None) -> bool:
    p = PurePosixPath(path.strip())
    if p.is_absolute() or ".." in p.parts:
        if workdir is None or not p.is_absolute():
            return True
        try:
            Path(path).resolve().relative_to(workdir.resolve())
        except ValueError:
            return True
    return False


def _cycle(stories: list[Story]) -> list[str]:
    """A dependency cycle among stories (ids, closing on the first), or []."""
    deps = {s.id: [d for d in s.depends_on if not _RX_REF.match(d)] for s in stories}
    state: dict[str, int] = {}          # 1: on the stack, 2: done
    stack: list[str] = []

    def visit(n: str) -> list[str]:
        state[n] = 1
        stack.append(n)
        for d in deps.get(n, []):
            if state.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if d in deps and not state.get(d):
                found = visit(d)
                if found:
                    return found
        state[n] = 2
        stack.pop()
        return []

    for s in sorted(deps):
        if not state.get(s):
            found = visit(s)
            if found:
                return found
    return []


# ── the stories dir ───────────────────────────────────────────────────────────

def slug(title: str) -> str:
    """A folder-safe short name for a plan title."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (s[:40].rstrip("-") or "plan")


def new_stories_dir(workdir: Path, title: str, now: datetime | None = None) -> str | Path:
    """Make .momo/stories/<slug>-<YYYYMMDD-HHMM>[-n]/ for a new plan and return
    it, or an ERROR string."""
    root = momo_dir.ensure(workdir, STORIES)
    if isinstance(root, str):
        return root
    base = f"{slug(title)}-{(now or datetime.now()).strftime('%Y%m%d-%H%M')}"
    for n in range(1, 100):
        d = root / (base if n == 1 else f"{base}-{n}")
        try:
            d.mkdir()
            return d
        except FileExistsError:
            continue
        except OSError as e:
            return f"ERROR: could not create {d}: {e.strerror or e}"
    return f"ERROR: could not find a free folder name for {base} in {root}"


def phase_file(stories_dir: Path, phase: int) -> Path:
    return stories_dir / f"phase-{phase}.md"


def write_story_file(stories_dir: Path, sset: StorySet) -> str:
    """Write a phase's stories; '' or an error message."""
    try:
        if stories_dir.is_symlink():
            return f"could not write stories: {stories_dir} is a symlink"
        stories_dir.mkdir(parents=True, exist_ok=True)
        phase_file(stories_dir, sset.phase).write_text(sset.to_markdown(), encoding="utf-8")
    except OSError as e:
        return f"could not write {phase_file(stories_dir, sset.phase)}: {e.strerror or e}"
    return ""


def read_story_file(stories_dir: Path, phase: int) -> StorySet | None:
    """A phase's stories as the file has them now (None if missing or empty)."""
    p = phase_file(stories_dir, phase)
    if p.is_symlink():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    sset = StorySet.from_markdown(text, phase)
    return sset if sset.stories else None
