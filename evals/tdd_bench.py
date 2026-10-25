#!/usr/bin/env python3
"""End-to-end bench for plan mode's TDD plans, against a live model.

One feature request (evals/tdd/project: add remove() and low_stock() to a small
Inventory class), run through plan mode from investigation to the finished
plan. The approval is answered "y" and every other question "Use your
recommendation.". Afterwards a hidden checker the model never sees
(evals/tdd/check.py, 8 cases) grades the code.

    python evals/tdd_bench.py --runs 3                 # TDD plans
    python evals/tdd_bench.py --runs 3 --kind both     # TDD and normal plans, same task
    python evals/tdd_bench.py --host http://127.0.0.1:9931 --json out.json --keep runs/

Per run it records: time, model calls, prompt/output tokens, the plan's stages,
every gate (stage, passed, why), the model's own suite run right before
complete_step, the edits after the test-writing stage, a dropped closing
"run the tests" step, the hidden result, and the TDD tags left in the project.
Sampling is unpinned, so look at the spread, not one run (see README.md).
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
HERE = Path(__file__).resolve().parent / "tdd"

# The harness writes prefs and session files under HOME.
if not os.environ.get("MOMO_EVAL_KEEP_HOME"):
    os.environ["HOME"] = tempfile.mkdtemp(prefix="momo_eval_home_")

from harness import tdd as tdd_mod  # noqa: E402
from harness.commands import handle  # noqa: E402
from harness.events import ChatEvent, ErrorEvent, ToolCallEvent, ToolResultEvent  # noqa: E402
from harness.harness import Harness  # noqa: E402
from harness.prompts import CAVECAT_LEVELS  # noqa: E402

PROMPT = ("Add two features to the Inventory class in shop/inventory.py:\n"
          "1. remove(name, qty): decrease the stock of name by qty. Raise ValueError when qty "
          "is not positive, or when there is not enough stock (an unknown item has 0); a failed "
          "remove changes nothing. When an item's count reaches 0 it is dropped entirely, so it "
          "no longer appears in names().\n"
          "2. low_stock(threshold): return the sorted names of the items whose count is below "
          "threshold (strictly less).")

# --slow-suite: a planted test that only takes time, so the cost of running a
# slow suite shows up in the numbers (the model is not told).
_SLOW_TEST = ("import time\nimport unittest\n\n\nclass Slow(unittest.TestCase):\n"
              "    def test_slow(self):\n        time.sleep({secs})\n")

_WRITES = ("edit_file", "edit_files", "write_file", "append_to_file", "replace_symbol")
# The first line of a tdd_gate result says why it passed or failed.
_GATE_WHY = (("Tests pass", "pass"), ("Tests fail", "red suite"), ("No pending tests", "missing tag"),
             ("These tests have no body", "hollow"), ("These checks were deleted", "deleted check"),
             ("These tests belong to finished", "finished test changed"),
             ("These tests are still marked pending", "marker left"))


def _why(report: str) -> str:
    return next((w for prefix, w in _GATE_WHY if report.startswith(prefix)), report[:40])


_calls: list[dict] = []
_patched: set[type] = set()


def _count_calls(client):
    """Wrap the client class's chat() to time each model call and count tokens."""
    cls = type(client)
    if cls in _patched:
        return
    orig = cls.chat

    def chat(self, *a, **k):
        t0 = time.time()
        r = orig(self, *a, **k)
        _calls.append({"secs": time.time() - t0, "prompt": r.prompt_tokens or 0,
                       "eval": r.eval_tokens or 0})
        return r

    cls.chat = chat
    _patched.add(cls)


def run_once(kind: str, args, keep: Path | None) -> dict:
    tmp = tempfile.TemporaryDirectory(prefix="momo_tdd_")
    proj = Path(tmp.name) / "project"
    shutil.copytree(HERE / "project", proj, ignore=shutil.ignore_patterns("__pycache__"))
    if args.slow_suite:
        (proj / "tests" / "test_slow.py").write_text(_SLOW_TEST.format(secs=args.slow_suite))
    h = Harness(host=args.host, model=args.model or "", workdir=proj, provider=args.provider)
    if not args.model:
        h.check_backend()      # adopt the model the server serves, as momo does at startup
    h.stream = False
    h.cavecat = args.cavecat
    h._ask_user = lambda q: "y" if q.startswith("Execute this plan?") else "Use your recommendation."
    _count_calls(h.client)
    handle(f"/plan {kind}", h)

    sub = h.event_queue.subscribe(replay=False)
    stage = {"name": "investigate", "t0": time.time()}
    stage_secs: dict[str, float] = {}
    tools: dict[str, Counter] = {}
    gates: list[dict] = []
    dropped, errors, log = [], [], []
    dropped_step_cmd: list[str] = []
    test_runs = {"secs": 0.0, "started": None}   # the harness's own test runs
    done = threading.Event()

    def pump():
        while not (done.is_set() and sub._q.empty()):
            try:
                ev = sub.get(timeout=0.5)
            except queue.Empty:
                continue
            if isinstance(ev, ChatEvent):
                log.append(f"\n[{ev.role}] {ev.text}")
                if ev.role == "system" and ev.text.startswith("The step test command failed"):
                    dropped_step_cmd.append(ev.text)
                if ev.role == "system" and ev.text.startswith("▶ Plan step"):
                    now = time.time()
                    stage_secs[stage["name"]] = round(now - stage["t0"], 1)
                    stage.update(name=ev.text.split(":", 1)[0].replace("▶ Plan step ", "step "), t0=now)
                elif ev.role == "system" and ev.text.startswith("Dropped steps the TDD plan does itself"):
                    dropped.append(ev.text)
            elif isinstance(ev, ToolCallEvent):
                tools.setdefault(stage["name"], Counter())[ev.name] += 1
                if ev.name in ("tdd_gate", "tdd_switch_on"):
                    test_runs["started"] = time.time()
                log.append(f"→ {ev.name} {json.dumps(ev.args)[:400]}")
            elif isinstance(ev, ToolResultEvent):
                if ev.name in ("tdd_gate", "tdd_switch_on") and test_runs["started"]:
                    test_runs["secs"] += time.time() - test_runs["started"]
                    test_runs["started"] = None
                if ev.name == "tdd_gate":
                    gates.append({"stage": stage["name"], "why": _why(ev.result or ""),
                                  "reused": (ev.result or "").startswith("Tests pass: your run of")})
                log.append(f"← {ev.name}: {(ev.result or '')[:800]}")
            elif isinstance(ev, ErrorEvent):
                errors.append(str(ev))

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    watchdog = threading.Timer(args.limit_min * 60, h.cancel)
    watchdog.start()
    first_call = len(_calls)
    t0 = time.time()
    try:
        h.send(PROMPT)
    except Exception as e:  # noqa: BLE001 — a transport failure is a result
        errors.append(f"{type(e).__name__}: {e}")
    total = time.time() - t0
    watchdog.cancel()
    done.set()
    t.join(timeout=10)
    sub.close()
    stage_secs[stage["name"]] = round(time.time() - stage["t0"], 1)

    hidden = {"passed": 0, "total": 8, "failures": {"checker": "did not run"}}
    try:
        out = subprocess.run([sys.executable, "-I", str(HERE / "check.py"), str(proj)],
                             capture_output=True, text=True, timeout=60).stdout
        hidden = json.loads(out.strip().splitlines()[-1])
    except (subprocess.TimeoutExpired, ValueError, IndexError) as e:
        hidden["failures"] = {"checker": repr(e)}
    suite = subprocess.run(f"{sys.executable} -m unittest discover -s tests -t .", cwd=proj,
                           shell=True, capture_output=True, text=True, timeout=300)
    fns = [tc["function"] for m in h.messages for tc in (m.get("tool_calls") or [])]
    seq = [f["name"] for f in fns]
    plan_args = next((f["arguments"] for f in reversed(fns) if f["name"] == "create_plan"), {})
    step_cmd = str(plan_args.get("step_test_command") or "") if isinstance(plan_args, dict) else ""
    calls = _calls[first_call:]
    rec = {
        "kind": kind, "model": h.client.model, "cavecat": args.cavecat, "secs": round(total, 1),
        "plan_done": h.plan is None, "calls": len(calls),
        "prompt_tokens": sum(c["prompt"] for c in calls),
        "eval_tokens": sum(c["eval"] for c in calls),
        "stages": len(stage_secs) - 1, "stage_secs": stage_secs,
        "tools": {k: dict(v) for k, v in tools.items()},
        "gates": gates,
        "refusals": dict(Counter(g["why"] for g in gates if g["why"] != "pass")),
        "run_before_complete": sum(1 for a, b in zip(seq, seq[1:])
                                   if b == "complete_step" and a == "run_command"),
        "edits_after_tests": sum(n for k, c in tools.items()
                                 if k != "investigate" and not k.startswith("step 1/")
                                 for name, n in c.items() if name in _WRITES),
        "dropped_verify_step": bool(dropped),
        "test_secs": round(test_runs["secs"], 1),
        "gates_reused": sum(g["reused"] for g in gates),
        "step_test_command": step_cmd, "step_command_dropped": bool(dropped_step_cmd),
        "hidden": hidden, "suite_ok": suite.returncode == 0,
        "tags_left": sorted(tdd_mod.find_tags(proj)), "errors": errors,
    }
    if keep is not None:
        dest = keep / f"{kind}-{time.strftime('%H%M%S')}"
        shutil.copytree(proj, dest / "project")
        (dest / "events.txt").write_text("\n".join(log))
        (dest / "messages.json").write_text(json.dumps(h.messages, indent=1, ensure_ascii=False))
        (dest / "result.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False))
    h.logger.close()
    tmp.cleanup()
    return rec


def _spread(values) -> str:
    values = list(values)
    if not values:
        return "-"
    return f"{statistics.mean(values):.0f} ({min(values):.0f}-{max(values):.0f})"


def summary(records: list[dict]) -> str:
    out = []
    for kind in sorted({r["kind"] for r in records}):
        rs = [r for r in records if r["kind"] == kind]
        refusals = Counter()
        for r in rs:
            refusals.update(r["refusals"])
        levels = "/".join(sorted({r.get("cavecat", "off") for r in rs}))
        out += [f"\n{kind}: {len(rs)} runs (cavecat {levels})",
                f"  hidden 8/8        {sum(r['hidden']['passed'] == r['hidden']['total'] for r in rs)}/{len(rs)}"
                f"   plans finished {sum(r['plan_done'] for r in rs)}/{len(rs)}"
                f"   suite green {sum(r['suite_ok'] for r in rs)}/{len(rs)}",
                f"  seconds           {_spread(r['secs'] for r in rs)}",
                f"  model calls       {_spread(r['calls'] for r in rs)}",
                f"  output tokens     {_spread(r['eval_tokens'] for r in rs)}",
                f"  prompt tokens     {_spread(r['prompt_tokens'] for r in rs)}",
                f"  stages            {_spread(r['stages'] for r in rs)}",
                f"  edits after tests {_spread(r['edits_after_tests'] for r in rs)}",
                f"  run before done   {_spread(r['run_before_complete'] for r in rs)}",
                f"  gate refusals     {dict(refusals) or 'none'}",
                f"  verify step drop  {sum(r['dropped_verify_step'] for r in rs)}/{len(rs)}",
                f"  harness test secs {_spread(r.get('test_secs', 0) for r in rs)}",
                f"  gates reused      {sum(r.get('gates_reused', 0) for r in rs)}"
                f" of {sum(len(r.get('gates', [])) for r in rs)}",
                f"  step command      {sum(bool(r.get('step_test_command')) for r in rs)}/{len(rs)} planned,"
                f" {sum(r.get('step_command_dropped', False) for r in rs)} dropped"]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--kind", choices=("tdd", "normal", "both"), default="tdd")
    ap.add_argument("--host", default="http://localhost:8080")
    ap.add_argument("--provider", default="llamacpp", choices=("llamacpp", "ollama"))
    ap.add_argument("--model", default="", help="default: the model the server serves")
    ap.add_argument("--limit-min", type=float, default=45, help="per-run limit; the run is cancelled after it")
    ap.add_argument("--cavecat", choices=CAVECAT_LEVELS, default="off",
                    help="/cavecat for every run: compressed plan steps and step summaries "
                         "(text), and thinking too (full)")
    ap.add_argument("--json", metavar="PATH", help="write the per-run records here")
    ap.add_argument("--keep", metavar="DIR", help="keep each run's project, events and messages here")
    ap.add_argument("--slow-suite", type=float, default=0, metavar="SECS",
                    help="plant a test that sleeps SECS, to measure the cost of a slow suite")
    args = ap.parse_args()
    if "://" not in args.host:
        ap.error(f"--host needs a scheme, e.g. http://{args.host}")
    keep = Path(args.keep) if args.keep else None
    kinds = ("tdd", "normal") if args.kind == "both" else (args.kind,)
    records = []
    for i in range(args.runs):
        for kind in kinds:
            r = run_once(kind, args, keep)
            records.append(r)
            print(f"{kind:6} run {i + 1}: {r['secs']:6.0f}s  calls {r['calls']:3}  stages {r['stages']}  "
                  f"hidden {r['hidden']['passed']}/{r['hidden']['total']}  refusals {r['refusals'] or '-'}"
                  f"{'  ERRORS ' + str(r['errors'][:1]) if r['errors'] else ''}", flush=True)
            if args.json:
                Path(args.json).write_text(json.dumps(records, indent=2, ensure_ascii=False))
    print(summary(records))


if __name__ == "__main__":
    main()
