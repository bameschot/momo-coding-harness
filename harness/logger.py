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
        if self._fh.closed:
            return
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

    def log_tool_call(self, mode: str, model: str, tool: str, args: dict):
        self._write({"type": "tool_call", "mode": mode, "model": model, "tool": tool, "args": args})

    def log_tool_result(self, mode: str, model: str, tool: str, result_length: int):
        self._write({"type": "tool_result", "mode": mode, "model": model, "tool": tool, "result_length": result_length})

    def log_compact(self, mode: str, model: str, removed: int, tokens_before: int, tokens_after: int):
        self._write({
            "type": "compact",
            "mode": mode,
            "model": model,
            "removed_messages": removed,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
        })

    def cost_summary(self) -> str:
        """Aggregate prompt/eval tokens from response records, grouped by mode+model."""
        from collections import defaultdict
        # (mode, model) -> [prompt_total, eval_total, call_count]
        buckets: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "response":
                        continue
                    key = (rec.get("mode", "?"), rec.get("model", "?"))
                    b = buckets[key]
                    b[0] += rec.get("prompt_tokens") or 0
                    b[1] += rec.get("eval_tokens") or 0
                    b[2] += 1
        except OSError:
            return "No log data available."

        if not buckets:
            return "No token data recorded yet."

        rows = sorted(buckets.items())
        col_w = max(len(f"{mode}/{model}") for (mode, model), _ in rows) + 2
        lines = ["Token usage this session:", ""]
        total_in = total_out = 0
        for (mode, model), (inp, out, calls) in rows:
            label = f"{mode}/{model}"
            lines.append(
                f"  {label:<{col_w}}  in: {inp:>7,}   out: {out:>7,}   "
                f"total: {inp+out:>8,}   ({calls} call{'s' if calls != 1 else ''})"
            )
            total_in += inp
            total_out += out
        if len(rows) > 1:
            lines.append("  " + "─" * (col_w + 52))
            lines.append(
                f"  {'total':<{col_w}}  in: {total_in:>7,}   out: {total_out:>7,}   "
                f"total: {total_in+total_out:>8,}"
            )
        return "\n".join(lines)

    def close(self):
        self._fh.close()

    @property
    def path(self) -> Path:
        return self._path
