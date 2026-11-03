"""Stories: the second planning layer of a /plan stories plan.

A stories plan's steps are phases.  When a phase starts, a story writer breaks
it into stories, written against the code as it is then: small technical tasks
(about one definition each) with a description, technical details, hints,
pitfalls and acceptance criteria (expected behaviour with examples), ordered by
a dependency graph.  The harness runs one ready story at a time, each in a
fresh context, and accepts it once an independent verifier's tests of its
examples pass; what it delivered is recorded for the stories that build on it.

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
    """One acceptance criterion: an expected behaviour in plain language
    (`text`) with concrete examples — an action through the product's
    interface and the output it must give.  An independent verifier pass
    writes and runs a test for it after the story is implemented; the harness
    keeps that test (`test`) and its verdict.  (Older plans' shell `check`s
    are no longer read.)"""
    text: str
    covers: list[str] = field(default_factory=list)   # the plan's requirement ids (R1, …) it proves
    examples: list[dict] = field(default_factory=list)  # [{"given": …, "expect": …}]
    test: dict | None = None   # the verifier's saved test: {"path", "command"}
    verdict: dict | None = None  # {"passed": bool, "evidence": str, "at": iso}
    # A later phase replaced the behaviour on purpose (a placeholder reply): its
    # test no longer runs at the gates, and it proves nothing.  {"by", "why", "at"}
    retired: dict | None = None

    @classmethod
    def from_dict(cls, d) -> "Criterion":
        if isinstance(d, str):
            return cls(text=d.strip())
        test = d.get("test") if isinstance(d.get("test"), dict) else None
        verdict = d.get("verdict") if isinstance(d.get("verdict"), dict) else None
        retired = d.get("retired") if isinstance(d.get("retired"), dict) else None
        return cls(text=str(d.get("behaviour") or d.get("text") or "").strip(),
                   covers=_requirement_ids(d.get("covers")),
                   examples=_examples(d.get("examples")),
                   test=({"path": str(test.get("path") or ""), "command": str(test.get("command") or "")}
                         if test and test.get("command") else None),
                   verdict=verdict, retired=retired)

    def to_dict(self) -> dict:
        return {"text": self.text,
                **({"covers": self.covers} if self.covers else {}),
                **({"examples": self.examples} if self.examples else {}),
                **({"test": self.test} if self.test else {}),
                **({"verdict": self.verdict} if self.verdict else {}),
                **({"retired": self.retired} if self.retired else {})}

    def to_markdown(self, indent: str = "  ") -> list[str]:
        """The lines under the criterion's own `- text` line."""
        lines = [f"{indent}Example: {e['given']} → {e['expect']}" for e in self.examples]
        if self.covers:
            lines.append(f"{indent}Covers: {', '.join(self.covers)}")
        if self.test:
            lines.append(f"{indent}Test: `{self.test['command']}`"
                         + (f" ({self.test['path']})" if self.test.get("path") else ""))
        if self.retired:
            lines.append(f"{indent}Retired ({self.retired.get('by', '')}): {self.retired.get('why', '')}")
        return lines

    @property
    def proven(self) -> bool:
        """The verifier's test for it passed."""
        return bool(self.verdict and self.verdict.get("passed"))


def _examples(v) -> list[dict]:
    """[{"given", "expect"}] from a list of dicts or of "given → expect" strings."""
    out = []
    for e in v if isinstance(v, list) else []:
        if isinstance(e, dict):
            given = " ".join(str(e.get("given") or "").split())
            expect = " ".join(str(e.get("expect") or "").split())
        elif isinstance(e, str) and _RX_ARROW.search(e):
            given, expect = (x.strip() for x in _RX_ARROW.split(e, 1))
        else:
            continue
        if given or expect:
            out.append({"given": given, "expect": expect})
    return out


_RX_ARROW = re.compile(r"\s+(?:→|->|=>)\s+")
_RX_TEST_LINE = re.compile(r"^`(.+)`\s*(?:\((.+)\))?$")


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
    facts: dict = field(default_factory=dict)   # the harness's: files, definitions, checks (tests passed)
    # How its latest run went, for the progress view (session only, not the file):
    # ISO times, _run_loop turns and refusals (complete_story's and the verifier's).
    started_at: str = ""
    finished_at: str = ""
    turns: int = 0
    refusals: int = 0
    failure: str = ""          # why its last run failed: the re-split (also on resume) uses it
    defect: str = ""           # the defect in earlier code it reported (report_defect), once

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
            failure=str(d.get("failure", "") or ""),
            defect=str(d.get("defect", "") or ""),
        )

    def problems(self, ids: set[str], earlier: dict[str, "Story"] | None = None,
                 workdir: Path | None = None, libraries=()) -> list[str]:
        """What is wrong with this story on its own ([] when nothing).  ids: the
        stories of this phase it may depend on; earlier: those of finished
        phases by "P<n>.S<m>"; libraries: the modules of a declared Library
        interface (their calls are not code in an expectation)."""
        earlier = earlier or {}
        errors: list[str] = []
        where = f"{self.id} ({self.title})"
        if not self.description:
            errors.append(f"{where}: no description")
        if not self.acceptance:
            errors.append(f"{where}: no acceptance criteria — add at least one expected "
                          "behaviour with examples (an action → the output it must give)")
        for c in self.acceptance:
            errors += [f"{where}: {e}" for e in criterion_problems(c, libraries)]
        errors += _dependency_problems(where, self.id, self.depends_on, ids, earlier)
        for f in self.files:
            if _bad_path(f, workdir):
                errors.append(f"{where}: file {f} is outside the working directory")
        return errors

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "description": self.description,
                "technical_details": self.technical_details, "hints": self.hints,
                "pitfalls": self.pitfalls, "files": self.files, "depends_on": self.depends_on,
                "provides": self.provides, "acceptance": [c.to_dict() for c in self.acceptance],
                "status": self.status, "attempts": self.attempts, "delivered": self.delivered,
                "facts": self.facts, "started_at": self.started_at, "finished_at": self.finished_at,
                "turns": self.turns, "refusals": self.refusals,
                "failure": self.failure, "defect": self.defect}

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
            lines += c.to_markdown()
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
    gate: dict = field(default_factory=dict)   # the phase gate's last result: passed, output, kinds, at
    report: str = ""            # the phase report, once the phase is done (a copy of Step.report)
    # An adding writer pass whose draft this is, until it is approved:
    # {extra, replaces, remap, blocks, may_be_empty, base: [story dicts]}
    amend: dict = field(default_factory=dict)

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
                 workdir: Path | None = None, libraries=()) -> list[str]:
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
            errors += s.problems(ids, earlier, workdir, libraries)
        errors += _duplicates(live)
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
                   gate=d.get("gate") if isinstance(d.get("gate"), dict) else {},
                   report=str(d.get("report", "") or ""),
                   amend=d.get("amend") if isinstance(d.get("amend"), dict) else {})

    def to_dict(self) -> dict:
        d = {"phase": self.phase, "title": self.title, "approved": self.approved,
             "gate": self.gate, "stories": [s.to_dict() for s in self.stories]}
        if self.report:
            d["report"] = self.report
        if self.amend:
            d["amend"] = self.amend
        return d

    def to_markdown(self) -> str:
        head = f"# Stories: phase {self.phase}" + (f" — {self.title}" if self.title else "")
        state = "approved" if self.approved else "draft — edit freely, then approve"
        parts = [head, "", f"Status: {state} · {self.progress()} done", "",
                 "<!-- One story per '## [ ] S<n> — title' heading. Legend: [ ] pending · "
                 "[~] in progress · [x] done · [!] failed · [-] replaced. 'Depends:' lists "
                 "story ids (P<n>.S<m> for earlier phases). Under an acceptance criterion, "
                 "'Example:' is an action → the output it must give, 'Test:' the acceptance "
                 "test the verifier saved for it, and 'Retired' a test a later phase replaced "
                 "on purpose. -->"]
        for s in self.stories:
            parts += ["", s.to_markdown()]
        if self.report:
            parts += ["", "## Report", "", self.report]
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
        in_report, report = False, []

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
            if in_report:                   # the report comes last; its own headings stay in it
                report.append(line)
                continue
            if line.startswith("## ") and not line.startswith("### "):
                flush_section()             # a heading that is not a story ends the last one
                cur, section = None, ""
                in_report = line[3:].strip().lower() == "report"
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
        return cls(phase=phase, title=title, stories=stories, approved=approved,
                   report="\n".join(report).strip())

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
                s.started_at, s.finished_at, s.turns, s.refusals, s.failure, s.defect = (
                    old.started_at, old.finished_at, old.turns, old.refusals, old.failure,
                    old.defect)
                for c in s.acceptance:          # a verdict belongs to an unchanged expectation
                    same = next((o for o in old.acceptance if o.text == c.text
                                 and o.examples == c.examples), None)
                    if same is not None:
                        c.test = c.test or same.test
                        c.verdict = same.verdict
            merged.append(s)
        merged += kept.values()
        merged.sort(key=lambda s: s.number)
        self.stories = merged


def _requirement_ids(v) -> list[str]:
    """R1, R2, … from a list or a comma-separated string, upper-cased, in order."""
    items = v if isinstance(v, list) else str(v or "").split(",")
    out = []
    for x in items:
        x = str(x).strip().strip("`").upper()
        if x and x not in out:
            out.append(x)
    return out


def _count(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


# Code in an expectation: run 2–3's criteria named classes and methods
# (`Room.__issubclass__`, `world.add_player(`), which tied them to code the
# writer had not seen.  A plain `name(args)`, or `module.name(args)` of a
# declared library, stays allowed: a library's public call is how its user
# meets it.  An import counts only as a statement (`tool import data.csv` is a
# command line), a call only with its parenthesis right after the name.
_RX_CODE = re.compile(r"(?:^|[`;]\s*)import\s+[A-Za-z_]|\bfrom\s+[\w.]+\s+import\s+\w"
                      r"|\bassert\b|\bdef\s+\w|\blambda\b|__\w+__"
                      r"|\b([A-Za-z_]\w*)\.[A-Za-z_]\w*\(")


def library_modules(interfaces) -> set[str]:
    """The modules a declared Library interface is used through:
    "Library: `import pager`" and "Library: `from pager import paginate`"."""
    out: set[str] = set()
    for x in interfaces or ():
        if str(x).lower().startswith("library"):
            out |= set(re.findall(r"\b(?:import|from)\s+([A-Za-z_]\w*)", str(x)))
    return out


def _code_in(text: str, libraries=()) -> str:
    """The first piece of code in text ('' when none), bar a declared
    library's own calls."""
    for m in _RX_CODE.finditer(text):
        if m.group(1) and m.group(1) in libraries:
            continue
        return m.group(0).strip(" `;")
    return ""
# Running the test suite is not a behaviour the user sees (run 4b's phase 1:
# "python3 -m unittest test_game.py → exit code 0" claimed all nine commands);
# the harness runs the request's own test command at the end anyway.
_RX_TEST_RUN = re.compile(r"^\W*(?:python3?\s+-m\s+(?:unittest|pytest)|pytest|npm\s+(?:run\s+)?test"
                          r"|yarn\s+test|go\s+test|cargo\s+test|make\s+(?:test|check))\b")
# A value the verifier can check: `quoted`, "quoted", 'quoted', or a number.
_RX_CONCRETE = re.compile(r"`[^`]+`|\"[^\"]+\"|(?<!\w)'[^'\n]+'(?!\w)|\b\d+\b")


# A test file run as the example, in either field (run 8's S5: "… → `python3
# tests/test_frontend_integration.py` exits with code `0`", which its verifier
# could not make sense of and ran out on).
_RX_TEST_FILE_RUN = re.compile(r"(?:\bpython3?(?:\s+-m\s+(?:unittest|pytest))?|\bpytest|\bsh|\bbash"
                               r"|\bnode)\s+[\w./-]*(?:\btests?/|\btest_|_test\.)[\w./-]*")
# An expectation with no examples that only says the tests pass (run 8: "All
# engine and parser tests pass").
_RX_SUITE_TEXT = re.compile(r"(?i)\btests?\b.{0,40}\b(?:pass|passes|succeed|are green)\b|\btest suite\b")


def _runs_tests(e: dict) -> bool:
    given, expect = e.get("given", ""), e.get("expect", "")
    return bool(_RX_TEST_RUN.match(given) or _RX_TEST_FILE_RUN.search(given)
                or _RX_TEST_FILE_RUN.search(expect))


def runs_the_suite(c: Criterion) -> bool:
    """Whether every example of an expectation only runs the tests: the test
    command, or a test file."""
    return bool(c.examples) and all(_runs_tests(e) for e in c.examples)


def drop_suite_runs(criteria: list[Criterion]) -> tuple[list[Criterion], list[Criterion]]:
    """(kept, dropped): the expectations that only run the tests, or with no
    examples only say they pass, taken out while others remain (the harness
    runs the test command at the last gate); alone they stay, and
    criterion_problems refuses them.  Callers drop only when a test command
    is known."""
    suite = [c for c in criteria
             if runs_the_suite(c) or (not c.examples and _RX_SUITE_TEXT.search(c.text))]
    if not suite or len(suite) == len(criteria):
        return list(criteria), []
    return [c for c in criteria if c not in suite], suite


def criterion_notes(c: Criterion) -> list[str]:
    """Advice on an expectation, not a refusal: an example whose expected
    value is not exact (run 6c was refused four times for "the program
    exits", which a verifier can still test)."""
    shown = c.text if len(c.text) <= 60 else c.text[:57] + "…"
    return [f"\"{shown}\": no exact value in \"{e['expect'][:60]}\", so the verifier's test is "
            "held to nothing — give it in backticks or as a number (exit code `0`, status `200`, "
            "the reply contains `Great Hall`)"
            for e in c.examples if e.get("given") and e.get("expect")
            and not _RX_CONCRETE.search(e["expect"])]


def criterion_problems(c: Criterion, libraries=()) -> list[str]:
    """What is wrong with one acceptance criterion as an expectation ([] when
    nothing): it needs the behaviour, at least one example with what is done
    and what must come out, and no code (criterion_notes advises on values)."""
    shown = c.text if len(c.text) <= 60 else c.text[:57] + "…"
    if not c.text:
        return ["an acceptance criterion has no behaviour"]
    errors = []
    if not c.examples:
        errors.append(f"\"{shown}\" needs at least one example: an action through the "
                      "product's interface → the output it must give")
    for e in c.examples:
        if not e.get("given") or not e.get("expect"):
            errors.append(f"\"{shown}\": an example needs both what is done (given) and "
                          "what must come out (expect)")
    if runs_the_suite(c):
        errors.append(f"\"{shown}\": running the tests is not a behaviour the user sees (the "
                      "harness runs the request's test command itself) — give what the user does "
                      "through the product's interface and what comes out")
    code = next((x for x in (_code_in(t, libraries) for t in [c.text] + [v for e in c.examples
                                                                        for v in e.values()]) if x), "")
    if code:
        errors.append(f"\"{shown}\" contains code ({code}…) — "
                      "describe what the user does and observes, not classes, methods or test "
                      "code")
    return errors


def criterion_line(c: Criterion, line: str) -> bool:
    """Apply an `Example:` / `Covers:` / `Test:` line to criterion c; False
    when the line is none of these.  An older plan's `Check:` line is
    consumed and dropped."""
    r = re.match(r"^retired\s*\(([^)]*)\):\s*(.*)$", line.strip(), re.I)
    if r:
        c.retired = {"by": r.group(1).strip(), "why": r.group(2).strip()}
        return True
    m = re.match(r"^(check|covers|example|test):\s*(.*)$", line.strip(), re.I)
    if not m:
        return False
    key, val = m.group(1).lower(), m.group(2).strip()
    if key == "check":
        pass                            # the legacy check model is gone
    elif key == "covers":
        c.covers = _requirement_ids(val)
    elif key == "example":
        c.examples += _examples([val])
    else:
        t = _RX_TEST_LINE.match(val)
        if t:
            c.test = {"command": t.group(1).strip(), "path": (t.group(2) or "").strip()}
    return True


def _parse_acceptance(lines: list[str]) -> list[Criterion]:
    out: list[Criterion] = []
    for line in lines:
        s = line.strip()
        if not s or (out and criterion_line(out[-1], s)):
            continue
        if s.startswith(("-", "*")):
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


def _dependency_problems(where: str, story_id: str, depends_on: list[str], ids: set[str],
                         earlier: dict[str, Story]) -> list[str]:
    errors: list[str] = []
    for d in depends_on:
        if d == story_id:
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
    return errors


def outline_entries(raw) -> list[dict]:
    """plan_stories' outline as [{id, title, depends_on, provides}], tolerant of
    what a model sends (a single item, ids in any case, deps as a string)."""
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    out = []
    for d in items:
        if not isinstance(d, dict):
            continue
        provides = d.get("provides")
        out.append({"id": str(d.get("id", "") or "").strip().upper(),
                    "title": str(d.get("title", "") or "").strip() or "(untitled story)",
                    "depends_on": _ids(d.get("depends_on")),
                    "provides": "; ".join(_items(provides)) if isinstance(provides, list)
                                else str(provides or "").strip()})
    return out


def validate_outline(outline: list[dict], kept: list[Story] | None = None,
                     earlier: dict[str, Story] | None = None) -> list[str]:
    """What is wrong with an outline from outline_entries ([] when nothing).
    kept: the phase's stories a re-split or a fix pass adds to, the replaced
    ones marked so (their ids stay taken); earlier: the stories of finished
    phases by "P<n>.S<m>"."""
    kept = kept or []
    live = [s for s in kept if s.status != "replaced"]
    taken = {s.id for s in kept}
    if not outline:
        return ["no stories: outline at least one"]
    errors: list[str] = []
    if len(live) + len(outline) > MAX_STORIES:
        errors.append(f"{len(live) + len(outline)} stories — at most {MAX_STORIES} per phase; "
                      "merge the smallest or move work to a later phase")
    seen: set[str] = set()
    for e in outline:
        if not _RX_ID.match(e["id"]):
            errors.append(f"{e['id'] or '(no id)'}: ids are S1, S2, …")
        elif e["id"] in seen:
            errors.append(f"{e['id']}: id used twice")
        elif e["id"] in taken:
            nxt = max((s.number for s in kept), default=0) + 1
            errors.append(f"{e['id']}: already a story of this phase — new ids start at S{nxt}")
        seen.add(e["id"])
    ids = seen | {s.id for s in live}
    for e in outline:
        errors += _dependency_problems(f"{e['id']} ({e['title']})", e["id"], e["depends_on"],
                                       ids, earlier or {})
    stubs = live + [Story(id=e["id"], title=e["title"], depends_on=e["depends_on"])
                    for e in outline]
    cycle = _cycle(stubs)
    if cycle:
        errors.append("dependency cycle: " + " → ".join(cycle))
    return errors


def _duplicates(live: list[Story]) -> list[str]:
    """Unfinished stories that are the same story twice: the same title, or the
    same files and examples (a re-split in the story-mud run wrote S2-S4 again as
    S5-S7 and S8-S10 while the originals stayed pending)."""
    errors, seen = [], {}
    for s in live:
        if s.finished or s.status == "in_progress":
            continue
        title = re.sub(r"[\W_]+", " ", s.title.lower()).strip()
        checks = frozenset(f"{e['given']} → {e['expect']}" for c in s.acceptance for e in c.examples)
        keys = []
        if title and title != "untitled story":
            keys.append(("title", title))
        if checks and s.files:
            keys.append(("work", frozenset(s.files), checks))
        first = next((seen[k] for k in keys if k in seen), None)
        if first is not None:
            errors.append(f"{s.id} duplicates {first} (same title, or same files and examples) — "
                          f"keep one: retire the other with plan_stories replaces=[{first}], or "
                          f"drop {s.id} from the outline")
        for k in keys:
            seen.setdefault(k, s.id)
    return errors


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
