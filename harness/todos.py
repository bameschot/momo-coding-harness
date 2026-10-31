"""The todos tool's checklist: the model's own list for a request with several
parts, outside plan mode.  The model sends the whole list on every call (no
add/update operations: a small model gets those wrong, and resending the same
list is harmless), so the newest todos result in the history is the list.

The list is not put in the system prompt, as the plan is: it changes every few
calls, and a changed system prompt makes llama.cpp process the whole context
again.  Compaction drops old tool results, so the open items go into the
summary (summary_line) instead."""
from __future__ import annotations

MAX_ITEMS = 20
MAX_TEXT = 200
STATUSES = ("pending", "in_progress", "done")
_MARKS = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}


def validate(items) -> list[dict] | str:
    """The list to keep, or an ERROR saying what to fix.  A missing status is
    pending; an empty list clears it."""
    if not isinstance(items, list):
        return "ERROR: todos needs 'items': a list of {text, status} objects (an empty list clears it)."
    if len(items) > MAX_ITEMS:
        return (f"ERROR: todos takes at most {MAX_ITEMS} items, got {len(items)}. "
                "Merge small items, or keep only the next ones.")
    out: list[dict] = []
    for i, item in enumerate(items, 1):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            return f"ERROR: todos item {i} is not an object with 'text' and 'status'."
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            return f"ERROR: todos item {i} has no text."
        status = str(item.get("status") or "pending").strip().lower()
        if status not in STATUSES:
            return (f"ERROR: todos item {i} has status '{status}': use "
                    + ", ".join(STATUSES) + ".")
        out.append({"text": text[:MAX_TEXT], "status": status})
    if sum(1 for t in out if t["status"] == "in_progress") > 1:
        return ("ERROR: todos has more than one item in_progress. Mark the one you are working "
                "on in_progress, the others pending.")
    return out


def render(items: list[dict]) -> str:
    """The list as the model and the user see it, with a count."""
    if not items:
        return "Todo list cleared."
    lines = [f"{_MARKS.get(t['status'], '[ ]')} {t['text']}" for t in items]
    done = sum(1 for t in items if t["status"] == "done")
    lines.append(f"{done} of {len(items)} done")
    if done == len(items):
        lines.append("All items are done: tell the user what was done.")
    return "\n".join(lines)


def progress(items: list[dict]) -> str:
    """"2/5" for the status bar; "" without a list."""
    if not items:
        return ""
    return f"{sum(1 for t in items if t['status'] == 'done')}/{len(items)}"


def summary_line(items: list[dict]) -> str:
    """The list for a compaction summary while any item is open, else ""."""
    if not any(t["status"] != "done" for t in items):
        return ""
    return "## Todo list (yours, from the todos tool)\n" + render(items)
