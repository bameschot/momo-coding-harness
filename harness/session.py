import json
from datetime import datetime, timezone
from pathlib import Path


SESSION_DIR = Path.home() / ".momo-harness" / "sessions"


def new_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def session_path(ts: str) -> Path:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR / f"{ts}.json"


def save(ts: str, model: str, mode: str, workdir: Path,
         messages: list[dict], context_limit: int,
         active_skills: list[str] | None = None):
    data = {
        "created_at": ts,
        "model": model,
        "mode": mode,
        "workdir": str(workdir),
        "context_limit": context_limit,
        "active_skills": active_skills or [],
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
