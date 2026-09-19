"""momo companion art and speech lines, shared by the TUI and the web UI."""
from __future__ import annotations

import random

_MOMO_WR = [   # walking right — two alternating leg frames
    ["\\    /\\ ", " )  ( ')", "( ¯¯  ) ", " /\\/\\/\\ "],
    ["\\    /\\ ", " )  ( ')", "( ¯¯  ) ", " \\/\\/\\/ "],
]
_MOMO_WL = [   # walking left — two alternating leg frames
    [" /\\   \\ ", "(' )  ( ", "(  ¯¯ ) ", " /\\/\\/\\ "],
    [" /\\   \\ ", "(' )  ( ", "(  ¯¯ ) ", " \\/\\/\\/ "],
]
_MOMO_SIT = [  # sitting — normal, blink
    ["\\    /\\ ", " )  ( ')", "(  /  ) ", " \\(__)| "],
    ["\\    /\\ ", " )  ( -)", "(  /  ) ", " \\(__)| "],
]
_MOMO_SIT_L = [  # sitting facing left — normal, blink
    [" /\\   \\ ", "(' )  ( ", "(  \\  ) ", "|(__)/ "],
    [" /\\   \\ ", "(- )  ( ", "(  \\  ) ", "|(__)/ "],
]
_MOMO_WR_BLINK = ["\\    /\\ ", " )  ( -)", "(  ¯  ) ", " /\\/\\/\\ "]  # walking-right blink
_MOMO_WL_BLINK = [" /\\   \\ ", "(' )  ( ", "(  ¯  ) ", " /\\/\\/\\ "]  # walking-left blink
# key: (mode, is_thinking)  value: list of strings each ≤ 25 visible chars
_SPEECH_TEXTS: dict[tuple[str, bool], list[str]] = {
    ("coding",  False): [
        "< mew~", "< purrr", "< found a bug!",
        "< git commit!", "< tests pass?", "< grep is love",
        "< ship it!", "< refactor?", "< code review!",
        "< off by one!", "< vim or emacs?",
        "< lgtm!", "< rubber duck?",
        "< dry it up!", "< lint errors!",
        "< push to main?", "< branch first!",
        "< stash it!", "< rebase time!",
        "< merge conflict?", "< squash it!",
        "< todo fixme!", "< purrr~",
        "< hot reload?", "< benchmarks!",
        "< profiler!", "< coverage!",
        "< make clean?", "< chmod +x!",
        "< mew mew~",
    ],
    ("coding",  True): [
        "< mew?", "< compiling...", "< stack trace!",
        "< segfault...", "< null pointer!",
        "< linker error!", "< undefined!",
        "< syntax error?", "< type mismatch",
        "< core dumped!", "< infinite loop?",
        "< race condition?", "< deadlock...",
        "< heap overflow!", "< bus error!",
        "< stack overflow!", "< exception!",
        "< unhandled err!", "< oom killed...",
        "< traceback!", "< mew...",
        "< assertion fail", "< divide by zero?",
        "< abi mismatch!", "< memory leak...",
        "< watchdog!", "< panic!",
        "< signal caught!", "< debugger...",
        "< step through?",
    ],
    ("design",  False): [
        "< mew~", "< nice api!", "< solid design!",
        "< decouple it!", "< dry principle",
        "< event driven?", "< schema first!",
        "< purrr", "< interface?", "< abstract it!",
        "< single concern", "< clean code!",
        "< patterns!", "< microservices?",
        "< idempotent!", "< immutable!",
        "< hexagonal?", "< monolith?",
        "< async!", "< solid!",
        "< loose coupling", "< extension pts?",
        "< separation?", "< dependency inj?",
        "< pure functions?", "< state machine?",
        "< event sourcing?", "< cqrs?",
        "< mew mew~", "< purrr~",
    ],
    ("design",  True): [
        "< mew mew mew", "< hmm...", "< thinking hard",
        "< trade-offs...", "< let me think",
        "< edge cases!", "< iterate!",
        "< coupling...", "< dependency?",
        "< mew?", "< complexity...",
        "< layering...", "< contracts!",
        "< invariants...", "< mew mew",
        "< abstractions?", "< modelling...",
        "< boundaries?", "< purrr...",
        "< cohesion?", "< simplify...",
        "< risk analysis?", "< first principles",
        "< tech debt...", "< scope creep?",
        "< bottleneck?", "< scalability?",
        "< feedback loop?", "< mew~",
        "< purrr~",
    ],
    ("chat",    False): [
        "< tell me more!", "< interesting!", "< got it!",
        "< ooh!", "< makes sense!", "< mew~",
        "< say more!", "< purrr", "< keep going!",
        "< I see!", "< right right!", "< nice!",
        "< elaborate?", "< and then?", "< really?",
        "< noted!", "< curious!", "< for sure!",
        "< neat!", "< love it!", "< mhm!",
        "< yep!", "< ah ha!", "< go on!",
        "< understood!", "< mew mew~", "< clever!",
        "< fascinating!", "< ok ok!", "< purrr~",
    ],
    ("chat",    True): [
        "< reading...", "< let me check", "< searching...",
        "< hmm...", "< found it!", "< scanning...",
        "< parsing...", "< mew?", "< cross-checking",
        "< grepping...", "< hold on...", "< one sec...",
        "< digging in...", "< inspecting!", "< ah interesting",
        "< found a ref", "< following up", "< tracing it...",
        "< mew mew?", "< mapping it...", "< connecting dots",
        "< hmm hmm...", "< checking...", "< pattern match!",
        "< got a clue!", "< narrowing...", "< almost there",
        "< verifying...", "< cross ref...", "< purrr...",
    ],
    ("plan",    False): [
        "< what's the plan?", "< mew~", "< step by step!",
        "< checklist!", "< purrr", "< plan approved?",
        "< one step at a time", "< ready when you are", "< looks solid!",
        "< tick tick!", "< mew mew~", "< measure twice",
        "< cut once!", "< y to run it!", "< edit the plan?",
        "< any feedback?", "< read it first!", "< good goal!",
        "< small steps!", "< tests last?", "< /plan run?",
        "< plan saved!", "< approve pls?", "< nice & tidy!",
        "< all boxes [x]!", "< plan complete!", "< what's next?",
        "< bug or feature?", "< tell me more!", "< purrr~",
    ],
    ("plan",    True): [
        "< investigating...", "< sniff sniff", "< tracing...",
        "< step done?", "< checking box!", "< following plan",
        "< hmm...", "< next step!", "< reproducing...",
        "< mapping it...", "< verifying...", "< purrr...",
        "< root cause?", "< found the bug!", "< reading code...",
        "< grepping...", "< writing steps...", "< [~] in progress",
        "< ticking [x]!", "< which step now?", "< stay on track!",
        "< no step skip!", "< complete_step!", "< revise plan?",
        "< running tests...", "< almost done!", "< hmm hmm...",
        "< one more step", "< mew?", "< *checks list*",
    ],
    ("momo",   False): [
        "< mew~", "< purrr~", "< hi there!",
        "< whatcha doing?", "< ooh!", "< shiny!",
        "< sniff sniff", "< found a bug?", "< cuddle break?",
        "< mew mew~", "< sunny spot!", "< outside?",
        "< zoomies!", "< nap time?", "< treat?",
        "< bird outside!", "< *chirps*", "< brrp!",
        "< purrr purrr", "< headbutt!", "< mew!",
        "< you okay?", "< proud of you!", "< good job!",
        "< mew mew mew", "< *kneads*", "< snooze...",
        "< i'm here!", "< tell me more!", "< oh no!",
        "< mew~",
    ],
    ("momo",   True): [
        "< reading...", "< sniff sniff", "< hmm...",
        "< looking...", "< mew?", "< found it?",
        "< scanning...", "< curious...", "< one sec...",
        "< *stares*", "< parsing...", "< mew mew?",
        "< digging in!", "< hold on...", "< oh interesting",
        "< following...", "< tracing...", "< almost!",
        "< mew...", "< inspecting!", "< got a clue!",
        "< cross ref...", "< hmm hmm", "< checking...",
        "< purrr...", "< nearly there", "< ah!",
        "< verifying...", "< *sniffs file*", "< mew!",
    ],
}
_SPEECH_TEXTS_DEFAULT = ["< mew~", "< purrr", "< mew mew"]  # fallback for unknown modes

# ── idle recap lines ──────────────────────────────────────────────────────────
# Model-written recaps must fit the same bubble as the canned lines above:
# ≤ 25 visible chars including the "< " prefix, single-width characters only.
_BUBBLE_MAX = 25
_BUBBLE_BODY = _BUBBLE_MAX - 2
MAX_RECAP_LINES = 5   # recap lines one recap call may produce


def _clean_line(line: str) -> str:
    import re
    import unicodedata
    s = unicodedata.normalize("NFKC", line).strip()
    prev = None
    while prev != s:                                          # bullets, numbering, "< "
        prev, s = s, re.sub(r"^(?:[-*+•>]\s+|\d+[.)]\s+|<\s+)", "", s)
    whole_action = bool(re.fullmatch(r"\*[^*]+\*", s))       # "*kneads*" stays as is
    if not whole_action:
        s = re.sub(r"[*_`]+", "", s)
    s = s.strip().strip("\"'").strip()
    # Printable ASCII plus the single-width ellipsis; drops emoji / wide chars.
    s = "".join(c for c in s if 32 <= ord(c) < 127 or c == "…")
    return " ".join(s.split())


def _fit(s: str) -> str:
    if len(s) <= _BUBBLE_BODY:
        return s
    cut = s[:_BUBBLE_BODY - 1]            # leave room for the ellipsis
    if s[len(cut)] != " ":                 # cut landed mid-word: drop the partial word
        if " " not in cut:
            return ""                      # one long word — can't fit it nicely
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "…"


_RECAP_REPEAT_SECS = 180    # the same recap line is never repeated within this window
_RECAP_REUSE_CHANCE = 0.25  # once new recaps are spoken: chance a bubble reuses an old one
_RECAP_REUSE_POOL = 5       # ... picked from this many most recent lines


class RecapPicker:
    """Chooses momo's recap bubbles (mirrored in the web UI's app.js).

    New recap lines take preference: each is spoken once, in order, before any
    canned line. After that an older recap only comes back occasionally, and never
    the same line within _RECAP_REPEAT_SECS — otherwise pick() returns None and the
    caller uses a canned line."""

    def __init__(self):
        self.lines: list[str] = []
        self._queue: list[str] = []            # new lines not spoken yet
        self._last_shown: dict[str, float] = {}

    def update(self, lines: list[str]):
        new = [line for line in lines[-MAX_RECAP_LINES:]
               if line not in self.lines and line not in self._queue]
        self._queue = [line for line in self._queue + new if line in lines]
        self.lines = list(lines)

    def pick(self, now: float, rng=random) -> str | None:
        if self._queue:
            line = self._queue.pop(0)
        else:
            if rng.random() >= _RECAP_REUSE_CHANCE:
                return None
            pool = [line for line in self.lines[-_RECAP_REUSE_POOL:]
                    if now - self._last_shown.get(line, float("-inf")) >= _RECAP_REPEAT_SECS]
            if not pool:
                return None
            line = rng.choice(pool)
        self._last_shown[line] = now
        return line


def fit_bubble(raw: str) -> list[str]:
    """Turn a model reply into ≤ MAX_RECAP_LINES speech-bubble lines (without the "< " prefix),
    each short enough to fit the companion bubble in the TUI and the web UI."""
    out: list[str] = []
    for line in (raw or "").splitlines():
        s = _fit(_clean_line(line))
        if s and s.lower() not in (o.lower() for o in out):
            out.append(s)
        if len(out) >= MAX_RECAP_LINES:
            break
    return out
