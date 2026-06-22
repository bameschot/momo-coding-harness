import json
import os
from datetime import datetime, timezone
from pathlib import Path


class Logger:
    def __init__(self, session_ts: str):
        log_dir = Path.home() / ".momo-harness" / "sessions"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._path = log_dir / f"{session_ts}.log"
        self._fh = open(self._path, "a", encoding="utf-8")

    def _write(self, record: dict):
        record["ts"] = datetime.now(timezone.utc).isoformat()
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()

    def log_request(self, mode: str, model: str, message_count: int, prompt_tokens: int | None):
        self._write({
            "type": "request",
            "mode": mode,
            "model": model,
            "message_count": message_count,
            "prompt_tokens": prompt_tokens,
        })

    def log_response(self, mode: str, model: str, prompt_tokens: int | None,
                     eval_tokens: int | None, has_tool_calls: bool):
        total = (prompt_tokens or 0) + (eval_tokens or 0) if (prompt_tokens and eval_tokens) else None
        self._write({
            "type": "response",
            "mode": mode,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "eval_tokens": eval_tokens,
            "total_tokens": total,
            "has_tool_calls": has_tool_calls,
        })

    def log_tool_call(self, mode: str, tool: str, args: dict):
        self._write({"type": "tool_call", "mode": mode, "tool": tool, "args": args})

    def log_tool_result(self, mode: str, tool: str, result_length: int):
        self._write({"type": "tool_result", "mode": mode, "tool": tool, "result_length": result_length})

    def log_compact(self, mode: str, removed: int, tokens_before: int, tokens_after: int):
        self._write({
            "type": "compact",
            "mode": mode,
            "removed_messages": removed,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
        })

    def close(self):
        self._fh.close()

    @property
    def path(self) -> Path:
        return self._path
