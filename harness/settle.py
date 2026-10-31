"""Results for tool calls the harness does not run: the reply was cut off before
the call was complete, its arguments were not valid JSON, or the user
interrupted the turn before it ran.  The assistant turn with the call is already
in the history, so each still gets an answer, and that answer says why the call
did not run and what a retry should change.

The plain "missing required argument" error a cut-off call used to get sent the
model back to make the same oversized write, which was cut off again; a call
whose tool needs no arguments ran with its defaults instead.

Every result starts with "ERROR: not run" so the loop guard counts the same
cut-off made again like any other repeated failure."""
from __future__ import annotations

# A reply that used this share of /max-output-tokens hit that cap, not the window.
_CAP_SHARE = 0.95

_WRITE_TOOLS = ("write_file",)
_EDIT_TOOLS = ("edit_file", "edit_files", "replace_symbol")


def cut_off_cause(max_output_tokens: int, eval_tokens: int | None) -> str:
    """What stopped the reply: the output cap or a full context window."""
    if max_output_tokens and eval_tokens and eval_tokens >= max_output_tokens * _CAP_SHARE:
        return f"your reply hit the {max_output_tokens:,}-token output limit"
    return "the context window filled up"


def retry_advice(name: str, tool_names) -> str:
    """How to make a call to `name` small enough to fit in one reply."""
    if name in _WRITE_TOOLS:
        if "append_to_file" in tool_names:
            return ("Write the file in parts: write_file with the first part, then "
                    "append_to_file for each next part, about 150 lines per call.")
        return "Write a shorter file, or leave out what can be added later."
    if name in _EDIT_TOOLS:
        return ("Make the change in smaller edits: one change per call, with the text "
                "to replace only as long as it needs to be to be unique.")
    return "Retry with smaller arguments, splitting the work across several calls."


def settled_result(kind: str, name: str, tool_names=(), cause: str = "", error: str = "") -> str:
    """The tool result for a call that was not run.  kind: "truncated" (cut off,
    `cause` from cut_off_cause), "malformed" (`error`: the JSON error) or
    "interrupted"."""
    if kind == "truncated":
        return (f"ERROR: not run — {cause} before this {name} call was complete, so it "
                f"was never run and nothing changed. Do not repeat it at the same size. "
                + retry_advice(name, tool_names))
    if kind == "malformed":
        return (f"ERROR: not run — the arguments of this {name} call were not valid JSON "
                f"({error}), so it was never run. Retry with the arguments as one valid "
                f"JSON object.")
    return ("ERROR: not run — the user interrupted the turn before this call ran. "
            "Retry it only if the result is still needed.")


def cut_off_text_note(name: str, tool_names, cause: str) -> str:
    """The note for a call written as text that the cut-off left unfinished: it
    has no tool result, so the note goes in as a user turn, marked as the
    harness's own."""
    what = f"a {name} call" if name != "a tool" else "a tool call"
    return (f"[Note from the harness, not the user] {cause[0].upper() + cause[1:]} while you "
            f"were writing {what}, so it was never run and nothing changed. Do not repeat "
            f"it at the same size. " + retry_advice(name, tool_names))
