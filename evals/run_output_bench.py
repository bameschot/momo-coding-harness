#!/usr/bin/env python3
"""What /run-mode new saves, and what it loses, on realistic command output.

No model involved: each case is a generated command output (a pytest run with
failures, a make/gcc build, git log, pip install, find, a short run) with the
facts ("needles") a model needs from it, and the call that would get them in
one go.  The output is replayed through the REAL run_command (as `cat` of a
file), so what is measured is exactly what the model would be sent:

  classic   /run-mode classic: the whole output
  default   /run-mode new, no tail/grep: whole if short, else head + tail
  ideal     /run-mode new with the case's best tail=/grep= call
  +follow   default, then that call through command_output when the default
            view missed a needle (the two calls a model would actually make)

A needle counts as found when its text appears in the result.  The fixed cost
is reported too: the new schema is longer, and that is paid on every request.

    python evals/run_output_bench.py
    python evals/run_output_bench.py --limit 3000 --show pytest
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from harness import run_store, tools  # noqa: E402


@dataclass
class Case:
    id: str
    text: str
    needles: tuple[str, ...]
    ideal: dict                  # tail= / grep= / context= for the one best call
    exit_code: int = 0
    note: str = ""
    tags: list[str] = field(default_factory=list)


def _pytest(rng: random.Random) -> Case:
    names = [f"tests/test_{m}.py::test_{m}_{i}" for m in ("cart", "pricing", "orders", "auth")
             for i in range(100)]
    failing = {names[37], names[161], names[288]}
    lines = ["============================= test session starts ==============================",
             "platform darwin -- Python 3.14.0, pytest-8.4.1, pluggy-1.6.0",
             "rootdir: /home/me/shop", "collected 400 items", ""]
    lines += [f"{n} {'FAILED' if n in failing else 'PASSED'} [{(i + 1) * 100 // 400:3d}%]"
              for i, n in enumerate(names)]
    lines += ["", "=================================== FAILURES ==================================="]
    for n in sorted(failing):
        fn = n.split("::")[1]
        lines += [f"_________________________________ {fn} _________________________________", ""]
        lines += [f"    def {fn}():"] + [f"        step_{k}(cart, {rng.randint(1, 99)})"
                                         for k in range(25)]
        lines += [f">       assert total == {rng.randint(100, 999)}",
                  f"E       AssertionError: assert {rng.randint(100, 999)} == {rng.randint(100, 999)}",
                  "", f"{n.split('::')[0]}:{rng.randint(10, 400)}: AssertionError"]
    lines += ["=========================== short test summary info ============================"]
    lines += [f"FAILED {n} - AssertionError" for n in sorted(failing)]
    lines += ["======================== 3 failed, 397 passed in 12.31s ========================"]
    return Case("pytest", "\n".join(lines),
                tuple(f"FAILED {n}" for n in sorted(failing)) + ("3 failed, 397 passed",),
                {"tail": 6}, exit_code=1,
                note="failures + summary at the end: tail is ideal")


def _make(rng: random.Random) -> Case:
    lines = []
    for i in range(400):
        lines.append(f"cc -O2 -Wall -Iinclude -c src/mod{i:03d}.c -o build/mod{i:03d}.o")
        if i == 173:
            lines.append("src/mod173.c:41:9: warning: unused variable 'scratch' [-Wunused-variable]")
    lines += ["src/parser.c:88:12: error: 'tok' undeclared (first use in this function)",
              "   88 |     return tok->kind;", "      |            ^~~",
              "make: *** [Makefile:12: build/parser.o] Error 1"]
    return Case("make", "\n".join(lines),
                ("warning: unused variable 'scratch'", "error: 'tok' undeclared"),
                {"grep": "error|warning"}, exit_code=2,
                note="a warning in the middle: the default view cannot see it")


def _git_log(rng: random.Random) -> Case:
    lines = []
    for i in range(250):
        msg = "fix: race in EventBus.subscribe" if i == 131 else \
            f"{rng.choice(['feat', 'fix', 'chore', 'docs'])}: change {i}"
        lines += [f"commit {rng.getrandbits(160):040x}", "Author: Dev <dev@example.com>",
                  f"Date:   Mon Sep {1 + i % 28} 10:{i % 60:02d}:00 2026 +0200", "",
                  f"    {msg}", ""]
        lines += [f" harness/f{rng.randint(1, 40)}.py | {rng.randint(1, 80)} +++--"
                  for _ in range(rng.randint(1, 4))]
        lines.append(f" {rng.randint(1, 4)} files changed")
        lines.append("")
    return Case("git-log", "\n".join(lines), ("fix: race in EventBus.subscribe",),
                {"grep": "EventBus", "context": 4},
                note="one commit deep in history: only grep finds it")


def _pip(rng: random.Random) -> Case:
    lines = []
    for pkg in ("requests", "urllib3", "idna", "certifi", "charset-normalizer", "rich",
                "pygments", "markdown-it-py", "mdurl") * 12:
        lines += [f"Collecting {pkg}", f"  Downloading {pkg}-1.{rng.randint(0, 9)}.whl "
                  f"({rng.randint(40, 900)} kB)",
                  "     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0/100.0 kB 1.2 MB/s eta 0:00:00",
                  f"  Using cached {pkg} from local index", "  Requirement already satisfied"]
    lines.append("Successfully installed certifi-2026.9.1 idna-3.10 requests-2.33.0 rich-14.1.0")
    return Case("pip", "\n".join(lines), ("Successfully installed",), {"tail": 3},
                note="only the last line matters")


def _find(rng: random.Random) -> Case:
    lines = [f"./src/{a}/{b}/file{c}.py" for a in ("api", "core", "ui", "db")
             for b in ("x", "y", "z", "w") for c in range(200)]
    lines.insert(1777, "./src/core/deep/config/settings.yaml")
    return Case("find", "\n".join(lines), ("settings.yaml",), {"grep": r"\.yaml$"},
                note="one path among 3,200")


def _short(rng: random.Random) -> Case:
    return Case("short", "ruff: All checks passed!\n12 files left unchanged",
                ("All checks passed",), {}, note="small: the default must return it whole")


CASES = (_pytest, _make, _git_log, _pip, _find, _short)


def measure(case: Case, limit: int, wd: Path, store: run_store.RunStore) -> dict:
    src = wd / f"{case.id}.out"
    src.write_text(case.text + "\n")
    command = f"cat {src.name}; exit {case.exit_code}"

    def run(mode, **args):
        return tools.dispatch("run_command", {"command": command, **args}, wd,
                              run_mode=mode, run_store=store, run_output_limit=limit)

    def found(out):
        return [n for n in case.needles if n in out]

    classic = run("classic")
    default = run("new")
    ideal = run("new", **case.ideal) if case.ideal else default
    follow = default
    if len(found(default)) < len(case.needles) and case.ideal:
        path = default.split("[output saved: ", 1)[1].split(" — ", 1)[0]
        follow = default + tools.dispatch("command_output", {"path": path, **case.ideal}, wd,
                                          run_mode="new", run_store=store,
                                          run_output_limit=limit)
    return {"case": case.id, "note": case.note, "n": len(case.needles),
            "out": {"classic": classic, "default": default, "ideal": ideal, "+follow": follow},
            "chars": {k: len(v) for k, v in (("classic", classic), ("default", default),
                                              ("ideal", ideal), ("+follow", follow))},
            "found": {k: len(found(v)) for k, v in (("classic", classic), ("default", default),
                                                    ("ideal", ideal), ("+follow", follow))}}


def prompt_cost() -> dict[str, int]:
    """Tokens (the harness's len/4 estimate) of the coding tool reference and
    schemas under each run mode — paid on every request."""
    out = {}
    for mode in tools.RUN_MODES:
        tl = tools.with_run_mode(tools.ALL_TOOLS, mode)
        out[mode] = (len(tools.render_tool_reference(tl)) + len(json.dumps(tl))) // 4
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=run_store.DEFAULT_LIMIT,
                    help="run_output_limit (default %(default)s)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--show", metavar="CASE", help="print what each strategy returns for CASE")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    cases = [make(rng) for make in CASES]
    kinds = ("classic", "default", "ideal", "+follow")
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp) / "wd"
        wd.mkdir()
        store = run_store.RunStore(Path(tmp) / "runs")
        rows = [measure(c, args.limit, wd, store) for c in cases]

    print(f"run_output_limit = {args.limit} chars; tokens ≈ chars / 4\n")
    head = f"{'case':9s}" + "".join(f"{k:>18s}" for k in kinds)
    print(head)
    print("-" * len(head))
    tot = {k: 0 for k in kinds}
    got = {k: 0 for k in kinds}
    needles = 0
    for r in rows:
        cells = "".join(f"{r['chars'][k]:>10,} {r['found'][k]}/{r['n']:<5}" for k in kinds)
        print(f"{r['case']:9s}{cells}   {r['note']}")
        for k in kinds:
            tot[k] += r["chars"][k]
            got[k] += r["found"][k]
        needles += r["n"]
    print("-" * len(head))
    print(f"{'total':9s}" + "".join(f"{tot[k]:>10,} {got[k]}/{needles:<5}" for k in kinds))
    print(f"{'vs classic':9s}" + "".join(f"{tot[k] / tot['classic']:>16.1%}  " for k in kinds))

    cost = prompt_cost()
    delta = cost["new"] - cost["classic"]
    saved = (tot["classic"] - tot["+follow"]) // 4
    print(f"\nfixed prompt cost (coding tools): classic ≈{cost['classic']:,} tokens, "
          f"new ≈{cost['new']:,} (+{delta:,} held in every request's context)")
    print(f"these {len(rows)} commands: ≈{saved:,} tokens fewer with default + one follow-up, "
          f"≈{saved // len(rows):,} per command")

    if args.show:
        r = next((r for r in rows if r["case"] == args.show), None)
        if r is None:
            ap.error(f"unknown case {args.show!r}; have: {', '.join(x['case'] for x in rows)}")
        for k in kinds[1:]:
            print(f"\n──── {k} ({r['chars'][k]:,} chars) ────\n{r['out'][k]}")


if __name__ == "__main__":
    main()
