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

from . import think_watch

# A reply that used this share of /max-output-tokens hit that cap, not the window.
_CAP_SHARE = 0.95
# What a capped reply keeps in the history (the rest is usually a loop).
_CAPPED_KEEP = 1_500
# The cause when the cut-off text was a repetition loop: splitting is the wrong advice.
LOOP_CAUSE = "your reply started repeating itself and ran into the output limit"

_WRITE_TOOLS = ("write_file",)
# A phase or a story is one call, so "split it across calls" cannot apply (run
# 8b's planner was cut off twice writing one phase that held every command).
_PLAN_ADVICE = {
    "write_phase": ("A phase is written in one call, so make it smaller: fewer expectations, one "
                    "example each, and shorter details. If it still holds more than that, split it "
                    "in the outline: call create_plan with one more phase (the phases already "
                    "written are kept by their titles)."),
    "write_story": ("A story is written in one call, so make it smaller: shorter technical "
                    "details, one example per expectation. If it is still too big, split it into "
                    "two stories with plan_stories."),
    "create_plan": ("Send only the frame and a one-line outline (one {title, outcome} per phase), "
                    "then write each phase with write_phase."),
}
_EDIT_TOOLS = ("edit_file", "edit_files", "replace_symbol")


def cut_off_cause(max_output_tokens: int, eval_tokens: int | None, text: str = "") -> str:
    """What stopped the reply: a repetition loop (text, the reply as streamed,
    reads as one), the output cap or a full context window."""
    if text and think_watch.looping(text):
        return LOOP_CAUSE
    if max_output_tokens and eval_tokens and eval_tokens >= max_output_tokens * _CAP_SHARE:
        return f"your reply hit the {max_output_tokens:,}-token output limit"
    return "the context window filled up"


def trim_capped_reply(text: str) -> str:
    """The head of a reply the output cap cut off, for the history: the whole of
    it (up to the cap, often one sentence repeated) primes the next reply to
    repeat it too."""
    if len(text) <= _CAPPED_KEEP:
        return text
    return (text[:_CAPPED_KEEP].rstrip()
            + f"\n[… reply cut off: {len(text) - _CAPPED_KEEP:,} more characters not kept]")


def retry_advice(name: str, tool_names, cause: str = "") -> str:
    """How to make a call to `name` small enough to fit in one reply, or, after
    a loop, how to make it at all."""
    if cause == LOOP_CAUSE:
        return ("Call the tool once, directly, with the real arguments: do not write code or "
                "tool calls out as text in your reply, and do not repeat any part of it.")
    if name in _WRITE_TOOLS:
        if "append_to_file" in tool_names:
            return ("Write the file in parts: write_file with the first part, then "
                    "append_to_file for each next part, about 150 lines per call.")
        return "Write a shorter file, or leave out what can be added later."
    if name in _EDIT_TOOLS:
        return ("Make the change in smaller edits: one change per call, with the text "
                "to replace only as long as it needs to be to be unique.")
    if name in _PLAN_ADVICE and (name != "create_plan" or "write_phase" in tool_names):
        return _PLAN_ADVICE[name]                # create_plan: a stories plan's only
    return "Retry with smaller arguments, splitting the work across several calls."


def settled_result(kind: str, name: str, tool_names=(), cause: str = "", error: str = "") -> str:
    """The tool result for a call that was not run.  kind: "truncated" (cut off,
    `cause` from cut_off_cause), "malformed" (`error`: the JSON error) or
    "interrupted"."""
    if kind == "truncated":
        return (f"ERROR: not run — {cause} before this {name} call was complete, so it "
                f"was never run and nothing changed. Do not repeat it at the same size. "
                + retry_advice(name, tool_names, cause))
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
            f"it at the same size. " + retry_advice(name, tool_names, cause))
