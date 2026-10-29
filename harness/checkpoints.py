"""File checkpoints: what the files the model changed looked like before each
turn, so the user can take a turn's changes back (/undo, /keep, the Changes menu).

Before a write tool runs, dispatch records the paths it is about to touch
(dispatcher.written_paths).  The first touch of a path in a checkpoint saves its
pre-image: the bytes and mode, a symlink's target, "a folder", or "did not
exist".  After the call the post-image is saved too, so a turn's diff is exact
and a file the user edited since can be told apart (undo skips it unless
forced).  Contents go to content-addressed blobs, so the after of one turn and
the before of the next are stored once.

One checkpoint per user turn, or per plan step.  The stack grows until /keep
(or /clear, /new), capped at max_turns (/undo-max; 0 = no cap).  Undo goes back
newest first, one checkpoint at a time, so an edit the user made between two
turns is protected like one made after the last.

The store lives in <workdir>/.momo/checkpoints/<session>/ (git-ignored, never
indexed), or ~/.momo-harness/checkpoints/<session>/ when .momo/ can't be used,
and is only created by the first write.  Changes made by run_command are not
seen: a checkpoint only notes that the turn ran commands.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat as stat_mod
import threading
import time
from pathlib import Path

from . import momo_dir
from .diff import build_diff_body
from .paths import momo_home, safe_entry_path, safe_path
from .session import atomic_write

DEFAULT_MAX_TURNS = 20
MAX_FILE_BYTES = 5 * 2**20      # larger files are noted, not saved: "not restorable"
STALE_SECS = 3 * 24 * 3600      # other sessions' checkpoint folders older than this are removed
CHECKPOINTS = "checkpoints"
# Tools that act on a directory entry itself (a link is moved or deleted, not
# followed); every other write tool writes through a link to its target.
ENTRY_TOOLS = ("move_file", "delete_file")
_INDEX = "index.json"
_SHA_RE = re.compile(r"[0-9a-f]{64}")


def fallback_root() -> Path:
    return momo_home() / CHECKPOINTS


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(data: bytes | None) -> str:
    return "" if data is None else data.decode("utf-8", errors="replace")


class CheckpointStore:
    """The checkpoints of one session.  Thread-safe: the worker thread records,
    the frontends list, diff and undo."""

    def __init__(self, workdir: Path, session: str, max_turns: int = DEFAULT_MAX_TURNS):
        self.workdir = Path(workdir).resolve()
        self.session = session
        self.max_turns = max_turns
        self.note = None                 # callable(str): a system line in the chat
        self._lock = threading.RLock()
        self._root: Path | None = None
        self._turns: list[dict] = []
        self._next_id = 1
        self._pending: dict = {"turn": 0, "label": "", "step": ""}
        self._open: dict | None = None   # the checkpoint the running turn writes into
        self._staged: dict[str, bytes] = {}   # pre-images of the call running, until after()
        self._ran_commands = False
        self._dropped_noted = False
        self._load()

    # ── where it lives ──────────────────────────────────────────────────────

    def _candidates(self) -> list[Path]:
        return [momo_dir.momo_root(self.workdir) / CHECKPOINTS / self.session,
                fallback_root() / self.session]

    def _load(self) -> None:
        for root in self._candidates():
            try:
                data = json.loads((root / _INDEX).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if root.is_symlink() or not isinstance(data, dict):
                continue
            self._root = root
            self._turns = [t for t in data.get("turns", []) if isinstance(t, dict)
                           and isinstance(t.get("files"), dict)]
            # The index sits in .momo/, which the model can write: an entry for a
            # path outside the workdir (or under .momo/) is never acted on.
            for t in self._turns:
                t["files"] = {k: e for k, e in t["files"].items()
                              if self._inside(k) and isinstance(e, dict)
                              and isinstance(e.get("before"), dict)}
                if not isinstance(t.get("plan"), dict):
                    t.pop("plan", None)
            self._turns = [t for t in self._turns if t["files"]]
            try:
                self._next_id = max([int(data.get("next_id", 1))]
                                    + [int(t.get("id", 0)) + 1 for t in self._turns])
            except (TypeError, ValueError):
                self._next_id = 1 + len(self._turns)
            return

    def _ensure_root(self) -> Path:
        if self._root is None:
            folder = momo_dir.ensure(self.workdir, CHECKPOINTS)
            base = fallback_root() if isinstance(folder, str) else folder
            self._root = base / self.session
            (self._root / "blobs").mkdir(parents=True, exist_ok=True)
            os.chmod(self._root, 0o700)
            prune_stale(base, current=self._root)
        return self._root

    def _save(self) -> None:
        if self._root is None:
            return
        atomic_write(self._root / _INDEX,
                     json.dumps({"version": 1, "session": self.session,
                                 "next_id": self._next_id, "turns": self._turns}, indent=1))

    def _put_blob(self, data: bytes) -> str:
        sha = _sha(data)
        root = self._ensure_root()
        if root.is_symlink() or (root / "blobs").is_symlink():
            raise OSError(f"{root} is (inside) a symlink — no file copies are saved there")
        path = root / "blobs" / sha
        if not path.exists():
            atomic_write(path, data)
        return sha

    def _get_blob(self, sha: str | None) -> bytes | None:
        # Only a hash names a blob: a tampered index must not read other files.
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha) or self._root is None:
            return None
        try:
            return (self._root / "blobs" / sha).read_bytes()
        except OSError:
            return None

    def _gc(self) -> None:
        """Delete blobs no checkpoint refers to any more."""
        if self._root is None:
            return
        live = {img.get("blob") for t in self._turns for e in t["files"].values()
                for img in (e["before"], e.get("after") or {})}
        try:
            for p in (self._root / "blobs").iterdir():
                if p.name not in live:
                    p.unlink(missing_ok=True)
        except OSError:
            pass

    # ── recording (the worker thread, from dispatch) ─────────────────────

    def begin_turn(self, turn: int, label: str, step: str = "",
                   plan: dict | None = None) -> None:
        """A new user turn or plan step: its writes go to a new checkpoint,
        made on the first one.  plan: the plan step or story it runs, which
        an undo of it puts back to pending (Harness._reset_plan_progress)."""
        with self._lock:
            self._pending = {"turn": turn, "label": " ".join(label.split())[:80], "step": step}
            if plan:
                self._pending["plan"] = plan
            self._open = None
            self._ran_commands = False

    def note_command(self) -> None:
        """The turn ran a command: its changes to files are not tracked."""
        with self._lock:
            self._ran_commands = True
            if self._open is not None and not self._open.get("ran_commands"):
                self._open["ran_commands"] = True
                self._save()

    def _inside(self, key: str) -> bool:
        """Whether key is a path in the workdir (its folder resolved: no link
        out) and not under .momo/ — the only places undo may write."""
        try:
            p = Path(os.path.normpath(key))
            rel = p.parent.resolve().relative_to(self.workdir) / p.name
        except (ValueError, OSError, TypeError):
            return False
        return bool(rel.parts) and rel.parts[0] != momo_dir.MOMO_DIR

    def resolve(self, raw: str, tool: str) -> Path | None:
        """A write tool's path argument as the path it acts on, or None when it
        is not one to track (outside the workdir, or under .momo/)."""
        if not isinstance(raw, str) or not raw or momo_dir.touches(raw, self.workdir):
            return None
        p = (safe_entry_path if tool in ENTRY_TOOLS else safe_path)(raw, self.workdir)
        return p if isinstance(p, Path) else None

    def record(self, raws: list[str], tool: str) -> list[Path]:
        """Save the pre-image of each path a write tool is about to touch (first
        touch in this checkpoint only).  Returns the resolved paths, for after()."""
        paths = [p for r in raws if (p := self.resolve(r, tool)) is not None]
        if not paths:
            return []
        with self._lock:
            cp = self._checkpoint()
            for p in paths:
                key = str(p)
                if key not in cp["files"]:
                    # Held in memory: only a call that changes the file stores it.
                    cp["files"][key] = {"rel": self._rel(p), "before": _image(p, self._stage)}
            if tool == "move_file" and len(paths) == 2:
                cp.setdefault("moves", []).append([str(paths[0]), str(paths[1])])
        return paths

    def after(self, paths: list[Path]) -> None:
        """Save the post-image of paths a write tool touched; an entry the call
        left as it was (a refused or failed write) is dropped again."""
        if not paths:
            return
        with self._lock:
            cp = self._open
            if cp is None:
                return
            for p in paths:
                e = cp["files"].get(str(p))
                if e is None:
                    continue
                e["after"] = _image(p, self._stage)
                if _same(e["before"], e["after"]):
                    del cp["files"][str(p)]
                    continue
                for img in (e["before"], e["after"]):
                    if (data := self._staged.get(img.get("blob"))) is not None:
                        self._put_blob(data)
            self._staged.clear()
            if not cp["files"]:
                self._turns.remove(cp)
                self._open = None
            else:
                self._ensure_root()     # a folder move or a link stores no blob
                self._trim()        # only now: a failed write must not cost a checkpoint
            self._save()
            self._gc()

    def _stage(self, data: bytes) -> str:
        sha = _sha(data)
        self._staged[sha] = data
        return sha

    def _checkpoint(self) -> dict:
        if self._open is None:
            self._open = {"id": self._next_id, "ts": time.time(), **self._pending,
                          "ran_commands": self._ran_commands, "files": {}}
            self._next_id += 1
            self._turns.append(self._open)
        return self._open

    def _trim(self) -> int:
        """Drop the oldest checkpoints over max_turns; returns how many."""
        n = len(self._turns) - self.max_turns if self.max_turns > 0 else 0
        if n <= 0:
            return 0
        del self._turns[:n]
        if self._open is not None and self._open not in self._turns:
            self._open = None
        if not self._dropped_noted and self.note is not None:
            self._dropped_noted = True
            self.note(f"Oldest file checkpoint dropped (keeping {self.max_turns} turns) — "
                      f"raise /undo-max, or /keep to accept the changes.")
        return n

    def set_max_turns(self, n: int) -> int:
        """Change the cap; returns how many checkpoints that dropped now."""
        with self._lock:
            self.max_turns = n
            noted, self._dropped_noted = self._dropped_noted, True   # the caller says it
            dropped = self._trim()
            self._dropped_noted = noted
            if dropped:
                self._save()
                self._gc()
            return dropped

    def would_drop(self, n: int) -> int:
        with self._lock:
            return max(0, len(self._turns) - n) if n > 0 else 0

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.workdir))
        except ValueError:
            return str(p)

    # ── reading (the frontends) ─────────────────────────────────────────────

    def count(self) -> int:
        with self._lock:
            return len(self._turns)

    def files_changed(self) -> int:
        """Distinct files in the stack (the Changes button's badge)."""
        with self._lock:
            return len({k for t in self._turns for k in t["files"]})

    @staticmethod
    def _renames(t: dict) -> dict[str, str]:
        """dst -> src for the moves in t that are still a plain rename: the
        source was there before and is gone after."""
        out = {}
        for src, dst in t.get("moves", []):
            s, d = t["files"].get(src), t["files"].get(dst)
            if (s is not None and d is not None and s["before"].get("kind") != "absent"
                    and (s.get("after") or {}).get("kind") == "absent"
                    and d["before"].get("kind") == "absent"):
                out[dst] = src
        return out

    @staticmethod
    def _special(t: dict) -> dict[str, tuple[str, str]]:
        """Moves that can only be undone by moving back (a folder, or a file too
        big to save): every key of such a pair -> (src, dst)."""
        out = {}
        for dst, src in CheckpointStore._renames(t).items():
            b = t["files"][src]["before"]
            if b.get("kind") == "dir" or b.get("big"):
                out[src] = out[dst] = (src, dst)
        return out

    def turns(self) -> list[dict]:
        """The stack for the Changes menu, newest first: [{id, turn, label, step,
        plan, ts, ran_commands, files: [{path, rel, op, from?, added, removed,
        restorable}]}]; op is A(dded), M(odified), D(eleted) or R(enamed)."""
        with self._lock:
            out = []
            for t in reversed(self._turns):
                renames = self._renames(t)
                hidden = set(renames.values())
                special = self._special(t)
                files = []
                for key, e in t["files"].items():
                    if key in hidden:
                        continue            # shown as the rename on its destination
                    src = t["files"][renames[key]] if key in renames else None
                    before = src["before"] if src else e["before"]
                    after = e.get("after") or {}
                    files.append(self._row(e, key, before, after, self._show(after),
                                           src=src, restorable=key in special
                                           or _restorable(before)))
                out.append({k: t.get(k) for k in ("id", "turn", "label", "step", "plan", "ts",
                                                  "ran_commands")} | {"files": files})
            return out

    def _row(self, e: dict, key: str, before: dict, after: dict, new_text: str,
             src: dict | None = None, restorable: bool = True) -> dict:
        b, a = before.get("kind"), after.get("kind")
        op = "R" if src else "A" if b == "absent" else "D" if a == "absent" else "M"
        row = {"path": key, "rel": e["rel"], "op": op, "restorable": restorable,
               "added": 0, "removed": 0}
        if src:
            row["from"] = src["rel"]
        if _textual(before) and _textual(after):
            _, row["added"], row["removed"] = build_diff_body(self._show(before), new_text)
        return row

    def _firsts(self) -> dict[str, dict]:
        """Each file's entry in the oldest checkpoint that has it."""
        first: dict[str, dict] = {}
        for t in self._turns:
            for key, e in t["files"].items():
                first.setdefault(key, e)
        return first

    def cumulative(self) -> list[dict]:
        """Net change per file since the keep point: its oldest pre-image
        against the disk now.  Files back as they were are left out."""
        with self._lock:
            rows = []
            for key, e in self._firsts().items():
                now = self._peek(Path(key))
                if _same(e["before"], now):
                    continue
                rows.append(self._row(e, key, e["before"], now, self._live(Path(key), now),
                                      restorable=_restorable(e["before"])))
            return sorted(rows, key=lambda r: r["rel"])

    def _peek(self, p: Path) -> dict:
        """What is at p now, like _image, but hashed instead of saved."""
        return _image(p, _sha)

    def _live(self, p: Path, img: dict) -> str:
        """The text of what is at p now (img = _peek(p)), for a diff."""
        if img.get("kind") != "file" or img.get("big"):
            return self._show(img)
        return _text(_read(p))

    def diff(self, path: str, checkpoint: int | None = None) -> dict | None:
        """{rel, op, old, new, ...} for one file: what one checkpoint changed, or
        with checkpoint None its net change since the keep point (to the disk)."""
        with self._lock:
            if checkpoint is None:
                e = self._firsts().get(path)
                if e is None:
                    return None
                now = self._peek(Path(path))
                new = self._live(Path(path), now)
                row = self._row(e, path, e["before"], now, new,
                                restorable=_restorable(e["before"]))
                return {**row, "old": self._show(e["before"]), "new": new}
            t = next((t for t in self._turns if t["id"] == checkpoint), None)
            if t is None or path not in t["files"]:
                return None
            e = t["files"][path]
            renames = self._renames(t)
            src = t["files"][renames[path]] if path in renames else None
            before, after = (src or e)["before"], e.get("after") or {}
            new = self._show(after)
            row = self._row(e, path, before, after, new, src=src,
                            restorable=path in self._special(t) or _restorable(before))
            return {**row, "checkpoint": checkpoint, "old": self._show(before), "new": new}

    def _show(self, img: dict) -> str:
        kind = img.get("kind")
        if kind == "link":
            return f"(symlink → {img.get('target')})\n"
        if kind == "dir":
            return "(folder)\n"
        if kind == "absent" or kind is None:
            return ""
        if img.get("big"):
            return f"(file too big to save: {img.get('size', 0):,} bytes)\n"
        return _text(self._get_blob(img.get("blob")))

    # ── undo / keep ─────────────────────────────────────────────────────────

    def _from(self, checkpoint: int | None) -> list[dict]:
        """The checkpoints undo_to(checkpoint) takes back, newest first."""
        if not self._turns:
            return []
        if checkpoint is None:
            return [self._turns[-1]]
        if not any(t["id"] == checkpoint for t in self._turns):
            return []
        return [t for t in reversed(self._turns) if t["id"] >= checkpoint]

    def _units(self, t: dict) -> list[tuple[str, ...]]:
        """What undoing t does, in the order to do it (newest change first): a
        key to restore from its pre-image, or a (src, dst) move to reverse."""
        special = self._special(t)
        units, seen = [], set()
        for key in reversed(list(t["files"])):
            if key in special:
                pair = special[key]
                if pair not in seen:
                    seen.add(pair)
                    units.append(pair)
            else:
                units.append((key,))
        return units

    def _check(self, t: dict, unit: tuple[str, ...], sim: dict | None = None) -> str:
        """'' when unit can be undone now, else "conflict" or "lost".  sim: the
        images a preview has undone so far, in place of the disk."""
        def now(key):
            return sim[key] if sim is not None and key in sim else self._peek(Path(key))
        if len(unit) == 2:
            if now(unit[0]).get("kind") != "absent":
                return "conflict"
            return "" if _same(now(unit[1]), t["files"][unit[1]].get("after") or {}) \
                else "conflict"
        e = t["files"][unit[0]]
        if not _restorable(e["before"]):
            return "lost"
        return "" if _same(now(unit[0]), e.get("after") or {}) else "conflict"

    def preview(self, checkpoint: int | None = None) -> dict:
        """What undo_to(checkpoint) would do (None: the newest checkpoint),
        without doing it: {ids, restore, conflicts, lost: [rel], ran_commands}."""
        with self._lock:
            todo = self._from(checkpoint)
            out: dict[str, list] = {"restore": [], "conflicts": [], "lost": []}
            sim: dict[str, dict] = {}       # what the newer checkpoints' undo leaves
            for t in todo:
                for unit in self._units(t):
                    state = self._check(t, unit, sim)
                    rel = t["files"][unit[-1]]["rel"]
                    if not state:
                        for k in unit:
                            sim[k] = t["files"][k]["before"]
                    out["restore" if not state else "conflicts" if state == "conflict"
                        else "lost"].append(rel)
            return {"ids": [t["id"] for t in todo],
                    **{k: _unique(v) for k, v in out.items()},
                    "ran_commands": any(t.get("ran_commands") for t in todo)}

    def undo_to(self, checkpoint: int | None = None, force: bool = False) -> dict:
        """Put back the files of checkpoint and every later one (None: only the
        newest), newest first.  A file changed since momo wrote it is skipped
        unless force: it stays in the stack, so a forced undo can still take it
        back.  Returns {restored, skipped: [rel], failed: [(rel, why)], ids:
        [checkpoints taken back completely]}."""
        with self._lock:
            restored, skipped, failed, done = [], [], [], []
            for t in self._from(checkpoint):
                for unit in self._units(t):
                    rel = t["files"][unit[-1]]["rel"]
                    state = self._check(t, unit)
                    if state == "lost":
                        b = t["files"][unit[0]]["before"]
                        failed.append((rel, "a folder" if b.get("kind") == "dir"
                                       else "too big to save"))
                        del t["files"][unit[0]]
                        continue
                    if state == "conflict" and (not force or len(unit) == 2):
                        skipped.append(rel)
                        continue
                    if len(unit) == 2:
                        err = (_move_back(Path(unit[0]), Path(unit[1]))
                               if all(self._inside(k) for k in unit)
                               else "outside the working directory")
                    else:
                        err = self._restore(Path(unit[0]), t["files"][unit[0]]["before"])
                    if err:
                        failed.append((rel, err))
                        continue
                    restored.append(t["files"][unit[0]]["rel"])
                    for k in unit:
                        del t["files"][k]
                if not t["files"]:
                    done.append(t["id"])
            self._drop_empty()
            return {"restored": _unique(restored), "skipped": _unique(skipped),
                    "failed": failed, "ids": done}

    def _drop_empty(self) -> None:
        for t in self._turns:
            t["moves"] = [m for m in t.get("moves", []) if all(k in t["files"] for k in m)]
        self._turns = [t for t in self._turns if t["files"]]
        if self._open is not None and self._open not in self._turns:
            self._open = None
        self._save()
        self._gc()

    def _restore(self, p: Path, img: dict) -> str:
        """Make p what img describes; '' or why it could not."""
        kind = img.get("kind")
        if not self._inside(str(p)):
            return "outside the working directory"
        try:
            if p.is_dir() and not p.is_symlink():
                if kind != "absent":
                    return "a folder is there now"
                try:
                    p.rmdir()               # only ever an empty folder
                except OSError:
                    return "a folder that is not empty is there now"
                return ""
            if kind == "absent":
                if p.is_symlink() or p.exists():
                    p.unlink()
                return ""
            p.parent.mkdir(parents=True, exist_ok=True)
            if kind == "link":
                if p.is_symlink() or p.exists():
                    p.unlink()
                os.symlink(img.get("target", ""), p)
                return ""
            data = self._get_blob(img.get("blob"))
            if data is None:
                return "its saved copy is missing"
            if p.is_symlink():
                p.unlink()
            tmp = p.with_name(f".{p.name}.momo-undo.tmp")
            try:
                tmp.write_bytes(data)
                os.chmod(tmp, int(img.get("mode", 0o644)) & 0o777)
                os.replace(tmp, p)
            finally:
                tmp.unlink(missing_ok=True)
            return ""
        except OSError as e:
            return e.strerror or str(e)

    def revert_file(self, path: str, force: bool = False) -> str:
        """Put one file back as it was at the keep point and drop it from every
        checkpoint (with the source of a rename that made it).  '' or why not."""
        with self._lock:
            hits = [t for t in self._turns if path in t["files"]]
            if not hits:
                return "not changed since the last /keep"
            first, last = hits[0]["files"][path], hits[-1]["files"][path]
            partners = [s for t in hits for d, s in self._renames(t).items() if d == path]
            if not _restorable(first["before"]) or any(path in self._special(t) for t in hits):
                return "it can only go back with /undo (a folder, or a file too big to save)"
            if not force and not _same(self._peek(Path(path)), last.get("after") or {}):
                return "changed since momo wrote it — revert with force to overwrite"
            err = self._restore(Path(path), first["before"])
            if err:
                return err
            for t in hits:
                del t["files"][path]
            self._drop_empty()
            for src in partners:
                if any(src in t["files"] for t in self._turns):
                    self.revert_file(src, force)
            return ""

    def keep(self) -> tuple[int, int]:
        """Accept every change: forget all checkpoints and their saved copies.
        Returns (checkpoints, files) it made final."""
        with self._lock:
            n, files = len(self._turns), self.files_changed()
            self._turns, self._open = [], None
            self._dropped_noted = False
            if self._root is not None:
                shutil.rmtree(self._root, ignore_errors=True)
                self._root = None
            return n, files


def _image(p: Path, blob) -> dict:
    """What is at p now: {"kind": "absent" | "file" | "dir" | "link", ...};
    blob(bytes) -> id names a file's contents (saved, or only hashed)."""
    try:
        st = p.lstat()
    except OSError:
        return {"kind": "absent"}
    if stat_mod.S_ISLNK(st.st_mode):
        try:
            return {"kind": "link", "target": os.readlink(p)}
        except OSError:
            return {"kind": "link", "target": ""}
    if stat_mod.S_ISDIR(st.st_mode):
        return {"kind": "dir"}
    mode = stat_mod.S_IMODE(st.st_mode)
    data = _read(p) if st.st_size <= MAX_FILE_BYTES else None
    if data is None:
        return {"kind": "file", "big": True, "size": st.st_size, "mode": mode,
                "sig": f"{st.st_size}:{st.st_mtime_ns}"}
    return {"kind": "file", "blob": blob(data), "size": len(data), "mode": mode}


def _restorable(img: dict) -> bool:
    """Whether img can be put back from what was saved."""
    return img.get("kind") != "dir" and not img.get("big")


def _textual(img: dict) -> bool:
    """Whether img has text to diff (nothing, for a file that is not there)."""
    return img.get("kind") in ("file", "absent") and not img.get("big")


def _move_back(src: Path, dst: Path) -> str:
    try:
        src.parent.mkdir(parents=True, exist_ok=True)
        dst.rename(src)
    except OSError as e:
        return e.strerror or str(e)
    return ""


def _same(a: dict, b: dict) -> bool:
    """Whether two images describe the same thing on disk."""
    if a.get("kind") != b.get("kind"):
        return False
    kind = a.get("kind")
    if kind == "link":
        return a.get("target") == b.get("target")
    if kind == "file":
        if a.get("big") or b.get("big"):
            return a.get("sig") == b.get("sig")
        return a.get("blob") == b.get("blob") and a.get("mode", 0) == b.get("mode", 0)
    return True


def _read(p: Path) -> bytes | None:
    try:
        return p.read_bytes()
    except OSError:
        return None


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def prune_stale(base: Path, current: Path | None = None) -> None:
    """Remove other sessions' checkpoint folders under base not written for a while."""
    if base.is_symlink() or not base.is_dir():
        return
    cutoff = time.time() - STALE_SECS
    for d in base.iterdir():
        try:
            if d.is_dir() and d != current and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def describe(plan: dict, force: bool = False) -> str:
    """A preview() as the lines of a confirmation question."""
    lines = []
    if plan["restore"]:
        lines.append("Restore: " + ", ".join(plan["restore"]))
    if plan["conflicts"]:
        lines.append(("Changed since momo wrote them — overwritten: " if force else
                      "Changed since momo wrote them — skipped (/undo force overwrites): ")
                     + ", ".join(plan["conflicts"]))
    if plan["lost"]:
        lines.append("Can't be restored (a folder or a file over 5 MB): " + ", ".join(plan["lost"]))
    if plan.get("ran_commands"):
        lines.append("Note: these turns ran commands — what they changed is not tracked.")
    return "\n".join(lines)
