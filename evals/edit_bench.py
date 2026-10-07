#!/usr/bin/env python3
"""Scorecard for edit_file's matcher.  No model involved.

A small model copies old_string with slips: read_file's line numbers, lost or
halved indentation, tabs written as spaces, trailing blanks, curly quotes,
different spacing.  Each case here is a real edit (EDITS: an old block that
occurs once in a fixture, and its replacement) put through one such slip
(MANGLES), then run through edit_file.  The result is one of:

    correct   the file is exactly what the clean edit would have made
    wrong     the file changed, but not into that — the outcome to never allow
    refused   nothing written; "with hint" when the error shows the text to copy

NEGATIVE cases must be refused: an old_string that matches two places once
whitespace is ignored, or that differs from the file in a real token.

The same corpus runs with edit_features off (the matcher as it was before
harness/editing.py) and on, so the gain is measured, not assumed.

    python evals/edit_bench.py          # both, side by side
    python evals/edit_bench.py -v       # also every case that is not correct

tests/test_edit_bench.py gates on: no wrong edits in either setting, and the
features-on matcher correct on at least as many cases as features-off.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from harness import editing  # noqa: E402
from harness.tools import dispatch  # noqa: E402

PY = '''import json


class Inventory:
    """Items by SKU."""

    def __init__(self, items):
        self.items = list(items)

    def find(self, sku):
        for it in self.items:
            if it["sku"] == sku:
                return it
        return None

    def restock(self, sku, qty):
        item = self.find(sku)
        if item is None:
            raise KeyError(sku)
        if qty <= 0:
            raise ValueError("qty must be positive")
        item["qty"] = item.get("qty", 0) + qty
        return item["qty"]

    def report(self, path):
        rows = [f"{it['sku']}: {it.get('qty', 0)}" for it in self.items]
        with open(path, "w") as fh:
            json.dump({"rows": rows, "count": len(rows)}, fh)
        return len(rows)


def load(path):
    with open(path) as fh:
        return Inventory(json.load(fh)["items"])
'''

JS = '''export function total(cart, tax) {
\tlet sum = 0;
\tfor (const line of cart.lines) {
\t\tif (line.qty > 0) {
\t\t\tsum += line.price * line.qty;
\t\t}
\t}
\treturn sum * (1 + tax);
}

export function label(item) {
\tconst name = item.name || "unnamed";
\treturn `${name} (${item.sku})`;
}
'''

YAML = '''services:
  web:
    image: "nginx:1.27"
    ports:
      - "8080:80"
  db:
    image: 'postgres:16'
    environment:
      POSTGRES_DB: shop
'''

FIXTURES = {"shop.py": PY, "cart.js": JS, "compose.yaml": YAML}

# (file, old block, new block): old occurs exactly once in the file.
EDITS = [
    ("shop.py",
     '            if it["sku"] == sku:\n                return it\n',
     '            if it["sku"] == sku:\n                return dict(it)\n'),
    ("shop.py",
     '        if qty <= 0:\n            raise ValueError("qty must be positive")\n',
     '        if qty <= 0:\n            raise ValueError("qty must be positive")\n'
     '        if qty > 1000:\n            raise ValueError("qty too large")\n'),
    ("shop.py",
     '        item = self.find(sku)\n        if item is None:\n            raise KeyError(sku)\n',
     '        item = self.find(sku)\n        if item is None:\n            return 0\n'),
    ("shop.py",
     "        with open(path, \"w\") as fh:\n            json.dump({\"rows\": rows, \"count\": len(rows)}, fh)\n",
     "        with open(path, \"w\") as fh:\n            json.dump({\"rows\": rows}, fh, indent=2)\n"),
    ("shop.py",
     "    with open(path) as fh:\n        return Inventory(json.load(fh)[\"items\"])\n",
     "    with open(path) as fh:\n        data = json.load(fh)\n    return Inventory(data[\"items\"])\n"),
    ("cart.js",
     "\t\tif (line.qty > 0) {\n\t\t\tsum += line.price * line.qty;\n\t\t}\n",
     "\t\tif (line.qty > 0 && !line.free) {\n\t\t\tsum += line.price * line.qty;\n\t\t}\n"),
    ("cart.js",
     "\tconst name = item.name || \"unnamed\";\n\treturn `${name} (${item.sku})`;\n",
     "\tconst name = item.name || \"unnamed\";\n\tif (!item.sku) {\n\t\treturn name;\n\t}\n"
     "\treturn `${name} (${item.sku})`;\n"),
    ("compose.yaml",
     "  db:\n    image: 'postgres:16'\n",
     "  db:\n    image: 'postgres:17'\n"),
]


def _lines_map(fn):
    """A mangle that applies fn to every line of old and new alike."""
    def mangle(old, new, start):
        return ("\n".join(fn(ln) for ln in old.split("\n")),
                "\n".join(fn(ln) for ln in new.split("\n")))
    return mangle


def _kept_lines_map(fn):
    """A slip in the lines the edit keeps: applied to old_string, and to the
    lines of new_string that repeat an old line.  A changed line holds what
    the model wants written, so there is nothing to undo in it."""
    def mangle(old, new, start):
        kept = set(old.split("\n"))
        return ("\n".join(fn(ln) for ln in old.split("\n")),
                "\n".join(fn(ln) if ln in kept else ln for ln in new.split("\n")))
    return mangle


def _numbered(old, new, start):
    num = lambda s: "\n".join(f"{start + i:4}: {ln}" for i, ln in enumerate(s.rstrip("\n").split("\n")))
    return num(old), num(new)


def _dedent(old, new, start):
    cut = len(old) - len(textwrap.dedent(old)) if old else 0
    ws = old[:len(old) - len(old.lstrip())]
    trim = lambda s: "\n".join(ln[len(ws):] if ln.startswith(ws) else ln.lstrip() for ln in s.split("\n"))
    return (trim(old), trim(new)) if cut else (old, new)


def _halve(line):
    ws = line[:len(line) - len(line.lstrip())].replace("\t", "    ")
    return " " * (len(ws) // 2) + line.lstrip()


def _first_line_unindented(old, new, start):
    return old.lstrip(" \t"), new.lstrip(" \t")


def _trailing(old, new, start):
    return "\n".join(ln + "  " if ln.strip() else ln for ln in old.split("\n")), new


_CURLY = str.maketrans({'"': "“", "'": "’"})

MANGLES = {
    "exact":          lambda old, new, start: (old, new),
    "line-numbers":   _numbered,
    "dedented":       _dedent,
    "flat":           _lines_map(str.lstrip),
    "two-space":      _lines_map(_halve),
    "tabs-as-spaces": _lines_map(lambda ln: re.sub(r"^\t+", lambda m: "    " * len(m.group()), ln)),
    "first-line":     _first_line_unindented,
    "trailing-ws":    _trailing,
    "curly-quotes":   _lines_map(lambda ln: ln.translate(_CURLY)),
    "spacing":        _kept_lines_map(lambda ln: ln.replace(", ", ",").replace(" == ", "==")),
}

# Must be refused: (file, old_string, new_string).
TWINS = '''def a():
    if ready:
        go()

def b():
        if ready:
            go()
'''
NEGATIVE = [
    ("twins.py", "if ready:\n    go()\n", "if ready:\n    stop()\n"),          # 2 places, ws aside
    ("shop.py", "        if qty <= 1:\n            raise ValueError(\"qty must be positive\")\n",
     "        if qty < 1:\n            raise ValueError(\"qty must be positive\")\n"),   # a real token differs
    ("shop.py", "        for it in self.stock:\n            if it[\"sku\"] == sku:\n",
     "        for it in self.stock:\n            if it[\"id\"] == sku:\n"),
    ("cart.js", "\treturn sum * (1 - tax);\n", "\treturn sum;\n"),
]
NEG_FIXTURES = {**FIXTURES, "twins.py": TWINS}


def _apply(root: Path, file: str, text: str, old: str, new: str, features) -> tuple[str, str]:
    (root / file).write_text(text, encoding="utf-8")
    out = dispatch("edit_file", {"path": file, "old_string": old, "new_string": new}, root,
                   edit_features=features)
    return out, (root / file).read_text(encoding="utf-8")


def run(features) -> dict:
    """{"cases": [...], "correct", "wrong", "refused", "hinted", "neg_wrong"}."""
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        for file, old, new in EDITS:
            text = FIXTURES[file]
            assert text.count(old) == 1, (file, old)
            want = text.replace(old, new, 1)
            start = text[:text.index(old)].count("\n") + 1
            for mname, mangle in MANGLES.items():
                m_old, m_new = mangle(old, new, start)
                if m_old == old and mname != "exact":
                    continue                     # the slip does not apply here
                out, got = _apply(root, file, text, m_old, m_new, features)
                if got == want:
                    verdict = "correct"
                elif got != text:
                    verdict = "wrong"
                else:
                    verdict = "refused"
                rows.append({"file": file, "mangle": mangle and mname, "verdict": verdict,
                             "hint": "closest" in out or "is in " in out, "out": out,
                             "got": got, "want": want})
        neg = []
        for file, old, new in NEGATIVE:
            text = NEG_FIXTURES[file]
            out, got = _apply(root, file, text, old, new, features)
            neg.append({"file": file, "old": old, "wrong": got != text, "out": out})
    return {"cases": rows,
            "correct": sum(r["verdict"] == "correct" for r in rows),
            "wrong": sum(r["verdict"] == "wrong" for r in rows),
            "refused": sum(r["verdict"] == "refused" for r in rows),
            "hinted": sum(r["verdict"] == "refused" and r["hint"] for r in rows),
            "neg": neg, "neg_wrong": sum(n["wrong"] for n in neg)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    results = {"off": run(frozenset()), "on": run(editing.EDIT_FEATURES)}
    n = len(results["on"]["cases"])
    print(f"{n} mangled edits ({len(EDITS)} edits x {len(MANGLES)} slips), "
          f"{len(NEGATIVE)} must-refuse cases\n")
    print(f"{'':18s} {'off':>8s} {'on':>8s}")
    for k in ("correct", "wrong", "refused", "hinted", "neg_wrong"):
        print(f"{k:18s} {results['off'][k]:8d} {results['on'][k]:8d}")
    print("\nper slip (correct, off -> on):")
    for mname in MANGLES:
        c = {s: [r for r in results[s]["cases"] if r["mangle"] == mname] for s in results}
        if c["on"]:
            print(f"  {mname:16s} {sum(r['verdict'] == 'correct' for r in c['off']):2d} -> "
                  f"{sum(r['verdict'] == 'correct' for r in c['on']):2d}  of {len(c['on'])}")
    if args.verbose:
        for setting, res in results.items():
            for r in res["cases"]:
                if r["verdict"] != "correct":
                    print(f"\n[{setting}] {r['verdict']}: {r['file']} / {r['mangle']}\n  {r['out'][:300]}")
                    if r["verdict"] == "wrong":
                        print("  --- got\n" + r["got"] + "  --- want\n" + r["want"])
            for ng in res["neg"]:
                if ng["wrong"]:
                    print(f"\n[{setting}] NEGATIVE edited: {ng['file']} {ng['old']!r}\n  {ng['out'][:300]}")
    bad = results["on"]["wrong"] + results["on"]["neg_wrong"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
