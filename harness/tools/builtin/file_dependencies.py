"""file_dependencies: what a file imports and what imports it.  Replaced by
index_file while the code index is on."""
from __future__ import annotations

from ... import code_nav
from ..common import CODE_NAV_LANGS
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("file_dependencies",
        f"Show what one source file DEPENDS ON and what depends on IT ({CODE_NAV_LANGS} files): "
        "the imports/includes it declares, and the files elsewhere in the project that import it. "
        "Use it to judge the blast radius of a change before making it, or to find a module's "
        "callers when you do not yet know a name to search for. Importers are matched on the text "
        "of each import, not resolved, so a same-named module elsewhere can show up and dynamic "
        "imports can be missed.",
        {"path":      {"type": "string", "description": "Source file to inspect"},
         "direction": {"type": "string", "description": "'both' (default), 'imports' for only what it imports, or 'importers' for only what imports it"}},
        ["path"]),
    run=code_nav.file_dependencies, order=340, group="code nav", modes=ALL_ROLES,
    keeps_green=True, requires=lambda: code_nav.AVAILABLE, hidden_by_index=True,
    example={"path": "src/parser.py"})
