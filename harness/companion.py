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
# key: (mode, is_thinking)  value: list of strings each ≤ BUBBLE_MAX (40) visible chars
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

# Longer lines (26–40 chars), mixed into the pools above so momo sometimes says a
# whole little sentence instead of a two-word quip.
_SPEECH_TEXTS_LONG: dict[tuple[str, bool], list[str]] = {
    ("coding",  False): [
        "< tests green, time for a nap?",
        "< that diff looks tidy, purrr",
        "< remember to commit before lunch!",
        "< i sat on the keyboard. sorry~",
        "< small functions are easier to chase",
        "< a failing test is just a clue!",
        "< did we check the edge cases?",
        "< that variable name is too long",
        "< one more refactor, then treats?",
        "< ship it! (after the tests pass)",
    ],
    ("coding",  True): [
        "< following that stack trace...",
        "< reading the file, line by line",
        "< editing carefully, no typos pls",
        "< running the tests, fingers crossed",
        "< hmm, where does this get called?",
        "< grepping for the culprit...",
        "< waiting on the compiler, purrr",
        "< *sniffs the diff suspiciously*",
        "< almost done with this change!",
        "< chasing a sneaky off-by-one...",
    ],
    ("design",  False): [
        "< that boundary looks nice and clean",
        "< one module, one job. purrr~",
        "< could this be simpler, maybe?",
        "< draw the data flow first!",
        "< i like where this is heading",
        "< who owns this state, though?",
        "< the interface is the contract!",
        "< less coupling, more napping",
        "< let's name things before we build",
        "< good design is lazy design~",
    ],
    ("design",  True): [
        "< weighing the trade-offs...",
        "< mapping out the modules, hmm",
        "< reading how it fits together",
        "< looking for the seams in this...",
        "< sketching the boundaries now",
        "< which way do dependencies go?",
        "< thinking about the edge cases",
        "< following the data around...",
        "< untangling the layers, purrr",
        "< hmm, is this the simplest shape?",
    ],
    ("chat",    False): [
        "< ooh, tell me more about that!",
        "< that makes a lot of sense, purrr",
        "< i was thinking the same thing~",
        "< interesting! what happens next?",
        "< you explain things so nicely",
        "< i'm all ears (both of them)",
        "< that's a clever way to see it",
        "< mew! i learned something new",
        "< go on, i'm listening closely",
        "< good question, let me think~",
    ],
    ("chat",    True): [
        "< reading up on that for you...",
        "< looking through the code, hmm",
        "< let me check that real quick",
        "< following the clue around...",
        "< digging through the files...",
        "< ooh, found something relevant!",
        "< cross-checking my answer, purrr",
        "< one moment, putting it together",
        "< hmm, this part is interesting",
        "< almost have an answer for you!",
    ],
    ("plan",    False): [
        "< a good plan is half the work!",
        "< read the plan, then say y~",
        "< small steps are easy to check",
        "< does every step have a test?",
        "< happy with the plan? purrr",
        "< tick, tick, all boxes [x]!",
        "< we can revise it if needed",
        "< the goal looks clear to me!",
        "< measure twice, cut once, mew",
        "< what should we plan next?",
    ],
    ("plan",    True): [
        "< ticking off step two, purrr",
        "< investigating before planning...",
        "< writing the steps down now",
        "< staying on track, one step at a time",
        "< verifying this step works...",
        "< reproducing the bug first!",
        "< following the plan closely",
        "< checking the box when it's done",
        "< running tests for this step...",
        "< nearly through the checklist!",
    ],
    ("momo",   False): [
        "< the sunny spot moved again, brb",
        "< i'm proud of you, you know that?",
        "< can we take a cuddle break soon?",
        "< a bird! outside! look! *chirps*",
        "< i knocked your pen off the desk",
        "< you're doing great, keep going!",
        "< zoomies incoming, clear the desk!",
        "< *kneads your sleeve* purrrrr",
        "< is it treat o'clock yet?",
        "< i'll guard the keyboard for you",
    ],
    ("momo",   True): [
        "< *sniffs the file very carefully*",
        "< ooh, what's in this folder?",
        "< reading... this bit is fun!",
        "< hold on, i'm on the trail!",
        "< *pounces on a bug* got it?",
        "< one sec, i'm very busy cat",
        "< checking everything twice, mew",
        "< following my nose through here",
        "< hmm hmm, almost figured it out",
        "< *stares intently at the code*",
    ],
}
for _key, _lines in _SPEECH_TEXTS_LONG.items():
    _SPEECH_TEXTS[_key] = _SPEECH_TEXTS[_key] + _lines

# ── bubble layout (shared by the TUI and, mirrored, the web UI) ──────────────
# The speech bubble sits on the cat's head row, beside the frame: on the right
# ("< text") when momo faces right, on the left ("text >") when it faces left.
BUBBLE_MAX = 40   # visible chars in a bubble, including the "< " prefix
CAT_W = 8         # visible width of every frame line


def walk_max_x(cols: int) -> int:
    """Rightmost walk position: leaves room for a full bubble on momo's right, so a
    bubble always fits on at least one side (see bubble_dir)."""
    return max(0, cols - 2 - CAT_W - (BUBBLE_MAX + 2))


def _fits(cx: int, cols: int, direction: int, n: int) -> bool:
    if direction < 0:
        return cx >= n                          # drawn from cx + 1 - n - 1 >= 0
    return cx + 1 + CAT_W + 1 + n < cols - 1   # must end before the last column


def bubble_dir(cx: int, cols: int, direction: int, n: int) -> int | None:
    """Which way momo should face to say an n-char line at walk position cx: its
    current direction if the line fits there, else the other side, else None."""
    for d in (direction, -direction):
        if _fits(cx, cols, d, n):
            return d
    return None


def bubble_room(cx: int, cols: int) -> int:
    """The longest line that fits on either side of momo at walk position cx."""
    return max(0, cx, cols - 2 - (cx + 1 + CAT_W + 1))

# ── idle recap lines ──────────────────────────────────────────────────────────
# Model-written recaps must fit the same bubble as the canned lines above:
# ≤ BUBBLE_MAX visible chars including the "< " prefix, single-width characters only.
_BUBBLE_BODY = BUBBLE_MAX - 2
MAX_RECAP_LINES = 5   # recap lines one recap call may produce


def _clean_line(line: str) -> str:
    import re
    import unicodedata
    s = unicodedata.normalize("NFKC", line).strip()
    prev = None
    while prev != s:                                          # bullets, numbering, "< "
        prev, s = s, re.sub(r"^(?:[-*+•>]\s+|\d+[.)]\s+|<\s+)", "", s)
    # Drop markdown bold and code marks, but keep single *actions* ("*kneads*")
    # and underscores (they are part of file names like my_file.py).
    s = re.sub(r"\*\*|__|`", "", s)
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

    def pick(self, now: float, max_len: int = BUBBLE_MAX, rng=random) -> str | None:
        """A recap line of at most max_len chars (what fits beside momo right now), or
        None. A queued line that doesn't fit stays queued for a roomier spot."""
        line = next((q for q in self._queue if len(q) <= max_len), None)
        if line is not None:
            self._queue.remove(line)
        else:
            if rng.random() >= _RECAP_REUSE_CHANCE:
                return None
            pool = [line for line in self.lines[-_RECAP_REUSE_POOL:]
                    if len(line) <= max_len
                    and now - self._last_shown.get(line, float("-inf")) >= _RECAP_REPEAT_SECS]
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
