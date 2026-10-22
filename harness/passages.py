"""Blocks and BM25 passage ranking over extracted page text — stdlib only.

Shared by web_search (read=true returns the best passages of the top pages)
and fetch_url (find= falls back to the best passage when its words occur
nowhere verbatim).  The text is html_text's output: '#' headings, ``` fences,
blank lines between blocks.
"""
from __future__ import annotations

import math
import re

_STOP = set("a an and are as at be by can do does for from how i in is it my of on or "
            "the this that to use using what when where which why with you your".split())


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9_]+(?:[.\-][a-z0-9_]+)*", text.lower())
            if len(t) > 1 and t not in _STOP]


def blocks_at(text: str) -> list[tuple[int, str, str]]:
    """(start offset, heading, block) triples: paragraphs and whole fenced
    code blocks, each under the nearest heading above it."""
    out, buf, heading, in_fence = [], [], "", False
    start, pos = 0, 0

    def flush():
        block = "\n".join(buf).strip()
        if block:
            out.append((start, heading, block))
        buf.clear()
    for line in text.split("\n"):
        here, pos = pos, pos + len(line) + 1
        if not buf:
            start = here
        if line.startswith("```"):
            buf.append(line)
            if in_fence:
                flush()
            in_fence = not in_fence
            continue
        if in_fence:
            buf.append(line)
            continue
        if re.match(r"#{1,6} \S", line):
            flush()
            heading = line.lstrip("#").strip()
            continue
        if not line.strip():
            flush()
            continue
        buf.append(line)
    flush()
    return out


def blocks(text: str) -> list[tuple[str, str]]:
    """(heading, block) pairs, as blocks_at() without the offsets."""
    return [(h, b) for _, h, b in blocks_at(text)]


def rank_passages(pages: list[tuple[str, str]], query: str) -> list[tuple[float, int, int, str, str]]:
    """BM25 over every block of every page: (score, page index, block index,
    heading, block), best first.  Only blocks sharing a term with the query."""
    q = set(tokens(query))
    docs = []
    for pi, (_, text) in enumerate(pages):
        for bi, (heading, block) in enumerate(blocks(text)):
            docs.append((pi, bi, heading, block, tokens(heading + " " + block)))
    if not q or not docs:
        return []
    avg = sum(len(d[4]) for d in docs) / len(docs) or 1
    df = {t: sum(1 for d in docs if t in d[4]) for t in q}
    k1, b = 1.2, 0.75
    scored = []
    for pi, bi, heading, block, toks in docs:
        score = 0.0
        for t in q:
            tf = toks.count(t)
            if not tf:
                continue
            idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(toks) / avg))
        if score > 0:
            scored.append((score, pi, bi, heading, block))
    scored.sort(key=lambda s: -s[0])
    return scored
