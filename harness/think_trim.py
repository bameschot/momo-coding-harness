"""/think-keep's trim of the reasoning /think-history sends back: where it got to.

The model re-reads its own earlier reasoning on every later request of a tool
loop.  What it needs from it to carry on, rather than plan again from scratch
(what it does with /think-history off), is where it got to: the plan steps it
listed and the closing sentences, the decision behind the call.  The
deliberation before that is dropped, and a note says how much.

How much is kept grows with the square root of the reasoning's length, with a
floor: budget() = max(FLOOR, SCALE·√chars) × the /think-keep level's factor.
At lean, 1k chars keep ~380, 10k ~1,200, 30k ~2,100: a long thought keeps
more, but a smaller share, as deliberation grows faster than decisions.  Off
(the default) sends it back whole: a small model that loses its earlier
conclusions re-derives them, and can loop on reaching opposite ones.  Lean
was faster on Ornith-9B (reasoning sent back made it reason more); more and
most leave a larger model more of its own thinking.

The budget is filled with the plan steps first (up to half of it, whole
lines), then backwards from the end: whole paragraphs, the one where it runs
out cut to its last whole sentences.  Code that the tool call repeats becomes
a pointer to the call; other code is kept whole or not at all.  The last
sentence is always kept.  The stored transcript keeps the full reasoning;
only what is sent is shortened.

Lossy on purpose: a lossless trim (only what the context already holds, and
"Let me…" narration) took 1-9% off recorded reasoning, as most of it is
deliberation full of names and numbers.

trim() is a pure function of its inputs, so a step's trimmed reasoning stays
byte-identical on every later request: llama.cpp's prompt cache then misses
only once per step, at the step itself.  No language- or tool-specific rules.
"""
from __future__ import annotations

import math
import re

FLOOR = 300                 # chars kept at least
SCALE = 12                  # × √chars
KEEP_FACTORS = {"lean": 1, "more": 2, "most": 4}   # /think-keep's trim levels
KEEP_LEVELS = ("off",) + tuple(KEEP_FACTORS)        # off: no trim
MIN_RUN = 3                 # code lines the call repeats before they become a pointer
MIN_LINE = 4                # a shorter line ("}", "pass") does not count

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_STEP_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+\S")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=\S)")


def budget(chars: int, keep: str = "lean") -> int:
    """Chars of a thought of that length that are sent back."""
    return round(max(FLOOR, SCALE * math.sqrt(chars)) * KEEP_FACTORS.get(keep, 1))


def call_text(message: dict) -> tuple[str, list[tuple[str, str]]]:
    """(all the text of an assistant message, [(tool name, its arguments' text)])."""
    def strings(v):
        if isinstance(v, str):
            yield v
        elif isinstance(v, dict):
            for x in v.values():
                yield from strings(x)
        elif isinstance(v, list):
            for x in v:
                yield from strings(x)
    calls = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", tc)
        calls.append((fn.get("name") or "tool", "\n".join(strings(fn.get("arguments")))))
    return "\n".join([message.get("content") or ""] + [t for _, t in calls]), calls


def _paragraphs(lines: list[str]) -> list[tuple[int, int]]:
    """[(start, end)) of the paragraphs: blank-line separated, a code fence
    with its contents counting as one paragraph."""
    out, i = [], 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        j, fenced = i, False
        while j < len(lines) and (fenced or lines[j].strip()):
            if _FENCE_RE.match(lines[j]):
                fenced = not fenced
            j += 1
        out.append((i, j))
        i = j
    return out


def _point_at_call(block: list[str], text: str, calls: list[tuple[str, str]]) -> list[str]:
    """block with its code that the call repeats replaced by a pointer."""
    real = [ln for ln in block if len(ln.strip()) >= MIN_LINE and not _FENCE_RE.match(ln)]
    if len(real) < MIN_RUN or not all(ln.strip() in text for ln in real):
        return block
    best = max(calls, key=lambda c: sum(ln.strip() in c[1] for ln in real), default=None)
    return [f"[repeated in the {best[0]} call below]" if best else "[repeated in the reply below]"]


def _closing(prose: str, limit: int, force: bool) -> str:
    """The last whole sentences of prose within limit chars ("" if none fits);
    with force at least the last one, keeping its end when it is too long."""
    out, used = [], 0
    for s in reversed(_SENTENCE_RE.split(prose.strip())):
        if used + len(s) + 1 > limit and (out or not force):
            break
        out.insert(0, s)
        used += len(s) + 1
    text = " ".join(out)
    return text if len(text) <= limit else "…" + text[-limit:].split(" ", 1)[-1]


def _tail(lines, paras, limit, text, calls) -> tuple[list[str], int]:
    """(the kept paragraphs from the end within limit chars, the line where
    what was left out ends: plan steps before it can still be sent)."""
    kept, used = [], 0
    for a, b in reversed(paras):
        block = lines[a:b]
        if _FENCE_RE.match(block[0]):
            code = "\n".join(_point_at_call(block, text, calls))
            if kept and used + len(code) > limit:
                return kept, b
            kept.insert(0, code)                # never cut; the last one even when over
        else:
            whole = "\n".join(block)
            if used + len(whole) > limit:
                prose = " ".join(ln.strip() for ln in block if not _STEP_RE.match(ln))
                part = _closing(prose, limit - used, force=not kept) if prose else ""
                if part:
                    kept.insert(0, part)
                return kept, b
            kept.insert(0, whole)
        used += len(kept[0]) + 2
    return kept, 0


def trim(reasoning: str, message: dict, keep: str = "lean") -> str:
    """The reasoning to send back with message (the assistant message it led
    to): its plan steps and closing sentences within budget(), with a note on
    what was left out."""
    n = len(reasoning)
    limit = budget(n, keep)
    if n - limit < n // 4:                          # not worth a cache miss
        return reasoning
    text, calls = call_text(message)
    lines = reasoning.split("\n")
    paras = _paragraphs(lines)
    if not paras or (len(paras) == 1 and _FENCE_RE.match(lines[paras[0][0]])):
        return reasoning                            # nothing but code
    fenced, candidates = False, []                  # plan steps, outside code
    for i, ln in enumerate(lines):
        if _FENCE_RE.match(ln):
            fenced = not fenced
        elif not fenced and _STEP_RE.match(ln):
            candidates.append((i, ln.strip()))

    def steps_before(end: int) -> list[str]:
        out, used = [], 0
        for i, ln in candidates:
            if i < end and used + len(ln) <= limit // 2:
                out.append(ln)
                used += len(ln) + 1
        return out

    # The steps take their room first; once the tail is known, the steps
    # inside it are not repeated, so the tail is laid out again with what that
    # frees (it can only grow, so one more pass settles it).
    steps = steps_before(len(lines))
    tail, end = _tail(lines, paras, limit - sum(len(s) + 1 for s in steps), text, calls)
    if len(steps_before(end)) < len(steps):
        steps = steps_before(end)
        tail, end = _tail(lines, paras, limit - sum(len(s) + 1 for s in steps), text, calls)
        steps = steps_before(end)
    kept = "\n\n".join((["\n".join(steps)] if steps else []) + tail)
    left = n - len(kept)
    if left < n // 4:
        return reasoning
    note = f"[earlier reasoning not sent: {left:,} chars{', plan steps kept' if steps else ''}]"
    return note + "\n\n" + kept
