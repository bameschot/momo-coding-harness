import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .paths import momo_home

# Overrides (tests patch them); None: under momo_home(), resolved per call so a
# HOME changed after import (tests) keeps sessions out of the real folder.
SESSION_DIR: Path | None = None
_PREFS_PATH: Path | None = None
_prefs_lock = threading.Lock()     # save_prefs is called from the TUI, web and worker threads


def session_dir() -> Path:
    return SESSION_DIR or momo_home() / "sessions"


def prefs_path() -> Path:
    return _PREFS_PATH or momo_home() / "prefs.json"


def atomic_write(path: Path, *chunks: str | bytes, private_dir: bool = False) -> None:
    """Write via a temp file and rename, so a crash never leaves a truncated
    file; 0600, since sessions, prefs and the code index hold file contents and
    conversations.  private_dir also makes the folder 0700."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if private_dir:
        os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            for chunk in chunks:
                f.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_prefs() -> dict:
    try:
        return json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_prefs(**kwargs) -> None:
    with _prefs_lock:
        prefs = load_prefs()
        prefs.update(kwargs)
        atomic_write(prefs_path(), json.dumps(prefs, indent=2))


def new_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def session_path(ts: str) -> Path:
    folder = session_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{ts}.json"


def save(ts: str, model: str, mode: str, workdir: Path,
         messages: list[dict], context_limit: int,
         active_skills: list[str] | None = None,
         input_history: list[str] | None = None,
         context_pct: int | None = None,
         context_fixed: bool = False,
         host: str | None = None,
         provider: str | None = None,
         plan: dict | None = None,
         plan_phase: str | None = None,
         plan_kind: str = "normal",
         momo_lines: list[str] | None = None,
         momo_recap_turn: int = 0,
         turn_count: int = 0,
         disabled_tools: dict[str, list[str]] | None = None,
         loaded_tools: list[str] | None = None,
         title: str = "",
         title_turn: int = 0,
         title_locked: bool = False,
         compacted: list[dict] | None = None,
         summary_state: dict | None = None):
    data = {
        "created_at": ts,
        "model": model,
        "host": host,
        "provider": provider,
        "mode": mode,
        "workdir": str(workdir),
        "context_limit": context_limit,
        "context_pct": context_pct,
        "context_fixed": context_fixed,
        "active_skills": active_skills or [],
        "input_history": input_history or [],
        "plan": plan,
        "plan_phase": plan_phase,
        "plan_kind": plan_kind,
        "momo_lines": momo_lines or [],
        "momo_recap_turn": momo_recap_turn,
        "turn_count": turn_count,
        "disabled_tools": disabled_tools or {},
        "loaded_tools": loaded_tools or [],
        "title": title,
        "title_turn": title_turn,
        "title_locked": title_locked,
        "messages": messages,
        "compacted": compacted or [],
        "summary_state": summary_state or {},
    }
    atomic_write(session_path(ts), json.dumps(data, indent=2))


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def list_sessions() -> list[Path]:
    folder = session_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return sorted(folder.glob("*.json"), reverse=True)


def _has_conversation(path: Path) -> bool:
    try:
        msgs = load(path).get("messages") or []
    except (OSError, ValueError, AttributeError):
        return False
    return any(isinstance(m, dict) and m.get("role") == "user" for m in msgs)


def latest_session() -> Path | None:
    """The newest saved session that has a conversation (a user message);
    unreadable files and settings-only sessions are skipped."""
    return next((p for p in list_sessions() if _has_conversation(p)), None)


_WORKSPACES_KEPT = 50   # workspace -> last session entries kept in prefs


def mark_current(ts: str, workdir: Path) -> None:
    """Remember the session in use and its workspace, for restore_target() on
    the next launch: overall, and as that workspace's last session.  A session
    belongs to one workspace: another one's entry for it (it moved with
    /workspace) is dropped, so that workspace falls back to its newest session."""
    key = str(workdir)
    per = {k: v for k, v in (load_prefs().get("workspace_sessions") or {}).items()
           if isinstance(k, str) and isinstance(v, str) and k != key and v != ts}
    per[key] = ts
    per = dict(list(per.items())[-_WORKSPACES_KEPT:])
    save_prefs(last_session=ts, last_workdir=key, workspace_sessions=per)


def forget(ts: str) -> None:
    """Delete a session's saved conversation (not its log): /clear."""
    if _NAME_RE.match(ts):
        (session_dir() / f"{ts}.json").unlink(missing_ok=True)


def _saved_workdir(path: Path) -> str | None:
    try:
        wd = load(path).get("workdir")
    except (OSError, ValueError, AttributeError):
        return None
    return wd if isinstance(wd, str) else None


def _usable(ts, workdir: str | None = None) -> Path | None:
    """ts's session file when it has a conversation (and, given a workdir, was
    saved in it); None otherwise."""
    if not isinstance(ts, str) or not _NAME_RE.match(ts):
        return None
    path = session_dir() / f"{ts}.json"
    if not path.is_file() or not _has_conversation(path):
        return None
    if workdir is not None and _saved_workdir(path) != workdir:
        return None
    return path


def restore_target(workdir: Path | None = None) -> tuple[Path | None, Path | None]:
    """What a launch reopens: (the session file to load or None, the workspace
    to use or None).

    Without a workdir: the session in use when momo last ran, in the workspace
    it was in, even when that session has no conversation (cleared, or new and
    never used): the launch then starts empty there rather than reviving an
    older session and its plan.  With a workdir: that workspace's last session,
    or none (an empty one, or one /workspace moved elsewhere, counts as none).
    Prefs from before these were kept fall back to the newest session (in that
    workspace, given one)."""
    prefs = load_prefs()
    if workdir is None:
        wd = prefs.get("last_workdir")
        remembered = Path(wd) if isinstance(wd, str) and Path(wd).is_dir() else None
        if "last_session" in prefs:
            return _usable(prefs.get("last_session")), remembered
        return latest_session(), remembered
    key = str(workdir)
    per = prefs.get("workspace_sessions")
    if isinstance(per, dict) and key in per:
        return _usable(per[key], key), workdir
    return next((p for p in list_sessions()
                 if _has_conversation(p) and _saved_workdir(p) == key), None), workdir


def find_session(name: str) -> Path | None:
    """Resolve a session by exact filename stem or partial match."""
    sessions = list_sessions()
    exact = next((p for p in sessions if p.stem == name or p.name == name), None)
    return exact or next((p for p in sessions if p.stem.startswith(name)), None)


# A session name is a file stem in session_dir(): no separators, no leading dot,
# so a name can never point outside the folder.
_NAME_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")


def delete_sessions(names: list[str], current: str | None) -> tuple[list[str], list[dict]]:
    """Delete saved sessions by exact name, with their .log files.  The session
    that is open (`current`) is never deleted.  Returns (deleted names,
    [{"name", "reason"}] for the ones kept)."""
    deleted: list[str] = []
    skipped: list[dict] = []
    for name in names:
        if not isinstance(name, str) or not _NAME_RE.match(name) or ".." in name:
            skipped.append({"name": str(name), "reason": "not a session name"})
            continue
        if name == current:
            skipped.append({"name": name, "reason": "it is the current session"})
            continue
        path = session_dir() / f"{name}.json"
        if not path.is_file():
            skipped.append({"name": name, "reason": "not found"})
            continue
        try:
            path.unlink()
        except OSError as e:
            skipped.append({"name": name, "reason": str(e)})
            continue
        (session_dir() / f"{name}.log").unlink(missing_ok=True)
        deleted.append(name)
    return deleted, skipped
