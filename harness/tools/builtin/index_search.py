"""index_search: find a definition by exact, partial or approximate name, from
the in-memory code index (harness/code_index/).

The index tools are offered while the code index is on (/index on).  They
replace find_references and file_dependencies then (hidden_by_index), so the
model never has to choose between two tools that answer the same question."""
from __future__ import annotations

from ... import code_index, code_nav
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("index_search",
        "Find any definition by name — exact, partial or approximate — across the whole project, "
        "from the code index: classes, functions and methods, plus config keys (YAML/TOML/JSON "
        "paths like 'services.web.ports'), HTML ids, CSS selectors, SQL tables and columns, shell "
        "functions and Dockerfile stages. Use it FIRST when you know roughly what something is "
        "called: words in any order work ('config parse' finds parse_config). Returns "
        "file:L<start>-<end>, kind, qualified name and signature line, best match first — then "
        "read_symbol reads one. Pass a LINE NUMBER ('1300') with path set to that one file to "
        "learn which definition the line is in.",
        {"query": {"type": "string", "description": "A name or part of one: 'parse_config', 'Harness.send', 'services.web', 'retry delay', a pattern like '*_handler', or a line number like '1300' (then path must be the file)"},
         "kind":  {"type": "string", "description": "Only this kind: class, function, method, key, table, rule, id, column, stage, ..., or 'file' to find files by name, path or glob like '*.yaml' (default: any; a path-like query also finds files)"},
         "path":  {"type": "string", "description": "Only under this directory or file, or matching a glob like '*.yaml' (default: whole project)"},
         "lang":  {"type": "string", "description": "Only this language: python, typescript, yaml, sql, css, ... (default: any)"}},
        ["query"]),
    run=code_index.index_search, order=200, group="index", modes=ALL_ROLES, keeps_green=True,
    needs_index=True, requires=lambda: code_nav.AVAILABLE, inject=("index", "cancel"),
    example={"query": "parse config"})
