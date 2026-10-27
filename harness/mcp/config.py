"""MCP server configuration: ~/.momo-harness/mcp.json and <workdir>/.mcp.json.

Both files use the common ``mcpServers`` shape (Claude Desktop / Claude Code;
VS Code's ``servers`` key is accepted too)::

    {"mcpServers": {
        "git":    {"command": "uvx", "args": ["mcp-server-git"], "env": {"X": "${X}"}},
        "remote": {"type": "http", "url": "https://example.com/mcp",
                   "headers": {"Authorization": "Bearer ${TOKEN}"}}}}

momo's own optional keys per server: ``enabled`` (default true), ``confirm``
(always|writes|never, default writes), ``tools`` ({"include": [...],
"exclude": [...]}, fnmatch patterns on the server's tool names), ``timeout``
(seconds per call, default 120) and ``untrusted`` (fence results as untrusted
content; default true for http servers, false for stdio).

``${VAR}`` and ``${VAR:-default}`` are expanded from momo's environment when a
server starts, so tokens need not be written into the file.  The project file
overrides the global one by server name; project servers only start after the
user approves them (see approval.py).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import momo_home

PROJECT_FILE = ".mcp.json"
CONFIRM_MODES = ("always", "writes", "never")
# With /tools discover on: "discover" lists a server's tools in the prompt's
# catalog until loaded (find_tools); "direct" declares them all as before.
EXPOSURES = ("discover", "direct")
DEFAULT_TIMEOUT = 120.0
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
# Keys that change how a server is launched or reached — what approval covers.
_LAUNCH_KEYS = ("type", "command", "args", "env", "cwd", "url", "headers")


def global_path() -> Path:
    return momo_home() / "mcp.json"


@dataclass
class ServerConfig:
    name: str
    source: str                         # "global" | "project" | the extra file's path
    transport: str                      # "stdio" | "http"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    confirm: str = "writes"
    exposure: str = "discover"
    include: list[str] | None = None
    exclude: list[str] = field(default_factory=list)
    timeout: float = DEFAULT_TIMEOUT
    untrusted: bool = False
    raw: dict = field(default_factory=dict)

    def launch_spec(self) -> dict:
        """The launch-relevant part of the entry, unexpanded (for fingerprints
        and change detection)."""
        return {k: self.raw[k] for k in _LAUNCH_KEYS if k in self.raw}

    def describe(self) -> str:
        if self.transport == "http":
            return self.url
        return " ".join([self.command, *self.args])


class ConfigError(ValueError):
    pass


def expand(value: str, environ: dict[str, str]) -> str:
    """Expand ${VAR} / ${VAR:-default}; an unset VAR without a default is an error."""
    def sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        if name in environ and environ[name] != "":
            return environ[name]
        if default is not None:
            return default
        raise ConfigError(f"environment variable {name} is not set")
    return _VAR_RE.sub(sub, value)


def expanded(cfg: ServerConfig, environ: dict[str, str]) -> ServerConfig:
    """A copy with every ${VAR} filled in (raises ConfigError)."""
    return ServerConfig(**{**cfg.__dict__,
                           "command": expand(cfg.command, environ),
                           "args": [expand(a, environ) for a in cfg.args],
                           "env": {k: expand(v, environ) for k, v in cfg.env.items()},
                           "cwd": expand(cfg.cwd, environ) if cfg.cwd else None,
                           "url": expand(cfg.url, environ),
                           "headers": {k: expand(v, environ) for k, v in cfg.headers.items()}})


def _str_map(entry: dict, key: str, name: str) -> dict[str, str]:
    value = entry.get(key) or {}
    if not isinstance(value, dict) or not all(isinstance(v, (str, int, float)) for v in value.values()):
        raise ConfigError(f"{name}: '{key}' must be an object of strings")
    return {str(k): str(v) for k, v in value.items()}


def parse_entry(name: str, entry, source: str) -> ServerConfig:
    if not NAME_RE.match(name):
        raise ConfigError(f"{name!r}: server names may only use letters, digits, '-' and '_'")
    if not isinstance(entry, dict):
        raise ConfigError(f"{name}: expected an object")
    kind = str(entry.get("type") or ("http" if entry.get("url") else "stdio")).lower()
    if kind in ("streamable-http", "streamablehttp", "http"):
        kind = "http"
    elif kind == "sse":
        raise ConfigError(f"{name}: the legacy SSE transport is not supported; use the "
                          "server's streamable HTTP endpoint (type \"http\")")
    elif kind != "stdio":
        raise ConfigError(f"{name}: unknown type {kind!r} (stdio or http)")
    cfg = ServerConfig(name=name, source=source, transport=kind, raw=dict(entry))
    if kind == "stdio":
        cfg.command = entry.get("command") or ""
        if not isinstance(cfg.command, str) or not cfg.command.strip():
            raise ConfigError(f"{name}: 'command' is required for a stdio server")
        args = entry.get("args") or []
        if not isinstance(args, list):
            raise ConfigError(f"{name}: 'args' must be a list")
        cfg.args = [str(a) for a in args]
        cfg.env = _str_map(entry, "env", name)
        cfg.cwd = str(entry["cwd"]) if entry.get("cwd") else None
    else:
        cfg.url = entry.get("url") or ""
        if not isinstance(cfg.url, str) or not re.match(r"(?i)^https?://", cfg.url):
            raise ConfigError(f"{name}: 'url' must be an http(s) URL")
        cfg.headers = _str_map(entry, "headers", name)
    cfg.enabled = entry.get("enabled", entry.get("disabled") is not True) is not False
    confirm = str(entry.get("confirm", "writes")).lower()
    if confirm not in CONFIRM_MODES:
        raise ConfigError(f"{name}: 'confirm' must be one of {', '.join(CONFIRM_MODES)}")
    cfg.confirm = confirm
    exposure = str(entry.get("exposure", "discover")).lower()
    if exposure not in EXPOSURES:
        raise ConfigError(f"{name}: 'exposure' must be one of {', '.join(EXPOSURES)}")
    cfg.exposure = exposure
    tools = entry.get("tools") or {}
    if not isinstance(tools, dict):
        raise ConfigError(f"{name}: 'tools' must be an object with include/exclude lists")
    if tools.get("include") is not None:
        cfg.include = [str(p) for p in tools["include"]]
    cfg.exclude = [str(p) for p in tools.get("exclude") or ()]
    try:
        cfg.timeout = float(entry.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        raise ConfigError(f"{name}: 'timeout' must be a number of seconds") from None
    if cfg.timeout <= 0:
        raise ConfigError(f"{name}: 'timeout' must be positive")
    cfg.untrusted = bool(entry.get("untrusted", kind == "http"))
    return cfg


def read_file(path: Path, source: str) -> tuple[list[ServerConfig], list[str]]:
    """Servers from one file plus error lines; a missing file is just empty."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return [], [f"{path}: {e}"]
    servers = data.get("mcpServers", data.get("servers")) if isinstance(data, dict) else None
    if servers is None:
        return [], [f"{path}: no \"mcpServers\" object"]
    if not isinstance(servers, dict):
        return [], [f"{path}: \"mcpServers\" must be an object"]
    out, errors = [], []
    for name, entry in servers.items():
        try:
            out.append(parse_entry(str(name), entry, source))
        except ConfigError as e:
            errors.append(f"{path}: {e}")
    return out, errors


def load(workdir: Path, extra: tuple[Path, ...] = ()) -> tuple[list[ServerConfig], list[str]]:
    """Global, then extra files (--mcp-config), then the project file; a later
    file replaces an earlier server of the same name."""
    merged: dict[str, ServerConfig] = {}
    errors: list[str] = []
    files = [(global_path(), "global"), *((Path(p), str(p)) for p in extra),
             (Path(workdir) / PROJECT_FILE, "project")]
    for path, source in files:
        servers, errs = read_file(path, source)
        errors += errs
        for cfg in servers:
            merged.pop(cfg.name, None)      # keep file order for the newcomer
            merged[cfg.name] = cfg
    return list(merged.values()), errors


# ── editing the global file (/mcp add|rm, web "Add server") ─────────────────

def _read_global() -> dict:
    path = global_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"mcpServers": {}}
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"{path} can't be read ({e}); fix it by hand first") from None
    if not isinstance(data, dict) or not isinstance(data.setdefault("mcpServers", {}), dict):
        raise ConfigError(f"{path} has no \"mcpServers\" object; fix it by hand first")
    return data


def _write_global(data: dict) -> None:
    from ..session import atomic_write
    atomic_write(global_path(), json.dumps(data, indent=2) + "\n")


def add_server(name: str, entry: dict) -> str:
    """Add or replace a server in the global file; returns a notice (raises ConfigError)."""
    parse_entry(name, entry, "global")          # validate before writing
    data = _read_global()
    replaced = name in data["mcpServers"]
    data["mcpServers"][name] = entry
    _write_global(data)
    return f"{'Replaced' if replaced else 'Added'} MCP server '{name}' in {global_path()}"


def remove_server(name: str) -> str:
    data = _read_global()
    if name not in data["mcpServers"]:
        raise ConfigError(f"no server '{name}' in {global_path()}"
                          " (project servers live in the workdir's .mcp.json)")
    del data["mcpServers"][name]
    _write_global(data)
    return f"Removed MCP server '{name}' from {global_path()}"
