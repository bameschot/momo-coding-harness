"""web_search: search keyless sources and check URLs (harness/search.py).
Its `source` parameter lists the session's loaded sources (adapt)."""
from __future__ import annotations

from ... import search
from ..registry import ALL_ROLES, Tool, tool_schema


def _sources(ctx, schema: dict) -> dict:
    if ctx.search_sources is None:
        return schema
    return search.web_search_tool(schema, ctx.search_sources)


TOOL = Tool(
    tool_schema("web_search", search.WEB_SEARCH_DESCRIPTION,
        {"query":       {"type": "string",  "description": "What to look for, in plain words, e.g. \"python asyncio timeout\". For osv: '<Ecosystem>:<package>@<version>'."},
         "source":      {"type": "string",  "description": "Source name, or several comma-separated. Omit to search the defaults."},
         "urls":        {"type": "array", "items": {"type": "string"}, "description": f"Up to {search._MAX_URLS} pages you think hold the answer (official docs, a README). Each is checked: a dead one costs one line, a live one is listed first."},
         "max_results": {"type": "integer", "description": f"Results to list (default {search._DEFAULT_RESULTS}, maximum {search._MAX_RESULTS})"},
         "read":        {"type": "boolean", "description": "Also fetch the top pages and return the passages that match the query (default false)"}},
        []),
    suggests=("add_search_source",),
    run=search.web_search, order=810, group="net", modes=ALL_ROLES, needs_net=True,
    inject=("net_access", "net_max_bytes", "net_max_chars", "cancel", "search_sources"),
    adapt=_sources, windows_itself=lambda ctx: True,
    example={"query": "python asyncio wait_for timeout", "read": True,
             "urls": ["https://docs.python.org/3/library/asyncio-task.html"]})
