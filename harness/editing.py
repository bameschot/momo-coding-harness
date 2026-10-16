"""The file-editing tools — edit_file, replace_symbol, write_file, append_to_file
— and what they tell the model about the result.

The extras come in four groups, named in `edit_features` (all on by default;
dispatch injects the session's set, so a group can be switched off and its
effect measured — evals/run_evals.py --edit-features):

  feedback  a successful edit shows the changed lines with their new line
            numbers and the definition they are in, and warns when the file no
            longer parses, or still does not where the edit touched it (the
            edit stays written); an edit that changes nothing says so
  match     old_string is also found when the block's line count changes or
            only quotes/spacing differ; errors list each occurrence (with the
            definition it is in) or show the nearest block
  symbol    replace_symbol: rewrite a whole definition by name (an edit_file
            that copied most of a definition points at it)
  checks    a removed or renamed definition, or a changed signature, lists the
            uses still to update — from the index, or without it from a
            bounded scan that says when it stopped early; old_string found in
            another file says which

With every group off the tools behave exactly as before they existed.
"""
import ast
import difflib
import json
import re
import time
import warnings
from pathlib import Path

from . import code_index, code_nav
from .paths import safe_path, split_lines

EDIT_FEATURES = frozenset({"feedback", "match", "symbol", "checks", "batch"})

_CONTEXT = 3              # unchanged lines shown around an edit
_SNIPPET_MAX = 40         # a longer change shows its two ends
_MAX_LINES_LISTED = 20    # replace_all: changed lines named
_MAX_OCCURRENCES = 10
_MAX_USES_SHOWN = 8
_MAX_CHECKED_DEFS = 3
_SIBLING_FILES = 200      # wrong-file check without the index: one directory, bounded
_SIBLING_BYTES = 1_000_000
_HINT_MAX_LINES = 20000   # the nearest-block search is skipped on bigger files
_TAB_WIDTH = 4
_STILL_BROKEN_NEAR = 2    # an error this close to the changed lines is the edit's to fix
_NUDGE_MIN_LINES = 10     # an edit_file this long over one definition suggests replace_symbol
_NUDGE_SHARE = 0.8
_SCAN_SECONDS = 1.0       # leftover-use scan without the index: wall-clock budget
_SCAN_FILES = 5000        # … and files listed
_FILE_LINES_SHOWN = 6     # edit_files: changed lines listed per file


# ── reading and writing ──────────────────────────────────────────────────────

class _File:
    """A text file as the edit tools see it: LF line endings, written back with
    the file's own.  A CRLF file is matched as LF (old_string from the model
    has LF) and written back as CRLF; mixed endings are matched as they are."""

    def __init__(self, p: Path, text: str):
        self.p = p
        self.crlf = "\r\n" in text and text.count("\r\n") == text.count("\n")
        self.text = text.replace("\r\n", "\n") if self.crlf else text

    def lf(self, s: str) -> str:
        return s.replace("\r\n", "\n") if self.crlf else s

    def save(self, text: str) -> str:
        """'' when written, else an ERROR."""
        try:
            self.p.write_bytes((text.replace("\n", "\r\n") if self.crlf else text).encode("utf-8"))
        except OSError as e:
            return f"ERROR: {e}"
        return ""


def _load(path: str, workdir: Path, tool: str) -> _File | str:
    p = safe_path(path, workdir)
    if isinstance(p, str):
        return p
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except OSError as e:
        return f"ERROR: {e}"
    # Decoding with errors="replace" and writing back would turn every non-UTF-8
    # byte in the file into U+FFFD, not just the edited text.
    try:
        return _File(p, raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        return (f"ERROR: {path} is not valid UTF-8 (byte {e.start}), so {tool} cannot "
                f"change it without corrupting it. Nothing was written.")


def _size_note(content: str) -> str:
    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return f"{lines} line{'s' if lines != 1 else ''}, {len(content.encode('utf-8'))} bytes"


# ── matching old_string ──────────────────────────────────────────────────────

# Leading "  12: " line-number prefix as emitted by read_file — models often copy
# it into old_string even though it is not part of the file.
_LINENO_PREFIX = re.compile(r'^\s*(\d+):(?:\s|$)')


def _lineno_start(s: str) -> int | None:
    """The first line number when every non-blank line of s carries read_file's
    prefix, numbered consecutively; else None.  Real text like a dict entry
    `    1: "one",` or a YAML `  200: OK` rarely passes that."""
    first = None
    for i, ln in enumerate(s.split("\n")):
        if not ln.strip():
            continue
        m = _LINENO_PREFIX.match(ln)
        if m is None:
            return None
        n = int(m.group(1)) - i
        if first is None:
            first = n
        elif n != first:
            return None
    return first


def _strip_lineno_prefixes(s: str, start: int | None = None) -> str:
    """s without read_file's line-number prefixes, or s unchanged when they are
    not there — or, with start, when they do not begin at that line (new_string
    copied from the same read as old_string starts where it does)."""
    first = _lineno_start(s)
    if first is None or (start is not None and first != start):
        return s
    return "\n".join(_LINENO_PREFIX.sub("", ln, count=1) for ln in s.split("\n"))


def _leading_ws(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _tolerant_replace(content: str, old_string: str, new_string: str) -> str | None:
    """Whitespace-tolerant block replacement. Matches old_string against the file
    line-by-line ignoring each line's leading/trailing whitespace, and only acts when
    exactly one block matches (so it can never edit the wrong location).

    Requires a 1:1 line edit (new_string has the same number of lines as old_string)
    so each new line inherits the matched file line's actual leading whitespace — this
    fixes the common case where the model reproduced the code but with wrong or missing
    indentation.  An indentation change between an old and a new line is applied on
    top of the file's indentation.  Returns the new file content, or None if there
    is no safe unique match."""
    file_lines = split_lines(content, keepends=True)
    old_lines = old_string.strip("\n").split("\n")
    new_lines = new_string.strip("\n").split("\n")
    k = len(old_lines)
    if k > len(file_lines) or len(new_lines) != k:
        return None
    old_sig = [ln.strip() for ln in old_lines]
    file_sig = [ln.strip() for ln in file_lines]
    matches = [i for i in range(len(file_lines) - k + 1) if file_sig[i:i + k] == old_sig]
    if len(matches) != 1:
        return None
    i0 = matches[0]
    rebuilt = []
    for j in range(k):
        raw = file_lines[i0 + j]
        nl = raw[len(raw.rstrip("\r\n")):]        # preserve the original line ending
        indent = _leading_ws(raw)                 # transfer the file's real indentation
        old_ws, new_ws = _leading_ws(old_lines[j]), _leading_ws(new_lines[j])
        if new_ws.startswith(old_ws):             # the edit indents this line further
            indent += new_ws[len(old_ws):]
        elif old_ws.startswith(new_ws):           # the edit dedents it
            indent = indent[:max(0, len(indent) - (len(old_ws) - len(new_ws)))]
        code = new_lines[j].strip()
        rebuilt.append(indent + code + nl if code else nl)
    return "".join(file_lines[:i0]) + "".join(rebuilt) + "".join(file_lines[i0 + k:])


_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "“": '"',
                         "”": '"', "„": '"', " ": " ", " ": " ",
                         " ": " "})


def _ws_key(line: str) -> str:
    return line.strip()


def _norm_key(line: str) -> str:
    """A line with curly quotes made straight and all whitespace dropped:
    `foo(a,b)` and `foo(a, b)` are the same line to this key."""
    return "".join(line.translate(_QUOTES).split())


def _shift(base: str, frm: str, to: str, scale: float = 1.0, char: str = " ") -> str:
    """The file's indentation `base`, moved as far as the model's indentation
    moves from `frm` to `to`, times `scale` (file indent chars per model indent
    char: 0.25 when the model writes 4 spaces where the file has one tab, 2
    when it writes 2-space steps in a 4-space file).  `char` is what the file
    indents with."""
    n = round((len(to) - len(frm)) * scale)
    if n > 0:
        return base + ("\t" if "\t" in base else char) * n
    return base[:max(0, len(base) + n)]


def _indent_char(lines: list[str]) -> str:
    """What a file indents with: a tab when more lines start with one than
    with a space."""
    tabs = sum(ln.startswith("\t") for ln in lines)
    return "\t" if tabs > sum(ln.startswith(" ") for ln in lines) else " "


def _unit(lines: list[str]) -> int | None:
    """The indentation step of some lines: the most common increase in leading
    whitespace from one non-blank line to the next (a tab counts as 1)."""
    steps: dict[int, int] = {}
    prev = None
    for ln in lines:
        if not ln.strip():
            continue
        w = len(_leading_ws(ln))
        if prev is not None and w > prev:
            steps[w - prev] = steps.get(w - prev, 0) + 1
        prev = w
    return max(steps, key=lambda k: (steps[k], -k)) if steps else None


def _indent_scale(old: list[str], new: list[str], block: list[str], file_lines: list[str],
                  a: int) -> float:
    """File indent chars per model indent char.  Read off the lines old_string
    and the file share (old[i] matched block[i]; a is a line both have text
    on); failing that, the ratio of the file's indent step to the model's."""
    votes: dict[float, int] = {}
    m0, f0 = len(_leading_ws(old[a])), len(_leading_ws(block[a]))
    for m, f in zip(old, block):
        dm, df = len(_leading_ws(m)) - m0, len(_leading_ws(f)) - f0
        if m.strip() and dm and df:
            votes[df / dm] = votes.get(df / dm, 0) + 1
    if votes:
        return max(votes, key=votes.get)
    um = _unit(old) or _unit(new)
    uf = _unit(file_lines)
    if um and uf:
        return uf / um
    tabs = _indent_char(file_lines) == "\t" and not any(m.startswith("\t") for m in old + new)
    return 1 / _TAB_WIDTH if tabs else 1.0


def _opens_block(lines: list[str]) -> bool:
    """A line ends with an opener (`{`, `:`, `(`, `[`) and the next text line
    is not its closer — at one depth that is indentation lost, not style."""
    text = [ln.strip() for ln in lines if ln.strip()]
    return any(t.endswith(("{", ":", "(", "[")) and not nxt.startswith(("}", ")", "]"))
               for t, nxt in zip(text, text[1:]))


def _ending(line: str) -> str:
    return line[len(line.rstrip("\r\n")):]


def _block_replace(content: str, old_string: str, new_string: str, key) -> str | None:
    """Replace the one block of file lines that equals old_string line by line
    under `key`.  new_string may have a different number of lines.  Lines that
    new_string keeps from old_string unchanged are copied from the file as they
    are, so the model's slips there are never written; every other line takes
    the indentation of the file line it replaces (or follows), shifted as
    new_string shifts it against old_string.  None when no block, or more than
    one, matches."""
    old_lines = old_string.strip("\n").split("\n")
    body = new_string.strip("\n")
    new_lines = body.split("\n") if body else []
    if not any(key(ln) for ln in old_lines):
        return None
    file_lines = split_lines(content, keepends=True)
    k = len(old_lines)
    if k > len(file_lines):
        return None
    file_keys = [key(ln) for ln in file_lines]
    old_keys = [key(ln) for ln in old_lines]
    matches = [i for i in range(len(file_lines) - k + 1)
               if file_keys[i] == old_keys[0] and file_keys[i:i + k] == old_keys]
    if len(matches) != 1:
        return None
    i0 = matches[0]
    block = file_lines[i0:i0 + k]
    a = next(j for j, ln in enumerate(old_lines) if key(ln))
    ops = difflib.SequenceMatcher(None, old_keys, [key(ln) for ln in new_lines],
                                  autojunk=False).get_opcodes()
    # Strings with no indentation structure left say nothing about where an
    # added line goes: refuse rather than guess (the caller shows the block to
    # copy).  That is old_string at one depth where the file's lines are not, or
    # old and new both at one depth while new opens a block it does not indent.
    depths = lambda ls: {len(_leading_ws(ln)) for ln in ls if ln.strip()}
    if len(depths(old_lines)) == 1 and any(tag != "equal" and (i2 - i1) != (j2 - j1)
                                           for tag, i1, i2, j1, j2 in ops):
        if len(depths(block)) > 1 or (len(depths(new_lines)) <= 1 and _opens_block(new_lines)):
            return None
    scale = _indent_scale(old_lines, new_lines, block, file_lines, a)
    char = _indent_char(file_lines)
    # Matched through curly quotes the file does not have: the model's new lines
    # carry the same slip, so they are written with the file's straight quotes.
    straighten = key is _norm_key and all(ln.translate(_QUOTES) == ln for ln in block)

    def placed(n: str, oi: int) -> str:
        """new line n where old line oi (and its file line) was."""
        if not n.strip():
            return ""
        if straighten:
            n = n.translate(_QUOTES)
        if not old_lines[oi].strip():
            oi = a
        return _shift(_leading_ws(block[oi]), _leading_ws(old_lines[oi]), _leading_ws(n),
                      scale, char) + n.strip()

    out: list[str] = []
    for tag, i1, i2, j1, j2 in ops:
        for t in range(j2 - j1):
            n = new_lines[j1 + t]
            if tag == "equal" and old_lines[i1 + t] == n:
                out.append(block[i1 + t].rstrip("\r\n"))   # untouched: the file's own line
            elif i2 > i1:
                out.append(placed(n, min(i1 + t, i2 - 1)))
            else:
                out.append(placed(n, max(0, i1 - 1)))         # inserted after old line i1-1
    eol = _ending(block[0]) or "\n"
    last = _ending(block[-1])
    rebuilt = "".join(ln + eol for ln in out[:-1]) + (out[-1] + last if out else "")
    return "".join(file_lines[:i0]) + rebuilt + "".join(file_lines[i0 + k:])


_TOLERANT_NOOP = ("No change: old_string matched only after ignoring whitespace, and new_string "
                  "applied that way leaves the file as it is. To change indentation, copy "
                  "old_string exactly from read_file.")


class _Found:
    """What _find_edit made of old_string in a text."""

    def __init__(self, text: str | None = None, count: int = 0, note: str = "",
                 message: str = "", old: str = "", new: str = ""):
        self.text = text          # the edited text, when the edit applies
        self.count = count        # occurrences replaced, or found when too many
        self.note = note          # how it matched, for the OK line
        self.message = message    # a final non-error answer ("No change …")
        self.old, self.new = old, new   # the variant that matched exactly


def _find_edit(text: str, old_string: str, new_string: str, replace_all: bool,
               match: bool) -> _Found:
    old, new = old_string, new_string
    count = text.count(old)
    # Models often copy read_file's "  12: " line-number prefix into old_string.
    # If the exact match fails, strip the prefix and retry.  new_string loses a
    # prefix only when it is numbered from the same line.
    start = _lineno_start(old_string)
    s_old = _strip_lineno_prefixes(old_string)
    s_new = new_string if start is None else _strip_lineno_prefixes(new_string, start)
    if count == 0 and s_old != old and text.count(s_old) > 0:
        old, new, count = s_old, s_new, text.count(s_old)
    if count:
        if replace_all:
            return _Found(text.replace(old, new), count, old=old, new=new)
        if count > 1:
            # Default: require exactly one match so the model can't accidentally
            # replace the wrong occurrence when the same string appears twice.
            return _Found(None, count, old=old, new=new)
        return _Found(text.replace(old, new, 1), 1, old=old, new=new)
    if not replace_all:
        # Whitespace-tolerant unique block match (single-edit path only).  Try the
        # raw strings first, then the prefix-stripped variants.
        tiers = [(None, " (matched with whitespace tolerance)")]
        if match:
            tiers += [(_ws_key, " (matched ignoring indentation; new_string re-indented to fit)"),
                      (_norm_key, " (matched after normalising quotes and spacing)")]
        for key, note in tiers:
            for o, n in ((old_string, new_string), (s_old, s_new)):
                nc = (_tolerant_replace(text, o, n) if key is None
                      else _block_replace(text, o, n, key))
                if nc == text:
                    return _Found(message=_TOLERANT_NOOP)
                if nc is not None:
                    return _Found(nc, 1, note)
    # Already-applied detection: if old_string is gone but a substantial
    # new_string is already present, the edit was very likely made on an
    # earlier turn. Report that as a non-error so the model stops re-trying
    # the same change (a common cause of repeated "not found" errors).  A
    # short or repeated new_string proves nothing — `return None` sits in
    # many places — so it must be distinctive, and the line is named.  Nor does
    # one that is part of old_string: a deletion's new_string is always still
    # in the file when its old_string fails to match.
    for cand in (new_string, s_new):
        if (cand and cand not in old_string and cand not in s_old and text.count(cand) == 1
                and (cand.strip().count("\n") >= 1 or len("".join(cand.split())) >= 30)):
            line = text[:text.index(cand)].count("\n") + 1
            return _Found(message=f"No change needed: the file already contains new_string at "
                                  f"line {line} — this edit appears to have been applied already.")
    return _Found()


# ── hints for an old_string that did not match ───────────────────────────────

def _closest_lines_hint(content: str, old_string: str, diff: bool = False) -> str:
    """Find the file lines most similar to old_string and return them verbatim so
    the model can copy the exact text on its next attempt, and with diff (the match
    feature) where a one-line old_string differs from the closest. Returns '' if
    nothing is close enough."""
    file_lines = split_lines(content)
    old_nonblank = [ln for ln in old_string.split("\n") if ln.strip()]
    if not old_nonblank or not file_lines:
        return ""
    # Match on the most distinctive (longest) line of old_string.
    query = max(old_nonblank, key=lambda ln: len(ln.strip())).strip()
    # get_close_matches runs the cheap quick_ratio filters before the full
    # ratio, so a long file costs little; map back to the lines as written.
    as_written: dict[str, str] = {}
    for ln in file_lines:
        as_written.setdefault(ln.strip(), ln)
    close = difflib.get_close_matches(query, list(as_written), n=3, cutoff=0.6)
    if not close:
        return ""
    out = (" The closest lines in the file are below — copy old_string exactly from these, "
           "including leading whitespace and WITHOUT the read_file line-number prefix:\n"
           + "\n".join(as_written[m][:200] for m in close))
    if diff and len(old_nonblank) == 1 and close[0] != query:
        # One line: say where the best one is and where it differs — the model
        # sent the same miss twice when it only had the line to compare by eye.
        n = next(i for i, ln in enumerate(file_lines) if ln.strip() == close[0]) + 1
        yours, theirs = _first_difference(query, close[0])
        out += f"\nFirst difference — yours: `{yours}`; the file's line {n}: `{theirs}`"
    return out


def _first_difference(a: str, b: str, around: int = 30) -> tuple[str, str]:
    """a and b cut to the stretch where they first differ, `around` chars of
    common text on each side, with … where text was cut."""
    op = next(o for o in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
              if o[0] != "equal")
    _, i1, i2, j1, j2 = op

    def cut(s: str, lo: int, hi: int) -> str:
        start, end = max(0, lo - around), min(len(s), hi + around)
        return ("…" if start else "") + s[start:end] + ("…" if end < len(s) else "")
    return cut(a, i1, i2), cut(b, j1, j2)


def _numbered(lines: list[str], first: int) -> str:
    """Lines in read_file's format, first numbered `first`."""
    return "\n".join(f"{first + i:4}: {ln}" for i, ln in enumerate(lines))


def _closest_block_hint(content: str, old_string: str) -> str:
    """The block of the file most like a multi-line old_string, in read_file's
    format, and the first line where the two differ; '' when nothing is close
    enough or two blocks are equally close."""
    file_lines = split_lines(content)
    old = _strip_lineno_prefixes(old_string).strip("\n").split("\n")
    k = len(old)
    if k < 2 or k > len(file_lines) or len(file_lines) > _HINT_MAX_LINES:
        return ""
    fk = [ln.strip() for ln in file_lines]
    ok = [ln.strip() for ln in old]
    where: dict[str, list[int]] = {}
    for i, s in enumerate(fk):
        if s:
            where.setdefault(s, []).append(i)
    # Candidate blocks start where some line of old_string nearly matches a file
    # line — scoring only those keeps a long file cheap.
    keys = list(where)
    starts: set[int] = set()
    for j in sorted(range(k), key=lambda j: -len(ok[j]))[:8]:
        if len(ok[j]) < 4:
            continue
        for m in difflib.get_close_matches(ok[j], keys, n=5, cutoff=0.6):
            starts.update(i - j for i in where[m][:50])
    want = "\n".join(ok)
    scored = sorted(((difflib.SequenceMatcher(None, "\n".join(fk[s:s + k]), want,
                                              autojunk=False).ratio(), s)
                     for s in starts if 0 <= s <= len(fk) - k), reverse=True)
    if not scored or scored[0][0] < 0.75:
        return ""
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return ""
    s = scored[0][1]
    j = next((j for j in range(k) if fk[s + j] != ok[j]), None)
    first = s + 1
    out = (f" The closest block in the file is lines {first}-{first + k - 1} — copy old_string "
           f"from it (the line-number prefixes are stripped automatically):\n"
           + _numbered(file_lines[s:s + k], first))
    if j is not None:
        out += (f"\nFirst difference — your line {j + 1}: `{old[j].strip()[:160]}`; "
                f"the file's line {first + j}: `{file_lines[s + j].strip()[:160]}`")
    return out


def _elsewhere(f: _File, path: str, old_string: str, workdir: Path, index, cancel) -> str:
    """' The text of old_string is in a.py:12 …' when old_string's most
    distinctive line is in another file — the model named the wrong one."""
    lines = [ln.strip() for ln in _strip_lineno_prefixes(old_string).split("\n")]
    needle = max(lines, key=len, default="")
    if len(needle) < 12:
        return ""
    hits: list[tuple[str, int]] = []
    if index is not None:
        if index.wait_fresh(cancel):            # cancelled or stopped: no hint
            return ""
        rel = index.rel(f.p)
        hits = code_index.find_line_text(index, needle, skip=rel, cancel=cancel)
    else:
        # Without the index only the file's own directory is read, bounded.
        try:
            siblings = sorted(q for q in f.p.parent.iterdir() if q.is_file() and q != f.p)
        except OSError:
            siblings = []
        base = workdir.resolve()
        for q in siblings[:_SIBLING_FILES]:
            try:
                if q.stat().st_size > _SIBLING_BYTES:
                    continue
                raw = q.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue
            text = raw.decode("utf-8", errors="replace")
            if needle not in text:
                continue
            ln = next((n for n, line in enumerate(split_lines(text), 1) if needle in line), 0)
            try:
                hits.append((str(q.relative_to(base)), ln))
            except ValueError:
                continue
            if len(hits) >= 3:
                break
    if not hits:
        return ""
    where = ", ".join(f"{h}:{ln}" for h, ln in hits)
    return f" Its line `{needle[:120]}` is in {where}, not in {path} — did you mean that file?"


def _occurrences(content: str, old: str, parsed) -> str:
    """Where each occurrence of old is, and how to pick one."""
    rows, at = [], content.find(old)
    while at != -1 and len(rows) < _MAX_OCCURRENCES:
        rows.append(content.count("\n", 0, at) + 1)
        at = content.find(old, at + max(1, len(old)))
    lines = []
    for r in rows:
        owner = code_nav.enclosing_symbol(parsed.symbols, r) if parsed is not None else None
        lines.append(f"  L{r}" + (f" (in {owner.kind} {owner.qualname})" if owner else ""))
    return (" It occurs at:\n" + "\n".join(lines)
            + "\nAdd a neighbouring line to old_string so it matches only the one you mean.")


# ── after an edit: what changed, does it parse, what else must change ────────

def _lang(p: Path) -> str | None:
    if not code_nav.AVAILABLE:
        return None
    try:
        return code_nav.language_for(p)
    except Exception:
        return None


def _parse(lang: str | None, text: str):
    if lang is None:
        return None
    try:
        return code_nav.parse_bytes(lang, text.encode("utf-8"))
    except Exception:
        return None


def _syntax_errors(p: Path, text: str, parsed) -> list[tuple[int, str]] | None:
    """[(line, message)] of the file's syntax errors, [] when it parses, None
    when it cannot be checked.  Python is compiled (exact); other code_nav
    languages use tree-sitter's ERROR / MISSING nodes."""
    if p.suffix.lower() in (".py", ".pyi"):
        # A SyntaxWarning printed to stderr would land on the curses screen.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                compile(text, p.name, "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
            except SyntaxError as e:
                return [(e.lineno or 1, e.msg)]
            except (ValueError, RecursionError, MemoryError):
                return None
        return []
    if parsed is None or getattr(parsed, "tree", None) is None:
        return None
    root = parsed.tree.root_node
    if not root.has_error:
        return []
    out: list[tuple[int, str]] = []
    stack = [root]
    while stack and len(out) < 50:
        n = stack.pop()
        if n.is_missing:
            out.append((n.start_point[0] + 1, f"missing '{n.type}'"))
        elif n.type == "ERROR":
            out.append((n.start_point[0] + 1, "syntax error"))
        elif n.has_error:
            stack.extend(reversed(n.children))
    return sorted(out)


def _syntax_note(p: Path, before: list | None, after: list | None, lines: list[str],
                 first: int, last: int) -> str:
    """A warning when the change added a syntax error (the file parsed before
    and not after, or has more errors than before), or when an error is still
    there next to the lines the change touched — an attempted fix that did not
    fix it.  A file broken elsewhere is not blamed on the edit."""
    if not after or before is None:
        return ""
    lo, hi = first, max(first, last)
    dist = lambda e: 0 if lo <= e[0] <= hi else min(abs(e[0] - lo), abs(e[0] - hi))
    ln, msg = min(after, key=dist)
    text = lines[ln - 1].strip()[:160] if 0 < ln <= len(lines) else ""
    if len(after) > len(before):
        return (f"\nWARNING: {p.name} no longer parses after this change — line {ln}: `{text}` "
                f"({msg}). The change WAS written; fix it with another edit.")
    if dist((ln, msg)) <= _STILL_BROKEN_NEAR:
        return (f"\nWARNING: {p.name} still does not parse — line {ln}: `{text}` ({msg}). "
                f"Nothing here fixed it yet.")
    return ""


def _changed_span(before: list[str], after: list[str]) -> tuple[int, int]:
    """(first, last) changed line of `after`, 1-based; last < first when lines
    were only removed."""
    nb, na = len(before), len(after)
    i = 0
    while i < nb and i < na and before[i] == after[i]:
        i += 1
    j = 0
    while j < nb - i and j < na - i and before[nb - 1 - j] == after[na - 1 - j]:
        j += 1
    return i + 1, na - j


def _report(head: str, lines: list[str], first: int, last: int, parsed) -> str:
    """head, then the changed lines with _CONTEXT lines around them, in
    read_file's format — the model can check the edit and copy from it."""
    n = len(lines)
    if n == 0:
        return head + " — the file is now empty."
    if last < first:
        lo, hi = max(1, first - _CONTEXT), min(n, first - 1 + _CONTEXT)
        what = f"lines removed; lines {lo}-{hi} around the cut now read"
        at = max(1, min(first, n))
    else:
        lo, hi = max(1, first - _CONTEXT), min(n, last + _CONTEXT)
        what = f"line {first} now reads" if first == last else f"lines {first}-{last} now read"
        at = first
    owner = code_nav.enclosing_symbol(parsed.symbols, at) if parsed is not None else None
    where = f" (inside {owner.kind} {owner.qualname}, L{owner.start}-{owner.end})" if owner else ""
    rows = list(range(lo, hi + 1))
    if len(rows) > _SNIPPET_MAX:
        half = _SNIPPET_MAX // 2
        body = (_numbered(lines[lo - 1:lo - 1 + half], lo)
                + f"\n      … {len(rows) - 2 * half} lines not shown …\n"
                + _numbered(lines[hi - half:hi], hi - half + 1))
    else:
        body = _numbered(lines[lo - 1:hi], lo)
    return f"{head} — {what}{where}:\n{body}"


def _replaced_lines(text: str, old: str, new: str) -> str:
    """' — at lines 12, 40, 77 (as the file reads now)' for a replace_all."""
    rows, at, k = [], text.find(old), 0
    shift = new.count("\n") - old.count("\n")
    while at != -1 and old:
        rows.append(text.count("\n", 0, at) + 1 + k * shift)
        k += 1
        at = text.find(old, at + len(old))
    if not rows:
        return ""
    shown = ", ".join(map(str, rows[:_MAX_LINES_LISTED]))
    more = f" and {len(rows) - _MAX_LINES_LISTED} more" if len(rows) > _MAX_LINES_LISTED else ""
    return f" — at lines {shown}{more} (as the file reads now)"


_DEF_KINDS = code_nav.CALLABLE_KINDS | code_nav.CONTAINER_KINDS if code_nav.AVAILABLE else set()


def _defs(parsed) -> dict[str, list]:
    """bare name -> its code definitions in a parsed file."""
    out: dict[str, list] = {}
    if parsed is None or parsed.lang in code_index.DATA_LANGS:
        return out          # config keys and CSS rules have no callers
    for s in parsed.symbols:
        if s.kind in _DEF_KINDS:
            out.setdefault(s.name, []).append(s)
    return out


def _gone(b: dict, a: dict) -> list:
    """Definitions in b whose name a no longer has — a removed class, not also
    each of its methods."""
    gone = [v[0] for name, v in b.items() if name not in a]
    quals = {s.qualname for s in gone}
    return [s for s in gone if s.qualname.rsplit(".", 1)[0] not in quals or "." not in s.qualname]


def _params(parsed, s) -> str:
    """The parameter list on a definition's name line: `def f(a, b=1) -> int:`
    -> '(a, b=1)'.  The whole rest of the line when it has no parenthesis."""
    line = parsed.lines[s.name_line - 1] if 0 < s.name_line <= len(parsed.lines) else ""
    at = line.find(s.name)
    rest = line[at + len(s.name):] if at >= 0 else line
    o = rest.find("(")
    if o < 0:
        return rest.strip()
    depth = 0
    for i in range(o, len(rest)):
        if rest[i] == "(":
            depth += 1
        elif rest[i] == ")":
            depth -= 1
            if depth == 0:
                return rest[o:i + 1]
    return rest[o:].strip()


def _same(a: str, b: str) -> bool:
    """Equal but for whitespace."""
    return "".join(a.split()) == "".join(b.split())


_ALIAS = re.compile(r"\bas\s+([A-Za-z_]\w*)")


def _imported_names(parsed) -> set[str]:
    """The names a file binds by importing: modules (`shop.pricing` and
    `pricing`) and the names pulled out of them."""
    out: set[str] = set()
    for imp in parsed.imports:
        mod = imp.module.strip(".").replace("::", ".")
        if mod:
            out.update({mod, mod.rsplit(".", 1)[-1]})
        out.update(imp.names)
        out.update(_ALIAS.findall(imp.text))     # `import shop.pricing as p`
    return out


def _is_use(parsed, role: str, recv: str | None, top_level: bool) -> bool:
    """A call on a receiver is a use of a top-level function only when the
    receiver is a module the file imports (`pricing.calc_tax()`), never an
    object (`ledger.calc_tax()` is some class's method of the same name)."""
    if not top_level or role != "call" or not recv:
        return True
    return recv.split(".", 1)[0] in _imported_names(parsed) or recv in _imported_names(parsed)


def _rows(parsed, rel: str, name: str, roles: set[str], top_level: bool):
    """[(file, line, owner, text)] of the uses of `name` in one parsed file."""
    found = code_nav.references_in(parsed, name)
    out = []
    for r in sorted(found):
        role, recv, _rtype = found[r]
        if role in roles and _is_use(parsed, role, recv, top_level):
            owner = code_nav.enclosing_symbol(parsed.symbols, r + 1)
            out.append((rel, r + 1, owner.qualname if owner else "",
                        parsed.lines[r].strip() if r < len(parsed.lines) else ""))
    return out


def _scan_uses(workdir: Path, rel: str, name: str, roles: set[str], lang: str | None,
               cancel, top_level: bool = True) -> tuple[list[tuple[str, int, str, str]], bool, int]:
    """Uses of `name` in the project's other files without the code index:
    (uses, whether every file was checked, files checked).  Bounded by
    _SCAN_SECONDS and _SCAN_FILES; a byte prefilter keeps most files unparsed."""
    family = code_index.lang_family(lang)
    needle = name.encode()
    base = workdir.resolve()
    deadline = time.monotonic() + _SCAN_SECONDS
    out, checked = [], 0
    for i, fpath in enumerate(code_nav.iter_source_files(base)):
        if (i >= _SCAN_FILES or time.monotonic() > deadline
                or (cancel is not None and cancel.is_set())):
            return out, False, checked
        try:
            frel = fpath.resolve().relative_to(base).as_posix()
        except (ValueError, OSError):
            continue
        if frel == rel or code_index.lang_family(code_nav.language_for(fpath)) != family:
            continue
        checked += 1
        try:
            if needle not in fpath.read_bytes():
                continue
            parsed = code_nav.parse(fpath)
        except OSError:
            continue
        if parsed is None:
            continue
        out += _rows(parsed, frel, name, roles, top_level)
    return out, True, checked


def _uses_left(name: str, qual: str, roles: set[str], rel: str, after, index, cancel,
               workdir: Path) -> tuple[list[tuple[str, int, str, str]], str]:
    """([(file, line, owner, text)] of the uses of a definition still in the
    project, and '' when that covers the whole project — else a note saying
    what was not checked).  This file's own come from its new parse; other
    files from the index, or without it from a bounded scan."""
    top_level = "." not in qual
    out = _rows(after, rel, name, roles, top_level) if after is not None else []
    pointer = f"find_references(\"{name}\", role=\"call\")"
    if index is None:
        lang = after.lang if after is not None else None
        more, complete, checked = _scan_uses(workdir, rel, name, roles, lang, cancel, top_level)
        note = "" if complete else (f"(other files: {checked} checked in the time allowed — "
                                    f"{pointer} lists the rest)")
        return out + more, note
    if index.wait_fresh(cancel):
        return out, f"(other files were not checked — {pointer} lists them)"
    bare, recv, cls = code_index.query_target(index, qual if "." in qual else name)
    uses, _defs_found = code_index.uses_of(index, bare, recv, cancel, cls)
    for u_rel, row, role, recv, owner, text in uses:
        if u_rel == rel or role not in roles:
            continue                # this file was answered from its new parse
        if top_level and role == "call" and recv:
            try:
                parsed = code_nav.parse(index.root / u_rel)
            except OSError:
                parsed = None
            if parsed is not None and not _is_use(parsed, role, recv, top_level):
                continue
        out.append((u_rel, row, owner.qualname if owner else "", text))
    return out, ""


def _batch_hint(uses, features) -> str:
    """Point at edit_files when several places are left to change."""
    if "batch" not in features or len(uses) < 2:
        return ""
    return "\n(edit_files changes all of these in ONE call: one edit per place, any files.)"


def _list_uses(uses) -> str:
    rows = [f"  {r}:{ln}" + (f" (in {o})" if o else "") + f": {t[:120]}"
            for r, ln, o, t in uses[:_MAX_USES_SHOWN]]
    if len(uses) > _MAX_USES_SHOWN:
        rows.append(f"  … and {len(uses) - _MAX_USES_SHOWN} more")
    return "\n".join(rows)


def _definition_checks(rel: str, before, after, index, cancel, workdir: Path, *,
                       signatures: bool = True, features=frozenset()) -> str:
    """What a change to the file's definitions leaves to do elsewhere: uses of
    a definition that is gone, calls of one whose parameters changed.  Every
    message says when it could not cover the whole project."""
    b, a = _defs(before), _defs(after)
    if not b:
        return ""
    gone = _gone(b, a)
    changed = []
    if signatures:
        for name, v in b.items():
            if name in a and len(v) == 1 and len(a[name]) == 1 and v[0].kind in code_nav.CALLABLE_KINDS:
                old_p, new_p = _params(before, v[0]), _params(after, a[name][0])
                if not _same(old_p, new_p):
                    changed.append((v[0], a[name][0], old_p, new_p))
    notes = []
    for s in gone[:_MAX_CHECKED_DEFS]:
        uses, scope = _uses_left(s.name, s.qualname, {"call", "import", "type"}, rel, after,
                                 index, cancel, workdir)
        if uses:
            notes.append(f"WARNING: `{s.name}` is no longer defined in {rel}, but these places "
                         f"still use it — update them as well:\n{_list_uses(uses)}"
                         + (f"\n{scope}" if scope else "") + _batch_hint(uses, features))
        elif scope:
            notes.append(f"NOTE: `{s.name}` is no longer defined in {rel}. {scope}")
    for old_s, new_s, old_p, new_p in changed[:_MAX_CHECKED_DEFS]:
        uses, scope = _uses_left(new_s.name, new_s.qualname, {"call"}, rel, after, index,
                                 cancel, workdir)
        what = f"`{new_s.name}`'s parameters changed from {old_p} to {new_p}"
        if uses:
            notes.append(f"NOTE: {what}. Check that each call passes the right arguments:\n"
                         f"{_list_uses(uses)}" + (f"\n{scope}" if scope else "")
                         + _batch_hint(uses, features))
        elif scope:
            notes.append(f"NOTE: {what}. {scope}")
    return "".join("\n" + n for n in notes)


def _after_edit(f: _File, path: str, head: str, before_text: str, after_text: str,
                lang: str | None, features, index, cancel, workdir: Path, *,
                snippet: bool = True, before_parsed=None) -> str:
    """The result of a written edit: head, the changed lines, a syntax warning
    and the definition checks, as the features ask."""
    if not ({"feedback", "checks"} & features):
        return head
    before = before_parsed if before_parsed is not None else _parse(lang, before_text)
    after = _parse(lang, after_text)
    out = head
    if "feedback" in features:
        b_lines, a_lines = split_lines(before_text), split_lines(after_text)
        first, last = _changed_span(b_lines, a_lines)
        if snippet:
            out = _report(head, a_lines, first, last, after)
        out += _syntax_note(f.p, _syntax_errors(f.p, before_text, before),
                            _syntax_errors(f.p, after_text, after), a_lines, first, last)
    if "checks" in features:
        try:
            out += _definition_checks(_rel_of(f.p, workdir, path), before, after, index, cancel,
                                      workdir, features=features)
        except Exception:
            pass            # a hint must never turn a written edit into an error
    return out


def _rel_of(p: Path, workdir: Path, path: str) -> str:
    try:
        return p.relative_to(workdir.resolve()).as_posix()
    except ValueError:
        return path


# ── the tools ────────────────────────────────────────────────────────────────

def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False, *,
              workdir: Path, index=None, cancel=None, edit_features=EDIT_FEATURES) -> str:
    features = frozenset(edit_features)
    f = _load(path, workdir, "edit_file")
    if isinstance(f, str):
        return f
    content = f.text
    old_string, new_string = f.lf(old_string), f.lf(new_string)
    # An empty old_string "occurs" between every character: replace_all would
    # splice new_string in everywhere.  Only an empty file may be filled this way.
    if not old_string and content:
        return ("ERROR: old_string is empty. Copy the text to replace from read_file; "
                "to add text at the end use append_to_file, to replace the whole file "
                "use write_file. Nothing was written.")
    match = "match" in features
    lang = _lang(f.p) if features else None

    found = _find_edit(content, old_string, new_string, replace_all, match)
    if found.message:
        return found.message
    if found.text is None:
        if found.count > 1:
            msg = (f"ERROR: old_string found {found.count} times; must match exactly once "
                   f"(set replace_all=true to replace all)")
            if match:
                msg += "." + _occurrences(content, found.old, _parse(lang, content))
            return msg
        msg = "ERROR: old_string not found in file."
        if "checks" in features:
            try:
                msg += _elsewhere(f, path, old_string, workdir, index, cancel)
            except Exception:
                pass
        hint = _closest_block_hint(content, old_string) if match else ""
        msg += hint or _closest_lines_hint(content, old_string, match)
        if ("symbol" in features and lang is not None
                and _strip_lineno_prefixes(old_string).strip("\n").count("\n") >= 10):
            msg += ("\nTo rewrite a whole function or class, replace_symbol(path, name, "
                    "new_source) needs no old_string.")
        return msg

    if found.text == content and "feedback" in features:
        # new_string equal to the text it replaces: saying "applied" here sent the
        # model to write_file, convinced its edit had not landed.
        lines = split_lines(content)
        at = content[:content.find(found.old)].count("\n") + 1 if found.old in content else 1
        msg = (f"No change: new_string is identical to the text it replaces, so {path} is "
               f"unchanged. Line {at} still reads: `{lines[at - 1].strip()[:200] if at <= len(lines) else ''}`")
        errs = _syntax_errors(f.p, content, _parse(lang, content))
        return msg + _syntax_note(f.p, errs, errs, lines, at, at + found.old.count("\n"))
    err = f.save(found.text)
    if err:
        return err
    if replace_all:
        head = f"Replaced {found.count} occurrence(s)"
        if "feedback" in features:
            head += _replaced_lines(content, found.old, found.new)
    else:
        head = "OK — 1 change applied" + found.note
    nudge = ""
    if "symbol" in features and not replace_all:
        nudge = _rewrite_nudge(path, content, found.text, old_string, _parse(lang, content))
    return _after_edit(f, path, head, content, found.text, lang, features, index, cancel,
                       workdir, snippet=not replace_all) + nudge


def _rewrite_nudge(path: str, before_text: str, after_text: str, old_string: str, before) -> str:
    """When an edit_file copied most of one definition to rewrite it, say that
    replace_symbol does the same without the copy (output the model must write)."""
    if before is None:
        return ""
    old_lines = _strip_lineno_prefixes(old_string).strip("\n").count("\n") + 1
    if old_lines < _NUDGE_MIN_LINES:
        return ""
    first, _last = _changed_span(split_lines(before_text), split_lines(after_text))
    defs = [s for s in before.symbols if s.kind in _DEF_KINDS and s.start <= first <= s.end
            and _NUDGE_SHARE * (s.end - s.start + 1) <= old_lines <= (s.end - s.start + 1) + 2]
    if not defs:
        return ""
    s = defs[-1]                    # pre-order: the innermost
    return (f"\n(This rewrote most of {s.kind} {s.qualname}; replace_symbol(\"{path}\", "
            f"\"{s.qualname}\", new_source) does that without copying the old text.)")


def replace_symbol(path: str, name: str, new_source: str, *, workdir: Path, index=None,
                   cancel=None, edit_features=EDIT_FEATURES) -> str:
    features = frozenset(edit_features)
    f = _load(path, workdir, "replace_symbol")
    if isinstance(f, str):
        return f
    lang = _lang(f.p)
    before = _parse(lang, f.text)
    if before is None:
        return (f"ERROR: {path} is not a source file replace_symbol can parse "
                f"({code_nav.SUPPORTED_EXTENSIONS}) — use edit_file. Nothing was written.")
    sym = code_nav.resolve_symbol(before, str(name).strip(), path,
                                  fallback="pass the name of the definition to replace")
    if isinstance(sym, str):
        return sym if sym.startswith("ERROR") else "ERROR: " + sym.strip("()")
    content = f.text
    src = _strip_lineno_prefixes(f.lf(new_source))
    lines = split_lines(content, keepends=True)
    block = lines[sym.start - 1:sym.end]
    body = src.strip("\n")
    new_lines = body.split("\n") if body.strip() else []
    # The definition keeps its place in the file: new_source's first line takes
    # the indentation the old first line had (a method keeps its class indent).
    base = _leading_ws(block[0]) if block else ""
    head_ws = next((_leading_ws(ln) for ln in new_lines if ln.strip()), base)
    char = _indent_char(lines)
    if head_ws != base or (char == "\t" and any(ln.startswith(" ") for ln in new_lines)):
        # Spaces from the model in a tab-indented file count _TAB_WIDTH to a tab.
        scale = (1 / _TAB_WIDTH if char == "\t" and "\t" not in "".join(new_lines) else 1.0)
        new_lines = [_shift(base, head_ws, _leading_ws(ln), scale, char) + ln.strip()
                     if ln.strip() else "" for ln in new_lines]
    eol = (_ending(block[0]) or "\n") if block else "\n"
    last = _ending(block[-1]) if block else eol
    rebuilt = "".join(ln + eol for ln in new_lines[:-1]) + (new_lines[-1] + last if new_lines else "")
    new_content = "".join(lines[:sym.start - 1]) + rebuilt + "".join(lines[sym.end:])
    if new_content == content:
        return (f"No change: {sym.kind} {sym.qualname} in {path} already reads exactly like "
                f"new_source.")
    err = f.save(new_content)
    if err:
        return err
    verb = "replaced" if new_lines else "deleted"
    head = f"OK — {verb} {sym.kind} {sym.qualname} (was L{sym.start}-{sym.end})"
    return _after_edit(f, path, head, content, new_content, lang, features, index, cancel,
                       workdir, before_parsed=before)


# ── several files in one call ────────────────────────────────────────────────
# A small model makes one edit per turn — a rename was 8 edit_file calls, each a
# full model round-trip (edit evals, 2026-10-07).  These change many places in
# one call, and write every file or none.

def _write_all(files: list[tuple[Path, bytes, bytes]]) -> str:
    """Write each (path, new bytes, old bytes); on a failure put back the files
    already written.  '' or an ERROR."""
    done = []
    for p, new, old in files:
        try:
            p.write_bytes(new)
        except OSError as e:
            for q, _new, prev in done:
                try:
                    q.write_bytes(prev)
                except OSError:
                    pass
            return f"ERROR: {e} — nothing was changed (files written before it were restored)."
        done.append((p, new, old))
    return ""


def _encode(f: _File, text: str) -> bytes:
    return (text.replace("\n", "\r\n") if f.crlf else text).encode("utf-8")


def _changed_lines(before: str, after: str) -> list[int]:
    """1-based lines of `after` that are new or changed against `before`."""
    sm = difflib.SequenceMatcher(None, split_lines(before), split_lines(after), autojunk=False)
    out = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "insert"):
            out.extend(range(j1 + 1, j2 + 1))
        elif tag == "delete" and j1 >= 1:
            out.append(j1)                       # the line before the cut
    return sorted(set(out))


def _file_summary(rel: str, before: str, after: str, parsed) -> str:
    """'  shop/cart.py: L17 (in Cart.tax): return compute_tax(...)' lines."""
    lines = split_lines(after)
    rows = []
    for n in _changed_lines(before, after)[:_FILE_LINES_SHOWN]:
        owner = code_nav.enclosing_symbol(parsed.symbols, n) if parsed is not None else None
        text = lines[n - 1].strip()[:140] if n <= len(lines) else ""
        rows.append(f"  {rel}:{n}" + (f" (in {owner.qualname})" if owner else "") + f": {text}")
    more = len(_changed_lines(before, after)) - _FILE_LINES_SHOWN
    return "\n".join(rows) + (f"\n  {rel}: … and {more} more changed lines" if more > 0 else "")


def _parse_edits(edits) -> list[dict] | str:
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except ValueError:
            return "ERROR: edits must be a list of {path, old_string, new_string} objects."
    if isinstance(edits, dict):
        edits = [edits]
    if not isinstance(edits, list) or not edits:
        return "ERROR: edits must be a non-empty list of {path, old_string, new_string} objects."
    out = []
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            return f"ERROR: edit {i} is not an object with path, old_string and new_string."
        missing = [k for k in ("path", "old_string", "new_string") if not isinstance(e.get(k), str)]
        if missing:
            return (f"ERROR: edit {i} has no {', '.join(missing)} — each edit needs path, "
                    f"old_string and new_string. Nothing was written.")
        ra = e.get("replace_all", False)
        if isinstance(ra, str):
            ra = ra.strip().lower() == "true"
        out.append({"path": e["path"], "old_string": e["old_string"],
                    "new_string": e["new_string"], "replace_all": bool(ra)})
    return out


def edit_files(edits, *, workdir: Path, index=None, cancel=None,
               edit_features=EDIT_FEATURES) -> str:
    features = frozenset(edit_features)
    parsed_edits = _parse_edits(edits)
    if isinstance(parsed_edits, str):
        return parsed_edits
    n = len(parsed_edits)
    match = "match" in features
    files: dict[Path, list] = {}           # resolved path -> [_File, text now, shown path]
    notes = []
    for i, e in enumerate(parsed_edits, 1):
        where = f"edit {i} of {n} ({e['path']})"
        p = safe_path(e["path"], workdir)
        if isinstance(p, str):
            return f"ERROR: {where}: {p.removeprefix('ERROR: ')} Nothing was written."
        if p not in files:
            f = _load(e["path"], workdir, "edit_files")
            if isinstance(f, str):
                return f"ERROR: {where}: {f.removeprefix('ERROR: ')} Nothing was written."
            files[p] = [f, f.text, e["path"]]
        f, text, _shown = files[p]
        old, new = f.lf(e["old_string"]), f.lf(e["new_string"])
        if not old and text:
            return f"ERROR: {where}: old_string is empty. Nothing was written."
        found = _find_edit(text, old, new, e["replace_all"], match)
        if found.message:
            notes.append(f"  edit {i} ({e['path']}): {found.message}")
            continue
        if found.text is None:
            lang = _lang(f.p)
            if found.count > 1:
                msg = (f"old_string found {found.count} times; must match exactly once "
                       f"(set replace_all=true on this edit to replace all)")
                if match:
                    msg += "." + _occurrences(text, found.old, _parse(lang, text))
            else:
                msg = "old_string not found in the file."
                if match:
                    msg += (_closest_block_hint(text, old) or _closest_lines_hint(text, old, True))
                else:
                    msg += _closest_lines_hint(text, old)
            return (f"ERROR: {where}: {msg}\nNothing was written — no file was changed. Fix "
                    f"this edit and send all {n} edits again.")
        if found.text == text:
            notes.append(f"  edit {i} ({e['path']}): no change — new_string equals old_string")
            continue
        files[p][1] = found.text
    changed = [(p, f, text, shown) for p, (f, text, shown) in files.items() if text != f.text]
    if not changed:
        return "No change: none of the edits changed anything.\n" + "\n".join(notes)
    err = _write_all([(p, _encode(f, text), _encode(f, f.text)) for p, f, text, _s in changed])
    if err:
        return err
    if index is not None:
        index.invalidate([str(p) for p, *_rest in changed])
    applied = n - len(notes)
    out = [f"OK — {applied} edit{'s' if applied != 1 else ''} applied in {len(changed)} "
           f"file{'s' if len(changed) != 1 else ''}. Changed lines, as the files read now:"]
    tail = []
    for p, f, text, shown in changed:
        rel = _rel_of(p, workdir, shown)
        lang = _lang(p)
        before, after = _parse(lang, f.text), _parse(lang, text)
        out.append(_file_summary(rel, f.text, text, after))
        if "feedback" in features:
            lines = _changed_lines(f.text, text)
            tail.append(_syntax_note(p, _syntax_errors(p, f.text, before),
                                     _syntax_errors(p, text, after), split_lines(text),
                                     lines[0] if lines else 1, lines[-1] if lines else 1))
        if "checks" in features:
            try:
                # No "use edit_files" pointer: this call is one.
                tail.append(_definition_checks(rel, before, after, index, cancel, workdir,
                                               features=features - {"batch"}))
            except Exception:
                pass
    return "\n".join(out + notes) + "".join(tail)


def write_file(path: str, content: str, *, workdir: Path, index=None, cancel=None,
               edit_features=EDIT_FEATURES) -> str:
    features = frozenset(edit_features)
    p = safe_path(path, workdir)
    if isinstance(p, str):
        return p
    # A small model that doubts its write landed rewrites the same file again and
    # again; say so plainly rather than silently writing identical bytes.
    try:
        if p.is_file() and p.read_text(encoding="utf-8") == content:
            return f"No change: {path} already contains exactly this content."
    except (OSError, UnicodeDecodeError):
        pass
    old_text = None
    if {"feedback", "checks"} & features:
        try:
            old_text = p.read_bytes().decode("utf-8") if p.is_file() else None
        except (OSError, UnicodeDecodeError):
            old_text = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"ERROR: {e}"
    # The harness inspects last_tool by name (not return value), but the model
    # uses this confirmation — with concrete size — to know the write landed.
    out = f"Written: {path} ({_size_note(content)})"
    lang = _lang(p) if {"feedback", "checks"} & features else None
    if lang is None and not ("feedback" in features and p.suffix.lower() in (".py", ".pyi")):
        return out
    try:
        after = _parse(lang, content)
        before = _parse(lang, old_text) if old_text is not None else None
        if "feedback" in features:
            a_lines = split_lines(content)
            errs_before = _syntax_errors(p, old_text, before) if old_text is not None else []
            out += _syntax_note(p, errs_before, _syntax_errors(p, content, after), a_lines,
                                1, len(a_lines))
        if "checks" in features and before is not None:
            gone = [s.name for s in _gone(_defs(before), _defs(after))]
            if gone:
                names = ", ".join(gone[:10]) + (f" and {len(gone) - 10} more" if len(gone) > 10 else "")
                out += (f"\nWARNING: this write removed {len(gone)} definition"
                        f"{'s' if len(gone) != 1 else ''} the file had: {names}. write_file "
                        f"replaces the whole file — if that was not intended, put them back.")
                out += _definition_checks(_rel_of(p, workdir, path), before, after, index,
                                          cancel, workdir, signatures=False, features=features)
    except Exception:
        pass
    return out


def append_to_file(path: str, content: str, *, workdir: Path) -> str:
    p = safe_path(path, workdir)
    if isinstance(p, str):
        return p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as e:
        return f"ERROR: {e}"
    return f"OK — appended {_size_note(content)} to {path}"
