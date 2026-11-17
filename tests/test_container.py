"""momo-box (container/): toolchain detection, the entrypoint's workspace setup and
snapshots, landing on the host, the gateway/proxy, the launcher's container
arguments, and momo's --web-public-url / --environment-note / --turn-marker.
No container engine needed: everything runs against temp git repos and sockets."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "container"))

import detect          # noqa: E402
import entrypoint      # noqa: E402
import forward         # noqa: E402
import momo_box        # noqa: E402

PRESETS = detect.load_presets()


def git(cwd, *args, env=None):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                            **(env or {})})
    if r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def write_tree(root: Path, files: dict[str, str]) -> Path:
    write(root, files)
    return root


def write(root: Path, files: dict[str, str]):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class TmpDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def tree(self, files: dict[str, str]) -> detect.DirTree:
        root = self.tmp / "proj"
        write(root, files)
        return detect.DirTree(root)

    def repo(self, path: Path, files: dict[str, str]) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        git(path, "init", "-q", "-b", "main")
        write(path, files)
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "init")
        return path


# ── detection ────────────────────────────────────────────────────────────────

POM_17 = "<project><properties><maven.compiler.release>17</maven.compiler.release></properties></project>"


class Detection(TmpDir):
    def resolve(self, files, **kw):
        return detect.resolve(self.tree(files), PRESETS, **kw)

    def test_maven_release_picks_the_jdk(self):
        r = self.resolve({"pom.xml": POM_17, "src/main/java/A.java": "class A {}"})
        self.assertEqual(r.source, "detected")
        self.assertEqual(r.base, "maven:3.9-eclipse-temurin-17")
        self.assertEqual(r.prepare, ["mvn -B -q dependency:go-offline",
                                     "-mvn -B -q test -Dmaven.test.failure.ignore=true"])
        self.assertIn("pom.xml", r.prepare_inputs)
        self.assertFalse(r.needs_approval)

    def test_jdk_rounds_up_to_a_released_tag(self):
        pom = POM_17.replace(">17<", ">19<")
        self.assertEqual(self.resolve({"pom.xml": pom}).base, "maven:3.9-eclipse-temurin-21")

    def test_gradle_toolchain_and_wrapper(self):
        r = self.resolve({
            "build.gradle.kts": "kotlin { jvmToolchain(17) }",
            "settings.gradle.kts": "",
            "gradlew": "#!/bin/sh",
            "gradle/wrapper/gradle-wrapper.properties":
                "distributionUrl=https\\://dl.example.com/gradle-8.10-bin.zip\n"})
        self.assertEqual(r.base, "gradle:8-jdk17")
        self.assertEqual(r.prepare[0], "./gradlew build -x test --no-daemon -q")
        # A host the repo names itself never joins the allowlist on its own.
        self.assertNotIn("dl.example.com", r.pull_hosts)
        self.assertTrue(any("--allow-host dl.example.com" in w for w in r.warnings))

    def test_known_wrapper_host_is_allowed(self):
        r = self.resolve({
            "build.gradle.kts": "", "gradlew": "#!/bin/sh",
            "gradle/wrapper/gradle-wrapper.properties":
                "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.10-bin.zip\n"})
        self.assertIn("services.gradle.org", r.registry_hosts)    # a known registry, like the rest
        self.assertEqual(r.warnings, [])

    def test_registry_hosts_follow_the_toolchains(self):
        maven = self.resolve({"pom.xml": POM_17})
        self.assertEqual(maven.registry_hosts, ["repo.maven.apache.org", "repo1.maven.org"])
        mixed = detect.resolve(detect.DirTree(write_tree(self.tmp / "m", {
            "pom.xml": POM_17, "web/package.json": "{}"})), PRESETS)
        self.assertIn("registry.npmjs.org", mixed.registry_hosts)
        self.assertNotIn("crates.io", mixed.registry_hosts)
        none = detect.resolve(detect.DirTree(write_tree(self.tmp / "n", {"notes.txt": "x"})), PRESETS)
        self.assertEqual(none.registry_hosts, [])            # the launcher then allows them all

    def test_kotlin_is_an_alias_of_the_gradle_preset(self):
        r = self.resolve({"a.txt": ""}, preset="kotlin")
        self.assertEqual(r.presets, ["gradle"])
        self.assertTrue(r.base.startswith("gradle:8-jdk"))

    def test_devcontainer_dockerfile_is_part_of_the_approval(self):
        files = {".devcontainer/devcontainer.json": '{"build": {"dockerfile": "Dockerfile"}}',
                 ".devcontainer/Dockerfile": "FROM debian\nRUN echo one\n"}
        root = write_tree(self.tmp / "dc", files)
        r = detect.resolve(detect.DirTree(root), PRESETS)
        self.assertTrue(r.needs_approval)
        self.assertIn("RUN echo one", r.approval_text)

    def test_cmake(self):
        r = self.resolve({"CMakeLists.txt": "project(x)", "src/main.cpp": ""})
        self.assertEqual(r.base, "gcc:14")
        self.assertEqual(r.packages, ["cmake", "ninja-build", "gdb"])

    def test_makefile_needs_c_sources(self):
        self.assertEqual(self.resolve({"Makefile": "all:\n\techo hi\n"}).source, "default")
        self.assertEqual(self.resolve({"Makefile": "all:", "main.c": ""}).presets, ["cpp"])

    def test_node_version_from_nvmrc(self):
        r = self.resolve({"package.json": '{"engines": {"node": ">=18"}}', ".nvmrc": "v20.11.0\n"})
        self.assertEqual(r.base, "node:20-bookworm-slim")

    def test_npm_ci_needs_a_lockfile(self):
        self.assertEqual(self.resolve({"package.json": "{}"}).prepare,
                         ["npm install --no-audit --no-fund"])
        self.assertEqual(self.resolve({"package.json": "{}", "package-lock.json": "{}"}).prepare,
                         ["npm ci --no-audit --no-fund"])

    def test_node_engine_floor_uses_the_default(self):
        r = self.resolve({"package.json": '{"engines": {"node": ">=18"}}'})
        self.assertEqual(r.base, "node:22-bookworm-slim")

    def test_tool_versions(self):
        r = self.resolve({"pom.xml": "<project/>", ".tool-versions": "java temurin-11.0.2\n"})
        self.assertEqual(r.base, "maven:3.9-eclipse-temurin-11")

    def test_mixed_repo_approximates_the_secondary(self):
        r = self.resolve({"build.gradle": "", "settings.gradle": "", "package.json": "{}"})
        self.assertEqual(r.presets, ["gradle"])
        self.assertEqual(r.approximated, ["node"])
        self.assertIn("nodejs", r.packages)
        self.assertIn("package.json", r.prepare_inputs)

    def test_nothing_found(self):
        r = self.resolve({"README.md": "hi"})
        self.assertEqual((r.source, r.base), ("default", "debian:bookworm-slim"))

    def test_depth_limit_and_skip_dirs(self):
        r = self.resolve({"a/b/c/d/pom.xml": POM_17, "node_modules/x/package.json": "{}"})
        self.assertEqual(r.source, "default")

    def test_box_json_wins_and_needs_approval(self):
        r = self.resolve({"pom.xml": POM_17, ".momo/box.json": json.dumps(
            {"base": "eclipse-temurin:21", "packages": "cmake ninja-build",
             "prepare": ["mvn -o -q verify"], "pull_hosts": ["github"], "env": {"A": "1"}})})
        self.assertEqual(r.source, "box.json")
        self.assertEqual(r.base, "eclipse-temurin:21")
        self.assertEqual(r.packages, ["cmake", "ninja-build"])
        self.assertEqual(r.prepare, ["mvn -o -q verify"])
        self.assertIn("github.com", r.pull_hosts)
        self.assertEqual(r.env, {"A": "1"})
        self.assertTrue(r.needs_approval)
        self.assertIn(".momo/box.json", r.approval_text)

    def test_box_json_preset_keeps_its_prepare(self):
        r = self.resolve({"pom.xml": POM_17, ".momo/box.json": '{"preset": "java"}'})
        self.assertEqual(r.base, "maven:3.9-eclipse-temurin-17")
        self.assertEqual(r.prepare[0], "mvn -B -q dependency:go-offline")

    def test_box_json_errors(self):
        with self.assertRaisesRegex(detect.ConfigError, "unknown key"):
            self.resolve({".momo/box.json": '{"bse": "x"}'})
        with self.assertRaisesRegex(detect.ConfigError, "not in the repo"):
            self.resolve({".momo/box.json": '{"dockerfile": ".momo/nope.Dockerfile"}'})

    def test_flags_beat_box_json(self):
        r = self.resolve({".momo/box.json": '{"base": "x:1"}'}, preset="rust")
        self.assertEqual((r.source, r.base), ("flags", "rust:1-slim-bookworm"))
        self.assertEqual(r.base_variant, "slim")
        self.assertFalse(r.needs_approval)
        own = lambda d, files, **kw: detect.resolve(                         # noqa: E731
            detect.DirTree(write_tree(self.tmp / d, files)), PRESETS, **kw)
        full = own("f1", {}, preset="rust", full_base=True)
        self.assertEqual((full.base, full.base_variant), ("rust:1-bookworm", "full"))
        boxed = own("f2", {"Cargo.toml": "", ".momo/box.json": '{"preset": "rust", "full_base": true}'})
        self.assertEqual(boxed.base, "rust:1-bookworm")
        with self.assertRaisesRegex(detect.ConfigError, "full_base"):
            own("f3", {".momo/box.json": '{"full_base": "yes"}'})
        java = own("f4", {"pom.xml": POM_17}, full_base=True)                # one image only
        self.assertEqual((java.base, java.base_variant), ("maven:3.9-eclipse-temurin-17", ""))

    def test_devcontainer_image_with_comments(self):
        r = self.resolve({"pom.xml": POM_17, ".devcontainer/devcontainer.json":
                          '{\n // the image\n "image": "mcr.microsoft.com/devcontainers/java:21",'
                          '\n "features": {"x": {}},\n}'})
        self.assertEqual(r.source, "devcontainer")
        self.assertEqual(r.base, "mcr.microsoft.com/devcontainers/java:21")
        self.assertTrue(r.needs_approval)
        self.assertTrue(any("features" in why for why in r.reasons))

    def test_devcontainer_dockerfile(self):
        r = self.resolve({".devcontainer/devcontainer.json":
                          '{"build": {"dockerfile": "Dockerfile", "context": ".."}}',
                          ".devcontainer/Dockerfile": "FROM debian"})
        self.assertEqual(r.base_dockerfile, ".devcontainer/Dockerfile")
        self.assertEqual(r.base_context, "")

    def test_git_tree_reads_the_commit_not_the_worktree(self):
        repo = self.repo(self.tmp / "r", {"pom.xml": POM_17})
        (repo / "package.json").write_text("{}")     # untracked: not in the box
        r = detect.resolve(detect.tree_for(repo), PRESETS)
        self.assertEqual(r.presets, ["java"])
        self.assertEqual(r.approximated, [])

    def test_box_json_round_trip(self):
        r = self.resolve({"pom.xml": POM_17})
        root = self.tmp / "proj"
        write(root, {".momo/box.json": json.dumps(r.box_json())})
        again = detect.resolve(detect.DirTree(root), PRESETS)
        self.assertEqual((again.source, again.base, again.prepare),
                         ("box.json", r.base, r.prepare))

    def test_expand_hosts(self):
        self.assertEqual(detect.expand_hosts(["a.org", "github"], PRESETS)[:2],
                         ["a.org", "github.com"])


# ── entrypoint: workspace + snapshot, and landing on the host ───────────────

class Workspace(TmpDir):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        for name, value in (("STATE_DIR", self.home / ".momo-box"),
                            ("TURN_MARKER", self.home / ".momo-box" / "turn.json"),
                            ("EXPORTS", self.home / "exports")):
            p = mock.patch.object(entrypoint, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.host = self.repo(self.tmp / "host", {"app.py": "print(1)\n", "README.md": "x\n"})
        self.inbox = self.tmp / "in"
        self.inbox.mkdir()
        git(self.host, "bundle", "create", "-q", str(self.inbox / "source.bundle"), "--all")
        self.ws = self.tmp / "ws"

    def clone(self, **kw):
        return entrypoint.prepare_workspace(self.ws, self.inbox, **kw)

    def snapshot(self, **kw):
        return entrypoint.snapshot(self.ws, self.home / "exports", **kw)

    def land(self, info, name="t"):
        return momo_box.land(self.host, Path(info["bundle"]), name, info)

    def test_clone_from_bundle(self):
        sha = git(self.host, "rev-parse", "HEAD")
        how = self.clone(ref=sha, ref_name="main")
        self.assertIn("cloned main", how)
        self.assertEqual(git(self.ws, "rev-parse", "refs/momo/base"), sha)
        self.assertEqual(git(self.ws, "branch", "--show-current"), "main")
        self.assertEqual(git(self.ws, "remote"), "")
        self.assertEqual(git(self.ws, "config", "user.name"), "momo")
        self.assertEqual(self.clone(), "kept")           # a second start keeps the work

    def test_snapshot_lands_as_a_branch(self):
        self.clone()
        (self.ws / "app.py").write_text("print(2)\n")              # tracked change
        (self.ws / "new.py").write_text("x = 1\n")                 # untracked file
        write(self.ws, {".momo/.gitignore": "*\n", ".momo/scratch/n.txt": "notes"})
        status_before = git(self.ws, "status", "--porcelain")
        info = self.snapshot()
        self.assertEqual(git(self.ws, "status", "--porcelain"), status_before)  # untouched
        self.assertEqual(git(self.ws, "diff", "--cached", "--name-only"), "")
        self.assertEqual(info["commits"], 1)
        line = self.land(info)
        self.assertIn("momo/t ← 1 commit (incl. uncommitted work)", line)
        files = git(self.host, "ls-tree", "-r", "--name-only", "momo/t").splitlines()
        self.assertIn("new.py", files)
        self.assertNotIn(".momo/scratch/n.txt", files)
        self.assertEqual(git(self.host, "show", "momo/t:app.py"), "print(2)")
        self.assertEqual(git(self.host, "branch", "--show-current"), "main")   # checkout untouched

    def test_commits_and_resync(self):
        self.clone()
        (self.ws / "a.txt").write_text("a\n")
        git(self.ws, "add", "a.txt")
        git(self.ws, "commit", "-q", "-m", "model commit")
        self.land(self.snapshot())
        self.assertEqual(git(self.host, "log", "-1", "--format=%s", "momo/t"), "model commit")
        self.assertIsNone(self.snapshot(if_changed=True))             # nothing new
        (self.ws / "b.txt").write_text("b\n")
        line = self.land(self.snapshot(if_changed=True))
        self.assertIn("momo/t ← 2 commits", line)
        self.assertIn("b.txt", git(self.host, "ls-tree", "--name-only", "momo/t"))

    def test_same_worktree_gives_the_same_commit(self):
        self.clone()
        (self.ws / "x").write_text("1")
        first = self.snapshot()["export"]
        self.assertEqual(self.snapshot()["export"], first)

    def test_host_commit_diverts_to_incoming(self):
        self.clone()
        (self.ws / "a").write_text("1")
        self.land(self.snapshot())
        git(self.host, "checkout", "-q", "momo/t")
        (self.host / "mine").write_text("mine")
        git(self.host, "add", "mine")
        git(self.host, "commit", "-q", "-m", "user commit")
        git(self.host, "checkout", "-q", "main")
        (self.ws / "a").write_text("2")
        with mock.patch.object(momo_box, "say"):
            line = self.land(self.snapshot())
        self.assertTrue(line.startswith("momo/t-incoming"))
        self.assertEqual(git(self.host, "log", "-1", "--format=%s", "momo/t"), "user commit")

    def test_a_new_box_with_a_removed_boxs_name_keeps_its_branch(self):
        self.clone()
        (self.ws / "a").write_text("old box\n")
        self.land(self.snapshot())
        old = git(self.host, "rev-parse", "momo/t")
        momo_box.forget_landed(self.host, "t")            # rm t (or a fresh box t)
        self.assertEqual(git(self.host, "rev-parse", "momo/t"), old)   # its work stays
        (self.ws / "a").write_text("new box\n")
        notes = []
        with mock.patch.object(momo_box, "say", notes.append):
            line = self.land(self.snapshot())
        self.assertTrue(line.startswith("momo/t-incoming"), line)
        self.assertEqual(git(self.host, "rev-parse", "momo/t"), old)
        self.assertIn("was not made by this box", notes[0])

    def test_temporary_refs_never_touch_another_boxs_tracking_ref(self):
        self.clone()
        (self.ws / "a").write_text("1")
        self.land(self.snapshot(), name="t-fetch")
        self.land(self.snapshot(), name="src-t")
        before = git(self.host, "for-each-ref", "refs/momo-box")
        (self.ws / "a").write_text("2")
        self.land(self.snapshot(), name="t")                         # uses its fetch ref
        src = momo_box.Source("local", self.host, self.host,
                              git(self.host, "rev-parse", "HEAD"), "main", "host")
        momo_box.write_bundle(src, self.tmp / "in2", False, name="t")   # its bundle ref
        after = git(self.host, "for-each-ref", "refs/momo-box")
        self.assertTrue(set(before.splitlines()) <= set(after.splitlines()), after)

    def test_checked_out_branch_diverts_to_incoming(self):
        self.clone()
        (self.ws / "a").write_text("1")
        self.land(self.snapshot())
        git(self.host, "checkout", "-q", "momo/t")
        (self.ws / "a").write_text("2")
        with mock.patch.object(momo_box, "say"):
            self.assertTrue(self.land(self.snapshot()).startswith("momo/t-incoming"))

    def test_clean_tree_has_nothing_to_land(self):
        self.clone()
        info = self.snapshot()
        self.assertEqual(info["export"], info["base"])
        self.assertIsNone(info["bundle"])

    def test_large_files_stay_out(self):
        self.clone()
        (self.ws / "big.bin").write_bytes(b"x" * 2000)
        (self.ws / "small.txt").write_text("s")
        with mock.patch.object(entrypoint, "MAX_EXPORT_FILE", 1000):
            info = self.snapshot()
        self.assertEqual(info["skipped_large"], ["big.bin"])
        self.assertIn("left out (over 10 MB): big.bin", self.land(info))
        self.assertNotIn("big.bin", git(self.host, "ls-tree", "--name-only", "momo/t"))

    def test_only_the_selected_commit_goes_into_the_box(self):
        git(self.host, "checkout", "-q", "-b", "secret")
        write(self.host, {"secret.txt": "s3cret\n"})
        git(self.host, "add", "-A")
        git(self.host, "commit", "-q", "-m", "secret work")
        secret = git(self.host, "rev-parse", "HEAD")
        git(self.host, "checkout", "-q", "main")
        git(self.host, "tag", "v1", secret)
        main = git(self.host, "rev-parse", "main")
        src = momo_box.Source("local", self.host, self.host, main, "main", "host")
        momo_box.write_bundle(src, self.inbox, False, name="t")
        self.assertEqual(git(self.host, "for-each-ref", "refs/momo-box"), "")    # temp ref gone
        self.assertEqual(git(self.host, "for-each-ref", "refs/momo-box-src"), "")
        self.clone(ref=main, ref_name="main")
        self.assertEqual(git(self.ws, "rev-parse", "HEAD"), main)
        self.assertEqual(git(self.ws, "branch", "--show-current"), "main")
        r = subprocess.run(["git", "cat-file", "-e", secret], cwd=self.ws, capture_output=True)
        self.assertNotEqual(r.returncode, 0)                    # the other branch never arrived
        self.assertEqual(git(self.ws, "tag"), "")
        # …and the work still lands, on top of a base the host has
        (self.ws / "app.py").write_text("print(3)\n")
        self.assertIn("momo/t ← 1 commit", self.land(self.snapshot()))

    def test_all_refs_is_opt_in(self):
        git(self.host, "branch", "other")
        src = momo_box.Source("local", self.host, self.host, git(self.host, "rev-parse", "HEAD"),
                              "main", "host")
        momo_box.write_bundle(src, self.inbox, False, name="t", all_refs=True)
        self.clone(ref=src.sha, ref_name="main")
        self.assertIn("refs/remotes/source/other", git(self.ws, "for-each-ref", "--format=%(refname)"))

    def test_snapshot_keeps_the_stat_cache_and_the_models_index(self):
        self.clone()
        (self.ws / "staged.py").write_text("s = 1\n")
        git(self.ws, "add", "staged.py")                        # the model staged something
        index_before = (self.ws / ".git/index").read_bytes()
        seen = []
        real = entrypoint.git

        def spy(*args, **kw):
            seen.append(args)
            return real(*args, **kw)
        with mock.patch.object(entrypoint, "git", spy):
            info = self.snapshot()
        self.assertNotIn(("read-tree", "HEAD"), seen)           # seeded from the repo's index
        self.assertEqual((self.ws / ".git/index").read_bytes(), index_before)
        self.assertEqual(list((self.ws / ".git").glob("momo-export-*")), [])
        self.assertIn("staged.py", git(self.ws, "ls-tree", "-r", "--name-only", info["export"]))

    def test_snapshot_sees_a_same_size_edit_in_the_same_second(self):
        """An edit of the same size, in the same second as git's last index write,
        snapshotted a second later: the stat cache alone says "unchanged".  The
        temporary index keeps the real one's timestamp so git still checks."""
        for attempt in range(4):
            ws = self.tmp / f"race{attempt}"
            time.sleep(1.02 - time.time() % 1)                       # the start of a second
            entrypoint.prepare_workspace(ws, self.inbox)
            (ws / "app.py").write_text("print(7)\n")                 # same size as print(1)
            index, edited = (ws / ".git/index").stat(), (ws / "app.py").stat()
            if int(index.st_mtime) == int(edited.st_mtime):          # both in that second
                break
        else:
            self.skipTest("could not get a checkout and an edit into one second")
        time.sleep(1.05 - time.time() % 1)                           # …and now it is the next
        seen = {}
        real = entrypoint.git

        def spy(*args, env=None, **kw):
            if args[:2] == ("add", "-A"):
                seen["mtime"] = os.stat(env["GIT_INDEX_FILE"]).st_mtime_ns
            return real(*args, env=env, **kw)
        with mock.patch.object(entrypoint, "git", spy):
            info = entrypoint.snapshot(ws, self.home / "exports")
        self.assertEqual(seen["mtime"], index.st_mtime_ns)           # the copy kept the timestamp
        self.assertEqual(git(ws, "show", f"{info['export']}:app.py"), "print(7)")

    def test_large_staged_or_modified_files_stay_out(self):
        self.clone()
        big = b"x" * (entrypoint.MAX_EXPORT_FILE + 1)
        (self.ws / "new-big.bin").write_bytes(big)
        git(self.ws, "add", "new-big.bin")                      # staged by the model, still left out
        (self.ws / "README.md").write_bytes(big)                # a tracked file grown huge
        (self.ws / "small.txt").write_text("ok\n")
        info = self.snapshot()
        self.assertEqual(sorted(info["skipped_large"]), ["README.md", "new-big.bin"])
        names = git(self.ws, "ls-tree", "-r", "--name-only", info["export"]).splitlines()
        self.assertIn("small.txt", names)
        self.assertNotIn("new-big.bin", names)
        self.assertEqual(git(self.ws, "show", f"{info['export']}:README.md"), "x")   # HEAD's version

    def test_since_sends_only_what_is_new(self):
        self.clone()
        write(self.ws, {"blob.bin": os.urandom(200_000).hex()})
        git(self.ws, "add", "-A")
        git(self.ws, "commit", "-q", "-m", "one")
        first = self.snapshot()
        self.land(first)
        full_size = Path(first["bundle"]).stat().st_size
        (self.ws / "app.py").write_text("print(2)\n")
        second = self.snapshot(since=first["export"])
        self.assertEqual(second["since"], first["export"])
        self.assertLess(Path(second["bundle"]).stat().st_size, full_size // 10)
        self.assertIn("momo/t ← 2 commits", self.land(second))
        self.assertEqual(git(self.host, "show", "momo/t:app.py"), "print(2)")
        # nothing new since: no bundle at all; an unknown commit: everything again
        self.assertIsNone(self.snapshot(since=second["export"])["bundle"])
        again = self.snapshot(since="f" * 40)
        self.assertIsNone(again["since"])
        self.assertGreater(Path(again["bundle"]).stat().st_size, full_size // 2)

    def test_watch_reports_each_turn_once(self):
        import io
        out = io.StringIO()
        entrypoint._write_json(entrypoint.TURN_MARKER, {"turn": 1, "seq": 1})
        entrypoint.watch(interval=0, out=out, rounds=2)
        entrypoint._write_json(entrypoint.TURN_MARKER, {"turn": 2, "seq": 2})
        os.utime(entrypoint.TURN_MARKER, ns=(1, 1))             # a new mtime for sure
        entrypoint.watch(interval=0, out=out, rounds=2)
        self.assertEqual([json.loads(ln)["seq"] for ln in out.getvalue().splitlines() if ln], [1, 2])
        beats = io.StringIO()
        entrypoint.watch(interval=0, heartbeat=0, out=beats, rounds=3)
        self.assertIn("\n\n", beats.getvalue())                 # blank lines keep the pipe checked

    def test_prepare_uses_the_proxy_when_told(self):
        self.clone()
        home = self.tmp / "jvmhome"
        env = {"MOMO_PREPARE": json.dumps(["echo \"$HTTPS_PROXY\" > proxy.txt"]),
               "MOMO_PREPARE_INPUTS": "[]", "MOMO_NET": "pull", "MOMO_PROXY": "http://gw:3128",
               "MOMO_NO_PROXY": '["gw"]'}
        with mock.patch.dict(os.environ, env), mock.patch.object(entrypoint, "WORKSPACE", self.ws), \
                mock.patch.object(entrypoint, "INBOX", self.inbox), \
                mock.patch.object(entrypoint, "HOME", home), mock.patch.object(entrypoint, "log"):
            self.assertEqual(entrypoint.prepare(), 0)
            self.assertEqual((self.ws / "proxy.txt").read_text().strip(), "http://gw:3128")
            self.assertIn("proxyHost=gw", (home / ".gradle/gradle.properties").read_text())
            del os.environ["MOMO_NET"], os.environ["MOMO_PROXY"]          # an open prefetch
            os.environ["MOMO_PREPARE_FORCE"] = "1"
            self.assertEqual(entrypoint.prepare(), 0)
            self.assertEqual((self.ws / "proxy.txt").read_text().strip(), "")
            self.assertNotIn("proxyHost", (home / ".gradle/gradle.properties").read_text())

    # ── the launcher's sync, with the box played by this workspace ──

    def engine(self, running=True):
        """Stands in for docker: `exec … export` snapshots the workspace, cp_out
        hands out what the box wrote."""
        case, exports = self, self.home / "exports"

        class Fake:
            dry_run = False
            calls: list = []

            def running(self, _c):
                return running

            def run(self, *args, **kw):
                self.calls.append(args)
                if args[0] == "exec" and "export" in args:
                    case.snapshot(since=args[args.index("--since") + 1] if "--since" in args else None)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            def cp_out(self, _c, path, dest=None, limit=0):
                f = exports / Path(path).name
                if not f.is_file():
                    return None
                if dest is None:
                    return f.read_bytes()
                shutil.copyfile(f, dest)
                return True
        cache = mock.patch.object(momo_box, "cache_dir", lambda: self.tmp / "cache")
        cache.start()
        self.addCleanup(cache.stop)
        momo_box.save_meta({"name": "t", "land": str(self.host), "landed_export": None})
        return Fake()

    def test_sync_sends_only_new_work_after_the_first_landing(self):
        self.clone()
        eng = self.engine()
        (self.ws / "app.py").write_text("print(2)\n")
        self.assertIn("momo/t ← 1 commit", momo_box.sync_once(eng, "t"))
        self.assertNotIn("--since", eng.calls[-1])                  # nothing landed before
        first = git(self.host, "rev-parse", "momo/t")
        self.assertIsNone(momo_box.sync_once(eng, "t"))             # unchanged: nothing to do
        (self.ws / "app.py").write_text("print(3)\n")
        self.assertIn("momo/t ←", momo_box.sync_once(eng, "t"))
        self.assertEqual(eng.calls[-1][-2:], ("--since", first))
        self.assertEqual(git(self.host, "show", "momo/t:app.py"), "print(3)")
        self.assertEqual(oct((self.tmp / "cache/boxes/t").stat().st_mode & 0o777), "0o700")

    def test_sync_falls_back_to_a_full_bundle(self):
        self.clone()
        eng = self.engine()
        (self.ws / "app.py").write_text("print(2)\n")
        momo_box.sync_once(eng, "t")
        (self.ws / "app.py").write_text("print(3)\n")
        real, failed = momo_box.land, []

        def flaky(repo, bundle, name, info, out=None):
            if info.get("since") and not failed:
                failed.append(1)
                raise momo_box.BoxError("missing objects")
            return real(repo, bundle, name, info, out)
        with mock.patch.object(momo_box, "land", flaky):
            self.assertIn("momo/t ←", momo_box.sync_once(eng, "t"))
        self.assertNotIn("--since", eng.calls[-1])                  # asked again, for everything
        self.assertEqual(git(self.host, "show", "momo/t:app.py"), "print(3)")

    def test_sync_refuses_a_forged_record(self):
        self.clone()
        eng = self.engine(running=False)
        exports = self.home / "exports"
        exports.mkdir(parents=True)
        for forged in ("{not json", "[]", json.dumps({"export": "--upload-pack=x", "bundle": 1})):
            (exports / "latest.json").write_text(forged)
            with self.assertRaisesRegex(momo_box.BoxError, "unreadable"):
                momo_box.sync_once(eng, "t")
        self.assertEqual(git(self.host, "for-each-ref", "refs/heads/momo"), "")

    def test_landing_checks_the_objects(self):
        self.clone()
        (self.ws / "a.txt").write_text("1")
        info = self.snapshot()
        seen = []
        real = momo_box._git

        def spy(repo, *args, **kw):
            seen.append(args)
            return real(repo, *args, **kw)
        with mock.patch.object(momo_box, "_git", spy):
            self.land(info)
        fetch = next(a for a in seen if "fetch" in a)
        self.assertEqual(fetch[:2], ("-c", "fetch.fsckObjects=true"))

    def test_uncommitted_host_changes_get_their_own_commit(self):
        (self.host / "app.py").write_text("print('dirty')\n")
        (self.host / "untracked.txt").write_text("u\n")
        (self.inbox / "dirty.patch").write_bytes(momo_box.uncommitted_patch(self.host))
        self.assertIn("uncommitted", self.clone())
        self.assertEqual(git(self.ws, "log", "-1", "--format=%s"),
                         "momo-box: uncommitted changes from the host")
        self.assertTrue((self.ws / "untracked.txt").is_file())
        self.assertEqual(git(self.host, "status", "--porcelain").count("\n"), 1)  # host untouched

    def test_empty_workspace_lands_in_a_new_repo(self):
        (self.inbox / "source.bundle").unlink()
        self.assertEqual(self.clone(), "empty repository")
        (self.ws / "hello.txt").write_text("hi")
        info = self.snapshot()
        target = self.tmp / "landing"
        momo_box.land(target, Path(info["bundle"]), "e", info)
        self.assertEqual(git(target, "show", "momo/e:hello.txt"), "hi")

    def test_turn_number_in_the_commit(self):
        self.clone()
        entrypoint._write_json(entrypoint.TURN_MARKER, {"turn": 4, "seq": 1})
        (self.ws / "a").write_text("1")
        info = self.snapshot()
        self.assertEqual(info["turn"], 4)
        self.assertIn("(turn 4)", git(self.ws, "log", "-1", "--format=%s", info["export"]))

    def test_prepare_best_effort_commands(self):
        self.clone()
        env = {"MOMO_PREPARE": json.dumps(["-exit 3", "echo ok > done.txt"]),
               "MOMO_PREPARE_INPUTS": "[]"}
        with mock.patch.dict(os.environ, env), mock.patch.object(entrypoint, "WORKSPACE", self.ws), \
                mock.patch.object(entrypoint, "INBOX", self.inbox), mock.patch.object(entrypoint, "log"), \
                mock.patch.object(entrypoint, "HOME", self.tmp / "jvmhome"):
            self.assertEqual(entrypoint.prepare(), 0)
            self.assertTrue((self.ws / "done.txt").is_file())
            os.environ["MOMO_PREPARE"] = json.dumps(["exit 3"])
            self.assertEqual(entrypoint.prepare(), 3)

    def test_prepare_hash_follows_its_inputs(self):
        self.clone()
        write(self.ws, {"pom.xml": "<a/>", "sub/pom.xml": "<b/>"})
        h1 = entrypoint.prepare_hash(["mvn x"], ["pom.xml", "**/pom.xml"], self.ws)
        self.assertEqual(h1, entrypoint.prepare_hash(["mvn x"], ["pom.xml", "**/pom.xml"], self.ws))
        (self.ws / "sub/pom.xml").write_text("<c/>")
        self.assertNotEqual(h1, entrypoint.prepare_hash(["mvn x"], ["**/pom.xml"], self.ws))
        self.assertNotEqual(h1, entrypoint.prepare_hash(["mvn y"], ["pom.xml", "**/pom.xml"], self.ws))


class EntrypointArgs(unittest.TestCase):
    def test_web_argv(self):
        env = {"MOMO_UI": "web", "MOMO_PROVIDER": "llamacpp", "MOMO_HOST": "http://gw:9000",
               "MOMO_WEB_TOKEN": "tok", "MOMO_WEB_PUBLIC_URL": "http://127.0.0.1:9/",
               "MOMO_NOTES": json.dumps(["note one"]), "MOMO_NET": "locked"}
        argv = entrypoint.momo_argv(env, ["--net", "on", "--mode", "coding"])
        self.assertIn("--web-only", argv)
        self.assertIn("--web-token=tok", argv)
        self.assertIn("--environment-note=note one", argv)
        self.assertEqual(argv[-2:], ["--net", "off"])       # a locked box can't turn fetch_url on
        self.assertTrue(any(a.startswith("--turn-marker=") for a in argv))

    def test_prompt_argv(self):
        argv = entrypoint.momo_argv({"MOMO_UI": "prompt", "MOMO_PROMPT_STDIN": "1",
                                     "MOMO_NET": "open"}, [])
        self.assertIn("--prompt=-", argv)

    def test_index_runs_in_momos_process(self):
        argv = entrypoint.momo_argv({"MOMO_UI": "web"}, ["--index-workers", "4"])
        self.assertIn("--index-workers=1", argv)
        self.assertLess(argv.index("--index-workers=1"), argv.index("--index-workers"))  # yours win
        self.assertFalse(any(a.startswith("--index-max-mem") for a in argv))
        low = entrypoint.momo_argv({"MOMO_UI": "web", "MOMO_LOW_MEM": "1"}, [])
        self.assertIn(f"--index-max-mem={entrypoint.LOW_MEM_INDEX}", low)

    def test_low_mem_gradle_block(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            entrypoint.configure_jvm_proxy("http://gw:3128", ["gw"], home)
            entrypoint.configure_low_mem(True, home)
            entrypoint.configure_low_mem(True, home)                         # idempotent
            text = (home / ".gradle/gradle.properties").read_text()
            self.assertEqual(text.count("org.gradle.daemon=false"), 1)
            self.assertIn("systemProp.https.proxyHost=gw", text)              # the proxy stays
            entrypoint.configure_low_mem(False, home)
            text = (home / ".gradle/gradle.properties").read_text()
            self.assertNotIn("daemon", text)
            self.assertIn("systemProp.https.proxyHost=gw", text)

    def test_helpers_need_only_the_stdlib(self):
        """The helpers run with python -S (no site-packages) and keep urllib out of the
        long-lived ones: momo's supervisor and the syncer's watch."""
        code = ("import sys; sys.path.insert(0, %r); import entrypoint, forward; "
                "print('urllib.request' in sys.modules)") % str(REPO / "container")
        r = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False")

    def test_values_that_start_with_a_dash_reach_momo(self):
        """A token from token_urlsafe starts with "-" 1 time in 64: momo must still
        parse it.  An unknown flag at the end makes momo's real parser stop with
        that complaint (and start nothing) once everything before it parsed."""
        env = {"MOMO_UI": "web", "MOMO_WEB_TOKEN": "-Ab3xYz", "MOMO_HOST": "http://gw:9000",
               "MOMO_NOTES": json.dumps(["-starts with a dash"]), "MOMO_NET": "open"}
        argv = entrypoint.momo_argv(env, ["--not-a-momo-flag"])[2:]
        r = subprocess.run([sys.executable, str(REPO / "momo-coding-harness.py"), *argv],
                           capture_output=True, text=True, cwd=REPO, timeout=60,
                           stdin=subprocess.DEVNULL)
        self.assertNotIn("expected one argument", r.stderr)
        self.assertIn("unrecognized arguments: --not-a-momo-flag", r.stderr)
        argv = entrypoint.momo_argv({"MOMO_UI": "web", "MOMO_WEB_TOKEN": "-x"}, [])
        self.assertIn("--web-token=-x", argv)

    def test_child_env_drops_launcher_settings(self):
        env = entrypoint.child_env({"MOMO_WEB_TOKEN": "secret", "PATH": "/bin", "MOMO_NET": "pull",
                                    "MOMO_PROXY": "http://gw:3128", "MOMO_NO_PROXY": '["gw"]',
                                    "MOMO_BOX_ENV": '{"MAVEN_OPTS": "-Xmx1g"}'})
        self.assertNotIn("MOMO_WEB_TOKEN", env)
        self.assertEqual(env["HTTPS_PROXY"], "http://gw:3128")
        self.assertIn("gw", env["NO_PROXY"])
        self.assertIn("-Dhttps.proxyHost=gw", env["JAVA_TOOL_OPTIONS"])
        self.assertEqual(env["MAVEN_OPTS"], "-Xmx1g")
        self.assertNotIn("HTTPS_PROXY", entrypoint.child_env({"MOMO_NET": "locked"}))

    def test_jvm_proxy_files(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            write(home, {".m2/settings.xml": "<settings>\n  <proxies>\n  </proxies>\n</settings>\n",
                         ".gradle/gradle.properties": "org.gradle.jvmargs=-Xmx2g\n"})
            entrypoint.configure_jvm_proxy("http://gw:3128", ["gw"], home)
            entrypoint.configure_jvm_proxy("http://gw:3128", ["gw"], home)   # idempotent
            sx = (home / ".m2/settings.xml").read_text()
            self.assertEqual(sx.count("<id>momo-box-https</id>"), 1)
            self.assertEqual(sx.count("<proxies>"), 1)
            gp = (home / ".gradle/gradle.properties").read_text()
            self.assertIn("org.gradle.jvmargs=-Xmx2g", gp)
            self.assertEqual(gp.count("systemProp.https.proxyHost=gw"), 1)
            entrypoint.configure_jvm_proxy(None, [], home)                    # locked again
            self.assertNotIn("momo-box", (home / ".m2/settings.xml").read_text())
            self.assertEqual((home / ".gradle/gradle.properties").read_text(),
                             "org.gradle.jvmargs=-Xmx2g\n")

    def test_new_settings_xml(self):
        with tempfile.TemporaryDirectory() as d:
            entrypoint.configure_jvm_proxy("http://gw:3128", [], Path(d))
            self.assertIn("<host>gw</host><port>3128</port>",
                          (Path(d) / ".m2/settings.xml").read_text())


# ── gateway ──────────────────────────────────────────────────────────────────

def _echo_server() -> tuple[socket.socket, int]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return

            def handle(c=c):
                with c:
                    while (data := c.recv(4096)):
                        c.sendall(b"echo:" + data)
            threading.Thread(target=handle, daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1]


def _http_server() -> tuple[socket.socket, int, list]:
    """Answers one HTTP request per connection; records the request heads."""
    seen = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                buf = b""
                while b"\r\n\r\n" not in buf and (chunk := c.recv(4096)):
                    buf += chunk
                seen.append(buf.decode())
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1], seen


def _recv_all(s: socket.socket) -> bytes:
    out = b""
    s.settimeout(5)
    while (chunk := s.recv(4096)):
        out += chunk
    return out


class Gateway(unittest.TestCase):
    def listen(self, handler) -> int:
        srv = forward.serve(0, handler, host="127.0.0.1")
        self.addCleanup(srv.close)
        return srv.getsockname()[1]

    @staticmethod
    def _dns_reply(query: bytes, records: list[tuple[int, bytes]], rcode: int = 0) -> bytes:
        """A reply to `query` with (type, rdata) answers, each owner a pointer to the question."""
        head = query[:2] + (0x8180 | rcode).to_bytes(2, "big") + b"\x00\x01" \
            + len(records).to_bytes(2, "big") + b"\x00\x00\x00\x00"
        body = query[12:]
        for rtype, rdata in records:
            body += b"\xc0\x0c" + rtype.to_bytes(2, "big") + b"\x00\x01\x00\x00\x00\x3c" \
                + len(rdata).to_bytes(2, "big") + rdata
        return head + body

    def test_dns_reply_parsing(self):
        q = forward._dns_query_packet("example.com", 1, 0xBEEF)
        self.assertEqual(q[:2], b"\xbe\xef")
        cname = (5, b"\x03www\xc0\x0c")
        ok = self._dns_reply(q, [cname, (1, bytes([93, 184, 215, 14])), (28, bytes(16))])
        self.assertEqual(forward._dns_answers(ok, 0xBEEF, 1), ["93.184.215.14"])
        self.assertEqual(forward._dns_answers(ok, 0xBEEF, 28), ["::"])
        self.assertEqual(forward._dns_answers(ok, 0x1111, 1), [])               # wrong id
        self.assertEqual(forward._dns_answers(self._dns_reply(q, [], rcode=3), 0xBEEF, 1), [])
        self.assertEqual(forward._dns_answers(ok[:30], 0xBEEF, 1), [])          # truncated
        with self.assertRaises(ValueError):
            forward._dns_query_packet("a..b", 1, 1)

    def test_dns_lookup_over_udp(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        self.addCleanup(srv.close)

        def answer():
            data, addr = srv.recvfrom(512)
            srv.sendto(b"\x00\x00junk", addr)                                  # someone else's
            srv.sendto(self._dns_reply(data, [(1, bytes([10, 0, 0, 7]))]), addr)
        threading.Thread(target=answer, daemon=True).start()
        self.assertEqual(forward._dns_lookup("127.0.0.1", "registry.npmjs.org", 1, timeout=3,
                                             port=srv.getsockname()[1]), ["10.0.0.7"])

    def test_outside_names_skip_the_box_networks_dns(self):
        with tempfile.TemporaryDirectory() as d:
            rc = Path(d) / "resolv.conf"
            rc.write_text("search home\nnameserver 10.89.0.1\nnameserver 192.168.127.1\n")
            self.assertEqual(forward._resolv_servers(str(rc)), ["10.89.0.1", "192.168.127.1"])
        asked = []

        def lookup(server, name, qtype, **kw):
            asked.append((server, qtype))
            return ["93.184.215.14"] if qtype == 1 else []
        servers = ["10.89.0.1", "192.168.127.1"]
        with mock.patch.object(forward, "_dns_lookup", lookup):
            self.assertEqual(forward._fallback_addrs("example.com", servers), ["93.184.215.14"])
            self.assertEqual(asked, [("192.168.127.1", 1), ("192.168.127.1", 28)])
            for host in ("momo", "gw", "10.0.0.1", "[::1]"):                   # never asked out
                self.assertEqual(forward._fallback_addrs(host, servers), [])
            self.assertEqual(forward._fallback_addrs("example.com", ["127.0.0.11"]), [])
        echo, port = _echo_server()
        self.addCleanup(echo.close)
        nxdomain = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        real = socket.create_connection
        with mock.patch.object(forward.socket, "create_connection",
                               side_effect=lambda a, **kw: (_ for _ in ()).throw(nxdomain)
                               if a[0] == "example.com" else real(a, **kw)), \
                mock.patch.object(forward, "_fallback_addrs", return_value=["127.0.0.1"]):
            forward._connect("example.com", port).close()
        with mock.patch.object(forward.socket, "create_connection", side_effect=nxdomain), \
                mock.patch.object(forward, "_fallback_addrs", return_value=[]):
            with self.assertRaises(socket.gaierror):
                forward._connect("nowhere.example", port)

    def test_forwarder_relays_and_half_closes(self):
        echo, port = _echo_server()
        self.addCleanup(echo.close)
        gw = self.listen(forward.forwarder("127.0.0.1", port))
        with socket.create_connection(("127.0.0.1", gw)) as s:
            s.sendall(b"hi")
            s.shutdown(socket.SHUT_WR)
            self.assertEqual(_recv_all(s), b"echo:hi")

    def test_allowlist_matching(self):
        a = forward.Allowlist(None, ["repo1.maven.org", ".gradle.org", "*.npmjs.org", "10.0.0.1"])
        self.assertTrue(a.allows("REPO1.maven.org."))
        self.assertTrue(a.allows("plugins.gradle.org"))
        self.assertTrue(a.allows("gradle.org"))
        self.assertTrue(a.allows("registry.npmjs.org"))
        self.assertFalse(a.allows("maven.org"))
        self.assertFalse(a.allows("evilgradle.org"))
        self.assertTrue(a.allows("10.0.0.1"))
        self.assertFalse(a.allows("10.0.0.2"))

    def test_allow_file_reloads(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "allow.txt"
            f.write_text("a.org  # comment\n")
            a = forward.Allowlist(f)
            self.assertTrue(a.allows("a.org"))
            self.assertFalse(a.allows("b.org"))
            time.sleep(0.01)
            f.write_text("a.org\nb.org\n")
            os.utime(f, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
            self.assertTrue(a.allows("b.org"))

    def proxy(self, hosts, log=None) -> int:
        p = forward.Proxy(forward.Allowlist(None, hosts), log, box="t")
        return self.listen(p.handle)

    def test_connect_allowed_and_denied(self):
        echo, port = _echo_server()
        self.addCleanup(echo.close)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(forward, "_CONNECT_PORTS", {port}):
            log = Path(d) / "net.log"
            px = self.proxy(["127.0.0.1"], log)
            with socket.create_connection(("127.0.0.1", px)) as s:
                s.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\nping".encode())
                s.settimeout(5)
                got = b""
                while b"echo:ping" not in got:
                    got += s.recv(4096)
                self.assertTrue(got.startswith(b"HTTP/1.1 200"))
            with socket.create_connection(("127.0.0.1", px)) as s:
                s.sendall(f"CONNECT example.com:{port} HTTP/1.1\r\n\r\n".encode())
                got = _recv_all(s)
            self.assertIn(b"403 Forbidden", got)
            self.assertIn(b"momo-box allow t example.com", got)
            entries = [json.loads(ln) for ln in log.read_text().splitlines()]
            self.assertEqual([(e["host"], e["allowed"]) for e in entries],
                             [("127.0.0.1", True), ("example.com", False)])

    def test_connect_only_to_443(self):
        px = self.proxy(["127.0.0.1"])
        with socket.create_connection(("127.0.0.1", px)) as s:
            s.sendall(b"CONNECT 127.0.0.1:22 HTTP/1.1\r\n\r\n")
            self.assertIn(b"only port 443", _recv_all(s))

    def test_plain_http_get_only(self):
        web, port, seen = _http_server()
        self.addCleanup(web.close)
        with mock.patch.object(forward, "_HTTP_PORTS", {port}):
            px = self.proxy(["localhost"])
            with socket.create_connection(("127.0.0.1", px)) as s:
                s.sendall(f"GET http://localhost:{port}/a/b?c=1 HTTP/1.1\r\nHost: localhost\r\n"
                          "Proxy-Authorization: x\r\nConnection: keep-alive\r\n\r\n".encode())
                got = _recv_all(s)
            self.assertTrue(got.startswith(b"HTTP/1.1 200") and got.endswith(b"ok"))
            self.assertTrue(seen[0].startswith("GET /a/b?c=1 HTTP/1.1"))
            self.assertNotIn("Proxy-Authorization", seen[0])
            self.assertIn("Connection: close", seen[0])
            with socket.create_connection(("127.0.0.1", px)) as s:
                s.sendall(f"POST http://localhost:{port}/ HTTP/1.1\r\nContent-Length: 1\r\n\r\nx"
                          .encode())
                self.assertIn(b"405", _recv_all(s))
        self.assertEqual(len(seen), 1)

    def test_parse_args(self):
        o = forward.parse_args(["9000=host.docker.internal:9931", "8765=momo:8765",
                                "--proxy", "3128", "--allow-file", "/gw/a"])
        self.assertEqual(o["maps"], [(9000, "host.docker.internal", 9931), (8765, "momo", 8765)])
        self.assertEqual(o["proxy"], 3128)
        f = forward.parse_args(["--filter", "9000=llm:11434", "--paths", "/api/chat,/api/tags"])
        self.assertEqual(f["filters"], [(9000, "llm", 11434)])
        self.assertEqual(f["paths"], ["/api/chat", "/api/tags"])
        with self.assertRaises(SystemExit):
            forward.parse_args(["--filter", "9000=llm:11434"])        # no paths: refuse to run

    # ── the model server, inference only ──

    def llm(self, paths=("/api/chat", "/api/tags")):
        """(gateway port, what the fake model server saw) — it streams two lines."""
        seen = []
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        self.addCleanup(srv.close)

        def loop():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                with c:
                    buf = b""
                    while b"\r\n\r\n" not in buf and (chunk := c.recv(4096)):
                        buf += chunk
                    head, _, body = buf.partition(b"\r\n\r\n")
                    length = 0
                    for ln in head.decode().split("\r\n")[1:]:
                        if ln.lower().startswith("content-length:"):
                            length = int(ln.split(":", 1)[1])
                    while len(body) < length and (chunk := c.recv(4096)):
                        body += chunk
                    c.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\ndata: 1\n")
                    time.sleep(0.05)
                    c.sendall(b"data: 2\n")
                    c.settimeout(0.3)
                    try:
                        extra = c.recv(4096)        # anything smuggled after the request
                    except OSError:
                        extra = b""
                    seen.append((head.decode(), body, extra))
        threading.Thread(target=loop, daemon=True).start()
        port = srv.getsockname()[1]
        return self.listen(forward.LlmFilter("127.0.0.1", port, list(paths)).handle), seen, port

    def ask(self, gw: int, raw: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", gw)) as s:
            s.sendall(raw)
            return _recv_all(s)

    def test_filter_lets_inference_through(self):
        gw, seen, port = self.llm()
        body = b'{"model":"m","stream":true}'
        out = self.ask(gw, b"POST /api/chat HTTP/1.1\r\nHost: gw:9000\r\nConnection: keep-alive\r\n"
                           b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body)
                           + body)
        self.assertTrue(out.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(out.endswith(b"data: 1\ndata: 2\n"))              # the stream arrives whole
        head, got, extra = seen[0]
        self.assertEqual(got, body)
        self.assertIn(f"Host: 127.0.0.1:{port}", head)                   # the server's own name
        self.assertNotIn("gw:9000", head)
        self.assertIn("Connection: close", head)
        self.assertNotIn("keep-alive", head)
        self.assertIn("Content-Type: application/json", head)
        self.assertEqual(extra, b"")
        self.assertIn(b"200 OK", self.ask(gw, b"GET /api/tags?x=1 HTTP/1.1\r\nHost: gw\r\n\r\n"))

    def test_filter_refuses_the_rest_of_the_api(self):
        gw, seen, _ = self.llm()
        for request in (b"DELETE /api/delete HTTP/1.1\r\nHost: gw\r\nContent-Length: 2\r\n\r\n{}",
                        b"POST /api/pull HTTP/1.1\r\nHost: gw\r\nContent-Length: 2\r\n\r\n{}",
                        b"POST /api/chat/../push HTTP/1.1\r\nHost: gw\r\n\r\n",
                        b"PUT /api/chat HTTP/1.1\r\nHost: gw\r\n\r\n",
                        b"GET http://other/api/tags HTTP/1.1\r\nHost: gw\r\n\r\n"):
            out = self.ask(gw, request)
            self.assertTrue(out.startswith(b"HTTP/1.1 403"), out[:40])
        self.assertIn(b"411", self.ask(gw, b"POST /api/chat HTTP/1.1\r\nHost: gw\r\n"
                                           b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"))
        self.assertEqual(seen, [])                                       # none reached the server

    def test_filter_drops_a_second_request_on_the_connection(self):
        gw, seen, _ = self.llm()
        out = self.ask(gw, b"POST /api/chat HTTP/1.1\r\nHost: gw\r\nContent-Length: 2\r\n\r\n{}"
                           b"DELETE /api/delete HTTP/1.1\r\nHost: gw\r\nContent-Length: 2\r\n\r\n{}")
        self.assertEqual(out.count(b"HTTP/1.1"), 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], b"{}")
        self.assertEqual(seen[0][2], b"")                                # the DELETE went nowhere

    def test_proxy_survives_malformed_requests(self):
        px = self.listen(forward.Proxy(forward.Allowlist(None, [".pythonhosted.org"])).handle)
        for request in (b"CONNECT [::1]:x HTTP/1.1\r\n\r\n", b"CONNECT [::1 HTTP/1.1\r\n\r\n",
                        b"CONNECT evil.com@files.pythonhosted.org:443 HTTP/1.1\r\n\r\n",
                        b"CONNECT a b c d\r\n\r\n"):
            out = self.ask(px, request)                                  # answered and closed
            self.assertRegex(out[:12], rb"HTTP/1.1 4\d\d", request)

    def test_net_log_is_rotated(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "net.log"
            log.write_bytes(b"x" * (forward._LOG_MAX + 1))
            forward.Proxy(forward.Allowlist(None, []), log).log(host="a", allowed=False)
            self.assertEqual((Path(d) / "net.log.1").stat().st_size, forward._LOG_MAX + 1)
            self.assertEqual(len(log.read_text().splitlines()), 1)


# ── launcher ─────────────────────────────────────────────────────────────────

def box(**kw) -> momo_box.Box:
    base = dict(name="t", ui="web", net="open", image="img:1", gw_image="gw:1",
                runtime="docker", llm="http://localhost:9931", provider="llamacpp",
                token="tok", system="Darwin", inbox=Path("/cache/in"))
    base.update(kw)
    return momo_box.Box(**base)


def _env(argv: list[str]) -> dict[str, str]:
    """-e NAME=VALUE pairs; a bare -e NAME (its value travels in the environment) → None."""
    pairs = [argv[i + 1].partition("=") for i, a in enumerate(argv) if a == "-e"]
    return {k: (v if sep else None) for k, sep, v in pairs}


class ExistingBox(TmpDir):
    """--name of an existing box with another --repo: its workspace holds the old
    repo, so the new repo's prepare (./mvnw) fails and work would land in the wrong repo."""

    def check(self, meta: dict, src) -> None:
        with mock.patch.object(momo_box, "load_meta", return_value=meta):
            momo_box.check_same_source("fix-auth", src)

    def test_other_repo_is_refused(self):
        a = self.repo(self.tmp / "a", {"README.md": "a"})
        b = self.repo(self.tmp / "b", {"pom.xml": "<project/>", "mvnw": "#!/bin/sh"})
        meta = {"name": "fix-auth", "source": str(a), "source_kind": "local"}
        src_b = momo_box.resolve_source(str(b), None, False, None, "fix-auth")
        with self.assertRaises(momo_box.BoxError) as e:
            self.check(meta, src_b)
        self.assertIn(str(a), str(e.exception))
        self.assertIn("momo-box start fix-auth", str(e.exception))
        # The same repo, also from a subfolder, is fine.
        (a / "sub").mkdir()
        self.check(meta, momo_box.resolve_source(str(a), None, False, None, "fix-auth"))
        self.check(meta, momo_box.resolve_source(str(a / "sub"), None, False, None, "fix-auth"))
        # --empty vs. a repo, and a box with no metadata (compose) or none at all.
        with self.assertRaises(momo_box.BoxError):
            self.check({"source_kind": "empty"}, src_b)
        self.check({"source_kind": "compose", "source": "compose"}, src_b)
        # A workspace volume with no record: which repo it holds is unknown.
        with mock.patch.object(momo_box, "load_meta", side_effect=momo_box.BoxError("none")):
            with self.assertRaisesRegex(momo_box.BoxError, "no record of box fix-auth"):
                momo_box.check_same_source("fix-auth", src_b)

    def test_a_kept_box_uses_the_commit_it_was_made_from(self):
        a = self.repo(self.tmp / "a", {"pom.xml": "<project/>"})
        made = git(a, "rev-parse", "HEAD")
        write(a, {"package.json": "{}"})               # the host moved on: now also node
        git(a, "add", "-A")
        git(a, "commit", "-q", "-m", "node")
        meta = {"name": "x", "source": str(a), "source_kind": "local", "sha": made,
                "ref_name": "main"}
        src = momo_box.resolve_source(str(a), None, False, None, "x")
        with mock.patch.object(momo_box, "say"):
            momo_box.pin_to_saved(src, meta, SimpleNamespace(ref=None))
        self.assertEqual(src.sha, made)
        self.assertNotIn("package.json", momo_box._tree_for(src).files())
        # --ref that is another commit only applies to a new box
        src = momo_box.resolve_source(str(a), "HEAD", False, None, "x")
        with self.assertRaisesRegex(momo_box.BoxError, "only applies to a new box"):
            momo_box.pin_to_saved(src, meta, SimpleNamespace(ref="HEAD"))
        # a record from before the commit was saved: as the source is now
        src = momo_box.resolve_source(str(a), None, False, None, "x")
        momo_box.pin_to_saved(src, {"name": "x"}, SimpleNamespace(ref=None))
        self.assertNotEqual(src.sha, made)
        # the commit is gone from the repo (gc'd): a note, and HEAD
        src = momo_box.resolve_source(str(a), None, False, None, "x")
        notes = []
        with mock.patch.object(momo_box, "say", notes.append):
            momo_box.pin_to_saved(src, dict(meta, sha="f" * 40), SimpleNamespace(ref=None))
        self.assertNotEqual(src.sha, "f" * 40)
        self.assertIn("is gone", notes[0])

    def test_a_running_box_is_not_set_up_again(self):
        a = self.repo(self.tmp / "a", {"README.md": "a"})
        eng = SimpleNamespace(ok=lambda *a: True, running=lambda c: c == "momo-x", rt="docker")
        args = SimpleNamespace(name="x", repo=str(a), ref=None, empty=False, land_repo=None)
        with mock.patch.object(momo_box, "_engine", lambda a: eng), \
                mock.patch.object(momo_box, "check_same_source") as check:
            with self.assertRaisesRegex(momo_box.BoxError, "box x is running"):
                momo_box.setup_box(args, "prompt")       # run --name x on a live web box
        check.assert_not_called()

    def test_the_record_keeps_what_a_kept_box_was_given(self):
        land = self.repo(self.tmp / "land", {"README.md": "l"})
        src = momo_box.Source("local", land, land, "a" * 40, "main", str(land))
        args = SimpleNamespace(net="pull", land_repo="../land", repo=str(land), ref=None,
                               preset="gradle", prompt="go", name="x")
        prior = {"created": 1.0, "allow_added": ["+a.org  # momo-box allow"], "ui": "web",
                 "args": {"net": "pull", "port": 9001}}
        meta = momo_box._meta_for(box(ui="prompt", token=None, name="x", prior=prior), src,
                                  False, args=args)
        self.assertEqual((meta["sha"], meta["ref_name"]), ("a" * 40, "main"))
        self.assertEqual(meta["created"], 1.0)
        self.assertEqual(meta["allow_added"], prior["allow_added"])
        self.assertEqual((meta["ui"], meta["args"]), ("web", prior["args"]))   # start still works
        fresh = momo_box._meta_for(box(ui="prompt", token=None, name="x"), src, False, args=args)
        self.assertEqual((fresh["ui"], fresh["args"]["preset"]), ("prompt", "gradle"))
        self.assertEqual(fresh["args"]["land_repo"], str(land))      # absolute, not ../land
        self.assertNotIn("allow_added", fresh)

    def test_a_new_session_starts_with_a_clean_gateway(self):
        status = self.tmp / "status"
        write(status, {"net.json": "{}", "stats.json": "{}", "sync.json": "{}"})
        calls = []
        eng = SimpleNamespace(dry_run=False, exists=lambda c: True,
                              run=lambda *a, **kw: calls.append(a))
        momo_box._fresh_gateway(eng, box(net="pull", name="x", status=status))
        self.assertIn(("rm", "-fv", "momo-x-gw"), calls)
        self.assertIn(("volume", "rm", "-f", "momo-x-gw"), calls)        # its net.log
        self.assertEqual(sorted(p.name for p in status.iterdir()), ["sync.json"])

    def test_syncer_pid_must_be_the_syncer(self):
        argv = [sys.executable, "-c", "import time; time.sleep(30)",
                str(self.tmp / "container" / "momo_box.py"), "sync", "x", "--follow"]
        p = subprocess.Popen(argv)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        time.sleep(0.2)
        self.assertTrue(momo_box._is_syncer(p.pid, "x"))
        self.assertFalse(momo_box._is_syncer(p.pid, "y"))
        self.assertFalse(momo_box._is_syncer(os.getpid(), "x"))      # a reused pid
        with mock.patch.object(momo_box, "box_dir", lambda n: self.tmp):
            (self.tmp / "sync.pid").write_text(str(os.getpid()))
            self.assertIsNone(momo_box._syncer_pid("x"))

    def test_prepare_uses_the_boxs_saved_toolchain(self):
        meta = {"name": "x", "source": "/src", "source_kind": "local", "sha": "a" * 40,
                "image": "img", "net": "open",
                "args": {"preset": "gradle", "base": None, "packages": None, "full_base": True,
                         "ref": "old"}}
        seen = {}
        eng = SimpleNamespace(running=lambda c: False, dry_run=False, rt="docker")

        def resolve(args, tree):
            seen["flags"] = (args.preset, args.full_base)
            return detect.Resolved(source="preset", base="debian")
        args = SimpleNamespace(name="x", runtime=None, preset=None, base=None, packages=None,
                               full_base=False, yes=True, prepare_net=None, low_mem=None)
        with mock.patch.object(momo_box, "_engine", lambda a: eng), \
                mock.patch.object(momo_box, "load_meta", lambda n: meta), \
                mock.patch.object(momo_box, "resolve_source",
                                  lambda repo, ref, *a: seen.setdefault("ref", ref) and None
                                  or SimpleNamespace(kind="local", sha="b" * 40)), \
                mock.patch.object(momo_box, "pin_to_saved",
                                  lambda src, m, a: seen.setdefault("pinned", m["sha"])), \
                mock.patch.object(momo_box, "_tree_for", lambda src: None), \
                mock.patch.object(momo_box, "_resolve_toolchain", resolve), \
                mock.patch.object(momo_box, "check_approval"), \
                mock.patch.object(momo_box, "say"):
            self.assertEqual(momo_box.cmd_prepare(args), 0)
        self.assertEqual(seen, {"flags": ("gradle", True), "ref": None, "pinned": "a" * 40})

    def test_start_uses_the_saved_commit_not_the_saved_ref(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(momo_box, "cache_dir", lambda: Path(d)):
            momo_box.save_meta({"name": "w", "ui": "web", "sha": "a" * 40,
                                "args": {"repo": "/src", "ref": "feature",
                                         "with_uncommitted": True, "all_refs": True}})
            seen = []
            with mock.patch.object(momo_box, "cmd_web", lambda ns: seen.append(ns) or 0):
                momo_box.cmd_start(SimpleNamespace(name="w", runtime=None, yes=False))
            self.assertEqual((seen[0].ref, seen[0].with_uncommitted, seen[0].all_refs),
                             (None, False, False))


class Launcher(unittest.TestCase):
    def test_low_mem(self):
        class Eng:
            rt, dry_run = "docker", False

            def __init__(self, total):
                self.total = total

            def run(self, *args, **kw):
                return SimpleNamespace(returncode=0, stdout=str(self.total).encode(), stderr=b"")
        with mock.patch.object(momo_box, "say"):
            self.assertTrue(momo_box.resolve_low_mem(Eng(2_525_360_128), None))     # 2.4 GB
            self.assertFalse(momo_box.resolve_low_mem(Eng(16 * 1024 ** 3), None))
            self.assertFalse(momo_box.resolve_low_mem(Eng(2_525_360_128), False))  # --no-low-mem
            self.assertTrue(momo_box.resolve_low_mem(Eng(16 * 1024 ** 3), True))
            self.assertFalse(momo_box.resolve_low_mem(Eng("garbage"), None))
        b = box(low_mem=True, memory="1g")
        self.assertIn("1g", b.momo_argv())
        self.assertEqual(_env(b.momo_argv())["MOMO_LOW_MEM"], "1")
        self.assertNotIn("MOMO_LOW_MEM", _env(box().momo_argv()))
        a = momo_box.build_parser().parse_args(["web", "--no-low-mem", "--memory", "3g"])
        self.assertEqual((a.low_mem, a.memory), (False, "3g"))
        self.assertIsNone(momo_box.build_parser().parse_args(["web"]).low_mem)

    def test_notes_mention_memory_and_slim_images(self):
        res = detect.Resolved(source="detected", base="python:3.13-slim-bookworm",
                              base_variant="slim")
        notes = " ".join(momo_box.box_notes("t", "open", res, [], "repo", memory="1g",
                                            low_mem=True))
        self.assertIn("--full-base", notes)
        self.assertIn("limited to 1g", notes)
        notes = " ".join(momo_box.box_notes("t", "open", detect.Resolved(source="default",
                                                                          base="debian"), [], "r"))
        self.assertNotIn("limited", notes)
        self.assertNotIn("--full-base", notes)

    def test_qualify_image(self):
        q = momo_box.qualify_image
        self.assertEqual(q("debian:bookworm-slim"), "docker.io/library/debian:bookworm-slim")
        self.assertEqual(q("gradle:8-jdk21"), "docker.io/library/gradle:8-jdk21")
        self.assertEqual(q("user/img:1"), "docker.io/user/img:1")
        for ref in ("ghcr.io/x/y:1", "docker.io/library/node:22", "localhost/momo:1",
                    "registry:5000/img", "quay.io/a/b@sha256:abc"):
            self.assertEqual(q(ref), ref)

    class _RecEng:
        dry_run = False

        def __init__(self, rt, images=b""):
            self.rt, self.images, self.calls = rt, images, []

        def run(self, *args, **kw):
            self.calls.append(args)
            out = self.images if args[0] == "images" else b""
            return SimpleNamespace(returncode=0, stdout=out, stderr=b"")

    def test_podman_builds_from_a_qualified_base(self):
        res = detect.Resolved(source="detected", base="gradle:8-jdk21")
        for rt, want in (("podman", "BASE=docker.io/library/gradle:8-jdk21"),
                         ("docker", "BASE=gradle:8-jdk21")):
            eng = self._RecEng(rt)
            with mock.patch.object(momo_box, "say"):
                momo_box.build_image(eng, res, None, "momo-harness:abc")
            build = next(c for c in eng.calls if c[0] == "build")
            self.assertIn(want, build)

    def test_podman_read_only_matches_docker(self):
        argv = box(runtime="podman").momo_argv()
        self.assertIn("--read-only-tmpfs=false", argv)
        self.assertIn("/dev/shm:rw,nosuid,nodev,noexec,size=64m", argv)
        self.assertNotIn("--read-only-tmpfs=false", box().momo_argv())

    def test_prune_keeps_podmans_localhost_image(self):
        rows = (b"localhost/momo-harness:new\nlocalhost/momo-harness:old\n"
                b"localhost/momo-harness:latest\n")
        eng = self._RecEng("podman", rows)
        momo_box.prune_images(eng, "momo-harness:new")
        removed = {c[1] for c in eng.calls if c[0] == "rmi"}
        self.assertEqual(removed, {"momo-harness:old", "momo-harness-overlay:old",
                                   "momo-harness-devbase:old"})

    def test_finds_an_engine_that_is_not_on_path(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "Docker.app/Contents/Resources/bin"
            app.mkdir(parents=True)
            (app / "docker").write_text("#!/bin/sh\n")
            (app / "docker").chmod(0o755)
            empty = Path(d) / "empty"
            empty.mkdir()
            with mock.patch.dict(os.environ, {"PATH": str(empty)}), \
                    mock.patch.dict(momo_box._ENGINE_DIRS, {"docker": [str(app)], "podman": []}):
                os.environ.pop("MOMO_BOX_RUNTIME", None)
                self.assertEqual(momo_box.find_runtime(), "docker")
                self.assertTrue(os.environ["PATH"].startswith(str(app)))   # its helpers too
            with mock.patch.dict(os.environ, {"PATH": str(empty)}), \
                    mock.patch.dict(momo_box._ENGINE_DIRS, {"docker": [], "podman": []}):
                os.environ.pop("MOMO_BOX_RUNTIME", None)
                with self.assertRaises(momo_box.BoxError):
                    momo_box.find_runtime()

    def test_rewrite_llm_url(self):
        f = momo_box.rewrite_llm_url
        self.assertEqual(f("http://localhost:9931", "docker", "Darwin"),
                         ("http://host.docker.internal:9931", False))
        self.assertEqual(f("http://127.0.0.1:8080/", "docker", "Linux"),
                         ("http://host.docker.internal:8080/", True))
        self.assertEqual(f("http://localhost:11434", "podman", "Linux"),
                         ("http://host.containers.internal:11434", False))
        self.assertEqual(f("https://llm.example.com", "docker", "Linux"),
                         ("https://llm.example.com", False))
        with self.assertRaises(momo_box.BoxError):
            f("localhost:8080", "docker")

    def test_open_web_box(self):
        b = box()
        argv = b.momo_argv()
        self.assertEqual(argv[:2], ["run", "-d"])
        self.assertIn("127.0.0.1:8765:8765", argv)
        for flag in ("--init", "--read-only", "no-new-privileges", "ALL"):
            self.assertIn(flag, argv)
        env = _env(argv)
        self.assertEqual(env["MOMO_HOST"], "http://host.docker.internal:9931")
        self.assertEqual(env["MOMO_WEB_PUBLIC_URL"], "http://127.0.0.1:8765/")
        self.assertIn("/cache/in:/in:ro", argv)
        self.assertNotIn("--network", argv)

    def test_linux_open_box_adds_host_gateway(self):
        self.assertIn("host.docker.internal:host-gateway", box(system="Linux").momo_argv())

    def test_locked_box_goes_through_the_gateway(self):
        b = box(net="locked")
        argv = b.momo_argv()
        self.assertEqual(argv[argv.index("--network") + 1], "momo-t-net")
        self.assertNotIn("127.0.0.1:8765:8765", argv)        # published by the gateway
        self.assertEqual(_env(argv)["MOMO_HOST"], "http://gw:9000")
        gw = b.gw_argv()
        self.assertIn("127.0.0.1:8765:8765", gw)
        self.assertIn("9000=host.docker.internal:9931", gw)
        self.assertIn("8765=momo:8765", gw)
        self.assertNotIn("--proxy", gw)
        self.assertEqual(b.gw_connect_argv(),
                         ["network", "connect", "--alias", "gw", "momo-t-net", "momo-t-gw"])

    def test_pull_box_runs_the_proxy(self):
        b = box(net="pull", ui="prompt", token=None)
        gw = b.gw_argv()
        self.assertIn("--proxy", gw)
        self.assertNotIn("8765=momo:8765", gw)
        env = _env(b.momo_argv())
        self.assertEqual(env["MOMO_PROXY"], "http://gw:3128")
        self.assertNotIn("MOMO_WEB_TOKEN", env)

    def test_https_server_keeps_its_name_in_a_gated_box(self):
        b = box(net="locked", llm="https://llm.example.com")
        self.assertEqual(_env(b.momo_argv())["MOMO_HOST"], "https://llm.example.com")
        self.assertIn("443=llm.example.com:443", b.gw_argv())
        self.assertIn("llm.example.com", b.gw_connect_argv())

    def test_prompt_box_stdin(self):
        argv = box(ui="prompt", token=None, interactive=True, prompt_stdin=True).momo_argv()
        self.assertEqual(argv[:2], ["run", "-i"])
        self.assertEqual(_env(argv)["MOMO_PROMPT_STDIN"], "1")

    def test_prepare_goes_through_the_allowlist_in_a_gated_box(self):
        for net in ("locked", "pull"):
            b = box(net=net, prepare=["mvn -q go-offline"], prepare_inputs=["pom.xml"])
            argv = b.prepare_argv(force=True)
            self.assertEqual(argv[argv.index("--network") + 1], "momo-t-net")   # no route out
            self.assertEqual(argv[-2:], ["img:1", "prepare"])
            env = _env(argv)
            self.assertEqual((env["MOMO_NET"], env["MOMO_PROXY"]), ("pull", "http://gw:3128"))
            self.assertEqual(json.loads(env["MOMO_PREPARE"]), ["mvn -q go-offline"])
            self.assertEqual(env["MOMO_PREPARE_FORCE"], "1")

    def test_prepare_on_the_open_network_is_opt_in(self):
        for kw in (dict(net="locked", prepare_net="open"), dict(net="open")):
            argv = box(prepare=["npm ci"], **kw).prepare_argv()
            self.assertNotIn("--network", argv)
            self.assertNotIn("MOMO_PROXY", _env(argv))

    def test_bring_up_order(self):
        """Gated: gateway with the proxy → prefetch → the gateway the box runs with."""
        calls = []
        eng = SimpleNamespace(
            dry_run=False, ok=lambda *a: True, exists=lambda c: False,
            run=lambda *a, **kw: calls.append(a) or SimpleNamespace(returncode=0))
        locked = box(net="locked", prepare=["mvn x"], allow_lines=["repo1.maven.org"],
                     llm_paths=["/v1/chat/completions"])
        with mock.patch.object(momo_box, "say"):
            momo_box._bring_up(eng, locked)
        kinds = [("gw" if "momo-t-gw" in a and a[0] == "run" else
                  "prepare" if "momo-t-prepare" in a else None) for a in calls]
        self.assertEqual([k for k in kinds if k], ["gw", "prepare", "gw"])
        first, last = [a for a in calls if a[0] == "run" and "momo-t-gw" in a]
        self.assertIn("--proxy", first)
        self.assertNotIn("--filter", first)        # the prefetch gateway serves nothing else
        self.assertNotIn("--proxy", last)          # locked again before momo starts
        self.assertIn("--filter", last)
        allow = [a for a in calls if a[:2] == ("exec", "-i")]
        self.assertEqual(len(allow), 1)            # the allowlist is written once, for the prefetch

    def test_failed_prefetch_names_the_refused_hosts(self):
        log = b"\n".join(json.dumps(e).encode() for e in (
            {"host": "repo1.maven.org", "allowed": True}, {"host": "evil.example", "allowed": False},
            {"host": "evil.example", "allowed": False}, {"host": "x\x1b[31m.io", "allowed": False}))
        eng = SimpleNamespace(run=lambda *a, **kw: SimpleNamespace(returncode=1),
                              cp_out=lambda c, path: log + b"\nnot json\n")
        b = box(net="locked", prepare=["mvn x"])
        with mock.patch.object(momo_box, "say"), \
                self.assertRaisesRegex(momo_box.BoxError, r"refused: evil\.example, x\[31m\.io\. Allow"):
            momo_box._run_prepare(eng, b)

    def test_gateway_filters_the_model_server(self):
        paths = ["/api/chat", "/api/tags"]
        gw = box(net="locked", provider="ollama", llm_paths=paths).gw_argv()
        self.assertEqual(gw[gw.index("--filter") + 1], "9000=host.docker.internal:9931")
        self.assertEqual(gw[gw.index("--paths") + 1], "/api/chat,/api/tags")
        self.assertNotIn("9000=host.docker.internal:9931", gw[gw.index("--paths"):])
        off = box(net="locked", llm_paths=paths, llm_filter=False).gw_argv()
        self.assertIn("9000=host.docker.internal:9931", off)
        self.assertNotIn("--filter", off)
        https = box(net="locked", llm="https://llm.example.com", llm_paths=paths).gw_argv()
        self.assertNotIn("--filter", https)        # TLS to the server itself: an opaque tunnel

    def test_token_is_not_on_the_command_line(self):
        b = box()
        argv = b.momo_argv()
        self.assertIsNone(_env(argv)["MOMO_WEB_TOKEN"])
        self.assertFalse(any("tok" == a or a.endswith("=tok") for a in argv))
        self.assertEqual(b.run_env()["MOMO_WEB_TOKEN"], "tok")

    def test_limits_are_configurable(self):
        argv = box(pids=2048, tmp_size="8g").momo_argv()
        self.assertEqual(argv[argv.index("--pids-limit") + 1], "2048")
        self.assertIn("/tmp:rw,exec,mode=1777,size=8g", argv)

    def fake_runtime(self, d: Path, members) -> momo_box.Engine:
        """An engine whose `cp … -` prints a tar of `members` [(name, data | linkname)]."""
        import tarfile
        tar_path = d / "out.tar"
        with tarfile.open(tar_path, "w") as tar:
            for name, data in members:
                ti = tarfile.TarInfo(name)
                if isinstance(data, bytes):
                    ti.size = len(data)
                    tar.addfile(ti, io.BytesIO(data))
                else:
                    ti.type, ti.linkname = tarfile.SYMTYPE, data
                    tar.addfile(ti)
        script = d / "engine"
        script.write_text(f'#!/bin/sh\ncat "{tar_path}"\n')
        script.chmod(0o755)
        return momo_box.Engine(str(script))

    def test_copy_out_is_streamed_and_capped(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            eng = self.fake_runtime(d, [("latest.bundle", b"x" * 5000)])
            self.assertEqual(eng.cp_out("c", "/p"), b"x" * 5000)
            self.assertIs(eng.cp_out("c", "/p", dest=d / "got"), True)
            self.assertEqual((d / "got").stat().st_size, 5000)
            with self.assertRaisesRegex(momo_box.BoxError, "over the 100 limit"):
                eng.cp_out("c", "/p", limit=100)
            # a symlink the box planted in place of the file is never followed
            link = self.fake_runtime(d, [("latest.bundle", "/etc/passwd")])
            self.assertIsNone(link.cp_out("c", "/p", dest=d / "got2"))
            self.assertFalse((d / "got2").exists())
            (d / "engine").write_text("#!/bin/sh\nexit 1\n")          # no such file
            self.assertIsNone(momo_box.Engine(str(d / "engine")).cp_out("c", "/p"))

    def run_box(self, momo, sync):
        """cmd_run with the engine and the box set up by hand; → (code, calls, cleanup kw)."""
        calls, cleanup = [], {}

        def run(*a, **kw):
            calls.append(a)
            if a[0] == "run":
                return momo()
            return SimpleNamespace(returncode=0)
        eng = SimpleNamespace(dry_run=False, exists=lambda c: False, run=run)
        b = box(ui="prompt", token=None, name="t")
        src = momo_box.Source("local", Path("/repo"), Path("/repo"), "a" * 40, "main", "/repo")
        meta = {"name": "t"}
        args = SimpleNamespace(prompt="go", name=None)
        with mock.patch.object(momo_box, "setup_box", lambda *a: (eng, b, src, None, True)), \
                mock.patch.object(momo_box, "save_meta", meta.update), \
                mock.patch.object(momo_box, "load_meta", lambda n: dict(meta)), \
                mock.patch.object(momo_box, "_write_inbox"), mock.patch.object(momo_box, "_bring_up"), \
                mock.patch.object(momo_box, "say"), mock.patch.object(momo_box, "sync_once", sync), \
                mock.patch.object(momo_box, "_cleanup",
                                  lambda eng, name, **kw: cleanup.update(kw)):
            code = momo_box.cmd_run(args)
        return code, calls, cleanup, meta

    def test_interrupted_run_still_lands_its_work(self):
        def interrupted():
            raise KeyboardInterrupt
        synced = []
        code, calls, cleanup, _ = self.run_box(interrupted, lambda eng, name: synced.append(name))
        self.assertEqual(code, 130)
        self.assertIn(("stop", "-t", "30", "momo-t"), calls)          # a graceful stop first
        self.assertEqual(synced, ["t"])                               # …then the work lands
        self.assertEqual(cleanup, {"remove_volumes": True, "keep_meta": False,
                                   "keep_container": False})

    def test_work_that_does_not_land_is_kept(self):
        def fails(eng, name):
            raise momo_box.BoxError("could not fetch")
        code, _, cleanup, meta = self.run_box(lambda: SimpleNamespace(returncode=0), fails)
        self.assertEqual(code, 1)
        self.assertEqual(cleanup, {"remove_volumes": False, "keep_meta": True,
                                   "keep_container": True})
        self.assertIs(meta["ephemeral"], False)                       # it is a named box now

    def test_a_normal_run_cleans_up(self):
        code, _, cleanup, _ = self.run_box(lambda: SimpleNamespace(returncode=3),
                                           lambda eng, name: None)
        self.assertEqual(code, 3)
        self.assertEqual(cleanup["remove_volumes"], True)

    def test_web_box_that_exits_is_reported(self):
        eng = SimpleNamespace(running=lambda c: False, run=lambda *a, **kw: SimpleNamespace(
            stdout=b"Traceback\n\x1b[31mboom\n", stderr=b""))
        with self.assertRaisesRegex(momo_box.BoxError, r"did not start in box t:\nTraceback\n\[31mboom"):
            momo_box._wait_ready(eng, box(), timeout=1)

    def test_web_box_readiness(self):
        srv, port, _ = _http_server()
        self.addCleanup(srv.close)
        eng = SimpleNamespace(running=lambda c: True)
        self.assertTrue(momo_box._wait_ready(eng, box(port=port), timeout=5))
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        free = closed.getsockname()[1]
        closed.close()
        self.assertFalse(momo_box._wait_ready(eng, box(port=free), timeout=0.6))   # still starting

    def test_start_needs_a_web_box(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(momo_box, "cache_dir", lambda: Path(d)):
            momo_box.save_meta({"name": "p", "ui": "prompt"})
            with self.assertRaisesRegex(momo_box.BoxError, "momo-box run --name p"):
                momo_box.cmd_start(SimpleNamespace(name="p", runtime=None, yes=False))
            momo_box.save_meta({"name": "w", "ui": "web", "runtime": "docker",
                                "args": {"net": "locked", "port": 9001, "repo": "/src",
                                         "momo_args": ["--mode", "coding"]}})
            seen = []
            with mock.patch.object(momo_box, "cmd_web", lambda ns: seen.append(ns) or 0):
                momo_box.cmd_start(SimpleNamespace(name="w", runtime=None, yes=False))
            ns = seen[0]
            self.assertEqual((ns.name, ns.net, ns.port, ns.repo, ns.momo_args),
                             ("w", "locked", 9001, "/src", ["--mode", "coding"]))
            self.assertEqual(ns.prepare_net, "pull")                  # defaults for the rest

    def test_defaults(self):
        ns = momo_box.build_parser().parse_args(["run", "-p", "x"])
        self.assertEqual((ns.net, ns.prepare_net, ns.llm_filter, ns.all_refs),
                         ("pull", "pull", "on", False))

    def test_names_are_valid_branches(self):
        for good in ("a", "fix-1", "my.box", "a_b", "x" * 40):
            self.assertTrue(momo_box._NAME_RE.match(good), good)
        for bad in ("a..b", "x.lock", "trail.", "-lead", "Upper", "x" * 41, "", "a/b",
                    "fix-incoming"):            # box fix's overflow branch
            self.assertFalse(momo_box._NAME_RE.match(bad), bad)

    def test_box_output_is_cleaned(self):
        info = momo_box.box_info(json.dumps({
            "export": "a" * 40, "head": "nope", "commits": "3", "turn": True,
            "shortstat": " 1 file\x1b[31m changed\n", "bundle": "/x",
            "skipped_large": ["big\x1b]0;evil\x07.bin", 7, "\u202egnp.exe"]}))
        self.assertEqual(info["export"], "a" * 40)
        self.assertIsNone(info["head"])
        self.assertEqual((info["commits"], info["turn"]), (0, None))
        self.assertEqual(info["shortstat"], " 1 file[31m changed")
        self.assertEqual(info["skipped_large"], ["big]0;evil.bin", "gnp.exe"])
        for junk in (b"not json", b"[1, 2]", b'"text"'):
            self.assertIsNone(momo_box.box_info(junk))

    def test_tar_fallback_takes_plain_files_only(self):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, data in (("ok/a.txt", b"a"), ("../escape.txt", b"x")):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
            link = tarfile.TarInfo("ok/link")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(link)
        buf.seek(0)
        with tempfile.TemporaryDirectory() as d, tarfile.open(fileobj=buf) as tar:
            dest = Path(d) / "ctx"
            dest.mkdir()
            momo_box._extract(tar, dest, has_filter=False)
            self.assertEqual((dest / "ok/a.txt").read_text(), "a")
            self.assertFalse((dest / "ok/link").exists() or (dest / "ok/link").is_symlink())
            self.assertFalse((Path(d) / "escape.txt").exists())

    def test_extra_momo_args(self):
        argv = box(ui="prompt", token=None, extra=["--mode", "coding", "--yes"]).momo_argv()
        self.assertEqual(argv[-5:], ["start", "--", "--mode", "coding", "--yes"])

    def test_notes(self):
        res = detect.Resolved(source="detected", base="gradle:8-jdk21", prepare=["gradle x"],
                              offline_hints=["Gradle: use `--offline`"])
        locked = momo_box.box_notes("t", "locked", res, [], "/repo")
        self.assertIn("momo/t", locked[0])
        self.assertIn("--offline", locked[2])
        pull = momo_box.box_notes("t", "pull", res, ["pypi.org"], "/repo")
        self.assertIn("pypi.org", pull[2])

    def test_image_tag_follows_the_toolchain(self):
        tree = detect.DirTree(Path("/nonexistent"))
        a = detect.Resolved(source="detected", base="x:1", packages=["b", "a"])
        b = detect.Resolved(source="detected", base="x:1", packages=["a", "b"])
        c = detect.Resolved(source="detected", base="x:2")
        tag = momo_box.image_tag(a, tree, "My Proj", "f" * 64)
        self.assertTrue(tag.startswith("momo-box/my-proj:"))
        self.assertEqual(tag, momo_box.image_tag(b, tree, "My Proj", "f" * 64))
        self.assertNotEqual(tag, momo_box.image_tag(c, tree, "My Proj", "f" * 64))
        self.assertNotEqual(tag, momo_box.image_tag(a, tree, "My Proj", "e" * 64))
        default = detect.Resolved(source="default", base="debian:bookworm-slim")
        self.assertEqual(momo_box.image_tag(default, tree, "x", "a" * 64),
                         "momo-harness:" + "a" * 12)

    def test_approval_is_remembered(self):
        res = detect.Resolved(source="box.json", base="x", needs_approval=True,
                              approval_text="--- .momo/box.json\n{}")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(momo_box, "approvals_path", lambda: Path(d) / "a.json"), \
                mock.patch("sys.stderr"):
            with mock.patch("sys.stdin", mock.Mock(isatty=lambda: False)):
                with self.assertRaisesRegex(momo_box.BoxError, "--yes"):
                    momo_box.check_approval(res, assume_yes=False)
                momo_box.check_approval(res, assume_yes=True)
                momo_box.check_approval(res, assume_yes=False)     # remembered now
                res.approval_text += "changed"
                with self.assertRaises(momo_box.BoxError):
                    momo_box.check_approval(res, assume_yes=False)

    def test_is_url(self):
        self.assertTrue(momo_box.is_url("https://github.com/a/b.git"))
        self.assertTrue(momo_box.is_url("git@github.com:a/b.git"))
        self.assertFalse(momo_box.is_url("../b"))

    def test_cli_parses(self):
        p = momo_box.build_parser()
        a = p.parse_args(["run", "-p", "hi", "--net", "pull", "--allow-host", "github"])
        self.assertEqual((a.cmd, a.net, a.allow_host), ("run", "pull", ["github"]))
        self.assertEqual(p.parse_args(["web", "--port", "9000"]).port, 9000)


# ── momo's own flags ─────────────────────────────────────────────────────────

# ── repo network rules (.momo/network.json) ─────────────────────────────────

NETWORK = """{
  // the company mirror only
  "instructions": "Maven resolves through nexus.corp.example.\\nNever add <repositories> to pom.xml.",
  "replace_defaults": true,
  "allow": {"nexus.corp.example": "company mirror", "git.corp.example:7999": "git over HTTPS",
            "*.cdn.corp.example": "", "github": "GitHub",},
  "deny": ["repo1.maven.org"],
}"""


class NetworkRules(TmpDir):
    def resolve(self, files, **kw):
        return detect.resolve(self.tree(files), PRESETS, **kw)

    def rules(self, cfg: dict | str) -> detect.NetworkRules:
        raw = cfg if isinstance(cfg, str) else json.dumps(cfg)
        return detect.load_network_json(self.tree({".momo/network.json": raw}), PRESETS)[0]

    def test_host_rules(self):
        cases = {"Nexus.Corp.Example.": "nexus.corp.example", "*.cdn.corp.example": "*.cdn.corp.example",
                 "git.corp.example:07999": "git.corp.example:7999", "https://docs.corp.example/": "docs.corp.example",
                 "http://h.example:8080": "h.example:8080", "10.0.0.1": "10.0.0.1",
                 "[::1]:8443": "[::1]:8443", "::1": "::1"}
        for entry, rule in cases.items():
            self.assertEqual(detect.parse_host_rule(entry, "t")[0], rule, entry)
        rule, warning = detect.parse_host_rule("https://docs.corp.example/api/v1", "t")
        self.assertEqual(rule, "docs.corp.example")
        self.assertIn("all of docs.corp.example is allowed", warning)
        for bad in ("a b", "x.org:0", "x.org:70000", "*.", "*", "ftp://x.org", "https://u:p@x.org",
                    "a..b", "-a.org", "a@b.org", "a.org\n+evil.org", ""):
            with self.assertRaises(detect.ConfigError, msg=bad):
                detect.parse_host_rule(bad, "t")

    def test_the_file(self):
        r = self.rules(NETWORK)                                   # JSONC: comments, trailing commas
        self.assertTrue(r.replace_defaults)
        self.assertEqual(r.allow["nexus.corp.example"], "company mirror")
        self.assertEqual(r.allow["git.corp.example:7999"], "git over HTTPS")
        self.assertEqual(r.allow["github.com"], "GitHub")         # a group, with its reason
        self.assertEqual(r.deny, {"repo1.maven.org": ""})          # the list form
        self.assertEqual(r.instructions.splitlines()[1], "Never add <repositories> to pom.xml.")
        self.assertEqual(r.warnings, [])

    def test_bad_files_are_refused(self):
        for cfg, msg in (({"alow": {}}, "unknown key"), ({"allow": {"a b": ""}}, "not a host"),
                         ({"allow": {"a.org:0": ""}}, "port"), ({"allow": {"a.org": 1}}, "reason"),
                         ({"allow": "a.org"}, "'allow'"), ({"replace_defaults": "yes"}, "true or false"),
                         ({"instructions": 3}, "instructions"),
                         ({"instructions": "x" * 2001}, "over 2000"), ("[]", "an object"),
                         ("{", "not valid JSON")):
            with self.assertRaisesRegex(detect.ConfigError, msg):
                self.rules(cfg)

    def test_reasons_cannot_add_rules(self):
        r = self.rules({"allow": {"a.org": "fine\n+evil.org # \x1b[31mred"}})
        self.assertEqual(r.allow["a.org"], "fine +evil.org # [31mred")
        lines = momo_box.build_allowlist(SimpleNamespace(
            network=r, pull_hosts=[], registry_hosts=["x.org"]), [], PRESETS)
        self.assertEqual(len(lines), 2)
        self.assertFalse(forward.Allowlist(None, lines).allows("evil.org"))

    def test_warnings(self):
        r = self.rules({"replace_defaults": True, "allow": {"*.com": "", "https://h.org/p": ""}})
        text = " ".join(r.warnings)
        self.assertIn("every host under .com", text)
        self.assertIn("a path can't be enforced", text)
        self.assertIn("replace_defaults", self.rules({"replace_defaults": True}).warnings[0])
        shadowed = self.rules({"allow": {"registry.npmjs.org": "npm", "a.org:8443": ""},
                               "deny": {"*.npmjs.org": "", "a.org:443": ""}})
        self.assertEqual(shadowed.warnings, [".momo/network.json: allow registry.npmjs.org has "
                                             "no effect: deny *.npmjs.org overrides it"])
        res = detect.Resolved(source="detected", base="x", network=shadowed)
        notes = "\n".join(momo_box.box_notes("t", "pull", res, ["a.org:8443"], "/r"))
        self.assertIn("Hosts this project allows: a.org:8443.", notes)  # not the overridden one

    def test_detection_is_kept_and_the_file_needs_approval(self):
        r = self.resolve({"pom.xml": POM_17, ".momo/network.json": NETWORK})
        self.assertEqual((r.source, r.base), ("detected", "maven:3.9-eclipse-temurin-17"))
        self.assertTrue(r.needs_approval)
        self.assertTrue(r.approval_text.startswith("--- .momo/network.json\n"))
        self.assertEqual(r.registry_hosts, [])                     # replaced
        flags = self.resolve({".momo/network.json": NETWORK}, preset="node")
        self.assertIsNotNone(flags.network)                         # applies with --preset too
        both = self.resolve({".momo/box.json": '{"base": "x:1"}', ".momo/network.json": "{}"})
        self.assertIn(".momo/box.json", both.approval_text)
        self.assertIn(".momo/network.json", both.approval_text)
        plain = detect.DirTree(write_tree(self.tmp / "plain", {"pom.xml": POM_17}))
        self.assertIsNone(detect.resolve(plain, PRESETS).network)

    def test_replace_defaults_drops_registries_and_wrapper_hosts(self):
        files = {"build.gradle.kts": "", "gradlew": "#!/bin/sh",
                 "gradle/wrapper/gradle-wrapper.properties":
                     "distributionUrl=https\\://services.gradle.org/distributions/g-8-bin.zip\n"}
        plain = momo_box.build_allowlist(self.resolve(files), [], PRESETS)
        self.assertIn("services.gradle.org  # registry", plain)
        files[".momo/network.json"] = '{"replace_defaults": true, "allow": {"nexus.corp.example": "mirror"}}'
        lines = momo_box.build_allowlist(self.resolve(files), ["extra.org:8443"], PRESETS)
        self.assertEqual(lines, ["+extra.org:8443  # --allow-host", "nexus.corp.example  # mirror"])

    def test_precedence(self):
        res = self.resolve({"package.json": "{}", ".momo/network.json": json.dumps(
            {"allow": {"registry.npmjs.org": "npm"}, "deny": {"*.npmjs.org": "use the mirror"}})})
        a = forward.Allowlist(None, momo_box.build_allowlist(res, [], PRESETS))
        ok, rule = a.match("registry.npmjs.org", 443)
        self.assertFalse(ok)                                       # a deny beats an allow
        self.assertEqual((rule.text, rule.reason), ("*.npmjs.org", "use the mirror"))
        a = forward.Allowlist(None, momo_box.build_allowlist(res, ["registry.npmjs.org"], PRESETS))
        self.assertTrue(a.allows("registry.npmjs.org"))            # your --allow-host beats it
        self.assertFalse(a.allows("other.npmjs.org"))

    def test_ports(self):
        a = forward.Allowlist(None, ["git.corp.example:7999  # git", "h.org", "[::1]:8443"])
        self.assertEqual(a.match("git.corp.example", 7999)[1].reason, "git")
        self.assertFalse(a.allows("git.corp.example"))             # a port rule opens that port only
        self.assertTrue(a.allows("h.org"))
        self.assertFalse(a.allows("h.org", 8443))                  # a plain host: the usual port only
        self.assertTrue(a.match("h.org", 80, {80})[0])
        self.assertTrue(a.allows("::1", 8443))
        self.assertTrue(a.names("git.corp.example"))
        self.assertFalse(a.names("nope.org"))

    def test_allow_file_plus_lines_beat_a_deny(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "allow.txt"
            f.write_text("!a.org  # no\n")
            a = forward.Allowlist(f)
            self.assertFalse(a.allows("a.org"))
            f.write_text("!a.org  # no\n+a.org  # momo-box allow\n")
            os.utime(f, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
            self.assertTrue(a.allows("a.org"))

    def test_proxy_names_the_deny_reason_and_honours_ports(self):
        echo, port = _echo_server()
        self.addCleanup(echo.close)
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "net.log"
            proxy = forward.Proxy(forward.Allowlist(None, [
                f"127.0.0.1:{port}  # the echo", "!localhost:%d  # use the mirror" % port]), log, box="t")
            srv = forward.serve(0, proxy.handle, host="127.0.0.1")
            self.addCleanup(srv.close)
            px = srv.getsockname()[1]
            with socket.create_connection(("127.0.0.1", px)) as s:     # no port patching: the rule
                s.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\nping".encode())
                s.settimeout(5)
                got = b""
                while b"echo:ping" not in got:
                    got += s.recv(4096)
            with socket.create_connection(("127.0.0.1", px)) as s:
                s.sendall(f"CONNECT localhost:{port} HTTP/1.1\r\n\r\n".encode())
                denied = _recv_all(s)
            self.assertIn(b"403", denied)
            self.assertIn(b"denied by this repository's .momo/network.json", denied)
            self.assertIn(b"use the mirror", denied)
            self.assertIn(b"momo-box allow t localhost", denied)
            entries = [json.loads(ln) for ln in log.read_text().splitlines()]
            self.assertEqual([(e["allowed"], e.get("rule"), e.get("reason")) for e in entries],
                             [(True, f"127.0.0.1:{port}", "the echo"),
                              (False, f"!localhost:{port}", "use the mirror")])

    def test_approval_shows_the_rules_and_tracks_changes(self):
        files = {"pom.xml": POM_17, ".momo/network.json": NETWORK}
        res = self.resolve(files)
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(momo_box, "approvals_path", lambda: Path(d) / "a.json"), \
                mock.patch("sys.stderr", err), \
                mock.patch("sys.stdin", mock.Mock(isatty=lambda: False)):
            momo_box.check_approval(res, assume_yes=True)
            momo_box.check_approval(res, assume_yes=False)               # remembered
            files[".momo/network.json"] = NETWORK.replace("company mirror", "changed")
            with self.assertRaisesRegex(momo_box.BoxError, "--yes"):
                momo_box.check_approval(self.resolve(files), assume_yes=False)
        out = err.getvalue()
        self.assertIn("configures its momo-box network", out)
        self.assertIn("allow nexus.corp.example  — company mirror", out)
        self.assertIn("deny  repo1.maven.org", out)
        self.assertIn("replaces the toolchain's public registries", out)

    def test_notes_carry_the_instructions_and_reasons(self):
        res = self.resolve({"pom.xml": POM_17, ".momo/network.json": NETWORK.replace(
            "Never", "\\u001b[2JNever")})
        lines = momo_box.build_allowlist(res, [], PRESETS)
        pull = momo_box.box_notes("t", "pull", res, momo_box.allowed_hosts(lines), "/repo")
        text = "\n".join(pull)
        self.assertIn("Network: the hosts below only", text)
        self.assertIn("nexus.corp.example (company mirror)", text)
        self.assertIn("Refused on purpose by this project: repo1.maven.org", text)
        self.assertIn("Project network instructions (from .momo/network.json):\n"
                      "Maven resolves through nexus.corp.example.\n[2JNever add", text)
        self.assertNotIn("\x1b", text)
        locked = momo_box.box_notes("t", "locked", res, [], "/repo")
        self.assertIn("Project network instructions", locked[-1])     # every mode

    def test_allow_command_writes_your_own_rules(self):
        sent, saved, up = [], [], [True]
        eng = SimpleNamespace(run=lambda *a, **kw: sent.append(kw.get("input")),
                              dry_run=False, running=lambda c: up[0])
        meta = {"name": "t", "net": "pull", "allow_lines": ["pypi.org"]}
        args = SimpleNamespace(name="t", hosts=["a.org", "https://b.org:8443/x"], runtime=None)
        with mock.patch.object(momo_box, "_engine", lambda a: eng), \
                mock.patch.object(momo_box, "load_meta", lambda n: meta), \
                mock.patch.object(momo_box, "save_meta", lambda m: saved.append(dict(m))), \
                mock.patch.object(momo_box, "say"):
            momo_box.cmd_allow(args)
            self.assertEqual(sent, [b"+a.org  # momo-box allow\n+b.org:8443  # momo-box allow\n"])
            # Saved with the box, so a restart and `prepare` keep them.
            added = ["+a.org  # momo-box allow", "+b.org:8443  # momo-box allow"]
            self.assertEqual(saved[-1]["allow_added"], added)
            self.assertEqual(saved[-1]["allow_lines"], ["pypi.org"] + added)
            # Again (no duplicates), and on a stopped box: saved, nothing sent.
            up[0] = False
            momo_box.cmd_allow(args)
            self.assertEqual(len(sent), 1)
            self.assertEqual(saved[-1]["allow_added"], added)
            args.hosts = ["a b"]
            with self.assertRaises(momo_box.BoxError):
                momo_box.cmd_allow(args)


class MomoFlags(unittest.TestCase):
    def test_public_url(self):
        from harness.main import _public_url
        self.assertEqual(_public_url("http://127.0.0.1:9", "tok"), "http://127.0.0.1:9/?token=tok")
        self.assertEqual(_public_url("http://h/x/?a=1", None), "http://h/x/")

    def test_environment_notes_in_the_prompt(self):
        from harness.prompts import _environment
        text = _environment(Path("/w"), set(), "off", False, ["Container: momo-box", " "])
        self.assertIn("- Container: momo-box", text)
        self.assertNotIn("- \n", text)

    def test_mark_turn(self):
        import harness.harness as hmod
        with tempfile.TemporaryDirectory() as d:
            fake = SimpleNamespace(turn_marker=Path(d) / "m" / "turn.json", _ts="s1",
                                   user_turns=lambda: 3, logger=mock.Mock())
            hmod.Harness.mark_turn(fake)
            hmod.Harness.mark_turn(fake)
            data = json.loads(fake.turn_marker.read_text())
            self.assertEqual((data["turn"], data["seq"], data["session"]), (3, 2, "s1"))
            fake.turn_marker = Path(d) / "file" / "x"
            (Path(d) / "file").write_text("not a dir")
            hmod.Harness.mark_turn(fake)                       # logged, not raised
            fake.logger.log_error.assert_called_once()

    def test_turn_end_marks(self):
        from harness.controller import Controller
        h = mock.Mock()
        ctrl = SimpleNamespace(_lock=threading.RLock(), _busy=True, _emit_busy=lambda: None,
                               harness=h, _last_activity=0)
        Controller._set_busy(ctrl, False)
        h.mark_turn.assert_called_once()
        Controller._set_busy(ctrl, False)                      # already idle: no new mark
        h.mark_turn.assert_called_once()

    def test_cli_checks(self):
        def momo(*args):
            with tempfile.TemporaryDirectory() as home:
                env = dict(os.environ, HOME=home, PYTHONPATH=str(REPO))
                return subprocess.run([sys.executable, "-m", "harness.main", *args], cwd=REPO,
                                      env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("needs the web UI", momo("--no-web", "--web-public-url", "http://x").stderr)
        self.assertIn("expected an http", momo("--web-public-url", "ftp://x").stderr)



# ── the Setup panel's status (boxstatus.py) ───────────────────────────────────

import boxstatus       # noqa: E402

INSPECT_OK = {
    "HostConfig": {"ReadonlyRootfs": True, "CapDrop": ["ALL"], "CapAdd": None,
                   "SecurityOpt": ["no-new-privileges"], "Privileged": False, "PidsLimit": 512,
                   "Memory": 4 * 1024 ** 3, "NanoCpus": 2_000_000_000},
    "Config": {"User": "momo"},
    "Mounts": [{"Destination": "/in", "RW": False, "Source": "/c/in"},
               {"Destination": "/run/momo-box", "RW": False, "Source": "/c/status"},
               {"Destination": "/workspace", "RW": True, "Source": ""}],
}


class BoxStatusFiles(TmpDir):
    def test_writes_are_atomic_and_readable_by_the_box(self):
        w = boxstatus.StatusWriter(self.tmp / "status")
        w.write_setup({"name": "t"})
        self.assertEqual(oct((self.tmp / "status").stat().st_mode & 0o777), "0o755")
        self.assertEqual(oct((self.tmp / "status" / "setup.json").stat().st_mode & 0o777), "0o644")
        self.assertEqual(w.read("setup.json")["schema"], 1)
        self.assertEqual([p.name for p in (self.tmp / "status").iterdir()], ["setup.json"])

    def test_setup_has_no_token_or_host_paths(self):
        b = box(net="pull", allow_lines=["+extra.example  # --allow-host",
                                         "!repo1.maven.org  # use the mirror",
                                         "nexus.corp.example  # company mirror",
                                         "pypi.org  # registry"],
                llm_paths=["/v1/chat/completions"], status=self.tmp / "status")
        src = momo_box.Source(kind="local", label=str(self.tmp / "secret-place" / "proj"),
                              repo=self.tmp, sha="a" * 40, ref_name="main", land=self.tmp)
        res = detect.Resolved(source="detected", base="debian:12")
        got = momo_box.setup_status(b, src, res, INSPECT_OK, internal=True)
        text = json.dumps(got)
        self.assertNotIn("tok", json.dumps(got).replace("tokens", ""))
        self.assertNotIn("secret-place", text)
        self.assertEqual(got["source"], {"name": "proj", "kind": "local", "ref": "main"})
        self.assertEqual(got["llm"]["location"]["kind"], "local")
        self.assertTrue(got["llm"]["filter"])
        rules = {r["rule"]: r for r in got["allowlist"]["rules"]}
        self.assertEqual(rules["extra.example"]["source"], "you (--allow-host)")
        self.assertEqual(rules["repo1.maven.org"]["kind"], "!")
        self.assertEqual(rules["repo1.maven.org"]["reason"], "use the mirror")
        self.assertEqual(rules["pypi.org"]["source"], "toolchain")
        self.assertEqual(rules["nexus.corp.example"]["source"], "network.json")
        self.assertNotIn("overridden_by", rules["pypi.org"])
        marked = {r["rule"]: r for r in momo_box._marked_rules(
            ["!*.npmjs.org  # mirror", "registry.npmjs.org  # registry", "+npmjs.org  # --allow-host"])}
        self.assertEqual(marked["registry.npmjs.org"]["overridden_by"], "*.npmjs.org")
        self.assertNotIn("overridden_by", marked["npmjs.org"])          # yours beats a deny
        self.assertTrue(all(r["ok"] for r in got["safeguards"]), got["safeguards"])

    def test_safeguards_come_from_inspect(self):
        bad = json.loads(json.dumps(INSPECT_OK))
        bad["HostConfig"].update(ReadonlyRootfs=False, CapAdd=["NET_RAW"], Privileged=True)
        bad["Config"]["User"] = ""
        bad["Mounts"].append({"Destination": "/var/run/docker.sock", "Source": "/var/run/docker.sock"})
        rows = {r["id"]: r for r in boxstatus.safeguards(
            "pull", bad, False, llm_filtered=True, llm_paths=["/x"], llm_https=False)}
        for key in ("read-only-root", "cap-drop", "not-privileged", "uid", "no-engine-socket",
                    "internal-network"):
            self.assertFalse(rows[key]["ok"], key)
        self.assertIn("NET_RAW", rows["cap-drop"]["detail"])
        # an open box says honestly what it lacks
        rows = {r["id"]: r for r in boxstatus.safeguards(
            "open", INSPECT_OK, None, llm_filtered=False, llm_paths=[], llm_https=False)}
        self.assertFalse(rows["internal-network"]["ok"])
        self.assertFalse(rows["proxy"]["ok"])
        self.assertTrue(rows["read-only-root"]["ok"])

    def test_safeguards_read_podmans_inspect(self):
        """Podman lists every dropped capability and reports what is left in
        BoundingCaps (shapes captured from podman 6.1 inspect, uid 1000)."""
        pod = json.loads(json.dumps(INSPECT_OK))
        pod["HostConfig"]["CapDrop"] = ["CAP_CHOWN", "CAP_KILL", "CAP_SETUID"]
        pod.update(EffectiveCaps=None, BoundingCaps=None)        # --cap-drop ALL
        ok = lambda d: {r["id"]: r["ok"] for r in boxstatus.safeguards(      # noqa: E731
            "pull", d, True, llm_filtered=True, llm_paths=["/x"], llm_https=False)}["cap-drop"]
        self.assertTrue(ok(pod))
        pod["BoundingCaps"] = ["CAP_DAC_OVERRIDE", "CAP_KILL"]   # --cap-drop CHOWN only
        self.assertFalse(ok(pod))
        pod.update(BoundingCaps=["CAP_NET_RAW"], EffectiveCaps=["CAP_NET_RAW"])
        self.assertFalse(ok(pod))
        del pod["BoundingCaps"], pod["EffectiveCaps"]           # no report at all
        self.assertFalse(ok(pod))

    def test_podman_networks_internal_flag(self):
        b = box(net="pull", status=self.tmp / "st", runtime="podman")
        seen = {}

        class Eng:
            rt, dry_run = "podman", False

            def run(self, *args, **kw):
                out = {"container": [INSPECT_OK], "network": [{"name": b.network, "internal": True}]}
                kind = args[2] if len(args) > 2 and args[0] == "inspect" else None
                return SimpleNamespace(returncode=0, stderr=b"",
                                       stdout=json.dumps(out[kind]).encode() if kind else b"1")
        src = SimpleNamespace(label="r", kind="local", sha="a" * 40, ref_name="main", land=self.tmp)
        with mock.patch.object(momo_box, "setup_status",
                               side_effect=lambda *a: seen.setdefault("internal", a[4]) and {}):
            momo_box.write_setup_status(Eng(), b, src, None, inspect=True)
        self.assertIs(seen["internal"], True)

    def test_status_folder_is_mounted_read_only(self):
        argv = box(status=Path("/cache/status")).momo_argv()
        self.assertIn("/cache/status:/run/momo-box:ro", argv)
        self.assertNotIn("/run/momo-box", " ".join(box().momo_argv()))

    def test_entrypoint_passes_the_folder_when_mounted(self):
        with mock.patch.object(entrypoint, "BOX_STATUS", self.tmp):
            self.assertIn(f"--box-status={self.tmp}", entrypoint.momo_argv({"MOMO_UI": "web"}, []))
        with mock.patch.object(entrypoint, "BOX_STATUS", self.tmp / "absent"):
            self.assertFalse(any(a.startswith("--box-status")
                                 for a in entrypoint.momo_argv({"MOMO_UI": "web"}, [])))

    def test_sync_status(self):
        w = boxstatus.StatusWriter(self.tmp / "status")
        w.beat(force=True)
        self.assertEqual(boxstatus.sync_state(w.read("sync.json")), "ok")
        w.failed("could not fetch")
        self.assertEqual(boxstatus.sync_state(w.read("sync.json")), "error")
        w.synced({"turn": 2, "landed": True, "branch": "momo/t-incoming", "incoming": True,
                  "reason": "momo/t is checked out", "commits": 1, "shortstat": "1 file",
                  "head": "b" * 40}, "momo/t-incoming ← 1 commit\x1b[31m")
        data = w.read("sync.json")
        self.assertEqual(boxstatus.sync_state(data), "incoming")
        self.assertEqual(data["turn"], 2)
        self.assertNotIn("\x1b", data["last"]["line"])
        self.assertEqual(len(data["history"]), 1)
        for _ in range(boxstatus.HISTORY_KEEP + 5):
            w.synced({"turn": 3, "landed": True, "branch": "momo/t"}, "x")
        self.assertEqual(len(w.read("sync.json")["history"]), boxstatus.HISTORY_KEEP)
        # a one-off `momo-box sync` is not the syncer's heartbeat
        stale = w.read("sync.json")
        stale["alive_ts"] = 1.0
        boxstatus.write_json(w.folder / "sync.json", stale)
        w.synced({"turn": 4}, None, beat=False)
        self.assertEqual(boxstatus.sync_state(w.read("sync.json")), "syncer-silent")

    def test_net_aggregation(self):
        agg = boxstatus.NetAggregator()
        lines = [
            {"ts": 1.0, "method": "CONNECT", "host": "pypi.org", "port": 443, "allowed": True, "rule": "pypi.org"},
            {"ts": 2.0, "method": "CONNECT", "host": "evil.example", "port": 443, "allowed": False},
            {"ts": 3.0, "method": "CONNECT", "host": "EVIL.example", "port": 443, "allowed": False,
             "rule": "!evil.example", "reason": "no\x1b]0;x\x07"},
            {"ts": 4.0, "kind": "llm", "method": "POST", "path": "/api/delete", "allowed": False},
            {"ts": 5.0, "kind": "llm", "method": "POST", "path": "/v1/chat/completions",
             "allowed": True, "status": 200},
        ]
        for e in lines:
            self.assertTrue(agg.feed(json.dumps(e)))
        for junk in ("not json", "[]", json.dumps({"host": "x"}),
                     json.dumps({"allowed": True, "host": ""})):
            self.assertFalse(agg.feed(junk))
        snap = agg.snapshot()
        self.assertEqual(snap["totals"], {"allowed": 1, "denied": 2})
        evil = next(h for h in snap["hosts"] if h["host"] == "evil.example")
        self.assertEqual((evil["denied"], evil["rule"]), (2, "!evil.example"))
        self.assertNotIn("\x1b", evil["reason"])
        self.assertEqual(snap["llm"]["requests"], 2)
        self.assertEqual(snap["llm"]["refused"], 1)
        self.assertEqual(snap["llm"]["last_refused"], "POST /api/delete")
        self.assertEqual(len(snap["recent"]), 5)
        agg.feed(json.dumps({"ts": 6.0, "kind": "llm", "method": "GET", "path": "/v1/models",
                             "allowed": True, "status": 200}))       # a health check
        snap = agg.snapshot()
        self.assertEqual((snap["llm"]["requests"], snap["llm"]["inference"]), (3, 2))
        self.assertEqual(len(snap["recent"]), 5)                      # counted, not listed
        for i in range(boxstatus.RECENT_KEEP + 10):
            agg.feed(json.dumps({"ts": 10.0 + i, "host": f"h{i}.example", "port": 443, "allowed": True}))
        self.assertEqual(len(agg.snapshot()["recent"]), boxstatus.RECENT_KEEP)

    def test_nettail_follows_across_rotation(self):
        log = self.tmp / "net.log"
        log.write_text('{"a": 1}\n')
        out = io.StringIO()
        forward.nettail(log, out=out, interval=0, rounds=1)
        self.assertEqual(out.getvalue(), '{"a": 1}\n')

        class Feed(io.StringIO):
            """Rotates the log after the first round, as NetLog does at 5 MB."""
            rounds = 0

            def flush(self):
                Feed.rounds += 1
                if Feed.rounds == 1:
                    os.replace(log, log.with_name("net.log.1"))
                    log.write_text('{"b": 2}\n')
        feed = Feed()
        forward.nettail(log, out=feed, interval=0, heartbeat=0, rounds=3)
        self.assertIn('{"a": 1}', feed.getvalue())
        self.assertIn('{"b": 2}', feed.getvalue())

    def test_llm_filter_logs_its_decisions(self):
        srv, port, _seen = _http_server()
        self.addCleanup(srv.close)
        netlog = forward.NetLog(self.tmp / "net.log")
        gw = forward.serve(0, forward.LlmFilter("127.0.0.1", port, ["/v1/chat/completions"],
                                                 netlog).handle, host="127.0.0.1")
        self.addCleanup(gw.close)
        gport = gw.getsockname()[1]
        for req in (b"POST /v1/chat/completions?x=1 HTTP/1.1\r\nContent-Length: 0\r\n\r\n",
                    b"POST /api/delete HTTP/1.1\r\nContent-Length: 0\r\n\r\n"):
            with socket.create_connection(("127.0.0.1", gport)) as s:
                s.sendall(req)
                _recv_all(s)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = [json.loads(ln) for ln in (self.tmp / "net.log").read_text().splitlines()] \
                if (self.tmp / "net.log").exists() else []
            if len(rows) >= 2:
                break
            time.sleep(0.05)
        by_path = {r["path"]: r for r in rows}
        self.assertEqual(by_path["/v1/chat/completions"]["status"], 200)   # no query string
        self.assertTrue(by_path["/v1/chat/completions"]["allowed"])
        self.assertFalse(by_path["/api/delete"]["allowed"])
        self.assertTrue(all(r["kind"] == "llm" for r in rows))

    def test_gateway_always_logs(self):
        argv = box(net="locked").gw_argv()
        self.assertIn("/gw/net.log", argv)
        self.assertNotIn("--proxy", argv)


class MemoryStats(TmpDir):
    def test_parse_mem_usage(self):
        f = boxstatus.parse_mem_usage
        self.assertEqual(f("19.57MiB / 1GiB"), {"current": int(19.57 * 1024 ** 2), "max": 1024 ** 3})
        self.assertEqual(f("1.2GB / 2.35GiB")["current"], 1_200_000_000)
        self.assertEqual(f("512KiB"), {"current": 512 * 1024, "max": None})
        for junk in ("", "--", "lots / of", "1.2 parsecs / 1GiB"):
            self.assertIsNone(f(junk), junk)
        got = boxstatus.docker_stats('{"Name":"a","MemUsage":"1MiB / 2MiB"}\nnot json\n'
                                     '{"Name":"b","MemUsage":"??"}\n')
        self.assertEqual(got, {"a": {"current": 1024 ** 2, "max": 2 * 1024 ** 2}})
        # podman 6.1: bytes, not text (captured from `podman stats --format '{{json .}}'`)
        podman = ('{"Name":"momo-t-gw","CPU":3.57,"MemUsage":249856,"MemLimit":268435456,'
                  '"MemPerc":0.093,"PIDs":1}\n{"Name":"c","MemUsage":-1,"MemLimit":0}\n'
                  '{"Name":"d","MemUsage":true}\n{"Name":"e","MemUsage":10,"MemLimit":0}')
        self.assertEqual(boxstatus.docker_stats(podman),
                         {"momo-t-gw": {"current": 249856, "max": 268435456},
                          "e": {"current": 10, "max": None}})

    def test_stats_thread_writes_the_gateways_memory(self):
        w = boxstatus.StatusWriter(self.tmp / "status")
        state = {"stop": False}

        class Eng:
            dry_run, rt = False, "docker"
            calls = 0

            def running(self, _c):
                return Eng.calls < 1

            def run(self, *args, **kw):
                Eng.calls += 1
                return SimpleNamespace(returncode=0, stderr=b"", stdout=json.dumps(
                    {"Name": "momo-t-gw", "MemUsage": "20MiB / 1GiB"}).encode())
        momo_box._follow_stats(Eng(), "t", state, w, every=0)
        self.assertEqual(w.read("stats.json")["gateway"]["current"], 20 * 1024 ** 2)


class SyncStatus(Workspace):
    def test_land_reports_what_landed(self):
        self.clone()
        (self.ws / "app.py").write_text("print(2)\n")
        out = {}
        self.assertIn("momo/t ←", momo_box.land(self.host, Path(self.snapshot()["bundle"]), "t",
                                                self.snapshot(), out))
        self.assertEqual((out["branch"], out["incoming"], out["commits"]), ("momo/t", False, 1))
        git(self.host, "checkout", "-q", "momo/t")
        (self.ws / "app.py").write_text("print(3)\n")
        info = self.snapshot()
        out = {}
        momo_box.land(self.host, Path(info["bundle"]), "t", info, out)
        self.assertTrue(out["incoming"])
        self.assertEqual(out["branch"], "momo/t-incoming")
        self.assertIn("checked out", out["reason"])

    def test_cmd_sync_records_without_a_heartbeat(self):
        self.clone()
        eng = self.engine()
        (self.ws / "app.py").write_text("print(2)\n")
        momo_box.status_writer("t").ensure()
        with mock.patch.object(momo_box, "_engine", lambda args: eng):
            momo_box.cmd_sync(SimpleNamespace(container=None, land_repo=None, follow=False, name="t"))
        data = momo_box.status_writer("t").read("sync.json")
        self.assertEqual(data["last"]["branch"], "momo/t")
        self.assertNotIn("alive_ts", data)


class Dash(TmpDir):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(momo_box, "cache_dir", lambda: self.tmp / "cache")
        p.start()
        self.addCleanup(p.stop)
        momo_box.save_meta({"name": "b1", "ui": "web", "net": "pull", "port": 4242,
                            "token": "SECRET", "land": "/repo"})
        w = momo_box.status_writer("b1")
        w.write_setup({"name": "b1", "net": "pull", "llm": {"provider": "llamacpp"}})
        w.beat(force=True)

        class Eng:
            dry_run, rt = False, "docker"

            def run(self, *args, **kw):
                if args[0] == "stats":
                    out = "\n".join(json.dumps({"Name": n, "MemUsage": u}) for n, u in (
                        ("momo-b1", "87.5MiB / 1GiB"), ("momo-b1-gw", "19.57MiB / 1GiB")))
                elif args[0] == "info":
                    out = str(2_525_360_128)
                else:
                    out = "momo-b1\nmomo-b1-gw\n"
                return SimpleNamespace(returncode=0, stdout=out.encode(), stderr=b"")
        self.eng = Eng()
        momo_box._DASH_MEM.ts = momo_box._DASH_ENGINE.ts = 0       # no cache from another test

    def test_rows(self):
        with mock.patch.object(momo_box, "_syncer_pid", lambda n: 1):
            rows = momo_box.dash_rows(self.eng)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "running")
        self.assertEqual(rows[0]["url"], "http://127.0.0.1:4242/?token=SECRET")
        self.assertEqual(rows[0]["view"]["box"]["sync_state"], "ok")
        self.assertEqual(rows[0]["view"]["kind"], "box")
        mem = rows[0]["view"]["memory"]
        self.assertEqual((mem["current"], mem["max"]), (int(87.5 * 1024 ** 2), 1024 ** 3))
        self.assertEqual(mem["gateway"]["current"], int(19.57 * 1024 ** 2))
        totals = momo_box.dash_totals(self.eng, rows)
        self.assertEqual(totals["engine_total"], 2_525_360_128)
        self.assertEqual(totals["boxes_used"], mem["current"] + mem["gateway"]["current"])

    def test_server_needs_the_token_and_the_host(self):
        import http.client
        srv = momo_box.serve_dash(self.eng, 0, "tok123")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]

        def get(path, headers):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", path, headers=headers)
            r = c.getresponse()
            body = r.read()
            c.close()
            return r.status, body
        host = {"Host": f"127.0.0.1:{port}"}
        self.assertEqual(get("/api/boxes", host)[0], 401)
        self.assertEqual(get("/api/boxes", {**host, "X-Momo-Token": "nope"})[0], 401)
        self.assertEqual(get("/api/boxes", {"Host": "evil.example", "X-Momo-Token": "tok123"})[0], 403)
        with mock.patch.object(momo_box, "_syncer_pid", lambda n: 1):
            code, body = get("/api/boxes", {**host, "X-Momo-Token": "tok123"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["boxes"][0]["name"], "b1")
        for path in ("/", "/dash.js", "/setup-render.js", "/app.css"):
            self.assertEqual(get(path, host)[0], 200, path)
        self.assertEqual(get("/../box.json", host)[0], 404)

if __name__ == "__main__":
    unittest.main()


def _load_wheels_update():
    import importlib.util
    spec = importlib.util.spec_from_file_location("wheels_update",
                                                  REPO / "container" / "wheels" / "update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VendoredWheels(unittest.TestCase):
    """The image's extras come from container/wheels only, each file pinned by hash."""

    def setUp(self):
        self.up = _load_wheels_update()

    def test_committed_folders_are_consistent(self):
        python = self.up.py_version()
        for arch in self.up.ARCHES:
            with self.subTest(arch=arch):
                self.assertEqual(self.up.check(self.up.HERE / arch, python), [])
                pins, _ = self.up.read_lock(self.up.HERE / arch)
                self.assertIn("tree-sitter", pins)                 # the parser core is never skipped

    def test_pins_satisfy_requirements_txt(self):
        try:
            from packaging.requirements import Requirement
        except ImportError:                                        # pragma: no cover
            from pip._vendor.packaging.requirements import Requirement
        for arch in self.up.ARCHES:
            pins, _ = self.up.read_lock(self.up.HERE / arch)
            for line in self.up.requirement_lines():
                req = Requirement(line)
                if (name := self.up.norm(req.name)) in pins:
                    self.assertIn(pins[name][0], req.specifier, f"{arch}: {line}")

    def test_dockerfile_installs_offline_with_hashes(self):
        text = (REPO / "container" / "Dockerfile").read_text()
        for flag in ("--no-index", "--require-hashes", "--only-binary :all:", "requirements.lock"):
            self.assertIn(flag, text)
        self.assertNotIn("COPY requirements.txt", text)
        self.assertIn("rm -rf /opt/momo/app/container/wheels", text)

    def test_check_catches_tampering_and_drift(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            whl = folder / "tree_sitter-0.26.1-cp314-cp314-manylinux2014_aarch64.whl"
            whl.write_bytes(b"real")
            (folder / "pypdf-6.21.4-py3-none-any.whl").write_bytes(b"pdf")
            with mock.patch.object(self.up, "requirement_lines",
                                   return_value=["tree-sitter>=0.25", "pypdf>=6",
                                                 "tree-sitter-dockerfile>=0.2"]):
                self.up.write_lock(folder, ["tree-sitter-dockerfile>=0.2"], "3.14", "arm64")
                self.assertEqual(self.up.check(folder, "3.14"), [])
                whl.write_bytes(b"swapped")                         # same name, other bytes
                self.assertTrue(any("does not match" in p for p in self.up.check(folder, "3.14")))
                self.up.write_lock(folder, [], "3.14", "arm64")       # skip list lost
                self.assertTrue(any("tree-sitter-dockerfile" in p
                                    for p in self.up.check(folder, "3.14")))
                self.up.write_lock(folder, ["tree-sitter-dockerfile>=0.2"], "3.15", "arm64")
                self.assertTrue(any("CPython 3.14" in p for p in self.up.check(folder, "3.15")))
                (folder / "extra-1.0-py3-none-any.whl").write_bytes(b"x")
                self.assertTrue(any("not in requirements.lock" in p
                                    for p in self.up.check(folder, "3.14")))

    def test_wheel_names(self):
        self.assertEqual(self.up.wheel_info("tree_sitter_cpp-0.23.4-cp39-abi3-manylinux2014_x86_64.whl"),
                         ("tree-sitter-cpp", "0.23.4", "abi3"))
        self.assertEqual(self.up.req_name("tree-sitter-dockerfile>=0.2"), "tree-sitter-dockerfile")
        with self.assertRaises(ValueError):
            self.up.wheel_info("notawheel.tar.gz")


class MomoPython(unittest.TestCase):
    """momo's Python: the official image pinned by digest, made self-contained in stage 1."""

    def setUp(self):
        self.up = _load_wheels_update()
        self.dockerfile = (REPO / "container" / "Dockerfile").read_text()

    def test_pinned_by_digest_and_matches_py_version(self):
        tag, digest = self.up.py_image()
        self.assertTrue(tag.startswith(self.up.py_version() + "."), tag)
        version = re.search(r"^ARG PY_VERSION=(\S+)$", self.dockerfile, re.M).group(1)
        self.assertTrue(tag.startswith(version + "-"), (tag, version))
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertIn("FROM ${PY_IMAGE} AS momo-runtime", self.dockerfile)

    def test_nothing_is_downloaded_for_it(self):
        for gone in ("python-build-standalone", "curl ", "github.com", "PBS_"):
            self.assertNotIn(gone, self.dockerfile)
        self.assertIn("bundle-python.sh /usr/local /opt/momo/python", self.dockerfile)
        self.assertIn("GLIBC_FLOOR", self.dockerfile)                 # the smoke test names it

    def test_bundle_script_checks_its_work(self):
        script = (REPO / "container" / "bundle-python.sh").read_text()
        self.assertIn("patchelf --set-rpath '$ORIGIN/../..'", script)
        self.assertIn("from the build image", script)                 # the leak check
        self.assertIn("not found", script)
        self.assertNotIn("LD_LIBRARY_PATH", self.dockerfile)          # would reach the model's commands
        r = subprocess.run(["sh", "-n", str(REPO / "container" / "bundle-python.sh")])
        self.assertEqual(r.returncode, 0)

    def test_repin_rewrites_both_args(self):
        with tempfile.TemporaryDirectory() as d:
            df = Path(d) / "Dockerfile"
            df.write_text("ARG PY_VERSION=3.14.10\nARG PY_IMAGE=docker.io/library/python:"
                          "3.14.10-slim-bookworm@sha256:" + "a" * 64 + "\nFROM ${PY_IMAGE}\n")
            asked = []
            with mock.patch.object(self.up, "registry_digest",
                                   side_effect=lambda tag: asked.append(tag) or "sha256:" + "b" * 64), \
                    mock.patch("builtins.print"):
                self.assertFalse(self.up.set_python_image(None, df))
                self.assertTrue(self.up.set_python_image("3.15.0", df))
            self.assertEqual(asked, ["3.14.10-slim-bookworm", "3.15.0-slim-bookworm"])
            text = df.read_text()
            self.assertIn("ARG PY_VERSION=3.15.0\n", text)
            self.assertIn("python:3.15.0-slim-bookworm@sha256:" + "b" * 64, text)
