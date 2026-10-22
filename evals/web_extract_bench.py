#!/usr/bin/env python3
"""How well fetch_url turns real web pages into text — no model involved.

Each case is a real page, snapshotted once so runs are reproducible, with the
facts a model would fetch it for ("needles"), chrome that should never reach
the model ("noise"), and URLs it should still be able to follow ("link
needles").  Every page goes through the REAL decode path (net._decode_body),
so what is measured is exactly what the model is sent before windowing.

    python evals/web_extract_bench.py --snapshot          # download the pages once
    python evals/web_extract_bench.py                     # measure, offline
    python evals/web_extract_bench.py --json base.json    # keep the numbers
    python evals/web_extract_bench.py --compare base.json # before/after table
    python evals/web_extract_bench.py --show wikipedia    # print one page's text

Snapshots live in evals/.cache/web/ (gitignored): pages change, and the bench
should measure the extractor, not the web.

Columns: out = chars of text, tok ≈ out/4, url% = share of the text that is
URLs, needles/noise/links = hits out of the case's list, fake# = heading lines
that are empty or that the page never marked as headings, dup = repeated lines, ms = decode time.
"""
from __future__ import annotations

import argparse
import http.client
import inspect
import json
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from email import message_from_string
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from harness import net  # noqa: E402

CACHE = REPO / "evals" / ".cache" / "web"


@dataclass
class Case:
    id: str
    url: str
    needles: list[str] = field(default_factory=list)
    noise: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)   # substrings of URLs that must stay reachable
    blocked: bool = False                            # expected to be a bot-check page


CASES = [
    Case("mdn", "https://developer.mozilla.org/en-US/docs/Web/HTML/Element/table",
         ["represents tabular data", "Any element that accepts flow content",
          "border-collapse: collapse", "Permitted parents"],
         ["Report a problem with this content", "Learn more"],
         ["HTMLTableElement", "Content_categories"]),
    Case("python-asyncio", "https://docs.python.org/3/library/asyncio-task.html",
         ["Coroutines declared with the async/await syntax", "asyncio.wait_for", "TaskGroup",
          "it cancels aw and raises `TimeoutError`"],
         ["Theme", "Navigation"],
         ["asyncio-eventloop"]),
    Case("wikipedia", "https://en.wikipedia.org/wiki/Markdown",
         ["John Gruber", "Aaron Swartz", "CommonMark", "text/markdown"],
         ["Hidden categories", "Retrieved from", "Edit links", "[edit", "Find sources",
          "Learn how and when to remove this message", "Archived from the original"],
         ["/wiki/Aaron_Swartz", "/wiki/John_Gruber"]),
    Case("github", "https://github.com/psf/requests",
         ["Requests is a simple, yet elegant, HTTP library", "python -m pip install requests"],
         ["Report repository", "Custom properties", "Code of conduct", "Last commit message"],
         ["requests.readthedocs.io"]),
    Case("pypi", "https://pypi.org/project/requests/",
         ["pip install requests", "Keep-Alive & Connection Pooling"],
         ["Release notifications", "RSS feed"],
         ["github.com/psf/requests"]),
    Case("stackoverflow", "https://stackoverflow.com/questions/231767/what-does-the-yield-keyword-do-in-python",
         blocked=True),
    Case("bbc", "https://www.bbc.com/news/technology",
         ["Technology"], [],
         ["bbc.com/news/articles/"]),
    Case("react", "https://react.dev/reference/react/useState",
         ["is a React Hook that lets you add", "const [state, setState] = useState(initialState)"],
         [], ["/reference/react"]),
    Case("docsrs", "https://docs.rs/serde/latest/serde/",
         ["Serde is a framework for serializing and deserializing"],
         [], ["serde.rs"]),
    Case("rustbook", "https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html",
         ["Each value in Rust has an owner", "String::from"],
         [], ["ch04-02-references-and-borrowing"]),
    Case("readthedocs", "https://requests.readthedocs.io/en/latest/user/quickstart/",
         ["r = requests.get", "Response Content"], [], []),
    Case("arxiv", "https://arxiv.org/abs/1706.03762",
         ["Attention Is All You Need", "based solely on attention mechanisms"],
         [], ["/pdf/1706.03762"]),
    Case("man7", "https://man7.org/linux/man-pages/man1/ls.1.html",
         ["list directory contents", "--almost-all"], [], []),
    Case("hn", "https://news.ycombinator.com/",
         ["Hacker News"], [], ["item?id="]),
    Case("npm", "https://www.npmjs.com/package/express", blocked=True),
    Case("effective-go", "https://go.dev/doc/effective_go",
         ["Gofmt", "Semicolons", "defer"], [], []),
    Case("mslearn", "https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/keywords/async",
         ["async", "await"], [], []),
    Case("kubernetes", "https://kubernetes.io/docs/concepts/workloads/pods/",
         ["Pods are the smallest deployable units", "init containers"],
         [], ["/docs/concepts/workloads/pods/init-containers/"]),
    Case("fowler", "https://martinfowler.com/articles/microservices.html",
         ["Componentization via Services", "Smart endpoints and dumb pipes"], [], []),
    Case("verge", "https://www.theverge.com/", ["The Verge"], [], []),
]


# ── snapshots ────────────────────────────────────────────────────────────────

def snapshot(cases: list[Case]) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    for c in cases:
        got = net._get(c.url, allow_private=False, max_bytes=net.DEFAULT_MAX_BYTES, timeout=30)
        if isinstance(got, str):
            print(f"{c.id:16} FAILED {got[:120]}")
            continue
        (CACHE / f"{c.id}.body").write_bytes(got["raw"])
        headers = {k.lower(): v for k, v in got["headers"].items()}
        (CACHE / f"{c.id}.json").write_text(json.dumps(
            {"url": c.url, "final_url": got["final_url"], "status": got["status"],
             "headers": headers}, indent=2))
        print(f"{c.id:16} {got['status']} {len(got['raw']):>9,} bytes  "
              f"{headers.get('content-type', '')}")


def load(c: Case):
    body, meta = CACHE / f"{c.id}.body", CACHE / f"{c.id}.json"
    if not body.exists() or not meta.exists():
        return None
    m = json.loads(meta.read_text())
    hdr = "".join(f"{k}: {v}\n" for k, v in m["headers"].items())
    headers = message_from_string(hdr, _class=http.client.HTTPMessage)
    return body.read_bytes(), headers, m


# ── metrics ──────────────────────────────────────────────────────────────────

class _Headings(HTMLParser):
    """Text of every <h1>-<h6> and <title>, to tell real headings from fake ones."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texts: set[str] = set()
        self._cur: list[str] | None = None
        self._tag = ""

    def handle_starttag(self, tag, attrs):
        if re.fullmatch(r"h[1-6]|title", tag):
            self._cur, self._tag = [], tag

    def handle_endtag(self, tag):
        if tag == self._tag and self._cur is not None:
            self.texts.add(_norm("".join(self._cur)))
            self._cur = None

    def handle_data(self, data):
        if self._cur is not None:
            self._cur.append(data)


def _norm(s: str) -> str:
    s = re.sub(r"<https?://[^>]*>|\]\([^)]*\)|[`*\[\]¶§#]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


_URL = re.compile(r"https?://[^\s<>()\]]+")


def _decode(raw, headers, final_url):
    """(kind, text, links, notes) through the production path; `links` is the
    link index where the extractor has one, else None."""
    notes: list[str] = []
    params = inspect.signature(net._decode_body).parameters
    if "links_out" in params:
        links: list | None = []
        kind, text = net._decode_body(raw, headers, final_url, None, notes, links_out=links)
    else:
        links = None
        kind, text = net._decode_body(raw, headers, final_url, None, notes)
    return kind, text, links, notes


def measure(c: Case) -> dict | None:
    snap = load(c)
    if snap is None:
        return None
    raw, headers, meta = snap
    times = []
    for _ in range(3):
        t = time.perf_counter()
        kind, text, links, notes = _decode(raw, headers, meta["final_url"])
        times.append(time.perf_counter() - t)
    low = text.lower()
    lines = [ln for ln in text.split("\n") if ln.strip()]
    in_fence, prose = False, []
    for ln in text.split("\n"):
        if ln.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            prose.append(ln)
    url_chars = sum(len(m) for m in _URL.findall("\n".join(prose)))

    fake = 0
    if "html" in kind:
        hp = _Headings()
        try:
            hp.feed(raw.decode("utf-8", "replace"))
        except Exception:
            pass
        for ln in prose:
            if re.fullmatch(r"#{1,6}\s*", ln) or (
                    re.match(r"#{1,6} \S", ln) and _norm(ln) not in hp.texts):
                fake += 1   # empty, or never a heading in the page

    if links is not None:
        reachable = " ".join(lk.url for lk in links) + " " + text
    else:
        reachable = text
    # Text of a link inside the content root that never reached the output: a
    # rendering bug (or a prune rule eating links), whatever the needles say.
    flat = re.sub(r"\s+", " ", text.replace("`", "")).lower()
    lost = [lk.text for lk in links or () if lk.in_content
            and re.sub(r"\s+", " ", lk.text.replace("`", "")).lower() not in flat]
    dup = sum(n - 1 for ln, n in Counter(lines).items() if n > 1 and len(ln) > 3
              and not ln.startswith(("```", "}", "{", ")", "]")))
    blocked_noted = any("bot-check" in n for n in notes)
    return {
        "id": c.id, "kind": kind, "raw": len(raw), "out": len(text),
        "url_pct": round(100 * url_chars / max(1, len(text)), 1),
        "needles": sum(1 for n in c.needles if n.lower() in low), "needles_of": len(c.needles),
        "missing": [n for n in c.needles if n.lower() not in low],
        "noise": sum(1 for n in c.noise if n.lower() in low), "noise_of": len(c.noise),
        "noise_hits": [n for n in c.noise if n.lower() in low],
        "links": sum(1 for n in c.links if n in reachable), "links_of": len(c.links),
        "indexed": len(links) if links is not None else None,
        "lost": len(lost), "lost_sample": lost[:5],
        "fake": fake, "dup": dup,
        "fences_ok": text.count("\n```") % 2 == 0 if not text.startswith("```") else True,
        "ms": round(1000 * statistics.median(times), 1),
        "blocked": c.blocked, "blocked_noted": blocked_noted,
    }


# ── output ───────────────────────────────────────────────────────────────────

def _row(r: dict) -> str:
    flag = ""
    if r["blocked"]:
        flag = "bot-check noted" if r["blocked_noted"] else "bot-check NOT noted"
    elif r["missing"] or r["noise_hits"] or r.get("lost"):
        flag = "; ".join([f"missing {r['missing']}"] * bool(r["missing"])
                         + [f"noise {r['noise_hits']}"] * bool(r["noise_hits"])
                         + [f"{r['lost']} link texts lost e.g. {r['lost_sample']}"]
                         * bool(r.get("lost")))
    return (f"{r['id']:15} {r['raw']:>9,} {r['out']:>8,} {r['out'] // 4:>7,} {r['url_pct']:>5.1f} "
            f"{r['needles']:>3}/{r['needles_of']:<2} {r['noise']:>3}/{r['noise_of']:<2} "
            f"{r['links']:>3}/{r['links_of']:<2} {r['fake']:>5} {r['dup']:>4} "
            f"{'ok' if r['fences_ok'] else 'BAD':>4} {r['ms']:>6}  {flag}")


HEAD = (f"{'case':15} {'raw':>9} {'out':>8} {'tok':>7} {'url%':>5} {'needle':>6} "
        f"{'noise':>6} {'links':>6} {'fake#':>5} {'dup':>4} {'fnc':>4} {'ms':>6}")


def totals(rows: list[dict]) -> dict:
    ok = [r for r in rows if not r["blocked"]]
    return {
        "out": sum(r["out"] for r in ok),
        "needles": sum(r["needles"] for r in ok), "needles_of": sum(r["needles_of"] for r in ok),
        "noise": sum(r["noise"] for r in ok), "noise_of": sum(r["noise_of"] for r in ok),
        "links": sum(r["links"] for r in ok), "links_of": sum(r["links_of"] for r in ok),
        "fake": sum(r["fake"] for r in ok), "dup": sum(r["dup"] for r in ok),
        "url_chars": sum(r["out"] * r["url_pct"] / 100 for r in ok),
        "ms_median": round(statistics.median(r["ms"] for r in ok), 1) if ok else 0,
        "bad_fences": sum(1 for r in ok if not r["fences_ok"]),
        "blocked_noted": sum(1 for r in rows if r["blocked"] and r["blocked_noted"]),
        "blocked": sum(1 for r in rows if r["blocked"]),
    }


def print_totals(t: dict) -> None:
    print(f"\ntotal text {t['out']:,} chars (~{t['out'] // 4:,} tokens), of which URLs "
          f"{t['url_chars']:,.0f}; needles {t['needles']}/{t['needles_of']}, noise "
          f"{t['noise']}/{t['noise_of']}, link needles {t['links']}/{t['links_of']}, "
          f"fake headings {t['fake']}, duplicate lines {t['dup']}, bad fences "
          f"{t['bad_fences']}, bot-checks noted {t['blocked_noted']}/{t['blocked']}, "
          f"median {t['ms_median']} ms/page")


def compare(rows: list[dict], base_path: Path) -> None:
    base = {r["id"]: r for r in json.loads(base_path.read_text())["rows"]}
    print(f"\n{'case':15} {'out before':>10} {'after':>8} {'Δ%':>6}  {'needles':>9} "
          f"{'noise':>9} {'links':>9} {'fake#':>9} {'ms':>11}")
    for r in rows:
        b = base.get(r["id"])
        if b is None:
            continue
        pct = 100 * (r["out"] - b["out"]) / max(1, b["out"])
        print(f"{r['id']:15} {b['out']:>10,} {r['out']:>8,} {pct:>+6.0f}  "
              f"{b['needles']:>3}→{r['needles']:<2}/{r['needles_of']:<2} "
              f"{b['noise']:>3}→{r['noise']:<2}/{r['noise_of']:<2} "
              f"{b['links']:>3}→{r['links']:<2}/{r['links_of']:<2} "
              f"{b['fake']:>4}→{r['fake']:<4} {b['ms']:>5}→{r['ms']:<5}")
    tb, ta = totals(list(base.values())), totals(rows)
    print(f"\ntotal text {tb['out']:,} → {ta['out']:,} chars "
          f"({100 * (ta['out'] - tb['out']) / max(1, tb['out']):+.0f}%), URLs "
          f"{tb['url_chars']:,.0f} → {ta['url_chars']:,.0f}, needles {tb['needles']} → "
          f"{ta['needles']}, noise {tb['noise']} → {ta['noise']}, links {tb['links']} → "
          f"{ta['links']}, fake headings {tb['fake']} → {ta['fake']}, median ms "
          f"{tb['ms_median']} → {ta['ms_median']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--snapshot", action="store_true", help="download the pages into the cache")
    ap.add_argument("--cases", help="comma-separated case ids (default: all)")
    ap.add_argument("--json", type=Path, help="write per-case results here")
    ap.add_argument("--compare", type=Path, help="baseline written earlier with --json")
    ap.add_argument("--show", help="print the extracted text of one case")
    a = ap.parse_args()

    cases = CASES
    if a.cases:
        want = set(a.cases.split(","))
        cases = [c for c in CASES if c.id in want]
    if a.snapshot:
        snapshot(cases)
        return 0
    if a.show:
        c = next((c for c in CASES if c.id == a.show), None)
        snap = c and load(c)
        if not snap:
            print(f"no snapshot for '{a.show}' — run with --snapshot first")
            return 1
        kind, text, links, notes = _decode(snap[0], snap[1], snap[2]["final_url"])
        print(f"[{kind}] notes={notes}\n{text}")
        if links:
            print(f"\n[{len(links)} links indexed]")
            for lk in links[:40]:
                print(f"  {'*' if lk.in_content else ' '} {lk.text[:60]} → {lk.url}")
        return 0

    rows = [r for c in cases if (r := measure(c)) is not None]
    if not rows:
        print("no snapshots — run with --snapshot first")
        return 1
    print(HEAD)
    for r in rows:
        print(_row(r))
    print_totals(totals(rows))
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"rows": rows, "totals": totals(rows)}, indent=2))
    if a.compare:
        compare(rows, a.compare)
    return 0


if __name__ == "__main__":
    sys.exit(main())
