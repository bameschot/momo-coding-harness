#!/usr/bin/env python3
"""Measure how well a local model uses the harness's code-navigation tools.

Not a unit test: it needs a running model server, takes minutes, and its results
are statistical rather than pass/fail.  It lives outside tests/ so
`python -m unittest discover tests` never picks it up.

    python evals/run_evals.py --runs 3
    python evals/run_evals.py --mode coding --runs 3 --tasks line-to-definition
    python evals/run_evals.py --json baseline.json

The model server samples with its own defaults (llama.cpp typically temperature
0.8 and a random seed) and the harness sends no sampling parameters at all, so
runs are NOT reproducible.  Always use --runs >= 3 and read the spread, not the
mean: the same task and config has ranged from 1 to 25 tool calls.
"""
import argparse
import json
import os
import queue
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# The harness writes ~/.momo-harness/prefs.json and a session file per run, so
# point HOME somewhere disposable before importing it.
if not os.environ.get("MOMO_EVAL_KEEP_HOME"):
    os.environ["HOME"] = tempfile.mkdtemp(prefix="momo_eval_home_")

from evals.tasks import TASKS  # noqa: E402
from harness.harness import (Harness, ChatEvent, DoneEvent, ErrorEvent,  # noqa: E402
                             ToolCallEvent, ToolResultEvent)


def run_once(task, *, host, model, provider, mode, think, timeout):
    """One task, one fresh conversation.  Returns what the model did."""
    h = Harness(host=host, model=model, workdir=REPO, provider=provider)
    h.mode = mode
    h.stream = False          # deltas would just duplicate the final ChatEvent
    h.think = think
    h.messages = [{"role": "system", "content": h._build_system_prompt()}]

    sub = h.event_queue.subscribe(replay=False)
    calls: list[tuple[str, dict]] = []
    results: list[str] = []
    answer, err = "", None

    def pump():
        nonlocal answer, err
        while True:
            try:
                ev = sub.get(timeout=timeout)
            except queue.Empty:
                err = "timeout waiting for events"
                return
            if isinstance(ev, ToolCallEvent):
                calls.append((ev.name, ev.args))
            elif isinstance(ev, ToolResultEvent):
                results.append(ev.result or "")
            elif isinstance(ev, ChatEvent):
                # role="system" is the harness talking to itself (nudges,
                # compaction notices) — only the assistant's reply is the answer.
                if ev.role == "assistant":
                    answer = ev.text
            elif isinstance(ev, ErrorEvent):
                err = getattr(ev, "text", str(ev))
            elif isinstance(ev, DoneEvent):
                return

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    t0 = time.time()
    try:
        h.send(task.prompt)
    except Exception as e:                      # a transport failure is a result
        err = f"{type(e).__name__}: {e}"
    t.join(timeout=timeout + 30)
    sub.close()

    names = [c[0] for c in calls]
    low = answer.lower()
    return {
        "task": task.id, "mode": mode, "think": think,
        "secs": round(time.time() - t0, 1),
        "tools": names,
        "args": [c[1] for c in calls],
        "n_calls": len(names),
        "used_ideal": bool(task.ideal & set(names)),
        "first_tool": names[0] if names else None,
        "over_budget": len(names) > task.max_calls,
        # Two different kinds of failure, worth keeping apart: dispatch rejecting
        # the call outright (a genuinely malformed call — bad arg names, missing
        # required args) versus a tool returning a coaching ERROR the model can
        # act on (wrong-but-understood arguments). The second costs a round trip;
        # the first means the model cannot drive the tool at all.
        # Anchor on the ERROR prefix, never a bare substring: these eval tasks
        # read harness/tools.py, whose source contains dispatch's own error
        # strings, so an unanchored match counts a successful read as a rejection.
        "rejected_calls": sum(1 for r in results
                              if r.startswith("ERROR")
                              and ("does not accept argument" in r
                                   or "Retry the call with all required arguments" in r
                                   or "unknown tool" in r)),
        "coached_calls": sum(1 for r in results if r.startswith("ERROR")),
        # Harness repairs, counted as model errors rather than successes.
        "repairs": sum(1 for r in results if r.startswith("(note: routed")),
        "shell_calls": sum(1 for n in names if n == "run_command"),
        "tool_chars": sum(len(r) for r in results),
        "hits": [m for m in task.must if m.lower() in low],
        "n_must": len(task.must),
        "err": err,
        "answer": answer,
    }


def summarise(rows):
    """Per-task table.  Spread matters as much as the mean here."""
    out = []
    by: dict[tuple[str, str], list] = {}
    for r in rows:
        by.setdefault((r["task"], r["mode"]), []).append(r)
    hdr = (f"{'task':22s} {'mode':6s} {'n':>2s} {'ideal':>6s} {'recall':>7s} "
           f"{'calls (min-max)':>16s} {'secs':>6s} {'toolKB':>7s} {'shell':>5s} {'rej':>4s} {'coach':>6s}")
    out.append(hdr)
    out.append("-" * len(hdr))
    for (task, mode), ds in by.items():
        n = len(ds)
        calls = [d["n_calls"] for d in ds]
        recall = sum(len(d["hits"]) for d in ds) / max(sum(d["n_must"] for d in ds), 1)
        out.append(
            f"{task:22s} {mode:6s} {n:2d} {sum(d['used_ideal'] for d in ds):3d}/{n:<2d} "
            f"{recall * 100:6.0f}% "
            f"{statistics.mean(calls):7.1f} ({min(calls)}-{max(calls)}){'':<3s} "
            f"{statistics.mean(d['secs'] for d in ds):6.1f} "
            f"{statistics.mean(d['tool_chars'] for d in ds) / 1024:7.1f} "
            f"{sum(d['shell_calls'] for d in ds):5d} {sum(d['rejected_calls'] for d in ds):4d} "
            f"{sum(d['coached_calls'] for d in ds):6d}")
    total = len(rows)
    tool_total = sum(r["n_calls"] for r in rows)
    nav = {"code_outline", "find_symbol", "read_symbol", "find_references", "file_dependencies"}
    counts: dict[str, int] = {}
    for r in rows:
        for t in r["tools"]:
            counts[t] = counts.get(t, 0) + 1
    out.append("")
    out.append(f"{total} runs, {tool_total} tool calls")
    if tool_total:
        rf = counts.get("read_file", 0)
        rc = counts.get("run_command", 0)
        nv = sum(counts.get(t, 0) for t in nav)
        out.append(f"  read_file        {rf:4d}  {rf / tool_total * 100:4.0f}%")
        out.append(f"  run_command      {rc:4d}  {rc / tool_total * 100:4.0f}%")
        out.append(f"  code-nav tools   {nv:4d}  {nv / tool_total * 100:4.0f}%")
    out.append(f"  ideal tool used  {sum(r['used_ideal'] for r in rows)}/{total}")
    out.append(f"  over call budget {sum(r['over_budget'] for r in rows)}/{total}")
    out.append(f"  rejected calls   {sum(r['rejected_calls'] for r in rows)}  "
               "(malformed args — should be 0)")
    out.append(f"  coached calls    {sum(r['coached_calls'] for r in rows)}  "
               "(tool explained the mistake; costs a round trip)")
    out.append(f"  harness repairs  {sum(r['repairs'] for r in rows)}")
    errs = [r for r in rows if r["err"]]
    if errs:
        out.append(f"  runs with errors {len(errs)}: " +
                   ", ".join(sorted({str(r['err'])[:60] for r in errs})))
    out.append("")
    out.append("Full tool-call census: " +
               ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="http://localhost:8080",
                    help="model server base URL — MUST include the scheme (default: %(default)s)")
    ap.add_argument("--model", default="qwen3.5:9b",
                    help="model name (ignored by llama.cpp, which serves what it loaded)")
    ap.add_argument("--provider", default="llamacpp", choices=("llamacpp", "ollama"))
    ap.add_argument("--mode", default=None,
                    help="force a single harness mode (default: each task's own modes)")
    ap.add_argument("--runs", type=int, default=3,
                    help="repeats per task; 3+ because sampling is not pinned (default: %(default)s)")
    ap.add_argument("--think", action="store_true", default=True,
                    help="model thinking mode, the harness default (on)")
    ap.add_argument("--no-think", dest="think", action="store_false")
    ap.add_argument("--timeout", type=int, default=600, help="per-run seconds")
    ap.add_argument("--tasks", nargs="*", help="only these task ids")
    ap.add_argument("--json", metavar="PATH", help="write the full per-run records here")
    args = ap.parse_args()

    if "://" not in args.host:
        ap.error(f"--host needs a scheme, e.g. http://{args.host}")

    tasks = TASKS
    if args.tasks:
        want = set(args.tasks)
        tasks = [t for t in TASKS if t.id in want]
        missing = want - {t.id for t in tasks}
        if missing:
            ap.error(f"unknown task id(s): {', '.join(sorted(missing))}")

    rows = []
    for task in tasks:
        modes = (args.mode,) if args.mode else task.modes
        for mode in modes:
            for i in range(args.runs):
                r = run_once(task, host=args.host, model=args.model,
                             provider=args.provider, mode=mode,
                             think=args.think, timeout=args.timeout)
                rows.append(r)
                flag = "" if r["used_ideal"] else "  <- ideal tool not used"
                print(f"[{len(rows):3d}] {task.id:22s} {mode:6s} run{i} "
                      f"{r['secs']:6.1f}s {r['n_calls']:2d} calls "
                      f"{len(r['hits'])}/{r['n_must']} facts{flag}", flush=True)

    print()
    print(summarise(rows))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))
        print(f"\nper-run records written to {args.json}")


if __name__ == "__main__":
    main()
