"""Stories (/plan stories): the story model, its markdown file, validation of a
phase's story set, the order stories run in, and the plan's phase fields."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from harness.plan import Plan, Step
from harness.stories import (MAX_STORIES, Criterion, Story, StorySet, criterion_problems,
                             new_stories_dir, outline_entries, read_story_file, slug,
                             validate_outline, write_story_file)


def story(sid, deps=(), status="pending", **kw):
    kw.setdefault("description", f"Do {sid}.")
    kw.setdefault("acceptance", [Criterion("it works", examples=[{"given": "`run`", "expect": "`ok`"}])])
    return Story(id=sid, title=f"Story {sid}", depends_on=list(deps), status=status, **kw)


FULL = Story(
    id="S2", title="Frame encoding: encode_frame()",
    description="Encode one WebSocket frame.",
    technical_details="Byte 0: FIN | opcode.\nByte 1: MASK bit | 7-bit length.",
    hints=["struct.pack('>H', n) for the 16-bit length"],
    pitfalls=["126 bytes already needs the 16-bit length", "the mask key comes after the length"],
    files=["mud/ws.py"], depends_on=["S1", "P1.S3"],
    provides=["encode_frame(opcode: int, payload: bytes, mask_key: bytes | None = None) -> bytes"],
    acceptance=[Criterion("RFC 6455 §5.7 'Hello' encodes to 81 05 48 65 6c 6c 6f", ["R2", "R4"],
                          examples=[{"given": "the server sends `Hello`",
                                     "expect": "the client reads bytes `81 05 48 65 6c 6c 6f`"}]),
                Criterion("the docstring names the RFC section"),
                Criterion("a short message is one frame", covers=["R1"],
                          examples=[{"given": "the client sends `hi`",
                                     "expect": "the server reads bytes `81 02 68 69`"}],
                          test={"path": "tests/acceptance/p1_s2_c3_frame.sh",
                                "command": "sh tests/acceptance/p1_s2_c3_frame.sh"})],
    status="done", attempts=1, delivered="Added encode_frame(); kept masking in a helper.",
    facts={"files": ["mud/ws.py"], "definitions": ["encode_frame", "_mask"], "checks": ["passed 1"]})


class Model(unittest.TestCase):
    def test_dict_round_trip(self):
        self.assertEqual(Story.from_dict(FULL.to_dict()), FULL)
        sset = StorySet(phase=2, title="Protocol", stories=[FULL], approved=True)
        self.assertEqual(StorySet.from_dict(sset.to_dict()), sset)

    def test_lenient_input(self):
        s = Story.from_dict({"id": "s3", "title": "x", "depends_on": "S1, s2",
                             "hints": "one\ntwo", "acceptance": ["plain text"]})
        self.assertEqual((s.id, s.depends_on, s.hints), ("S3", ["S1", "S2"], ["one", "two"]))
        self.assertEqual(s.acceptance, [Criterion("plain text")])
        self.assertEqual(Story.from_dict({"id": "S1", "depends_on": "none"}).depends_on, [])


class Markdown(unittest.TestCase):
    def test_round_trip(self):
        sset = StorySet(phase=2, title="Protocol", approved=True,
                        stories=[story("S1"), FULL])
        back = StorySet.from_markdown(sset.to_markdown())
        self.assertEqual((back.phase, back.title, back.approved), (2, "Protocol", True))
        b = back.get("S2")
        for f in ("title", "description", "technical_details", "hints", "pitfalls", "files",
                  "depends_on", "provides", "acceptance", "status", "attempts", "delivered"):
            self.assertEqual(getattr(b, f), getattr(FULL, f), f)
        self.assertEqual(b.facts, {})        # facts live in the session, not the file
        self.assertIn("  Covers: R2, R4", sset.to_markdown())

    def test_hand_edits(self):
        text = StorySet(phase=1, stories=[story("S1"), story("S2", ["S1"])]).to_markdown()
        text = text.replace("## [ ] S2 — Story S2", "## [ ] S2 — Decode frames")
        text = text.replace("Depends: S1", "Depends: none")
        text += ("\n## [ ] S3 — Added by hand\nDepends: S1, S2\n\n### Description\nNew.\n\n"
                 "### Notes from me\nKeep it short.\n\n### Acceptance\n- tests pass\n"
                 "  Check: `python3 -m unittest`\n- reads well\n")   # an older plan's Check: is dropped
        back = StorySet.from_markdown(text)
        self.assertEqual([s.id for s in back.stories], ["S1", "S2", "S3"])
        self.assertEqual(back.get("S2").title, "Decode frames")
        self.assertEqual(back.get("S2").depends_on, [])
        s3 = back.get("S3")
        self.assertEqual(s3.depends_on, ["S1", "S2"])
        self.assertIn("Keep it short.", s3.description)       # an unknown heading is kept
        self.assertEqual(s3.acceptance, [Criterion("tests pass"), Criterion("reads well")])

    def test_merge_keeps_what_was_delivered(self):
        sset = StorySet(phase=1, stories=[FULL, story("S3", ["S2"])])
        edited = StorySet.from_markdown(sset.to_markdown())
        edited.get("S2").title = "rewritten after the fact"
        edited.get("S3").title = "Narrower S3"
        sset.merge_from_file(edited)
        self.assertEqual(sset.get("S2"), FULL)
        self.assertEqual(sset.get("S3").title, "Narrower S3")

    def test_run_data_lives_in_the_session_not_the_file(self):
        s = story("S1", status="failed", started_at="2026-10-28T15:00:00",
                  finished_at="2026-10-28T15:06:00", turns=12, refusals=2,
                  failure="AssertionError")
        sset = StorySet(phase=1, stories=[s], gate={"passed": False, "output": "x", "at": "t"})
        self.assertEqual(StorySet.from_dict(sset.to_dict()), sset)
        md = sset.to_markdown()
        for leak in ("15:06", "AssertionError", "turns", "refusals"):
            self.assertNotIn(leak, md)

    def test_a_merge_keeps_the_last_run(self):
        s = story("S1", status="failed", turns=12, refusals=2, failure="boom")
        s.acceptance[0].verdict = {"passed": False, "evidence": "x", "at": "t"}
        sset = StorySet(phase=1, stories=[s])
        sset.merge_from_file(StorySet.from_markdown(sset.to_markdown()))
        back = sset.get("S1")
        self.assertEqual((back.turns, back.refusals, back.failure), (12, 2, "boom"))
        self.assertEqual(back.acceptance[0].verdict["passed"], False)
        edited = StorySet.from_markdown(sset.to_markdown().replace("`ok`", "`fine`"))
        sset.merge_from_file(edited)
        self.assertIsNone(sset.get("S1").acceptance[0].verdict)     # its expectation changed

    def test_a_failed_story_reset_in_the_file_is_retried(self):
        sset = StorySet(phase=1, stories=[story("S1", status="failed", attempts=2)])
        text = sset.to_markdown().replace("## [!] S1", "## [ ] S1")
        sset.merge_from_file(StorySet.from_markdown(text))
        self.assertEqual((sset.get("S1").status, sset.get("S1").attempts), ("pending", 2))


class Validate(unittest.TestCase):
    def errors(self, *stories, earlier=None):
        return StorySet(phase=2, stories=list(stories)).validate(earlier)

    def test_a_good_set(self):
        done = story("S3", status="done")
        self.assertEqual(self.errors(story("S1"), story("S2", ["S1", "P1.S3"]),
                                     earlier={"P1.S3": done}), [])

    def test_ids_and_dependencies(self):
        errs = "\n".join(self.errors(story("S1"), story("S1"), story("X9"),
                                     story("S4", ["S7"]), story("S5", ["S5"])))
        for want in ("S1: id used twice", "X9: ids are S1", "depends on S7, which is not in "
                     "this phase", "S5 (Story S5): depends on itself"):
            self.assertIn(want, errs)

    def test_a_cycle(self):
        errs = self.errors(story("S1", ["S3"]), story("S2", ["S1"]), story("S3", ["S2"]))
        self.assertTrue(any(e.startswith("dependency cycle: S1 → S3 → S2 → S1") for e in errs), errs)

    def test_earlier_phases(self):
        failed = story("S2", status="failed")
        errs = "\n".join(self.errors(story("S1", ["P1.S2", "P1.S9"]),
                                     earlier={"P1.S2": failed}))
        self.assertIn("depends on P1.S2, which was not delivered", errs)
        self.assertIn("depends on P1.S9, which is not a story of an earlier phase", errs)

    def test_criteria_and_files(self):
        errs = "\n".join(self.errors(
            story("S1", acceptance=[]),
            story("S3", files=["../outside.py"]),
            story("S4", description="")))
        self.assertIn("S1 (Story S1): no acceptance criteria", errs)
        self.assertIn("file ../outside.py is outside", errs)
        self.assertIn("S4 (Story S4): no description", errs)

    def test_size_limit_and_empty(self):
        many = [story(f"S{n}") for n in range(1, MAX_STORIES + 2)]
        self.assertIn(f"at most {MAX_STORIES} per phase", "\n".join(self.errors(*many)))
        self.assertEqual(self.errors(), ["no stories: write at least one"])
        self.assertEqual(self.errors(story("S1", status="replaced")),
                         ["no stories: write at least one"])


class Expectations(unittest.TestCase):
    """Acceptance criteria are behaviour with examples, never code (run 3's
    `assert Room.__issubclass__`, `parse_command('go north')` proving quit)."""

    def test_a_good_expectation(self):
        c = Criterion("look describes the room", examples=[
            {"given": "Ann in the Great Hall sends `look`",
             "expect": "the reply names `Great Hall` and the exits `north`, `east`"}])
        self.assertEqual(criterion_problems(c), [])
        http = Criterion("paths outside the folder are refused",
                         examples=[{"given": "GET /../server.py", "expect": "status 404"}])
        self.assertEqual(criterion_problems(http), [])
        lib = Criterion("pages of 5", examples=[{"given": "paginate(range(11), 5)",
                                                 "expect": "3 pages, the last `[10]`"}])
        self.assertEqual(criterion_problems(lib), [])

    def test_what_is_refused(self):
        cases = {
            "needs at least one example": Criterion("look works"),
            "needs both what is done": Criterion("look works", examples=[{"given": "", "expect": "`x`"}]),
            "contains code": Criterion("models exist", examples=[
                {"given": "import models", "expect": "`True`"}]),
        }
        for why, c in cases.items():
            with self.subTest(why):
                self.assertTrue(any(why in e for e in criterion_problems(c)), criterion_problems(c))
        from harness.stories import criterion_notes
        vague = Criterion("look works", examples=[{"given": "Ann sends look",
                                                   "expect": "the room is described"}])
        self.assertEqual(criterion_problems(vague), [])                 # advice, not a refusal
        self.assertTrue(any("no exact value" in x for x in criterion_notes(vague)))
        suite = Criterion("commands work", examples=[{"given": "python3 -m unittest test_game.py",
                                                      "expect": "exit code `0`"}])
        self.assertTrue(any("running the tests is not a behaviour" in e for e in criterion_problems(suite)))
        dotted = Criterion("rooms", examples=[{"given": "Room.__init__(x)", "expect": "`1`"}])
        self.assertTrue(any("contains code" in e for e in criterion_problems(dotted)))

    def test_examples_from_strings_and_markdown(self):
        c = Criterion.from_dict({"behaviour": "b", "examples": ["`look` → names `Hall`"]})
        self.assertEqual(c.examples, [{"given": "`look`", "expect": "names `Hall`"}])
        self.assertEqual(c.text, "b")


class Duplicates(unittest.TestCase):
    def test_duplicates_are_refused(self):
        a = story("S2", files=["game.py"])
        b = story("S5", files=["game.py"])
        b.title = a.title.upper() + "!"
        errors = StorySet(phase=1, stories=[a, b]).validate({})
        self.assertTrue(any(e.startswith("S5 duplicates S2") for e in errors))
        b.title, b.acceptance = "Something else", [Criterion("x", examples=[{"given": "`b`",
                                                                          "expect": "`2`"}])]
        self.assertEqual(StorySet(phase=1, stories=[a, b]).validate({}), [])


class Outline(unittest.TestCase):
    """plan_stories' outline, and one story checked on its own (write_story)."""

    def test_entries_are_lenient(self):
        self.assertEqual(outline_entries({"id": "s1", "title": " A ", "depends_on": "none",
                                          "provides": ["f()", "g()"]}),
                         [{"id": "S1", "title": "A", "depends_on": [], "provides": "f(); g()"}])
        self.assertEqual(outline_entries("nonsense"), [])

    def test_a_good_outline(self):
        out = outline_entries([{"id": "S1", "title": "a"},
                               {"id": "S2", "title": "b", "depends_on": ["S1", "P1.S1"]}])
        self.assertEqual(validate_outline(out, earlier={"P1.S1": story("S1", status="done")}), [])

    def test_ids_dependencies_cycles_and_size(self):
        self.assertEqual(validate_outline([]), ["no stories: outline at least one"])
        errs = validate_outline(outline_entries([
            {"id": "X1", "title": "a"}, {"id": "S2", "title": "b", "depends_on": ["S9"]},
            {"id": "S2", "title": "c"}]))
        self.assertTrue(any("X1: ids are S1" in e for e in errs))
        self.assertTrue(any("S2: id used twice" in e for e in errs))
        self.assertTrue(any("depends on S9, which is not in this phase" in e for e in errs))
        errs = validate_outline(outline_entries([{"id": "S1", "title": "a", "depends_on": ["S2"]},
                                                 {"id": "S2", "title": "b", "depends_on": ["S1"]}]))
        self.assertTrue(any(e.startswith("dependency cycle") for e in errs))
        many = outline_entries([{"id": f"S{i}", "title": "x"} for i in range(1, MAX_STORIES + 2)])
        self.assertTrue(any(f"at most {MAX_STORIES}" in e for e in validate_outline(many)))

    def test_added_to_kept_stories(self):
        kept = [story("S1", status="done"), story("S2", status="replaced")]
        new = outline_entries([{"id": "S3", "title": "c", "depends_on": ["S1"]}])
        self.assertEqual(validate_outline(new, kept), [])
        errs = validate_outline(outline_entries([{"id": "S2", "title": "again"}]), kept)
        self.assertIn("S2: already a story of this phase — new ids start at S3", errs)
        errs = validate_outline(outline_entries([{"id": "S3", "title": "c", "depends_on": ["S2"]}]),
                                kept)
        self.assertTrue(any("depends on S2, which is not in this phase" in e for e in errs))

    def test_one_story_checks_as_the_set_does(self):
        bad = story("S1", deps=["S1", "S7", "P1.S1"], acceptance=[], description="",
                    files=["../out.py"])
        alone = bad.problems({"S1"}, {}, Path("/tmp/x"))
        in_set = StorySet(phase=1, stories=[bad]).validate({}, Path("/tmp/x"))
        self.assertEqual(alone + ["dependency cycle: S1 → S1"], in_set)   # the cycle is the set's
        self.assertEqual(len(alone), 6)


class Order(unittest.TestCase):
    def test_dependencies_first_then_by_number(self):
        sset = StorySet(phase=1, stories=[story("S3"), story("S1", ["S2"]), story("S2")])
        order = []
        while (s := sset.next_ready()) is not None:
            order.append(s.id)
            s.status = "done"
        self.assertEqual(order, ["S2", "S1", "S3"])
        self.assertTrue(sset.done)

    def test_a_story_in_progress_resumes_first(self):
        sset = StorySet(phase=1, stories=[story("S1"), story("S2", status="in_progress")])
        self.assertEqual(sset.next_ready().id, "S2")

    def test_earlier_phase_dependencies_do_not_wait(self):
        sset = StorySet(phase=2, stories=[story("S1", ["P1.S4"])])
        self.assertEqual(sset.next_ready().id, "S1")

    def test_blocked_behind_a_failure(self):
        sset = StorySet(phase=1, stories=[story("S1", status="failed"), story("S2", ["S1"]),
                                         story("S3", ["S2"]), story("S4")])
        self.assertEqual(sset.next_ready().id, "S4")
        self.assertEqual([s.id for s in sset.blocked()], ["S2", "S3"])

    def test_progress_ignores_replaced(self):
        sset = StorySet(phase=1, stories=[story("S1", status="done"),
                                         story("S2", status="replaced"), story("S3")])
        self.assertEqual(sset.progress(), "1/2")


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_folder_per_plan(self):
        now = datetime(2026, 10, 28, 15, 4)
        a = new_stories_dir(self.wd, "Tiny MUD webserver (rooms + movement)", now)
        b = new_stories_dir(self.wd, "Tiny MUD webserver (rooms + movement)", now)
        self.assertEqual(a.name, "tiny-mud-webserver-rooms-movement-20261028-1504")
        self.assertEqual(b.name, a.name + "-2")
        self.assertEqual(a.parent, self.wd.resolve() / ".momo" / "stories")
        self.assertEqual(slug("!!!"), "plan")

    def test_write_and_read(self):
        d = new_stories_dir(self.wd, "x")
        sset = StorySet(phase=3, title="Server", stories=[story("S1")])
        self.assertEqual(write_story_file(d, sset), "")
        self.assertEqual((d / "phase-3.md").exists(), True)
        self.assertEqual(read_story_file(d, 3).get("S1").title, "Story S1")
        self.assertIsNone(read_story_file(d, 4))


class PlanPhases(unittest.TestCase):
    def plan(self):
        p = Plan(title="Tiny MUD", goal="A MUD.", kind="stories",
                 stories_dir=".momo/stories/tiny-mud-20261028-1504",
                 steps=[Step(title="WebSocket protocol", details="mud/ws.py",
                             outcome="mud/ws.py speaks RFC 6455", report="Done: ws.py"),
                        Step(title="World model")])
        p.story_sets[1] = StorySet(phase=1, title="WebSocket protocol",
                                   stories=[story("S1", status="done")])
        return p

    def test_dict_round_trip(self):
        p = self.plan()
        back = Plan.from_dict(p.to_dict())
        self.assertEqual(back, p)
        self.assertTrue(back.stories)

    def test_the_request_and_the_report_round_trip(self):
        p = self.plan()
        p.request = "Build a MUD.\n- [ ] players enter a name"   # a checkbox is not a step
        p.story_sets[1].report = "## What exists now\nmud/ws.py"
        self.assertEqual(Plan.from_dict(p.to_dict()), p)
        back = Plan.from_markdown(p.to_markdown())
        self.assertEqual(back.request, p.request)
        self.assertEqual(len(back.steps), 2)
        sset = StorySet.from_markdown(p.story_sets[1].to_markdown(), 1)
        self.assertEqual(sset.report, "## What exists now\nmud/ws.py")
        self.assertEqual([x.id for x in sset.stories], ["S1"])
        self.assertNotIn("What exists", sset.stories[0].description)

    def test_markdown_keeps_the_phase_fields(self):
        p = self.plan()
        md = p.to_markdown()
        self.assertIn("Type: stories", md)
        self.assertIn("Stories: 1/1 done (phase-1.md)", md)
        back = Plan.from_markdown(md)
        self.assertEqual((back.kind, back.stories_dir), ("stories", p.stories_dir))
        self.assertEqual(back.steps[0].outcome, "mud/ws.py speaks RFC 6455")
        self.assertNotIn("Stories:", back.steps[0].details)

    def test_earlier_stories(self):
        p = self.plan()
        p.story_sets[2] = StorySet(phase=2, stories=[story("S1")])
        self.assertEqual(list(p.earlier_stories(2)), ["P1.S1"])

    def test_normal_and_tdd_plans_are_unchanged(self):
        p = Plan(title="t", steps=[Step(title="a", details="b")])
        self.assertNotIn("outcome", p.to_dict()["steps"][0])
        self.assertNotIn("story_sets", p.to_dict())
        self.assertEqual(Plan.from_markdown(p.to_markdown()).kind, "normal")


    def test_a_request_with_headings_survives_the_plan_file(self):
        # pasted specs have headings, and a checkbox in one became a phase
        p = Plan(title="t", goal="g\n## Notes\nn", kind="stories",
                 request="Build a MUD.\n\n## Commands\nlook, go, say\n\n## Rules\n- [ ] keep it small",
                 steps=[Step(title="a", details="d")])
        back = Plan.from_markdown(p.to_markdown())
        self.assertEqual((back.request, back.goal), (p.request, p.goal))
        self.assertEqual([s.title for s in back.steps], ["a"])


class CodeAndLists(unittest.TestCase):
    """What counts as code in an expectation, and what is a list in a request."""

    def problems(self, given, libraries=()):
        return criterion_problems(Criterion("x", examples=[{"given": given, "expect": "exit `0`"}]),
                                  libraries)

    def test_command_lines_and_library_calls_are_not_code(self):
        from harness.stories import library_modules
        libs = library_modules(["Library: `from pager import paginate`"])
        self.assertIn("pager", libs)
        for given in ("`tool import data.csv` is run", "python3 build.py (no flags)",
                      "paginate(range(11), 5)"):
            self.assertEqual(self.problems(given), [], given)
        self.assertEqual(self.problems("`pager.paginate(range(11), 5)`", libs), [])

    def test_code_is_still_refused(self):
        for given in ("`world.add_player('Ann')`", "Room.__init__ runs", "import calc",
                      "`from calc import add`", "`x = 1; import os`", "assert add(1, 1) == 2"):
            self.assertTrue(any("contains code" in e for e in self.problems(given)), given)
        self.assertTrue(self.problems("`pager.paginate(range(11), 5)`"))   # no Library interface

    def test_only_a_list_of_what_the_product_offers_is_split(self):
        from harness.plan_runner import _request_list_items
        self.assertEqual(_request_list_items("Constraints: stdlib only, no frameworks, Python 3.12."), [])
        self.assertEqual(_request_list_items("A MUD. Commands: look, go/n/s, say, quit."),
                         ["look", "go", "say", "quit"])

    def test_a_requirement_with_a_bracketed_list_is_one_thing(self):
        from harness.plan_runner import _top_level_commas
        self.assertEqual(_top_level_commas("GET /state returns {room, exits, items, players}"), 0)
        self.assertEqual(_top_level_commas("`look`, `go`, `say`, `quit` work"), 3)
        self.assertEqual(_top_level_commas("Commands: look, go, say, who"), 3)

    def test_module_interfaces_count_as_absent(self):
        import tempfile
        from harness.story_runner import _absent_interfaces
        with tempfile.TemporaryDirectory() as d:
            wd = Path(d)
            ui = ["Service: `python3 -m src.api`, HTTP on localhost:8080"]
            self.assertEqual(_absent_interfaces(wd, ui), ["src.api"])
            (wd / "src").mkdir()
            (wd / "src" / "api.py").write_text("")
            self.assertEqual(_absent_interfaces(wd, ui), [])



if __name__ == "__main__":
    unittest.main()
