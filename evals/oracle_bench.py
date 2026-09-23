#!/usr/bin/env python3
"""Check the code index against independent oracles on REAL code.

evals/lang_bench.py scores hand-written fixtures against hand-written ground
truth — both written by the same person who wrote the extractor.  This compares
the index with sources it has no say in:

  python   the interpreter's own `ast` (definitions, call sites, imports) and
           `symtable` (which names are local to each function)
  kotlin   the Kotlin 2.2 compiler's own parser (PSI), via evals/oracle/KtOracle.java
           (jars: `--fetch-kotlin` downloads them into evals/.cache/ktoracle)
  c-clang  clang's own AST (evals/oracle/clang_c.py): definitions and call sites of
           every C file clang compiles, preprocessor and all — so it also measures
           the files tree-sitter cannot parse cleanly (macro-heavy C)
  others   each grammar's own tags.scm query (definitions and call references),
           shipped with the tree-sitter wheel by the grammar's maintainers

Every disagreement is either an index bug or an oracle quirk; the report lists
samples of each kind so they can be triaged.  No model involved.

    python evals/oracle_bench.py                           # default corpora
    python evals/oracle_bench.py --python ~/src/proj --limit 300 --show 15
    python evals/oracle_bench.py --tags ~/src/cproj --langs c cpp
    python evals/oracle_bench.py --langs c-clang --clang ~/projects/mgba --limit 1000
"""
from __future__ import annotations

import argparse
import ast
import importlib
import os
import random
import symtable
import re
import sys
import sysconfig
import time
import warnings
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from harness import code_nav  # noqa: E402

SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "target", "build", "dist",
        "site-packages", ".tox"}


def files_under(roots, exts, limit, seed=7, max_bytes=400_000) -> list[Path]:
    out = []
    for root in roots:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")]
            for f in filenames:
                if f.endswith(exts):
                    p = Path(dirpath) / f
                    try:
                        if 0 < p.stat().st_size < max_bytes:
                            out.append(p)
                    except OSError:
                        pass
    out.sort()
    random.Random(seed).shuffle(out)          # a spread sample, the same every run
    return out[:limit]


class Tally:
    def __init__(self):
        self.tp = self.fn = self.fp = 0
        self.missed: list[str] = []
        self.extra: list[str] = []

    def add(self, truth: set, got: set, where: str):
        self.tp += len(truth & got)
        miss, extra = truth - got, got - truth
        self.fn += len(miss)
        self.fp += len(extra)
        self.missed += [f"{where}:{x}" for x in sorted(miss, key=str)[:3]]
        self.extra += [f"{where}:{x}" for x in sorted(extra, key=str)[:3]]

    def line(self, label: str) -> str:
        r = self.tp / max(self.tp + self.fn, 1)
        p = self.tp / max(self.tp + self.fp, 1)
        return f"  {label:22s} recall {r * 100:5.1f}%  precision {p * 100:5.1f}%   (agree {self.tp}, " \
               f"index missed {self.fn}, index extra {self.fp})"


# ── Python: ast + symtable ───────────────────────────────────────────────────

def _ast_defs(tree) -> set:
    """(qualname, def line) for functions/classes, module/class-level names."""
    out = set()

    def visit(node, prefix, in_func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}.{child.name}" if prefix else child.name
                out.add((q, child.lineno))
                visit(child, q, isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))
            elif not in_func and isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        out.add((f"{prefix}.{t.id}" if prefix else t.id, child.lineno))
            elif isinstance(child, (ast.If, ast.Try, ast.With, ast.For, ast.While)) or (
                    hasattr(ast, "TryStar") and isinstance(child, ast.TryStar)):
                visit(child, prefix, in_func)
            elif in_func:
                visit(child, prefix, in_func)
            elif isinstance(child, (ast.ExceptHandler, ast.match_case)) if hasattr(ast, "match_case") else False:
                visit(child, prefix, in_func)
    visit(tree, "", False)
    return out


def _ast_calls(tree) -> set:
    """(called name, line of the name) — `f(x)` -> f, `a.b.f(x)` -> f."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add((f.id, f.lineno))
            elif isinstance(f, ast.Attribute):
                out.add((f.attr, f.end_lineno))
    return out


def _ast_imports(tree) -> set:
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add((n.lineno, a.name, ()))
        elif isinstance(n, ast.ImportFrom):
            mod = "." * n.level + (n.module or "")
            out.add((n.lineno, mod, tuple(sorted(a.name for a in n.names))))
    return out


def _index_calls(parsed) -> set:
    """Call sites as the index classifies them.  A local that is called (a
    parameter holding a function) is still a call site."""
    out = set()
    stack = [parsed.tree.root_node]
    while stack:
        n = stack.pop()
        if n.child_count:
            stack.extend(n.children)
            continue
        if n.type != "identifier" or n.parent is None:
            continue
        p = n.parent
        if p.type not in code_nav._CALL_NODES and p.type not in code_nav._MEMBER_NODES:
            continue
        role = code_nav._classify(n, "python", False)[0]
        if role == "call" or (role == "local" and p.type in code_nav._CALL_NODES
                              and code_nav._is_callee(n)):
            out.add((code_nav._text(n), n.start_point[0] + 1))
    return out


def _func_tables(st) -> dict:
    """{def line: symtable of that function} for every function in a module."""
    out = {}
    stack = [st]
    while stack:
        t = stack.pop()
        if t.get_type() == "function":
            out.setdefault(t.get_lineno(), t)
        stack.extend(t.get_children())
    return out


def _local_agreement(parsed, src: str, fname: str, tally: Tally, rel: str):
    """For every bare-name use inside a function, does the index's `local`
    verdict match symtable's?"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tables = _func_tables(symtable.symtable(src, fname, "exec"))
    except (SyntaxError, ValueError):
        return
    truth, got = set(), set()
    stack = [parsed.tree.root_node]
    while stack:
        n = stack.pop()
        if n.child_count:
            stack.extend(n.children)
            continue
        if n.type != "identifier" or n.parent is None or n.parent.type in code_nav._MEMBER_NODES \
                and code_nav._field_of(n) not in code_nav._RECEIVER_FIELDS:
            continue
        # innermost enclosing def / lambda; a def's / class's own NAME belongs
        # to the scope around it
        f = n.parent
        if code_nav._field_of(n) == "name" and f.type in ("function_definition", "class_definition"):
            f = f.parent
        while f is not None and f.type not in ("function_definition", "lambda"):
            f = f.parent
        if f is None or f.type != "function_definition":
            continue    # module level, or inside a lambda (symtable keys lambdas by line only)
        table = tables.get(f.start_point[0] + 1)
        if table is None:
            dec = f.parent if f.parent is not None and f.parent.type == "decorated_definition" else None
            table = tables.get(dec.start_point[0] + 1) if dec is not None else None
        if table is None:
            continue
        name = code_nav._text(n)
        try:
            sym = table.lookup(name)
            # A free variable is a closure over an enclosing function's local:
            # for the index that is a local too (not a use of a same-named global).
            is_local = sym.is_local() or sym.is_parameter() or sym.is_free()
        except KeyError:
            is_local = False
        key = (name, n.start_point[0] + 1, n.start_point[1])
        if is_local:
            truth.add(key)
        if code_nav._is_local(n):
            got.add(key)
    tally.add(truth, got, rel)


def run_python(paths: list[Path], show: int) -> list[str]:
    t_defs, t_calls, t_imps, t_locals = Tally(), Tally(), Tally(), Tally()
    n_ok = n_err = 0
    t0 = time.perf_counter()
    for p in paths:
        src = p.read_text(encoding="utf-8", errors="replace")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(src)
        except (SyntaxError, ValueError):
            n_err += 1
            continue
        parsed = code_nav.parse_uncached(p)
        if parsed is None or parsed.tree.root_node.has_error:
            n_err += 1
            continue
        n_ok += 1
        rel = str(p)
        t_defs.add(_ast_defs(tree), {(s.qualname, s.name_line) for s in parsed.symbols}, rel)
        t_calls.add(_ast_calls(tree), _index_calls(parsed), rel)
        t_imps.add(_ast_imports(tree),
                   {(i.line, i.module, tuple(sorted(i.names))) for i in parsed.imports}, rel)
        _local_agreement(parsed, src, p.name, t_locals, rel)
    out = [f"python — {n_ok} files checked ({n_err} skipped: syntax the oracle or grammar rejects), "
           f"{time.perf_counter() - t0:.1f}s",
           t_defs.line("definitions (ast)"), t_calls.line("call sites (ast)"),
           t_imps.line("imports (ast)"), t_locals.line("locals (symtable)")]
    for label, t in (("definitions", t_defs), ("call sites", t_calls), ("imports", t_imps),
                     ("locals", t_locals)):
        if t.missed:
            out.append(f"    {label} the index missed, e.g.:")
            out += [f"      {x}" for x in t.missed[:show]]
        if t.extra:
            out.append(f"    {label} only the index has, e.g.:")
            out += [f"      {x}" for x in t.extra[:show]]
    return out


# ── other languages: the grammar's own tags.scm ──────────────────────────────

_TAG_MODULES = {"c": "tree_sitter_c", "cpp": "tree_sitter_cpp", "java": "tree_sitter_java",
                "rust": "tree_sitter_rust", "javascript": "tree_sitter_javascript",
                "typescript": "tree_sitter_typescript", "tsx": "tree_sitter_typescript",
                "python": "tree_sitter_python"}
_EXTS = {"c": (".c", ".h"), "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hh"), "java": (".java",),
         "rust": (".rs",), "javascript": (".js", ".mjs", ".cjs", ".jsx"),
         "typescript": (".ts",), "tsx": (".tsx",)}


def _tags_query(lang: str):
    from tree_sitter import Query, QueryCursor
    mod = importlib.import_module(_TAG_MODULES[lang])
    src = getattr(mod, "TAGS_QUERY", None)
    if src is None:
        qfile = Path(mod.__file__).parent / "queries" / "tags.scm"
        src = qfile.read_text() if qfile.exists() else None
    if lang in ("typescript", "tsx"):
        js = importlib.import_module("tree_sitter_javascript").TAGS_QUERY
        src = (src or "") + "\n" + js      # TS tags extend the JS ones, as in the tree-sitter CLI
    if not src:
        return None
    L = code_nav._parser(lang).language
    try:
        return QueryCursor(Query(L, src))
    except Exception as e:  # a pattern this grammar version does not accept
        print(f"  ({lang}: tags.scm does not compile against this grammar: {e})")
        return None


def _called_locals(parsed, name: bytes) -> set[int]:
    out = set()
    stack = [parsed.tree.root_node]
    while stack:
        n = stack.pop()
        if n.child_count:
            stack.extend(n.children)
        elif n.text == name and n.parent is not None and n.parent.type in code_nav._CALL_NODES \
                and code_nav._is_callee(n) and code_nav._is_local(n):
            out.add(n.start_point[0] + 1)
    return out


def _has_body(node) -> bool:
    """A function_declarator inside a function_definition, or a class/struct
    specifier with a body."""
    if node.type == "function_declarator":
        p = node.parent
        while p is not None and p.type in ("pointer_declarator", "reference_declarator",
                                           "parenthesized_declarator"):
            p = p.parent
        return p is not None and p.type == "function_definition"
    if node.type in ("class_specifier", "struct_specifier", "union_specifier", "enum_specifier"):
        return node.child_by_field_name("body") is not None
    return True


def run_tags(lang: str, paths: list[Path], show: int) -> list[str]:
    cursor = _tags_query(lang)
    if cursor is None:
        return [f"{lang} — no tags.scm oracle available"]
    t_defs, t_calls = Tally(), Tally()
    kinds = Counter()
    n = 0
    for p in paths:
        if code_nav.language_for(p) != lang:
            continue
        parsed = code_nav.parse_uncached(p)
        if parsed is None or parsed.tree.root_node.has_error:
            continue
        n += 1
        defs, calls = set(), set()
        for _pat, caps in cursor.matches(parsed.tree.root_node):
            names = caps.get("name", [])
            for cap, nodes in caps.items():
                if cap.startswith("definition.") and names and lang in ("c", "cpp") \
                        and not all(_has_body(nd) for nd in nodes):
                    # C/C++ tags.scm also tags prototypes (`int f(void);`) and forward
                    # declarations (`class A;`); the index defines "definition" as
                    # having a body, so compare like with like.
                    continue
                if cap.startswith("definition.") and names:
                    kinds[cap] += 1
                    for nm in names:
                        defs.add((code_nav._text(nm).split("::")[-1], nm.start_point[0] + 1))
                elif cap == "reference.call" and names:
                    for nm in names:
                        calls.add((code_nav._text(nm), nm.start_point[0] + 1))
        rel = str(p)
        idx_defs = {(s.name, s.name_line) for s in parsed.symbols}
        # tags.scm only knows functions / classes / methods / modules / macros:
        # compare against the index's symbols of those kinds, the rest is extra by design.
        # Kinds tags.scm has no pattern for: namespaces, macros, constants,
        # variables, constructors — the index's extras there are by design.
        comparable = {(s.name, s.name_line) for s in parsed.symbols
                      if s.kind not in ("constant", "variable", "key", "column", "var", "namespace",
                                        "macro", "constructor", "module", "object")}
        t_defs.add(defs, comparable & (idx_defs | defs) | comparable, rel)
        idx_calls = set()
        wanted = {c[0].encode() for c in calls}
        for row_name in wanted:
            for r, (role, _recv, _t) in code_nav.references_in(parsed, row_name.decode()).items():
                if role == "call":
                    idx_calls.add((row_name.decode(), r + 1))
            # a local that is CALLED (a parameter holding a function) is a call site too
            for nd in _called_locals(parsed, row_name):
                idx_calls.add((row_name.decode(), nd))
        t_calls.add(calls, idx_calls, rel)
    out = [f"{lang} — {n} files checked against tags.scm "
           f"({', '.join(f'{k.split(chr(46))[1]} {v}' for k, v in kinds.most_common())})",
           t_defs.line("definitions (tags)"),
           t_calls.line("call sites (tags)") if t_calls.tp + t_calls.fn + t_calls.fp
           else "  call sites (tags)      — this grammar's tags.scm has no call patterns: no oracle"]
    for label, t in (("definitions", t_defs), ("call sites", t_calls)):
        if t.missed:
            out.append(f"    {label} the index missed, e.g.:")
            out += [f"      {x}" for x in t.missed[:show]]
        if t.extra:
            out.append(f"    {label} only the index has, e.g.:")
            out += [f"      {x}" for x in t.extra[:show]]
    return out


# ── Kotlin: the compiler's own parser ────────────────────────────────────────

KT_DIR = REPO / "evals/.cache/ktoracle"
_KT_JARS = {
    "org/jetbrains/kotlin/kotlin-compiler-embeddable/2.2.0": "kotlin-compiler-embeddable-2.2.0.jar",
    "org/jetbrains/kotlin/kotlin-stdlib/2.2.0": "kotlin-stdlib-2.2.0.jar",
    "org/jetbrains/kotlin/kotlin-script-runtime/2.2.0": "kotlin-script-runtime-2.2.0.jar",
    "org/jetbrains/kotlin/kotlin-reflect/1.6.10": "kotlin-reflect-1.6.10.jar",
    "org/jetbrains/kotlin/kotlin-daemon-embeddable/2.2.0": "kotlin-daemon-embeddable-2.2.0.jar",
    "org/jetbrains/kotlinx/kotlinx-coroutines-core-jvm/1.8.0": "kotlinx-coroutines-core-jvm-1.8.0.jar",
    "org/jetbrains/intellij/deps/trove4j/1.0.20200330": "trove4j-1.0.20200330.jar",
    "org/jetbrains/annotations/13.0": "annotations-13.0.jar",
}


def fetch_kotlin() -> None:
    import urllib.request
    KT_DIR.mkdir(parents=True, exist_ok=True)
    for path, jar in _KT_JARS.items():
        if not (KT_DIR / jar).exists():
            print(f"  downloading {jar}")
            urllib.request.urlretrieve(f"https://repo1.maven.org/maven2/{path}/{jar}", KT_DIR / jar)


def _kt_oracle(paths: list[Path]) -> dict[str, dict]:
    import subprocess
    cp = ":".join(str(KT_DIR / j) for j in _KT_JARS.values())
    src = REPO / "evals/oracle/KtOracle.java"
    cls = KT_DIR / "KtOracle.class"
    if not cls.exists() or cls.stat().st_mtime < src.stat().st_mtime:
        subprocess.run(["javac", "-d", str(KT_DIR), "-cp", cp, str(src)], check=True)
    listing = KT_DIR / "files.txt"
    listing.write_text("\n".join(str(p) for p in paths))
    out = subprocess.run(["java", "-cp", f"{KT_DIR}:{cp}", "KtOracle", str(listing)],
                         capture_output=True, text=True, check=True).stdout
    res: dict[str, dict] = {}
    cur = None
    for line in out.splitlines():
        f = line.split("\t")
        if f[0] == "F":
            cur = res.setdefault(f[1], {"defs": set(), "members": set(), "calls": set(), "imports": set()})
        elif f[0] == "D":
            (cur["members"] if f[1] == "member-property" else cur["defs"]).add((f[2].strip("`"), int(f[3])))
        elif f[0] == "C":
            cur["calls"].add((f[1].strip("`"), int(f[2])))
        elif f[0] == "I":
            cur["imports"].add((int(f[2]), f[1]))
    return res


def run_kotlin(paths: list[Path], show: int) -> list[str]:
    if not all((KT_DIR / j).exists() for j in _KT_JARS.values()):
        return ["kotlin — the compiler oracle is not installed: run with --fetch-kotlin once "
                "(downloads ~61 MB of jars from Maven Central into evals/.cache/ktoracle)"]
    t0 = time.perf_counter()
    oracle = _kt_oracle(paths)
    t_defs, t_members, t_calls, t_imps = Tally(), Tally(), Tally(), Tally()
    n = skipped = 0
    for p in paths:
        o = oracle.get(str(p))
        parsed = code_nav.parse_uncached(p)
        if o is None or parsed is None or parsed.tree.root_node.has_error:
            skipped += 1
            continue
        n += 1
        rel = str(p)
        idx = {(s.name.strip("`"), s.name_line) for s in parsed.symbols}
        t_defs.add(o["defs"], {x for x in idx if x not in o["members"]}, rel)
        t_members.add(o["members"], idx & o["members"], rel)
        got_calls = set()
        for name in {c[0] for c in o["calls"]}:
            for r, (role, _recv, _t) in code_nav.references_in(parsed, name).items():
                if role == "call":
                    got_calls.add((name, r + 1))
            for ln in _called_locals(parsed, name.encode()):
                got_calls.add((name, ln))
        t_calls.add(o["calls"], got_calls, rel)
        t_imps.add(o["imports"], {(i.line, i.module) for i in parsed.imports}, rel)
    out = [f"kotlin — {n} files checked against the Kotlin 2.2 compiler's parser "
           f"({skipped} skipped: tree-sitter parse errors), {time.perf_counter() - t0:.1f}s",
           t_defs.line("definitions (PSI)"),
           t_members.line("member properties") + "   <- not indexed by design (state, like Java fields)",
           t_calls.line("call sites (PSI)"), t_imps.line("imports (PSI)")]
    for label, t in (("definitions", t_defs), ("call sites", t_calls), ("imports", t_imps)):
        if t.missed:
            out.append(f"    {label} the index missed, e.g.:")
            out += [f"      {x}" for x in t.missed[:show]]
        if t.extra:
            out.append(f"    {label} only the index has, e.g.:")
            out += [f"      {x}" for x in t.extra[:show]]
    return out


# ── C: clang's AST ───────────────────────────────────────────────────────────

def _c_index_calls(parsed) -> set:
    """Every identifier the index classifies as a call — what references_in
    reports for that name."""
    out = set()
    kw = code_nav._KEYWORDS.get("c", ())
    stack = [parsed.tree.root_node]
    while stack:
        n = stack.pop()
        if n.child_count:
            stack.extend(n.children)
            continue
        if not n.text or not code_nav._is_ident(n.type) or n.parent is None or n.text.decode() in kw:
            continue
        role = code_nav._classify(n, "c", False)[0]
        if role == "call" or (role == "local" and n.parent.type in code_nav._CALL_NODES
                              and code_nav._is_callee(n)):
            out.add((code_nav._text(n), n.start_point[0] + 1))
    return out


def _c_preprocessor_text(parsed) -> tuple[set, set, set]:
    """(#define names with their line, lines inside #define bodies, lines of
    #if / #include conditions) — text the index does not read as code."""
    names, bodies, conditions = set(), set(), set()
    stack = [parsed.tree.root_node]
    while stack:
        n = stack.pop()
        if n.type in ("preproc_def", "preproc_function_def"):
            bodies.update(range(n.start_point[0] + 1, n.end_point[0] + 2))
            nm = n.child_by_field_name("name")
            if nm is not None:
                names.add((code_nav._text(nm), nm.start_point[0] + 1))
        if n.type in ("preproc_if", "preproc_elif", "preproc_include"):
            c = n.child_by_field_name("condition") or n.child_by_field_name("path")
            if c is not None:
                conditions.update(range(c.start_point[0] + 1, c.end_point[0] + 2))
        stack.extend(n.children)
    return names, bodies, conditions


def run_clang(roots: list[str], limit: int, show: int) -> list[str]:
    """Compare with clang on every C file of `roots` that compiles without a
    build system.  What the index cannot see by design is counted apart:
    code in inactive #if branches (both sides dropped), macros (not in the AST),
    calls or definitions that only exist after macro expansion, and index
    "calls" in a macro argument the expansion drops."""
    import tempfile
    from concurrent.futures import ThreadPoolExecutor
    sys.path.insert(0, str(REPO / "evals/oracle"))
    import clang_c
    if not clang_c.available():
        return ["c-clang — no clang on PATH: the clang oracle is not available"]
    t0 = time.perf_counter()
    jobs = []
    for root in roots:
        root = Path(root).expanduser()
        if root.is_dir():
            incs = clang_c.include_dirs(root, Path(tempfile.mkdtemp(prefix="clang_stub_")))
            jobs += [(p, incs) for p in files_under([root], (".c",), limit)]
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        facts = list(pool.map(lambda j: clang_c.facts(*j), jobs))
    tally = {b: (Tally(), Tally()) for b in ("clean", "errors")}
    files = Counter()
    cat = Counter()
    for (p, _), o in zip(jobs, facts):
        if o is None:
            files["clang errors"] += 1
            continue
        parsed = code_nav.parse_uncached(p)
        bucket = "errors" if parsed.tree.root_node.has_error else "clean"
        files[bucket] += 1
        defines, bodies, conditions = _c_preprocessor_text(parsed)
        macros = o["macros"] | {m for m, ln in defines if ln not in o["inactive"]}
        skip = o["inactive"] | bodies | conditions

        def keep(x):
            return x[1] not in skip
        t_defs, t_calls = tally[bucket]
        cat["definitions made by a macro (oracle)"] += len(o["macro_defs"])
        idx_defs = {(s.name, s.name_line) for s in parsed.symbols
                    if s.name not in macros and keep((s.name, s.name_line))
                    and (s.name, s.name_line) not in o["globals"]
                    and (s.name, s.name_line) not in o["macro_defs"]}
        t_defs.add({d for d in o["defs"] if keep(d)}, idx_defs, str(p))
        truth = set()
        for c in o["calls"]:
            if c[1] in o["inactive"] or c[1] in conditions:
                continue
            if c[1] in bodies:
                cat["calls inside #define bodies (oracle)"] += 1
                continue
            line = parsed.lines[c[1] - 1] if c[1] <= len(parsed.lines) else ""
            if not re.search(r"\b" + re.escape(c[0]) + r"\s*\(", line):
                cat["calls made only by macro expansion (oracle)"] += 1
                continue
            truth.add(c)
        index_calls = _c_index_calls(parsed)
        macro_lines = {c[1] for c in index_calls if c[0] in macros}
        got = set()
        for c in index_calls:
            if not keep(c):
                continue
            if c[0] in macros:
                cat["calls of function-like macros (index)"] += 1
            elif c[1] in macro_lines and c not in o["calls"]:
                cat["calls in a macro argument the expansion drops (index)"] += 1
            else:
                got.add(c)
        t_calls.add(truth, got, str(p))
    out = [f"c — {files['clean'] + files['errors']} files checked against clang's AST "
           f"({files['errors']} of them with tree-sitter parse errors; {files['clang errors']} "
           f"skipped: clang errors without their build system), {time.perf_counter() - t0:.1f}s"]
    for bucket, label in (("clean", "parsed cleanly"), ("errors", "with parse errors")):
        t_defs, t_calls = tally[bucket]
        out += [f"  files {label}:", t_defs.line("definitions (clang)"), t_calls.line("call sites (clang)")]
        for what, t in (("definitions", t_defs), ("call sites", t_calls)):
            if t.missed:
                out.append(f"    {what} the index missed, e.g.:")
                out += [f"      {x}" for x in t.missed[:show]]
            if t.extra:
                out.append(f"    {what} only the index has, e.g.:")
                out += [f"      {x}" for x in t.extra[:show]]
    out.append("  not comparable by design: " + ", ".join(f"{k} {v}" for k, v in cat.items()))
    return out


# ── YAML: Ruby's Psych (libyaml) ─────────────────────────────────────────────

def run_yaml(paths: list[Path], show: int) -> list[str]:
    import shutil, subprocess, tempfile
    if not shutil.which("ruby"):
        return ["yaml — no ruby on PATH: the Psych oracle is not available"]
    listing = Path(tempfile.mkdtemp()) / "files.txt"
    listing.write_text("\n".join(str(p) for p in paths))
    out = subprocess.run(["ruby", str(REPO / "evals/oracle/yaml_keys.rb"), str(listing)],
                         capture_output=True, text=True, check=True).stdout
    oracle: dict[str, set | None] = {}
    cur = None
    for line in out.splitlines():
        f = line.split("\t")
        if f[0] == "F":
            cur = oracle.setdefault(f[1], set())
        elif f[0] == "E":
            oracle[list(oracle)[-1]] = None          # Psych rejects the file
        elif f[0] == "K" and cur is not None:
            cur.add((f[1], int(f[2])))
    t = Tally()
    n = rejected = 0
    for p in paths:
        truth = oracle.get(str(p))
        parsed = code_nav.parse_uncached(p)
        if truth is None or parsed is None or parsed.tree.root_node.has_error:
            rejected += 1
            continue
        if len(parsed.symbols) >= code_nav._MAX_DATA_SYMBOLS:
            rejected += 1                           # capped on purpose (generated files)
            continue
        n += 1
        t.add(truth, {(s.qualname, s.name_line) for s in parsed.symbols}, str(p))
    out_lines = [f"yaml — {n} files checked against Ruby's Psych (libyaml) ({rejected} skipped: "
                 f"rejected by a parser, or over the {code_nav._MAX_DATA_SYMBOLS}-key cap)",
                 t.line("key paths (Psych)")]
    if t.missed:
        out_lines.append("    key paths the index missed, e.g.:")
        out_lines += [f"      {x}" for x in t.missed[:show]]
    if t.extra:
        out_lines.append("    key paths only the index has, e.g.:")
        out_lines += [f"      {x}" for x in t.extra[:show]]
    return out_lines


_JS_TYPES = ("", "text/javascript", "application/javascript", "module", "text/babel")


def html_script_mirrors(roots, limit, outdir: Path) -> list[Path]:
    """Inline <script> blocks of HTML files, written to <name>.html.js mirrors
    that keep every script line at its original line and column, so results
    map straight back onto the HTML file.  Single-file web apps keep all their
    JavaScript there."""
    parser = code_nav._parser("html")
    out = []
    for html in files_under(roots, (".html", ".htm"), limit, max_bytes=3_000_000):
        raw = html.read_bytes()
        tree = parser.parse(raw)
        lines = [""] * (raw.count(b"\n") + 1)
        found = False
        stack = [tree.root_node]
        while stack:
            n = stack.pop()
            if n.type == "script_element":
                start = next((c for c in n.named_children if c.type == "start_tag"), None)
                attrs = code_nav._html_attrs(start) if start is not None else {}
                body = next((c for c in n.named_children if c.type == "raw_text"), None)
                if body is None or "src" in attrs or attrs.get("type", "").lower() not in _JS_TYPES:
                    continue
                row, col = body.start_point
                for i, text in enumerate(code_nav._text(body).split("\n")):
                    lines[row + i] = (" " * col if i == 0 else "") + text
                found = True
            else:
                stack.extend(n.children)
        if found:
            mirror = outdir / (str(html).strip("/").replace("/", "__") + ".js")
            mirror.write_text("\n".join(lines))
            out.append(mirror)
    return out


def main() -> int:
    stdlib = sysconfig.get_paths()["stdlib"]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", nargs="*", default=[stdlib, str(REPO / "harness"),
                                                    "~/projects/external/badgeware-simulator"],
                    help="Python roots to check against ast/symtable")
    ap.add_argument("--tags", nargs="*", default=["~/projects/external/badgeware-simulator",
                                                  "~/projects/external/badger2350",
                                                  "~/projects/claude-code-ws-moz-profiel",
                                                  str(REPO / "harness/web/static")],
                    help="roots for the tags.scm comparison")
    ap.add_argument("--yaml", nargs="*", default=["~/projects/Maestro"],
                    help="YAML roots to check against Ruby's Psych parser")
    ap.add_argument("--kotlin", nargs="*", default=["~/projects/Maestro"],
                    help="Kotlin roots to check against the Kotlin compiler's parser")
    ap.add_argument("--fetch-kotlin", action="store_true",
                    help="download the Kotlin compiler jars for the kotlin oracle (~61 MB)")
    ap.add_argument("--clang", nargs="*", default=["~/projects/mgba"],
                    help="C roots to check against clang's AST")
    ap.add_argument("--html", nargs="*", default=["~/projects/momo-agent-workspaces"],
                    help="roots whose HTML files' inline <script> JavaScript is checked too")
    ap.add_argument("--langs", nargs="*", default=["python", "c", "cpp", "java", "javascript",
                                                   "typescript", "html-js", "kotlin", "yaml",
                                                   "c-clang"])
    ap.add_argument("--limit", type=int, default=200, help="files per language (sampled)")
    ap.add_argument("--show", type=int, default=8, help="disagreements to print per category")
    args = ap.parse_args()
    if args.fetch_kotlin:
        fetch_kotlin()
    for lang in args.langs:
        if lang == "python":
            report = run_python(files_under(args.python, (".py",), args.limit), args.show)
        elif lang == "yaml":
            report = run_yaml(files_under(args.yaml, (".yaml", ".yml"), args.limit), args.show)
        elif lang == "kotlin":
            report = run_kotlin(files_under(args.kotlin, (".kt", ".kts"), args.limit), args.show)
        elif lang == "c-clang":
            report = run_clang(args.clang, args.limit, args.show)
        elif lang == "html-js":
            import tempfile
            tmp = Path(tempfile.mkdtemp(prefix="oracle_html_js_"))
            mirrors = html_script_mirrors(args.html, args.limit, tmp)
            report = run_tags("javascript", mirrors, args.show)
            report[0] = report[0].replace("javascript —", "html inline <script> (javascript) —", 1)
        else:
            report = run_tags(lang, files_under(args.tags, _EXTS[lang], args.limit), args.show)
        print("\n".join(report))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
