"""Import resolution by the language's own rules, where the project spells
them out — so an import means the file the interpreter would load, not every
file with a matching name.  Pure string work over the indexed paths."""
from __future__ import annotations

import json
import posixpath
import re
import sys


def _parent(rel: str) -> str:
    """'a/b/c.py' -> 'a/b'; a top-level path -> ''."""
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


class PyModules:
    """Python's view of the project.  Import roots: the project root, src/,
    the folder above every top-level package, and — for a script outside any
    package — the script's own folder (Python puts it on sys.path).  A file in
    a nested project (evals/x/project/shop/...) sees its own root first."""

    def __init__(self, rels):
        self.files = set(rels)
        self.pkg_dirs = {_parent(r) for r in self.files if r.rsplit("/", 1)[-1] == "__init__.py"}
        roots = {""}
        if any(r.startswith("src/") for r in self.files):
            roots.add("src")
        for rel in self.files:
            d, top = _parent(rel), None
            while d and d in self.pkg_dirs:
                top, d = d, _parent(d)
            if top is not None:
                roots.add(_parent(top))
        self.roots = sorted(roots, key=lambda r: (r != "", r != "src", r))
        self._order: dict[str, list[str]] = {}

    def order(self, importer: str) -> list[str]:
        d = _parent(importer)
        got = self._order.get(d)
        if got is None:
            first = [] if d in self.pkg_dirs else [d]
            inside = sorted((r for r in self.roots if r and (d == r or d.startswith(r + "/"))),
                            key=len, reverse=True)
            got = list(dict.fromkeys(first + inside + self.roots))
            self._order[d] = got
        return got

    def find(self, importer: str, dotted: str) -> str | None:
        """The file `import dotted` loads when run from `importer`, or None."""
        if not dotted or not all(p.isidentifier() for p in dotted.split(".")):
            return None
        path = dotted.replace(".", "/")
        for r in self.order(importer):
            base = f"{r}/{path}" if r else path
            for cand in (f"{base}.py", f"{base}/__init__.py", f"{base}.pyi"):
                if cand in self.files:
                    return cand
        return None

    def resolve(self, importer: str, module: str, names: list[str]) -> set[str] | None:
        """Files an absolute import means: `from p import n` is p/n.py when n is
        a module, else p's own file; `import a.b.c` is the longest prefix that
        exists.  None when no part of it is in the project: a library, or a
        folder put on sys.path at run time — the caller decides what then."""
        if names:
            base = self.find(importer, module)
            out, loose = set(), False
            for n in names:
                sub = self.find(importer, f"{module}.{n}") if n != "*" else None
                if sub:
                    out.add(sub)
                else:
                    loose = True
            if base and (loose or not out):
                out.add(base)
            return out or None
        parts = module.split(".")
        for k in range(len(parts), 0, -1):
            hit = self.find(importer, ".".join(parts[:k]))
            if hit:
                return {hit}
        return None


def is_stdlib(module: str) -> bool:
    """`json`, `os.path`: the standard library, never a project file."""
    return module.split(".", 1)[0] in sys.stdlib_module_names


# ── JavaScript / TypeScript: tsconfig paths and local packages ───────────────

def loose_json(text: str):
    """JSON as tsconfig.json writes it: // and /* */ comments, trailing commas.
    None when it still does not parse."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1:i + 2])
                i += 2
                continue
            in_str = c != '"'
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    try:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))
    except ValueError:
        return None


def _join(*parts: str) -> str | None:
    """Join project-relative path parts; None when it leaves the project."""
    p = posixpath.normpath("/".join(x for x in parts if x not in ("", ".")))
    return None if p.startswith("..") else ("" if p == "." else p)


class JsConfig:
    """What a bare specifier means where the project says so: tsconfig.json /
    jsconfig.json `compilerOptions.paths` and `baseUrl` (through `extends`), and
    the `name` of every package.json in the project (workspaces, monorepos)."""

    def __init__(self, files, read):
        self.configs: dict[str, tuple] = {}         # dir -> (baseUrl dir | None, paths base, [(pattern, [targets])])
        self.packages: list[tuple[str, str, str]] = []   # (name, dir, entry)
        for rel in files:
            base = rel.rsplit("/", 1)[-1]
            if base in ("tsconfig.json", "jsconfig.json"):
                opts = self._options(rel, read, 3)
                if opts is not None and (opts[0] is not None or opts[2]):
                    self.configs.setdefault(_parent(rel), opts)
            elif base == "package.json":
                data = loose_json(read(rel) or "")
                if isinstance(data, dict) and isinstance(data.get("name"), str):
                    self.packages.append((data["name"], _parent(rel), _entry(data)))
        self.packages.sort(key=lambda p: -len(p[0]))

    def _options(self, rel: str, read, depth: int):
        data = loose_json(read(rel) or "")
        if not isinstance(data, dict):
            return None
        base_url, paths_base, paths = None, None, []
        ext = data.get("extends")
        for parent in ([ext] if isinstance(ext, str) else ext if isinstance(ext, list) else []):
            if depth and isinstance(parent, str) and parent.startswith("."):
                prel = _join(_parent(rel), parent if parent.endswith(".json") else parent + ".json")
                got = self._options(prel, read, depth - 1) if prel else None
                if got is not None:
                    base_url, paths_base, paths = got
        co = data.get("compilerOptions")
        if isinstance(co, dict):
            if isinstance(co.get("baseUrl"), str):
                base_url = _join(_parent(rel), co["baseUrl"])
            if isinstance(co.get("paths"), dict):
                paths = [(k, [t for t in v if isinstance(t, str)])
                         for k, v in co["paths"].items() if isinstance(v, list)]
                paths_base = base_url if base_url is not None else _parent(rel)
        return base_url, paths_base if paths_base is not None else _parent(rel), paths

    def candidates(self, importer: str, spec: str) -> list[str]:
        """Project paths (without extension) a non-relative specifier may mean."""
        out: list[str] = []
        d = _parent(importer)
        while True:
            cfg = self.configs.get(d)
            if cfg is not None:
                base_url, paths_base, paths = cfg
                for pattern, targets in paths:
                    pre, star, post = pattern.partition("*")
                    if star and spec.startswith(pre) and spec.endswith(post) \
                            and len(spec) >= len(pre) + len(post):
                        mid = spec[len(pre):len(spec) - len(post)]
                        out += [p for t in targets if (p := _join(paths_base, t.replace("*", mid)))]
                    elif not star and spec == pattern:
                        out += [p for t in targets if (p := _join(paths_base, t))]
                if base_url is not None and (p := _join(base_url, spec)) is not None:
                    out.append(p)
                break
            if not d:
                break
            d = _parent(d)
        for name, pdir, entry in self.packages:
            if spec == name:
                out += [p for e in (entry, "src/index", "index") if (p := _join(pdir, e))]
            elif spec.startswith(name + "/"):
                sub = spec[len(name) + 1:]
                out += [p for e in (sub, f"src/{sub}") if (p := _join(pdir, e))]
        return out


def _entry(pkg: dict) -> str:
    """A package.json's entry file: exports["."], module, main, or index."""
    exp = pkg.get("exports")
    if isinstance(exp, dict):
        exp = exp.get(".", exp)
    while isinstance(exp, dict):        # {"import": ..., "require": ..., "default": ...}
        exp = next((v for k, v in exp.items() if k in ("import", "default", "require", "types")), None)
    for v in (exp, pkg.get("module"), pkg.get("main")):
        if isinstance(v, str) and v:
            return re.sub(r"\.(c|m)?js$", "", v.lstrip("./")) if v.startswith("./") else v
    return "index"


# ── Rust: crate, self and super paths ────────────────────────────────────────

def rust_candidates(importer: str, module: str, names: list[str], files) -> list[str] | None:
    """Files a `use crate::a::b` / `use super::x` / `mod foo;` (`self::foo`)
    means, longest module path first; None for a path that starts with an
    external crate's name (left to the textual match)."""
    parts = module.split("::")
    if parts[0] == "crate":
        base, rest = _rust_crate_dir(importer, files), parts[1:]
    elif parts[0] in ("self", "super"):
        base = _rust_module_dir(importer)
        k = 0 if parts[0] == "self" else sum(1 for p in parts if p == "super")
        for _ in range(k):
            base = _parent(base)
        rest = parts[1:] if parts[0] == "self" else parts[k:]
    else:
        return None
    if base is None:
        return []
    out: list[str] = []
    for n in names:                     # use crate::a::{b, c}: b and c may be modules
        out += _rust_module_files(base, rest + [n], files)
    for k in range(len(rest), -1, -1):
        hit = _rust_module_files(base, rest[:k], files)
        if hit:
            return out + hit
    return out


def _rust_module_dir(rel: str) -> str:
    """The folder a module's children live in: src/a/b.rs -> src/a/b,
    src/a/mod.rs or src/lib.rs -> src/a or src."""
    stem = rel.rsplit("/", 1)[-1][:-3]
    return _parent(rel) if stem in ("mod", "lib", "main") else rel[:-3]


def _rust_crate_dir(rel: str, files) -> str | None:
    d = _parent(rel)
    while True:
        if f"{d}/Cargo.toml" in files or (not d and "Cargo.toml" in files):
            return _join(d, "src")
        if any((f"{d}/{r}" if d else r) in files for r in ("lib.rs", "main.rs")):
            return d
        if not d:
            return None
        d = _parent(d)


def _rust_module_files(base: str, path: list[str], files) -> list[str]:
    if not path:
        cands = [f"{base}/lib.rs", f"{base}/main.rs", f"{base}/mod.rs", f"{base}.rs"]
    else:
        p = _join(base, *path)
        cands = [f"{p}.rs", f"{p}/mod.rs"] if p else []
    return [c for c in cands if c in files][:1]
