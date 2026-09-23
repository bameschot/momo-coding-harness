#!/usr/bin/env python3
"""Per-language scorecard for the code index.  No model involved.

Each evals/lang/<lang>/ holds a small synthetic project built around that
language's tricky constructs, plus expect.py with hand-written ground truth.
This indexes the project and scores what the index gets right:

    definitions   recall of expected definitions; ABSENT names must not appear
    search        top-1 and mean reciprocal rank of index_search answers
    callers       recall / precision of index_callers' level-1 uses, role accuracy,
                  and the expected level-2 chain
    imports       index_file's "Imported by" against the real importers
    freshness     an appended definition is found right after the edit
    efficiency    build time, index memory, query latency, and the size of an
                  index_callers answer vs the grep_files output for the same name

    python evals/lang_bench.py                    # every language
    python evals/lang_bench.py --langs c java -v  # some, with every failure listed
    python evals/lang_bench.py --scale 50         # build cost with 50 copies

KNOWN_GAPS in expect.py are expectations that fail today, each with a reason:
they are reported separately and do not fail the run.  A known gap that starts
passing is reported too, so the list cannot go stale.  The exit status is 1 when
anything outside KNOWN_GAPS fails — tests/test_lang_bench.py relies on that.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from harness import code_index as ci  # noqa: E402
from harness import code_nav, tools  # noqa: E402

LANG_DIR = Path(__file__).resolve().parent / "lang"
_RESULT_LINE = re.compile(r"^(\S+):L(\d+)-(\d+)\s+(\S+)\s+(\S+)\s+\|")


@dataclass
class Result:
    lang: str
    metrics: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)    # (key, detail)
    gaps: list = field(default_factory=list)        # (key, reason)
    fixed: list = field(default_factory=list)       # known gaps that now pass
    extras: list = field(default_factory=list)      # name-matched uses that are not real ones


def load_expect(lang: str):
    spec = importlib.util.spec_from_file_location(f"expect_{lang}", LANG_DIR / lang / "expect.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def langs_available() -> list[str]:
    return sorted(p.name for p in LANG_DIR.iterdir() if (p / "expect.py").exists())


def line_of(root: Path, rel: str, anchor: str) -> int:
    """1-based line of the only line containing `anchor` — ground truth comes
    from the text, never from the index."""
    hits = [i for i, line in enumerate((root / rel).read_text().splitlines(), 1) if anchor in line]
    if len(hits) != 1:
        raise ValueError(f"anchor {anchor!r} matches {len(hits)} lines in {rel}; make it unique")
    return hits[0]


class Scorer:
    def __init__(self, res: Result, gaps: dict):
        self.res, self.known = res, gaps

    def check(self, key: str, ok: bool, detail: str = "") -> bool:
        if ok:
            if key in self.known:
                self.res.fixed.append(key)
        elif key in self.known:
            self.res.gaps.append((key, self.known[key]))
        else:
            self.res.failures.append((key, detail))
        return ok


def _parse_results(out: str) -> list[tuple[str, str, str]]:
    return [(m.group(1), m.group(4), m.group(5))
            for m in (_RESULT_LINE.match(l) for l in out.splitlines()) if m]


def _importers(out: str) -> set[str]:
    got, inside = set(), False
    for line in out.splitlines():
        if line.startswith("Imported by"):
            inside = True
            continue
        if inside:
            m = re.match(r"^  (\S+):L\d+", line)
            if not m:
                break
            got.add(m.group(1))
    return got


def run_lang(lang: str, scale: int = 1) -> Result:
    exp = load_expect(lang)
    res = Result(lang)
    sc = Scorer(res, getattr(exp, "KNOWN_GAPS", {}))
    src = LANG_DIR / lang / "project"
    tmp = Path(tempfile.mkdtemp(prefix=f"lang_bench_{lang}_")).resolve()
    try:
        if scale > 1:
            for i in range(scale):
                shutil.copytree(src, tmp / f"copy{i:03d}")
        else:
            shutil.copytree(src, tmp, dirs_exist_ok=True)
        idx = ci.ProjectIndex(tmp)
        t0 = time.perf_counter()
        idx.start()
        idx.wait_fresh()
        res.metrics["build_ms"] = round((time.perf_counter() - t0) * 1000)
        res.metrics["files"] = idx.live_count()
        res.metrics["lines"] = sum(e.nlines for _, e in idx._alive())
        res.metrics["mem_kb"] = round(idx.mem_used() / 1024)
        if scale == 1:
            _score(exp, idx, tmp, sc, res)
        idx.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def _score(exp, idx, root: Path, sc: Scorer, res: Result) -> None:
    m = res.metrics
    with idx._lock:
        by_file = {e.path: list(e.symbols) for _, e in idx._alive()}

    # definitions
    found = 0
    for qual, kind, rel, anchor in exp.DEFINITIONS:
        line = line_of(root, rel, anchor)
        syms = by_file.get(rel, [])
        # The anchor is any line of the definition: its header, or a body line
        # when several same-named definitions share an identical header.
        ok = any(s.qualname == qual and s.kind == kind and s.start <= line <= s.end for s in syms)
        near = [f"{s.kind} {s.qualname} L{s.start}" for s in syms
                if s.name == qual.rsplit(".", 1)[-1]]
        found += sc.check(f"def:{qual}@{rel}#{anchor}", ok,
                          f"not found at L{line}" + (f" (has: {', '.join(near)})" if near else ""))
    m["def_recall"] = found / max(len(exp.DEFINITIONS), 1)
    absent_ok = 0
    for name, rel in exp.ABSENT:
        bad = [s for s in by_file.get(rel, []) if s.name == name]
        absent_ok += sc.check(f"absent:{name}@{rel}", not bad,
                              "indexed as " + ", ".join(f"{s.kind} L{s.start}" for s in bad))
    m["absent_ok"] = absent_ok / len(exp.ABSENT) if exp.ABSENT else None

    # search
    rr, top1, lat = 0.0, 0, 0.0
    for entry in exp.SEARCHES:
        query, kwargs, want = entry[0], dict(entry[1]), entry[2]
        want_file = entry[3] if len(entry) > 3 else None
        if isinstance(query, tuple):
            kwargs["path"] = query[0]
            label = f"line {query[0]}#{query[1]}"
            query = str(line_of(root, query[0], query[1]))
        else:
            label = query + (f" {kwargs}" if kwargs else "")
        t0 = time.perf_counter()
        out = ci.index_search(query, workdir=root, index=idx, **kwargs)
        lat += time.perf_counter() - t0
        ranked = _parse_results(out)
        rank = next((i for i, (path, _k, q) in enumerate(ranked, 1)
                     if q == want and (want_file is None or path == want_file)), 0)
        rr += 1 / rank if rank else 0
        top1 += rank == 1
        got = f"{ranked[0][2]} ({ranked[0][0]})" if ranked else out.splitlines()[0][:80]
        sc.check(f"search:{label}", rank == 1, f"rank {rank or '-'}; top was {got}")
    n = max(len(exp.SEARCHES), 1)
    m["search_top1"] = top1 / n
    m["search_mrr"] = rr / n
    m["search_ms"] = round(lat / n * 1000, 1)

    # callers
    tp = fp = fn = role_ok = role_n = 0
    idx_chars = grep_chars = 0
    for name, want in exp.CALLERS.items():
        truth = {(rel, line_of(root, rel, a)): role for rel, a, role in want}
        bare, want_recv, cls = ci.query_target(idx, name)
        uses, _defs = ci._uses(idx, bare, want_recv, None, cls)
        got = {(u[0], u[1]): u[2] for u in uses}
        for (rel, line), role in truth.items():
            hit = (rel, line) in got
            anchor = next(a for r, a, _ in want if r == rel and line_of(root, r, a) == line)
            sc.check(f"caller:{name}@{rel}#{anchor}", hit, "missed")
            if hit:
                role_n += 1
                role_ok += got[(rel, line)] == role
                sc.check(f"role:{name}@{rel}#{anchor}", got[(rel, line)] == role,
                         f"tagged {got[(rel, line)]}, expected {role}")
        for rel, line in sorted(got.keys() - truth.keys()):
            text = (root / rel).read_text().splitlines()[line - 1].strip()
            res.extras.append(f"{name}: {rel}:{line} ({got[(rel, line)]}) {text[:70]}")
        tp += len(truth.keys() & got.keys())
        fn += len(truth.keys() - got.keys())
        fp += len(got.keys() - truth.keys())
        idx_chars += len(ci.index_callers(name, workdir=root, index=idx))
        grep_chars += len(tools._grep_files(rf"\b{re.escape(bare)}\b", ".", workdir=root))
    m["callers_recall"] = tp / max(tp + fn, 1)
    m["callers_precision"] = tp / max(tp + fp, 1)
    m["role_acc"] = role_ok / max(role_n, 1)
    m["callers_vs_grep"] = round(idx_chars / max(grep_chars, 1), 2)

    for name, wanted in getattr(exp, "CHAINS", {}).items():
        out = ci.index_callers(name, depth=2, workdir=root, index=idx)
        level2 = out.split("Level 2", 1)[1] if "Level 2" in out else ""
        for q in sorted(wanted):
            ok = re.search(rf"\s\w+ (\S+\.)?{re.escape(q)}\s*$", level2, re.M) is not None
            sc.check(f"chain:{name}->{q}", ok, "not at level 2")

    # imports
    i_tp = i_fp = i_fn = 0
    for rel, want in exp.IMPORTS.items():
        got = _importers(ci.index_file(rel, workdir=root, index=idx))
        for imp in sorted(want - got):
            sc.check(f"import:{rel}<-{imp}", False, "importer missed")
        for imp in sorted(got - want):
            sc.check(f"import-extra:{rel}<-{imp}", False, "not a real importer")
        for imp in sorted(want & got):
            sc.check(f"import:{rel}<-{imp}", True)
        i_tp += len(want & got)
        i_fn += len(want - got)
        i_fp += len(got - want)
    m["import_recall"] = i_tp / (i_tp + i_fn) if i_tp + i_fn else None
    m["import_precision"] = i_tp / (i_tp + i_fp) if i_tp + i_fp else None

    # freshness: an edit is visible to the very next query
    rel, text, qual = exp.FRESH
    with open(root / rel, "a") as f:
        f.write(text)
    idx.invalidate([rel])
    out = ci.index_search(qual.rsplit(".", 1)[-1], workdir=root, index=idx)
    ranked = _parse_results(out)
    m["fresh"] = sc.check("fresh", bool(ranked) and ranked[0][2] == qual,
                          f"top was {ranked[0][2] if ranked else out[:60]}")


# ── output ───────────────────────────────────────────────────────────────────

_COLS = [("defs", "def_recall", "%"), ("absent", "absent_ok", "%"), ("top1", "search_top1", "%"),
         ("mrr", "search_mrr", "f"), ("call R", "callers_recall", "%"), ("call P", "callers_precision", "%"),
         ("roles", "role_acc", "%"), ("imp R", "import_recall", "%"), ("imp P", "import_precision", "%"),
         ("fresh", "fresh", "b"), ("files", "files", "d"), ("lines", "lines", "d"),
         ("build ms", "build_ms", "d"), ("mem KB", "mem_kb", "d"), ("q ms", "search_ms", "f1"),
         ("vs grep", "callers_vs_grep", "x")]


def _fmt(v, how) -> str:
    if v is None:
        return "-"
    return {"%": lambda: f"{v * 100:.0f}%", "f": lambda: f"{v:.2f}", "f1": lambda: f"{v:.1f}",
            "b": lambda: "ok" if v else "FAIL", "d": lambda: str(v), "x": lambda: f"{v:.2f}x"}[how]()


def report(results: list[Result], verbose: bool) -> str:
    w = 17
    out = ["lang".ljust(w) + "".join(h.rjust(max(len(h), 6) + 2) for h, _, _ in _COLS)
           + "   fail  gaps"]
    for r in results:
        out.append(r.lang.ljust(w) + "".join(_fmt(r.metrics.get(k), how).rjust(max(len(h), 6) + 2)
                                             for h, k, how in _COLS)
                   + f"   {len(r.failures):4d}  {len(r.gaps):4d}")
    for r in results:
        if r.failures:
            out.append(f"\n{r.lang}: {len(r.failures)} failure(s)")
            out.extend(f"  FAIL {k}: {d}" for k, d in (r.failures if verbose else r.failures[:15]))
            if not verbose and len(r.failures) > 15:
                out.append(f"  ... {len(r.failures) - 15} more (-v lists all)")
        if r.gaps and verbose:
            out.append(f"\n{r.lang}: {len(r.gaps)} known gap(s)")
            out.extend(f"  gap  {k}: {why}" for k, why in r.gaps)
        if r.extras and verbose:
            out.append(f"\n{r.lang}: {len(r.extras)} caller false positive(s) (name-based matching, "
                       f"lowers 'call P'):")
            out.extend(f"  extra {x}" for x in r.extras)
        if r.fixed:
            out.append(f"\n{r.lang}: known gaps that now PASS — remove them from KNOWN_GAPS:")
            out.extend(f"  fixed {k}" for k in r.fixed)
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--langs", nargs="*", help="only these (default: every evals/lang/<lang>)")
    ap.add_argument("--scale", type=int, default=1,
                    help="index N copies of each project and report build cost only")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every failure and known gap")
    ap.add_argument("--json", metavar="PATH", help="write the per-language results here")
    args = ap.parse_args()
    langs = args.langs or langs_available()
    results = [run_lang(lang, args.scale) for lang in langs]
    if args.scale > 1:
        print("lang".ljust(11) + "files".rjust(8) + "lines".rjust(9) + "build ms".rjust(10)
              + "ms/1k lines".rjust(13) + "mem KB".rjust(9) + "KB/file".rjust(9))
        for r in results:
            mt = r.metrics
            print(r.lang.ljust(11) + f"{mt['files']:8d}{mt['lines']:9d}{mt['build_ms']:10d}"
                  f"{mt['build_ms'] / max(mt['lines'], 1) * 1000:13.1f}{mt['mem_kb']:9d}"
                  f"{mt['mem_kb'] / max(mt['files'], 1):9.1f}")
        return 0
    print(report(results, args.verbose))
    if args.json:
        Path(args.json).write_text(json.dumps([r.__dict__ for r in results], indent=1))
    return 1 if any(r.failures for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
