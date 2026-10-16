"""index_status, /index's status text, and the memory breakdown behind /index
and the web UI's INDEX popover."""
from __future__ import annotations

from pathlib import Path

from .records import _C_FILE, _C_IMPORT, _C_SYMBOL, _fmt_size
from .persist import filter_path
from .store import ProjectIndex, auto_workers


def index_status(*, workdir: Path, index: ProjectIndex | None = None, cancel=None) -> str:
    if index is None:
        return "Code index: off (the user can turn it on with /index on)"
    return status_text(index)


# (key, label) of the composition categories, in stacking order.
BREAKDOWN_CATEGORIES = (
    ("files",    "File records"),
    ("symbols",  "Definitions"),
    ("imports",  "Imports"),
    ("idents",   "Identifiers"),
    ("trigrams", "Text signatures"),
)


def breakdown(index: ProjectIndex) -> dict:
    """What the index's memory is spent on — backs /index and the web UI's
    INDEX popover, the way context_breakdown backs /context.  Bytes are the
    same estimates the budget uses, so the categories add up to `used`."""
    with index._lock:
        n = nsym = nimp = nocc = 0
        by_lang: dict[str, list[int]] = {}
        for _, e in index._alive():
            n += 1
            nsym += len(e.symbols)
            nimp += len(e.imports)
            lang = e.lang or "text"
            row = by_lang.setdefault(lang, [0, 0])
            row[0] += 1
            row[1] += e.cost + e.cost_idents + e.cost_sig
        nocc = sum(len(a) for a in index._idents.values())
        cats = {
            "files":    (n * _C_FILE, f"{n:,} files"),
            "symbols":  (nsym * _C_SYMBOL, f"{nsym:,} definitions"),
            "imports":  (nimp * _C_IMPORT, f"{nimp:,} imports"),
            "idents":   (index.mem["idents"], f"{len(index._idents):,} names, {nocc:,} uses"),
            "trigrams": (index.mem["trigrams"], "per-file bitmaps"),
        }
        enabled = {"idents": index.components["idents"], "trigrams": index.components["trigrams"]}
        used = index.mem_used()
        return {
            "enabled": True,
            "state": index.state,
            "progress": f"{index.done}/{index.total}" if index.state in ("building", "refreshing")
                        and index.total else "",
            "files": n,
            "used": used,
            "limit": index.max_bytes,
            "max_files": index.max_files,
            "workers": index.workers,
            "workers_used": index.worker_count(),
            "filter": {"path": str(filter_path(index.root)), "rules": index.filter_rule_count()},
            "pct": min(100, round(used / index.max_bytes * 100)) if index.max_bytes else 0,
            "partial": index.partial,
            "degraded": index.degraded(),
            "categories": [{"key": k, "label": label, "bytes": max(0, cats[k][0]),
                            "detail": cats[k][1], "enabled": enabled.get(k, True)}
                           for k, label in BREAKDOWN_CATEGORIES],
            "languages": [{"lang": lang, "files": f, "bytes": b}
                          for lang, (f, b) in sorted(by_lang.items(), key=lambda kv: -kv[1][1])],
            # Distinct identifier names belong to the project, not to one file.
            "shared": max(0, used - sum(b for _, b in by_lang.values())),
            "skipped": {k: v for k, v in index.skipped.items() if v},
            "error": index.last_error,
        }


def skip_label(reason: str, n: int, max_files: int) -> str:
    """One "Skipped:" entry; "limit" is a flag (the listing stops at the limit)."""
    if reason == "limit":
        return f"files past the {max_files:,}-file limit"
    return f"{n:,} {reason.replace('_', ' ')}"


def workers_label(workers: int) -> str:
    return f"auto ({auto_workers()})" if workers == 0 else str(workers)


def status_text(index: ProjectIndex) -> str:
    with index._lock:
        langs: dict[str, int] = {}
        nsym = 0
        for _, e in index._alive():
            langs[e.lang or "text"] = langs.get(e.lang or "text", 0) + 1
            nsym += len(e.symbols)
        nfiles = len(index._by_path)
        state = index.state
        if state != "idle" and index.total:
            state += f" {index.done}/{index.total}"
        comp = ", ".join(f"{k} {'on' if v else 'DROPPED'}" for k, v in index.components.items())
        lines = [
            f"Code index: {state} — {nfiles} files, {nsym} definitions, "
            f"{len(index._idents)} distinct identifiers",
            "By language: " + ", ".join(f"{k} {v}" for k, v in sorted(langs.items(), key=lambda kv: -kv[1])),
            f"Memory: ~{_fmt_size(index.mem_used())} of {_fmt_size(index.max_bytes)} "
            f"(files {_fmt_size(index.mem['files'])}, identifiers {_fmt_size(index.mem['idents'])}, "
            f"text signatures {_fmt_size(index.mem['trigrams'])}; estimated)",
            f"Components: {comp}" + ("; PARTIAL (budget reached)" if index.partial else ""),
            f"Workers: {workers_label(index.workers)}"
            + ("; worker processes failed, building in one process" if index._pool_broken else ""),
        ]
        sk = {k: v for k, v in index.skipped.items() if v}
        if sk:
            lines.append("Skipped: " + ", ".join(skip_label(k, v, index.max_files)
                                                 for k, v in sk.items()))
        if index.last_error:
            lines.append(f"Last error: {index.last_error}")
    return "\n".join(lines)
