"""The index's files in ~/.momo-harness/index/: the pickle (/index save|load,
/index-persist) and the gitignore-syntax filter (/index-filter).

Loading goes through a restricted unpickler that only admits the index's own
classes, so a tampered file fails to load instead of running code.  A header
names the format, Python, grammar versions and a hash of the extractor's
source, so a pickle written by other code is discarded, never misread."""
from __future__ import annotations

import hashlib
import os
import pickle
import sys
from functools import lru_cache
from pathlib import Path

from .. import code_nav, ignore_rules
from ..paths import momo_home, untrusted_reason
from ..session import atomic_write

FORMAT_VERSION = 7   # 6: HTML inline-script JavaScript; 7: skipped files, source fingerprint

_ALLOWED_GLOBALS = {
    ("harness.code_index.records", "FileEntry"),
    ("harness.code_nav", "Symbol"),
    ("harness.code_nav", "Import"),
    ("array", "array"),
    ("array", "_array_reconstructor"),
    ("builtins", "set"),
    ("builtins", "frozenset"),
}


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) in _ALLOWED_GLOBALS:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"{module}.{name} is not allowed in an index file")


def index_dir() -> Path:
    return momo_home() / "index"


def pickle_path(root: Path) -> Path:
    digest = hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:16]
    return index_dir() / f"{digest}.pickle"


def filter_path(root: Path) -> Path:
    return pickle_path(root).with_suffix(".filter")


_untrusted_reason = untrusted_reason      # (paths.py: custom tool files use it too)


def load_filter(root: Path) -> tuple[str, str]:
    """The project's index filter text, and a notice ('' when there is nothing
    to say).  A missing filter is seeded from the .gitignore files and saved;
    an untrusted or unreadable one is replaced by the seed for this run."""
    path = filter_path(root)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        text = ignore_rules.seed_text(root)
        try:
            atomic_write(path, text, private_dir=True)
        except OSError as e:
            return text, f"Code index filter could not be saved ({e}); using the .gitignore defaults."
        n = len(ignore_rules.Rules.parse(text))
        return text, (f"Code index filter created from .gitignore ({n} rules): {path} — "
                      f"/index-filter to view or edit it.")
    except OSError as e:
        return ignore_rules.seed_text(root), f"Code index filter not read ({e}); using the .gitignore defaults."
    why = _untrusted_reason(path, st)
    if why:
        return ignore_rules.seed_text(root), f"Code index filter not used: {why}. Using the .gitignore defaults."
    try:
        return path.read_text(encoding="utf-8", errors="replace"), ""
    except OSError as e:
        return ignore_rules.seed_text(root), f"Code index filter not read ({e}); using the .gitignore defaults."


def save_filter(root: Path, text: str) -> str:
    """Write the filter; returns '' or an ERROR string."""
    if not text.endswith("\n"):
        text += "\n"
    try:
        atomic_write(filter_path(root), text, private_dir=True)
    except OSError as e:
        return f"ERROR: could not save the code index filter: {e}"
    return ""


def _grammar_versions() -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version
    names = {"tree-sitter"} | {f"tree-sitter-{'typescript' if g == 'tsx' else g}"
                               for g in set(code_nav._EXTENSIONS.values()) | {"dockerfile"}}
    out = {}
    for n in sorted(names):
        try:
            out[n] = version(n)
        except PackageNotFoundError:
            out[n] = ""
    return out


# The package modules that build and hold what a pickle stores.
_STORED_BY = ("records", "build", "store", "persist")


@lru_cache(maxsize=1)
def _source_fingerprint() -> str:
    """A hash of the code that decides what gets stored: any change to it
    discards saved indexes, without relying on FORMAT_VERSION being bumped.
    (The query modules read the store; changing them keeps saved indexes.)"""
    h = hashlib.sha1()
    here = Path(__file__).parent
    for path in [Path(code_nav.__file__)] + [here / f"{m}.py" for m in _STORED_BY]:
        try:
            h.update(path.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:16]


def _header(root: Path) -> dict:
    return {"format": FORMAT_VERSION, "root": str(root), "python": list(sys.version_info[:2]),
            "grammars": _grammar_versions(), "source": _source_fingerprint()}
