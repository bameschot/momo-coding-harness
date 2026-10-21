"""The built-in tools, one per file.  Each file ends with `TOOL = Tool(...)`
(harness/tools/registry.py) and is registered by registry.load_builtins() —
adding a tool means adding a file here, nothing else.

Their `order` values place them in every tool list (spaced by 10, so a new
tool can slot in between): files 100s, code index 200s, code navigation
300s, writes 400s, shell 500s, plan and scratchpad 600-700s, internet 800s.
Custom tools default to 9000."""
