"""word_count: an example custom tool for momo.

Install it by copying this file into ~/.momo-harness/tools/ (the folder and
the file must be yours and not writable by others: chmod 700 the folder,
chmod 600 the file), then run /tools reload or restart momo.  It shows up in
/tools under "custom" and in the tool reference of every mode.

A tool file declares one Tool: the schema the model sees, the function that
runs, and how the harness treats it.  The fields are documented on
harness.tools.Tool (harness/tools/registry.py); the built-in tools in
harness/tools/builtin/ are more examples.
"""
from pathlib import Path

from harness.tools import ALL_ROLES, Tool, read_text, tool_schema


def word_count(path: str, *, workdir: Path) -> str:
    # read_text keeps the path inside the working directory and refuses
    # binaries; an "ERROR: ..." string is a result the model can act on.
    got = read_text(path, workdir, refuse_binary=True)
    if isinstance(got, str):
        return got
    _, text = got
    return (f"{path}: {len(text.splitlines()):,} lines, {len(text.split()):,} words, "
            f"{len(text):,} characters")


TOOL = Tool(
    tool_schema(
        "word_count",
        # The first sentence is what a small model picks a tool by.
        "Count the lines, words and characters of a text file in the project.",
        {"path": {"type": "string", "description": "File to count, e.g. 'README.md'"}},
        ["path"]),
    run=word_count,
    modes=ALL_ROLES,        # read-only, so every mode may use it (default: coding, momo, a running plan)
    keeps_green=True,       # it changes no file: a TDD plan's passing tests stay valid
    example={"path": "README.md"},
)
