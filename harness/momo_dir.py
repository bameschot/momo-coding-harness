"""The workspace's own harness folder, <workdir>/.momo/.

It holds the model's scratchpad (scratch/), the plan file (plan.md), the
run_command logs (runs/<session>/), and the workspace tools (tools/) with their
settings (tools.json).  A `.gitignore` inside it keeps everything but the
tools out of git without touching a tracked file; the tools are meant to be
committed.  The code index, grep_files and find_files never look inside
(paths.SKIP_DIRS, ignore_rules).

Only data that is harmless when a cloned repository ships its own copy, or the
model overwrites it, belongs here: approvals, pickles, keys and command config
stay in paths.momo_home().  The one exception is the workspace tools, which are
code: they run only after the user approved each file, and that approval is
kept in paths.momo_home() (tools/workspace.py).

A leaf module (imports only paths), so plan, run_store and tools can use it.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .paths import safe_path

MOMO_DIR = ".momo"
SCRATCH = "scratch"
RUNS = "runs"
PLAN = "plan.md"
SCRATCH_REL = f"{MOMO_DIR}/{SCRATCH}"
TOOLS = "tools"
TOOLS_SETTINGS = "tools.json"
PLAN_REL = f"{MOMO_DIR}/{PLAN}"
_GITIGNORE = (
    "# Created by momo: harness state and the model's scratchpad, never committed;\n"
    "# workspace tools and their settings are meant to be committed.\n"
    "*\n"
    "!tools/\n"
    "!tools/**\n"
    "!tools.json\n"
    "tools/.venv/\n"
    "__pycache__/\n")
# What ensure() wrote before workspace tools existed: upgraded when found as is.
_OLD_GITIGNORE = "# Created by momo: harness state and the model's scratchpad, never committed.\n*\n"


def momo_root(workdir: Path) -> Path:
    return workdir / MOMO_DIR


def scratch_root(workdir: Path) -> Path:
    return workdir / MOMO_DIR / SCRATCH


def runs_root(workdir: Path) -> Path:
    return workdir / MOMO_DIR / RUNS


def tools_root(workdir: Path) -> Path:
    return workdir / MOMO_DIR / TOOLS


def tools_settings_path(workdir: Path) -> Path:
    return workdir / MOMO_DIR / TOOLS_SETTINGS


def plan_path(workdir: Path) -> Path:
    return workdir / MOMO_DIR / PLAN


def ensure(workdir: Path, sub: str = "") -> Path | str:
    """Create .momo/<sub> (and .momo/.gitignore) and return it, or an ERROR
    string.  A symlinked .momo or sub folder is refused: it would make every
    "inside the scratchpad" check pass for wherever the link points."""
    root = workdir.resolve()
    target = root / MOMO_DIR
    try:
        for d in (target, *([target / sub] if sub else [])):
            if d.is_symlink():
                return f"ERROR: {d.relative_to(root)} is a symlink — momo only uses a real folder there"
            d.mkdir(exist_ok=True)
            if d.resolve().parent != (root if d == target else target.resolve()):
                return f"ERROR: {d.relative_to(root)} is not inside the working directory"
        ignore = target / ".gitignore"
        if not ignore.is_symlink():
            if not ignore.exists() or ignore.read_text(encoding="utf-8") == _OLD_GITIGNORE:
                ignore.write_text(_GITIGNORE, encoding="utf-8")
    except OSError as e:
        return f"ERROR: could not create {MOMO_DIR}/{sub}: {e.strerror or e}"
    return target / sub if sub else target


def touches(raw: str, workdir: Path) -> bool:
    """Whether a tool's path argument names something under .momo/."""
    p = safe_path(raw, workdir) if isinstance(raw, str) and raw else None
    if not isinstance(p, Path):
        return False
    try:
        rel = p.relative_to(workdir.resolve())
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] == MOMO_DIR


def resolve_scratch(raw: str, workdir: Path) -> Path | str:
    """A scratchpad path from a model's argument: relative to the scratchpad
    ("probe.py"), to the workdir (".momo/scratch/probe.py") or absolute inside
    it.  Anything that resolves outside the scratchpad is an ERROR."""
    root = scratch_root(workdir.resolve())
    raw = (raw or "").strip()
    if not raw:
        return "ERROR: path is empty — name a file inside the scratchpad, e.g. probe.py"
    if "\0" in raw:
        return "ERROR: path contains a NUL character"
    if root.parent.is_symlink() or root.is_symlink():
        return f"ERROR: {SCRATCH_REL} is (inside) a symlink — momo only uses a real folder there"
    rel = raw
    if os.path.isabs(raw):
        norm = Path(os.path.normpath(raw))
        for base in (root, scratch_root(workdir)):   # the workdir as resolved or as given
            try:
                rel = str(norm.relative_to(base))
                break
            except ValueError:
                continue
        else:
            return _outside(raw)
    else:
        norm = raw.replace("\\", "/")
        for prefix in (f"./{SCRATCH_REL}/", f"{SCRATCH_REL}/"):
            if norm.startswith(prefix):
                rel = norm[len(prefix):]
                break
    p = safe_path(rel, root)
    if isinstance(p, str) or p == root.resolve():
        return _outside(raw) if isinstance(p, str) else "ERROR: path is the scratchpad itself — name a file in it"
    return p


def _outside(raw: str) -> str:
    return (f"ERROR: {raw} is outside the scratchpad ({SCRATCH_REL}/) — scratch_write only "
            "writes there; give a path relative to it, e.g. probe.py or out/results.txt")


def scratch_usage(workdir: Path, newest: int = 10) -> tuple[int, int, list[tuple[str, int]]]:
    """(files, bytes, [(relative path, size)] newest first) of the scratchpad."""
    root = scratch_root(workdir)
    if root.is_symlink() or not root.is_dir():
        return 0, 0, []
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames:
            p = Path(dirpath) / f
            try:
                st = p.lstat()
            except OSError:
                continue
            found.append((st.st_mtime, str(p.relative_to(root)), st.st_size))
    found.sort(reverse=True)
    return len(found), sum(s for _, _, s in found), [(r, s) for _, r, s in found[:newest]]


def clear_scratch(workdir: Path) -> int:
    """Delete everything in the scratchpad (the folder itself stays); returns
    the number of entries removed.  Links are removed, never followed."""
    root = scratch_root(workdir)
    if root.is_symlink() or not root.is_dir():
        return 0
    n = 0
    for e in os.scandir(root):
        try:
            if e.is_dir(follow_symlinks=False):
                shutil.rmtree(e.path)
            else:
                os.unlink(e.path)
            n += 1
        except OSError:
            pass
    return n
