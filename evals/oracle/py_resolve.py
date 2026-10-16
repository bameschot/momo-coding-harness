"""Python resolution oracle: which file an import means, and which project
function a call reaches — computed from `ast` and Python's own import rules,
then compared with the code index's import graph and index_callers.

The extraction oracles (oracle_bench.py) check each file on its own; this one
checks what the index makes of a whole project.  Ground truth:

  import roots   the project root, src/, the folder above every top-level
                 package, and a script's own folder (a folder without
                 __init__.py: Python puts the script's folder on sys.path)
  import edges   `import a.b` -> a/b.py; `from p import n` -> p/n.py when n is a
                 module, else p's own file; relative imports by their level.
                 Edges to the packages on the way (a/__init__.py) are allowed,
                 not required.
  re-exports     `from pkg import f` where pkg/__init__.py got f from .impl:
                 the file that defines f is a separate, stricter tally.
  calls          a call whose callee is bound by an import, or by a top-level
                 def/class of the module itself, resolves to that definition;
                 a name bound inside the calling function is local; anything
                 else (obj.f(), parameters, dynamic) is unknown and not scored.

Index side, for each sampled (defining file, function): exactly what
index_callers("module.function") computes — query_target, _module_scope,
uses_of — and its (call) uses.
"""
from __future__ import annotations

import ast
import random
import warnings
from pathlib import Path

from harness import ignore_rules
from harness import code_index as ci


# ── the project's modules ────────────────────────────────────────────────────

def _parent(rel: str) -> str:
    """'a/b/c.py' -> 'a/b'; a top-level path -> ''."""
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


class Project:
    def __init__(self, root: Path, rels: list[str]):
        self.root = root
        self.rels = rels
        self.files = set(rels)
        roots = {"", "src"} if (root / "src").is_dir() else {""}
        for rel in rels:
            d, top = _parent(rel), None
            while d and (root / d / "__init__.py").is_file():
                top, d = d, _parent(d)
            if top is not None:
                roots.add(_parent(top))
        self.roots = sorted(roots, key=lambda r: (r != "", r != "src", r))
        # A file's module name is its path under the innermost root holding it:
        # a nested project (evals/x/project/shop/cart.py) is run from its own root.
        self._name: dict[str, str] = {}
        for rel in rels:
            for r in self._holding(rel) + [""]:
                parts = (rel[len(r) + 1:] if r else rel)[:-3].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if parts and all(p.isidentifier() for p in parts):
                    self._name[rel] = ".".join(parts)
                    break

    def _holding(self, rel: str) -> list[str]:
        """The non-empty roots a path lies under, innermost first."""
        d = _parent(rel)
        return sorted((r for r in self.roots if r and (d == r or d.startswith(r + "/"))),
                      key=len, reverse=True)

    def is_package_file(self, rel: str) -> bool:
        return rel.endswith("/__init__.py") or rel == "__init__.py"

    def package_of(self, rel: str) -> str | None:
        """Dotted package a file belongs to ('' for a script folder's file)."""
        name = self.module_name(rel)
        if name is None:
            return None
        if self.is_package_file(rel):
            return name
        return name.rsplit(".", 1)[0] if "." in name else ""

    def module_name(self, rel: str) -> str | None:
        return self._name.get(rel)

    def script_module(self, importer: str, name: str) -> str | None:
        """A sibling module of a script that is not in a package."""
        d = Path(importer).parent
        if (self.root / d / "__init__.py").is_file():
            return None
        rel = (d / (name.replace(".", "/") + ".py")).as_posix()
        if rel in self.files:
            return rel
        rel = (d / name.replace(".", "/") / "__init__.py").as_posix()
        return rel if rel in self.files else None

    def find(self, importer: str, dotted: str) -> str | None:
        hit = self.script_module(importer, dotted)
        if hit or not all(p.isidentifier() for p in dotted.split(".")):
            return hit
        path = dotted.replace(".", "/")
        for r in dict.fromkeys(self._holding(importer) + self.roots):
            base = f"{r}/{path}" if r else path
            for cand in (f"{base}.py", f"{base}/__init__.py"):
                if cand in self.files:
                    return cand
        return None

    def chain(self, importer: str, dotted: str) -> list[str]:
        """The package files on the way to `dotted` (a, a.b for a.b.c)."""
        parts = dotted.split(".")
        return [f for k in range(1, len(parts)) if (f := self.find(importer, ".".join(parts[:k])))]


def _absolute(proj: Project, importer: str, level: int, module: str | None) -> str | None:
    if not level:
        return module
    pkg = proj.package_of(importer)
    if pkg is None:
        return None
    parts = pkg.split(".") if pkg else []
    if level - 1 > len(parts):
        return None
    base = parts[:len(parts) - (level - 1)]
    return ".".join(base + ([module] if module else []))


# ── per-file truth ───────────────────────────────────────────────────────────

class FileTruth:
    def __init__(self):
        self.edges: set[str] = set()            # files the imports mean
        self.allowed: set[str] = set()          # packages on the way, definers
        self.definer_edges: set[str] = set()    # re-exported names -> defining file
        self.calls: dict[tuple[int, str], tuple] = {}   # (line, name) -> resolution


def _top_defs(tree) -> dict[str, str]:
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = "function"
        elif isinstance(n, ast.ClassDef):
            out[n.name] = "class"
    return out


class Resolver:
    """Parses each project file once; resolves names and re-exports."""

    def __init__(self, proj: Project):
        self.proj = proj
        self._trees: dict[str, ast.Module | None] = {}

    def tree(self, rel: str):
        if rel not in self._trees:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    src = (self.proj.root / rel).read_text(encoding="utf-8", errors="replace")
                    self._trees[rel] = ast.parse(src)
            except (SyntaxError, ValueError, OSError):
                self._trees[rel] = None
        return self._trees[rel]

    def definer(self, rel: str, name: str, depth: int = 3):
        """(file, kind) that defines `name` as seen in module `rel` — following
        `from .x import name` re-exports; None when it is not a project def."""
        tree = self.tree(rel)
        if tree is None or depth == 0:
            return None
        kind = _top_defs(tree).get(name)
        if kind:
            return rel, kind
        for n in tree.body:
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if (a.asname or a.name) == name or a.name == "*":
                        mod = _absolute(self.proj, rel, n.level, n.module)
                        if mod is None:
                            continue
                        target = self.proj.find(rel, mod)
                        if target is None:
                            continue
                        got = self.definer(target, a.name if a.name != "*" else name, depth - 1)
                        if got:
                            return got
        return None


def _bindings(proj: Project, res: Resolver, rel: str, stmts, out: dict, truth: FileTruth | None):
    """Bind the names a block's imports and defs introduce (not nested defs'
    bodies).  Values: ('module', file) | ('def', file, name, kind) | ('ext',) |
    ('local',)."""
    for n in stmts:
        if isinstance(n, ast.Import):
            for a in n.names:
                f = proj.find(rel, a.name)
                if truth is not None and f is not None:
                    truth.edges.add(f)
                    truth.allowed.update(proj.chain(rel, a.name))
                if a.asname:
                    out[a.asname] = ("module", f) if f else ("ext",)
                else:
                    top = a.name.split(".")[0]
                    tf = proj.find(rel, top)
                    out[top] = ("module", tf, a.name) if tf else ("ext",)
        elif isinstance(n, ast.ImportFrom):
            if n.module == "__future__":
                continue
            mod = _absolute(proj, rel, n.level, n.module)
            base = proj.find(rel, mod) if mod else None
            if truth is not None and base is not None:
                truth.allowed.update(proj.chain(rel, mod))
            for a in n.names:
                local = a.asname or a.name
                if a.name == "*":
                    if truth is not None and base is not None:
                        truth.edges.add(base)
                    continue
                sub = proj.find(rel, f"{mod}.{a.name}") if mod else None
                if sub is not None:
                    if truth is not None:
                        truth.edges.add(sub)
                        if base is not None:
                            truth.allowed.add(base)
                    out[local] = ("module", sub)
                    continue
                if base is None:
                    out[local] = ("ext",)
                    continue
                if truth is not None:
                    truth.edges.add(base)
                d = res.definer(base, a.name)
                if d is not None:
                    if truth is not None and d[0] != base:
                        truth.definer_edges.add(d[0])
                        truth.allowed.add(d[0])
                    out[local] = ("def", d[0], a.name, d[1])
                else:
                    out[local] = ("local",)      # a variable re-exported, or unknown: not scored
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = ("def", rel, n.name, "class" if isinstance(n, ast.ClassDef) else "function")
        elif isinstance(n, (ast.If, ast.Try, ast.With, ast.For, ast.While)) or (
                hasattr(ast, "TryStar") and isinstance(n, ast.TryStar)):
            blocks = [getattr(n, f, []) for f in ("body", "orelse", "finalbody")]
            blocks += [h.body for h in getattr(n, "handlers", [])]
            for b in blocks:
                _bindings(proj, res, rel, b, out, truth)


def _function_locals(fn) -> set[str]:
    """Names bound in a function's own scope (params, assignments, loops,
    with/except targets, nested defs, imports) minus `global` ones."""
    names, glob = set(), set()
    a = fn.args
    for arg in a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]:
        names.add(arg.arg)
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
            continue                    # its body is its own scope
        if isinstance(n, ast.Lambda):
            continue
        if isinstance(n, ast.Global):
            glob.update(n.names)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update((x.asname or x.name).split(".")[0] for x in n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        stack.extend(ast.iter_child_nodes(n))
    return names - glob


def _attr_chain(node) -> list[str] | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


def file_truth(proj: Project, res: Resolver, rel: str) -> FileTruth | None:
    tree = res.tree(rel)
    if tree is None:
        return None
    truth = FileTruth()
    module_scope: dict = {}
    _bindings(proj, res, rel, tree.body, module_scope, truth)
    # Imports inside functions are edges too (the index counts them).
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _bindings(proj, res, rel, n.body, {}, truth)

    def resolve(func, scopes):
        chain = _attr_chain(func)
        if chain is None:
            return None
        head, rest = chain[0], chain[1:]
        for local_names, local_binds in reversed(scopes):
            if head in local_binds:
                b = local_binds[head]
                break
            if head in local_names:
                # f() with a local f is not the module's f; obj.f() on a local
                # object is unknown (self.json may well be the json module).
                return ("local",) if not rest else None
        else:
            b = module_scope.get(head)
        if b is None:
            return None
        if b[0] in ("ext", "local"):
            return b
        if b[0] == "def":
            return b if not rest else None          # Cls.method(): not scored
        # a module: walk the attribute path through submodules
        cur = b[1]
        dotted = b[2] if len(b) > 2 else proj.module_name(cur)
        if len(b) > 2:                               # `import a.b` binds a; a.b.f()
            path = dotted.split(".")
            if rest[:len(path) - 1] != path[1:]:
                return None
            rest = rest[len(path) - 1:]
            cur = proj.find(rel, dotted)
        while len(rest) > 1 and dotted:
            nxt = proj.find(rel, f"{dotted}.{rest[0]}")
            if nxt is None:
                return None
            dotted, cur, rest = f"{dotted}.{rest[0]}", nxt, rest[1:]
        if len(rest) != 1 or cur is None:
            return None
        d = res.definer(cur, rest[0])
        return ("def", d[0], rest[0], d[1]) if d else None

    def visit(node, scopes):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                binds: dict = {}
                _bindings(proj, res, rel, [s for s in ast.walk(child) if isinstance(s, (ast.Import, ast.ImportFrom))],
                          binds, None)
                visit(child, scopes + [(_function_locals(child), binds)])
                continue
            if isinstance(child, ast.Call):
                f = child.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name is not None:
                    line = f.lineno if isinstance(f, ast.Name) else f.end_lineno
                    r = resolve(f, scopes)
                    if r is not None:
                        truth.calls[(line, name)] = r
            visit(child, scopes)
    visit(tree, [])
    return truth


# ── comparison ───────────────────────────────────────────────────────────────

def run(root: Path, limit: int, show: int, Tally) -> list[str]:
    import time
    t0 = time.perf_counter()
    root = root.expanduser().resolve()
    idx = ci.ProjectIndex(root, max_bytes=2 << 30, filter_text=ignore_rules.seed_text(root))
    idx.start()
    if idx.wait_fresh():
        return [f"python-resolve {root}: the index did not build"]
    with idx._lock:
        fid_of = {e.path: fid for fid, e in idx._alive() if e.lang == "python"}
        graph = {idx.files[f].path: {idx.files[d].path for d in ds}
                 for f, ds in ci.graph._import_graph(idx).items()}
    build_s = time.perf_counter() - t0
    proj = Project(root, sorted(fid_of))
    res = Resolver(proj)
    t_edges, t_defs, t_calls = Tally(), Tally(), Tally()
    truths: dict[str, FileTruth] = {}
    for rel in proj.rels:
        tr = file_truth(proj, res, rel)
        if tr is None:
            continue
        truths[rel] = tr
        got = {g for g in graph.get(rel, set()) if g.endswith(".py")} - {rel}
        want = tr.edges - {rel}
        t_edges.add(want, got - (tr.allowed - want), rel)
        t_defs.add(tr.definer_edges - {rel}, got & tr.definer_edges, rel)
    # calls: sample (defining file, function) pairs that the oracle sees called
    called: dict[tuple[str, str], set] = {}
    for rel, tr in truths.items():
        for (line, name), r in tr.calls.items():
            # One-letter names are not in the occurrence index, by design.
            if r[0] == "def" and r[3] == "function" and len(r[2]) > 1:
                called.setdefault((r[1], r[2]), set()).add((rel, line))
    pairs = sorted(called)
    random.Random(7).shuffle(pairs)
    for definer, fname in pairs[:limit]:
        modname = proj.module_name(definer)
        if modname is None:
            continue
        dotted = f"{modname}.{fname}" if modname else fname
        bare, recv, cls = ci.query_target(idx, dotted)
        module = ci.callers._module_scope(idx, recv, bare) if recv else None
        if module is not None:
            recv = None
        uses, _defs = ci.uses_of(idx, bare, recv, None, cls, module)
        got = {(u[0], u[1]) for u in uses if u[2] == "call"}
        want = called[(definer, fname)]
        # Precision only over calls the oracle resolved: a hit the oracle
        # attributes elsewhere is wrong; one it cannot resolve is not scored.
        scored = set()
        for rel, line in got:
            r = truths.get(rel, FileTruth()).calls.get((line, fname))
            if r is not None:
                scored.add((rel, line))
        t_calls.add(want, scored, f"{dotted}@")
    idx.stop()
    out = [f"python-resolve {root} — {len(truths)} files, {len(proj.roots)} import roots, "
           f"index built in {build_s:.1f}s, {time.perf_counter() - t0:.1f}s in all",
           t_edges.line("import edges"), t_defs.line("re-export -> definer"),
           t_calls.line(f"calls ({min(limit, len(pairs))} fns)")]
    for label, t in (("import edges", t_edges), ("re-export -> definer", t_defs), ("calls", t_calls)):
        if not show:
            break
        if t.missed:
            out.append(f"    {label} the index missed, e.g.:")
            out += [f"      {x}" for x in t.missed[:show]]
        if t.extra:
            out.append(f"    {label} only the index has, e.g.:")
            out += [f"      {x}" for x in t.extra[:show]]
    return out
