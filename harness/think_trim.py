"""/cavecat's trim of the reasoning /think-history sends back: the decision only.

The model re-reads its own earlier reasoning on every later request of a tool
loop.  What it needs from it to carry on, rather than plan again from scratch
(what it does with /think-history off), is where it got to: the plan steps it
listed and the last paragraph, the decision behind the call.  The deliberation
before that is dropped, and a note says how much: on recorded sessions the
last paragraph is ~6% of the reasoning.  Code in the kept part that the tool
call repeats is replaced by a pointer to the call.  The stored transcript
keeps the full reasoning; only what is sent is shortened.

Lossy on purpose: a lossless trim (only what the context already holds, and
"Let me…" narration) took 1-9% off recorded reasoning, as most of it is
deliberation full of names and numbers.

trim() is a pure function of its inputs, so a step's trimmed reasoning stays
byte-identical on every later request: llama.cpp's prompt cache then misses
only once per step, at the step itself.  No language- or tool-specific rules.
"""
from __future__ import annotations

import re

MIN_CHARS = 400             # shorter reasoning is sent as it is
MAX_DECISION = 800          # chars of the closing paragraph(s) kept, from the end
MAX_STEPS = 1200            # chars of plan-step lines kept
MIN_RUN = 3                 # code lines the call repeats before they become a pointer
MIN_LINE = 4                # a shorter line ("}", "pass") does not count

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_STEP_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+\S")


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


def trim(reasoning: str, message: dict) -> str:
    """The reasoning to send back with message (the assistant message it led
    to): its plan steps and closing decision, with a note on what was left out."""
    if len(reasoning) < MIN_CHARS:
        return reasoning
    text, calls = call_text(message)
    lines = reasoning.split("\n")
    paras = _paragraphs(lines)
    if len(paras) < 2:
        return reasoning
    # The decision: the last paragraph, and the one before it when the last is
    # only code (the draft of the call, typically after "I'll write it:").
    keep = [paras[-1]]
    if _FENCE_RE.match(lines[paras[-1][0]]) and len(paras) > 1:
        keep.insert(0, paras[-2])
    decision: list[str] = []
    for a, b in keep:
        block = lines[a:b]
        decision += (_point_at_call(block, text, calls) if _FENCE_RE.match(block[0]) else block)
        decision.append("")
    decision_text = "\n".join(decision).strip()
    if len(decision_text) > MAX_DECISION:
        decision_text = "…" + decision_text[-MAX_DECISION:].split(" ", 1)[-1]
    # Plan steps listed before the decision, outside code.
    first = keep[0][0]
    steps, used, fenced = [], 0, False
    for ln in lines[:first]:
        if _FENCE_RE.match(ln):
            fenced = not fenced
        elif not fenced and _STEP_RE.match(ln) and used + len(ln) <= MAX_STEPS:
            steps.append(ln)
            used += len(ln) + 1
    left = len(reasoning) - len(decision_text) - used
    if left < len(reasoning) // 4:                  # not worth a cache miss
        return reasoning
    note = f"[earlier reasoning not sent: {left:,} chars{', plan steps kept' if steps else ''}]"
    return "\n\n".join([note] + (["\n".join(steps)] if steps else []) + [decision_text])
