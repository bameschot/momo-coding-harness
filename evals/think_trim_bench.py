#!/usr/bin/env python3
"""What /think-keep's trim would send back, on reasoning you recorded.

No model involved: every stored thinking message in the saved sessions that
led to an assistant message is put through harness/think_trim.trim(), the way
/think-history sends it back with /think-keep.  Reports how much is sent
against the whole reasoning, per model, and checks that the trim is stable
(the same input gives the same text: what keeps llama.cpp's prompt cache warm).

    python evals/think_trim_bench.py                       # ~/.momo-harness/sessions
    python evals/think_trim_bench.py --sessions DIR --show 3
    python evals/think_trim_bench.py --keep more            # /think-keep's level

Sessions stay on this machine; only counts are printed (and, with --show,
one thought before and after).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from harness.paths import momo_home  # noqa: E402
from harness.think_trim import KEEP_LEVELS, trim  # noqa: E402


def pairs(folder: Path):
    """(model, reasoning, the assistant message it led to) per stored thought."""
    for f in sorted(folder.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        model = data.get("model") or "?"
        if model == "fake":                     # test sessions
            continue
        ms = data.get("messages") or []
        for a, b in zip(ms, ms[1:]):
            if a.get("role") == "thinking" and a.get("content") and b.get("role") == "assistant":
                yield model, a["content"], b


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", type=Path, default=momo_home() / "sessions")
    ap.add_argument("--keep", choices=KEEP_LEVELS[1:], default="lean", help="/think-keep level (default lean)")
    ap.add_argument("--show", type=int, metavar="N", help="print the Nth trimmed thought, before and after")
    args = ap.parse_args()

    by = defaultdict(lambda: {"n": 0, "trimmed": 0, "before": 0, "after": 0})
    sizes = defaultdict(lambda: [0, 0])          # thought length band -> [chars, sent]
    shown = 0
    for model, reasoning, msg in pairs(args.sessions):
        sent = trim(reasoning, msg, args.keep)
        assert trim(reasoning, msg, args.keep) == sent, "trim is not stable"
        band = next(b for b in (500, 1000, 2000, 4000, 8000, float("inf")) if len(reasoning) < b)
        sizes[band][0] += len(reasoning)
        sizes[band][1] += len(sent)
        for key in (Path(model).name, "all"):
            row = by[key]
            row["n"] += 1
            row["trimmed"] += sent != reasoning
            row["before"] += len(reasoning)
            row["after"] += len(sent)
        if sent != reasoning:
            shown += 1
            if args.show == shown:
                print(f"──── before ({len(reasoning):,} chars) ────\n{reasoning}\n")
                print(f"──── sent ({len(sent):,} chars) ────\n{sent}\n")
    if not by:
        sys.exit(f"no recorded reasoning in {args.sessions}")
    print(f"keep {args.keep}\n")
    print(f"{'model':36s} {'thoughts':>8s} {'trimmed':>8s} {'chars':>10s} {'sent':>10s} {'saved':>6s}")
    for key, r in sorted(by.items(), key=lambda kv: (kv[0] == "all", kv[0])):
        print(f"{key[:36]:36s} {r['n']:>8,} {r['trimmed']:>8,} {r['before']:>10,} {r['after']:>10,} "
              f"{1 - r['after'] / r['before']:>6.0%}")
    print(f"\n{'thought length':>16s} {'chars':>10s} {'sent':>10s} {'saved':>6s}")
    lo = 0
    for band, (chars, sent) in sorted(sizes.items()):
        label = f"{lo:,}-{band:,.0f}" if band != float("inf") else f"{lo:,}+"
        print(f"{label:>16s} {chars:>10,} {sent:>10,} {1 - sent / chars:>6.0%}")
        lo = band



if __name__ == "__main__":
    main()
