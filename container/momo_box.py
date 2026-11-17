#!/usr/bin/env python3
"""momo-box: run momo in a container with a clean clone of a workspace.

    momo-box run  [-p TEXT|-] [--repo PATH|URL] [--net pull|locked|open] [-- momo args]
    momo-box web  [--repo PATH|URL] [--name NAME] [--port 8765] [--net …]
    momo-box sync NAME [--follow]     land the box's work as branch momo/NAME
    momo-box detect [--repo PATH] [--write]
    momo-box build [--project] [--pull]
    momo-box start | prepare | allow | netlog | ls | logs | url | stop | rm …

The source goes in as a git bundle (never credentials); the work comes back as
branch momo/<name> in the source repo after every turn and when the box stops.
See the README's "Containers" section.

Runs on the host: Python >= 3.10, standard library only, Docker or Podman.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import detect  # noqa: E402
import boxstatus  # noqa: E402

IMAGE_REPO = "momo-harness"
BOX_PY = "/opt/momo/python/bin/python3"
# Helpers in the box are stdlib-only: -S skips site (site-packages, .pth files),
# which keeps the long-lived ones (watch, nettail) small.
BOX_PY_S = (BOX_PY, "-S")
ENTRY = "/opt/momo/app/container/entrypoint.py"
WEB_PORT_IN = 8765
PROXY_PORT = 3128
LLM_PORT_GW = 9000
# Low-memory mode: on by itself when the engine has less than this, and the
# box's memory cap then (the JVM and Node size their heaps from the cap).
LOW_MEM_BELOW = 4 * 1024 ** 3
LOW_MEM_MEMORY = "1g"
_DEFAULT_LLM = {"ollama": "http://localhost:11434", "llamacpp": "http://localhost:8080"}
# Also a git branch (momo/<name>) and a container name: no "..", no trailing "." or ".lock".
# No "-incoming" ending: momo/<name>-incoming is box <name>'s overflow branch.
_NAME_RE = re.compile(
    r"^(?!.*\.\.)(?!.*\.lock$)(?!.*-incoming$)[a-z0-9]([a-z0-9_.-]{0,38}[a-z0-9_-])?$")
_NAME_HELP = ("lowercase letters, digits, '.', '_' and '-' (max 40; no '..', no trailing '.', "
              "not ending in '-incoming')")
# Where the launcher keeps its git refs in the land repo.  Only a box's tracking ref
# (what it landed last) lives under refs/momo-box/<name>; the temporary refs have
# namespaces of their own, so no box name can collide with them.
TRACK_REF = "refs/momo-box/{}"
BUNDLE_REF = "refs/momo-box-src/{}"
FETCH_REF = "refs/momo-box-fetch/{}"
_SHA_RE = re.compile(r"[0-9a-f]{40,64}")
LATEST_JSON = "/home/momo/exports/latest.json"
LATEST_BUNDLE = "/home/momo/exports/latest.bundle"
TURN_JSON = "/home/momo/.momo-box/turn.json"
BOX_STATUS = "/run/momo-box"       # the status folder inside the box (read-only)
_SMALL_MAX = 8 * 1024 * 1024       # the box's JSON records
# What makes the momo app layer: a change here means a new image.
_APP_PARTS = ("harness", "roles", "skills", "search_sources", "container",
              "momo-coding-harness.py", "requirements.txt", ".dockerignore")


class BoxError(Exception):
    pass


def say(msg: str):
    print(f"momo-box: {msg}", file=sys.stderr, flush=True)


# ── places on the host ───────────────────────────────────────────────────────

def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "momo-box"


def box_dir(name: str) -> Path:
    return cache_dir() / "boxes" / name


def status_dir(name: str) -> Path:
    """What the box's Setup panel shows (boxstatus.py); mounted read-only."""
    return box_dir(name) / "status"


def status_writer(name: str) -> boxstatus.StatusWriter:
    return boxstatus.StatusWriter(status_dir(name))


def bundle_max() -> int:
    """Largest bundle taken from a box (it is written by code we do not trust)."""
    try:
        return int(os.environ.get("MOMO_BOX_MAX_BUNDLE", "") or 2 * 1024 ** 3)
    except ValueError:
        return 2 * 1024 ** 3


def _private_dir(path: Path) -> Path:
    """Only this user gets in: the box's folder holds the source bundle and the
    web token.  (The inbox inside it is mounted by itself, so the container's
    uid still reads it.)"""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _clean(text) -> str:
    """A string the box supplied, safe to print: no control or format characters
    (terminal escape sequences, bidi overrides, newlines)."""
    return "".join(ch for ch in str(text) if ch.isprintable())


def approvals_path() -> Path:
    return Path.home() / ".momo-harness" / "box-approved.json"


def _write_private(path: Path, text: str):
    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def load_meta(name: str) -> dict:
    try:
        return json.loads((box_dir(name) / "box.json").read_text())
    except (OSError, ValueError):
        raise BoxError(f"no box named {name!r} (momo-box ls lists them)") from None


def save_meta(meta: dict):
    _write_private(box_dir(meta["name"]) / "box.json", json.dumps(meta, indent=1))


# ── container engine ─────────────────────────────────────────────────────────

# Where the engines' installers put their CLI when it is not on PATH: Docker
# Desktop's own copy (the /usr/local/bin/docker link it makes can point at the
# installer's disk image, long unmounted), its per-user CLI folder, the Podman
# installer and Homebrew.
_ENGINE_DIRS = {
    "docker": ["/Applications/Docker.app/Contents/Resources/bin",
               "~/Applications/Docker.app/Contents/Resources/bin", "~/.docker/bin",
               "/opt/homebrew/bin", "/usr/local/bin"],
    "podman": ["/opt/podman/bin", "/opt/homebrew/bin", "/usr/local/bin"],
}


def find_runtime(wanted: str | None = None) -> str:
    """docker or podman.  One that is installed but not on PATH (a broken link,
    a shell without Docker Desktop's folder) is found in its usual place, and
    that folder goes on PATH for us and the commands we start: the docker CLI
    runs its credential helper and buildx from the same folder."""
    wanted = wanted or os.environ.get("MOMO_BOX_RUNTIME")
    names = [wanted] if wanted else ["docker", "podman"]
    for rt in names:
        if shutil.which(rt):
            return rt
    for rt in names:
        for folder in _ENGINE_DIRS.get(rt, []):
            folder = os.path.expanduser(folder)
            if shutil.which(rt, path=folder):
                os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
                return rt
    broken = [f"{d}/{rt}" for rt in names for d in ("/usr/local/bin", "/opt/homebrew/bin")
              if os.path.islink(f"{d}/{rt}") and not os.path.exists(f"{d}/{rt}")]
    hint = (f" ({', '.join(broken)} is a broken link: reinstall the CLI tools, e.g. Docker "
            "Desktop → Settings → Advanced)" if broken else "")
    raise BoxError(("neither docker nor podman is installed" if not wanted
                    else f"{wanted} is not installed") + hint)


def host_gateway_name(runtime: str) -> str:
    return "host.containers.internal" if runtime == "podman" else "host.docker.internal"


def _is_local_host(host: str) -> bool:
    h = host.strip("[]").lower()
    return h in ("localhost", "0.0.0.0", "::1", "::") or h.startswith("127.")


def rewrite_llm_url(url: str, runtime: str, system: str | None = None) -> tuple[str, bool]:
    """The inference URL as seen from inside a container, and whether the
    container needs `--add-host host.docker.internal:host-gateway` (Linux Docker:
    Docker Desktop and Podman provide the name themselves)."""
    system = system or platform.system()
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise BoxError(f"--llm: expected a URL like http://localhost:8080, got {url!r}")
    if not _is_local_host(parts.hostname):
        return url, False
    netloc = host_gateway_name(runtime) + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc)), runtime == "docker" and system == "Linux"


def llm_hint(url: str, runtime: str, system: str | None = None) -> str:
    parts = urlsplit(url)
    if not _is_local_host(parts.hostname or ""):
        return "Check the URL and that the server accepts connections from this machine."
    if (system or platform.system()) == "Linux" and runtime == "docker":
        return ("On Linux the server must listen on 0.0.0.0 or the docker bridge address "
                "(e.g. `llama-server --host 0.0.0.0`), not only 127.0.0.1.")
    return f"Check that the server is running on this machine at port {parts.port}."


_PROBE = {"ollama": "/api/tags", "llamacpp": "/v1/models"}


def _probe(url: str, path: str, timeout: float) -> int | str:
    """The HTTP status the server answers `path` with, or why nothing answered."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via a proxy
    try:
        with opener.open(url.rstrip("/") + path, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (OSError, ValueError) as e:
        return str(getattr(e, "reason", e))


def llm_preflight(url: str, provider: str, *, defaulted: bool,
                  timeout: float = 3.0) -> str | None:
    """None when the model server answers like `provider`; else a warning that
    says what is wrong and which flags fix it.  Asked from this machine, before
    the box is built: the box reaches the same server through the gateway.
    `defaulted`: neither --llm nor --provider was given (a lost line of a
    multi-line command, for one)."""
    flags = ("; pass --llm URL --provider ollama|llamacpp" if defaulted else "")
    what = f"{provider} at {url}" + (" (the default: no --llm/--provider given)" if defaulted else "")
    got = _probe(url, _PROBE[provider], timeout)
    if got == 200:
        return None
    if isinstance(got, str):
        return f"nothing answers at {url} ({got}): the box expects {what}{flags}."
    other = "llamacpp" if provider == "ollama" else "ollama"
    if got == 404 and _probe(url, _PROBE[other], timeout) == 200:
        return (f"{url} answers like {other}, not {provider}: pass --provider {other} "
                "(the gateway lets only the provider's own requests through).")
    return None     # it answered (401, 500…): momo reports the details itself


class Engine:
    """Thin wrapper over the docker / podman CLI (same syntax for what we use)."""

    def __init__(self, runtime: str, dry_run: bool = False):
        self.rt = runtime
        self.dry_run = dry_run

    def cmd(self, *args: str) -> list[str]:
        return [self.rt, *args]

    def run(self, *args: str, check: bool = True, capture: bool = True,
            input: bytes | None = None, **kw) -> subprocess.CompletedProcess:
        argv = self.cmd(*args)
        if self.dry_run:
            print(shlex.join(argv))
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        r = subprocess.run(argv, capture_output=capture, input=input, **kw)
        if check and r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", "replace").strip() if capture else ""
            raise BoxError(f"{self.rt} {args[0]} failed: {err or f'exit {r.returncode}'}")
        return r

    def ok(self, *args: str) -> bool:
        if self.dry_run:
            return False
        return subprocess.run(self.cmd(*args), capture_output=True).returncode == 0

    def running(self, container: str) -> bool:
        if self.dry_run:
            return False
        r = subprocess.run(self.cmd("inspect", "-f", "{{.State.Running}}", container),
                           capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.strip() == "true"

    def exists(self, container: str) -> bool:
        return self.ok("container", "inspect", container)

    def cp_out(self, container: str, path: str, dest: Path | None = None,
               limit: int = _SMALL_MAX):
        """One regular file from a container (running or stopped): its bytes, or
        with `dest` written there (True).  None when it is absent or not a regular
        file (a symlink the box planted is never followed).  The file comes from
        code we do not trust, so it is streamed and refused over `limit`."""
        if self.dry_run:
            return None
        proc = subprocess.Popen(self.cmd("cp", f"{container}:{path}", "-"),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                for m in tar:
                    if not m.isfile():
                        continue
                    if m.size > limit:
                        raise BoxError(f"{path} in the box is {m.size:,} bytes, over the "
                                       f"{limit:,} limit (MOMO_BOX_MAX_BUNDLE raises it for "
                                       "bundles)")
                    f = tar.extractfile(m)
                    if f is None:
                        return None
                    if dest is None:
                        return f.read()
                    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
                    with open(tmp, "wb") as out:
                        shutil.copyfileobj(f, out)
                    os.replace(tmp, dest)
                    return True
        except (tarfile.TarError, OSError):
            return None
        finally:
            proc.stdout.close()
            proc.kill()
            proc.wait()
        return None


# ── the source repo ──────────────────────────────────────────────────────────

def _git(repo: Path, *args: str, check: bool = True, env: dict | None = None,
         text: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=text,
                       env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        err = r.stderr if text else r.stderr.decode("utf-8", "replace")
        raise BoxError(f"git {args[0]} in {repo}: {err.strip()}")
    return r


def is_url(repo: str) -> bool:
    return "://" in repo or bool(re.match(r"^[\w.-]+@[\w.-]+:", repo))


def mirror_for(url: str) -> Path:
    """A host-side mirror of a remote repo (fetched with the host's credentials)."""
    path = cache_dir() / "mirrors" / hashlib.sha256(url.encode()).hexdigest()[:16]
    if (path / "HEAD").is_file():
        say(f"updating mirror of {url}")
        _git(path, "remote", "update", "--prune")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        say(f"cloning {url}")
        r = subprocess.run(["git", "clone", "-q", "--mirror", url, str(path)])
        if r.returncode != 0:
            shutil.rmtree(path, ignore_errors=True)
            raise BoxError(f"could not clone {url}")
    return path


@dataclass
class Source:
    kind: str                  # local | url | empty
    repo: Path | None          # the git dir/worktree the bundle comes from
    land: Path                 # where momo/<name> is created
    sha: str | None = None
    ref_name: str | None = None
    label: str = ""


def resolve_source(repo: str | None, ref: str | None, empty: bool, land: str | None,
                   name: str) -> Source:
    if empty:
        land_path = Path(land).expanduser().resolve() if land else box_dir(name) / "repo"
        return Source("empty", None, land_path, label="empty workspace")
    repo = repo or "."
    if is_url(repo):
        path = mirror_for(repo)
        kind, label = "url", repo
    else:
        p = Path(repo).expanduser().resolve()
        r = _git(p, "rev-parse", "--show-toplevel", check=False)
        if r.returncode != 0:
            raise BoxError(f"{p} is not a git repository (git init it, or use --empty)")
        path, kind, label = Path(r.stdout.strip()), "local", str(p)
    rev = ref or "HEAD"
    r = _git(path, "rev-parse", "--verify", "-q", f"{rev}^{{commit}}", check=False)
    if r.returncode != 0:
        raise BoxError(f"{label}: {rev} is not a commit" + (" (the repo has no commits yet)"
                                                             if rev == "HEAD" else ""))
    sha = r.stdout.strip()
    full = _git(path, "rev-parse", "--symbolic-full-name", rev, check=False).stdout.strip()
    ref_name = full[len("refs/heads/"):] if full.startswith("refs/heads/") else None
    land_path = Path(land).expanduser().resolve() if land else path
    return Source(kind, path, land_path, sha, ref_name, label)


def write_bundle(src: Source, inbox: Path, with_uncommitted: bool, *, name: str = "box",
                 all_refs: bool = False):
    """The box's source: the history of the one commit it starts from, through a
    temporary ref (a bundle needs a ref name).  Other branches, tags, stashes and
    remote-tracking refs stay out unless all_refs."""
    inbox.mkdir(parents=True, exist_ok=True)
    os.chmod(inbox, 0o755)
    bundle = inbox / "source.bundle"
    ref = BUNDLE_REF.format(name)
    _git(src.repo, "update-ref", ref, src.sha)
    try:
        _git(src.repo, "bundle", "create", "-q", str(bundle), *(["--all"] if all_refs else [ref]))
    finally:
        _git(src.repo, "update-ref", "-d", ref, check=False)
    os.chmod(bundle, 0o644)
    if with_uncommitted and src.kind == "local":
        patch = uncommitted_patch(src.repo)
        (inbox / "dirty.patch").write_bytes(patch)
        os.chmod(inbox / "dirty.patch", 0o644)
        if patch:
            say(f"including uncommitted changes ({len(patch):,} bytes)")


def uncommitted_patch(repo: Path) -> bytes:
    """`git diff --binary HEAD` including untracked files, via a temporary index."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(repo, "read-tree", "HEAD", env=env)
        _git(repo, "add", "-A", env=env)
        return _git(repo, "diff", "--cached", "--binary", "HEAD", env=env, text=False).stdout


# ── landing on the host ──────────────────────────────────────────────────────

_ZERO = "0" * 40


def box_info(data) -> dict | None:
    """The box's export record (latest.json), which the model could have written
    itself: only the fields we use, typed, with printable strings.  None when it
    is not a record at all."""
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None

    def sha(key):
        v = data.get(key)
        return v if isinstance(v, str) and _SHA_RE.fullmatch(v) else None

    def num(key):
        v = data.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) else None
    skipped = data.get("skipped_large")
    return {"export": sha("export"), "head": sha("head"), "base": sha("base"),
            "since": sha("since"), "bundle": bool(data.get("bundle")),
            "commits": num("commits") or 0, "turn": num("turn"), "exit_code": num("exit_code"),
            "shortstat": _clean(data.get("shortstat") or "")[:200],
            "skipped_large": [_clean(x)[:200] for x in skipped if isinstance(x, str)][:20]
            if isinstance(skipped, list) else []}


def _rev(repo: Path, ref: str) -> str | None:
    r = _git(repo, "rev-parse", "--verify", "-q", ref, check=False)
    return r.stdout.strip() or None


def _checked_out(repo: Path, branch: str) -> bool:
    r = _git(repo, "worktree", "list", "--porcelain", check=False)
    return f"branch refs/heads/{branch}" in r.stdout.splitlines()


def land(repo: Path, bundle: Path, name: str, info: dict, out: dict | None = None) -> str:
    """Fetch the box's snapshot into `repo` as branch momo/<name>.  The branch is
    only moved while it still points at what momo-box put there last; otherwise
    (you committed on it, or have it checked out) the work goes to
    momo/<name>-incoming.  Returns the line to show."""
    if not (repo / ".git").exists() and not (repo / "HEAD").is_file():
        if repo.exists() and any(repo.iterdir()):
            raise BoxError(f"{repo} is not a git repository")
        repo.mkdir(parents=True, exist_ok=True)       # an --empty box's own repo
        _git(repo, "init", "-q")
    branch, track = f"momo/{name}", TRACK_REF.format(name)
    tmp_ref = FETCH_REF.format(name)
    # The bundle was written inside the box: check every object before it joins the repo.
    r = _git(repo, "-c", "fetch.fsckObjects=true", "fetch", "-q", "--no-tags",
             "--no-write-fetch-head", str(bundle), f"+refs/momo/export:{tmp_ref}", check=False)
    if r.returncode != 0:
        hint = (" The repo lacks the box's base commit: fetch it first, or pass --land-repo."
                if "prerequisite" in r.stderr else "")
        raise BoxError(f"could not fetch the box's work into {repo}: "
                       f"{_clean(r.stderr.strip())}{hint}")
    new = _rev(repo, tmp_ref)
    _git(repo, "update-ref", "-d", tmp_ref)
    cur, last = _rev(repo, f"refs/heads/{branch}"), _rev(repo, track)
    target, why = branch, ""
    if cur == new:
        return f"{branch} is up to date"
    if cur is not None and (cur != last or _checked_out(repo, branch)):
        target = f"{branch}-incoming"
        # No tracking ref: the branch is an earlier box's of this name (rm keeps the
        # branch, it is that box's work) or yours.  Either way it is not ours to move.
        why = ("is checked out" if cur == last else
               "was not made by this box" if last is None else
               "has commits momo-box did not make")
        say(f"{branch} {why}; this sync goes to {target}")
        _git(repo, "update-ref", f"refs/heads/{target}", new)
    else:
        _git(repo, "update-ref", f"refs/heads/{branch}", new, cur or _ZERO)
        _git(repo, "update-ref", track, new)
    info = box_info(info) or {}
    commits = info.get("commits", 0)
    extra = " (incl. uncommitted work)" if info.get("export") != info.get("head") else ""
    stat = info.get("shortstat") or ""
    turn = f" (turn {info['turn']})" if info.get("turn") is not None else ""
    skipped = info.get("skipped_large") or []
    line = (f"{target} ← {commits} commit{'s' if commits != 1 else ''}{extra}"
            f"{', ' + stat if stat else ''}{turn}")
    if skipped:
        line += f"; left out (over 10 MB): {', '.join(skipped[:5])}"
    if out is not None:         # for the Setup panel's sync history
        out.update(landed=True, branch=target, incoming=target != branch,
                   reason=f"{branch} {why}" if why else "", commits=commits, shortstat=stat,
                   skipped_large=skipped, head=new or "")
    return line


# ── toolchain image ──────────────────────────────────────────────────────────

def app_hash() -> str:
    h = hashlib.sha256()
    for part in _APP_PARTS:
        p = REPO_ROOT / part
        files = [p] if p.is_file() else sorted(
            f for f in p.rglob("*") if f.is_file() and "__pycache__" not in f.parts)
        for f in files:
            h.update(f.relative_to(REPO_ROOT).as_posix().encode() + b"\0")
            h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", s.lower()).strip("-.") or "project"


def image_tag(res: detect.Resolved, tree, project: str, app: str) -> str:
    """momo-box/<project>:<sha12 of everything that goes into the image>."""
    if res.source == "default" and not res.packages:
        return f"{IMAGE_REPO}:{app[:12]}"
    h = hashlib.sha256(json.dumps([res.base, sorted(res.packages), res.dockerfile,
                                   res.base_dockerfile, res.base_context, app]).encode())
    for rel in filter(None, (res.dockerfile, res.base_dockerfile)):
        h.update((tree.read(rel) or "").encode())
    return f"momo-box/{_slug(project)}:{h.hexdigest()[:12]}"


def check_approval(res: detect.Resolved, assume_yes: bool) -> None:
    """A repo-supplied config (box.json, devcontainer, a Dockerfile) runs code on
    the container engine when built: show it and ask once per version."""
    if not res.needs_approval:
        return
    net = res.network
    fp = hashlib.sha256((res.approval_text + json.dumps([
        res.base, res.packages, res.prepare, res.pull_hosts, res.env,
        None if net is None else [net.instructions, net.replace_defaults, net.allow, net.deny],
    ])).encode()).hexdigest()
    path = approvals_path()
    try:
        approved = json.loads(path.read_text())
    except (OSError, ValueError):
        approved = {}
    if fp in approved:
        return
    toolchain = res.source in ("box.json", "devcontainer")
    print("─" * 60, file=sys.stderr)
    what = f"its momo-box ({res.source})" if toolchain else "its momo-box network"
    print(f"This repository configures {what}:", file=sys.stderr)
    # The repo's own text: printable characters only (no terminal escape sequences).
    print(_clean_lines(res.approval_text.rstrip().encode()), file=sys.stderr)
    if toolchain:
        print(f"→ base {res.base}; packages {' '.join(res.packages) or '-'}; "
              f"prepare {res.prepare or '-'}; extra hosts {res.pull_hosts or '-'}",
              file=sys.stderr)
        print("Building and running it executes this configuration in a container.",
              file=sys.stderr)
    if net is not None:
        print(f"→ network rules ({detect.NETWORK_JSON}), for a pull box and the dependency "
              "prefetch:", file=sys.stderr)
        if net.replace_defaults:
            print("    replaces the toolchain's public registries", file=sys.stderr)
        for rule, why in net.allow.items():
            print(f"    allow {_clean(rule)}" + (f"  — {_clean(why)}" if why else ""),
                  file=sys.stderr)
        for rule, why in net.deny.items():
            print(f"    deny  {_clean(rule)}" + (f"  — {_clean(why)}" if why else ""),
                  file=sys.stderr)
        if net.instructions:
            print("    and tells the model (instructions above).", file=sys.stderr)
        for warning in net.warnings:
            print(f"    note: {_clean(warning)}", file=sys.stderr)
    print("─" * 60, file=sys.stderr)
    if not assume_yes:
        if not sys.stdin.isatty():
            raise BoxError("the box configuration needs approval: run interactively once, or "
                           "pass --yes")
        answer = input("Approve this box configuration? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            raise BoxError("not approved")
    approved[fp] = {"ts": time.time(), "source": res.source, "base": res.base}
    _write_private(path, json.dumps(approved, indent=1))


def _project_context(tree, dest: Path):
    """The project's files at the box's revision, as a Docker build context."""
    if isinstance(tree, detect.GitTree):
        data = _git(tree.repo, "archive", "--format=tar", tree.rev, text=False).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            _extract(tar, dest)
    else:
        shutil.copytree(tree.root, dest, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", ".momo", "node_modules"))


def _extract(tar: tarfile.TarFile, dest: Path, has_filter: bool | None = None):
    """extractall with the "data" filter; on a Python that predates it (3.10/3.11
    before their security releases), the same rules by hand: plain files and
    folders only, nothing absolute or outside dest."""
    if hasattr(tarfile, "data_filter") if has_filter is None else has_filter:
        tar.extractall(dest, filter="data")
        return
    members = []
    for m in tar:
        parts = Path(m.name).parts
        if m.name.startswith(("/", "\\")) or ".." in parts or not (m.isfile() or m.isdir()):
            continue
        m.mode = (m.mode or 0o644) & 0o755
        members.append(m)
    tar.extractall(dest, members=members)


def qualify_image(ref: str) -> str:
    """A Docker Hub short name with its registry spelled out (`gradle:8` →
    `docker.io/library/gradle:8`, `user/img` → `docker.io/user/img`).  Docker
    assumes Docker Hub; Podman's short-name rules may refuse, or stop to ask on a
    terminal, when its registry list has more than one entry."""
    first, sep, _ = ref.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return ref
    return f"docker.io/{ref}" if sep else f"docker.io/library/{ref}"


def _local_ref(line: str) -> str:
    """An `images` row without Podman's `localhost/` prefix for images built here."""
    return line[len("localhost/"):] if line.startswith("localhost/") else line


def check_builder(eng: Engine):
    """The Dockerfile keeps its offline steps offline with `RUN --network=none`,
    which Docker's legacy builder refuses ("requires BuildKit") halfway through a
    build: say so before starting.  Podman's builder (buildah) supports it."""
    if eng.rt != "docker" or eng.dry_run:
        return
    if os.environ.get("DOCKER_BUILDKIT") == "0":
        raise BoxError("DOCKER_BUILDKIT=0 selects Docker's legacy builder, which can't build "
                       "the momo-box image (it needs BuildKit for RUN --network=none); unset it")
    if not eng.ok("buildx", "version"):
        raise BoxError("Docker's BuildKit plugin (buildx) is missing; the momo-box image needs "
                       "it (RUN --network=none). Install it (Debian/Ubuntu: docker-buildx-plugin "
                       "or docker-buildx; Docker Desktop has it) or use Podman (--runtime podman)")


def build_image(eng: Engine, res: detect.Resolved, tree, tag: str, *, pull: bool = False,
                with_extras: bool = True) -> str:
    """Build `tag`: [devcontainer Dockerfile →] base + momo overlay [→ project Dockerfile]."""
    check_builder(eng)
    base = qualify_image(res.base) if eng.rt == "podman" else res.base
    with tempfile.TemporaryDirectory(prefix="momo-box-ctx-") as tmp:
        ctx = Path(tmp) / "ctx"
        if res.base_dockerfile or res.dockerfile:
            _project_context(tree, ctx)
        if res.base_dockerfile:
            base = tag.replace(":", "-devbase:", 1) if ":" in tag else tag + "-devbase"
            say(f"building the devcontainer image {res.base_dockerfile}")
            eng.run("build", *(["--pull"] if pull else []), "-t", base,
                    "-f", str(ctx / res.base_dockerfile), str(ctx / res.base_context),
                    capture=False)
        overlay = tag if not res.dockerfile else tag.replace(":", "-overlay:", 1)
        say(f"building {overlay} (base {base})")
        eng.run("build", *(["--pull"] if pull and not res.base_dockerfile else []),
                "-t", overlay, "-f", str(HERE / "Dockerfile"),
                "--build-arg", f"BASE={base}",
                "--build-arg", f"PACKAGES={' '.join(res.packages)}",
                "--build-arg", f"WITH_EXTRAS={1 if with_extras else 0}",
                str(REPO_ROOT), capture=False)
        if res.dockerfile:
            say(f"building {tag} from {res.dockerfile}")
            eng.run("build", "-t", tag, "-f", str(ctx / res.dockerfile),
                    "--build-arg", f"MOMO_BOX_BASE={overlay}", str(ctx), capture=False)
    return tag


def ensure_image(eng: Engine, res: detect.Resolved, tree, project: str, *,
                 rebuild: bool = False, pull: bool = False) -> str:
    tag = image_tag(res, tree, project, app_hash())
    if rebuild or pull or not eng.ok("image", "inspect", tag):
        build_image(eng, res, tree, tag, pull=pull)
        prune_images(eng, tag)
    return tag


def prune_images(eng: Engine, keep: str):
    """Drop older builds of the same image (same repository, other tags): each momo
    or toolchain change makes a new tag.  An image a container still uses stays."""
    if eng.dry_run:
        return
    repo = keep.rsplit(":", 1)[0]
    r = eng.run("images", repo, "--format", "{{.Repository}}:{{.Tag}}", check=False)
    for line in (r.stdout or b"").decode().split():
        line = _local_ref(line)
        if line not in (keep, f"{repo}:latest") and not line.endswith(":<none>"):
            for suffix in ("", "-overlay", "-devbase"):
                eng.run("rmi", line.replace(":", f"{suffix}:", 1) if suffix else line,
                        check=False)


def default_image(eng: Engine) -> str:
    """The plain momo image (Debian + git), used for the gateway."""
    res = detect.Resolved(source="default", base=detect.load_presets()["default_base"])
    return ensure_image(eng, res, None, "momo")


# ── one box ──────────────────────────────────────────────────────────────────

@dataclass
class Box:
    name: str
    ui: str                         # web | prompt
    net: str                        # open | pull | locked
    image: str
    gw_image: str
    runtime: str
    llm: str                        # as given (host view)
    provider: str
    model: str | None = None
    port: int = WEB_PORT_IN
    token: str | None = None
    extra: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    allow_lines: list[str] = field(default_factory=list)   # the gateway's allow.txt (pull)
    prepare: list[str] = field(default_factory=list)
    prepare_inputs: list[str] = field(default_factory=list)
    inbox: Path | None = None
    status: Path | None = None      # host-written status for the Setup panel (read-only inside)
    ref: str | None = None
    ref_name: str | None = None
    interactive: bool = False       # docker run -i (stdin for -p)
    prompt_stdin: bool = False      # -p -
    memory: str | None = None
    cpus: str | None = None
    pids: int = 512
    tmp_size: str = "2g"
    prepare_net: str = "pull"       # pull: through the registry allowlist | open (gated boxes)
    low_mem: bool = False           # --low-mem: no build daemons, a small code index
    llm_filter: bool = True         # the gateway forwards inference requests only (http)
    llm_paths: list[str] = field(default_factory=list)
    system: str = field(default_factory=platform.system)
    prior: dict = field(default_factory=dict)       # a kept box's record (box.json)

    @property
    def container(self) -> str:
        return f"momo-{self.name}"

    @property
    def gw(self) -> str:
        return f"momo-{self.name}-gw"

    @property
    def network(self) -> str:
        return f"momo-{self.name}-net"

    @property
    def gated(self) -> bool:
        return self.net in ("locked", "pull")

    def llm_in_box(self) -> tuple[str, bool]:
        """(momo's --host inside the box, add-host needed).  A gated box reaches the
        server only through the gateway: an http URL to gw:9000, or for https the
        server's own name as a gateway alias so the certificate still matches."""
        url, add_host = rewrite_llm_url(self.llm, self.runtime, self.system)
        if not self.gated:
            return url, add_host
        parts = urlsplit(url)
        if parts.scheme == "https" and not _is_ip(parts.hostname or ""):
            return url, False
        return urlunsplit(parts._replace(netloc=f"gw:{LLM_PORT_GW}")), False

    def llm_target(self) -> tuple[str, int, str | None]:
        """(host, port, alias) the gateway forwards to; alias for https by name."""
        url, _ = rewrite_llm_url(self.llm, self.runtime, self.system)
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        alias = parts.hostname if parts.scheme == "https" and not _is_ip(parts.hostname or "") \
            else None
        return parts.hostname or "", port, alias

    @property
    def proxied_prepare(self) -> bool:
        """The dependency prefetch goes through the gateway's allowlist proxy."""
        return self.gated and bool(self.prepare) and self.prepare_net == "pull"

    def run_env(self) -> dict[str, str]:
        """The engine CLI's environment: secrets travel as `-e NAME` (no value on a
        command line other users can read with ps)."""
        env = dict(os.environ)
        if self.token:
            env["MOMO_WEB_TOKEN"] = self.token
        return env

    def _hardening(self) -> list[str]:
        out = ["--init", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--read-only", "--tmpfs", f"/tmp:rw,exec,mode=1777,size={self.tmp_size}",
               "--pids-limit", str(self.pids)]
        if self.runtime == "podman":
            # Podman's --read-only also leaves /run and /var/tmp writable; turning
            # that off makes /dev/shm read-only too, which Docker keeps (64 MB).
            out += ["--read-only-tmpfs=false",
                    "--tmpfs", "/dev/shm:rw,nosuid,nodev,noexec,size=64m"]
        if self.memory:
            out += ["--memory", self.memory]
        if self.cpus:
            out += ["--cpus", self.cpus]
        return out

    def _volumes(self) -> list[str]:
        out = ["-v", f"momo-{self.name}-ws:/workspace", "-v", f"momo-{self.name}-home:/home/momo"]
        if self.inbox is not None:
            out += ["-v", f"{self.inbox}:/in:ro"]
        if self.status is not None:
            out += ["-v", f"{self.status}:{BOX_STATUS}:ro"]
        return out

    def momo_env(self) -> dict[str, str]:
        host, _ = self.llm_in_box()
        env = {"MOMO_UI": self.ui, "MOMO_NET": self.net, "MOMO_PROVIDER": self.provider,
               "MOMO_HOST": host, "MOMO_BOX_NAME": self.name,
               "MOMO_NOTES": json.dumps(self.notes), "MOMO_BOX_ENV": json.dumps(self.env),
               "MOMO_LLM_HINT": llm_hint(self.llm, self.runtime, self.system)}
        if self.model:
            env["MOMO_MODEL"] = self.model
        if self.ref:
            env["MOMO_REF"] = self.ref
        if self.ref_name:
            env["MOMO_REF_NAME"] = self.ref_name
        if self.ui == "web":
            env["MOMO_WEB_PUBLIC_URL"] = f"http://127.0.0.1:{self.port}/"
            if self.token:
                env["MOMO_WEB_TOKEN"] = self.token
        if self.prompt_stdin:
            env["MOMO_PROMPT_STDIN"] = "1"
        if self.low_mem:
            env["MOMO_LOW_MEM"] = "1"
        if self.net == "pull":
            env["MOMO_PROXY"] = f"http://gw:{PROXY_PORT}"
            env["MOMO_NO_PROXY"] = json.dumps(["gw"])
        return env

    def momo_argv(self) -> list[str]:
        """`run` for momo itself."""
        argv = ["run", "--pull=never", "--name", self.container, "--hostname", "momo-box",
                "--label", "momo-box=1", "--label", f"momo-box.name={self.name}",
                "--stop-timeout", "30", *self._hardening(), *self._volumes()]
        _, add_host = self.llm_in_box()
        if self.gated:
            argv += ["--network", self.network, "--network-alias", "momo"]
        else:
            if add_host:
                argv += ["--add-host", f"{host_gateway_name('docker')}:host-gateway"]
            if self.ui == "web":
                argv += ["-p", f"127.0.0.1:{self.port}:{WEB_PORT_IN}"]
        for k, v in sorted(self.momo_env().items()):
            argv += ["-e", k if k == "MOMO_WEB_TOKEN" else f"{k}={v}"]   # see run_env
        if self.ui == "web":
            argv.insert(1, "-d")
        elif self.interactive:
            argv.insert(1, "-i")
        argv += [self.image, "start", "--", *self.extra]
        return argv

    def gw_argv(self, proxy: bool | None = None, serve: bool = True) -> list[str]:
        """`run` for the gateway (on the default network; joined to the box's next).
        proxy: run the registry allowlist proxy (default: a pull box).  serve=False
        is the gateway for a prefetch only: no model server, no web UI."""
        proxy = self.net == "pull" if proxy is None else proxy
        host, port, _alias = self.llm_target()
        argv = ["run", "--pull=never", "-d", "--name", self.gw, "--label", "momo-box=1",
                "--label", f"momo-box.name={self.name}", *self._hardening(),
                # non-root, no capabilities: an https alias listens on the server's own port
                "--sysctl", "net.ipv4.ip_unprivileged_port_start=0",
                "-v", f"momo-{self.name}-gw:/gw"]
        _, add_host = rewrite_llm_url(self.llm, self.runtime, self.system)
        if add_host and serve:
            argv += ["--add-host", f"{host_gateway_name('docker')}:host-gateway"]
        if self.ui == "web" and serve:
            argv += ["-p", f"127.0.0.1:{self.port}:{WEB_PORT_IN}"]
        argv += ["-e", f"MOMO_BOX_NAME={self.name}", self.gw_image, "gw"]
        if serve:
            listen = port if _alias else LLM_PORT_GW
            if self.llm_filter and self.llm_paths and not _alias \
                    and urlsplit(self.llm).scheme == "http":
                # Inference requests only: the rest of the server's API stays out of reach.
                argv += ["--filter", f"{listen}={host}:{port}", "--paths", ",".join(self.llm_paths)]
            else:
                argv.append(f"{listen}={host}:{port}")
            if self.ui == "web":
                argv.append(f"{WEB_PORT_IN}=momo:{WEB_PORT_IN}")
        if proxy:
            argv += ["--proxy", str(PROXY_PORT), "--allow-file", "/gw/allow.txt"]
        # Every decision of the proxy and the model filter, for netlog and the Setup panel.
        argv += ["--log", "/gw/net.log", "--box", self.name]
        return argv

    def gw_connect_argv(self) -> list[str]:
        _host, _port, alias = self.llm_target()
        argv = ["network", "connect", "--alias", "gw"]
        if alias:
            argv += ["--alias", alias]
        return [*argv, self.network, self.gw]

    def prepare_argv(self, force: bool = False) -> list[str]:
        """A throwaway container that pre-fetches dependencies.  For a pull or locked
        box it sits on the box's internal network and reaches only the registries on
        the allowlist, through the gateway; otherwise (an open box, or --prepare-net
        open) it is on the open network."""
        env = {"MOMO_PREPARE": json.dumps(self.prepare),
               "MOMO_PREPARE_INPUTS": json.dumps(self.prepare_inputs),
               "MOMO_BOX_ENV": json.dumps(self.env), "MOMO_BOX_NAME": self.name}
        if self.low_mem:
            env["MOMO_LOW_MEM"] = "1"
        network = []
        if self.proxied_prepare:
            env.update(MOMO_NET="pull", MOMO_PROXY=f"http://gw:{PROXY_PORT}",
                       MOMO_NO_PROXY=json.dumps(["gw"]))
            network = ["--network", self.network]
        if self.ref:
            env["MOMO_REF"] = self.ref
        if self.ref_name:
            env["MOMO_REF_NAME"] = self.ref_name
        if force:
            env["MOMO_PREPARE_FORCE"] = "1"
        argv = ["run", "--pull=never", "--rm", "--name", f"momo-{self.name}-prepare",
                "--label", "momo-box=1",
                *self._hardening(), *self._volumes(), *network]
        for k, v in sorted(env.items()):
            argv += ["-e", f"{k}={v}"]
        return [*argv, self.image, "prepare"]


def _is_ip(host: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def build_allowlist(res: detect.Resolved, cli_hosts: list[str], presets: dict) -> list[str]:
    """The gateway's allow.txt lines (see forward._Rule), strongest first:
    `+` the user's own hosts (--allow-host; beat a deny), `!` the repo's denies,
    then the repo's allows (.momo/network.json, box.json pull_hosts), then the
    toolchains' registries (all known ones when nothing was detected) unless
    network.json replaces them."""
    lines: list[str] = []
    seen: set[str] = set()

    def add(prefix: str, rule: str, why: str):
        if prefix + rule in seen:
            return
        seen.add(prefix + rule)
        why = " ".join(why.replace("#", "").split())
        lines.append(f"{prefix}{rule}" + (f"  # {why}" if why else ""))

    try:
        mine = [detect.parse_host_rule(h, "--allow-host")[0]
                for h in detect.expand_hosts(cli_hosts, presets)]
    except detect.ConfigError as e:
        raise BoxError(str(e)) from None
    for rule in mine:
        add("+", rule, "--allow-host")
    rules = res.network
    for rule, why in (rules.deny if rules else {}).items():
        add("!", rule, why or detect.NETWORK_JSON)
    for rule, why in (rules.allow if rules else {}).items():
        add("", rule, why or detect.NETWORK_JSON)
    for rule in res.pull_hosts:
        add("", rule, detect.BOX_JSON)
    if not (rules and rules.replace_defaults):
        registries = res.registry_hosts or detect.expand_hosts(
            presets["pull_hosts"]["default"], presets)
        for rule in registries:
            add("", rule, "registry")
    return lines


def allowed_hosts(lines: list[str]) -> list[str]:
    """The rules an allow.txt lets through (its allows, without the reasons)."""
    out = []
    for ln in lines:
        rule = ln.split("#", 1)[0].strip()
        if rule and not rule.startswith("!"):
            out.append(rule.lstrip("+"))
    return out


def describe_allowlist(lines: list[str]) -> list[str]:
    """allow.txt lines for people: `allow host  (reason)` / `deny host  (reason)`."""
    out = []
    for ln in lines:
        rule, _, why = ln.partition("#")
        rule, why = rule.strip(), why.strip()
        verb = "deny " if rule.startswith("!") else "allow"
        out.append(f"{verb} {rule.lstrip('!+')}" + (f"  ({why})" if why else ""))
    return out or ["(no hosts: the box reaches only the model server)"]


def box_notes(name: str, net: str, res: detect.Resolved, pull_hosts: list[str],
              land_label: str, memory: str | None = None, low_mem: bool = False) -> list[str]:
    """What the model is told about the box (momo --environment-note)."""
    notes = [f"Container: momo-box `{name}` — a fresh clone of the project. Your work is synced "
             f"to the user's repository ({land_label}) as branch `momo/{name}` after every turn, "
             "uncommitted changes included; commit when a piece of work is done."]
    tc = f"Toolchain: image `{res.base}`"
    if res.packages:
        tc += f" + {' '.join(res.packages)}"
    notes.append(tc + ". You are not root and cannot install system packages: if a tool "
                 "is missing (e.g. `cmake: not found`), stop and tell the user which package "
                 "to add to .momo/box.json.")
    if res.base_variant == "slim":
        notes.append("The image is a slim variant without a C compiler: if a build needs one "
                     "(a native pip or npm package), tell the user to start the box with "
                     "--full-base or to add build-essential to .momo/box.json packages.")
    if memory:
        notes.append(f"Memory is limited to {memory}"
                     + (" (low-memory mode: Gradle runs without a daemon)" if low_mem else "")
                     + ": don't run several builds or test suites at once, and prefer "
                       "running a single test over the whole suite while iterating.")
    hints = "; ".join(res.offline_hints)
    if net == "locked":
        notes.append("Network: none except the model server. "
                     + ("Dependencies were pre-fetched; build offline"
                        + (f" ({hints})" if hints else "") + "." if res.prepare
                        else "Dependencies cannot be downloaded."))
    elif net == "pull":
        rules = res.network
        mine = {h: why for h, why in (rules.allow if rules else {}).items()
                if not detect.denied_by(h, rules.deny)}
        others = [h for h in pull_hosts if h not in mine]
        shown = ", ".join(others[:8]) + (" …" if len(others) > 8 else "")
        what = "the hosts below" if rules and rules.replace_defaults else "package registries"
        notes.append(f"Network: {what} only, through a proxy that is already set up for pip, "
                     f"npm, Maven, Gradle, cargo, go and git" + (f" ({shown})" if shown else "")
                     + ". Other hosts are refused with a 403: if the task needs one, tell "
                     "the user.")
        if mine:
            listed = list(mine.items())[:12]
            notes.append("Hosts this project allows: " + "; ".join(
                _clean(h) + (f" ({_clean(why)})" if why else "") for h, why in listed)
                + (" …" if len(mine) > 12 else "") + ".")
        if rules and rules.deny:
            listed = list(rules.deny.items())[:12]
            notes.append("Refused on purpose by this project: " + "; ".join(
                _clean(h) + (f" ({_clean(why)})" if why else "") for h, why in listed) + ".")
    else:
        notes.append("Network: open (the internet is reachable from run_command).")
    if res.network and res.network.instructions:
        text = "\n".join(_clean(ln) for ln in res.network.instructions.splitlines())
        notes.append(f"Project network instructions (from {detect.NETWORK_JSON}):\n{text}")
    return notes


# ── commands ─────────────────────────────────────────────────────────────────

def _engine(args) -> Engine:
    return Engine(find_runtime(getattr(args, "runtime", None)), getattr(args, "dry_run", False))


def _auto_name(src: Source) -> str:
    stem = _slug(Path(src.label.rstrip("/")).stem if src.kind != "empty" else "empty")[:24]
    return f"{stem}-{secrets.token_hex(2)}"


def _tree_for(src: Source):
    if src.kind == "empty":
        return detect.DirTree(Path(tempfile.mkdtemp(prefix="momo-box-empty-")))
    return detect.GitTree(src.repo, src.sha)


def engine_memory(eng: Engine) -> int | None:
    """The memory the engine's machine has (Docker Desktop's VM, Podman's
    machine, or this host on Linux), in bytes; None when it can't say."""
    if eng.dry_run:
        return None
    fmt = "{{.MemTotal}}" if eng.rt == "docker" else "{{.Host.MemTotal}}"
    r = eng.run("info", "--format", fmt, check=False)
    try:
        return int((r.stdout or b"").decode().strip()) or None
    except ValueError:
        return None


def resolve_low_mem(eng: Engine, flag: bool | None) -> bool:
    """--low-mem / --no-low-mem, else on when the engine has under 4 GB."""
    if flag is not None:
        return flag
    total = engine_memory(eng)
    if total is not None and total < LOW_MEM_BELOW:
        say(f"low-memory mode (the engine has {total / 1024 ** 3:.1f} GB; --no-low-mem turns "
            "it off): a 1g cap, no Gradle daemon, a small code index")
        return True
    return False


def _resolve_toolchain(args, tree) -> detect.Resolved:
    try:
        return detect.resolve(tree, preset=args.preset, base=args.base,
                              packages=(args.packages or "").split() or None,
                              full_base=bool(getattr(args, "full_base", False)))
    except detect.ConfigError as e:
        raise BoxError(str(e)) from None


def _same_source(meta: dict, src: Source) -> bool:
    kind = meta.get("source_kind")
    if kind == "compose" or not kind:
        return True
    if kind != src.kind:
        return False
    if kind == "empty":
        return True
    if kind == "url":
        return meta.get("source") == src.label
    old = Path(meta.get("source") or "")
    r = _git(old, "rev-parse", "--show-toplevel", check=False) if old.is_dir() else None
    top = Path(r.stdout.strip()) if r is not None and r.returncode == 0 else old
    return top.resolve() == Path(src.repo).resolve()


def check_same_source(name: str, src: Source, runtime: str = "docker") -> dict:
    """An existing box (its workspace volume is there) keeps its workspace.
    Starting it with another repo would run the new repo's toolchain and prepare
    (./mvnw …) on the old checkout, and land the old checkout's work in the new
    repo.  Returns the box's record."""
    try:
        meta = load_meta(name)
    except BoxError:
        raise BoxError(
            f"a workspace volume momo-{name}-ws exists, but momo-box has no record of box "
            f"{name} (in {box_dir(name)}), so it can't tell which repo it holds.\n"
            f"  another repo:  pick a new --name\n"
            f"  start over:    {runtime} volume rm momo-{name}-ws momo-{name}-home "
            "(this deletes what is in it)") from None
    if _same_source(meta, src):
        return meta
    old = meta.get("source") or "an empty workspace"
    raise BoxError(
        f"box {name} already exists and its workspace holds {old}, not {src.label}.\n"
        f"  another repo:     pick a new --name (or leave --name out)\n"
        f"  restart this box: momo-box start {name}\n"
        f"  start over:       momo-box sync {name}, then momo-box rm {name}")


def pin_to_saved(src: Source, meta: dict, args) -> None:
    """A kept box's workspace is the commit it was made from, whatever the source
    repo's HEAD (or --ref) says now: detection, the image, the prepare commands and
    the Setup panel all follow that commit, not the host's current checkout."""
    name = meta.get("name") or "?"
    if getattr(args, "with_uncommitted", False) or getattr(args, "all_refs", False):
        say(f"note: box {name} keeps its workspace, so --with-uncommitted and --all-refs "
            "only apply to a new box")
    sha = meta.get("sha")
    if src.kind == "empty" or not sha:
        return          # a box from before momo-box saved this: as the source is now
    if getattr(args, "ref", None) and src.sha != sha:
        raise BoxError(f"box {name} keeps its workspace, made from "
                       f"{meta.get('ref_name') or sha[:12]}; --ref only applies to a new "
                       "box (pick a new --name)")
    if _rev(src.repo, f"{sha}^{{commit}}") is None:
        say(f"note: {sha[:12]}, which box {name} was made from, is gone from "
            f"{src.label}: its toolchain is detected from {src.sha[:12]} instead")
        return
    src.sha, src.ref_name = sha, meta.get("ref_name")


def _stdin_has_data() -> bool:
    try:
        import stat
        mode = os.fstat(sys.stdin.fileno()).st_mode
        return stat.S_ISFIFO(mode) or stat.S_ISREG(mode)
    except (OSError, ValueError, AttributeError):
        return False


def setup_box(args, ui: str) -> tuple[Engine, Box, Source, detect.Resolved, bool]:
    eng = _engine(args)
    if args.name and not _NAME_RE.match(args.name):
        raise BoxError(f"--name: {_NAME_HELP}")
    if not eng.dry_run:     # seconds now rather than a box that can't think, after the build
        provider = args.provider or "ollama"
        note = llm_preflight(args.llm or _DEFAULT_LLM[provider], provider,
                             defaulted=args.llm is None and args.provider is None)
        if note:
            say(f"warning: {note}")
    provisional = args.name or "box"
    src = resolve_source(args.repo, args.ref, args.empty, args.land_repo, provisional)
    name = args.name or _auto_name(src)
    if not args.name and src.kind == "empty" and not args.land_repo:
        src.land = box_dir(name) / "repo"
    fresh = not eng.ok("volume", "inspect", f"momo-{name}-ws")
    if not fresh and not args.name:
        raise BoxError(f"box {name} exists")
    prior: dict = {}
    if not fresh:
        if eng.running(f"momo-{name}"):
            raise BoxError(f"box {name} is running (`momo-box url {name}`); "
                           f"`momo-box stop {name}` first")
        prior = check_same_source(name, src, eng.rt)
        pin_to_saved(src, prior, args)
    tree = _tree_for(src)
    res = _resolve_toolchain(args, tree)
    say(res.summary())
    if args.verbose:
        for why in res.reasons:
            say(f"  {why}")
    for warning in res.warnings + _untravelled(tree):
        say(f"note: {warning}")
    check_approval(res, args.yes)
    project = Path(src.label.rstrip("/")).stem if src.kind != "empty" else "empty"
    image = args.image or ensure_image(eng, res, tree, project, pull=args.pull)
    gated = args.net in ("locked", "pull")
    gw_image = default_image(eng) if gated else image
    presets = detect.load_presets()
    # A pull box's allowlist; for a locked box, what its prefetch may reach.
    allow_lines = build_allowlist(res, args.allow_host or [], presets) if gated else []
    if args.net == "pull":      # hosts `momo-box allow` added to this box earlier
        allow_lines += [ln for ln in prior.get("allow_added") or [] if ln not in allow_lines]
    provider = args.provider or "ollama"
    llm = args.llm or _DEFAULT_LLM[provider]
    base_path = urlsplit(llm).path.rstrip("/")
    inbox = box_dir(name) / "in"
    low_mem = resolve_low_mem(eng, getattr(args, "low_mem", None))
    box = Box(name=name, ui=ui, net=args.net, image=image, gw_image=gw_image,
              runtime=eng.rt, llm=llm, provider=provider, model=args.model,
              port=getattr(args, "port", WEB_PORT_IN),
              token=secrets.token_urlsafe(24) if ui == "web" else None,
              extra=list(args.momo_args), env=dict(res.env), allow_lines=allow_lines,
              prepare=[] if args.no_prepare else list(res.prepare),
              prepare_inputs=list(res.prepare_inputs), inbox=inbox,
              status=status_dir(name),
              ref=src.sha, ref_name=src.ref_name, low_mem=low_mem,
              memory=args.memory or (LOW_MEM_MEMORY if low_mem else None), cpus=args.cpus,
              pids=args.pids, tmp_size=args.tmp_size, prepare_net=args.prepare_net,
              llm_filter=args.llm_filter == "on",
              llm_paths=[base_path + path for path in presets.get("llm_paths", {}).get(provider, [])],
              prior=prior)
    land_label = str(src.land) if src.kind != "url" else f"a mirror of {src.label}"
    box.notes = box_notes(name, args.net, res, allowed_hosts(allow_lines), land_label,
                          memory=box.memory, low_mem=low_mem)
    if ui == "prompt":
        box.prompt_stdin = args.prompt == "-"
        box.interactive = box.prompt_stdin or _stdin_has_data()
    return eng, box, src, res, fresh


def _untravelled(tree) -> list[str]:
    """What a git bundle does not carry into the box."""
    files, out = tree.files(), []
    if ".gitmodules" in files:
        out.append("this repo has git submodules: their folders are empty in the box "
                   "(a bundle carries one repository)")
    if "filter=lfs" in (tree.read(".gitattributes") or "" if ".gitattributes" in files else ""):
        out.append("this repo uses Git LFS: the box gets the pointer files, not the content")
    return out


def _write_inbox(box: Box, src: Source, fresh: bool, args, prompt: str | None):
    _private_dir(box.inbox.parent)
    box.inbox.mkdir(parents=True, exist_ok=True)
    os.chmod(box.inbox, 0o755)
    if box.status is not None:
        boxstatus.StatusWriter(box.status).ensure()
    for stale in ("source.bundle", "dirty.patch", "prompt.txt"):
        (box.inbox / stale).unlink(missing_ok=True)
    if fresh and src.kind != "empty":
        say(f"bundling {src.label} at {src.sha[:12]}")
        write_bundle(src, box.inbox, args.with_uncommitted, name=box.name,
                     all_refs=args.all_refs)
    if prompt is not None and prompt != "-":
        (box.inbox / "prompt.txt").write_text(prompt, encoding="utf-8")
        os.chmod(box.inbox / "prompt.txt", 0o644)


def _start_network(eng: Engine, box: Box, proxy: bool | None = None, serve: bool = True):
    """The box's internal network and its gateway (recreated on every call)."""
    if not box.gated:
        return
    proxy = box.net == "pull" if proxy is None else proxy
    if not eng.ok("network", "inspect", box.network):
        eng.run("network", "create", "--internal", "--label", "momo-box=1", box.network)
    if eng.exists(box.gw):
        eng.run("rm", "-fv", box.gw)
    eng.run(*box.gw_argv(proxy=proxy, serve=serve))
    eng.run(*box.gw_connect_argv())
    if proxy:
        hosts = "\n".join(box.allow_lines) + "\n"
        eng.run("exec", "-i", box.gw, *BOX_PY_S, "-c",
                "import sys; open('/gw/allow.txt','w').write(sys.stdin.read())",
                input=hosts.encode())


def _run_prepare(eng: Engine, box: Box, force: bool = False):
    if not box.prepare:
        return
    where = "registries only" if box.proxied_prepare else "open network"
    say(f"prepare ({where}): {'; '.join(box.prepare)}")
    r = eng.run(*box.prepare_argv(force), check=False, capture=False)
    if r.returncode != 0:
        hint = ""
        if box.proxied_prepare:
            refused = _refused_hosts(eng, box)
            hint = (f" The allowlist refused: {', '.join(refused)}. Allow a host with "
                    "--allow-host HOST, or lift the limit with --prepare-net open."
                    if refused else " (No host was refused by the allowlist.)")
        raise BoxError(f"prepare failed (exit {r.returncode}); fix the command in "
                       f".momo/box.json or rerun with --no-prepare.{hint}")


def _refused_hosts(eng: Engine, box: Box) -> list[str]:
    """Hosts the gateway's proxy turned away so far (from its decision log)."""
    try:
        raw = eng.cp_out(box.gw, "/gw/net.log") or b""
    except BoxError:
        return []
    hosts: list[str] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and not e.get("allowed"):
            host = _clean(e.get("host", ""))[:100]
            if host and host not in hosts:
                hosts.append(host)
    return hosts[:10]


def _fresh_gateway(eng: Engine, box: Box):
    """A new session starts with no gateway log (net.log, in its volume) and no
    network or memory summary from the last one: what the prepare error, netlog and
    the Setup panel report is then this session's."""
    if eng.dry_run:
        return
    if box.gated:
        if eng.exists(box.gw):
            eng.run("rm", "-fv", box.gw, check=False)
        eng.run("volume", "rm", "-f", f"momo-{box.name}-gw", check=False)
    if box.status is not None:
        for stale in ("net.json", "stats.json"):
            (box.status / stale).unlink(missing_ok=True)


def _bring_up(eng: Engine, box: Box):
    """Prefetch the dependencies, then leave the box's network as it runs with.
    A pull or locked box never touches the open internet, not even for the
    prefetch: that goes through the gateway's allowlist proxy, which a locked box
    loses again before momo starts."""
    _fresh_gateway(eng, box)
    if box.proxied_prepare:
        _start_network(eng, box, proxy=True, serve=False)
        _run_prepare(eng, box)
    else:
        _run_prepare(eng, box)
    _start_network(eng, box)


def setup_status(box: Box, src: Source, res: detect.Resolved, inspect=None,
                 internal: bool | None = None, created: float | None = None) -> dict:
    """setup.json: how the box is set up, for its Setup panel.  No token and
    no host paths: the box can read this."""
    _host, _port, alias = box.llm_target() if box.gated else ("", 0, None)
    https = urlsplit(box.llm).scheme == "https"
    filtered = box.gated and box.llm_filter and bool(box.llm_paths) and not alias and not https
    rules = res.network
    source_name = Path(src.label.rstrip("/")).name if src.kind != "empty" else "empty box"
    return {
        "name": box.name, "net": box.net, "ui": box.ui, "image": box.image,
        "created": created or time.time(),
        "source": {"name": boxstatus.clean(source_name, 120), "kind": src.kind,
                   "ref": boxstatus.clean(src.ref_name or (src.sha or "")[:12], 120)},
        "land_branch": f"momo/{box.name}",
        "toolchain": {"source": res.source, "presets": list(res.presets), "base": res.base,
                      "base_variant": res.base_variant,
                      "packages": list(res.packages), "prepare": list(box.prepare),
                      "prepare_inputs": list(box.prepare_inputs),
                      "offline_hints": list(res.offline_hints),
                      "warnings": [boxstatus.clean(w, 300) for w in res.warnings][:20]},
        "llm": {"provider": box.provider, "location": boxstatus.locate(box.llm),
                "filter": filtered, "paths": list(box.llm_paths) if filtered else [],
                "hint": llm_hint(box.llm, box.runtime, box.system)},
        "limits": {"memory": box.memory, "cpus": box.cpus, "pids": box.pids, "tmp": box.tmp_size,
                   "low_mem": box.low_mem},
        "allowlist": {"rules": _marked_rules(box.allow_lines),
                      "replace_defaults": bool(rules and rules.replace_defaults),
                      "instructions": "\n".join(_clean(ln) for ln in
                                                (rules.instructions or "").splitlines())
                      if rules else ""},
        "safeguards": (boxstatus.safeguards(box.net, inspect, internal, llm_filtered=filtered,
                                            llm_paths=box.llm_paths, llm_https=https)
                       if inspect is not None else []),
    }


def _marked_rules(lines: list[str]) -> list[dict]:
    """The allowlist table, with plain allows that a repo deny overrides marked
    (`+` rules, yours, beat a deny: as the proxy decides)."""
    rules = boxstatus.allow_rules(lines)
    deny = {r["rule"]: r["reason"] for r in rules if r["kind"] == "!"}
    for r in rules:
        if r["kind"] == "" and (by := detect.denied_by(r["rule"], deny)):
            r["overridden_by"] = by
    return rules


def _inspect(eng: Engine, *args: str) -> dict | None:
    r = eng.run("inspect", *args, check=False)
    try:
        got = json.loads(r.stdout or b"[]")
    except ValueError:
        return None
    return got[0] if isinstance(got, list) and got and isinstance(got[0], dict) else None


def write_setup_status(eng: Engine, box: Box, src: Source, res: detect.Resolved,
                       inspect: bool = False):
    """setup.json, with the safeguards once the container exists.  Never fails
    the box: the panel is a view."""
    if eng.dry_run or box.status is None:
        return
    try:
        details = internal = None
        if inspect:
            details = _inspect(eng, "--type", "container", box.container) or {}
            if box.gated:
                netinfo = _inspect(eng, "--type", "network", box.network) or {}
                internal = True in (netinfo.get("Internal"), netinfo.get("internal"))  # docker, podman
        setup = setup_status(box, src, res, details, internal)
        setup["engine_memory"] = engine_memory(eng)     # the Setup panel's "of N GB"
        boxstatus.StatusWriter(box.status).write_setup(setup)
    except (OSError, BoxError, ValueError) as e:
        say(f"note: could not write the box's status: {e}")


def _meta_for(box: Box, src: Source, ephemeral: bool, args=None) -> dict:
    meta = {"name": box.name, "ui": box.ui, "net": box.net, "port": box.port,
            "token": box.token, "image": box.image, "runtime": box.runtime,
            "source": src.label, "source_kind": src.kind, "land": str(src.land),
            "ephemeral": ephemeral, "created": time.time(), "landed_export": None,
            "last_line": None, "prepare_net": box.prepare_net, "allow_lines": box.allow_lines,
            "low_mem": box.low_mem, "memory": box.memory,
            # The commit the workspace was made from: a kept box is detected, built
            # and prepared from it later, whatever the source repo's HEAD is then.
            "sha": src.sha, "ref_name": src.ref_name}
    if args is not None:      # what `momo-box start NAME` / `prepare NAME` use again
        keep = {k: v for k, v in vars(args).items()
                if k not in ("func", "cmd", "yes", "pull", "dry_run", "prompt", "name")
                and isinstance(v, (str, int, float, bool, list, type(None)))}
        if src.kind == "local":
            keep["repo"] = src.label
        if keep.get("land_repo"):           # absolute: `start` may run from elsewhere
            keep["land_repo"] = str(src.land)
        meta["args"] = keep
    prior = box.prior
    if prior:                 # a kept box: what it was given before stays
        meta["created"] = prior.get("created") or meta["created"]
        if prior.get("allow_added"):
            meta["allow_added"] = list(prior["allow_added"])
        if box.ui == "prompt" and prior.get("ui") == "web":
            # `run --name X` on a stopped web box: `momo-box start X` still works.
            meta["ui"], meta["args"] = "web", prior.get("args")
    return meta


class _Signals:
    """SIGTERM and SIGHUP become KeyboardInterrupt, so a run that is killed or
    whose terminal closes still lands its work and cleans up."""

    def __enter__(self):
        def raise_(*_):
            raise KeyboardInterrupt
        self._old = {}
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                self._old[sig] = signal.signal(sig, raise_)
            except (ValueError, OSError):       # not the main thread
                pass
        return self

    def __exit__(self, *_):
        for sig, old in self._old.items():
            signal.signal(sig, old)


def cmd_run(args) -> int:
    prompt = args.prompt
    if prompt is None:
        raise BoxError("run needs -p TEXT (or -p - to read stdin); `momo-box web` serves the UI")
    eng, box, src, res, fresh = setup_box(args, "prompt")
    ephemeral = not args.name
    meta = _meta_for(box, src, ephemeral, args=args)
    if not eng.dry_run:
        if fresh:
            forget_landed(src.land, box.name)
        save_meta(meta)
    code, started, landed = 1, False, False
    with _Signals():
        try:
            _write_inbox(box, src, fresh, args, prompt)
            write_setup_status(eng, box, src, res)
            if eng.exists(box.container):
                eng.run("rm", "-fv", box.container)
            _bring_up(eng, box)
            started = True
            try:
                code = eng.run(*box.momo_argv(), check=False, capture=False).returncode
            except KeyboardInterrupt:
                say("interrupted: stopping the box and landing its work")
                eng.run("stop", "-t", "30", box.container, check=False)
                code = 130
            if not eng.dry_run:
                line = sync_once(eng, box.name)
                if line:
                    say(line)
            landed = True
        except BoxError as e:
            if not started:
                raise
            say(f"error: {e}")
            code = code or 1
        finally:
            if not eng.dry_run:
                # Work that did not land is never thrown away: the stopped box stays.
                keep = started and not landed
                if keep:
                    meta = load_meta(box.name)
                    meta["ephemeral"] = False
                    save_meta(meta)
                    say(f"the box's work did not land; it is kept: `momo-box sync {box.name}` "
                        f"retries, `momo-box rm {box.name}` discards it")
                _cleanup(eng, box.name, remove_volumes=ephemeral and not keep,
                         keep_meta=keep or not ephemeral, keep_container=keep)
    return code


def cmd_web(args) -> int:
    eng, box, src, res, fresh = setup_box(args, "web")
    if eng.exists(box.container):
        if eng.running(box.container):
            raise BoxError(f"box {box.name} is already running: momo-box url {box.name}")
        eng.run("rm", "-fv", box.container)
    meta = _meta_for(box, src, ephemeral=False, args=args)
    if not eng.dry_run:
        if fresh:
            forget_landed(src.land, box.name)
        save_meta(meta)
    try:
        _write_inbox(box, src, fresh, args, None)
        write_setup_status(eng, box, src, res)
        _bring_up(eng, box)
        eng.run(*box.momo_argv(), env=box.run_env())
        write_setup_status(eng, box, src, res, inspect=True)
        ready = eng.dry_run or _wait_ready(eng, box)
    except (BoxError, KeyboardInterrupt):
        if not eng.dry_run:     # a box that never started: leave nothing behind
            _cleanup(eng, box.name, remove_volumes=fresh, keep_meta=not fresh)
        raise
    if eng.dry_run:
        return 0
    start_syncer(box.name)
    url = f"http://127.0.0.1:{box.port}/?token={box.token}"
    say(f"box {box.name} ({box.net} network) is {'up' if ready else 'still starting'}"
        + ("" if ready else f" (`momo-box logs {box.name}` shows why)"))
    print(url, flush=True)
    say(f"work lands as branch momo/{box.name} in {src.land} after every turn; "
        f"`momo-box stop {box.name}` stops it")
    return 0


def _wait_ready(eng: Engine, box: Box, timeout: float = 30.0) -> bool:
    """True once the web UI answers; False when it is still starting after
    `timeout` (a big clone).  A box whose momo exited is an error, with its log."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not eng.running(box.container):
            r = eng.run("logs", "--tail", "20", box.container, check=False)
            tail = _clean_lines((r.stdout or b"") + (r.stderr or b""))
            raise BoxError(f"momo did not start in box {box.name}:\n{tail}")
        try:
            with opener.open(f"http://127.0.0.1:{box.port}/", timeout=2):
                return True
        except urllib.error.HTTPError:
            return True                 # it answered (401 without the token)
        except (OSError, ValueError):
            time.sleep(0.5)
    return False


def _clean_lines(raw: bytes) -> str:
    return "\n".join(_clean(ln) for ln in raw.decode("utf-8", "replace").splitlines())


def cmd_start(args) -> int:
    """Start a stopped web box again with the settings it was created with."""
    meta = load_meta(args.name)
    saved = meta.get("args")
    if meta.get("ui") != "web" or not isinstance(saved, dict):
        raise BoxError(f"{args.name} was not started with `momo-box web`; run it with "
                       f"`momo-box run --name {args.name} -p …`")
    ns = build_parser().parse_args(["web"])
    for k, v in saved.items():
        setattr(ns, k, v)
    ns.name, ns.runtime, ns.yes = args.name, args.runtime or meta.get("runtime"), args.yes
    ns.momo_args = list(saved.get("momo_args") or [])
    # The workspace is kept: these only shaped the first bundle.  The saved commit
    # (meta["sha"]) stands in for --ref; an older record without it keeps its --ref.
    ns.with_uncommitted = ns.all_refs = False
    if meta.get("sha"):
        ns.ref = None
    return cmd_web(ns)


# ── sync ─────────────────────────────────────────────────────────────────────

def _landed_commit(repo: Path, name: str) -> str | None:
    """What the land repo got from this box last, while it still has it: the box
    then only needs to send what is new."""
    if not ((repo / ".git").exists() or (repo / "HEAD").is_file()):
        return None
    return _rev(repo, TRACK_REF.format(name) + "^{commit}")


def _export(eng: Engine, container: str, snapshot: bool, since: str | None) -> dict | None:
    """Have the box snapshot (when it runs) and read its export record."""
    if snapshot:
        r = eng.run("exec", container, *BOX_PY_S, ENTRY, "export",
                    *(["--since", since] if since else []), check=False)
        if r.returncode != 0:
            raise BoxError(f"export failed: {_clean((r.stderr or b'').decode('utf-8', 'replace').strip())}")
    raw = eng.cp_out(container, LATEST_JSON)
    if raw is None:
        return None
    info = box_info(raw)
    if info is None or info["export"] is None:
        raise BoxError("the box's export record is unreadable")
    return info


def sync_once(eng: Engine, name: str, fresh_snapshot: bool | None = None,
              out: dict | None = None) -> str | None:
    """Snapshot (when the box is running) and land it; the line to show or None.
    `out` gets what the Setup panel shows: the turn, and the land if any."""
    out = {} if out is None else out
    meta = load_meta(name)
    container = meta.get("container") or f"momo-{name}"
    repo = Path(meta["land"])
    running = eng.running(container) if fresh_snapshot is None else fresh_snapshot
    since = _landed_commit(repo, name) if running else None
    info = _export(eng, container, running, since)
    if info is not None:
        out["turn"] = info.get("turn")
    if info is None or info["export"] == meta.get("landed_export"):
        return None
    if not info["bundle"]:
        meta["landed_export"] = info["export"]
        save_meta(meta)
        return None
    bundle = _private_dir(box_dir(name)) / "latest.bundle"

    def fetch() -> str:
        if not eng.cp_out(container, LATEST_BUNDLE, dest=bundle, limit=bundle_max()):
            raise BoxError("the box's bundle could not be copied out")
        return land(repo, bundle, name, info, out)
    try:
        line = fetch()
    except BoxError:
        if not (running and info["since"]):
            raise
        info = _export(eng, container, True, None)      # the short bundle did not fit:
        if info is None or not info["bundle"]:          # take all of the box's work
            raise
        line = fetch()
    meta["landed_export"] = info["export"]
    meta["last_line"] = line
    meta["last_sync"] = time.time()
    save_meta(meta)
    return line


def follow(eng: Engine, name: str, interval: float = 2.0) -> int:
    """Sync after every finished turn until the box stops, then once more.
    The box tells us when a turn ends (`entrypoint.py watch` over one exec), so
    nothing is polled; an image that predates `watch` falls back to polling."""
    meta = load_meta(name)
    container = meta.get("container") or f"momo-{name}"
    state = {"stop": False, "proc": None, "net": None}
    writer = status_writer(name)

    def on_term(*_):
        state["stop"] = True
        for key in ("proc", "net"):
            if state[key] is not None:
                state[key].terminate()
    signal.signal(signal.SIGTERM, on_term)

    def beat():
        try:
            writer.beat()
        except OSError:
            pass

    def sync(fresh: bool):
        out: dict = {}
        try:
            line = sync_once(eng, name, fresh_snapshot=fresh, out=out)
            if line:
                say(line)
            _status(writer.synced, out, line)
        except (BoxError, ValueError, TypeError, OSError) as e:    # never die on one bad sync
            say(str(e))
            _status(writer.failed, str(e))

    _status(writer.beat, True)
    if meta.get("net") in ("pull", "locked") and not meta.get("container"):
        threading.Thread(target=_follow_net, args=(eng, name, state, writer), daemon=True).start()
        threading.Thread(target=_follow_stats, args=(eng, name, state, writer), daemon=True).start()
    while not state["stop"] and eng.running(container):
        began = time.monotonic()
        proc = state["proc"] = subprocess.Popen(
            eng.cmd("exec", container, *BOX_PY_S, ENTRY, "watch"), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        got = False
        for line in proc.stdout:
            got = True
            if state["stop"]:
                break
            if line.strip():                      # blank lines are heartbeats
                sync(True)
            else:
                beat()
        proc.stdout.close()
        code = proc.wait()
        state["proc"] = None
        if code == 2 and not got and time.monotonic() - began < 5:
            _follow_poll(eng, name, container, state, sync, interval, beat)
            break
        if not state["stop"]:
            time.sleep(interval)
    # The entrypoint snapshots when momo exits; give it the stop timeout.
    for _ in range(40):
        if not eng.running(container):
            break
        time.sleep(1)
    sync(False)
    return 0


def _status(fn, *args):
    """Write the Setup panel's status; a full disk must not stop the syncer."""
    try:
        fn(*args)
    except OSError as e:
        say(f"note: could not write the box's status: {e}")


def _follow_net(eng: Engine, name: str, state: dict, writer: boxstatus.StatusWriter):
    """net.json from the gateway's net.log: one `exec … nettail` that prints
    the log and then each new decision; written at most once a second."""
    gw, agg = f"momo-{name}-gw", boxstatus.NetAggregator()
    delay = 1.0
    while not state["stop"] and eng.running(f"momo-{name}"):
        agg.reset()
        last = 0.0
        began = time.monotonic()
        proc = state["net"] = subprocess.Popen(
            eng.cmd("exec", gw, *BOX_PY_S, ENTRY, "gw", "nettail"), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, errors="replace")
        for line in proc.stdout:
            if state["stop"]:
                break
            if line.strip():
                agg.feed(line[:20000])
            now = time.monotonic()
            if agg.changed and now - last >= 1.0:
                _status(writer.write_net, agg.snapshot())
                last = now
        proc.stdout.close()
        proc.wait()
        state["net"] = None
        if agg.changed:
            _status(writer.write_net, agg.snapshot())
        # the gateway restarted (start, allow) or has no nettail (an older image)
        delay = 1.0 if time.monotonic() - began > 30 else min(delay * 2, 60.0)
        for _ in range(int(delay * 4)):
            if state["stop"]:
                return
            time.sleep(0.25)


STATS_EVERY = 15.0       # seconds between two samples of the gateway's memory


def container_memory(eng: Engine, names: list[str]) -> dict[str, dict]:
    """{name: {current, max}} from one `docker stats --no-stream` (about a second)."""
    if eng.dry_run or not names:
        return {}
    r = eng.run("stats", "--no-stream", "--format", "{{json .}}", *names, check=False)
    return boxstatus.docker_stats((r.stdout or b"").decode("utf-8", "replace"))


def _follow_stats(eng: Engine, name: str, state: dict, writer: boxstatus.StatusWriter,
                  every: float = STATS_EVERY):
    """stats.json: the gateway's memory, which the box can't see itself."""
    gw = f"momo-{name}-gw"
    while not state["stop"] and eng.running(f"momo-{name}"):
        got = container_memory(eng, [gw]).get(gw)
        if got is not None:
            _status(writer.write_stats, got)
        for _ in range(int(every * 4)):
            if state["stop"]:
                return
            time.sleep(0.25)


def _follow_poll(eng: Engine, name: str, container: str, state: dict, sync, interval: float,
                 beat=lambda: None):
    """follow() for a box whose image has no `watch`: poll the turn marker."""
    last_seq = None
    while not state["stop"] and eng.running(container):
        beat()
        try:
            raw = eng.cp_out(container, TURN_JSON)
            seq = json.loads(raw).get("seq") if raw else None
        except (BoxError, ValueError, AttributeError):
            seq = None
        if seq != last_seq and seq is not None:
            last_seq = seq
            sync(True)
        time.sleep(interval)


def start_syncer(name: str):
    d = _private_dir(box_dir(name))
    log = open(d / "sync.log", "a")
    p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "sync", name, "--follow"],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    (d / "sync.pid").write_text(str(p.pid))


def _syncer_pid(name: str) -> int | None:
    try:
        pid = int((box_dir(name) / "sync.pid").read_text())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    # After a reboot or logout the pid can belong to another process by now.
    return pid if _is_syncer(pid, name) else None


def _is_syncer(pid: int, name: str) -> bool:
    """Whether `pid` runs `momo_box.py sync NAME --follow` (start_syncer's argv).
    When this machine can't say (no /proc, no ps), it is taken to be."""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").split("\0")
    except OSError:
        try:
            r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return True
        if r.returncode != 0:
            return True
        argv = r.stdout.split()
    return ("sync" in argv and name in argv and "--follow" in argv
            and any(a.endswith("momo_box.py") for a in argv))


def cmd_sync(args) -> int:
    eng = _engine(args)
    if args.container or args.land_repo:
        # A container momo-box did not start (compose): remember where it lands.
        if not (args.container and args.land_repo):
            raise BoxError("--container and --land-repo go together")
        if not _NAME_RE.match(args.name):
            raise BoxError(f"NAME: {_NAME_HELP}")
        try:
            meta = load_meta(args.name)
        except BoxError:
            meta = {"name": args.name, "ui": "web", "net": "?", "ephemeral": False,
                    "source": "compose", "source_kind": "compose", "landed_export": None,
                    "created": time.time()}
        meta.update(container=args.container,
                    land=str(Path(args.land_repo).expanduser().resolve()))
        save_meta(meta)
    if args.follow:
        return follow(eng, args.name)
    out: dict = {}
    writer = status_writer(args.name)
    record = not eng.dry_run and writer.folder.is_dir()
    try:
        line = sync_once(eng, args.name, out=out)
    except BoxError as e:
        if record:          # a one-off sync is not the syncer's heartbeat
            _status(writer.failed, str(e), False)
        raise
    if record:
        _status(writer.synced, out, line, False)
    say(line or "nothing new to land")
    return 0


# ── lifecycle ────────────────────────────────────────────────────────────────

def forget_landed(land: Path | str | None, name: str):
    """Drop the tracking ref of a box that is gone (rm, or a new box of the same
    name).  Its momo/<name> branch stays: that is its work.  Without the ref, a new
    box of that name lands next to it on momo/<name>-incoming instead of moving it."""
    if not land:
        return
    repo = Path(land)
    if (repo / ".git").exists() or (repo / "HEAD").is_file():
        _git(repo, "update-ref", "-d", TRACK_REF.format(name), check=False)


def _cleanup(eng: Engine, name: str, remove_volumes: bool, keep_meta: bool = True,
             keep_container: bool = False):
    for c in ([] if keep_container else [f"momo-{name}"]) + [f"momo-{name}-gw"]:
        eng.run("rm", "-fv", c, check=False)
    eng.run("network", "rm", f"momo-{name}-net", check=False)
    eng.run("volume", "rm", "-f", f"momo-{name}-gw", check=False)
    if remove_volumes and not keep_container:
        for v in ("ws", "home"):
            eng.run("volume", "rm", "-f", f"momo-{name}-{v}", check=False)
    shutil.rmtree(box_dir(name) / "in", ignore_errors=True)
    if not keep_meta:
        d = box_dir(name)
        meta = {}
        try:
            meta = json.loads((d / "box.json").read_text())
        except (OSError, ValueError):
            pass
        if not eng.dry_run:
            forget_landed(meta.get("land"), name)
        # An --empty box's landing repo lives here: keep it if it holds work.
        repo = d / "repo"
        if repo.exists() and meta.get("land") == str(repo):
            for f in d.iterdir():
                if f.name != "repo":
                    shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink()
            say(f"the box's work stays in {repo}")
        else:
            shutil.rmtree(d, ignore_errors=True)


def cmd_stop(args) -> int:
    eng = _engine(args)
    load_meta(args.name)
    eng.run("stop", "-t", "30", f"momo-{args.name}", check=False)
    pid = _syncer_pid(args.name)
    if pid:
        for _ in range(60):          # it lands the final snapshot, then exits
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.5)
    else:
        if (line := sync_once(eng, args.name, fresh_snapshot=False)):
            say(line)
    eng.run("stop", "-t", "5", f"momo-{args.name}-gw", check=False)
    meta = load_meta(args.name)
    if meta.get("last_line"):
        say(f"last sync: {meta['last_line']}")
    return 0


def cmd_rm(args) -> int:
    eng = _engine(args)
    meta = load_meta(args.name)
    if not args.yes:
        if not sys.stdin.isatty():
            raise BoxError("rm deletes the box's workspace and sessions: pass --yes")
        a = input(f"Remove box {args.name} (workspace and sessions; its work is on "
                  f"momo/{args.name} in {meta.get('land')})? [y/N] ").strip().lower()
        if a not in ("y", "yes"):
            return 1
    if eng.running(f"momo-{args.name}"):
        cmd_stop(args)
    _cleanup(eng, args.name, remove_volumes=True, keep_meta=False)
    say(f"removed {args.name}" + (f"; its work stays on momo/{args.name} in {meta['land']}"
                                  if meta.get("land") else ""))
    return 0


def cmd_ls(args) -> int:
    eng = _engine(args)
    root = cache_dir() / "boxes"
    rows = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            m = json.loads((d / "box.json").read_text())
            name = m["name"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        running = eng.running(m.get("container") or f"momo-{name}")
        sync = "-"
        if running and m.get("ui") == "web":
            sync = "on"
            if _syncer_pid(name) is None and not eng.dry_run:
                start_syncer(name)        # it died (a logout, a reboot): turns land again
                sync = "restarted"
        rows.append((name, m.get("ui", ""), m.get("net", ""),
                     "running" if running else "stopped", sync, m.get("last_line") or "-"))
    if not rows:
        print("no boxes")
        return 0
    w = max(4, *(len(r[0]) for r in rows))
    print(f"{'NAME':<{w}}  {'UI':<6} {'NET':<6} {'STATE':<8} {'SYNC':<9} LAST SYNC")
    for r in rows:
        print(f"{r[0]:<{w}}  {r[1]:<6} {r[2]:<6} {r[3]:<8} {r[4]:<9} {_clean(r[5])}")
    return 0


# ── dash ─────────────────────────────────────────────────────────────────────
# A page on this machine with every box: the same Setup view as inside a box
# (harness/web/static/setup-render.js), from the status folders momo-box
# writes.  Read-only; the page needs the token printed with its address.

_DASH_STATIC = REPO_ROOT / "harness" / "web" / "static"
_DASH_FILES = {"/": ("dash.html", "text/html; charset=utf-8"),
               "/dash.js": ("dash.js", "text/javascript; charset=utf-8"),
               "/setup-render.js": ("setup-render.js", "text/javascript; charset=utf-8"),
               "/app.css": ("app.css", "text/css; charset=utf-8"),
               "/favicon.svg": ("favicon.svg", "image/svg+xml")}


def _running_boxes(eng: Engine) -> set[str]:
    r = eng.run("ps", "--filter", "label=momo-box=1", "--format", "{{.Names}}", check=False)
    return set((r.stdout or b"").decode("utf-8", "replace").split())


def dash_rows(eng: Engine, restart: bool = True) -> list[dict]:
    """Every box with its status files, shaped as the Setup view."""
    root = cache_dir() / "boxes"
    running = _running_boxes(eng)
    mem = _DASH_MEM.get(lambda: container_memory(eng, sorted(running)))
    rows = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            m = json.loads((d / "box.json").read_text())
            name = m["name"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        up = (m.get("container") or f"momo-{name}") in running
        syncer = _syncer_pid(name) is not None
        if up and m.get("ui") == "web" and not syncer and restart and not eng.dry_run:
            start_syncer(name)          # as `ls` does: turns land again
            syncer = True
        w = status_writer(name)
        setup, sync, net = w.read("setup.json"), w.read("sync.json"), w.read("net.json")
        llm = setup.get("llm") if isinstance(setup.get("llm"), dict) else {}
        tc = setup.get("toolchain") if isinstance(setup.get("toolchain"), dict) else {}
        state = boxstatus.sync_state(sync) if up else ("unknown" if not sync else "ok")
        view = {
            "kind": "box", "turn": sync.get("turn"),
            "box": {"available": bool(setup), "folder": w.folder.is_dir(), "setup": setup,
                    "sync": sync, "net": net, "sync_state": state,
                    "recent_denied": boxstatus.recent_denied(net),
                    "prepare": {"commands": tc.get("prepare") or [],
                                "inputs": tc.get("prepare_inputs") or [], "ran": None,
                                "stale": None, "unknown": True}},
            "model": {"provider": llm.get("provider") or "", "location": llm.get("location"),
                      "url": "", "reachable": None, "latency": {}, "context": {},
                      "perf": {"rows": []}, "load": None},
            "safeguards": setup.get("safeguards") or [],
        }
        container = m.get("container") or f"momo-{name}"
        box_mem = mem.get(container) if up else None
        view["memory"] = {"kind": "box", "available": box_mem is not None,
                          **(box_mem or {"current": None, "max": None}),
                          "gateway": mem.get(f"momo-{name}-gw") if up else None,
                          "engine_total": setup.get("engine_memory"), "processes": [],
                          "history": [], "peak": None, "oom_kills": 0}
        port, token = m.get("port"), m.get("token")
        rows.append({"name": name, "ui": m.get("ui"), "net": m.get("net"),
                     "state": "running" if up else "stopped", "syncer": syncer,
                     "url": f"http://127.0.0.1:{port}/?token={token}"
                     if m.get("ui") == "web" and up and token else None,
                     "land": m.get("land"), "last_line": _clean(m.get("last_line") or ""),
                     "created": m.get("created"), "view": view})
    return rows


class _TTL:
    """A value recomputed at most every `ttl` seconds (the dash polls every 3 s)."""

    def __init__(self, ttl: float):
        self.ttl, self.ts, self.value = ttl, 0.0, None
        self.lock = threading.Lock()

    def get(self, compute):
        with self.lock:
            if self.ts and time.monotonic() - self.ts < self.ttl:
                return self.value
            self.value, self.ts = compute(), time.monotonic()
            return self.value


_DASH_MEM = _TTL(5.0)


def dash_totals(eng: Engine, rows: list[dict]) -> dict:
    """The dash header: the engine's memory and what the boxes use of it."""
    used = 0
    for r in rows:
        m = r["view"].get("memory") or {}
        used += (m.get("current") or 0) + ((m.get("gateway") or {}).get("current") or 0)
    return {"engine_total": _DASH_ENGINE.get(lambda: engine_memory(eng)), "boxes_used": used}


_DASH_ENGINE = _TTL(300.0)


def serve_dash(eng: Engine, port: int, token: str):
    """The dashboard's HTTP server (not started); ThreadingHTTPServer on 127.0.0.1."""
    import hmac
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self'; img-src 'self' data:; "
                             "connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            host = (self.headers.get("Host") or "").lower()
            if host not in (f"127.0.0.1:{self.server.server_address[1]}",
                            f"localhost:{self.server.server_address[1]}"):
                self._send(403, b"wrong host", "text/plain")     # DNS rebinding
                return
            path = urlsplit(self.path).path
            if path == "/api/boxes":
                given = self.headers.get("X-Momo-Token") or ""
                if not hmac.compare_digest(given.encode(), token.encode()):
                    self._send(401, b'{"error": "token"}', "application/json")
                    return
                rows = dash_rows(eng)
                body = json.dumps({"boxes": rows, "now": time.time(),
                                   "memory": dash_totals(eng, rows)}).encode()
                self._send(200, body, "application/json")
                return
            if path in _DASH_FILES:
                fname, ctype = _DASH_FILES[path]
                try:
                    self._send(200, (_DASH_STATIC / fname).read_bytes(), ctype)
                except OSError:
                    self._send(404, b"missing", "text/plain")
                return
            self._send(404, b"not found", "text/plain")

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def cmd_dash(args) -> int:
    eng = _engine(args)
    token = secrets.token_urlsafe(24)
    try:
        server = serve_dash(eng, args.port, token)
    except OSError as e:
        raise BoxError(f"dash: cannot listen on 127.0.0.1:{args.port}: {e}") from None
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    say("dashboard for every box (read-only); Ctrl+C stops it")
    print(url, flush=True)
    if not args.no_open:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_url(args) -> int:
    meta = load_meta(args.name)
    if meta.get("ui") != "web":
        raise BoxError(f"{args.name} is not a web box")
    print(f"http://127.0.0.1:{meta['port']}/?token={meta['token']}")
    return 0


def cmd_logs(args) -> int:
    eng = _engine(args)
    load_meta(args.name)
    return eng.run("logs", *(["-f"] if args.follow else []), f"momo-{args.name}",
                   check=False, capture=False).returncode


def cmd_allow(args) -> int:
    eng = _engine(args)
    meta = load_meta(args.name)
    if meta.get("net") != "pull":
        raise BoxError(f"{args.name} is a {meta.get('net')} box; allow only applies to pull boxes")
    presets = detect.load_presets()
    try:
        hosts = [detect.parse_host_rule(h, "allow")[0]
                 for h in detect.expand_hosts(args.hosts, presets)]
    except detect.ConfigError as e:
        raise BoxError(str(e)) from None
    # `+`: your own allow, which also beats a deny in the repo's .momo/network.json
    lines = [f"+{h}  # momo-box allow" for h in hosts]
    # Saved with the box: `start` and `prepare` rebuild the allowlist and keep these.
    if not eng.dry_run:
        meta["allow_added"] = (meta.get("allow_added") or []) + [
            ln for ln in lines if ln not in (meta.get("allow_added") or [])]
        meta["allow_lines"] = (meta.get("allow_lines") or []) + [
            ln for ln in lines if ln not in (meta.get("allow_lines") or [])]
        save_meta(meta)
    if not eng.running(f"momo-{args.name}-gw"):
        say(f"allowed {', '.join(hosts)} (the box is stopped: from its next start)")
        return 0
    eng.run("exec", "-i", f"momo-{args.name}-gw", *BOX_PY_S, "-c",
            "import sys; open('/gw/allow.txt','a').write(sys.stdin.read())",
            input=("".join(ln + "\n" for ln in lines)).encode())
    say(f"allowed {', '.join(hosts)} (takes effect at once, and after a restart)")
    writer = status_writer(args.name)
    setup = writer.read("setup.json")
    if setup and not eng.dry_run:       # the Setup panel's allowlist table
        rules = setup.setdefault("allowlist", {}).setdefault("rules", [])
        rules[:0] = boxstatus.allow_rules([f"+{h}  # momo-box allow" for h in hosts])
        setup.pop("schema", None), setup.pop("updated", None)
        writer.write_setup(setup)
    return 0


def cmd_netlog(args) -> int:
    eng = _engine(args)
    raw = eng.cp_out(f"momo-{args.name}-gw", "/gw/net.log")
    if raw is None:
        say("no network log (not a pull box, or nothing requested yet)")
        return 0
    lines = raw.decode("utf-8", "replace").splitlines()[-args.lines:]
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        stamp = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)
                                                        if isinstance(e.get("ts"), (int, float)) else 0))
        verdict = "allow" if e.get("allowed") else "DENY "
        if e.get("kind") == "llm":
            tail = f"  HTTP {e['status']}" if isinstance(e.get("status"), int) else ""
            if e.get("error"):
                tail = f"  ({_clean(str(e['error']))[:120]})"
            print(f"{stamp} {verdict} model {_clean(str(e.get('method', '')))[:10]} "
                  f"{_clean(str(e.get('path', '')))[:120]}{tail}")
            continue
        why = ""
        if e.get("rule"):
            why = f"  ({_clean(str(e['rule']))[:80]}" + (
                f": {_clean(str(e['reason']))[:120]}" if e.get("reason") else "") + ")"
        print(f"{stamp} {verdict} {_clean(str(e.get('method', '')))[:10]} "
              f"{_clean(str(e.get('host')))[:120]}:{_clean(str(e.get('port')))[:6]}{why}")
    return 0


def cmd_prepare(args) -> int:
    eng = _engine(args)
    meta = load_meta(args.name)
    if eng.running(f"momo-{args.name}"):
        raise BoxError("stop the box first: prepare and momo share the workspace")
    # Re-resolve to know the commands (they are not stored): from the commit the box
    # was made from, with the toolchain flags it was made with, unless given here.
    saved = meta.get("args") if isinstance(meta.get("args"), dict) else {}
    for flag in ("preset", "base", "packages"):
        if getattr(args, flag, None) is None:
            setattr(args, flag, saved.get(flag))
    if not getattr(args, "full_base", False):
        args.full_base = bool(saved.get("full_base"))
    src = resolve_source(meta["source"] if meta["source_kind"] != "empty" else None,
                         None if meta.get("sha") else saved.get("ref"),
                         meta["source_kind"] == "empty", meta.get("land"), args.name)
    pin_to_saved(src, meta, argparse.Namespace(ref=None))
    res = _resolve_toolchain(args, _tree_for(src))
    check_approval(res, args.yes)
    net = meta.get("net") if meta.get("net") in ("open", "pull", "locked") else "open"
    gated = net in ("pull", "locked")
    if not eng.dry_run and not eng.ok("image", "inspect", meta["image"]):
        raise BoxError(f"box {args.name}'s image {meta['image']} is gone (a newer build of the "
                       f"project replaced it); `momo-box start {args.name}` rebuilds it")
    box = Box(name=args.name, ui="prompt", net=net, image=meta["image"],
              gw_image=default_image(eng) if gated else "",
              runtime=eng.rt, llm="http://localhost:1", provider="ollama",
              prepare=res.prepare, prepare_inputs=res.prepare_inputs, env=res.env,
              allow_lines=list(meta.get("allow_lines") or meta.get("pull_hosts") or []),
              prepare_net=args.prepare_net or meta.get("prepare_net") or "pull",
              low_mem=bool(meta.get("low_mem")) if args.low_mem is None else args.low_mem,
              memory=meta.get("memory"))
    if box.low_mem and not box.memory:
        box.memory = LOW_MEM_MEMORY
    if not box.prepare:
        say("no prepare commands for this box")
        return 0
    try:
        _fresh_gateway(eng, box)
        if box.proxied_prepare:
            _start_network(eng, box, proxy=True, serve=False)
        _run_prepare(eng, box, force=True)
    finally:
        if box.proxied_prepare and not eng.dry_run:    # the box is stopped: no gateway stays
            eng.run("rm", "-fv", box.gw, check=False)
            eng.run("network", "rm", box.network, check=False)
    return 0


def cmd_detect(args) -> int:
    src_path = Path(args.repo or ".").expanduser().resolve()
    tree = detect.tree_for(src_path) if not is_url(args.repo or "") else \
        detect.GitTree(mirror_for(args.repo))
    res = _resolve_toolchain(args, tree)
    print(res.summary())
    for why in res.reasons:
        print(f"  {why}")
    if res.prepare:
        print(f"  prepare: {'; '.join(res.prepare)}")
    if res.pull_hosts:
        print(f"  extra pull hosts: {', '.join(res.pull_hosts)}")
    for warning in res.warnings:
        print(f"  note: {warning}")
    print("network (a pull box's allowlist; a locked box's prefetch):")
    for line in describe_allowlist(build_allowlist(res, getattr(args, "allow_host", None) or [],
                                                   detect.load_presets())):
        print(f"  {line}")
    if res.network and res.network.instructions:
        print(f"  instructions for the model ({detect.NETWORK_JSON}):")
        for ln in res.network.instructions.splitlines():
            print(f"    {ln}")
    if args.write:
        target = src_path / detect.BOX_JSON
        if target.exists() and not args.force:
            raise BoxError(f"{target} exists (--force overwrites it)")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(res.box_json(), indent=2) + "\n")
        print(f"wrote {target} — commit it to pin this toolchain")
    return 0


def cmd_build(args) -> int:
    eng = _engine(args)
    if not args.project:
        res = detect.Resolved(source="default", base=detect.load_presets()["default_base"])
        tag = ensure_image(eng, res, None, "momo", rebuild=True, pull=args.pull)
        eng.run("tag", tag, f"{IMAGE_REPO}:latest", check=False)
        say(f"built {tag} (also {IMAGE_REPO}:latest)")
        return 0
    src = resolve_source(args.repo, args.ref, False, None, "build")
    tree = _tree_for(src)
    res = _resolve_toolchain(args, tree)
    say(res.summary())
    check_approval(res, args.yes)
    tag = ensure_image(eng, res, tree, Path(src.label.rstrip("/")).stem, rebuild=True,
                       pull=args.pull)
    say(f"built {tag}")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def _common(p: argparse.ArgumentParser):
    p.add_argument("--runtime", choices=("docker", "podman"), default=None,
                   help="Container engine (default: docker, else podman; or $MOMO_BOX_RUNTIME)")
    p.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)


def _toolchain_flags(p: argparse.ArgumentParser):
    p.add_argument("--preset", default=None, help="java, gradle, cpp, node, rust, go or python")
    p.add_argument("--base", default=None, metavar="IMAGE", help="Base image for the toolchain")
    p.add_argument("--packages", default=None, metavar="'PKG …'",
                   help="Extra system packages to install in the image")
    p.add_argument("--yes", action="store_true",
                   help="Approve a repo's box configuration without asking")
    p.add_argument("--full-base", action="store_true",
                   help="Use a preset's full image (with a C compiler) instead of its slim one")


def _box_flags(p: argparse.ArgumentParser):
    _common(p)
    _toolchain_flags(p)
    p.add_argument("--repo", default=None, metavar="PATH|URL",
                   help="The workspace: a local git repo (default: .) or a git URL")
    p.add_argument("--ref", default=None, help="Commit, branch or tag to check out (default HEAD)")
    p.add_argument("--empty", action="store_true", help="Start from an empty repository")
    p.add_argument("--with-uncommitted", action="store_true",
                   help="Also bring the local repo's uncommitted changes (as their own commit)")
    p.add_argument("--land-repo", default=None, metavar="PATH",
                   help="Where branch momo/NAME is created (default: the source repo)")
    p.add_argument("--name", default=None, help="Keep the box under this name (reusable)")
    p.add_argument("--net", choices=("pull", "locked", "open"), default="pull",
                   help="pull (default): package registries only; locked: only the model "
                        "server; open: the internet, this machine and its network")
    p.add_argument("--prepare-net", choices=("pull", "open"), default="pull",
                   help="Where a pull or locked box prefetches dependencies: through the "
                        "registry allowlist (default) or on the open network")
    p.add_argument("--llm-filter", choices=("on", "off"), default="on",
                   help="pull/locked: forward only inference requests to an http model "
                        "server (default on); off forwards its whole API")
    p.add_argument("--all-refs", action="store_true",
                   help="Bring every branch and tag into the box, not just the one it starts from")
    p.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                   help="Extra host for --net pull (repeatable; 'github' adds GitHub's)")
    p.add_argument("--llm", default=None, metavar="URL",
                   help="Inference server as seen from this machine (default by provider)")
    p.add_argument("--provider", choices=("ollama", "llamacpp"), default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--image", default=None,
                   help="Use this local image instead of building one (pull it yourself "
                        "first: momo-box never pulls the image it runs)")
    p.add_argument("--pull", action="store_true", help="Refresh the base image")
    p.add_argument("--no-prepare", action="store_true", help="Skip the dependency prefetch")
    p.add_argument("--memory", default=None, help="Memory limit, e.g. 8g (low-memory mode: 1g)")
    _low_mem_flag(p)
    p.add_argument("--cpus", default=None, help="CPU limit, e.g. 4")
    p.add_argument("--pids", type=int, default=512, help="Process/thread limit (default 512)")
    p.add_argument("--tmp-size", default="2g", help="Size of the box's /tmp (default 2g)")
    p.add_argument("-v", "--verbose", action="store_true", help="Show why the toolchain was chosen")


def _low_mem_flag(p: argparse.ArgumentParser):
    p.add_argument("--low-mem", action=argparse.BooleanOptionalAction, default=None,
                   help="A 1g memory cap, no Gradle/Kotlin daemon, a small code index "
                        "(default: on when the engine has under 4 GB)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="momo-box", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="Run one prompt headless in a box")
    _box_flags(p)
    p.add_argument("-p", "--prompt", default=None, metavar="TEXT", help="The prompt ('-': stdin)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("web", help="Serve the web UI from a box")
    _box_flags(p)
    p.add_argument("--port", type=int, default=WEB_PORT_IN, help="Host port (127.0.0.1)")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("sync", help="Land the box's work as branch momo/NAME now")
    _common(p)
    p.add_argument("name")
    p.add_argument("--follow", action="store_true", help="Keep syncing after every turn")
    p.add_argument("--container", default=None,
                   help="For a container momo-box did not start (compose): its name")
    p.add_argument("--land-repo", default=None, metavar="PATH",
                   help="With --container: the repo that gets branch momo/NAME")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("detect", help="Show (or --write) the toolchain for a repo")
    _toolchain_flags(p)
    p.add_argument("--repo", default=None, metavar="PATH|URL")
    p.add_argument("--write", action="store_true", help="Save it as .momo/box.json")
    p.add_argument("--force", action="store_true", help="Overwrite an existing box.json")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("build", help="Build the momo image (or --project's)")
    _common(p)
    _toolchain_flags(p)
    p.add_argument("--project", action="store_true", help="Build the repo's toolchain image")
    p.add_argument("--repo", default=None, metavar="PATH|URL")
    p.add_argument("--ref", default=None)
    p.add_argument("--pull", action="store_true", help="Refresh the base image")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("prepare", help="Re-run a stopped box's dependency prefetch")
    _common(p)
    _toolchain_flags(p)
    p.add_argument("name")
    p.add_argument("--prepare-net", choices=("pull", "open"), default=None,
                   help="Override where it fetches (default: as the box was created)")
    _low_mem_flag(p)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("start", help="Start a stopped web box again, with its settings")
    _common(p)
    p.add_argument("name")
    p.add_argument("--yes", action="store_true",
                   help="Approve a repo's box configuration without asking")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("allow", help="Allow more hosts in a running pull box")
    _common(p)
    p.add_argument("name")
    p.add_argument("hosts", nargs="+")
    p.set_defaults(func=cmd_allow)

    p = sub.add_parser("netlog", help="A pull box's proxy decisions")
    _common(p)
    p.add_argument("name")
    p.add_argument("-n", "--lines", type=int, default=50)
    p.set_defaults(func=cmd_netlog)

    p = sub.add_parser("dash", help="A page on this machine showing every box's setup, "
                                    "network and sync")
    _common(p)
    p.add_argument("--port", type=int, default=0, help="Port on 127.0.0.1 (default: any free one)")
    p.add_argument("--no-open", action="store_true", help="Print the address, don't open a browser")
    p.set_defaults(func=cmd_dash)

    for cmd, fn, helptext in (("ls", cmd_ls, "List boxes"),):
        p = sub.add_parser(cmd, help=helptext)
        _common(p)
        p.set_defaults(func=fn)
    for cmd, fn, helptext in (("url", cmd_url, "Print a web box's address"),
                              ("stop", cmd_stop, "Stop a box (keeps its workspace)"),
                              ("rm", cmd_rm, "Remove a box and its volumes")):
        p = sub.add_parser(cmd, help=helptext)
        _common(p)
        p.add_argument("name")
        if cmd == "rm":
            p.add_argument("--yes", action="store_true")
        p.set_defaults(func=fn)
    p = sub.add_parser("logs", help="A box's momo output")
    _common(p)
    p.add_argument("name")
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    momo_args: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, momo_args = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    args.momo_args = momo_args
    try:
        return args.func(args)
    except BoxError as e:
        say(f"error: {e}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
