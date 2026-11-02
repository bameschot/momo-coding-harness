"""/think-budget: watching a reply's reasoning while it streams, and stopping it
when it runs long or goes round in circles.

/max-output-tokens caps a whole reply, reasoning included, but a reply that
thinks for 20k tokens before acting already costs minutes on a local model:
single replies of about that size were the capped runs of the multi-part
bench (evals/README.md, rounds 1-3), and several edit-suite runs hit the time
cap before making any edit.  Telling the model to think less does not work
(/cavecat's rounds 1-2: a 9B ignored "keep the reasoning" and verified by
tool call under "conclusions only"), so the harness watches instead.

Three trips on the reasoning stream (the "thinking" deltas):

- over budget: the reasoning passed the budget, in tokens as the harness
  estimates them (4 characters each).
- repeating: one line of reasoning came back REPEATS times, verbatim apart
  from case and spacing, within the last WINDOW characters.  That is the
  degenerate loop that once ran a reply to 125k tokens (llm/base.py).  Short
  lines ("Wait.", "Hmm, no.") recur in healthy reasoning and do not count;
  numbers are not normalised away, so tracing a loop step by step
  ("i = 1, total = 3") is not mistaken for one.
- compressing: the last RATIO_WINDOW characters compress to under MIN_RATIO
  of their size.  A loop of short lines ("Actually, I'll do it." 2,400 times
  in the story-mud run) never counts as a repeated line, and a code block
  written out 29 times has no single short line that repeats in the window;
  both compress to 0.005-0.09, while the healthy reasoning and code of that
  run stayed above 0.17.

The reply text (the "content" deltas) gets a watch of its own with only the
compressing trip (/think-budget reply-repeat): code repeats lines legitimately,
and the same run streamed three looping replies of 55-87k characters to the
output cap.

The harness then drops the stream and asks the model to act on what it has
(Harness._think_overrun).  A non-streamed reply has no deltas: there only
/max-output-tokens applies.
"""
from __future__ import annotations

import re
import zlib
from collections import Counter, deque

DEFAULT_BUDGET = 8_000   # reasoning tokens per reply before the watch stops it
MIN_BUDGET = 1_000       # /think-budget's lowest value: below it most replies would trip
REPEATS = 3              # one line this many times in the window is a repetition loop
WINDOW = 6_000           # chars of recent reasoning the repetition check looks at
MIN_LINE = 40            # a shorter line does not count towards a repetition
STOPS_PER_TURN = 2       # watchdog stops in a row without a tool call; after that a reply runs to the cap
RATIO_WINDOW = 24_000    # chars of recent text the compression check looks at
RATIO_MIN_CHARS = 8_000  # the compression check starts once this much has streamed
RATIO_STEP = 1_000       # chars between two compression checks
MIN_RATIO = 0.10         # compressed size / size below this is a repetition loop

_RX_SPLIT = re.compile(r"\n+|(?<=[.!?])\s+")
_RX_SPACE = re.compile(r"\s+")


def compress_ratio(text: str) -> float:
    """Compressed size / size of text (1.0 for empty text): a looping text
    compresses to a few percent, healthy prose and code to 0.2-0.5."""
    data = text.encode("utf-8", "replace")
    return len(zlib.compress(data)) / len(data) if data else 1.0


def looping(text: str) -> bool:
    """Whether a finished text reads as a repetition loop: long enough for the
    compression check, and its last RATIO_WINDOW characters compress below
    MIN_RATIO."""
    return len(text) >= RATIO_MIN_CHARS and compress_ratio(text[-RATIO_WINDOW:]) < MIN_RATIO


def most_repeated_line(text: str) -> str:
    """The line (or sentence) that occurs most often in text, for quoting a loop."""
    counts = Counter(p.strip() for p in _RX_SPLIT.split(text) if p.strip())
    return counts.most_common(1)[0][0] if counts else text.strip()[-80:]


class Overrun(Exception):
    """The watch stopped a reply.  kind: "budget" or "repeat"; reasoning: what
    had streamed so far; line: the repeated line (kind "repeat"); stream:
    "thinking" or "reply", the stream that tripped."""

    def __init__(self, kind: str, reasoning: str, line: str = "", stream: str = "thinking"):
        super().__init__(kind)
        self.kind, self.reasoning, self.line, self.stream = kind, reasoning, line, stream

    @property
    def tokens(self) -> int:
        return len(self.reasoning) // 4

    def describe(self) -> str:
        if self.stream == "reply":
            return "Reply stopped: it kept repeating itself"
        if self.kind == "repeat":
            return "Reasoning stopped: it kept repeating itself"
        return f"Reasoning stopped after about {self.tokens:,} tokens"


class ThinkWatch:
    """Feed it each reasoning delta; it raises Overrun when a trip fires.
    budget 0 turns the budget trip off, repeat=False the repetition trips.
    stream="reply" watches reply text: only the compression trip applies."""

    def __init__(self, budget: int = DEFAULT_BUDGET, repeat: bool = True,
                 stream: str = "thinking"):
        self.budget, self.repeat, self.stream = budget, repeat, stream
        self._parts: list[str] = []
        self._chars = 0
        self._next_ratio = RATIO_MIN_CHARS          # chars streamed at the next compression check
        self._pending = ""                          # the line still streaming in
        self._lines: deque[tuple[int, str]] = deque()   # (end offset, normalised line)
        self._counts: Counter[str] = Counter()

    @property
    def active(self) -> bool:
        return bool(self.budget) or self.repeat

    def text(self) -> str:
        return "".join(self._parts)

    def feed(self, delta: str) -> None:
        if not delta:
            return
        self._parts.append(delta)
        self._chars += len(delta)
        if self.budget and self._chars // 4 > self.budget:
            raise Overrun("budget", self.text(), stream=self.stream)
        if self.repeat:
            if self.stream == "thinking":
                self._scan(delta)
            if self._chars >= self._next_ratio:
                self._next_ratio = self._chars + RATIO_STEP
                text = self.text()
                if looping(text):
                    raise Overrun("repeat", text, most_repeated_line(text[-RATIO_WINDOW:]),
                                  stream=self.stream)

    def _scan(self, delta: str) -> None:
        pieces = _RX_SPLIT.split(self._pending + delta)
        self._pending = pieces.pop()               # not finished yet
        while self._lines and self._lines[0][0] < self._chars - WINDOW:
            _, old = self._lines.popleft()
            self._counts[old] -= 1
            if not self._counts[old]:
                del self._counts[old]
        for piece in pieces:
            line = _RX_SPACE.sub(" ", piece).strip().lower()
            if len(line) < MIN_LINE:
                continue
            self._lines.append((self._chars, line))
            self._counts[line] += 1
            if self._counts[line] >= REPEATS:
                raise Overrun("repeat", self.text(), piece.strip())
