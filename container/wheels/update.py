#!/usr/bin/env python3
"""Refresh the vendored wheels of momo's optional extras for the momo-box image.

    .venv/bin/python container/wheels/update.py            # both architectures
    .venv/bin/python container/wheels/update.py --check    # verify only, no network
    .venv/bin/python container/wheels/update.py --python-image [X.Y.Z]
                                     # re-pin momo's Python (the official image's digest)

The image installs momo's extras (tree-sitter grammars, pypdf: requirements.txt)
from these folders with `pip install --no-index --require-hashes`, so a build
never asks PyPI for anything: what goes into the image is what is committed here.

For each architecture this downloads the Linux wheels that requirements.txt
allows for the Python the Dockerfile pins (PY_VERSION), replaces
container/wheels/<arch>/*.whl, and writes <arch>/requirements.lock: one exact
`name==version --hash=sha256:…` line per wheel.  A requirement without a wheel
for that platform is skipped (momo runs without that grammar) and listed in the
lock's header.  Review the diff before committing: the wheels you commit are the
ones every box runs.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
REQUIREMENTS = REPO / "requirements.txt"
DOCKERFILE = REPO / "container" / "Dockerfile"
ARCHES = {"amd64": "x86_64", "arm64": "aarch64"}
# Newest first: pip takes the best wheel it may install on any of these.
PLATFORMS = ("manylinux_2_28_{m}", "manylinux_2_17_{m}", "manylinux2014_{m}")
LOCK = "requirements.lock"
SKIPPED = "# skipped (no wheel for this platform): "


def py_version(dockerfile: Path = DOCKERFILE) -> str:
    """'3.14' from the Dockerfile's `ARG PY_VERSION=3.14.10`."""
    m = re.search(r"^ARG PY_VERSION=(\d+)\.(\d+)", dockerfile.read_text(), re.M)
    if not m:
        raise SystemExit(f"no ARG PY_VERSION in {dockerfile}")
    return f"{m.group(1)}.{m.group(2)}"


# ── momo's Python: the official image, pinned by digest ──────────────────────

_IMAGE_RE = re.compile(r"^ARG PY_IMAGE=docker\.io/library/python:(?P<tag>\S+)@(?P<digest>sha256:[0-9a-f]{64})$", re.M)


def py_image(dockerfile: Path = DOCKERFILE) -> tuple[str, str]:
    """(tag, digest) of the Dockerfile's `ARG PY_IMAGE=docker.io/library/python:TAG@DIGEST`."""
    m = _IMAGE_RE.search(dockerfile.read_text())
    if not m:
        raise SystemExit(f"no ARG PY_IMAGE=docker.io/library/python:TAG@sha256:… in {dockerfile}")
    return m.group("tag"), m.group("digest")


def registry_digest(tag: str, repo: str = "library/python") -> str:
    """The multi-arch index digest Docker Hub serves for repo:tag (anonymous)."""
    import json
    import urllib.request
    token = json.load(urllib.request.urlopen(
        "https://auth.docker.io/token?service=registry.docker.io"
        f"&scope=repository:{repo}:pull", timeout=30))["token"]
    req = urllib.request.Request(
        f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}", method="HEAD",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.oci.image.index.v1+json, "
                           "application/vnd.docker.distribution.manifest.list.v2+json"})
    digest = urllib.request.urlopen(req, timeout=30).headers.get("Docker-Content-Digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SystemExit(f"Docker Hub gave no digest for {repo}:{tag}")
    return digest


def set_python_image(version: str | None = None, dockerfile: Path = DOCKERFILE) -> bool:
    """Pin PY_IMAGE to the current digest of python:<version>-<variant> (variant as
    now, e.g. slim-bookworm), and PY_VERSION to version.  True when the minor
    version changed: the cp3XY wheels must follow."""
    text = dockerfile.read_text()
    tag, old_digest = py_image(dockerfile)
    old_version = re.search(r"^ARG PY_VERSION=(\S+)$", text, re.M).group(1)
    if not tag.startswith(old_version + "-"):
        raise SystemExit(f"PY_IMAGE tag {tag} does not start with PY_VERSION {old_version}")
    version = version or old_version
    new_tag = version + tag[len(old_version):]
    digest = registry_digest(new_tag)
    text = re.sub(r"^ARG PY_VERSION=\S+$", f"ARG PY_VERSION={version}", text, flags=re.M)
    text = _IMAGE_RE.sub(f"ARG PY_IMAGE=docker.io/library/python:{new_tag}@{digest}", text)
    dockerfile.write_text(text)
    print(f"python image: {tag}@{old_digest[:19]}… -> {new_tag}@{digest[:19]}…")
    return version.split(".")[:2] != old_version.split(".")[:2]


def requirement_lines(path: Path = REQUIREMENTS) -> list[str]:
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def req_name(line: str) -> str:
    return norm(re.split(r"[<>=!~;\[\s]", line, maxsplit=1)[0])


def wheel_info(filename: str) -> tuple[str, str, str]:
    """(normalized name, version, abi tag) from a wheel's file name."""
    parts = filename[:-len(".whl")].split("-")
    if not filename.endswith(".whl") or len(parts) not in (5, 6):
        raise ValueError(f"not a wheel file name: {filename}")
    return norm(parts[0]), parts[1], parts[-2]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_lock(folder: Path, skipped: list[str], python: str, arch: str):
    lines = [f"# momo-box extras for CPython {python}, linux/{arch}: generated by "
             "container/wheels/update.py from requirements.txt; do not edit."]
    lines += [SKIPPED + s for s in skipped]
    for whl in sorted(folder.glob("*.whl")):
        name, version, _ = wheel_info(whl.name)
        lines.append(f"{name}=={version} --hash=sha256:{sha256(whl)}")
    (folder / LOCK).write_text("\n".join(lines) + "\n")


def read_lock(folder: Path) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """({name: (version, sha256)}, skipped requirement lines)."""
    pins, skipped = {}, []
    for line in (folder / LOCK).read_text().splitlines():
        if line.startswith(SKIPPED):
            skipped.append(line[len(SKIPPED):].strip())
        elif line and not line.startswith("#"):
            m = re.fullmatch(r"(\S+)==(\S+) --hash=sha256:([0-9a-f]{64})", line.strip())
            if not m:
                raise ValueError(f"{folder / LOCK}: bad line {line!r}")
            pins[norm(m.group(1))] = (m.group(2), m.group(3))
    return pins, skipped


def check(folder: Path, python: str) -> list[str]:
    """What is wrong with one architecture's folder ([] when it is consistent)."""
    problems = []
    try:
        pins, skipped = read_lock(folder)
    except (OSError, ValueError) as e:
        return [str(e)]
    on_disk = {}
    for whl in folder.glob("*.whl"):
        name, version, abi = wheel_info(whl.name)
        on_disk[name] = whl
        if name not in pins:
            problems.append(f"{whl.name} is not in {LOCK}")
        elif pins[name] != (version, sha256(whl)):
            problems.append(f"{whl.name} does not match its {LOCK} line")
        cp = re.fullmatch(r"cp(\d)(\d+)", abi)
        if cp and f"{cp.group(1)}.{cp.group(2)}" != python:
            problems.append(f"{whl.name} is built for CPython {cp.group(1)}.{cp.group(2)}, "
                            f"the image runs {python}")
    problems += [f"{n}=={v} in {LOCK} has no wheel" for n, (v, _) in pins.items()
                 if n not in on_disk]
    skipped_names = {req_name(s) for s in skipped}
    problems += [f"{req} from requirements.txt is neither vendored nor listed as skipped"
                 for req in requirement_lines() if req_name(req) not in pins
                 and req_name(req) not in skipped_names]
    return problems


def download(arch: str, python: str, dest: Path) -> list[str]:
    """Wheels for every requirement into dest; returns the ones with no wheel."""
    machine = ARCHES[arch]
    base = [sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "-q",
            "--only-binary", ":all:", "--implementation", "cp", "--python-version", python,
            "--dest", str(dest)]
    for p in PLATFORMS:
        base += ["--platform", p.format(m=machine)]
    skipped = []
    for req in requirement_lines():
        r = subprocess.run([*base, req], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  {arch}: no wheel for {req}, skipped", file=sys.stderr)
            skipped.append(req)
    return skipped


def update(arch: str, python: str):
    folder = HERE / arch
    old = read_lock(folder)[0] if (folder / LOCK).exists() else {}
    with tempfile.TemporaryDirectory(prefix=f".dl-{arch}-", dir=HERE) as tmp:
        skipped = download(arch, python, Path(tmp))
        new = sorted(Path(tmp).glob("*.whl"))
        if not new:
            raise SystemExit(f"{arch}: nothing downloaded")
        folder.mkdir(exist_ok=True)
        for whl in folder.glob("*.whl"):
            whl.unlink()
        for whl in new:
            shutil.move(str(whl), folder / whl.name)
    write_lock(folder, skipped, python, arch)
    pins = read_lock(folder)[0]
    for name in sorted(set(old) | set(pins)):
        was, now = old.get(name, (None,))[0], pins.get(name, (None,))[0]
        if was != now:
            print(f"  {arch}: {name} {was or '(new)'} -> {now or '(removed)'}")
    print(f"{arch}: {len(pins)} wheels, {sum(f.stat().st_size for f in folder.glob('*.whl')) // 1024} KB")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="verify the folders; download nothing")
    ap.add_argument("--arch", choices=sorted(ARCHES), action="append",
                    help="only this architecture (repeatable; default: all)")
    ap.add_argument("--python-image", nargs="?", const="", metavar="X.Y.Z",
                    help="re-pin the Dockerfile's PY_IMAGE digest (optionally to Python "
                         "X.Y.Z) instead of refreshing the wheels")
    args = ap.parse_args(argv)
    if args.python_image is not None:
        if set_python_image(args.python_image or None):
            print("the minor version changed: run update.py again to refresh the wheels")
        return 0
    python = py_version()
    bad = False
    for arch in args.arch or sorted(ARCHES):
        if not args.check:
            update(arch, python)
        for p in check(HERE / arch, python):
            print(f"{arch}: {p}", file=sys.stderr)
            bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
