"""momo-box entrypoint: runs inside the container.

    entrypoint.py [start] [-- momo args…]   set up /workspace, run momo, snapshot on exit
    entrypoint.py export [--if-changed] [--since SHA]
                                            snapshot the workspace for the host to land
    entrypoint.py watch                     print a line whenever a turn ends (the syncer)
    entrypoint.py prepare                   set up /workspace, run the prepare commands
    entrypoint.py gw …                      the gateway sidecar (forward.py)

Configuration arrives as MOMO_* environment variables set by the launcher
(container/momo_box.py); none of them reach momo's own environment.

The workspace comes in as a git bundle (/in/source.bundle) and goes out the same
way: `export` commits the worktree into a temporary index (the model's index and
worktree are never touched), points refs/momo/export at it and writes
~/exports/latest.bundle (refs/momo/base..refs/momo/export, or only what is new
since the commit the host landed last: --since) + latest.json, which the
launcher copies out and fetches into the source repo as branch momo/<name>.

Standard library only; stdout belongs to momo (`-p` prints its answer there),
so this file reports on stderr, except `export`, whose JSON is for the launcher.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

# Imported where they are used (hashlib, shutil, urllib): this module also runs
# as long-lived helpers (the supervisor, the syncer's `watch`), and every module
# loaded stays in their memory.  urllib.request alone is 8 MB.

APP = Path(__file__).resolve().parent.parent          # /opt/momo/app
MOMO = APP / "momo-coding-harness.py"
WORKSPACE = Path(os.environ.get("MOMO_WORKSPACE", "/workspace"))
INBOX = Path(os.environ.get("MOMO_INBOX", "/in"))
HOME = Path.home()
STATE_DIR = HOME / ".momo-box"
EXPORTS = HOME / "exports"
TURN_MARKER = STATE_DIR / "turn.json"
BOX_STATUS = Path("/run/momo-box")     # the host's status for the Setup panel (read-only)
MAX_EXPORT_FILE = 10 * 1024 * 1024    # bigger new/changed files stay out of the snapshot
WEB_PORT = 8765
GIT_NAME, GIT_EMAIL = "momo", "momo@momo-box.invalid"
SKIP_DIRS = {".git", ".momo", "node_modules", ".venv", "venv", "__pycache__", "build",
             "target", "dist", ".gradle"}

_MARK_BEGIN, _MARK_END = ">>> momo-box proxy >>>", "<<< momo-box proxy <<<"
_LOWMEM_BEGIN, _LOWMEM_END = ">>> momo-box low-mem >>>", "<<< momo-box low-mem <<<"
# Low-memory mode (momo-box --low-mem): no Gradle daemon or Kotlin daemon left
# resident between builds, and a modest heap for the one-use build JVM.
LOW_MEM_GRADLE = ["org.gradle.daemon=false", "kotlin.compiler.execution.strategy=in-process",
                  "org.gradle.jvmargs=-Xmx512m -XX:+UseSerialGC"]
LOW_MEM_INDEX = "32mb"                # momo's code index budget in low-memory mode
_SHA_RE = re.compile(r"[0-9a-f]{40,64}")


def log(msg: str):
    print(f"momo-box: {msg}", file=sys.stderr, flush=True)


def git(*args: str, cwd: Path = WORKSPACE, env: dict | None = None, check: bool = True,
        text: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=text,
                       env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        err = r.stderr if text else r.stderr.decode("utf-8", "replace")
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {err.strip()}")
    return r


def _out(*args: str, **kw) -> str:
    return git(*args, **kw).stdout.strip()


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


# ── workspace ─────────────────────────────────────────────────────────────────

def prepare_workspace(ws: Path | None = None, inbox: Path | None = None,
                      ref: str | None = None, ref_name: str | None = None) -> str:
    """Clone the bundle into an empty workspace (or start an empty repo); a
    workspace that already has a repo is kept.  Returns what happened."""
    ws, inbox = ws or WORKSPACE, inbox or INBOX
    ref = ref or os.environ.get("MOMO_REF") or None
    ref_name = ref_name or os.environ.get("MOMO_REF_NAME") or None
    state_path = STATE_DIR / "state.json"
    ws.mkdir(parents=True, exist_ok=True)
    if (ws / ".git").exists():
        return "kept"
    if any(ws.iterdir()):
        raise RuntimeError(f"{ws} is not empty and is not a git repository")
    bundle = inbox / "source.bundle"
    if bundle.is_file():
        # The launcher's bundle holds one ref (refs/momo-box-src/<name>), a compose
        # user's `git bundle create --all` holds branches and tags: take whatever is
        # there, without a remote (the bundle's path is gone later).
        git("init", "-q", "-b", "momo-box", cwd=ws)
        git("fetch", "-q", "--no-tags", str(bundle), "+refs/heads/*:refs/remotes/source/*",
            "+refs/tags/*:refs/tags/*", "+refs/momo-box-src/*:refs/momo/src/*",
            "+refs/momo-box/*:refs/momo/src/*", cwd=ws)
        sha = _source_commit(ws, bundle, ref)
        branch = ref_name if ref_name and _valid_branch(ref_name, ws) else "momo-box"
        git("checkout", "-q", "-B", branch, sha, cwd=ws)
        host_has_base = True
        how = f"cloned {branch} at {sha[:12]}"
    else:
        git("init", "-q", "-b", "main", cwd=ws)
        host_has_base = False
        how = "empty repository"
    git("config", "user.name", GIT_NAME, cwd=ws)
    git("config", "user.email", GIT_EMAIL, cwd=ws)
    if not host_has_base:
        git("commit", "-q", "--allow-empty", "-m", "momo-box: empty workspace", cwd=ws)
    base = _out("rev-parse", "HEAD", cwd=ws)
    git("update-ref", "refs/momo/base", base, cwd=ws)
    dirty = inbox / "dirty.patch"
    if dirty.is_file() and dirty.stat().st_size:
        # The host's uncommitted changes (--with-uncommitted): one commit of their own
        # on top of base, so they land separately from what the model does.
        git("apply", "--index", "--whitespace=nowarn", str(dirty), cwd=ws)
        git("commit", "-q", "--no-verify", "-m", "momo-box: uncommitted changes from the host",
            cwd=ws)
        how += " + the host's uncommitted changes"
    _write_json(state_path, {"base": base, "host_has_base": host_has_base,
                             "created": time.time(), "how": how})
    return how


def _source_commit(ws: Path, bundle: Path, ref: str | None) -> str:
    """The commit the box starts from: `ref` (a sha, or a branch or tag of the
    bundle), else the bundle's HEAD, else its only ref."""
    if ref:
        for cand in (ref, f"refs/remotes/source/{ref}", f"refs/tags/{ref}"):
            r = git("rev-parse", "--verify", "-q", f"{cand}^{{commit}}", cwd=ws, check=False)
            if r.returncode == 0:
                return r.stdout.strip()
        raise RuntimeError(f"{ref} is not a commit in the source bundle")
    heads = [ln.split(None, 1) for ln in
             _out("bundle", "list-heads", str(bundle), cwd=ws).splitlines() if ln.strip()]
    for sha, name in heads:
        if name == "HEAD":
            return sha
    if len(heads) == 1:
        return heads[0][0]
    raise RuntimeError("the source bundle has several refs and no HEAD: set MOMO_REF")


def _valid_branch(name: str, ws: Path) -> bool:
    return git("check-ref-format", "--branch", name, cwd=ws, check=False).returncode == 0


# ── snapshot / export ─────────────────────────────────────────────────────────

def _large_changes(ws: Path) -> list[str]:
    """New or modified files over MAX_EXPORT_FILE (git-ignored ones excluded):
    whatever differs from HEAD, staged or not, plus untracked files."""
    names = git("ls-files", "-z", "-o", "--exclude-standard", cwd=ws).stdout.split("\0")
    names += git("diff", "--name-only", "-z", "HEAD", cwd=ws, check=False).stdout.split("\0")
    big = []
    for rel in filter(None, names):
        try:
            if (ws / rel).stat().st_size > MAX_EXPORT_FILE:
                big.append(rel)
        except OSError:
            pass
    return sorted(set(big))


def _worktree_tree(ws: Path, big: list[str]) -> str:
    """The tree of the worktree as it is now, via a temporary index.  The index
    starts as a copy of the repo's own, so git's stat cache is kept and only
    changed files are read (a fresh `read-tree HEAD` would re-hash every file on
    every snapshot).  It sits in the git dir so a split index still resolves.

    The copy keeps the original's mtime.  Git trusts a cached entry only when
    the file is older than the index itself; a copy with a newer timestamp would
    make it trust entries for files edited in the same second as the last index
    write (same size, same second: an edit the snapshot would then miss)."""
    gitdir = Path(_out("rev-parse", "--absolute-git-dir", cwd=ws))
    index = gitdir / f"momo-export-{os.getpid()}.index"
    env = {"GIT_INDEX_FILE": str(index)}
    try:
        try:
            st = (gitdir / "index").stat()
            import shutil
            shutil.copyfile(gitdir / "index", index)
            os.utime(index, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            git("read-tree", "HEAD", cwd=ws, env=env)
        excludes = [f":(exclude,literal){p}" for p in big]
        git("add", "-A", "--", ".", *excludes, cwd=ws, env=env)
        if big:      # left out: whatever the model staged of them goes back to HEAD's entry
            git("reset", "-q", "HEAD", "--", *[f":(literal){p}" for p in big], cwd=ws, env=env,
                check=False)
        return _out("write-tree", cwd=ws, env=env)
    finally:
        index.unlink(missing_ok=True)
        index.with_name(index.name + ".lock").unlink(missing_ok=True)


def snapshot(ws: Path | None = None, exports: Path | None = None, *, if_changed: bool = False,
             exit_code: int | None = None, since: str | None = None) -> dict | None:
    """Commit the worktree (incl. untracked, minus ignored and >10 MB files) into a
    temporary index → refs/momo/export, and write latest.bundle + latest.json.
    The model's own index, worktree and branches are untouched.  `since` is a
    commit the host already has (what it landed last): the bundle then holds only
    what is new after it.  None when if_changed and nothing changed since the
    last snapshot."""
    ws, exports = ws or WORKSPACE, exports or EXPORTS
    state = _read_json(STATE_DIR / "state.json")
    base = state.get("base") or _out("rev-parse", "refs/momo/base", cwd=ws)
    host_has_base = state.get("host_has_base", True)
    head = _out("rev-parse", "HEAD", cwd=ws)
    big = _large_changes(ws)
    tree = _worktree_tree(ws, big)
    turn = _read_json(TURN_MARKER)
    last = _read_json(exports / "latest.json")
    if if_changed and last.get("head") == head and last.get("tree") == tree:
        return None
    if tree == _out("rev-parse", "HEAD^{tree}", cwd=ws):
        export = head
    else:
        msg = "momo-box: uncommitted work" + (f" (turn {turn['turn']})" if "turn" in turn else "")
        # Fixed dates: the same worktree gives the same commit, so re-syncs are no-ops.
        stamp = {"GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
                 "GIT_AUTHOR_NAME": GIT_NAME, "GIT_AUTHOR_EMAIL": GIT_EMAIL,
                 "GIT_COMMITTER_NAME": GIT_NAME, "GIT_COMMITTER_EMAIL": GIT_EMAIL}
        export = _out("commit-tree", tree, "-p", head, "-m", msg, cwd=ws, env=stamp)
    git("update-ref", "refs/momo/export", export, cwd=ws)
    commits = int(_out("rev-list", "--count", f"{base}..{export}", cwd=ws) or 0) \
        if host_has_base else int(_out("rev-list", "--count", export, cwd=ws) or 0)
    info = {
        "seq": int(last.get("seq", 0)) + 1, "ts": time.time(),
        "base": base, "head": head, "tree": tree, "export": export,
        "host_has_base": host_has_base, "commits": commits,
        "shortstat": _out("diff", "--shortstat", base, export, cwd=ws) if host_has_base else "",
        "turn": turn.get("turn"), "skipped_large": big, "exit_code": exit_code,
        "bundle": None, "since": None,
    }
    exports.mkdir(parents=True, exist_ok=True)
    bundle = exports / "latest.bundle"
    if since and not (_SHA_RE.fullmatch(since) and git(
            "cat-file", "-e", f"{since}^{{commit}}", cwd=ws, check=False).returncode == 0):
        since = None                      # not a commit this box knows: send everything
    if export != base or not host_has_base:
        tmp = exports / f".latest.{os.getpid()}.bundle"
        rng = ["refs/momo/export"]
        if host_has_base:
            rng.append("^refs/momo/base")
        if since and since != export:
            rng.append(f"^{since}")
            info["since"] = since
        if since == export:               # the host has all of it: nothing to send
            bundle.unlink(missing_ok=True)
        else:
            git("bundle", "create", "-q", str(tmp), *rng, cwd=ws)
            os.replace(tmp, bundle)
            info["bundle"] = str(bundle)
    else:
        bundle.unlink(missing_ok=True)
    _write_json(exports / "latest.json", info)
    return info


# ── pull-mode proxy settings ──────────────────────────────────────────────────

def proxy_env(proxy: str, no_proxy: list[str]) -> dict[str, str]:
    """Environment that routes package managers through the gateway's allowlist proxy."""
    host, port = _host_port(proxy)
    np = ",".join(["localhost", "127.0.0.1", *no_proxy])
    env = {}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "npm_config_proxy", "npm_config_https_proxy"):
        env[k] = proxy
    env["NO_PROXY"] = env["no_proxy"] = np
    jvm = (f"-Dhttp.proxyHost={host} -Dhttp.proxyPort={port} -Dhttps.proxyHost={host} "
           f"-Dhttps.proxyPort={port} -Dhttp.nonProxyHosts={'|'.join(['localhost', '127.0.0.1', *no_proxy])}")
    old = os.environ.get("JAVA_TOOL_OPTIONS", "")
    env["JAVA_TOOL_OPTIONS"] = f"{old} {jvm}".strip()
    return env


def _host_port(url: str) -> tuple[str, int]:
    rest = url.split("://", 1)[-1].split("/", 1)[0]
    host, _, port = rest.rpartition(":")
    return (host, int(port)) if host else (rest, 80)


def _strip_block(text: str, begin: str, end: str) -> str:
    """Remove a marked block (marker lines included)."""
    lines, out, inside = text.splitlines(keepends=True), [], False
    for line in lines:
        if begin in line:
            inside = True
            continue
        if end in line and inside:
            inside = False
            continue
        if not inside:
            out.append(line)
    return "".join(out)


def configure_low_mem(on: bool, home: Path | None = None):
    """The low-memory block in ~/.gradle/gradle.properties (it overrides the
    project's own settings); on=False removes it again."""
    gp = (home or HOME) / ".gradle" / "gradle.properties"
    text = gp.read_text() if gp.is_file() else ""
    text = _strip_block(text, _LOWMEM_BEGIN, _LOWMEM_END)
    if on:
        block = [f"# {_LOWMEM_BEGIN}", *LOW_MEM_GRADLE, f"# {_LOWMEM_END}"]
        text = (text.rstrip("\n") + "\n" if text.strip() else "") + "\n".join(block) + "\n"
    if text or gp.is_file():
        gp.parent.mkdir(parents=True, exist_ok=True)
        gp.write_text(text)


def configure_jvm_proxy(proxy: str | None, no_proxy: list[str], home: Path | None = None):
    """Gradle's gradle.properties and Maven's settings.xml proxy entries, inside
    marked blocks; proxy=None removes them again (locked/open boxes)."""
    home = home or HOME
    gp = home / ".gradle" / "gradle.properties"
    text = gp.read_text() if gp.is_file() else ""
    text = _strip_block(text, _MARK_BEGIN, _MARK_END)
    if proxy:
        host, port = _host_port(proxy)
        hosts = "|".join(["localhost", "127.0.0.1", *no_proxy])
        block = [f"# {_MARK_BEGIN}"]
        for scheme in ("http", "https"):
            block += [f"systemProp.{scheme}.proxyHost={host}", f"systemProp.{scheme}.proxyPort={port}"]
        block += [f"systemProp.http.nonProxyHosts={hosts}", f"# {_MARK_END}"]
        text = (text.rstrip("\n") + "\n" if text.strip() else "") + "\n".join(block) + "\n"
    if text or gp.is_file():
        gp.parent.mkdir(parents=True, exist_ok=True)
        gp.write_text(text)

    sx = home / ".m2" / "settings.xml"
    text = sx.read_text() if sx.is_file() else ""
    text = _strip_block(text, _MARK_BEGIN, _MARK_END)
    if proxy:
        host, port = _host_port(proxy)
        hosts = "|".join(["localhost", "127.0.0.1", *no_proxy])
        entries = "".join(
            f"    <proxy><id>momo-box-{s}</id><active>true</active><protocol>{s}</protocol>"
            f"<host>{host}</host><port>{port}</port><nonProxyHosts>{hosts}</nonProxyHosts></proxy>\n"
            for s in ("http", "https"))
        block = f"    <!-- {_MARK_BEGIN} -->\n{entries}    <!-- {_MARK_END} -->\n"
        if not text.strip():
            text = ('<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">\n'
                    f"  <proxies>\n{block}  </proxies>\n</settings>\n")
        elif "<proxies>" in text:
            i = text.index("<proxies>") + len("<proxies>")
            text = text[:i] + "\n" + block + text[i:].lstrip("\n")
        elif "</settings>" in text:
            i = text.rindex("</settings>")
            text = text[:i] + f"  <proxies>\n{block}  </proxies>\n" + text[i:]
    if text or sx.is_file():
        sx.parent.mkdir(parents=True, exist_ok=True)
        sx.write_text(text)


# ── inference pre-flight ──────────────────────────────────────────────────────

def preflight_in_child(host: str, provider: str) -> str | None:
    """preflight() in a short-lived process: the supervisor that waits for momo
    then never loads urllib (8 MB it would keep for the box's lifetime)."""
    try:
        r = subprocess.run([sys.executable, "-S", __file__, "preflight", host, provider],
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None


def preflight(host: str, provider: str, timeout: float = 5.0) -> str | None:
    """None if the inference server answers, else a hint for the log."""
    import urllib.error
    import urllib.request
    path = "/api/tags" if provider == "ollama" else "/v1/models"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via a proxy
    try:
        with opener.open(host.rstrip("/") + path, timeout=timeout):
            return None
    except urllib.error.HTTPError:
        return None                    # it answered; momo deals with the details
    except (OSError, ValueError) as e:
        hint = os.environ.get("MOMO_LLM_HINT", "")
        return f"the inference server at {host} is not reachable ({e}). {hint}".strip()


# ── momo ──────────────────────────────────────────────────────────────────────

def momo_argv(env: dict[str, str], extra: list[str]) -> list[str]:
    """momo's command line.  Every value goes as --flag=VALUE: a value that
    starts with "-" (a web token from token_urlsafe, 1 in 64; a prompt; a note)
    would otherwise be read as an option and momo would not start."""
    argv = [sys.executable, str(MOMO), f"--workspace={WORKSPACE}",
            f"--turn-marker={TURN_MARKER}"]
    for key, flag in (("MOMO_PROVIDER", "--provider"), ("MOMO_HOST", "--host"),
                      ("MOMO_MODEL", "--model")):
        if env.get(key):
            argv.append(f"{flag}={env[key]}")
    for note in json.loads(env.get("MOMO_NOTES") or "[]"):
        argv.append(f"--environment-note={note}")
    if BOX_STATUS.is_dir():
        argv.append(f"--box-status={BOX_STATUS}")
    ui = env.get("MOMO_UI", "web")
    if ui == "web":
        argv += ["--web-only", "--web-host=0.0.0.0", f"--web-port={WEB_PORT}"]
        if env.get("MOMO_WEB_TOKEN"):
            argv.append(f"--web-token={env['MOMO_WEB_TOKEN']}")
        if env.get("MOMO_WEB_PUBLIC_URL"):
            argv.append(f"--web-public-url={env['MOMO_WEB_PUBLIC_URL']}")
    elif ui == "prompt":
        if env.get("MOMO_PROMPT_STDIN") == "1":
            argv.append("--prompt=-")
        else:
            text = (INBOX / "prompt.txt").read_text(encoding="utf-8")
            argv.append(f"--prompt={text}")
    elif ui == "tui":
        pass
    else:
        raise RuntimeError(f"MOMO_UI: unknown {ui!r}")
    # The code index in this process: worker processes (up to 8, one per core the
    # engine shows) would each hold a Python and tree-sitter.  `-- --index-workers N`
    # comes later and wins.
    argv.append("--index-workers=1")
    if env.get("MOMO_LOW_MEM") == "1":
        argv.append(f"--index-max-mem={LOW_MEM_INDEX}")
    argv += extra
    if env.get("MOMO_NET") in ("locked", "pull"):
        argv += ["--net", "off"]      # last, so pass-through args can't turn fetch_url on
    return argv


def child_env(env: dict[str, str]) -> dict[str, str]:
    """momo's environment: no MOMO_* launcher settings (the web token among them),
    plus the proxy variables in a pull box and the box.json env."""
    out = {k: v for k, v in env.items() if not k.startswith("MOMO_")}
    out.update(json.loads(env.get("MOMO_BOX_ENV") or "{}"))
    if env.get("MOMO_NET") == "pull" and env.get("MOMO_PROXY"):
        out.update(proxy_env(env["MOMO_PROXY"], json.loads(env.get("MOMO_NO_PROXY") or "[]")))
    return out


def start(extra: list[str]) -> int:
    env = dict(os.environ)
    how = prepare_workspace()
    if how != "kept":
        log(f"workspace: {how}")
    pull = env.get("MOMO_NET") == "pull"
    configure_jvm_proxy(env.get("MOMO_PROXY") if pull else None,
                        json.loads(env.get("MOMO_NO_PROXY") or "[]"))
    configure_low_mem(env.get("MOMO_LOW_MEM") == "1")
    if env.get("MOMO_HOST") and (hint := preflight_in_child(env["MOMO_HOST"],
                                                            env.get("MOMO_PROVIDER", ""))):
        log(hint)
    child = subprocess.Popen(momo_argv(env, extra), env=child_env(env), cwd=WORKSPACE)

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, forward)
    code = child.wait()
    if code < 0:
        code = 128 - code
    try:
        snapshot(exit_code=code)
    except Exception as e:                       # never hide momo's exit code
        log(f"could not snapshot the workspace: {e}")
    return code


# ── prepare ───────────────────────────────────────────────────────────────────

def _matches(rel: str, patterns: list[str]) -> bool:
    p = PurePosixPath(rel)
    return any(p.full_match(pat) for pat in patterns)


def prepare_hash(commands: list[str], inputs: list[str], ws: Path | None = None) -> str:
    """Of the commands and the content of every file the inputs match."""
    ws = ws or WORKSPACE
    import hashlib
    h = hashlib.sha256(json.dumps(commands).encode())
    if inputs:
        for dirpath, dirnames, filenames in os.walk(ws):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                rel = (Path(dirpath) / name).relative_to(ws).as_posix()
                if _matches(rel, inputs):
                    h.update(rel.encode() + b"\0")
                    try:
                        h.update(hashlib.sha256((ws / rel).read_bytes()).digest())
                    except OSError:
                        pass
    return h.hexdigest()


def prepare() -> int:
    env = dict(os.environ)
    how = prepare_workspace()
    if how != "kept":
        log(f"workspace: {how}")
    commands = json.loads(env.get("MOMO_PREPARE") or "[]")
    inputs = json.loads(env.get("MOMO_PREPARE_INPUTS") or "[]")
    if not commands:
        return 0
    digest = prepare_hash(commands, inputs)
    stamp = STATE_DIR / "prepare.json"
    if env.get("MOMO_PREPARE_FORCE") != "1" and _read_json(stamp).get("hash") == digest:
        log("prepare: up to date")
        return 0
    # MOMO_NET=pull + MOMO_PROXY: the prefetch goes through the gateway's registry
    # allowlist (the default for pull and locked boxes); without them it is open.
    pull = env.get("MOMO_NET") == "pull" and bool(env.get("MOMO_PROXY"))
    configure_jvm_proxy(env.get("MOMO_PROXY") if pull else None,
                        json.loads(env.get("MOMO_NO_PROXY") or "[]"))
    configure_low_mem(env.get("MOMO_LOW_MEM") == "1")
    run_env = child_env(env)
    for cmd in commands:
        # "-cmd": best effort (as in a Makefile), e.g. a test run that fetches test-time
        # plugins and providers but may fail on the project's own failing tests.
        optional = cmd.startswith("-")
        cmd = cmd[1:].strip() if optional else cmd
        log(f"prepare: {cmd}")
        r = subprocess.run(["sh", "-c", cmd], cwd=WORKSPACE, env=run_env,
                           stdout=sys.stderr, stdin=subprocess.DEVNULL)
        if r.returncode != 0 and optional:
            log(f"prepare: {cmd} exited {r.returncode} (best effort, continuing)")
        elif r.returncode != 0:
            log(f"prepare failed ({r.returncode}): {cmd}")
            return r.returncode
    _write_json(stamp, {"hash": digest, "ts": time.time(), "commands": commands, "inputs": inputs})
    return 0


# ── watch ─────────────────────────────────────────────────────────────────────

def watch(interval: float = 0.5, heartbeat: float = 20.0, out=None, rounds: int | None = None) -> int:
    """One JSON line whenever the turn marker changes, for the host's syncer: it
    holds a single `exec` and blocks on this instead of polling.  A blank line
    every `heartbeat` seconds makes a dead reader show up as a broken pipe."""
    out = out or sys.stdout
    last, stamp, beat = None, None, time.monotonic()
    while rounds is None or rounds > 0:
        if rounds is not None:
            rounds -= 1
        try:
            mtime = TURN_MARKER.stat().st_mtime_ns
        except OSError:
            mtime = None
        try:
            if mtime is not None and mtime != stamp:
                stamp = mtime
                seq = _read_json(TURN_MARKER).get("seq")
                if seq is not None and seq != last:
                    last = seq
                    out.write(json.dumps({"seq": seq}) + "\n")
                    out.flush()
                    beat = time.monotonic()
            if time.monotonic() - beat >= heartbeat:
                out.write("\n")
                out.flush()
                beat = time.monotonic()
        except (BrokenPipeError, OSError):
            return 0
        time.sleep(interval)
    return 0


# ── main ──────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    cmd = argv[0] if argv and not argv[0].startswith("-") else "start"
    rest = argv[1:] if argv and argv[0] == cmd else argv
    if rest[:1] == ["--"]:
        rest = rest[1:]
    try:
        if cmd == "start":
            return start(rest)
        if cmd == "export":
            since = rest[rest.index("--since") + 1] if "--since" in rest[:-1] else None
            info = snapshot(if_changed="--if-changed" in rest, since=since)
            if info is not None:
                print(json.dumps(info))
            return 0
        if cmd == "watch":
            return watch()
        if cmd == "preflight":                  # preflight_in_child
            print(preflight(rest[0], rest[1] if len(rest) > 1 else "") or "")
            return 0
        if cmd == "prepare":
            return prepare()
        if cmd == "gw":
            sys.path.insert(0, str(Path(__file__).parent))
            import forward
            return forward.main(rest)
    except RuntimeError as e:
        log(str(e))
        return 1
    log(f"unknown command {cmd!r} (start, export, watch, prepare, gw)")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
