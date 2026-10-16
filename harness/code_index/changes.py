"""index_changes: what changed since a git revision (by default: everything
not committed yet), which definitions that touches, and who outside the change
still uses them — the blast radius of a diff in one call, before a review or a
test run."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .. import code_nav
from .records import DATA_LANGS
from .store import ProjectIndex
from .common import _MAX_CALLERS_CHARS, _defs_named, _ready, _scope, _with_note
from .callers import _tested_line, query_target, uses_of, _module_scope
from .defdiff import _DEF_KINDS, _params, _same

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_MAX_CHANGED_DEFS = 10      # changed definitions whose uses are looked up
_MAX_PLACES = 5             # uses listed per changed definition
_ORDER = {"removed": 0, "signature changed": 1, "moved": 2, "changed": 3, "added": 4}


def _git(root: Path, *args: str, timeout: float = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=off", *args],
                           capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    stream = r.stdout if r.returncode == 0 else r.stderr
    return r.returncode, stream.decode("utf-8", errors="replace")


def _parse_diff(text: str) -> list[dict]:
    """[{old, new, old_ranges, new_ranges}] from `git diff -U0`; a path is None
    for an added (old) or deleted (new) file.  A range is (first, last) line;
    a pure insertion or deletion is the point it happened at."""
    files: list[dict] = []
    cur = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "old_ranges": [], "new_ranges": []}
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("--- "):
            cur["old"] = None if line[4:] == "/dev/null" else line[4:].removeprefix("a/")
        elif line.startswith("+++ "):
            cur["new"] = None if line[4:] == "/dev/null" else line[4:].removeprefix("b/")
        elif (m := _HUNK.match(line)):
            o, ol, n, nl = (int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1))
            cur["old_ranges"].append((o, o + max(ol, 1) - 1))
            cur["new_ranges"].append((n, n + max(nl, 1) - 1))
    return [f for f in files if f["old"] or f["new"]]


def _touched(symbols, ranges) -> list:
    """The innermost definitions each changed range falls in."""
    out = []
    for a, b in ranges:
        hits = [s for s in symbols if s.kind in _DEF_KINDS and s.start <= b and a <= s.end]
        inner = [s for s in hits if not any(o is not s and s.start <= o.start and o.end <= s.end
                                             and (o.start, o.end) != (s.start, s.end) for o in hits)]
        out += [s for s in inner if s not in out]
    return out


def _in_ranges(line: int, ranges) -> bool:
    return any(a <= line <= b for a, b in ranges)


def index_changes(base: str = "HEAD", path: str | None = None, *, workdir: Path,
                  index: ProjectIndex | None = None, cancel=None) -> str:
    if (err := _ready(index, cancel)):
        return err
    base = (base or "HEAD").strip()
    if base.startswith("-") or not base:
        return f"ERROR: not a revision: {base!r}"
    path, err = _scope(path, workdir, index)
    if err:
        return err
    root = index.root
    rc, msg = _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if rc != 0:
        rc2, _ = _git(root, "rev-parse", "--show-toplevel")
        return ("ERROR: the project is not a git repository — index_changes compares with git"
                if rc2 != 0 else f"ERROR: unknown revision {base!r} (try HEAD, HEAD~1, main)")
    spec = ["--", path] if path else []
    rc, diff = _git(root, "diff", "--no-color", "--no-ext-diff", "--relative", "-U0", "-M", base, *spec)
    if rc != 0:
        return f"ERROR: git diff failed: {diff.strip()[:300]}"
    rc, untracked = _git(root, "ls-files", "--others", "--exclude-standard", *spec)
    new_files = [u for u in untracked.split("\n") if u] if rc == 0 else []
    files = _parse_diff(diff)
    if not files and not new_files:
        return _with_note(f"No changes since {base}" + (f" under {path}" if path else "") + ".", index)

    changes = []        # (kind, rel, old Symbol | None, new Symbol | None, note)
    changed_ranges: dict[str, list] = {}
    others = []
    for f in files:
        rel = f["new"] or f["old"]
        lang = code_nav.language_for(Path(rel))
        if lang is None or lang in DATA_LANGS:
            others.append(rel)
            continue
        if f["new"]:
            changed_ranges[f["new"]] = f["new_ranges"]
        new_parsed = old_parsed = None
        if f["new"]:
            try:
                new_parsed = code_nav.parse(root / f["new"], cache=False)
            except OSError:
                new_parsed = None
        if f["old"]:
            rc, src = _git(root, "show", f"{base}:./{f['old']}")
            if rc == 0:
                old_parsed = code_nav.parse_bytes(lang, src.encode("utf-8"))
        new_syms = new_parsed.symbols if new_parsed else []
        old_syms = old_parsed.symbols if old_parsed else []
        new_by_q = {s.qualname: s for s in new_syms}
        old_by_q = {s.qualname: s for s in old_syms}
        for s in _touched(new_syms, f["new_ranges"]):
            o = old_by_q.get(s.qualname)
            if o is None:
                changes.append(("added", rel, None, s, ""))
            elif s.kind in code_nav.CALLABLE_KINDS and old_parsed is not None \
                    and not _same(op := _params(old_parsed, o), np := _params(new_parsed, s)):
                changes.append(("signature changed", rel, o, s, f"{op} → {np}"))
            else:
                changes.append(("changed", rel, o, s, ""))
        for o in _touched(old_syms, f["old_ranges"]):
            if o.qualname not in new_by_q and not any(c[2] is o for c in changes):
                home = _moved_to(index, rel, o)
                changes.append(("moved", rel, o, None, f"→ now {home}") if home
                               else ("removed", rel, o, None, ""))
    changes.sort(key=lambda c: (_ORDER[c[0]], c[1], (c[3] or c[2]).start))

    counts: dict[str, int] = {}
    for c in changes:
        counts[c[0]] = counts.get(c[0], 0) + 1
    out = [f"Changes since {base}" + (f" under {path}" if path else "") + f": "
           f"{len({c[1] for c in changes} | set(others) | set(new_files))} files, {len(changes)} "
           f"definition{'s' if len(changes) != 1 else ''} touched"
           + (" (" + ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: _ORDER[kv[0]]))
              + ")" if counts else "") + ":"]
    looked = 0
    for kind, rel, o, s, note in changes:
        sym = s or o
        line = f"  {kind}: {rel}:L{sym.start}-{sym.end} {sym.kind} {sym.qualname}" + (f" {note}" if note else "")
        out.append(line)
        if kind in ("added", "moved") or looked >= _MAX_CHANGED_DEFS:
            continue
        if cancel is not None and cancel.is_set():
            return "ERROR: cancelled"
        looked += 1
        uses = _uses_outside(index, rel, sym, changed_ranges)
        if uses:
            what = ("still used — update these" if kind in ("removed", "signature changed")
                    else "used")
            places = "; ".join(f"{u[0]}:L{u[1]}" + (f" [in {u[4].qualname}]" if u[4] else "")
                               for u in uses[:_MAX_PLACES])
            more = f" and {len(uses) - _MAX_PLACES} more" if len(uses) > _MAX_PLACES else ""
            out.append(f"    {what} in {len(uses)} place{'s' if len(uses) != 1 else ''} outside "
                       f"the change: {places}{more}")
        elif kind in ("removed", "signature changed"):
            out.append("    no use outside the change")
        out.append("    " + _tested_line(uses, set()))
    if len([c for c in changes if c[0] not in ("added", "moved")]) > _MAX_CHANGED_DEFS:
        out.append(f"  (uses looked up for the first {_MAX_CHANGED_DEFS} — pass path= to narrow)")
    if new_files:
        out.append("New files, not committed: " + ", ".join(new_files[:15])
                   + (" ..." if len(new_files) > 15 else ""))
    if others:
        out.append("Also changed (no definitions compared): " + ", ".join(others[:15])
                   + (" ..." if len(others) > 15 else ""))
    text, size = [], 0
    for line in out:
        if size + len(line) + 1 > _MAX_CALLERS_CHARS:
            text.append(f"... (output capped at {_MAX_CALLERS_CHARS} chars — pass path= to narrow)")
            break
        text.append(line)
        size += len(line) + 1
    return _with_note("\n".join(text), index)


def _moved_to(index: ProjectIndex, rel: str, old) -> str:
    """'harness/common.py:L19-22' when a definition gone from `rel` is now
    defined in exactly one other file, with the same kind; '' otherwise."""
    homes = [(e, s) for e, s in _defs_named(index, old.name)
             if e.path != rel and s.kind == old.kind and s.depth == old.depth]
    if len(homes) != 1:
        return ""
    e, s = homes[0]
    return f"{e.path}:L{s.start}-{s.end}"


def _uses_outside(index: ProjectIndex, rel: str, sym, changed_ranges: dict) -> list:
    """Uses of a changed definition, minus the ones inside the change itself."""
    if "." in sym.qualname:
        bare, recv, cls = query_target(index, sym.qualname)
        module = None
    else:
        dotted = rel.rsplit(".", 1)[0].replace("/", ".")
        bare, recv, cls = sym.name, None, None
        module = _module_scope(index, dotted, sym.name)
    uses, _defs = uses_of(index, bare, recv, None, cls, module)
    return [u for u in uses if not _in_ranges(u[1], changed_ranges.get(u[0], ()))]
