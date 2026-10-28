"""The tool registry (harness/tools/): one file per built-in tool, and the
tables the harness derives from the tools' fields instead of keeping by hand."""
import importlib
import inspect
import pkgutil
import re
import unittest

from harness import tools
from harness.tools import ROLES, ToolContext, builtin, dispatcher, registry


class BuiltinFiles(unittest.TestCase):
    def test_one_tool_per_file_named_after_it(self):
        seen = []
        for info in pkgutil.iter_modules(builtin.__path__):
            module = importlib.import_module(f"{builtin.__name__}.{info.name}")
            found = registry.module_tools(module)
            if info.name.startswith("_"):
                self.assertEqual(found, [], f"{info.name} is a helper, not a tool file")
                continue
            self.assertEqual([t.name for t in found], [info.name], f"{info.name}.py")
            seen.append(info.name)
        self.assertEqual(sorted(seen), sorted(t.name for t in registry.builtins()))

    def test_names_and_orders_are_unique(self):
        orders = [t.order for t in registry.builtins()]
        self.assertEqual(len(orders), len(set(orders)))

    def test_schemas_are_well_formed(self):
        for t in registry.builtins():
            for schema in (t.schema, *t.variants.values()):
                with self.subTest(tool=t.name):
                    fn = schema["function"]
                    self.assertEqual(fn["name"], t.name)
                    self.assertRegex(fn["name"], r"^[a-z][a-z0-9_]+$")
                    self.assertTrue(fn["description"].strip())
                    params = fn["parameters"]
                    self.assertLessEqual(set(params.get("required", ())), set(params["properties"]))

    def test_executors_take_what_they_are_given(self):
        for t in registry.builtins():
            with self.subTest(tool=t.name):
                self.assertTrue(set(t.modes) <= set(ROLES))
                if t.harness:
                    self.assertIsNone(t.run, "the harness loop answers it, dispatch never runs it")
                    continue
                params = inspect.signature(t.run).parameters
                for key in ("workdir", *t.inject):
                    self.assertIn(key, params, f"{t.name} is injected {key}")
                self.assertTrue(set(t.inject) <= set(dispatcher.STATE))
                # every schema property reaches the executor by name
                props = set(t.parameters["properties"])
                self.assertLessEqual(props, set(params), t.name)

    def test_examples_match_their_schema(self):
        for t in registry.builtins():
            for key, schema in [("", t.schema), *t.variants.items()]:
                example = registry.example_for(schema)
                if example is None:
                    continue
                with self.subTest(tool=t.name, variant=key):
                    props = schema["function"]["parameters"]["properties"]
                    self.assertLessEqual(set(example), set(props))


class DerivedTables(unittest.TestCase):
    """The hand-kept tables these replaced, as they were."""

    def test_keeps_green(self):
        self.assertEqual(tools.KEEPS_GREEN, (
            {t["function"]["name"] for t in tools.READ_ONLY_TOOLS + tools.CODE_NAV_TOOLS}
            | tools.INDEX_TOOL_NAMES
            | {"command_output", "stop_command", "ask_user", "complete_step", "scratch_write",
               "find_tools"}))

    def test_diff_locked_and_index_tables(self):
        self.assertEqual(tools.DIFF_TOOLS, {"edit_file", "append_to_file", "replace_symbol",
                                            "write_file", "delete_file", "move_file", "edit_files"})
        self.assertEqual(tools.LOCKED_TOOLS, {"create_plan", "complete_step", "revise_plan"})
        self.assertEqual(tools.INDEX_REPLACES, {"find_references", "file_dependencies"})
        self.assertEqual(tools.NET_TOOL_NAMES, {"fetch_url", "web_search", "add_search_source"})
        self.assertEqual(tools.RUN_STORE_TOOLS, {"run_command", "command_output", "stop_command"})

    def test_groups(self):
        groups = {t.name: t.group for t in registry.builtins()}
        self.assertEqual({n for n, g in groups.items() if g == "ask & plan"},
                         {"ask_user", "create_plan", "complete_step", "revise_plan", "find_tools"})
        self.assertEqual(groups["scratch_write"], "edit & shell")


class Offered(unittest.TestCase):
    def names(self, **kw):
        return [t["function"]["name"] for t in tools.offered(**kw)]

    def test_gating(self):
        self.assertNotIn("fetch_url", self.names(role="chat"))
        self.assertIn("fetch_url", self.names(role="chat", net_on=True))
        self.assertNotIn("command_output", self.names(role="coding", run_mode="classic"))
        self.assertIn("command_output", self.names(role="plan", run_mode="new"))
        self.assertNotIn("command_output", self.names(role="design", run_mode="new"))
        self.assertNotIn("edit_files", self.names(role="coding", edit_features=frozenset()))
        self.assertIn("create_plan", self.names(role="plan"))
        self.assertNotIn("create_plan", self.names(role="plan-exec"))

    def test_variants(self):
        def schema(name, **kw):
            return next(t for t in tools.offered(**kw) if t["function"]["name"] == name)
        tdd = schema("create_plan", role="plan", tdd=True)
        self.assertIn("test_command", tdd["function"]["parameters"]["properties"])
        self.assertNotIn("test_command", schema("create_plan", role="plan")["function"]
                         ["parameters"]["properties"])
        classic = schema("run_command", role="coding", run_mode="classic")
        self.assertNotIn("tail", classic["function"]["parameters"]["properties"])

    def test_every_offered_tool_is_in_the_universe(self):
        for role in ROLES:
            for index_on in (False, True):
                ctx = ToolContext(role=role, index_on=index_on, net_on=True,
                                  edit_features=tools.EDIT_FEATURES)
                offered = {t["function"]["name"] for t in registry.offered(ctx)}
                universe = {t["function"]["name"] for t in registry.universe(ctx)}
                with self.subTest(role=role, index_on=index_on):
                    self.assertLessEqual(offered, universe)

    def test_dispatch_refuses_harness_tools(self):
        from pathlib import Path
        for name in ("ask_user", "create_plan", "complete_step"):
            self.assertEqual(tools.dispatch(name, {}, Path(".")), f"ERROR: unknown tool '{name}'")


if __name__ == "__main__":
    unittest.main()


class MinimalReference(unittest.TestCase):
    """/tool-ref minimal: only how to write a call; no line per tool."""

    def test_no_tool_lines(self):
        offered = tools.offered("coding")
        text = tools.render_tool_reference(offered, "minimal")
        self.assertIn("<tool_call>", text)
        self.assertIn("`path` before `content`", text)
        for t in offered:
            self.assertNotIn(t["function"]["name"], text)
        self.assertLess(len(text), len(tools.render_tool_reference(offered, "compact")) // 5)
        self.assertEqual(tools.render_tool_reference([], "minimal"), "")
