"""What the index stores per file: FileEntry, the memory each stored object is
estimated at, the packed occurrence format, and the per-file trigram
signatures index_text filters on.  Shared by the indexer, its worker
processes and the query tools."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import code_nav
from ..net import format_size

# Estimated bytes per stored object, measured with a deep getsizeof walk and
# tracemalloc on this repo and a 325-file C++/Python tree (imgui), then rounded
# up for dict/array over-allocation.  An estimate — /index status says so.
_C_FILE = 1300        # FileEntry, its path, lists and signature int header
_C_SYMBOL = 780       # Symbol + its strings incl. doc line (~560) and its _sym_by_name slot (~200)
_C_IMPORT = 420
_C_OCC = 9            # one packed (file, line) in an array('Q'), plus growth slack
_C_NAMEREF = 8        # one (interned) name in a file's names tuple
_C_SIG_OVERHEAD = 40
_C_NEWNAME = 160      # a distinct identifier: its key string, array header and dict slot

# Packed occurrence: file id in the high bits, 1-based line in the low 24.
_LINE_BITS = 24
_LINE_MASK = (1 << _LINE_BITS) - 1


@dataclass
class FileEntry:
    path: str                 # workdir-relative, "/"-separated
    mtime_ns: int
    size: int
    lang: str | None          # None: plain text, searchable by index_text only
    nlines: int
    has_error: bool = False
    symbols: list = field(default_factory=list)
    imports: list = field(default_factory=list)
    names: tuple = ()         # distinct identifiers, so the file can be removed from idents
    package: str = ""         # Java/Kotlin `package a.b`: imports resolve through it
    sig: int = 0              # trigram signature bitmap (0 when trigrams are off)
    sig_bits: int = 0
    cost: int = 0             # estimated bytes: base + symbols + imports
    cost_idents: int = 0
    cost_sig: int = 0


# Only trigrams inside [a-z0-9_] runs are indexed.  Every such run in a query
# is a substring of a run in any file that matches, so the filter never drops
# a real hit; punctuation-only queries fall back to scanning.

_WORD = re.compile(rb"[a-z0-9_]{3,}")
_WORD_STR = re.compile(r"[a-z0-9_]{3,}")
_TRI_SPACE = 37 ** 3
_CODE = [36] * 256
for _i, _c in enumerate(b"abcdefghijklmnopqrstuvwxyz0123456789"):
    _CODE[_c] = _i


def _trigram_ids(words) -> set[int]:
    ids: set[int] = set()
    C = _CODE
    for w in words:
        if isinstance(w, str):
            w = w.encode("ascii", "ignore")
        for i in range(len(w) - 2):
            ids.add((C[w[i]] * 37 + C[w[i + 1]]) * 37 + C[w[i + 2]])
    return ids


def _signature(raw_lower: bytes) -> tuple[int, int]:
    """(bitmap, bits) — about 4 bits per distinct trigram, exact once the
    bitmap covers the whole trigram space."""
    ids = _trigram_ids(set(_WORD.findall(raw_lower)))
    if not ids:
        return 0, 0
    m = min(_TRI_SPACE, max(512, 4 * len(ids)))
    ba = bytearray((m + 7) // 8)
    for t in ids:
        h = t % m
        ba[h >> 3] |= 1 << (h & 7)
    return int.from_bytes(ba, "little"), m


def _mask(ids: set[int], m: int) -> int:
    out = 0
    for t in ids:
        out |= 1 << (t % m)
    return out


def _fmt_size(n: int) -> str:
    return format_size(max(0, int(n)))


DATA_LANGS = set(code_nav._DATA_EXTRACTORS)

# Languages that can call or import each other's definitions.
_FAMILY = {"javascript": "js", "typescript": "js", "tsx": "js", "html": "js",
           "c": "c", "cpp": "c", "java": "jvm", "kotlin": "jvm"}


def lang_family(lang: str | None) -> str | None:
    return _FAMILY.get(lang, lang) if lang else None


# Languages whose imports name declarations (a class, a function), not files.
_DECL_IMPORT_LANGS = {"java", "kotlin"}
