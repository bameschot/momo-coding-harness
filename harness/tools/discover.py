"""Loading tools on demand (/tools discover): the catalog of tools that are
listed but not declared, the ranking find_tools uses for a query, and what the
model reads back once a tool is loaded.

With discovery on, a discoverable tool costs one catalog line in the system
prompt instead of its schema plus its reference entry.  The catalog lists every
such tool, loaded or not, so loading never changes the system prompt: only the
request's tool list grows, and the server re-reads the prompt once per load.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .reference import example_call, signature, summary

FIND_TOOLS = "find_tools"
MAX_LOAD = 5          # tools one find_tools call may load
QUERY_LOAD = 3        # tools a query loads at most (its best matches)
QUERY_CUTOFF = 0.6    # ... each scoring at least this share of the best one

_STOP = frozenset({"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "i", "in",
                   "is", "it", "me", "my", "need", "of", "on", "or", "some", "that", "the",
                   "this", "to", "tool", "tools", "want", "with", "you"})
_WORD = re.compile(r"[a-z0-9]+")


def _stem(term: str) -> str:
    """A naive singular, so `files` matches `file` and `searches` matches `search`."""
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 4 and re.search(r"(ches|shes|sses|xes)$", term):
        return term[:-2]
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def tokenize(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(text.lower().replace("_", " ")) if w not in _STOP]


def _document(schema: dict) -> list[str]:
    """What a query is matched against: the name twice (it is the strongest
    signal), the description and the parameters."""
    fn = schema["function"]
    props = (fn.get("parameters") or {}).get("properties") or {}
    params = " ".join(f"{p} {spec.get('description', '')}" for p, spec in props.items())
    name = tokenize(fn["name"])
    return name + name + tokenize(fn.get("description", "")) + tokenize(params)


def rank(query: str, schemas: list[dict], limit: int = QUERY_LOAD,
         cutoff: float = QUERY_CUTOFF) -> list[str]:
    """The names of the tools that best match query (BM25), best first: at
    most limit, and only those scoring at least cutoff x the best, so a clear
    match loads alone.  Ties keep catalog order; a tool no query term matches
    is never returned."""
    terms = tokenize(query)
    if not terms or not schemas:
        return []
    docs = [_document(s) for s in schemas]
    avg = sum(len(d) for d in docs) / len(docs) or 1.0
    df = Counter(t for d in docs for t in set(d))
    n = len(docs)
    k1, b = 1.2, 0.75
    scored = []
    for i, (schema, doc) in enumerate(zip(schemas, docs)):
        tf = Counter(doc)
        score = 0.0
        for t in terms:
            if tf[t]:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(doc) / avg))
        if score > 0:
            scored.append((-score, i, schema["function"]["name"]))
    scored.sort()
    best = -scored[0][0] if scored else 0.0
    return [name for neg, _, name in scored[:limit] if -neg >= cutoff * best]


def render_catalog(schemas: list[dict]) -> str:
    """The system prompt's "More tools" section: one line per loadable tool."""
    lines = [f"- {s['function']['name']}({signature(s)}) — {summary(s)}" for s in schemas]
    return ("## More tools (load with find_tools)\n\n"
            "These tools exist but are not loaded yet. When one fits the task better than the "
            "tools above, load it with find_tools (by name, or describe what you need) and then "
            "call it like any other tool. A loaded tool stays loaded.\n\n" + "\n".join(lines))


def render_loaded(schemas: list[dict]) -> str:
    """What find_tools returns for the tools it loaded: signature, purpose and
    an example call, for a model that writes its calls as text."""
    return "\n".join(f"- {s['function']['name']}({signature(s)}) — {summary(s)}\n"
                     f"  {example_call(s)}" for s in schemas)


def mentioned(text: str, names) -> set[str]:
    """The names that appear in text as whole words."""
    return {n for n in names if re.search(rf"(?<![\w.]){re.escape(n)}(?!\w)", text)}
