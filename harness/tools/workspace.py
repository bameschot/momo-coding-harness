"""Workspace tools: tool files a repository brings along in <workdir>/.momo/tools/.

They are committed with the project (.momo/.gitignore lets tools/ and
tools.json through), so a team shares them, and they are code that arrives
with a clone.  Hence:

  * Listing never runs them.  A tool file declares its metadata as a plain
    `TOOL = {...}` literal, read here with `ast.literal_eval`; its `run`
    function is only ever executed in its own process.
  * Nothing runs before the user approves it (`/tools approve`).  The approval
    is a fingerprint of the workdir, the tool's name, the code in .momo/tools/
    (every .py file there, since a tool can import `_helper` files) and the
    Python it runs with, kept in ~/.momo-harness/ (approvals.py) — a
    repository cannot approve itself, and any change asks again.
  * Each call runs as `python -I -B -c <workspace_runner.py>` with the workdir
    as current directory: harness modules cannot be imported, a crash or hang
    cannot take momo down, and Esc or the tool's timeout kills it.  The tool
    may use anything in the workspace — that is the contract, not a sandbox:
    the process has the user's permissions, which is why approval is the gate.

A tool's category (read | write | run | net, declared in TOOL; missing or
unknown counts as write) gives it its roles, like the built-in tools.  The
committed .momo/tools.json can override roles, category and enabled per tool,
and name the interpreter (default .momo/tools/.venv when present, else momo's
own Python).  Built-in and personal tools are never affected by that file.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import momo_dir
from .. import run_store as run_store_mod
from ..approvals import ApprovalStore, fingerprint
from ..paths import momo_home
from . import registry, shell
from .custom import _NAME_RE

CATEGORIES = ("read", "write", "run", "net")
CATEGORY_ROLES = {
    "read":  registry.ALL_ROLES,
    "write": registry.WRITE_ROLES,
    "run":   registry.WRITE_ROLES | {"plan"},
    "net":   registry.ALL_ROLES,
}
# The keys a TOOL literal may have (skills/workspace-tools.md lists exactly these).
KEYS = {"name": str, "description": str, "parameters": dict, "required": list,
        "category": str, "example": dict, "order": int, "timeout": int, "confirm": bool}
REQUIRED_KEYS = ("name", "description", "parameters")
DEFAULT_ORDER = 9500          # after the personal custom tools (9000)
DEFAULT_TIMEOUT = 120
_REPLY_CAP = 4 * 2**20        # a reply bigger than this is not a tool result
_NOT_TOOLS = {"README.md", "requirements.txt"}
_GUIDE = Path(__file__).resolve().parents[2] / "skills" / "workspace-tools.md"
_RUNNER = Path(__file__).with_name("workspace_runner.py")

_STORE = ApprovalStore(lambda: momo_home() / "workspace-tools-approved.json")


# ── settings: .momo/tools.json ───────────────────────────────────────────────

def load_settings(workdir: Path) -> tuple[dict, list[str]]:
    """(settings, notes).  A missing file is empty settings; a broken one is
    ignored with a note (the tools still load, with their own categories)."""
    path = momo_dir.tools_settings_path(workdir)
    if not path.is_file() or path.is_symlink():
        return {}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {}, [f"{momo_dir.MOMO_DIR}/{momo_dir.TOOLS_SETTINGS} ignored: {e}"]
    if not isinstance(data, dict):
        return {}, [f"{momo_dir.MOMO_DIR}/{momo_dir.TOOLS_SETTINGS} ignored: not a JSON object"]
    if not isinstance(data.get("tools", {}), dict):
        return {k: v for k, v in data.items() if k != "tools"}, [
            f"{momo_dir.MOMO_DIR}/{momo_dir.TOOLS_SETTINGS}: \"tools\" must be an object; ignored"]
    return data, []


def save_settings(workdir: Path, data: dict) -> str:
    """Write .momo/tools.json ('' or an ERROR).  Plain 0644 and sorted keys:
    the file is meant to be committed and diffed."""
    folder = momo_dir.ensure(workdir)
    if isinstance(folder, str):
        return folder
    path = momo_dir.tools_settings_path(workdir)
    tools = {k: v for k, v in (data.get("tools") or {}).items() if v}
    data = {**{k: v for k, v in data.items() if k != "tools"}, **({"tools": tools} if tools else {})}
    try:
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tools.json.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError as e:
        return f"ERROR: could not write {path}: {e.strerror or e}"
    return ""


def tool_settings(settings: dict, name: str) -> dict:
    entry = (settings.get("tools") or {}).get(name)
    return entry if isinstance(entry, dict) else {}


# ── the interpreter ──────────────────────────────────────────────────────────

def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def venv_python(root: Path) -> Path | None:
    for rel in ("bin/python", "bin/python3", "Scripts/python.exe"):
        if (root / rel).exists():
            return root / rel
    return None


def interpreter(workdir: Path, settings: dict) -> tuple[Path, str, str]:
    """(python, where it came from, problem) — problem is '' when it may run.
    tools.json "python" first, then .momo/tools/.venv, then momo's own."""
    wd = workdir.resolve()
    configured = settings.get("python")
    if isinstance(configured, str) and configured.strip():
        exe = Path(os.path.normpath(wd / configured.strip()))
        origin = f"\"python\" in {momo_dir.MOMO_DIR}/{momo_dir.TOOLS_SETTINGS}"
    elif (exe := venv_python(momo_dir.tools_root(wd) / ".venv")) is not None:
        origin = f"{momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/.venv"
    else:
        return Path(sys.executable), "momo's own Python", ""
    return exe, origin, check_interpreter(exe, wd)


def check_interpreter(exe: Path, workdir: Path) -> str:
    """Why exe must not run workspace tools ('' when it may).  A Python inside
    the workspace came with the repository, so only a real virtualenv is
    accepted: pyvenv.cfg beside its bin/, and an executable that resolves to a
    Python outside the workspace (a committed script named python does not)."""
    if not exe.exists():
        return f"{exe} does not exist (create it with /tools venv, or fix \"python\" in tools.json)"
    if not _inside(exe, workdir):
        return "" if os.access(exe, os.X_OK) else f"{exe} is not executable"
    rel = exe.relative_to(workdir)
    if not (exe.parent.parent / "pyvenv.cfg").is_file():
        return f"{rel} is not a virtualenv's Python (no pyvenv.cfg) — momo won't run it"
    if os.name != "nt" and _inside(exe.resolve(), workdir):
        return (f"{rel} resolves inside the workspace — a real virtualenv links to a Python "
                f"outside it, so momo won't run this one")
    return ""


# ── discovery (reads, never runs) ────────────────────────────────────────────

@dataclass
class WorkspaceTool:
    name: str
    path: Path
    meta: dict = field(default_factory=dict)
    category: str = "write"
    category_from: str = "default"       # declared | default | tools.json
    roles: frozenset = registry.WRITE_ROLES
    roles_from: str = "category"         # category | tools.json
    status: str = "pending"              # loaded | pending | denied | disabled | error
    reason: str = ""
    fingerprint: str = ""
    notes: list[str] = field(default_factory=list)


def code_hash(workdir: Path) -> str:
    """Every .py file under .momo/tools/ (a tool imports its `_helper` files),
    the virtualenv and caches excepted."""
    root = momo_dir.tools_root(workdir)
    files = []
    for folder, dirs, names in os.walk(root):     # prunes .venv: no walk of site-packages
        dirs[:] = [d for d in dirs if d != "__pycache__" and not (d == ".venv" and folder == str(root))]
        files += [Path(folder, n) for n in names if n.endswith(".py")]
    h = hashlib.sha256()
    for p in sorted(files):
        rel = p.relative_to(root)
        h.update(str(rel).encode() + b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()


def signature(workdir: Path) -> str:
    """What a reload depends on in the workspace — tool code and
    .momo/tools.json — so an unchanged one can skip it.  Approvals are not in
    it: /tools approve|deny reload themselves."""
    h = hashlib.sha256(code_hash(workdir).encode())
    try:
        h.update(momo_dir.tools_settings_path(workdir).read_bytes())
    except OSError:
        pass
    return h.hexdigest()


def _parse(path: Path) -> tuple[dict, str]:
    """(TOOL literal, problem) from a tool file, without executing it."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (OSError, UnicodeDecodeError) as e:
        return {}, f"cannot read it: {e}"
    except SyntaxError as e:
        return {}, f"syntax error on line {e.lineno}: {e.msg}"
    meta, run = None, None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "TOOL"):
            try:
                meta = ast.literal_eval(node.value)
            except (ValueError, SyntaxError, TypeError):
                return {}, "TOOL must be a plain literal (strings, numbers, lists, dicts) — no names or calls"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run":
            run = node
    if meta is None:
        return {}, "it has no module-level TOOL = {...}"
    if not isinstance(meta, dict):
        return {}, "TOOL must be a dict"
    if run is None:
        return meta, "it has no module-level def run(...)"
    if isinstance(run, ast.AsyncFunctionDef):
        return meta, "run must be a plain def, not async"
    a = run.args
    names = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
    if "workdir" not in names and a.kwarg is None:
        return meta, "run must take a workdir keyword argument: def run(..., *, workdir)"
    missing = [p for p in (meta.get("parameters") or {}) if p not in names] if a.kwarg is None else []
    if missing:
        return meta, f"run does not take the parameter(s) {', '.join(missing)}"
    return meta, ""


def _validate(meta: dict, stem: str) -> str:
    for key in REQUIRED_KEYS:
        if key not in meta:
            return f"TOOL has no {key!r}"
    unknown = sorted(set(meta) - set(KEYS))
    if unknown:
        return f"TOOL has unknown key(s) {', '.join(unknown)} (allowed: {', '.join(KEYS)})"
    for key, want in KEYS.items():
        if key in meta and not (isinstance(meta[key], want) and not
                                (want is int and isinstance(meta[key], bool))):
            return f"TOOL[{key!r}] must be a {want.__name__}"
    name = meta["name"]
    if not _NAME_RE.fullmatch(name):
        return f"name {name!r} must be 2-64 characters of a-z, 0-9 and _, starting with a letter"
    if name != stem:
        return f"name {name!r} must match the file name ({stem}.py)"
    if name.startswith("mcp_"):
        return f"name {name!r}: the mcp_ prefix is reserved for MCP servers"
    kind = registry.kind_of(name)
    if kind in ("builtin", "custom"):
        return f"name {name!r} is already a {'built-in' if kind == 'builtin' else 'personal custom'} tool"
    if not meta["description"].strip():
        return "the description is empty: it is how the model learns what the tool is for"
    params = meta["parameters"]
    if not all(isinstance(v, dict) and isinstance(v.get("type", "string"), str) for v in params.values()):
        return "each parameter must be a JSON-schema object like {\"type\": \"string\", \"description\": ...}"
    required = meta.get("required", [])
    if not all(isinstance(r, str) and r in params for r in required):
        return "every name in 'required' must be one of the parameters"
    if not set(meta.get("example", {})) <= set(params):
        return "the example uses an argument that is not a parameter"
    if not 1 <= meta.get("timeout", DEFAULT_TIMEOUT) <= shell.MAX_COMMAND_TIMEOUT:
        return f"timeout must be 1-{shell.MAX_COMMAND_TIMEOUT} seconds"
    return ""


def discover(workdir: Path, settings: dict | None = None) -> list[WorkspaceTool]:
    """Every tool file in .momo/tools/ with its status, without running any."""
    wd = workdir.resolve()
    root = momo_dir.tools_root(wd)
    if root.is_symlink() or not root.is_dir() or momo_dir.momo_root(wd).is_symlink():
        return []
    settings = settings if settings is not None else load_settings(wd)[0]
    python, origin, python_problem = interpreter(wd, settings)
    code = code_hash(wd)
    out = []
    for path in sorted(root.glob("*.py")):
        if path.name.startswith("_") or path.name in _NOT_TOOLS:
            continue
        t = WorkspaceTool(name=path.stem, path=path)
        out.append(t)
        if path.is_symlink() or not path.is_file():
            t.status, t.reason = "error", "it is not a regular file (a symlink?)"
            continue
        meta, problem = _parse(path)
        t.meta = meta
        problem = problem or _validate(meta, path.stem)
        if problem:
            t.status, t.reason = "error", problem
            continue
        own = tool_settings(settings, t.name)
        declared = meta.get("category")
        if own.get("category") in CATEGORIES:
            t.category, t.category_from = own["category"], "tools.json"
        elif declared in CATEGORIES:
            t.category, t.category_from = declared, "declared"
        else:
            t.category = "write"
            if declared is not None:
                t.notes.append(f"unknown category {declared!r} — treated as write")
        t.roles = CATEGORY_ROLES[t.category]
        roles = own.get("roles")
        if isinstance(roles, list):
            bad = [r for r in roles if r not in registry.ROLES]
            if bad:
                t.notes.append(f"tools.json roles {bad} are not roles; ignored")
            t.roles = frozenset(r for r in roles if r in registry.ROLES)
            t.roles_from = "tools.json"
        if python_problem:
            t.status, t.reason = "error", f"its Python ({origin}): {python_problem}"
            continue
        t.fingerprint = fingerprint(workdir=str(wd), name=t.name, code=code, python=str(python))
        if own.get("enabled") is False:
            t.status = "disabled"
            continue
        verdict = _STORE.status(t.fingerprint)
        t.status = {"approved": "loaded"}.get(verdict, verdict)
    return out


# ── loading and calling ──────────────────────────────────────────────────────

@dataclass
class LoadReport:
    workdir: Path
    tools: list[WorkspaceTool] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)       # settings problems
    python: str = ""
    python_origin: str = ""
    signature: str = ""

    def by_status(self, status: str) -> list[WorkspaceTool]:
        return [t for t in self.tools if t.status == status]

    def summary(self) -> str:
        """The start-up notice ('' when there is nothing to say)."""
        lines = list(self.notes)
        if (loaded := self.by_status("loaded")):
            lines.append(f"Workspace tools: {', '.join(t.name for t in loaded)} "
                         f"(from {momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/)")
        if (pending := self.by_status("pending")):
            lines.append(f"Workspace tools waiting for approval: {', '.join(t.name for t in pending)} "
                         f"— read them in {momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/, then "
                         f"/tools approve <name> (or all)")
        for t in self.by_status("error"):
            lines.append(f"Workspace tool {t.path.name} not loaded: {t.reason}")
        return "\n".join(lines)


def _ask_to_reapprove(name: str) -> str:
    return (f"ERROR: {name} was not run: the code in {momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/ or the "
            f"Python it runs with changed since the user approved it. Ask the user to review "
            f"it and run /tools approve {name}.")


def _executor(info: WorkspaceTool, python: Path, timeout: int):
    def run(*, workdir: Path, cancel=None, **args) -> str:
        # The approval covers the code as it was: check it is still that code.
        settings = load_settings(workdir)[0]
        now_python, _, problem = interpreter(workdir, settings)
        if problem:
            return f"ERROR: {info.name} was not run: its Python — {problem}"
        current = fingerprint(workdir=str(workdir.resolve()), name=info.name,
                              code=code_hash(workdir), python=str(now_python))
        if current != info.fingerprint or _STORE.status(current) != "approved":
            return _ask_to_reapprove(info.name)
        try:
            source = info.path.read_text(encoding="utf-8")
        except OSError as e:
            return f"ERROR: {info.name} was not run: cannot read {info.path}: {e.strerror or e}"
        return call(info.name, info.path, source, args, workdir, now_python, timeout, cancel)
    return run


def call(name: str, path: Path, source: str, args: dict, workdir: Path, python: Path,
         timeout: int, cancel=None) -> str:
    """Run one call in its own process (workspace_runner.py) and return the
    text for the model."""
    request = json.dumps({"source": source, "path": str(path), "args": args})
    env = shell.command_env(workdir)
    env.update({"MOMO_WORKDIR": str(workdir), "MOMO_TOOL": name})
    with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, \
            tempfile.TemporaryFile() as err:
        inp.write(request.encode("utf-8"))
        inp.seek(0)
        try:
            proc = subprocess.Popen([str(python), "-I", "-B", "-c", _RUNNER.read_text(encoding="utf-8"),
                                     str(path)], cwd=workdir, stdin=inp, stdout=out, stderr=err,
                                    start_new_session=True, env=env)
        except OSError as e:
            return f"ERROR: {name} could not start ({python}): {e.strerror or e}"
        deadline = time.monotonic() + timeout
        stopped = None
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                stopped = f"{name} was interrupted by the user"
            elif time.monotonic() >= deadline:
                stopped = f"{name} timed out after {timeout}s (its timeout)"
            elif os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > shell.MAX_OUTPUT_BYTES:
                stopped = f"{name} was stopped: it wrote more than {shell.MAX_OUTPUT_BYTES // 2**20} MB"
            if stopped:
                run_store_mod.kill_group(proc)
                break
            time.sleep(0.05)
        out.seek(0)
        raw = out.read(_REPLY_CAP + 1)
        err_tail = shell.output_tail(err)[-1500:]
    if stopped:
        return f"ERROR: {stopped}" + (f"\n[its stderr]\n{err_tail}" if err_tail else "")
    try:
        reply = json.loads(raw.decode("utf-8")) if len(raw) <= _REPLY_CAP else None
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        return (f"ERROR: {name} failed: it exited with code {proc.returncode} and no result"
                + (f"\n[its stderr]\n{err_tail}" if err_tail else ""))
    if "result" in reply:
        return str(reply["result"])
    if reply.get("missing_module"):
        return (f"ERROR: {name} failed: {reply['error']} — the package is not installed for "
                f"workspace tools. Add it to {momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/requirements.txt "
                f"and ask the user to run /tools venv.")
    trace = "\n".join(str(reply.get("traceback", "")).strip().splitlines()[-8:])
    return f"ERROR: {name} failed: {reply.get('error')}" + (f"\n{trace}" if trace else "")


def _marks_index(result: str, args: dict, workdir: Path, index) -> str:
    if index is not None:
        index.mark_dirty()           # it may have changed files
    return result


def _as_tool(info: WorkspaceTool, python: Path) -> registry.Tool:
    meta = info.meta
    schema = registry.tool_schema(info.name, meta["description"], meta["parameters"],
                                  meta.get("required", []))
    return registry.Tool(
        schema, run=_executor(info, python, meta.get("timeout", DEFAULT_TIMEOUT)),
        order=meta.get("order", DEFAULT_ORDER), group="workspace", modes=info.roles,
        inject=("cancel",), example=meta.get("example"), keeps_green=info.category == "read",
        needs_net=info.category == "net", confirm=bool(meta.get("confirm", False)),
        after=_marks_index if info.category in ("write", "run") else None,
        source=f"workspace:{info.path}")


def load_workspace_tools(workdir: Path) -> LoadReport:
    """Discover the workspace's tools and register the approved, enabled ones
    (replacing those of the workspace loaded before)."""
    settings, notes = load_settings(workdir)
    report = LoadReport(workdir, notes=notes, signature=signature(workdir))
    python, report.python_origin, _ = interpreter(workdir.resolve(), settings)
    report.python = str(python)
    report.tools = discover(workdir, settings)
    named = {t.name for t in report.tools}
    for name in (settings.get("tools") or {}):
        if name not in named:
            kind = registry.kind_of(name)
            report.notes.append(f"{momo_dir.MOMO_DIR}/{momo_dir.TOOLS_SETTINGS}: {name!r} is "
                                + ("not a workspace tool (built-in and personal tools keep their roles)"
                                   if kind in ("builtin", "custom") else "not in .momo/tools/")
                                + "; ignored")
    registry.set_workspace([_as_tool(t, python) for t in report.tools if t.status == "loaded"])
    return report


def decide(workdir: Path, names: list[str], approve: bool) -> str:
    """/tools approve|deny: record the decision for each tool's current code."""
    tools = {t.name: t for t in discover(workdir)}
    if names == ["all"]:            # the waiting ones; a denial stands until named again
        names = [n for n, t in tools.items() if t.status == "pending"]
        if not names:
            return "No workspace tools are waiting for approval."
    lines = []
    for name in names:
        t = tools.get(name)
        if t is None:
            lines.append(f"{name}: no such tool in {momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/")
        elif t.status in ("error", "disabled"):
            lines.append(f"{name}: {t.status}{' — ' + t.reason if t.reason else ''}; nothing to "
                         f"{'approve' if approve else 'deny'}")
        else:
            _STORE.decide(t.fingerprint, approve)
            lines.append(f"{name}: {'approved' if approve else 'denied'} "
                         f"({t.category}, roles {', '.join(sorted(t.roles)) or 'none'})")
    return "\n".join(lines)


def describe(t: WorkspaceTool, report: LoadReport | None = None) -> str:
    """One tool for /tools approve and /tools check: what would run, where."""
    lines = [f"{t.name} — {t.status}{': ' + t.reason if t.reason else ''}",
             f"  file:     {t.path}"]
    if t.meta.get("description"):
        lines.append(f"  does:     {t.meta['description']}")
    if t.status != "error" or t.meta:
        lines.append(f"  category: {t.category} ({t.category_from})")
        lines.append(f"  roles:    {', '.join(sorted(t.roles)) or 'none'} "
                     f"({'from tools.json' if t.roles_from == 'tools.json' else 'from its category'})")
    if report is not None:
        lines.append(f"  python:   {report.python} ({report.python_origin})")
    lines += [f"  note:     {n}" for n in t.notes]
    return "\n".join(lines)


def status_hint(t: WorkspaceTool) -> str:
    """Why a tool is not offered, and the command that changes that."""
    return {"pending": f"waiting for approval — /tools approve {t.name}",
            "denied": f"denied — /tools approve {t.name} to allow it",
            "disabled": f"disabled in {momo_dir.MOMO_DIR}/{momo_dir.TOOLS_SETTINGS} — "
                        f"/tools enable {t.name}"}.get(t.status, t.reason)


_NONE_YET = (f"No workspace tools in {momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/ — /tools init "
             f"creates the folder with a guide for writing them.")


def listing(report: LoadReport) -> str:
    """/tools workspace: every tool in .momo/tools/ and whether it is approved."""
    if not report.tools:
        return "\n".join([*report.notes, _NONE_YET])
    approved, pending = len(report.by_status("loaded")), len(report.by_status("pending"))
    lines = [f"Workspace tools in {momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/ — {approved} approved, "
             f"{pending} waiting for approval",
             f"  python: {report.python} ({report.python_origin})", *report.notes]
    for t in report.tools:
        if t.status == "loaded":
            lines.append(f"  ✓ {t.name}  approved · {t.category} · {', '.join(sorted(t.roles)) or 'no roles'}")
        else:
            lines.append(f"  {'?' if t.status == 'pending' else '✗'} {t.name}  {status_hint(t)}")
    return "\n".join(lines)


def check(workdir: Path, name: str | None = None) -> str:
    """/tools check: validate tool files and the interpreter, running nothing."""
    settings, notes = load_settings(workdir)
    python, origin, problem = interpreter(workdir.resolve(), settings)
    report = LoadReport(workdir, notes=notes, python=str(python), python_origin=origin)
    tools = discover(workdir, settings)
    if name:
        tools = [t for t in tools if t.name == name]
        if not tools:
            return f"No tool file {name}.py in {momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/"
    if not tools:
        return _NONE_YET
    lines = [*notes, f"Python: {python} ({origin})" + (f" — {problem}" if problem else ""), ""]
    lines += [describe(t) for t in tools]
    lines.append("\n(Checked without running anything. A tool runs once it is approved: "
                 "/tools approve <name>.)")
    return "\n".join(lines)


def set_override(workdir: Path, name: str, key: str, value) -> str:
    """/tools roles|category|enable|disable: change tools.json for one
    workspace tool (value None: back to automatic)."""
    if name not in {t.name for t in discover(workdir)}:
        kind = registry.kind_of(name)
        if kind in ("builtin", "custom"):
            return (f"ERROR: {name} is a {'built-in' if kind == 'builtin' else 'personal'} tool; "
                    f"{momo_dir.MOMO_DIR}/{momo_dir.TOOLS_SETTINGS} only sets workspace tools "
                    f"(turn it off for a mode with /tools {name} off)")
        return f"ERROR: no workspace tool {name} in {momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/"
    settings, notes = load_settings(workdir)
    if notes:
        return f"ERROR: {notes[0]} — fix the file first"
    entry = dict(tool_settings(settings, name))
    if value is None:
        entry.pop(key, None)
    else:
        entry[key] = value
    settings.setdefault("tools", {})[name] = entry
    return save_settings(workdir, settings)


def init(workdir: Path, force: bool = False) -> str:
    """/tools init: .momo/tools/ with the agent guide (README.md) and an
    empty requirements.txt, both meant to be committed."""
    folder = momo_dir.ensure(workdir, momo_dir.TOOLS)
    if isinstance(folder, str):
        return folder
    done = []
    readme = folder / "README.md"
    if force or not readme.exists():
        guide = _GUIDE.read_text(encoding="utf-8")
        readme.write_text("<!-- Copied from momo's skills/workspace-tools.md by /tools init; "
                          "/tools init --force refreshes it. -->\n\n" + guide, encoding="utf-8")
        done.append("README.md (how to write a workspace tool — for you and for coding agents)")
    req = folder / "requirements.txt"
    if not req.exists():
        req.write_text("# Packages the workspace tools need, one per line (pip format).\n"
                       "# Install them for this machine with /tools venv.\n", encoding="utf-8")
        done.append("requirements.txt")
    where = f"{momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/"
    if not done:
        return f"{where} already has README.md and requirements.txt (/tools init --force refreshes the README)"
    return f"Created in {where}: " + ", ".join(done) + ". Commit them with your tools."


def build_venv(workdir: Path, update: bool, emit) -> str:
    """/tools venv: create .momo/tools/.venv (stdlib venv + ensurepip) and
    pip-install requirements.txt into it; emit(line) streams the output.
    Blocking — the harness runs it on a thread."""
    folder = momo_dir.ensure(workdir, momo_dir.TOOLS)
    if isinstance(folder, str):
        return folder
    root = folder / ".venv"
    if root.is_symlink():
        return f"ERROR: {root} is a symlink — momo only creates a real folder there"
    if not root.exists():
        emit(f"Creating {momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/.venv …")
        done = subprocess.run([sys.executable, "-m", "venv", str(root)], capture_output=True, text=True)
        if done.returncode != 0:
            return f"ERROR: could not create the virtualenv: {(done.stderr or done.stdout).strip()[-800:]}"
    elif not update:
        return (f"{momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/.venv already exists — /tools venv update "
                f"installs requirements.txt into it again")
    python = venv_python(root)
    if python is None:
        return f"ERROR: {root} has no Python in it"
    req = folder / "requirements.txt"
    wanted = [ln for ln in (req.read_text(encoding="utf-8").splitlines() if req.is_file() else [])
              if ln.strip() and not ln.lstrip().startswith("#")]
    if not wanted:
        return (f"Virtualenv ready: {momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/.venv (requirements.txt "
                f"lists nothing to install). Workspace tools now run with it, so approve them again.")
    proc = subprocess.Popen([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                             "--progress-bar", "off", "-r", str(req)], cwd=folder,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        if line.strip():
            emit(line.rstrip())
    code = proc.wait()
    if code != 0:
        return f"ERROR: pip install failed (exit code {code}) — see the output above"
    return (f"Installed requirements.txt into {momo_dir.MOMO_DIR}/{momo_dir.TOOLS}/.venv. "
            f"Workspace tools now run with it, so approve them again (/tools approve all).")
