"""Which toolchain a momo-box needs for a project: its base image, extra packages,
dependency prefetch (`prepare`) and registry hosts.

Resolution, first match wins:

1. command-line flags (--preset / --base / --packages)
2. `.momo/box.json` committed in the repo
3. `.devcontainer/devcontainer.json` (its image, or its Dockerfile)
4. detection from the build files and version pins
5. the default base (Debian + git)

Separately, `.momo/network.json` states which hosts a pull box may reach (and
what the model is told about the network), whatever the toolchain source is.

Everything here only *reads* files, never runs project code, and works on a
plain folder (DirTree) or on a git revision (GitTree, what goes into the box).
Pure apart from that, so it is unit-tested without a container engine.
Runs on the host: Python >= 3.10, standard library only.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
from harness.paths import SKIP_DIRS  # noqa: E402  (a leaf module, safe on any Python 3.10+)

PRESETS_PATH = _HERE / "presets.json"
MAX_DEPTH = 3                # folders below the root that detection looks into
_MAX_READ = 1_000_000

BOX_JSON = ".momo/box.json"
NETWORK_JSON = ".momo/network.json"
_NETWORK_KEYS = {"instructions", "replace_defaults", "allow", "deny"}
MAX_INSTRUCTIONS = 2000
MAX_REASON = 200
_CONFIG_FILES = (BOX_JSON, NETWORK_JSON)
_BOX_KEYS = {"preset", "base", "packages", "dockerfile", "prepare", "prepare_inputs",
             "pull_hosts", "env", "full_base"}


def load_presets(path: Path = PRESETS_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── file trees ────────────────────────────────────────────────────────────────

class DirTree:
    """A folder on disk, walked to MAX_DEPTH, skipping harness.paths.SKIP_DIRS."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def files(self) -> list[str]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel = Path(dirpath).relative_to(self.root)
            depth = 0 if str(rel) == "." else len(rel.parts)
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and depth < MAX_DEPTH)
            if depth > MAX_DEPTH:
                continue
            for name in filenames:
                out.append((rel / name).as_posix() if depth else name)
        # .momo is in SKIP_DIRS (never indexed), but box.json and network.json are config.
        out.extend(rel for rel in _CONFIG_FILES if (self.root / rel).is_file())
        return sorted(set(out))

    def read(self, rel: str) -> str | None:
        p = self.root / rel
        try:
            if not p.is_file() or p.is_symlink() or p.stat().st_size > _MAX_READ:
                return None
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None


class GitTree:
    """A git revision (default HEAD): exactly what a bundle puts in the box."""

    def __init__(self, repo: Path, rev: str = "HEAD"):
        self.repo, self.rev = Path(repo), rev
        self._files: list[str] | None = None

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True,
                              timeout=60)

    def files(self) -> list[str]:
        if self._files is None:
            r = self._git("ls-tree", "-r", "--name-only", "-z", self.rev)
            names = r.stdout.decode("utf-8", "replace").split("\0") if r.returncode == 0 else []
            self._files = sorted(n for n in names if n and _visible(n))
        return self._files

    def read(self, rel: str) -> str | None:
        r = self._git("show", f"{self.rev}:{rel}")
        if r.returncode != 0 or len(r.stdout) > _MAX_READ:
            return None
        return r.stdout.decode("utf-8", "replace")


def _visible(rel: str) -> bool:
    """Within MAX_DEPTH and outside SKIP_DIRS (the config files and devcontainer excepted)."""
    if rel in _CONFIG_FILES:
        return True
    parts = PurePosixPath(rel).parts
    if len(parts) - 1 > MAX_DEPTH and parts[0] != ".devcontainer":
        return False
    return not any(p in SKIP_DIRS for p in parts[:-1])


def tree_for(repo: Path, rev: str | None = None):
    """The tree that will be in the box: the git revision for a repo, else the folder."""
    repo = Path(repo)
    if (repo / ".git").exists() or repo.suffix == ".git":
        probe = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q",
                                f"{rev or 'HEAD'}^{{commit}}"], capture_output=True)
        if probe.returncode == 0:
            return GitTree(repo, rev or "HEAD")
    return DirTree(repo)


# ── version pins ──────────────────────────────────────────────────────────────

def _first(pattern: str, text: str | None, flags=0) -> str | None:
    if not text:
        return None
    m = re.search(pattern, text, flags)
    return m.group(1) if m else None


def _java_major(v: str | None) -> str | None:
    """'1.8' → '8', '17.0.2' → '17', 'temurin-21.0.1' → '21'."""
    if not v:
        return None
    m = re.search(r"(\d+)(?:\.(\d+))?", v)
    if not m:
        return None
    return m.group(2) if m.group(1) == "1" and m.group(2) else m.group(1)


def _tool_versions(tree) -> dict[str, str]:
    """asdf / mise `.tool-versions`: tool → version (first one listed)."""
    out = {}
    for line in (tree.read(".tool-versions") or "").splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2:
            out.setdefault(parts[0], parts[1])
    return out


def _round_up(value: str, known: list[str]) -> str:
    """The first known tag >= value (versions compared numerically); the newest
    known one when value is newer than all of them; value itself without a list."""
    if not known:
        return value
    key = lambda v: tuple(int(x) for x in re.findall(r"\d+", v))   # noqa: E731
    want = key(value)
    for tag in sorted(known, key=key):
        if key(tag) >= want:
            return tag
    return sorted(known, key=key)[-1]


def _versions(tree, files: list[str]) -> tuple[dict[str, str], list[str]]:
    """The version slots found (jdk/node/python/rust/go) and why."""
    tv = _tool_versions(tree)
    found: dict[str, str] = {}
    why: list[str] = []

    def put(slot, value, reason):
        if value and slot not in found:
            found[slot] = value
            why.append(f"{reason} → {slot} {value}")

    # Java: pom.xml, Gradle build scripts, .java-version, .tool-versions
    pom = tree.read("pom.xml") if "pom.xml" in files else None
    for tag in ("maven.compiler.release", "release", "java.version", "maven.compiler.source",
                "maven.compiler.target"):
        v = _java_major(_first(rf"<{re.escape(tag)}>\s*([\d.]+)\s*</{re.escape(tag)}>", pom))
        if v:
            put("jdk", v, f"pom.xml <{tag}>")
            break
    for name in ("build.gradle.kts", "build.gradle", "app/build.gradle.kts", "app/build.gradle"):
        text = tree.read(name) if name in files else None
        jv = _first(r"JavaVersion\.VERSION_(\d+(?:_\d+)?)", text)
        v = (_first(r"jvmToolchain\s*\(\s*(\d+)\s*\)", text)
             or _first(r"JavaLanguageVersion\.of\s*\(\s*(\d+)\s*\)", text)
             or (_java_major(jv.replace("_", ".")) if jv else None)
             or _java_major(_first(r"""sourceCompatibility\s*=\s*['"]?([\d.]+)""", text)))
        if v:
            put("jdk", v, f"{name} toolchain")
            break
    put("jdk", _java_major((tree.read(".java-version") or "").strip() or None), ".java-version")
    put("jdk", _java_major(tv.get("java")), ".tool-versions java")

    # Node: .nvmrc, .node-version, package.json engines, .tool-versions
    for name in (".nvmrc", ".node-version"):
        v = _first(r"(\d+)", (tree.read(name) or "") if name in files else "")
        put("node", v, name)
    if "package.json" in files:
        try:
            engines = (json.loads(tree.read("package.json") or "{}").get("engines") or {})
        except (ValueError, AttributeError):
            engines = {}
        spec = engines.get("node") if isinstance(engines, dict) else None
        # A floor (">=18") is met by the default (newest); a pin or caret names the major.
        if isinstance(spec, str) and not spec.strip().startswith(">"):
            put("node", _first(r"(\d+)", spec), "package.json engines.node")
    put("node", _first(r"(\d+)", tv.get("nodejs") or tv.get("node") or ""), ".tool-versions nodejs")

    # Python: .python-version, pyproject requires-python (pins only), .tool-versions
    put("python", _first(r"(\d+\.\d+)", (tree.read(".python-version") or "")
                         if ".python-version" in files else ""), ".python-version")
    if "pyproject.toml" in files:
        spec = _first(r"""requires-python\s*=\s*["']([^"']+)["']""", tree.read("pyproject.toml"))
        if spec and re.match(r"\s*(==|~=)", spec):
            put("python", _first(r"(\d+\.\d+)", spec), "pyproject.toml requires-python")
    put("python", _first(r"(\d+\.\d+)", tv.get("python") or ""), ".tool-versions python")

    # Rust: rust-toolchain(.toml)
    for name in ("rust-toolchain.toml", "rust-toolchain"):
        if name in files:
            text = tree.read(name) or ""
            ch = _first(r"""channel\s*=\s*["']([^"']+)["']""", text) or text.strip()
            v = _first(r"^(\d+\.\d+)", ch)
            if v:
                put("rust", v, name)
            elif ch and ch != "stable":
                why.append(f"{name}: channel {ch!r} is not a release; the image has stable "
                           "(install it in a box.json dockerfile)")
            break
    put("rust", _first(r"(\d+\.\d+)", tv.get("rust") or ""), ".tool-versions rust")

    # Go: go.mod's go directive
    if "go.mod" in files:
        put("go", _first(r"^go\s+(\d+\.\d+)", tree.read("go.mod"), re.M), "go.mod go directive")
    put("go", _first(r"(\d+\.\d+)", tv.get("golang") or tv.get("go") or ""), ".tool-versions golang")
    return found, why


# ── detection ─────────────────────────────────────────────────────────────────

_C_SOURCES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp")
# Kind → build-file names.  Order = priority on a tie.
_MARKERS = {
    "gradle": ("build.gradle.kts", "build.gradle", "settings.gradle.kts", "settings.gradle",
               "gradlew"),
    "java":   ("pom.xml", "mvnw"),
    "cpp":    ("CMakeLists.txt", "meson.build"),
    "rust":   ("Cargo.toml",),
    "go":     ("go.mod",),
    "node":   ("package.json",),
    "python": ("pyproject.toml", "setup.py", "requirements.txt", "setup.cfg"),
}


@dataclass
class Detection:
    primary: str | None = None
    secondary: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    wrappers: dict[str, str] = field(default_factory=dict)   # kind → wrapper script
    extra_hosts: list[str] = field(default_factory=list)     # wrapper distribution hosts
    meson: bool = False


def _wrapper_host(text: str | None) -> str | None:
    url = _first(r"distributionUrl\s*=\s*(\S+)", text)
    return _first(r"https?\\?://([^/:\\]+)", url) if url else None


def detect(tree) -> Detection:
    files = tree.files()
    names = {}
    for rel in files:
        names.setdefault(PurePosixPath(rel).name, []).append(rel.count("/"))
    scores: dict[str, int] = {}
    for kind, markers in _MARKERS.items():
        for m in markers:
            for depth in names.get(m, []):
                scores[kind] = scores.get(kind, 0) + max(1, 3 - depth)
    # A Makefile alone is a task runner as often as a C build: count it only with C sources.
    if "Makefile" in names and any(f.endswith(_C_SOURCES) for f in files):
        scores["cpp"] = scores.get("cpp", 0) + max(1, 3 - min(names["Makefile"]))
    # requirements*.txt variants (requirements-dev.txt …) count for Python.
    if any(re.fullmatch(r"requirements[-_.\w]*\.txt", PurePosixPath(f).name) for f in files):
        scores.setdefault("python", 1)

    det = Detection()
    if not scores:
        return det
    order = list(_MARKERS)
    ranked = sorted(scores, key=lambda k: (-scores[k], order.index(k)))
    det.primary, det.secondary = ranked[0], ranked[1:]
    for kind in ranked:
        found = [m for m in _MARKERS[kind] if m in names] or ["Makefile + C sources"]
        det.reasons.append(f"{', '.join(found)} → {kind}")
    det.versions, why = _versions(tree, files)
    det.reasons.extend(why)
    for kind, script, props in (("java", "mvnw", ".mvn/wrapper/maven-wrapper.properties"),
                                ("gradle", "gradlew", "gradle/wrapper/gradle-wrapper.properties")):
        if script in names and 0 in names[script]:
            det.wrappers[kind] = script
            host = _wrapper_host(tree.read(props)) if props in files else None
            if host:
                det.extra_hosts.append(host)
                det.reasons.append(f"{props} downloads from {host}")
    det.meson = "meson.build" in names
    return det


# ── box.json and devcontainer.json ───────────────────────────────────────────

class ConfigError(ValueError):
    pass


def _strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas (devcontainer.json) → JSON."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _str_list(cfg: dict, key: str) -> list[str]:
    v = cfg.get(key, [])
    if isinstance(v, str):
        v = v.split() if key in ("packages", "pull_hosts") else [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{BOX_JSON}: {key!r} must be a list of strings")
    return v


def load_box_json(tree) -> tuple[dict | None, str]:
    """The repo's .momo/box.json, validated, and its raw text ('' when absent)."""
    raw = tree.read(BOX_JSON) if BOX_JSON in tree.files() else None
    if raw is None:
        return None, ""
    try:
        cfg = json.loads(_strip_jsonc(raw))
    except ValueError as e:
        raise ConfigError(f"{BOX_JSON}: not valid JSON ({e})") from None
    if not isinstance(cfg, dict):
        raise ConfigError(f"{BOX_JSON}: expected an object")
    unknown = sorted(set(cfg) - _BOX_KEYS - {k for k in cfg if k.startswith("_")})
    if unknown:
        raise ConfigError(f"{BOX_JSON}: unknown key(s) {', '.join(unknown)} "
                          f"(known: {', '.join(sorted(_BOX_KEYS))})")
    for key in ("preset", "base", "dockerfile"):
        if key in cfg and not isinstance(cfg[key], str):
            raise ConfigError(f"{BOX_JSON}: {key!r} must be a string")
    for key in ("packages", "prepare", "prepare_inputs", "pull_hosts"):
        if key in cfg:
            cfg[key] = _str_list(cfg, key)
    if "full_base" in cfg and not isinstance(cfg["full_base"], bool):
        raise ConfigError(f"{BOX_JSON}: 'full_base' must be true or false")
    env = cfg.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                            for k, v in env.items()):
        raise ConfigError(f"{BOX_JSON}: 'env' must map names to strings")
    return cfg, raw


def load_devcontainer(tree) -> tuple[dict | None, str]:
    """(image / dockerfile info, raw text) from .devcontainer/devcontainer.json."""
    for rel in (".devcontainer/devcontainer.json", ".devcontainer.json"):
        if rel in tree.files():
            raw = tree.read(rel) or ""
            try:
                cfg = json.loads(_strip_jsonc(raw))
            except ValueError:
                return None, raw
            if not isinstance(cfg, dict):
                return None, raw
            info: dict = {"path": rel}
            if isinstance(cfg.get("image"), str):
                info["image"] = cfg["image"]
            build = cfg.get("build") if isinstance(cfg.get("build"), dict) else {}
            df = build.get("dockerfile") or cfg.get("dockerFile")
            if isinstance(df, str):
                here = PurePosixPath(rel).parent
                info["dockerfile"] = _norm(here / df)
                ctx = build.get("context") if isinstance(build.get("context"), str) else "."
                info["context"] = _norm(here / ctx)
            if cfg.get("features"):
                info["features_ignored"] = True
            return (info if ("image" in info or "dockerfile" in info) else None), raw
    return None, ""


def _norm(p: PurePosixPath) -> str:
    """'a/b/../c' → 'a/c'; '' for the root.  Refuses paths that leave the repo."""
    out: list[str] = []
    for part in p.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                raise ConfigError(f"{p} points outside the repository")
            out.pop()
        else:
            out.append(part)
    return "/".join(out)


# ── network rules (.momo/network.json) ────────────────────────────────────────

@dataclass
class NetworkRules:
    instructions: str = ""                                    # for the model
    replace_defaults: bool = False                            # drop the toolchain registries
    allow: dict[str, str] = field(default_factory=dict)       # rule → reason
    deny: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_RULE_HOST_RE = re.compile(rf"(?:\*\.)?{_LABEL}(?:\.{_LABEL})*")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def parse_host_rule(entry: str, where: str) -> tuple[str, str | None]:
    """One allow/deny entry → (rule, warning).  A rule is `host`, `*.domain` or
    either with `:port`; IPv6 literals in brackets when a port follows.  An
    http(s) URL is reduced to its host (and explicit port): the proxy sees only
    that much of an HTTPS request, so a path can't be enforced."""
    if not isinstance(entry, str) or not entry.strip():
        raise ConfigError(f"{where}: empty host entry")
    s, warning = entry.strip(), None
    if "://" in s:
        u = urlsplit(s)
        if u.scheme.lower() not in ("http", "https"):
            raise ConfigError(f"{where}: {entry!r}: only http and https URLs")
        if u.username or u.password:
            raise ConfigError(f"{where}: {entry!r}: no credentials in a URL")
        try:
            port = u.port
        except ValueError:
            raise ConfigError(f"{where}: {entry!r}: bad port") from None
        host = u.hostname or ""
        if (u.path not in ("", "/")) or u.query or u.fragment:
            warning = (f"{where}: {entry} — a path can't be enforced (the proxy sees only "
                       f"the host of an HTTPS request), so all of {host} is allowed")
        if ":" in host:
            host = f"[{host}]"
        s = host + (f":{port}" if port else "")
    s = s.lower().rstrip(".")
    host, port = s, None
    if s.startswith("["):
        inner, _, rest = s[1:].partition("]")
        host, port = inner, rest[1:] if rest.startswith(":") else (rest or None)
        if rest and not rest.startswith(":"):
            raise ConfigError(f"{where}: {entry!r} is not a host")
    elif s.count(":") == 1:
        host, port = s.split(":")
    if port is not None:
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ConfigError(f"{where}: {entry!r}: the port must be 1–65535")
        port = str(int(port))
    host = host.rstrip(".")
    try:
        ip = ipaddress.ip_address(host)
        host = f"[{ip.compressed}]" if ip.version == 6 and port else ip.compressed
    except ValueError:
        if not _RULE_HOST_RE.fullmatch(host) or len(host) > 253:
            raise ConfigError(f"{where}: {entry!r} is not a host name "
                              "(host, *.domain, host:port or an http(s) URL)") from None
    return (f"{host}:{port}" if port else host), warning


def _clean_text(text: str, limit: int, *, one_line: bool) -> str:
    text = _CONTROL_RE.sub("", text.replace("\r\n", "\n"))
    if one_line:
        text = " ".join(text.split())
    return text.strip()[:limit]


def _host_map(cfg: dict, key: str, presets: dict, warnings: list[str]) -> dict[str, str]:
    raw = cfg.get(key, {})
    if isinstance(raw, list):
        raw = {h: "" for h in raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"{NETWORK_JSON}: {key!r} must map hosts to reasons, or list hosts")
    groups = presets.get("pull_hosts", {})
    out: dict[str, str] = {}
    for entry, reason in raw.items():
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            raise ConfigError(f"{NETWORK_JSON}: {key}[{entry!r}]: the reason must be a string")
        reason = _clean_text(reason, MAX_REASON, one_line=True)
        names = groups[entry] if isinstance(entry, str) and entry in groups \
            and entry != "default" else [entry]
        for name in names:
            rule, warning = parse_host_rule(name, f"{NETWORK_JSON} {key}")
            if warning:
                warnings.append(warning)
            if key == "allow" and rule.startswith("*.") and "." not in rule[2:].split(":")[0]:
                warnings.append(f"{NETWORK_JSON} allow: {rule} lets the box reach every "
                                f"host under .{rule[2:]}")
            out.setdefault(rule, reason)
    return out


def _split_rule(rule: str) -> tuple[str, str | None]:
    if rule.startswith("["):
        host, _, rest = rule[1:].partition("]")
        return host, rest[1:] or None
    host, _, port = rule.partition(":") if rule.count(":") == 1 else (rule, "", "")
    return host, port or None


def denied_by(rule: str, deny: dict[str, str]) -> str | None:
    """The deny rule that overrides an allow rule entirely, if any (the proxy
    lets your own allows beat a deny, never the repo's)."""
    host, port = _split_rule(rule)
    for d in deny:
        dhost, dport = _split_rule(d)
        if dport is not None and dport != port:
            continue
        if dhost == host or (dhost.startswith("*.") and (
                host.lstrip("*.") == dhost[2:] or host.endswith("." + dhost[2:]))):
            return d
    return None


def load_network_json(tree, presets: dict | None = None) -> tuple[NetworkRules | None, str]:
    """The repo's .momo/network.json, validated, and its raw text ('' when absent)."""
    raw = tree.read(NETWORK_JSON) if NETWORK_JSON in tree.files() else None
    if raw is None:
        return None, ""
    presets = presets or load_presets()
    try:
        cfg = json.loads(_strip_jsonc(raw))
    except ValueError as e:
        raise ConfigError(f"{NETWORK_JSON}: not valid JSON ({e})") from None
    if not isinstance(cfg, dict):
        raise ConfigError(f"{NETWORK_JSON}: expected an object")
    unknown = sorted(set(cfg) - _NETWORK_KEYS - {k for k in cfg if k.startswith("_")})
    if unknown:
        raise ConfigError(f"{NETWORK_JSON}: unknown key(s) {', '.join(unknown)} "
                          f"(known: {', '.join(sorted(_NETWORK_KEYS))})")
    instructions = cfg.get("instructions", "")
    if isinstance(instructions, list) and all(isinstance(x, str) for x in instructions):
        instructions = "\n".join(instructions)          # one line per item, for long notes
    if not isinstance(instructions, str):
        raise ConfigError(f"{NETWORK_JSON}: 'instructions' must be a string")
    if len(instructions) > MAX_INSTRUCTIONS:
        raise ConfigError(f"{NETWORK_JSON}: 'instructions' is over {MAX_INSTRUCTIONS} "
                          "characters; keep it to what the model needs about the network")
    replace = cfg.get("replace_defaults", False)
    if not isinstance(replace, bool):
        raise ConfigError(f"{NETWORK_JSON}: 'replace_defaults' must be true or false")
    rules = NetworkRules(instructions=_clean_text(instructions, MAX_INSTRUCTIONS, one_line=False),
                         replace_defaults=replace)
    rules.allow = _host_map(cfg, "allow", presets, rules.warnings)
    rules.deny = _host_map(cfg, "deny", presets, rules.warnings)
    for rule in rules.allow:
        d = denied_by(rule, rules.deny)
        if d:
            rules.warnings.append(f"{NETWORK_JSON}: allow {rule} has no effect: deny {d} "
                                  "overrides it")
    if replace and not rules.allow:
        rules.warnings.append(f"{NETWORK_JSON}: replace_defaults with nothing in 'allow': "
                              "a pull box reaches no host (--net locked says so more plainly)")
    return rules, raw


# ── resolution ────────────────────────────────────────────────────────────────

@dataclass
class Resolved:
    source: str                         # flags | box.json | devcontainer | detected | default
    base: str
    packages: list[str] = field(default_factory=list)
    dockerfile: str | None = None       # repo-relative project Dockerfile, applied on top
    base_dockerfile: str | None = None  # devcontainer Dockerfile that *is* the base
    base_variant: str = ""              # slim | full, for a preset with both images
    base_context: str = ""              # its build context, repo-relative ('' = root)
    prepare: list[str] = field(default_factory=list)
    prepare_inputs: list[str] = field(default_factory=list)
    pull_hosts: list[str] = field(default_factory=list)       # extra hosts (box.json, wrappers)
    registry_hosts: list[str] = field(default_factory=list)   # its toolchains' registries
    env: dict[str, str] = field(default_factory=dict)
    presets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)         # shown on every run
    reasons: list[str] = field(default_factory=list)
    approximated: list[str] = field(default_factory=list)
    offline_hints: list[str] = field(default_factory=list)
    needs_approval: bool = False        # repo-supplied config: ask before building
    approval_text: str = ""             # what the user approves (config + Dockerfiles)
    network: NetworkRules | None = None  # .momo/network.json

    def summary(self) -> str:
        """The one-line `toolchain: …` header."""
        what = " + ".join(self.presets) if self.presets else self.source
        extra = f", packages {' '.join(self.packages)}" if self.packages else ""
        df = f", dockerfile {self.dockerfile}" if self.dockerfile else ""
        variant = (" (slim: no C compiler; --full-base for the full image)"
                   if self.base_variant == "slim" else "")
        return f"toolchain: {what} — {self.base}{variant}{extra}{df} ({self.source})"

    def box_json(self) -> dict:
        """What `momo-box detect --write` saves."""
        out: dict = {"base": self.base}
        if self.packages:
            out["packages"] = self.packages
        if self.prepare:
            out["prepare"] = self.prepare
        if self.prepare_inputs:
            out["prepare_inputs"] = self.prepare_inputs
        extra = [h for h in self.pull_hosts]
        if extra:
            out["pull_hosts"] = extra
        if self.env:
            out["env"] = self.env
        return out


_PREPARE_INPUTS = {
    "java": ["pom.xml", "**/pom.xml"],
    "gradle": ["build.gradle.kts", "build.gradle", "settings.gradle.kts", "settings.gradle",
               "gradle/libs.versions.toml", "**/build.gradle.kts", "**/build.gradle"],
    "node": ["package.json", "package-lock.json", "npm-shrinkwrap.json"],
    "rust": ["Cargo.toml", "Cargo.lock"],
    "go": ["go.mod", "go.sum"],
    "python": ["pyproject.toml", "requirements.txt"],
}


def _fill(preset: dict, versions: dict[str, str], full: bool = False) -> str:
    """The preset's image for these versions: its slim variant unless `full`
    (the slim images have no C compiler; a preset without one has one image)."""
    base = preset.get("base_full", preset["base"]) if full else preset["base"]
    for slot, default in preset.get("defaults", {}).items():
        value = versions.get(slot, default)
        value = _round_up(value, preset.get("versions", {}).get(slot, []))
        base = base.replace("{" + slot + "}", value)
    return base


def _apply_preset(res: Resolved, name: str, presets: dict, versions: dict[str, str],
                  wrappers: dict[str, str] | None = None, prepare: bool = True,
                  files: list[str] | None = None, full: bool = False):
    table = presets["presets"]
    name = presets.get("aliases", {}).get(name, name)
    if name not in table:
        raise ConfigError(f"unknown preset {name!r} (known: {', '.join(table)})")
    p = table[name]
    res.base = _fill(p, versions, full)
    if "base_full" in p:
        res.base_variant = "full" if full else "slim"
    res.presets.append(name)
    _add(res.packages, p.get("packages", []))
    _add(res.registry_hosts, p.get("pull_hosts", []))
    if prepare:
        use_wrapper = wrappers and name in wrappers
        key = "wrapper_prepare" if use_wrapper else "prepare"
        if (p.get("lockfiles") and files is not None
                and not any(lf in files for lf in p["lockfiles"])):
            key = "prepare_without_lock"      # `npm ci` refuses to run without a lockfile
        _add(res.prepare, p.get(key, []))
        _add(res.prepare_inputs, _PREPARE_INPUTS.get(name, []))
    if p.get("offline_hint"):
        res.offline_hints.append(p["offline_hint"])


def _add(into: list, items):
    for x in items:
        if x not in into:
            into.append(x)


def expand_hosts(names: list[str], presets: dict) -> list[str]:
    """pull_hosts entries: a group name from presets.json ('github') or a host."""
    groups = presets.get("pull_hosts", {})
    out: list[str] = []
    for n in names:
        _add(out, groups.get(n, [n]) if n in groups and n != "default" else [n])
    return out


def resolve(tree, presets: dict | None = None, *, preset: str | None = None,
            base: str | None = None, packages: list[str] | None = None,
            full_base: bool = False) -> Resolved:
    """The box's toolchain for this tree (see the module docstring for the order),
    with the repo's network rules, which apply whatever the toolchain source is."""
    presets = presets or load_presets()
    res = _resolve(tree, presets, preset=preset, base=base, packages=packages,
                   full_base=full_base)
    rules, raw = load_network_json(tree, presets)
    if rules is not None:
        res.network = rules
        if rules.replace_defaults:          # the project's own hosts, not the public registries
            res.registry_hosts = []
        res.warnings.extend(rules.warnings)
        res.reasons.append(NETWORK_JSON)
        # The repo widens what the box may reach and talks to the model: the user approves it.
        res.needs_approval = True
        part = f"--- {NETWORK_JSON}\n{raw}"
        res.approval_text = f"{res.approval_text}\n{part}" if res.approval_text else part
    return res


def _resolve(tree, presets: dict, *, preset: str | None, base: str | None,
             packages: list[str] | None, full_base: bool = False) -> Resolved:
    det = detect(tree)
    box, box_raw = load_box_json(tree)
    files = tree.files()

    if preset or base:                                        # 1. flags
        res = Resolved(source="flags", base=presets["default_base"])
        if preset:
            _apply_preset(res, preset, presets, det.versions, det.wrappers, files=files,
                          full=full_base)
            res.reasons.append(f"--preset {preset}")
        if base:
            res.base = base
            res.base_variant = ""
            res.reasons.append(f"--base {base}")
    elif box is not None:                                     # 2. .momo/box.json
        res = Resolved(source="box.json", base=presets["default_base"])
        if box.get("preset"):
            _apply_preset(res, box["preset"], presets, det.versions, det.wrappers,
                          prepare="prepare" not in box, files=files,
                          full=full_base or box.get("full_base") is True)
        if box.get("base"):
            res.base = box["base"]
            res.base_variant = ""
        if "prepare" in box:
            res.prepare = list(box["prepare"])
        res.dockerfile = box.get("dockerfile")
        _add(res.packages, box.get("packages", []))
        _add(res.prepare_inputs, box.get("prepare_inputs", []))
        _add(res.pull_hosts, [parse_host_rule(h, f"{BOX_JSON} pull_hosts")[0]
                              for h in expand_hosts(box.get("pull_hosts", []), presets)])
        res.env.update(box.get("env", {}))
        res.reasons.append(f"{BOX_JSON}")
        res.needs_approval = True
    else:
        dev, dev_raw = load_devcontainer(tree)
        if dev is not None:                                   # 3. devcontainer.json
            res = Resolved(source="devcontainer", base=dev.get("image") or presets["default_base"])
            res.base_dockerfile = dev.get("dockerfile")
            res.base_context = dev.get("context", "")
            res.reasons.append(f"{dev['path']}: "
                               + (f"image {dev['image']}" if "image" in dev
                                  else f"dockerfile {dev['dockerfile']}"))
            if dev.get("features_ignored"):
                res.reasons.append("devcontainer features are not supported and were ignored")
            res.needs_approval = True
            res.approval_text = f"--- {dev['path']}\n{dev_raw}"
            if res.base_dockerfile:      # it runs at build time: show and fingerprint it too
                res.approval_text += (f"\n--- {res.base_dockerfile}\n"
                                      f"{tree.read(res.base_dockerfile) or '(missing)'}")
            _detected_extras(res, det, approximate=False, presets=presets)
            _add(res.packages, packages or [])
            return res
        if det.primary:                                       # 4. detection
            res = Resolved(source="detected", base=presets["default_base"])
            _apply_preset(res, det.primary, presets, det.versions, det.wrappers, files=files,
                          full=full_base)
            if det.primary == "cpp" and det.meson:
                _add(res.packages, ["meson"])
        else:                                                 # 5. default
            res = Resolved(source="default", base=presets["default_base"])
            res.reasons.append("no build files found")
    _detected_extras(res, det, approximate=res.source == "detected", presets=presets)
    _add(res.packages, packages or [])
    if res.needs_approval:
        parts = [f"--- {BOX_JSON}\n{box_raw}"]
        if res.dockerfile:
            parts.append(f"--- {res.dockerfile}\n{tree.read(res.dockerfile) or '(missing)'}")
        res.approval_text = "\n".join(parts)
    if res.dockerfile and res.dockerfile not in tree.files() and tree.read(res.dockerfile) is None:
        raise ConfigError(f"{BOX_JSON}: dockerfile {res.dockerfile} is not in the repo")
    return res


def _detected_extras(res: Resolved, det: Detection, *, approximate: bool, presets: dict):
    """Detection's reasons and wrapper hosts; with `approximate` (pure detection),
    each secondary toolchain as Debian packages on top of the primary's image.
    Flags, box.json and devcontainer images are taken as complete."""
    res.reasons.extend(det.reasons)
    table = presets["presets"]
    # A wrapper's download host comes from a file in the repo: it only joins the
    # allowlist when it is a registry momo-box knows, never on the repo's say-so.
    known = set(expand_hosts(presets.get("pull_hosts", {}).get("default", []), presets))
    for host in det.extra_hosts:
        if host in known:
            _add(res.registry_hosts, [host])
        else:
            res.warnings.append(f"the build wrapper downloads from {host}, which is not a known "
                                f"registry: a pull box refuses it unless you pass --allow-host {host}")
    for kind in filter(None, [det.primary, *det.secondary]):
        _add(res.registry_hosts, table.get(kind, {}).get("pull_hosts", []))
    if not approximate:
        return
    for kind in det.secondary:
        if kind in res.presets or kind not in table:
            continue
        _add(res.packages, table[kind].get("debian", []))
        _add(res.prepare_inputs, _PREPARE_INPUTS.get(kind, []))
        res.approximated.append(kind)
    if res.approximated:
        res.reasons.append(f"also {', '.join(res.approximated)}: approximated with Debian "
                           "packages (pin exact versions in .momo/box.json)")
