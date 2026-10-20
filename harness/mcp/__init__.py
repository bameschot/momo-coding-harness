"""MCP (Model Context Protocol) client: external servers' tools and resources.

stdlib only — JSON-RPC over a subprocess's stdio or streamable HTTP.  See
config.py for the config files, manager.py for how the harness uses it.
"""
from .convert import is_mcp_name
from .manager import RESOURCE_TOOLS, MCPManager

__all__ = ["MCPManager", "RESOURCE_TOOLS", "is_mcp_name"]
