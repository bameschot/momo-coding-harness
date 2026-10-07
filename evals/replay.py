#!/usr/bin/env python3
"""Replay A/B: one tool result, two wordings, the same conversation.

A whole-suite A/B at 3 runs per cell cannot see a change to one tool result:
the runs differ long before they reach it.  This fixes everything up to that
result instead.  It takes a real session from an eval run, cuts it right after
tool result k, rebuilds the project as it was at that point (a fresh copy of
the task's workdir, with the session's own write calls up to k replayed
through tools.dispatch), and lets the model continue from there — once with
the result as it was recorded (A) and once with what the current code says
for the same call (B).  Each finished run is scored by the task's hidden check
(evals/edit/checks.py), plus the calls and time it took.

    python evals/replay.py                      # every experiment, 10 samples per variant
    python evals/replay.py --exp R1 --n 6 --json r1.json

Needs a running model server (default http://localhost:8080).  Sampling is
not pinned, which is the point: N samples per variant from one fixed prefix.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
if not os.environ.get("MOMO_EVAL_KEEP_HOME"):
    os.environ["HOME"] = tempfile.mkdtemp(prefix="momo_replay_home_")

from evals.edit.checks import run_check  # noqa: E402
from evals.tasks import EDIT_TASKS  # noqa: E402
from harness.editing import EDIT_FEATURES  # noqa: E402
from harness.harness import Harness  # noqa: E402
from harness.tools import dispatch  # noqa: E402

_REPLAYS = REPO / "evals" / "edit" / "replays"
_WRITES = {"write_file", "edit_file", "append_to_file", "replace_symbol", "move_file",
           "delete_file"}
_EXIT = re.compile(r"exit code (\d+)")


def _first_edit(pairs):
    return next(i for i, (_, name, _a, _r) in enumerate(pairs) if name == "edit_file")


def _first_warning(pairs):
    return next(i for i, (_, name, _a, r) in enumerate(pairs)
                if name in ("edit_file", "replace_symbol") and "WARNING" in r)


def _first_noop(pairs):
    return next(i for i, (_, name, a, _r) in enumerate(pairs)
                if name == "edit_file" and a.get("old_string") == a.get("new_string"))


# id: (session file, task id, which tool result to vary, what B adds)
EXPERIMENTS = {
    "R1": ("nested-branch-dropped-else.json", "nested-branch", _first_edit,
           "the edit result lists the lines it removed (the dropped else-branch)"),
    "R2": ("rename-index-off.json", "rename-across", _first_warning,
           "index off: the leftover-use warning covers the whole project, not just the file"),
    "R3": ("signature-noop-edit.json", "signature-change", _first_noop,
           "an edit with new_string == old_string says 'No change' and that the file "
           "still does not parse, instead of 'OK — lines removed'"),
}


def _pairs(messages):
    """[(index of the tool message, name, args, recorded result)]."""
    out, pending = [], []
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            pending = [(c["function"]["name"], c["function"].get("arguments") or {})
                       for c in m.get("tool_calls") or []]
        elif m.get("role") == "tool" and pending:
            name, args = pending.pop(0)
            out.append((i, name, args, str(m.get("content") or "")))
    return out


def _rebuild(task, pairs, cut: int) -> tuple[tempfile.TemporaryDirectory, Path, str]:
    """A copy of the task's project as it was right after call `cut`, and what
    the current code returns for that call."""
    tmp = tempfile.TemporaryDirectory(prefix="momo_replay_wd_")
    wd = Path(tmp.name) / "project"
    shutil.copytree(REPO / task.workdir, wd, ignore=shutil.ignore_patterns("__pycache__"))
    for _i, name, args, recorded in pairs[:cut]:
        if name in _WRITES and not recorded.startswith("ERROR"):
            dispatch(name, dict(args), wd, edit_features=frozenset())
    _i, name, args, _r = pairs[cut]
    return tmp, wd, dispatch(name, dict(args), wd, edit_features=EDIT_FEATURES)


def run_variant(exp: str, variant: str, *, host: str, model: str, max_iter: int) -> dict:
    session, task_id, pick, _what = EXPERIMENTS[exp]
    task = next(t for t in EDIT_TASKS if t.id == task_id)
    recorded = json.loads((_REPLAYS / session).read_text())["messages"]
    pairs = _pairs(recorded)
    cut = pick(pairs)
    tmp, wd, new_result = _rebuild(task, pairs, cut)
    h = None
    try:
        h = Harness(host=host, model=model, workdir=wd, provider="llamacpp")
        h.mode = "coding"
        h.run_mode = "new"
        h.stream = False
        h.think_history = "turn"
        h.edit_features = EDIT_FEATURES
        h._ask_user = lambda q: "No thanks, that's all I needed."
        msg_i = pairs[cut][0]
        prefix = copy.deepcopy(recorded[1:msg_i + 1])
        prefix[-1]["content"] = pairs[cut][3] if variant == "A" else new_result
        h.messages = [{"role": "system", "content": h._build_system_prompt()}] + prefix
        h._turn_user = next(m for m in h.messages if m.get("role") == "user")
        start = len(h.messages)
        t0 = time.time()
        err = ""
        try:
            outcome = h._run_loop(h._current_tools(), max_iter)
        except Exception as e:                  # a transport failure is a result
            outcome, err = "error", f"{type(e).__name__}: {e}"
        secs = time.time() - t0
        passed, detail = run_check(task.check, wd)
        after = _pairs(h.messages[start:])      # the turns after the cut
        names = [n for _i, n, _a, _r in after]
        failing = sum(1 for _i, n, _a, r in after if n == "run_command"
                      and (m := _EXIT.search(r)) and m.group(1) != "0")
        return {"exp": exp, "variant": variant, "passed": passed, "detail": detail,
                "calls": len(names), "edits": sum(n in _WRITES for n in names),
                "failing_commands": failing, "secs": round(secs, 1), "outcome": outcome,
                "err": err, "tools": names}
    finally:
        if h is not None:
            h.logger.close()
        tmp.cleanup()


def summarise(rows) -> str:
    out = [f"{'exp':4s} {'var':3s} {'n':>3s} {'pass':>6s} {'calls':>14s} {'edits':>6s} "
           f"{'failing cmds':>12s} {'secs':>6s}", "-" * 60]
    for exp in dict.fromkeys(r["exp"] for r in rows):
        for v in ("A", "B"):
            rs = [r for r in rows if r["exp"] == exp and r["variant"] == v]
            if not rs:
                continue
            c = [r["calls"] for r in rs]
            out.append(f"{exp:4s} {v:3s} {len(rs):3d} {sum(r['passed'] for r in rs):3d}/{len(rs):<2d} "
                       f"{statistics.mean(c):6.1f} ({min(c)}-{max(c)}) "
                       f"{statistics.mean(r['edits'] for r in rs):6.1f} "
                       f"{sum(r['failing_commands'] for r in rs):12d} "
                       f"{statistics.mean(r['secs'] for r in rs):6.1f}")
        out.append(f"     B = {EXPERIMENTS[exp][3]}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="http://localhost:8080")
    ap.add_argument("--model", default="qwen3.5:9b")
    ap.add_argument("--exp", nargs="*", choices=sorted(EXPERIMENTS), help="default: all")
    ap.add_argument("--n", type=int, default=10, help="samples per variant (default: 10)")
    ap.add_argument("--max-iter", type=int, default=40, help="model turns per sample")
    ap.add_argument("--json", metavar="PATH", help="write the per-sample records here")
    args = ap.parse_args()
    rows = []
    for exp in args.exp or sorted(EXPERIMENTS):
        for i in range(args.n):
            for v in ("A", "B"):                # interleaved, so drift hits both alike
                r = run_variant(exp, v, host=args.host, model=args.model, max_iter=args.max_iter)
                rows.append(r)
                print(f"[{len(rows):3d}] {exp} {v} #{i}: {'PASS' if r['passed'] else 'fail'} "
                      f"{r['calls']:2d} calls {r['secs']:6.1f}s"
                      + (f"  {r['detail'][:70]}" if not r["passed"] else "")
                      + (f"  ERR {r['err'][:60]}" if r["err"] else ""), flush=True)
                if args.json:
                    Path(args.json).write_text(json.dumps(rows, indent=1))
    print()
    print(summarise(rows))


if __name__ == "__main__":
    main()
