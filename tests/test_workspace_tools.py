"""Workspace tools (.momo/tools/, harness/tools/workspace.py): read without
running, approved per code fingerprint (kept in HOME), run in their own
`python -I` process, roles from their category, settings in .momo/tools.json."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import commands, momo_dir
from harness import session as session_mod
from harness.ignore_rules import Rules
from harness.llm.base import ChatResponse, ToolCall
from harness.tools import dispatch, offered, registry, workspace

from test_run_output import _Scripted

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "skills" / "workspace-tools.md"
EXAMPLE = REPO / "docs" / "examples" / "workspace-tools" / "todo_count.py"


def tool_file(name: str, *, category: str | None = "read", body: str = 'return "ok"',
              params: str = "{}", sig: str = "", extra: str = "", top: str = "") -> str:
    """A workspace tool file.  body is run()'s body; its later lines are written
    indented by four spaces, like the first line's place in the function."""
    cat = f'"category": {category!r}, ' if category is not None else ""
    first, *rest = body.split("\n")
    body_lines = "\n".join(["    " + first] + ["    " + ln.removeprefix("    ") for ln in rest])
    return (f"import os, sys\nfrom pathlib import Path\n{top}\n"
            f'TOOL = {{"name": {name!r}, "description": "Test tool {name}.", '
            f'"parameters": {params}, {cat}{extra}}}\n\n'
            f"def run({sig}*, workdir: Path) -> str:\n{body_lines}\n")


class _Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.home = root / "home"
        self.home.mkdir()
        self.wd = root / "project"
        self.tools = self.wd / ".momo" / "tools"
        self.tools.mkdir(parents=True)
        p = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(registry.set_workspace, [])
        self.addCleanup(self.tmp.cleanup)

    def put(self, name: str, text: str) -> Path:
        path = self.tools / name
        path.write_text(text)
        return path

    def load(self):
        return workspace.load_workspace_tools(self.wd)

    def approve(self, *names):
        out = workspace.decide(self.wd, list(names) or ["all"], True)
        self.load()
        return out

    def call(self, name, **args):
        return dispatch(name, args, self.wd)

    def status(self, name):
        return next(t for t in workspace.discover(self.wd) if t.name == name)


class Discovery(_Workspace):
    def test_listing_runs_nothing_and_pending_tools_are_not_offered(self):
        marker = self.wd / "MARKER"
        self.put("probe.py", tool_file("probe", top=f"open({str(marker)!r}, 'w').write('ran')"))
        report = self.load()
        self.assertEqual([t.status for t in report.tools], ["pending"])
        self.assertIn("waiting for approval: probe", report.summary())
        self.assertFalse(marker.exists())
        self.assertIn("without running anything", workspace.check(self.wd))
        self.assertFalse(marker.exists())
        self.assertNotIn("probe", [t["function"]["name"] for t in offered("coding")])
        self.assertEqual(self.call("probe"), "ERROR: unknown tool 'probe'")

    def test_approval_then_change_then_approval_again(self):
        self.put("probe.py", tool_file("probe"))
        self.put("_helper.py", "X = 1\n")
        self.approve("probe")
        self.assertEqual(self.call("probe"), "ok")
        self.assertIn("probe", [t["function"]["name"] for t in offered("chat")])
        self.put("_helper.py", "X = 2\n")                 # a helper is part of the code
        out = self.call("probe")
        self.assertTrue(out.startswith("ERROR: probe was not run") and "/tools approve probe" in out)
        self.assertEqual(self.status("probe").status, "pending")
        self.approve("probe")
        self.assertEqual(self.call("probe"), "ok")

    def test_approvals_live_in_home_and_denials_stick(self):
        self.put("aa.py", tool_file("aa"))
        self.put("bb.py", tool_file("bb"))
        workspace.decide(self.wd, ["bb"], False)
        self.approve()                                     # "all" = the waiting ones
        self.assertEqual(self.status("aa").status, "loaded")
        self.assertEqual(self.status("bb").status, "denied")
        self.assertTrue((self.home / ".momo-harness" / "workspace-tools-approved.json").is_file())
        self.assertEqual(sorted(p.name for p in self.wd.rglob("*approved*")), [])
        other = Path(self.tmp.name) / "clone"
        shutil.copytree(self.wd, other)                    # the same repo elsewhere: asks again
        self.assertEqual({t.status for t in workspace.discover(other)}, {"pending"})

    def test_invalid_files_are_reported(self):
        cases = {
            "wrong_name.py": (tool_file("other_name"), "must match the file name"),
            "unknown_key.py": (tool_file("unknown_key", extra='"color": "red", '), "unknown key"),
            "not_literal.py": ('X = "a"\nTOOL = {"name": X}\ndef run(*, workdir): return ""\n',
                               "plain literal"),
            "no_run.py": ('TOOL = {"name": "no_run", "description": "d", "parameters": {}}\n',
                          "no module-level def run"),
            "no_workdir.py": (tool_file("no_workdir").replace("*, workdir: Path", ""),
                              "workdir keyword"),
            "lost_param.py": (tool_file("lost_param", params='{"x": {"type": "string"}}'),
                              "does not take the parameter(s) x"),
            "read_file.py": (tool_file("read_file"), "already a built-in tool"),
            "bad_required.py": (tool_file("bad_required", extra='"required": ["zz"], '),
                                "'required' must be one of the parameters"),
            "syntax.py": ("TOOL = {\n", "syntax error"),
        }
        for file, (text, _) in cases.items():
            self.put(file, text)
        self.put("_helper.py", "raise SystemExit('never loaded as a tool')\n")
        found = {t.path.name: t for t in workspace.discover(self.wd)}
        self.assertNotIn("_helper.py", found)
        for file, (_, expect) in cases.items():
            with self.subTest(file=file):
                self.assertEqual(found[file].status, "error")
                self.assertIn(expect, found[file].reason)
        self.assertIn("not loaded", self.load().summary())


class Isolation(_Workspace):
    def run_tool(self, body, **kw):
        self.put("probe.py", tool_file("probe", body=body, **kw))
        self.approve("probe")
        return self.call("probe")

    def test_harness_is_not_importable(self):
        out = self.run_tool("import harness\n    return 'imported'")
        self.assertTrue(out.startswith("ERROR: probe failed: ModuleNotFoundError"), out)

    def test_environment_prints_and_helpers(self):
        self.put("_helper.py", "VALUE = 'from the helper'\n")
        out = self.run_tool(
            "print('noise on stdout'); os.system('echo more noise')\n"
            "    import _helper\n"
            "    return json.dumps([_helper.VALUE, str(Path.cwd()), str(workdir),\n"
            "                       os.environ.get('MOMO_SCRATCH'), sys.flags.isolated,\n"
            "                       sys.dont_write_bytecode])",
            top="import json")
        value, cwd, wd, scratch, isolated, no_bytecode = json.loads(out)
        self.assertEqual(value, "from the helper")
        self.assertEqual((cwd, wd), (str(self.wd), str(self.wd)))
        self.assertEqual(scratch, str(self.wd / ".momo" / "scratch"))
        self.assertTrue(isolated and no_bytecode)
        self.assertEqual(list(self.tools.rglob("__pycache__")), [])

    def test_failures_become_errors(self):
        out = self.run_tool("raise ValueError('bad input')")
        self.assertTrue(out.startswith("ERROR: probe failed: ValueError: bad input"), out)
        self.assertIn("Traceback", out)
        out = self.run_tool("return 42")
        self.assertEqual(out, "ERROR: probe failed: run() returned int, not a str")
        out = self.run_tool("sys.exit(3)")
        self.assertTrue(out.startswith("ERROR: probe failed: SystemExit"), out)
        out = self.run_tool("import not_a_real_package_xyz\n    return ''")
        self.assertIn("requirements.txt", out)
        self.assertIn("/tools venv", out)

    def test_timeout_kills_the_tool_and_what_it_started(self):
        pid_file = self.wd / "child.pid"
        out = self.run_tool(
            "import subprocess\n"
            f"    p = subprocess.Popen(['sleep', '30']); open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
            "    p.wait(); return 'done'", extra='"timeout": 2, ')
        self.assertTrue(out.startswith("ERROR: probe timed out after 2s"), out)
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_esc_interrupts(self):
        self.put("slow.py", tool_file("slow", body="import time; time.sleep(30); return 'late'"))
        self.approve("slow")
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        t0 = time.monotonic()
        out = dispatch("slow", {}, self.wd, cancel=cancel)
        self.assertEqual(out, "ERROR: slow was interrupted by the user")
        self.assertLess(time.monotonic() - t0, 10)


class Categories(_Workspace):
    def roles(self, name):
        return {r for r in registry.ROLES
                if name in [t["function"]["name"] for t in offered(r, net_on=True)]}

    def test_category_roles(self):
        for cat in ("read", "write", "run", "net", None, "nonsense"):
            name = f"t_{cat or 'none'}"
            self.put(f"{name}.py", tool_file(name, category=cat))
        self.approve()
        self.assertEqual(self.roles("t_read"), set(registry.ROLES))
        self.assertEqual(self.roles("t_write"), {"coding", "momo", "plan-exec", "story-exec"})
        self.assertEqual(self.roles("t_run"), {"coding", "momo", "plan-exec", "story-exec", "plan",
                                                "story-writer"})
        self.assertEqual(self.roles("t_none"), {"coding", "momo", "plan-exec", "story-exec"})
        self.assertEqual(self.roles("t_nonsense"), {"coding", "momo", "plan-exec", "story-exec"})
        self.assertIn("treated as write", " ".join(self.status("t_nonsense").notes))
        self.assertEqual(self.roles("t_net"), set(registry.ROLES))
        self.assertNotIn("t_net", [t["function"]["name"] for t in offered("coding")])   # /net off
        self.assertTrue(registry.get("t_read").keeps_green)
        self.assertFalse(registry.get("t_write").keeps_green)

    def test_never_indexed(self):
        self.assertTrue(Rules.parse("").excluded(".momo/tools/t_read.py"))
        self.assertTrue(Rules.parse("!.momo/\n!.momo/**").excluded(".momo/tools/t_read.py"))


class Interpreter(_Workspace):
    def test_order_and_fake_venvs(self):
        self.assertEqual(workspace.interpreter(self.wd, {})[1], "momo's own Python")
        # a committed "python" that is really a script
        fake = self.tools / ".venv" / "bin" / "python"
        fake.parent.mkdir(parents=True)
        marker = self.wd / "PWNED"
        fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
        fake.chmod(0o755)
        exe, origin, problem = workspace.interpreter(self.wd, {})
        self.assertEqual(origin, ".momo/tools/.venv")
        self.assertIn("no pyvenv.cfg", problem)
        (self.tools / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
        self.assertIn("resolves inside the workspace", workspace.interpreter(self.wd, {})[2])
        self.put("probe.py", tool_file("probe"))
        t = self.status("probe")
        self.assertEqual(t.status, "error")
        self.assertIn("won't run", t.reason)
        self.assertIn("nothing to approve", workspace.decide(self.wd, ["probe"], True))
        self.assertFalse(marker.exists())
        # tools.json "python" wins over .momo/tools/.venv
        settings = {"python": os.path.realpath(sys.executable)}
        exe, origin, problem = workspace.interpreter(self.wd, settings)
        self.assertEqual((str(exe), problem), (os.path.realpath(sys.executable), ""))
        self.assertIn("tools.json", origin)

    def test_another_interpreter_needs_a_new_approval(self):
        self.put("probe.py", tool_file("probe"))
        self.approve("probe")
        self.assertEqual(self.call("probe"), "ok")
        real = os.path.realpath(sys.executable)
        if real == sys.executable:
            self.skipTest("momo's Python is not a link, so there is no second path to it")
        workspace.save_settings(self.wd, {"python": real})
        self.assertTrue(self.call("probe").startswith("ERROR: probe was not run"))
        self.assertEqual(self.status("probe").status, "pending")


class Settings(_Workspace):
    def test_overrides_are_written_and_validated(self):
        self.put("probe.py", tool_file("probe", category="write"))
        self.approve("probe")
        self.assertEqual(workspace.set_override(self.wd, "probe", "roles", ["chat", "coding"]), "")
        self.assertEqual(workspace.set_override(self.wd, "probe", "category", "read"), "")
        data = json.loads((self.wd / ".momo" / "tools.json").read_text())
        self.assertEqual(data, {"tools": {"probe": {"category": "read", "roles": ["chat", "coding"]}}})
        self.assertEqual(oct((self.wd / ".momo" / "tools.json").stat().st_mode & 0o777), "0o644")
        t = self.status("probe")
        self.assertEqual((t.category, t.category_from, sorted(t.roles), t.roles_from),
                         ("read", "tools.json", ["chat", "coding"], "tools.json"))
        self.assertIn("built-in", workspace.set_override(self.wd, "read_file", "roles", ["chat"]))
        self.assertTrue(workspace.set_override(self.wd, "nope", "roles", ["chat"]).startswith("ERROR"))
        workspace.set_override(self.wd, "probe", "enabled", False)
        self.assertEqual(self.status("probe").status, "disabled")
        workspace.set_override(self.wd, "probe", "enabled", None)
        workspace.set_override(self.wd, "probe", "roles", None)
        workspace.set_override(self.wd, "probe", "category", None)
        self.assertFalse((self.wd / ".momo" / "tools.json").read_text().count("probe"))

    def test_broken_or_foreign_settings_are_reported(self):
        self.put("probe.py", tool_file("probe"))
        (self.wd / ".momo" / "tools.json").write_text("{not json")
        self.assertIn("ignored", self.load().summary())
        (self.wd / ".momo" / "tools.json").write_text(json.dumps({"tools": {"fetch_url": {"roles": ["chat"]}}}))
        self.assertIn("built-in and personal tools keep their roles", self.load().summary())


class Git(_Workspace):
    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_tools_are_committed_the_rest_is_not(self):
        subprocess.run(["git", "init", "-q", str(self.wd)], check=True)
        momo_dir.ensure(self.wd)
        for rel in (".momo/tools/a.py", ".momo/tools/_h.py", ".momo/tools/README.md",
                    ".momo/tools/requirements.txt", ".momo/tools.json", ".momo/scratch/x",
                    ".momo/runs/s/r1.log", ".momo/plan.md", ".momo/tools/.venv/bin/python",
                    ".momo/tools/__pycache__/a.pyc"):
            (self.wd / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.wd / rel).write_text("x")
        status = subprocess.run(["git", "-C", str(self.wd), "status", "--porcelain",
                                 "--untracked-files=all"], capture_output=True, text=True).stdout
        self.assertEqual(sorted(line[3:] for line in status.splitlines()),
                         [".momo/tools.json", ".momo/tools/README.md", ".momo/tools/_h.py",
                          ".momo/tools/a.py", ".momo/tools/requirements.txt"])

    def test_old_gitignore_is_upgraded_and_a_custom_one_kept(self):
        ignore = self.wd / ".momo" / ".gitignore"
        ignore.write_text(momo_dir._OLD_GITIGNORE)
        momo_dir.ensure(self.wd)
        self.assertIn("!tools/", ignore.read_text())
        ignore.write_text("*\n# mine\n")
        momo_dir.ensure(self.wd)
        self.assertEqual(ignore.read_text(), "*\n# mine\n")


class Guide(_Workspace):
    def block(self) -> str:
        return re.search(r"```python\n(\"\"\"todo_count.*?)```", GUIDE.read_text(), re.S).group(1)

    def test_the_guides_example_works(self):
        self.assertEqual(self.block(), EXAMPLE.read_text())
        self.put("todo_count.py", self.block())
        (self.wd / "src").mkdir()
        (self.wd / "src" / "a.py").write_text("# TODO one\nx = 1  # FIXME two\n")
        (self.wd / "README.md").write_text("TODO three\n")
        self.approve("todo_count")
        self.assertEqual(self.call("todo_count", path="src"),
                         "2 TODO/FIXME comments in 1 files:\n   2  src/a.py")
        self.assertTrue(self.call("todo_count", path="../..").startswith("ERROR"))
        self.assertIn("3 TODO/FIXME", self.call("todo_count"))

    def test_the_guide_lists_exactly_the_accepted_keys(self):
        table = re.search(r"These are the only keys:\n\n(.*?)\n\n", GUIDE.read_text(), re.S).group(1)
        keys = re.findall(r"^\| `(\w+)` \|", table, re.M)
        self.assertEqual(keys, list(workspace.KEYS))

    def test_init_writes_the_guide_once(self):
        out = workspace.init(self.wd)
        self.assertIn("README.md", out)
        readme = self.tools / "README.md"
        self.assertIn("Writing momo workspace tools", readme.read_text())
        readme.write_text("ours")
        workspace.init(self.wd)
        self.assertEqual(readme.read_text(), "ours")
        workspace.init(self.wd, force=True)
        self.assertNotEqual(readme.read_text(), "ours")
        self.assertEqual(workspace.discover(self.wd), [])     # README and requirements aren't tools


class Venv(_Workspace):
    def wheel(self) -> Path:
        """A tiny pure-Python wheel, built by hand so pip needs no network."""
        path = Path(self.tmp.name) / "wspkg-1.0-py3-none-any.whl"
        files = {"wspkg/__init__.py": "VALUE = 'from the venv'\n",
                 "wspkg-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: wspkg\nVersion: 1.0\n",
                 "wspkg-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\n"
                                              "Root-Is-Purelib: true\nTag: py3-none-any\n"}
        record = "".join(f"{n},,\n" for n in files) + "wspkg-1.0.dist-info/RECORD,,\n"
        with zipfile.ZipFile(path, "w") as z:
            for name, text in {**files, "wspkg-1.0.dist-info/RECORD": record}.items():
                z.writestr(name, text)
        return path

    def test_venv_with_requirements(self):
        (self.tools / "requirements.txt").write_text(f"# comment\n{self.wheel()}\n")
        self.put("uses_pkg.py", tool_file("uses_pkg", body="import wspkg\n    return wspkg.VALUE"))
        self.approve("uses_pkg")
        self.assertIn("No module named 'wspkg'", self.call("uses_pkg"))
        lines = []
        out = workspace.build_venv(self.wd, False, lines.append)
        if out.startswith("ERROR: could not create"):
            self.skipTest(f"no venv/ensurepip here: {out}")
        self.assertTrue(out.startswith("Installed requirements.txt"), out + "\n" + "\n".join(lines))
        self.assertTrue(any("wspkg" in ln for ln in lines))
        self.assertEqual(workspace.interpreter(self.wd, {})[1:], (".momo/tools/.venv", ""))
        self.assertEqual(self.status("uses_pkg").status, "pending")       # new Python: approve again
        self.approve("uses_pkg")
        self.assertEqual(self.call("uses_pkg"), "from the venv")
        self.assertIn("already exists", workspace.build_venv(self.wd, False, lines.append))


class InTheHarness(_Workspace):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                  mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
                  mock.patch.object(hmod, "make_client",
                                    lambda *a, **k: _Scripted("http://fake", "fake"))):
            p.start()
            self.addCleanup(p.stop)
        self.put("probe.py", tool_file("probe", body='return "probed"'))

    def harness(self):
        h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.addCleanup(h.logger.close)
        h.set_mode("coding")
        return h

    def test_commands_and_pickers(self):
        h = self.harness()
        self.assertIn("waiting for approval: probe", h.workspace_tools.summary())
        self.assertIn("✗ probe  (waiting for approval", commands.handle("/tools list", h).output)
        out = commands.handle("/tools approve probe", h).output
        self.assertIn("file:", out)
        self.assertIn("probe: approved", out)
        self.assertIn("probe", h.messages[0]["content"])
        choice = next(c for c in h.tool_choices() if c["name"] == "probe")
        self.assertEqual(choice["group"], "workspace")
        out = commands.handle("/tools roles probe chat", h).output
        self.assertIn("roles chat", out)
        self.assertNotIn("probe", [t["function"]["name"] for t in h._current_tools()])   # coding
        h2 = self.harness()                                    # settings survive a new session
        h2.set_mode("chat")
        self.assertIn("probe", [t["function"]["name"] for t in h2._current_tools()])
        self.assertIn("ERROR", commands.handle("/tools roles probe admin", h).output)
        self.assertIn("ERROR", commands.handle("/tools category probe big", h).output)
        self.assertIn("disabled", commands.handle("/tools disable probe", h).output)
        self.assertIn("Python:", commands.handle("/tools check", h).output)
        self.assertIn("README.md", commands.handle("/tools init", h).output)
        venv = commands.handle("/tools venv", h)
        self.assertIn("Installing packages runs their setup code", venv.confirm_prompt)

    def test_confirm_and_workdir_switch(self):
        self.put("careful.py", tool_file("careful", category="write", extra='"confirm": True, '))
        h = self.harness()
        commands.handle("/tools approve all", h)
        asked = []
        h._ask_user = lambda q: asked.append(q) or "n"
        _Scripted.replies = [ChatResponse(tool_calls=[ToolCall(name="careful", arguments={})]),
                             ChatResponse(content="Done.", done_reason="stop")]
        h.send("go")
        result = [m["content"] for m in h.messages if m.get("role") == "tool"][-1]
        self.assertTrue(result.startswith("ERROR: the user declined"), result)
        self.assertIn("careful", asked[0])
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        commands.handle(f"/workspace {other}", h)
        self.assertNotIn("probe", [t["function"]["name"] for t in h._current_tools()])
        commands.handle(f"/workspace {self.wd}", h)
        self.assertIn("probe", [t["function"]["name"] for t in h._current_tools()])

    def test_workspace_listing_and_pending_status(self):
        self.put("other.py", tool_file("other"))
        self.put("broken.py", "TOOL = {")
        h = self.harness()
        self.assertEqual(h.status_event().workspace_pending, ["other", "probe"])
        out = commands.handle("/tools workspace", h).output
        self.assertIn("0 approved, 2 waiting for approval", out)
        self.assertIn("? probe  waiting for approval — /tools approve probe", out)
        self.assertIn("✗ broken  ", out)
        commands.handle("/tools approve probe", h)
        commands.handle("/tools deny other", h)
        self.assertEqual(h.status_event().workspace_pending, [])
        out = commands.handle("/tools workspace", h).output
        self.assertIn("1 approved, 0 waiting", out)
        self.assertIn("✓ probe  approved · read · ", out)
        self.assertIn("✗ other  denied — /tools approve other to allow it", out)
        choices = {c["name"]: c for c in h.workspace_choices()}
        self.assertEqual(choices["probe"]["status"], "loaded")
        self.assertEqual(choices["probe"]["path"], ".momo/tools/probe.py")
        self.assertEqual(choices["other"]["hint"], "denied — /tools approve other to allow it")
        self.assertEqual(choices["broken"]["status"], "error")
        for p in self.tools.glob("*.py"):
            p.unlink()
        self.assertIn("No workspace tools", commands.handle("/tools workspace", h).output)

    def test_a_new_or_changed_tool_is_noticed_without_a_reload(self):
        h = self.harness()
        commands.handle("/tools approve probe", h)
        loads = []
        real = workspace.load_workspace_tools
        with mock.patch.object(workspace, "load_workspace_tools",
                               lambda wd: loads.append(wd) or real(wd)):
            self.assertFalse(h.check_workspace_tools())        # nothing changed
            self.put("fresh.py", tool_file("fresh"))           # the model wrote one
            self.assertTrue(h.check_workspace_tools())
            # the approval covers every file in the folder, so probe waits again too
            self.assertEqual(h.status_event().workspace_pending, ["fresh", "probe"])
            self.assertFalse(h.check_workspace_tools())
            self.assertEqual(len(loads), 1)
            commands.handle("/tools approve all", h)           # reloads itself
            self.assertEqual(h.status_event().workspace_pending, [])
            self.assertFalse(h.check_workspace_tools())
            self.assertEqual(len(loads), 2)
            # a turn's end checks too: editing an approved tool makes it pending again
            self.put("probe.py", tool_file("probe", body='return "changed"'))
            _Scripted.replies = [ChatResponse(content="Done.", done_reason="stop")]
            h.send("hi")
        self.assertEqual(h.status_event().workspace_pending, ["fresh", "probe"])
        self.assertEqual(len(loads), 3)

    def test_code_hash_skips_the_venv_without_walking_it(self):
        self.put("_helper.py", "X = 1")
        before = workspace.code_hash(self.wd)
        (self.tools / ".venv" / "lib").mkdir(parents=True)
        (self.tools / ".venv" / "lib" / "site.py").write_text("y = 2")
        (self.tools / "__pycache__").mkdir()
        (self.tools / "__pycache__" / "probe.py").write_text("z")
        self.assertEqual(workspace.code_hash(self.wd), before)
        walked = []
        real_walk = os.walk
        with mock.patch.object(workspace.os, "walk",
                               lambda root: (walked.append(f) or (f, d, n) for f, d, n in real_walk(root))):
            workspace.code_hash(self.wd)
        self.assertFalse([f for f in walked if ".venv" in f], walked)



if __name__ == "__main__":
    unittest.main()
