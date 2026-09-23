#!/usr/bin/env python3
"""Measure how well a local model uses the harness's code-navigation tools.

Not a unit test: it needs a running model server, takes minutes, and its results
are statistical rather than pass/fail.  It lives outside tests/ so
`python -m unittest discover tests` never picks it up.

    python evals/run_evals.py --runs 3
    python evals/run_evals.py --mode coding --runs 3 --tasks line-to-definition
    python evals/run_evals.py --json baseline.json
    python evals/run_evals.py --index --runs 3      # with the code index on (/index on)
    python evals/run_evals.py --cache evals/.cache/runs.jsonl --runs 5
                                                    # reuse runs whose inputs did not change

--cache keeps every run under a fingerprint of everything that can change its
outcome: the task, the files of the project it runs in, the model and settings,
the system prompt and tool schemas the model is sent, and the harness code that
produces tool output.  A run is reused only when the fingerprint matches, so an
unchanged baseline (e.g. --index off) is not rerun, and anything that could
affect it triggers a fresh run.  Asking for more runs than are cached tops up.
Caching freezes noise as well as signal: prefer --runs 5+ for a stored baseline.

The model server samples with its own defaults (llama.cpp typically temperature
0.8 and a random seed) and the harness sends no sampling parameters at all, so
runs are NOT reproducible.  Always use --runs >= 3 and read the spread, not the
mean: the same task and config has ranged from 1 to 25 tool calls.
"""
import argparse
import hashlib
import json
import os
import subprocess
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

from evals.tasks import INDEX_TASKS, LANG_TASKS, TASKS  # noqa: E402
from harness.harness import (Harness, ChatEvent, DoneEvent, ErrorEvent,  # noqa: E402
                             ToolCallEvent, ToolResultEvent)


def run_once(task, *, host, model, provider, mode, think, timeout, index=False):
    """One task, one fresh conversation.  Returns what the model did."""
    h = Harness(host=host, model=model, workdir=REPO / task.workdir, provider=provider)
    h.mode = mode
    h.stream = False          # deltas would just duplicate the final ChatEvent
    h.think = think
    # Nobody is there to answer: without this a run that calls ask_user blocks forever.
    # The model mostly asks "want me to look into X too?" once it has answered, so
    # decline — an open-ended reply sent it exploring and its last message then
    # described whatever it found next.
    h._ask_user = lambda q: "No thanks, that's all I needed."
    if index:
        h.set_index(True)
        h.index.wait_fresh()  # the build is not the model's time
    h.messages = [{"role": "system", "content": h._build_system_prompt()}]

    sub = h.event_queue.subscribe(replay=False)
    calls: list[tuple[str, dict]] = []
    results: list[str] = []
    answer, err = "", None
    replies: list[str] = []

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
                    replies.append(ev.text or "")
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
    h.shutdown_index()

    names = [c[0] for c in calls]
    # Grade every reply of the turn, not just the last: a fact stated before a
    # follow-up question ("…in net.py. Want the callers too?") was still given.
    low = "\n".join(replies).lower()
    return {
        "task": task.id, "mode": mode, "think": think, "index": index,
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


# Harness code whose behaviour reaches the model's tool results; the prompt and
# tool schemas are fingerprinted separately, as rendered.
_CODE_FILES = ("harness/tools.py", "harness/code_nav.py", "harness/harness.py",
               "harness/net.py")
_INDEX_CODE_FILES = ("harness/code_index.py",)


def _project_hash(workdir: Path) -> str:
    """Hash of the files the model can read there (git-tracked + untracked,
    .gitignore respected), so an edited fixture invalidates its runs."""
    h = hashlib.sha256()
    try:
        out = subprocess.run(["git", "-C", str(workdir), "ls-files", "-co", "--exclude-standard", "-z"],
                             capture_output=True, check=True, timeout=30).stdout
        files = sorted(f for f in out.decode().split("\0") if f)
    except (OSError, subprocess.SubprocessError):
        files = sorted(str(p.relative_to(workdir)) for p in workdir.rglob("*") if p.is_file())
    for rel in files:
        p = workdir / rel
        if p.is_file():
            h.update(rel.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


_env_cache: dict = {}


def fingerprint(task, *, mode, think, index, model, provider, host) -> str:
    """Everything that can change a run's outcome, hashed."""
    key = (task.workdir, mode, index)
    if key not in _env_cache:
        h = Harness(host=host, model=model, workdir=REPO / task.workdir, provider=provider)
        h.mode = mode
        if index:
            h.set_index(True)
        prompt = h._build_system_prompt()
        schemas = json.dumps(h._current_tools(), sort_keys=True)
        served = h.client.model          # a fixed-model server reports what it serves
        h.shutdown_index()
        h.logger.close()
        code = hashlib.sha256()
        for f in _CODE_FILES + (_INDEX_CODE_FILES if index else ()):
            code.update((REPO / f).read_bytes())
        _env_cache[key] = hashlib.sha256("\0".join([
            prompt, schemas, served, code.hexdigest(), _project_hash(REPO / task.workdir),
        ]).encode()).hexdigest()
    spec = json.dumps({"id": task.id, "prompt": task.prompt, "must": list(task.must),
                       "ideal": sorted(task.ideal), "max_calls": task.max_calls,
                       "mode": mode, "think": think, "index": index, "provider": provider},
                      sort_keys=True)
    return hashlib.sha256((spec + _env_cache[key]).encode()).hexdigest()[:24]


def load_cache(path: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                entry = json.loads(line)
                out.setdefault(entry["fp"], []).append(entry["record"])
    return out


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
    nav = {"code_outline", "find_symbol", "read_symbol", "find_references", "file_dependencies",
           "index_search", "index_text", "index_callers", "index_map", "index_file", "index_status"}
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
    ap.add_argument("--suite", choices=("nav", "index", "lang", "all"), default="nav",
                    help="nav = the code-navigation tasks (default), index = the code-index "
                         "use cases, lang = 3 tasks per language on evals/lang/<lang>/project, "
                         "all = every task")
    ap.add_argument("--index", action="store_true",
                    help="turn the code index on, so the model gets the index_* tools")
    ap.add_argument("--cache", metavar="PATH",
                    help="JSONL store of past runs: reuse those whose fingerprint matches, "
                         "append new ones (e.g. evals/.cache/runs.jsonl)")
    ap.add_argument("--refresh", action="store_true",
                    help="with --cache: ignore stored runs and run everything again "
                         "(new runs are still stored)")
    args = ap.parse_args()

    if "://" not in args.host:
        ap.error(f"--host needs a scheme, e.g. http://{args.host}")

    pool = {"nav": TASKS, "index": INDEX_TASKS, "lang": LANG_TASKS,
            "all": TASKS + INDEX_TASKS + LANG_TASKS}[args.suite]
    tasks = pool
    if args.tasks:
        want = set(args.tasks)
        pool = TASKS + INDEX_TASKS + LANG_TASKS
        tasks = [t for t in pool if t.id in want]
        missing = want - {t.id for t in tasks}
        if missing:
            ap.error(f"unknown task id(s): {', '.join(sorted(missing))}")

    cache_path = Path(args.cache) if args.cache else None
    cache = load_cache(cache_path) if cache_path else {}
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
    rows, reused = [], 0
    for task in tasks:
        modes = (args.mode,) if args.mode else task.modes
        for mode in modes:
            stored: list[dict] = []
            fp = None
            if cache_path:
                fp = fingerprint(task, mode=mode, think=args.think, index=args.index,
                                 model=args.model, provider=args.provider, host=args.host)
                stored = [] if args.refresh else cache.get(fp, [])
            for i in range(args.runs):
                if i < len(stored):
                    r, source = stored[i], "cached"
                    reused += 1
                else:
                    r = run_once(task, host=args.host, model=args.model,
                                 provider=args.provider, mode=mode,
                                 think=args.think, timeout=args.timeout, index=args.index)
                    source = ""
                    if cache_path and not r.get("err"):
                        with cache_path.open("a") as f:
                            f.write(json.dumps({"fp": fp, "record": r}) + "\n")
                rows.append(r)
                flag = "" if r["used_ideal"] else "  <- ideal tool not used"
                print(f"[{len(rows):3d}] {task.id:22s} {mode:6s} run{i} "
                      f"{r['secs']:6.1f}s {r['n_calls']:2d} calls "
                      f"{len(r['hits'])}/{r['n_must']} facts{flag}"
                      + (f"  ({source})" if source else ""), flush=True)

    print()
    if cache_path:
        print(f"{reused} of {len(rows)} runs reused from {cache_path}; "
              f"{len(rows) - reused} run now\n")
    print(summarise(rows))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))
        print(f"\nper-run records written to {args.json}")


if __name__ == "__main__":
    main()
