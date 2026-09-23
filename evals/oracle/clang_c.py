"""Independent C oracle: clang's own AST (`-ast-dump=json`), for oracle_bench.py.

For one C file that clang compiles cleanly it returns
  defs        (name, line) of functions with a body, structs/unions/enums with
              a body, typedefs and enum constants written in the file
  macro_defs  the same, but whose name is a macro argument: made by expansion
              (`DECL_BITFIELD(Flags, uint8_t)`), which no parser of the text sees
  calls       (name, line) of call sites whose callee name is in the file:
              `f(x)`, `obj->fn(x)`, also inside macro arguments
  globals     file-scope variables (the index lists some as constants)
and, separately, the lines inside `#if` branches the preprocessor does not take
(`inactive_lines`) and every macro name visible in the file (`macro_names`).

Only parsing and name lookup are used — no types, no linking — and only files
that compile without an error count, so a disagreement is the index's or a
documented quirk of comparing preprocessed C with source text.
"""
from __future__ import annotations

import bisect
import json
import re
import shutil
import subprocess
from pathlib import Path


def available() -> bool:
    return shutil.which("clang") is not None


_VENDOR_DIRS = {"third-party", "third_party", "thirdparty", "external", "extern", "vendor",
                "deps", "libs"}


def include_dirs(root: Path, stub_dir: Path) -> list[str]:
    """-I directories for a project without its build system: the project's
    include/ and src/, and each bundled library (third-party/zlib, vendor/x).
    Every header directory would shadow system headers with local ones.
    `x.h.prebuilt` files (libpng's pnglibconf) are copied to `stub_dir` as
    `x.h`, standing in for the configure step."""
    dirs = [root / "include", root / "src"]
    for vendor in sorted(d for d in root.rglob("*") if d.is_dir() and d.name in _VENDOR_DIRS
                         and not any(part.startswith(".") for part in d.relative_to(root).parts)):
        dirs += sorted(d for d in vendor.iterdir() if d.is_dir())
    stub_dir.mkdir(parents=True, exist_ok=True)
    for pre in root.rglob("*.h.prebuilt"):
        target = stub_dir / pre.name[: -len(".prebuilt")]
        if not target.exists():
            shutil.copyfile(pre, target)
    return [str(d) for d in dirs if d.is_dir()] + [str(stub_dir)]


def _flags(incs: list[str]) -> list[str]:
    return [f"-I{i}" for i in incs]


def ast_facts(path: Path, incs: list[str]) -> dict | None:
    """None when clang reports an error for the file."""
    r = subprocess.run(["clang", "-fsyntax-only", "-Wno-everything", "-Xclang", "-ast-dump=json",
                        *_flags(incs), str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    ast = json.loads(r.stdout)
    raw = path.read_bytes()
    newlines = [i for i, b in enumerate(raw) if b == 10]
    main = str(path)
    state = {"file": None}

    def line_of(offset: int) -> int:
        return bisect.bisect_left(newlines, offset) + 1

    def bare(loc):
        # The JSON names a location's file only when it changes from the
        # previously printed one, so the walk must follow print order.
        if not loc:
            return False, None
        if "file" in loc:
            state["file"] = loc["file"]
        return state["file"] == main, loc.get("offset")

    def where(loc):
        """(line, how): how = 'text' written in the file, 'arg' written in the
        file as a macro argument, 'macro' written in a macro body."""
        if not loc:
            return None, None
        if "spellingLoc" in loc:
            in_main, off = bare(loc["spellingLoc"])
            bare(loc["expansionLoc"])
            if in_main and off is not None:
                arg = loc["spellingLoc"].get("isMacroArgExpansion") \
                    or loc["expansionLoc"].get("isMacroArgExpansion")
                return line_of(off), ("arg" if arg else "macro")
            return None, None
        in_main, off = bare(loc)
        return (line_of(off), "text") if in_main and off is not None else (None, None)

    defs, macro_defs, calls, globals_ = set(), set(), set(), set()

    def walk(node, parent_kind):
        kind = node.get("kind")
        ln, how = where(node.get("loc"))
        rng = node.get("range") or {}
        begin, end = where(rng.get("begin")), where(rng.get("end"))
        if kind == "DeclRefExpr":
            name, at = (node.get("referencedDecl") or {}).get("name"), begin
        elif kind == "MemberExpr":
            name, at = node.get("name"), end         # an expression's member is its last token
        else:
            name, at = None, (None, None)
        if name and at[0]:
            node["_ref"] = (name, at[0], at[1])
        nm = node.get("name")
        inner = node.get("inner", [])
        if ln and how in ("text", "arg") and nm and not node.get("isImplicit"):
            into = defs if how == "text" else macro_defs
            if kind == "FunctionDecl" and any(c.get("kind") == "CompoundStmt" for c in inner):
                into.add((nm, ln))
            elif kind == "RecordDecl" and node.get("completeDefinition"):
                into.add((nm, ln))
            elif kind == "EnumDecl" and any(c.get("kind") == "EnumConstantDecl" for c in inner):
                into.add((nm, ln))
            elif kind in ("TypedefDecl", "EnumConstantDecl"):
                into.add((nm, ln))
            elif kind == "VarDecl" and parent_kind == "TranslationUnitDecl" \
                    and node.get("storageClass") != "extern":
                globals_.add((nm, ln))
        for c in inner:
            walk(c, kind)
        if kind == "CallExpr" and inner:
            c = inner[0]
            while c.get("kind") in ("ImplicitCastExpr", "ParenExpr") and c.get("inner"):
                c = c["inner"][0]
            ref = c.get("_ref")
            if ref and ref[2] in ("text", "arg"):
                calls.add(ref[:2])

    walk(ast, None)
    return {"defs": defs, "macro_defs": macro_defs, "calls": calls, "globals": globals_}


def macro_names(path: Path, incs: list[str]) -> set[str]:
    """Every macro defined at the end of the file (clang -dM); macros #undef'd
    before that are found in the file's own #defines by the caller."""
    r = subprocess.run(["clang", "-E", "-dM", "-Wno-everything", *_flags(incs), str(path)],
                       capture_output=True, text=True)
    return {ln.split()[1].split("(")[0] for ln in r.stdout.splitlines()
            if ln.startswith("#define ") and len(ln.split()) > 1}


_DIRECTIVE = re.compile(r"\s*#\s*(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b")


def inactive_lines(path: Path, incs: list[str]) -> set[int]:
    """1-based lines inside #if branches the preprocessor does not take.  A
    marker line is added after every branch-opening directive; the branches
    whose marker survives `clang -E` are the active ones."""
    lines = path.read_text(errors="replace").split("\n")
    out, branches, stack = [], [], []
    i = 0
    while i < len(lines):
        start, text = i, lines[i]
        while text.endswith("\\") and i + 1 < len(lines):
            i += 1
            text = text[:-1] + lines[i]
        out.extend(lines[start:i + 1])
        m = _DIRECTIVE.match(text)
        if m:
            d = m.group(1)
            if d in ("elif", "elifdef", "elifndef", "else", "endif") and stack:
                stack.pop()[2] = start          # the branch ends before this directive
            if d != "endif":
                b = [len(branches), i + 2, None]
                branches.append(b)
                stack.append(b)
                out.append(f"ORACLEMARK_{b[0]}")
        i += 1
    if not branches:
        return set()
    r = subprocess.run(["clang", "-E", "-P", "-w", "-x", "c", "-", f"-I{path.parent}", *_flags(incs)],
                       input="\n".join(out), capture_output=True, text=True)
    alive = {int(x) for x in re.findall(r"ORACLEMARK_(\d+)", r.stdout)}
    dead: set[int] = set()
    for idx, first, end in branches:
        if idx not in alive and end is not None:
            dead.update(range(first, end + 1))
    return dead


def facts(path: Path, incs: list[str]) -> dict | None:
    """Everything the comparison needs from clang for one file (runs clang 3x)."""
    got = ast_facts(path, incs)
    if got is None:
        return None
    got["inactive"] = inactive_lines(path, incs)
    got["macros"] = macro_names(path, incs)
    return got
