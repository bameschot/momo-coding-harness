"""The compaction archive: what context compaction took out of the history, kept
so the full export (/export full, web "Download full log") can show it in place.

Each record is
    {"pos": int, "kind": "removed" | "trimmed", "n": int, "time": iso, "message": {...}}
in the coordinates of the current history:
  removed  the message stood just before messages[pos]
  trimmed  message is the untrimmed original of messages[pos]
`n` numbers the compaction that made the record.  The list is kept in
transcript order, so records at the same pos render in list order.
"""
from __future__ import annotations

from bisect import bisect_left

# Part of the marker Harness._compact writes into a trimmed tool result.
TRIM_MARKER = "removed by context compaction"


def archive_compaction(archive: list[dict], msgs: list[dict], drop: set[int],
                       trims: dict[int, str], keep_idx: list[int],
                       n: int, time: str) -> list[dict]:
    """The archive after a compaction of `msgs` that drops `drop`, trims `trims`
    and keeps `keep_idx` (ascending).  Call it before the trims are written into
    `msgs`: a trimmed record copies the message as it is now."""
    trimmed_at = {r["pos"]: r for r in archive if r["kind"] == "trimmed"}
    items: list[tuple[tuple, dict]] = []
    for seq, r in enumerate(archive):
        if r["kind"] == "removed":
            items.append(((r["pos"], 0, seq), dict(r)))
        elif r["pos"] in drop:
            # Trimmed earlier, dropped now: the record already holds the original.
            items.append(((r["pos"], 1), {**r, "kind": "removed", "n": n, "time": time}))
        else:
            items.append(((r["pos"], 1), dict(r)))
    for k in sorted(drop):
        if k not in trimmed_at:
            items.append(((k, 1), {"pos": k, "kind": "removed", "n": n, "time": time,
                                   "message": msgs[k]}))
    for k in sorted(trims):
        if k not in trimmed_at:   # trimmed again: keep the first, full original
            items.append(((k, 1), {"pos": k, "kind": "trimmed", "n": n, "time": time,
                                   "message": dict(msgs[k])}))
    items.sort(key=lambda it: it[0])
    out = []
    for _, r in items:
        r["pos"] = bisect_left(keep_idx, r["pos"])
        out.append(r)
    return out


def _note(r: dict) -> str:
    when = r.get("time", "")[11:16]
    when = f" ({when})" if when else ""
    if r["kind"] == "trimmed":
        return f"full original; trimmed in context by compaction #{r['n']}{when}"
    return f"removed from context by compaction #{r['n']}{when}"


def full_transcript(messages: list[dict], archive: list[dict]) -> list[tuple[dict, str | None]]:
    """`messages` with the archive merged back in, as (message, note) pairs; note
    is None for a message that is still in the history unchanged."""
    before: dict[int, list[dict]] = {}
    replaces: dict[int, dict] = {}
    tail: list[dict] = []
    for r in archive:
        pos = r["pos"]
        if pos >= len(messages):   # the history was cut back since (/retry)
            tail.append(r)
        elif r["kind"] == "trimmed" and TRIM_MARKER in str(messages[pos].get("content") or ""):
            replaces[pos] = r
        else:   # removed, or a trimmed record no longer lined up with its message
            before.setdefault(pos, []).append(r)
    out: list[tuple[dict, str | None]] = []
    for i, m in enumerate(messages):
        out += [(r["message"], _note(r)) for r in before.get(i, ())]
        r = replaces.get(i)
        out.append((r["message"], _note(r)) if r else (m, None))
    out += [(r["message"], _note(r)) for r in tail]
    return out
