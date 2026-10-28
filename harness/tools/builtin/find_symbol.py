"""find_symbol: where a name is defined, or which definition a line is in.

It stays while the code index is on: its line form ("which definition is line
N in?") is a one-line answer the model reliably reaches for, and hiding it
sent the 9B to read_symbol, which returns the whole body (evals, 2026-09-23:
0.1 KB / 3 s with find_symbol vs 26 KB / 13-51 s without).  It reads the index
through code_nav's provider hook, so it is just as fresh."""
from __future__ import annotations

import json
from pathlib import Path

from ... import code_nav
from ..common import CODE_NAV_LANGS, index_target
from ..registry import ALL_ROLES, Tool, tool_schema


def _index_hint(result: str, args: dict, workdir: Path, index) -> str:
    """With the code index on, a name lookup points at index_search, which also
    finds approximate names (the line form stays find_symbol's job)."""
    if index is None or result.startswith("ERROR"):
        return result
    if index_target(args.get("directory"), workdir, index) is None:
        return result
    query = str(args.get("name", ""))
    if not query or code_nav.line_query(query) is not None:
        return result
    return result + (f"\n(code index: index_search({json.dumps(query)}) finds this by approximate "
                     f"name too, and shows short values inline)")


TOOL = Tool(
    tool_schema("find_symbol",
        f"Find where a class, function or method is DEFINED, or WHICH definition a line belongs to "
        f"({CODE_NAV_LANGS}). A LINE NUMBER as the name ('1300') plus that one file gives the "
        "definition containing it in one line — the cheap answer after a stack trace or a grep "
        "hit. A name finds only real definitions, never comments or call sites. A pattern with "
        "kind LISTS them: name='*', kind='class' gives every class.",
        {"name":      {"type": "string", "description": "A line number like '1300' to get the definition containing it; or a name to find, e.g. 'send', a qualified 'Harness.send', or a pattern like '*_handler' or '*' for all"},
         "directory": {"type": "string", "description": "Where to look: a directory, or a single file. When name is a line number this MUST be the one file that line is in, e.g. 'harness/harness.py' — not the directory containing it. Default: ."},
         "kind":      {"type": "string", "description": "Only this kind: class, interface, enum, struct, trait, impl, object, function, method, ... (default: any)"}},
        ["name"]),
    run=code_nav.find_symbol, order=310, group="code nav", modes=ALL_ROLES, keeps_green=True,
    requires=lambda: code_nav.AVAILABLE, example={"name": "L1300", "directory": "src/parser.py"},
    after=_index_hint)
