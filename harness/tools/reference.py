"""The tool reference in every role's system prompt, generated from the schemas
of exactly the tools the role is offered, so it can never drift from the real
tools.  (Each role .md file once carried its own hand-copied table, kept in
sync by hand across five files.)"""
from __future__ import annotations

import json
import re

from . import registry

_TOOL_REFERENCE_INTRO = (
    "## Tool reference\n\n"
    "Use the function-calling API when available. If not, output calls in this format — "
    "the harness detects and executes them automatically:\n\n"
    "```\n"
    '<tool_call>{"name": "tool_name", "arguments": {"param": "value"}}</tool_call>\n'
    "```\n\n"
    "**Argument order matters.** Pass arguments in the order shown in each tool's signature. "
    "For file writes especially, put `path` before `content` — some models drop a trailing "
    "`path` after a large `content` value, and a `write_file`/`append_to_file` call without "
    "`path` fails."
)

_TOOL_REFERENCE_INTRO_COMPACT = (
    "## Tool reference\n\n"
    "Each tool's full description and parameters come with the function-calling API. "
    "If that is not available, output calls in the format of the examples below — the "
    "harness detects and executes them. Pass arguments in the order shown; for file "
    "writes put `path` before `content`."
)

TOOL_REF_STYLES = ("full", "compact")


def example_args(tool: dict) -> dict:
    """A tool's example arguments: the curated one its file declares (for the
    variant shown), else its required parameters with placeholder values."""
    example = registry.example_for(tool)
    if example is not None:
        return example
    params = tool["function"]["parameters"]
    props = params.get("properties", {})
    out: dict = {}
    for p in params.get("required", []):
        ptype = props.get(p, {}).get("type", "string")
        out[p] = {"boolean": True, "integer": 1}.get(ptype, f"<{p}>")
    return out


def _first_sentence(text: str) -> str:
    """The description up to its first sentence end, which is where every tool
    names what it is for (see the evals note on putting affordances first)."""
    m = re.search(r"[.!?](\s|$)", text)
    return text[:m.start() + 1] if m else text


def render_tool_reference(tool_list: list[dict], style: str = "full") -> str:
    """Render the Markdown tool reference for exactly the given tools. The harness
    calls this per mode with that mode's tool set, so each role sees a reference
    covering precisely the tools it actually has.

    style="compact" gives one line per tool (signature, first sentence, example):
    the full text already reaches the model in the native tool schemas, so the
    full reference repeats it, and the prompt only needs enough for a model that
    writes text-format calls to copy."""
    if style == "compact":
        return _render_compact_reference(tool_list)
    sections = [_TOOL_REFERENCE_INTRO]
    for tool in tool_list:
        fn = tool["function"]
        name = fn["name"]
        desc = fn.get("description", "").strip()
        params = fn["parameters"]
        props = params.get("properties", {})
        required = set(params.get("required", []))
        lines = [f"**{name}** — {desc}", ""]
        if props:
            lines.append("| Parameter | Type | Required | Notes |")
            lines.append("|-----------|------|----------|-------|")
            for pname, pspec in props.items():
                ptype = pspec.get("type", "string")
                req = "yes" if pname in required else "no"
                note = (pspec.get("description") or "").strip() or "—"
                lines.append(f"| `{pname}` | {ptype} | {req} | {note} |")
            lines.append("")
        example = json.dumps({"name": name, "arguments": example_args(tool)},
                             ensure_ascii=False)
        lines.append(f"Example: `<tool_call>{example}</tool_call>`")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _render_compact_reference(tool_list: list[dict]) -> str:
    entries = []
    for tool in tool_list:
        fn = tool["function"]
        params = fn["parameters"]
        required = set(params.get("required", []))
        sig = ", ".join(p if p in required else p + "?" for p in params.get("properties", {}))
        example = json.dumps({"name": fn["name"], "arguments": example_args(tool)},
                             ensure_ascii=False)
        entries.append(f"- **{fn['name']}**({sig}) — {_first_sentence(fn.get('description', '').strip())}\n"
                       f"  `<tool_call>{example}</tool_call>`")
    return _TOOL_REFERENCE_INTRO_COMPACT + "\n\n" + "\n".join(entries)
