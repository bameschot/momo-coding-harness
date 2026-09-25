import json
import re
from datetime import datetime, timezone
from pathlib import Path


SESSION_DIR = Path.home() / ".momo-harness" / "sessions"
_PREFS_PATH = Path.home() / ".momo-harness" / "prefs.json"


def load_prefs() -> dict:
    try:
        return json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_prefs(**kwargs) -> None:
    _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    prefs = load_prefs()
    prefs.update(kwargs)
    _PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def new_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def session_path(ts: str) -> Path:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR / f"{ts}.json"


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
         momo_lines: list[str] | None = None,
         momo_recap_turn: int = 0,
         turn_count: int = 0):
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
        "momo_lines": momo_lines or [],
        "momo_recap_turn": momo_recap_turn,
        "turn_count": turn_count,
        "messages": messages,
    }
    session_path(ts).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def list_sessions() -> list[Path]:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(SESSION_DIR.glob("*.json"), reverse=True)


def find_session(name: str) -> Path | None:
    """Resolve a session by exact filename stem or partial match."""
    for p in list_sessions():
        if p.stem == name or p.name == name:
            return p
    # partial prefix match
    for p in list_sessions():
        if p.stem.startswith(name):
            return p
    return None


# A session name is a file stem in SESSION_DIR: no separators, no leading dot,
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
        path = SESSION_DIR / f"{name}.json"
        if not path.is_file():
            skipped.append({"name": name, "reason": "not found"})
            continue
        try:
            path.unlink()
        except OSError as e:
            skipped.append({"name": name, "reason": str(e)})
            continue
        (SESSION_DIR / f"{name}.log").unlink(missing_ok=True)
        deleted.append(name)
    return deleted, skipped
