"""add_search_source: register a web_search source the model designed, after
a dry run.  The harness loop handles the call (Harness._add_search_source:
keeping it for later always asks the user); the executor is search.py's."""
from __future__ import annotations

from ... import search
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("add_search_source",
        "Add a search source for web_search that fits this project — e.g. docs.rs for a Rust "
        "crate, a company's Nexus/Artifactory search, a Read the Docs project's search API. "
        "First look at the API's response with fetch_url(url, json_path=...) to learn its "
        "shape, then describe it as a spec. The source is dry-run with test_query: on success "
        "it is usable at once for this session; on failure the error lists the keys found so "
        "you can fix the paths and call again with the same name. save=true asks the user to "
        "keep it for future sessions.",
        {"spec": {"type": "object", "description": (
            "JSON object: name (a-z0-9_-), description (one line), url (https template with "
            "{query} and optional {n}), results (dotted path to the result list; \"\" for the "
            "top level), title and link (dotted paths within one result; link may be a "
            "template like \"https://docs.rs/{name}\"), optional snippet, extra "
            "({label: path}, up to 5) and headers (no credentials).")},
         "test_query": {"type": "string",  "description": "A query this source should find something for"},
         "save":       {"type": "boolean", "description": "Ask the user to keep the source for future sessions (default false)"}},
        ["spec", "test_query"]),
    discoverable=True,
    run=search.add_search_source, order=820, group="net", modes=ALL_ROLES, needs_net=True,
    inject=("net_access", "net_max_bytes", "cancel", "search_sources"),
    example={"spec": {"name": "docsrs", "description": "Rust crate docs on docs.rs",
                      "url": "https://crates.io/api/v1/crates?q={query}&per_page={n}",
                      "results": "crates", "title": "name",
                      "link": "https://docs.rs/{name}", "snippet": "description"},
             "test_query": "serde"})
