#!/usr/bin/env python3
"""Multi-part change requests in coding mode, against a live model.

Change requests with several parts on a copy of evals/edit/project, each part
graded on its own by a hidden check the model never sees.  A one-step control
task checks the harness stays out of the way when there is one thing to do.
It measures what goes wrong across several parts: a turn that ends with work
only announced (the harness's next-step note), edits that drop a neighbouring
definition (refused by the "keep" edit feature; lost = original definitions
missing at the end), and the time cap.

Rounds 1-4 (evals/README.md, raw rows in evals/todos/) ran this as
todos_bench.py, an A/B of the todos tool, which was removed after it.

    python evals/multipart_bench.py --host http://127.0.0.1:9931 --runs 3
    python evals/multipart_bench.py --runs 2 --tasks prose-6
    python evals/multipart_bench.py --runs 3 --context-limit 14000   # force compaction mid-task
    python evals/multipart_bench.py --selftest      # the checks against reference solutions

Per run it records: the parts that pass, the project's own tests, model calls,
time and tokens, next-step notes, refused edits and lost definitions.
Sampling is unpinned: read the spread over --runs, not one run.
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
PROJECT = REPO / "evals" / "edit" / "project"

# The harness writes prefs, sessions and logs under HOME.
if not os.environ.get("MOMO_EVAL_KEEP_HOME"):
    os.environ["HOME"] = tempfile.mkdtemp(prefix="momo_multipart_home_")

from harness import code_nav  # noqa: E402
from harness.code_index.defdiff import _defs  # noqa: E402
from harness.harness import (ChatEvent, DoneEvent, ErrorEvent, Harness,  # noqa: E402
                             ToolCallEvent, ToolResultEvent)

_PRELUDE = """
import ast, io, pathlib, re, sys, unittest
sys.path.insert(0, {root!r})
ROOT = pathlib.Path({root!r})
def src(rel):
    return (ROOT / rel).read_text()
def raises(exc, fn, *a):
    try:
        fn(*a)
    except exc:
        return True
    return False
"""


@dataclass(frozen=True)
class Task:
    id: str
    prompt: str
    parts: dict          # part name -> check script (passes when it exits 0)
    reference: tuple     # (path, old, new) edits that solve it, for --selftest
    note: str = ""


TASKS = [
    Task("numbered-4",
         "Please make these changes in the shop package:\n"
         "1. format_price in shop/pricing.py takes an optional currency symbol that goes in "
         "front: format_price(12.5, \"€\") gives \"€12.50\". Without it the output stays as it is.\n"
         "2. Add Inventory.remove(sku) in shop/inventory.py: it removes the item with that SKU "
         "and returns it, and raises KeyError when there is none.\n"
         "3. free_shipping in shop/shipping.py is also true for the \"eu\" zone once the subtotal "
         "is 100 or more.\n"
         "4. Add a test for Inventory.remove to tests/test_shop.py.\n"
         "Run the tests when you are done.",
         {
             "currency": """
from shop.pricing import format_price
assert format_price(12.5, "€") == "€12.50", format_price(12.5, "€")
assert format_price(3) == "3.00", format_price(3)
""",
             "remove": """
from shop.inventory import Inventory, Item
inv = Inventory([Item("a", "A", 1.0, 1), Item("b", "B", 2.0, 1)])
assert inv.remove("a").sku == "a"
assert [i.sku for i in inv.items] == ["b"]
assert raises(KeyError, inv.remove, "zz"), "no KeyError for a missing SKU"
""",
             "eu-free": """
from shop.shipping import free_shipping
assert free_shipping(100, "eu") and not free_shipping(99.99, "eu")
assert free_shipping(50, "domestic") and not free_shipping(49, "domestic")
assert not free_shipping(500, "intl")
""",
             "test-added": """
assert ".remove(" in src("tests/test_shop.py"), "no test calls remove"
r = unittest.TextTestRunner(stream=io.StringIO()).run(
    unittest.defaultTestLoader.loadTestsFromName("tests.test_shop"))
assert r.wasSuccessful(), "the tests fail"
assert r.testsRun >= 4, f"{r.testsRun} tests"
""",
         },
         (("shop/pricing.py", 'def format_price(amount):\n    """An amount with two decimals, e.g. \'12.50\'."""\n    return f"{amount:.2f}"',
           'def format_price(amount, currency=""):\n    return f"{currency}{amount:.2f}"'),
          ("shop/inventory.py", "    def restock(self, sku, qty):",
           "    def remove(self, sku):\n        item = self.find(sku)\n        if item is None:\n"
           "            raise KeyError(sku)\n        self.items.remove(item)\n        return item\n\n"
           "    def restock(self, sku, qty):"),
          ("shop/shipping.py", 'return zone == "domestic" and subtotal >= 50',
           'return (zone == "domestic" and subtotal >= 50) or (zone == "eu" and subtotal >= 100)'),
          ("tests/test_shop.py", "    def test_slug(self):",
           "    def test_remove(self):\n        inv = Inventory([Item(\"a1\", \"Apple\", 0.5, 10)])\n"
           "        self.assertEqual(inv.remove(\"a1\").sku, \"a1\")\n\n    def test_slug(self):")),
         note="a numbered list over four files, ending in a test run"),
    Task("prose-chain",
         "The cart needs some work. Customers should be able to take an item out again: "
         "Cart.remove(sku) drops every line for that SKU and returns how many lines it removed. "
         "We also want Cart.item_count(), the total quantity over all lines, and the summary "
         "should then read like '3 items, total 12.10' instead of counting lines (use '1 item' "
         "for a single one). Finally, checkout() in shop/checkout.py should put the item count "
         "in its result under 'items'.",
         {
             "remove": """
from shop.cart import Cart
from shop.inventory import Item
a, b = Item("a", "A", 1.0), Item("b", "B", 2.0)
c = Cart(); c.add(a, 2); c.add(b, 1); c.add(a, 1)
assert c.remove("a") == 2, c.remove
assert [(i.sku, q) for i, q in c.lines] == [("b", 1)], c.lines
""",
             "item-count": """
from shop.cart import Cart
from shop.inventory import Item
c = Cart(); c.add(Item("a", "A", 1.0), 2); c.add(Item("b", "B", 2.0), 1)
assert c.item_count() == 3, c.item_count()
assert Cart().item_count() == 0
""",
             "summary": """
from shop.cart import Cart
from shop.inventory import Item
c = Cart(); c.add(Item("a", "A", 1.0), 3)
assert c.summary() == "3 items, total 3.63", c.summary()
one = Cart(); one.add(Item("a", "A", 1.0), 1)
assert one.summary() == "1 item, total 1.21", one.summary()
""",
             "checkout-items": """
from shop.cart import Cart
from shop.checkout import checkout
from shop.inventory import Item
c = Cart(); c.add(Item("a", "A", 1.0), 3)
r = checkout(c)
assert r.get("items") == 3, r
assert r["total"] == 3.63 and "text" in r, r
""",
         },
         (("shop/cart.py", "    def subtotal(self):",
           "    def remove(self, sku):\n        before = len(self.lines)\n"
           "        self.lines = [(i, q) for i, q in self.lines if i.sku != sku]\n"
           "        return before - len(self.lines)\n\n    def item_count(self):\n"
           "        return sum(q for _, q in self.lines)\n\n    def subtotal(self):"),
          ("shop/cart.py", 'return f"{len(self.lines)} lines, total {format_price(self.total())}"',
           'n = self.item_count()\n        return f"{n} item{\'\' if n == 1 else \'s\'}, total {format_price(self.total())}"'),
          ("shop/checkout.py", 'return {"total": total, "text": f"Pay {format_price(total)}"}',
           'return {"total": total, "text": f"Pay {format_price(total)}", "items": cart.item_count()}')),
         note="prose, and the parts build on each other (summary and checkout need item_count)"),
    Task("prose-6",
         "A few things for the shop package. Belgium and France are missing from the tax table: "
         "Belgium pays 21% and France 20%. calc_tax should refuse a negative amount with a "
         "ValueError. Inventory needs a total_value() method that adds up price times quantity "
         "over all items. tax_report in shop/reports.py should also return the region it was "
         "made for, under 'region'. Shipping to the 'uk' zone is missing: 7.0 per parcel plus "
         "1.5 per kg. And slug in shop/util.py should drop anything that is not a letter, digit "
         "or space before joining with dashes, so 'Hello, World!' becomes 'hello-world'.",
         {
             "tax-table": """
from shop.pricing import calc_tax
assert calc_tax(100, "be") == 21.0 and calc_tax(100, "fr") == 20.0
assert calc_tax(100, "nl") == 21.0
""",
             "negative-tax": """
from shop.pricing import calc_tax
assert raises(ValueError, calc_tax, -1, "nl"), "no ValueError"
assert calc_tax(0, "nl") == 0
""",
             "total-value": """
from shop.inventory import Inventory, Item
assert Inventory([Item("a", "A", 2.0, 3), Item("b", "B", 1.5, 2)]).total_value() == 9.0
assert Inventory().total_value() == 0
""",
             "report-region": """
from shop.reports import Ledger, tax_report
r = tax_report([100], "nl", Ledger())
assert r.get("region") == "nl", r
assert r["owed"] == 21.0 and "booked" in r and "open" in r, r
""",
             "uk-zone": """
from shop.shipping import Parcel, shipping_cost
assert shipping_cost([Parcel(2)], "uk") == 10.0, shipping_cost([Parcel(2)], "uk")
assert shipping_cost([Parcel(2)], "domestic") == 3.5
assert shipping_cost([Parcel(1)], "us") == 14.0
""",
             "slug": """
from shop.util import slug
assert slug("Hello, World!") == "hello-world", slug("Hello, World!")
assert slug("Big Red Box") == "big-red-box"
""",
         },
         (("shop/pricing.py", 'TAX_RATES = {"nl": 0.21, "de": 0.19, "uk": 0.20}',
           'TAX_RATES = {"nl": 0.21, "de": 0.19, "uk": 0.20, "be": 0.21, "fr": 0.20}'),
          ("shop/pricing.py", "    rate = TAX_RATES.get(region, 0.0)",
           "    if amount < 0:\n        raise ValueError(amount)\n    rate = TAX_RATES.get(region, 0.0)"),
          ("shop/inventory.py", "    def restock(self, sku, qty):",
           "    def total_value(self):\n        return sum(i.price * i.qty for i in self.items)\n\n"
           "    def restock(self, sku, qty):"),
          ("shop/reports.py", 'return {"owed": owed, "booked": booked, "open": round(owed - booked, 2)}',
           'return {"owed": owed, "booked": booked, "open": round(owed - booked, 2), "region": region}'),
          ("shop/shipping.py", '            elif zone == "eu":',
           '            elif zone == "uk":\n                total += 7.0 + parcel.weight * 1.5\n'
           '            elif zone == "eu":'),
          ("shop/util.py", 'return "-".join(text.lower().split())',
           'text = "".join(c for c in text if c.isalnum() or c.isspace())\n'
           '    return "-".join(text.lower().split())')),
         note="six independent parts in one paragraph of prose: the easiest to lose one"),
    Task("single",
         "Add Belgium to the tax table in shop/pricing.py: it pays 21%.",
         {"tax-table": """
from shop.pricing import calc_tax
assert calc_tax(100, "be") == 21.0
"""},
         (("shop/pricing.py", '"uk": 0.20}', '"uk": 0.20, "be": 0.21}'),),
         note="control: one step, nothing to track"),
]
BY_ID = {t.id: t for t in TASKS}


def run_part(script: str, root: Path) -> tuple[bool, str]:
    code = _PRELUDE.format(root=str(root)) + script
    try:
        p = subprocess.run([sys.executable, "-B", "-c", code], cwd=root, capture_output=True,
                           text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "check timed out"
    if p.returncode == 0:
        return True, ""
    lines = [ln for ln in (p.stderr or p.stdout).strip().splitlines() if ln.strip()]
    return False, lines[-1][:200] if lines else f"exit {p.returncode}"


def defined(root: Path) -> set[tuple[str, str]]:
    """(file, qualified name) of every definition in the project's source files,
    for any language the parser knows."""
    out = set()
    if not code_nav.AVAILABLE:
        return out
    for f in root.rglob("*"):
        lang = code_nav.language_for(f) if f.is_file() and "__pycache__" not in f.parts else None
        if not lang:
            continue
        try:
            parsed = code_nav.parse_bytes(lang, f.read_bytes())
        except Exception:
            continue
        rel = f.relative_to(root).as_posix()
        out |= {(rel, s.qualname) for v in _defs(parsed).values() for s in v}
    return out


def own_tests_pass(root: Path) -> bool:
    p = subprocess.run([sys.executable, "-B", "-m", "unittest", "-q", "tests.test_shop"], cwd=root,
                       capture_output=True, text=True, timeout=120)
    return p.returncode == 0


def copy_project() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory(prefix="momo_multipart_wd_")
    wd = Path(tmp.name) / "project"
    shutil.copytree(PROJECT, wd, ignore=shutil.ignore_patterns("__pycache__", ".momo"))
    return tmp, wd


def grade(task: Task, wd: Path) -> dict:
    parts = {name: run_part(script, wd) for name, script in task.parts.items()}
    # Code the request never asked to remove that is gone at the end (an edit
    # that used it as an anchor and left it out).
    lost = sorted(f"{f}:{q}" for f, q in defined(PROJECT) - defined(wd))
    return {"parts": {n: ok for n, (ok, _) in parts.items()}, "lost_defs": lost,
            "part_detail": {n: d for n, (ok, d) in parts.items() if not ok},
            "parts_passed": sum(ok for ok, _ in parts.values()), "n_parts": len(parts),
            "all_passed": all(ok for ok, _ in parts.values()),
            "own_tests": own_tests_pass(wd)}


# ── one run ──────────────────────────────────────────────────────────────────

def run_once(task: Task, *, host: str, timeout: int, max_secs: int,
             context_limit: int | None, keep: Path | None, tag: str) -> dict:
    tmp, wd = copy_project()
    h = Harness(host=host, model="", workdir=wd, provider="llamacpp")
    h.mode = "coding"
    h.stream = False
    if context_limit:
        h.context_fixed, h.context_limit = True, context_limit
    # Nobody answers: a question gets "go on", so the run is about the request.
    h._ask_user = lambda q: "Use your best judgement and carry on."
    h.messages = [{"role": "system", "content": h._build_system_prompt()}]

    sub = h.event_queue.subscribe(replay=False)
    calls: list[tuple[str, dict]] = []
    refusals = 0
    answer, err, cost = "", None, {"eval_tokens": 0, "prompt_total": 0}
    compactions = next_notes = 0
    done = threading.Semaphore(0)       # one release per finished send()
    stop = threading.Event()

    def pump():
        nonlocal answer, err, compactions, next_notes, refusals
        while not stop.is_set():
            try:
                ev = sub.get(timeout=timeout)
            except queue.Empty:
                err = "timeout waiting for events"
                done.release()
                return
            if isinstance(ev, ToolCallEvent):
                calls.append((ev.name, ev.args))
            elif isinstance(ev, ToolResultEvent):
                if (ev.result or "").startswith("ERROR: not written —"):
                    refusals += 1           # an edit that would have dropped a definition
            elif isinstance(ev, ChatEvent):
                if ev.role == "assistant":
                    answer = ev.text or ""
                elif ev.role == "system" and "ompact" in (ev.text or ""):
                    compactions += 1
                elif ev.role == "system" and (ev.text or "").startswith("The reply announced"):
                    next_notes += 1
            elif isinstance(ev, ErrorEvent):
                err = getattr(ev, "text", str(ev))
            elif isinstance(ev, DoneEvent):
                cost["eval_tokens"] += ev.eval_tokens or 0
                cost["prompt_total"] += ev.prompt_total or 0
                done.release()

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    capped = threading.Event()
    watchdog = threading.Timer(max_secs, lambda: (capped.set(), h.cancel())) if max_secs else None
    if watchdog:
        watchdog.start()
    t0 = time.time()
    try:
        h.send(task.prompt)
        done.acquire(timeout=timeout + 30)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    if watchdog:
        watchdog.cancel()
    stop.set()
    t.join(timeout=5)
    sub.close()
    secs = round(time.time() - t0, 1)
    g = grade(task, wd)

    names = [n for n, _ in calls]
    if keep:
        dest = keep / f"{tag}"
        shutil.copytree(wd, dest / "project", dirs_exist_ok=True)
        (dest / "transcript.json").write_text(json.dumps(h.messages, indent=1, default=str))
    h.logger.close()
    tmp.cleanup()
    return {
        "task": task.id, "secs": secs, "capped": capped.is_set(), "err": err,
        "n_calls": len(names), "tools": names,
        "eval_tokens": cost.get("eval_tokens"), "prompt_total": cost.get("prompt_total"),
        "compactions": compactions, "next_step_notes": next_notes,
        "refusals": refusals,
        **g,
        "answer": answer[-600:],
    }


# ── summary ──────────────────────────────────────────────────────────────────

def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def _fmt(x, nd=1):
    return "-" if x is None else f"{x:.{nd}f}"


def summarise(rows: list[dict]) -> str:
    out = []
    head = (f"{'task':12s} {'n':>2s} {'parts':>6s} {'all':>4s} {'tests':>5s} {'notes':>5s} "
            f"{'refs':>4s} {'lost':>4s} {'capped':>6s} {'calls':>5s} {'secs':>6s} "
            f"{'out tok':>8s} {'ctx tok':>9s}")
    out.append(head)
    out.append("-" * len(head))
    order = [t.id for t in TASKS]
    for task in sorted({r["task"] for r in rows}, key=order.index):
        rs = [r for r in rows if r["task"] == task]
        out.append(
            f"{task:12s} {len(rs):2d} "
            f"{_fmt(_mean([r['parts_passed'] / r['n_parts'] for r in rs]) * 100, 0):>5s}% "
            f"{sum(r['all_passed'] for r in rs):>2d}/{len(rs):<1d} "
            f"{sum(r['own_tests'] for r in rs):>3d}/{len(rs):<1d} "
            f"{sum(r['next_step_notes'] for r in rs):>5d} "
            f"{sum(r['refusals'] for r in rs):>4d} "
            f"{sum(bool(r['lost_defs']) for r in rs):>4d} "
            f"{sum(r['capped'] for r in rs):>6d} "
            f"{_fmt(_mean([r['n_calls'] for r in rs]), 1):>5s} "
            f"{_fmt(_mean([r['secs'] for r in rs]), 0):>6s} "
            f"{_fmt(_mean([r['eval_tokens'] for r in rs]), 0):>8s} "
            f"{_fmt(_mean([r['prompt_total'] for r in rs]), 0):>9s}")
    out.append("")
    out.append("parts = mean share of parts passing; all = runs with every part; tests = the "
               "project's own tests pass; notes = the harness's announced-next-step notes; "
               "refs = edits refused for dropping a definition; lost = runs that end with a "
               "definition of the original project missing; capped = runs cancelled at "
               "--max-run-secs.")
    fails: dict = {}
    for r in rows:
        for part, ok in r["parts"].items():
            if not ok:
                fails[(r["task"], part)] = fails.get((r["task"], part), 0) + 1
    if fails:
        out.append("")
        out.append("failing parts (task part: runs): " + "; ".join(
            f"{t} {p}: {n}" for (t, p), n in sorted(fails.items())))
    return "\n".join(out)


# ── main ─────────────────────────────────────────────────────────────────────

def selftest() -> int:
    bad = 0
    for task in TASKS:
        tmp, wd = copy_project()
        before = grade(task, wd)
        for rel, old, new in task.reference:
            p = wd / rel
            s = p.read_text()
            assert s.count(old) == 1, (task.id, rel, old[:40])
            p.write_text(s.replace(old, new))
        after = grade(task, wd)
        tmp.cleanup()
        ok = (before["parts_passed"] == 0 and after["all_passed"] and after["own_tests"]
              and not before["lost_defs"] and not after["lost_defs"])
        bad += not ok
        print(f"{task.id:12s} before {before['parts_passed']}/{before['n_parts']}  "
              f"reference {after['parts_passed']}/{after['n_parts']} tests "
              f"{'ok' if after['own_tests'] else 'FAIL'}  {'ok' if ok else 'BROKEN'}"
              + (f"  {after['part_detail']}" if after["part_detail"] else ""))
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="http://127.0.0.1:9931")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--tasks", nargs="+", choices=list(BY_ID), default=list(BY_ID))
    ap.add_argument("--jobs", type=int, default=4, help="runs at a time (the server's slots)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds without any event")
    ap.add_argument("--max-run-secs", type=int, default=900)
    ap.add_argument("--context-limit", type=int, default=None,
                    help="a fixed, small compaction threshold, so compaction happens mid-task")
    ap.add_argument("--json", metavar="PATH")
    ap.add_argument("--keep", metavar="DIR", help="keep each run's project and transcript here")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())

    keep = Path(args.keep) if args.keep else None
    jobs = [(BY_ID[t], i) for i in range(args.runs) for t in args.tasks]
    rows: list[dict] = []
    lock = threading.Lock()

    def one(job, delay):
        task, i = job
        time.sleep(delay)          # session timestamps have 1 s resolution: start apart
        r = run_once(task, host=args.host, timeout=args.timeout,
                     max_secs=args.max_run_secs, context_limit=args.context_limit,
                     keep=keep, tag=f"{task.id}-{i}")
        with lock:
            rows.append(r)
            print(f"[{len(rows):3d}/{len(jobs)}] {task.id:12s} run{i} {r['secs']:6.1f}s "
                  f"{r['n_calls']:2d} calls  parts {r['parts_passed']}/{r['n_parts']}"
                  f"  refused {r['refusals']}" + ("  (capped)" if r["capped"] else "")
                  + (f"  ERR {r['err'][:60]}" if r["err"] else ""), flush=True)
            if args.json:
                Path(args.json).write_text(json.dumps(rows, indent=1, default=str))
        return r

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(one, job, 1.1 * k if k < args.jobs else 1.1)
                   for k, job in enumerate(jobs)]
    for job, f in zip(jobs, futures):
        if f.exception() is not None:       # a crash in the bench itself, not a model result
            print(f"bench error in {job[0].id} run{job[1]}: {f.exception()!r}")
    print()
    print(summarise(rows))


if __name__ == "__main__":
    main()
