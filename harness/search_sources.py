"""Python hooks for search sources a JSON spec cannot fully express.

A source file names its hook with ``"hook": "<name>"``.  Only sources the user
installed (shipped or ~/.momo-harness/search_sources/) may use one; a spec the
model proposes with add_search_source never can, since a hook is code.

* ``build(src, query, n)`` replaces the GET of the url template with its own
  request: ``(url, method, body_bytes)``, or an error string for a bad query.
* ``enrich(results, ctx)`` runs in read mode after the results are parsed; it
  may set ``Result.content`` so read mode uses it instead of fetching the page.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

from . import net


@dataclass(frozen=True)
class Hook:
    build: Callable | None = None
    enrich: Callable | None = None


# ── Stack Exchange: answer bodies for read mode ──────────────────────────────

_SE_ANSWERS = ("https://api.stackexchange.com/2.3/questions/{ids}/answers"
               "?order=desc&sort=votes&site={site}&filter=withbody&pagesize=30")
_SE_PER_QUESTION = 2
_SE_QUESTIONS = 3


def _stackexchange_enrich(results, ctx) -> None:
    """Put the accepted and top-voted answers of the first few questions into
    Result.content — the answers are what a question page is read for, and
    one API call replaces three page fetches."""
    ids = [r.item.get("question_id") for r in results[:_SE_QUESTIONS]
           if r.item and isinstance(r.item.get("question_id"), int)]
    if not ids:
        return
    site = "stackoverflow"
    m = re.match(r"https://([a-z0-9.]+?)(?:\.stackexchange)?\.com/", results[0].url)
    if m and m.group(1) != "stackoverflow":
        site = m.group(1)
    data = ctx.get_json(_SE_ANSWERS.format(ids=";".join(map(str, ids)), site=site))
    if not isinstance(data, dict):
        return
    by_q: dict[int, list[dict]] = {}
    for a in data.get("items") or []:
        if isinstance(a, dict):
            by_q.setdefault(a.get("question_id"), []).append(a)
    for r in results[:_SE_QUESTIONS]:
        answers = by_q.get((r.item or {}).get("question_id")) or []
        answers.sort(key=lambda a: (not a.get("is_accepted"), -(a.get("score") or 0)))
        parts = [f"# {r.title}"]
        for a in answers[:_SE_PER_QUESTION]:
            label = f"Answer (score {a.get('score', 0)}{', accepted' if a.get('is_accepted') else ''})"
            parts.append(f"## {label}\n\n" + net.html_to_text(a.get("body") or "", r.url))
        if len(parts) > 1:
            r.content = "\n\n".join(parts)


# ── OSV: vulnerabilities for "Ecosystem:package[@version]" ───────────────────

_OSV_QUERY = "https://api.osv.dev/v1/query"


def _osv_build(src, query: str, n: int):
    eco, sep, rest = query.strip().partition(":")
    if not sep or not eco or not rest:
        return ("osv needs the query as '<Ecosystem>:<package>[@version]', e.g. "
                "'PyPI:requests@2.19.0', 'npm:lodash@4.17.0' or "
                "'Maven:org.apache.logging.log4j:log4j-core@2.14.1'")
    name, version = rest, None
    at = rest.rfind("@")
    if at > 0:                          # index 0 is an npm scope: @types/node
        name, version = rest[:at], rest[at + 1:]
    body = {"package": {"name": name.strip(), "ecosystem": eco.strip()}}
    if version:
        body["version"] = version.strip()
    return _OSV_QUERY, "POST", json.dumps(body).encode()


HOOKS: dict[str, Hook] = {
    "stackexchange": Hook(enrich=_stackexchange_enrich),
    "osv": Hook(build=_osv_build),
}
