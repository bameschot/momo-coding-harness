"""HTML to plain text for fetch_url and web_search — stdlib only.

The page is parsed into a small tree first, because every decision that
matters needs to see a whole element before emitting any of it: how much of
the page's text a container holds, how much of that is link text, whether a
table is data or layout.  The stdlib HTMLParser is only a tokenizer, so
_TreeBuilder adds the subset of the HTML5 tree-construction rules real pages
lean on (implied end tags, scoped end tags, a missing </head>).  Measured on
twenty real sites, modern pages are almost always well formed; the rules are
for the old and hand-written ones.

Then, on the tree:

1. Every link is indexed (text, absolute URL), before anything is dropped, so
   navigation stays reachable through fetch_url(links=...) even though it never
   reaches the text.
2. Chrome is pruned: by tag, ARIA role, hidden state and class/id names.  The
   soft rules never remove a node holding a large share of the page's text,
   which is what a mis-marked or unclosed wrapper looks like.
3. The content root is chosen: a known content container, else the
   best-scoring main/article (descending while one child keeps most of the
   text), else a readability-style paragraph vote, else the body.
4. The root is written out as plain text.  The only markup kept is the part
   the tools navigate by: '#' headings (find=, the section list, passage
   ranking), ``` fences around code, '-' / '1.' list items and `code`.  Link
   URLs are left out — they cost 16–69% of the text on link-heavy pages.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


@dataclass(frozen=True)
class Link:
    text: str
    url: str
    in_content: bool      # inside the content root, as opposed to navigation


@dataclass
class Page:
    text: str             # with the page title as a '# ' line when the body lacks it
    title: str
    links: list[Link] = field(default_factory=list)


# ── tree ──────────────────────────────────────────────────────────────────────

VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                       "meta", "param", "source", "track", "wbr"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# Opening one of these closes an open <p> ("close a p element in button scope").
_CLOSES_P = frozenset({"address", "article", "aside", "blockquote", "center", "details",
                       "dialog", "dir", "div", "dl", "fieldset", "figcaption", "figure",
                       "footer", "form", "header", "hgroup", "hr", "main", "menu", "nav",
                       "ol", "p", "pre", "section", "summary", "table", "ul", "li", "dd",
                       "dt", "listing", "xmp"}) | _HEADINGS
# An implied or end-tag close never reaches past one of these.  <button> bounds
# only the search for an open <p> ("button scope"): a self-closed <button/> is
# still open, and the next </div> must close it as browsers do, or the rest of
# the page ends up inside it.
_SCOPE = frozenset({"html", "table", "td", "th", "caption", "template", "marquee",
                    "object", "applet"})
_P_BOUND = frozenset({"button"})
# Table parts close through open cells: "table scope" stops only at the table.
_TABLE_SCOPE = frozenset({"html", "table", "template"})
_TABLE_PARTS = frozenset({"caption", "thead", "tbody", "tfoot", "tr", "td", "th"})
# tag -> (open elements it closes, the elements that stop the search)
_SIBLINGS = {
    "li": ({"li"}, _SCOPE | {"ul", "ol", "menu"}),
    "dt": ({"dt", "dd"}, _SCOPE | {"dl"}),
    "dd": ({"dt", "dd"}, _SCOPE | {"dl"}),
    "tr": ({"tr"}, _TABLE_SCOPE | {"thead", "tbody", "tfoot"}),
    "td": ({"td", "th"}, _TABLE_SCOPE | {"tr"}),
    "th": ({"td", "th"}, _TABLE_SCOPE | {"tr"}),
    "thead": ({"thead", "tbody", "tfoot"}, _TABLE_SCOPE),
    "tbody": ({"thead", "tbody", "tfoot"}, _TABLE_SCOPE),
    "tfoot": ({"thead", "tbody", "tfoot"}, _TABLE_SCOPE),
    "option": ({"option"}, _SCOPE | {"select", "datalist", "optgroup"}),
    "optgroup": ({"optgroup", "option"}, _SCOPE | {"select"}),
}
# End tag -> the elements that stop its search (default _SCOPE).
_END_SCOPE = {"li": _SCOPE | {"ul", "ol", "menu"}, "dt": _SCOPE | {"dl"},
              "dd": _SCOPE | {"dl"}, "p": _SCOPE | _P_BOUND,
              "table": _TABLE_SCOPE - {"table"},
              **{t: _TABLE_SCOPE for t in _TABLE_PARTS}}
_HEAD_TAGS = frozenset({"title", "meta", "link", "style", "script", "base", "noscript",
                        "template"})
# Deeper elements are attached flat.  Rendering recurses once or twice per
# level, so this keeps a pathological page well clear of the recursion limit.
_MAX_DEPTH = 200


class Node:
    __slots__ = ("tag", "attrs", "kids", "parent")

    def __init__(self, tag: str, attrs: dict, parent: "Node | None"):
        self.tag, self.attrs, self.kids, self.parent = tag, attrs, [], parent

    def attr(self, name: str) -> str:
        return self.attrs.get(name) or ""

    def classes(self) -> list[str]:
        return self.attr("class").split()


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self.stack = [self.root]
        self.head: Node | None = None
        self._head_closed = False

    def _close(self, tags, scope=_SCOPE) -> bool:
        """Pop through the nearest open element in `tags`, unless an element
        in `scope` comes first."""
        for i in range(len(self.stack) - 1, 0, -1):
            t = self.stack[i].tag
            if t in tags:
                del self.stack[i:]
                return True
            if t in scope:
                return False
        return False

    def _head_open(self) -> bool:
        # Once closed, <head> never reopens: stop scanning the stack for it.
        if self.head is None or self._head_closed:
            return False
        if any(n is self.head for n in self.stack):
            return True
        self._head_closed = True
        return False

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag in ("html", "body"):
            for n in self.stack:
                if n.tag == tag:          # a second <body>: merge, never nest
                    for k, v in a.items():
                        n.attrs.setdefault(k, v)
                    return
        if tag not in _HEAD_TAGS and self._head_open():
            self._close({"head"})         # </head> is optional
        if tag in _CLOSES_P:
            self._close({"p"}, _SCOPE | _P_BOUND)
        if tag in _HEADINGS and self.stack[-1].tag in _HEADINGS:
            self.stack.pop()
        if (rule := _SIBLINGS.get(tag)):
            self._close(*rule)
        parent = self.stack[-1]
        node = Node(tag, a, parent)
        parent.kids.append(node)
        if tag == "head" and self.head is None:
            self.head = node
        if tag not in VOID_TAGS and len(self.stack) < _MAX_DEPTH:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        # HTML ignores the slash on a non-void element: <div/> opens a div.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS or tag in ("html", "body"):
            return                        # content after </body> is still body
        self._close({tag}, _END_SCOPE.get(tag, _SCOPE))

    def handle_data(self, data):
        if not data:
            return
        if self.stack[-1] is self.head and data.strip():
            self._close({"head"})
        parent = self.stack[-1]
        if parent.kids and isinstance(parent.kids[-1], str):
            parent.kids[-1] += data
        else:
            parent.kids.append(data)


def parse(html: str) -> Node:
    """The document tree.  Never raises: malformed markup yields a partial tree."""
    b = _TreeBuilder()
    try:
        b.feed(html)
        b.close()
    except Exception:
        pass
    return b.root


def _walk(node: Node, skip=frozenset()):
    """Element nodes under `node`, pre-order, iteratively."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        for k in reversed(n.kids):
            if not isinstance(k, str) and k.tag not in skip:
                stack.append(k)


def _text(node: Node, br: str = " ", blocks: bool = False) -> str:
    """All text under `node`, in order, whitespace untouched.  With `blocks`,
    block elements are spaced apart (a card link's title and summary)."""
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        if isinstance(n, str):
            out.append(n)
        elif n.tag == "br":
            out.append(br)
        else:
            if blocks and n.tag in _BLOCK:
                stack.append(" ")
            stack.extend(reversed(n.kids))
            if blocks and n.tag in _BLOCK:
                out.append(" ")
    return "".join(out)


_WS = re.compile(r"[ \t\r\n\f\v\xa0 -​  　﻿]+")


def _squash(s: str) -> str:
    return _WS.sub(" ", s).strip()


# ── links ─────────────────────────────────────────────────────────────────────

_TRACKING = re.compile(r"^(?:utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|ref_src|_hsenc|_hsmi)$")


def clean_url(url: str) -> str:
    """`url` without tracking parameters."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not _TRACKING.match(k)]
    return urlunsplit(parts._replace(query=urlencode(q)))


def resolve(href: str, base: str, page: str) -> str | None:
    """Absolute http(s) URL for a link target, or None for one not worth
    indexing: in-page fragments, javascript:/mailto: and the like, and links
    back to this same page."""
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return None
    try:
        url = urljoin(base, href)
        if urlsplit(url).scheme not in ("http", "https"):
            return None
    except ValueError:
        return None
    url = clean_url(url)
    if page and url.split("#", 1)[0] == page:
        return None
    return url


def _link_text(a: Node) -> str:
    text = _squash(_text(a, blocks=True))
    if not text:
        for n in _walk(a):
            if n.tag == "img" and n.attr("alt").strip():
                return _squash(n.attr("alt"))
        text = _squash(a.attr("aria-label") or a.attr("title"))
    return text


def _anchors(body: Node, base: str, page: str) -> list[tuple[Node, str, str]]:
    out = []
    for n in _walk(body, skip=_HARD_DROP):
        if n.tag == "a" and "href" in n.attrs:
            url = resolve(n.attrs["href"], base, page)
            text = _link_text(n) if url else ""
            if url and text:
                out.append((n, text, url))
    return out


# ── pruning ───────────────────────────────────────────────────────────────────

# Never prose, wherever it appears.
_HARD_DROP = frozenset({"script", "style", "head", "noscript", "svg", "template", "iframe",
                        "object", "embed", "canvas", "audio", "video", "map", "datalist",
                        "title"})
# Chrome, unless the node holds a large share of the page (then it is a wrapper
# that is mis-marked or never closed, and dropping it would drop the page).
_SOFT_DROP = frozenset({"nav", "footer", "aside", "select", "button", "textarea", "dialog"})
_DROP_ROLES = frozenset({"navigation", "banner", "contentinfo", "search", "complementary",
                         "menu", "menubar", "toolbar", "dialog", "alertdialog"})
_BOILERPLATE = re.compile(
    r"(?:^|[-_])(?:nav|navbar|navigation|menu|breadcrumbs?|sidebar|footer|cookies?|consent|"
    r"share|sharing|social|related|promo|advert|ads?|advertisement|sponsored|newsletter|"
    r"subscribe|subscription|comments?|pagination|pager|skip-?link|toolbar|popup|modal)"
    r"(?:$|[-_])", re.I)
# Exact class names: wiki and doc-site chrome, and screen-reader-only text.
# Not "reference": Sphinx puts it on every link; Wikipedia's citation markers
# are matched as <sup class="reference"> instead.
_BOILERPLATE_CLASSES = frozenset({
    "mw-editsection", "reflist", "references", "mw-references-wrap", "navbox",
    "navbox-styles", "catlinks", "printfooter", "mw-jump-link", "ambox", "noprint", "hatnote",
    "sistersitebox", "mw-indicators", "mw-cite-backlink", "headerlink", "sr-only",
    "visually-hidden", "screen-reader-text"})
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_CODE_TAGS = frozenset({"pre", "code", "kbd", "samp"})
# A soft drop is refused for a node holding at least this share of the page's
# text, and at least _MAIN_MIN_CHARS of it.
_PROTECT_SHARE = 0.3
# A content root is only trusted when it holds at least this much text; a tiny
# <main> is more likely a mis-marked widget than the page body.
_MAIN_MIN_CHARS = 500


def _measure(root: Node, skip=frozenset()) -> dict[int, tuple[int, int]]:
    """id(node) -> (text chars, link-text chars), bottom-up in one pass."""
    order = list(_walk(root, skip))
    stats: dict[int, tuple[int, int]] = {}
    for n in reversed(order):
        t = lt = 0
        for k in n.kids:
            if isinstance(k, str):
                t += len(_squash(k))
            elif (s := stats.get(id(k))) is not None:
                t += s[0]
                lt += s[1]
        stats[id(n)] = (t, t if n.tag == "a" else lt)
    return stats


# How sure a drop rule is.  A broad rule (a tag, a role, 'nav' somewhere in a
# class name) can hit a wrapper holding the page, so it yields to the share
# protection; an exact wiki/doc class name cannot, so it does not — Wikipedia's
# references are close to half of an article's text.
_BROAD, _EXACT = 1, 2


def _drop_rule(n: Node, in_code: bool) -> int:
    """0 to keep `n`, else how sure the rule that drops it is."""
    if n.tag in _SOFT_DROP:
        return _BROAD
    a = n.attrs
    if (a.get("role") or "").lower() in _DROP_ROLES:
        return _BROAD
    if "hidden" in a or (a.get("aria-hidden") or "").lower() == "true":
        return _BROAD
    if (style := a.get("style")) and _HIDDEN_STYLE.search(style):
        return _BROAD
    if n.tag == "a" and "hreflang" in a:      # language switchers
        return _BROAD
    if in_code:
        return 0                              # hljs-comment, prism "token comment"
    classes = n.classes()
    if (n.tag == "sup" and "reference" in classes) or _BOILERPLATE_CLASSES.intersection(classes):
        return _EXACT
    if any(_BOILERPLATE.search(c) for c in classes):
        return _BROAD
    return _BROAD if (ident := a.get("id")) and _BOILERPLATE.search(ident) else 0


def _is_content_marker(n: Node) -> bool:
    return n.tag in ("main", "article") or n.attr("role").lower() == "main"


def _prune(body: Node, soft: bool) -> None:
    stats = _measure(body, _HARD_DROP)
    total = stats.get(id(body), (0, 0))[0]
    floor = max(_MAIN_MIN_CHARS, total * _PROTECT_SHARE)
    protect: set[int] = set()
    if soft:
        for n in _walk(body, _HARD_DROP):
            if _is_content_marker(n):
                p = n
                while p is not None and id(p) not in protect:
                    protect.add(id(p))
                    p = p.parent
    stack = [(body, False)]
    while stack:
        n, in_code = stack.pop()
        keep = []
        for k in n.kids:
            if isinstance(k, str):
                keep.append(k)
                continue
            if k.tag in _HARD_DROP:
                continue
            if soft and id(k) not in protect and (rule := _drop_rule(k, in_code)) and (
                    rule == _EXACT or stats.get(id(k), (0, 0))[0] < floor):
                continue
            keep.append(k)
            stack.append((k, in_code or k.tag in _CODE_TAGS))
        n.kids = keep


# ── content root ──────────────────────────────────────────────────────────────

_KNOWN_CONTENT = frozenset({"markdown-body", "mw-parser-output", "rst-content", "md-content",
                            "entry-content"})
_CONTENT_IDS = frozenset({"content", "main-content", "maincontent", "main"})
_DESCEND_SHARE = 0.6


def _within(node: Node, ancestor: Node) -> bool:
    p = node.parent
    while p is not None:
        if p is ancestor:
            return True
        p = p.parent
    return False


def _choose_root(body: Node) -> Node:
    stats = _measure(body)

    def score(n: Node) -> int:
        t, lt = stats.get(id(n), (0, 0))
        return t - lt

    nodes = list(_walk(body))
    known = [n for n in nodes if _KNOWN_CONTENT.intersection(n.classes())
             and score(n) >= _MAIN_MIN_CHARS]
    if known:
        return max(known, key=score)

    articles = [n for n in nodes if n.tag == "article" and score(n) >= _MAIN_MIN_CHARS]
    cands = [n for n in nodes if score(n) >= _MAIN_MIN_CHARS and (
        n.tag == "main" or n.attr("role").lower() == "main"
        or n.attr("itemprop") == "articleBody" or n.attr("id").lower() in _CONTENT_IDS
        or (n.tag == "article" and len(articles) == 1))]
    if cands:
        best = max(cands, key=score)
        inner = cands + [a for a in articles if a not in cands]
        while True:
            below = [c for c in inner if c is not best and _within(c, best)]
            if not below:
                return best
            nxt = max(below, key=score)
            if score(nxt) < _DESCEND_SHARE * score(best):
                return best
            best = nxt

    # Nothing marks the content: let paragraphs vote for their container.
    votes: dict[int, float] = {}
    by_id: dict[int, Node] = {}
    for n in nodes:
        if n.tag in ("p", "pre", "td", "blockquote"):
            t = stats.get(id(n), (0, 0))[0]
            if t < 25:
                continue
            v = 1 + min(3, t // 100)
            for p, w in ((n.parent, 1.0), (n.parent and n.parent.parent, 0.5)):
                if p is not None:
                    votes[id(p)] = votes.get(id(p), 0) + v * w
                    by_id[id(p)] = p
    if votes:
        best = by_id[max(votes, key=votes.get)]
        if score(best) >= max(_MAIN_MIN_CHARS, 0.5 * score(body)):
            return best
    return body


# ── rendering ─────────────────────────────────────────────────────────────────

_BLOCK = frozenset({"address", "article", "aside", "blockquote", "body", "caption", "center",
                    "dd", "details", "dialog", "dir", "div", "dl", "dt", "fieldset",
                    "figcaption", "figure", "footer", "form", "header", "hgroup", "hr",
                    "html", "legend", "li", "main", "menu", "nav", "ol", "p", "pre",
                    "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead",
                    "tr", "ul", "search", "#document"}) | _HEADINGS
_BR = ""            # a hard line break, kept through the whitespace collapse
_FAKE_MARKUP = re.compile(r"(?m)^(?=#{1,6}(?:\s|$)|```)")
_LANG = re.compile(r"^(?:language|lang|highlight-source|highlight)-([\w+#.-]+)$", re.I)
_NO_LANG = frozenset({"default", "none", "text", "plaintext", "plain", "notranslate", "nohighlight"})
# Permalink markers around heading text: '¶' after it (Sphinx), '§' glued in
# front (docs.rs), a separate '#' on either side.  'C#' keeps its '#'.
_HEADING_JUNK = re.compile(r"^[¶§🔗]\s*|^#\s+|\s*[¶§🔗]$|\s+#$")
_KV_KEY_MAX = 60


def _escape(text: str) -> str:
    """Neutralise lines that would read as a heading or a fence, so the
    section list, find= and passage ranking only ever see real ones."""
    return _FAKE_MARKUP.sub("\\\\", text)


def _clean_inline(text: str) -> str:
    text = _WS.sub(" ", text)
    text = re.sub(f" *{_BR} *", "\n", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _one_line(blocks: list[str]) -> str:
    return _squash(" ".join(blocks))


def _indent(text: str, pad: str) -> str:
    """Indent continuation lines, except inside code fences — the tools find
    code by a fence at the start of a line."""
    out, fence = [], False
    for line in text.split("\n"):
        if line.startswith("```"):
            fence = not fence
            out.append(line)
        elif fence or not line:
            out.append(line)
        else:
            out.append(pad + line)
    return "\n".join(out)


def _code_lang(n: Node) -> str:
    probe = [n] + [k for k in n.kids if not isinstance(k, str) and k.tag == "code"][:1]
    p = n.parent
    for _ in range(2):
        if p is not None:
            probe.append(p)
            p = p.parent
    for node in probe:
        for c in node.classes():
            if (m := _LANG.match(c)) and m.group(1).lower() not in _NO_LANG:
                return m.group(1).lower()
    return ""


class _Renderer:
    def __init__(self):
        self._blockish: dict[int, bool] = {}

    def is_block(self, n) -> bool:
        """Block-level by tag, or an inline element wrapping blocks (a card
        link, a custom element) — those are laid out as blocks too."""
        if isinstance(n, str):
            return False
        key = id(n)
        if (hit := self._blockish.get(key)) is not None:
            return hit
        result = n.tag in _BLOCK or any(self.is_block(k) for k in n.kids)
        self._blockish[key] = result
        return result

    # blocks

    def blocks(self, n: Node) -> list[str]:
        out: list[str] = []
        buf: list = []
        for k in n.kids:
            if isinstance(k, str) or not self.is_block(k):
                buf.append(k)
                continue
            self._flush(buf, out)
            out.extend(self.block(k))
        self._flush(buf, out)
        return out

    def _flush(self, buf: list, out: list[str]) -> None:
        if buf:
            text = _clean_inline(self.inline_seq(buf))
            buf.clear()
            if text:
                out.append(_escape(text))

    def block(self, n: Node) -> list[str]:
        tag = n.tag
        if tag in _HEADINGS:
            text = _HEADING_JUNK.sub("", _squash(self.inline_seq(n.kids).replace(_BR, " ")))
            return [f"{'#' * int(tag[1])} {text}"] if text else []
        if tag in ("ul", "ol", "menu", "dir"):
            return self.list_block(n, tag == "ol")
        if tag == "li":
            return self.list_block(Node("ul", {}, n.parent), False, [n])
        if tag == "pre":
            return self.pre_block(n)
        if tag == "table":
            return self.table_block(n)
        if tag == "dl":
            return self.dl_block(n)
        if tag == "hr":
            return []
        return self.blocks(n)

    def list_block(self, n: Node, ordered: bool, items=None) -> list[str]:
        try:
            num = int(n.attr("start") or 1)
        except ValueError:
            num = 1
        lines = []
        for k in (items if items is not None else n.kids):
            if isinstance(k, str):
                if (text := _clean_inline(k)):
                    lines.append(f"- {_escape(text)}")
                continue
            if k.tag in ("ul", "ol") and lines:
                # A list nested straight inside a list (invalid, common):
                # it belongs to the item before it.
                lines[-1] += "\n" + _indent("\n".join(self.block(k)), "  ")
                continue
            body = "\n".join(self.blocks(k) if k.tag == "li" else self.block(k)
                             if self.is_block(k) else [_clean_inline(self.inline(k))])
            if not body.strip():
                continue
            marker = f"{num}. " if ordered else "- "
            num += 1
            first, _, rest = body.partition("\n")
            lines.append(marker + first + ("\n" + _indent(rest, "  ") if rest else ""))
        return ["\n".join(lines)] if lines else []

    def pre_block(self, n: Node) -> list[str]:
        code = _text(n, br="\n").strip("\n")
        code = "\n".join(line.rstrip() for line in code.split("\n"))
        if not code.strip():
            return []
        run = max((len(m) for m in re.findall(r"`{3,}", code)), default=0)
        fence = "`" * max(3, run + 1)
        return [f"{fence}{_code_lang(n)}\n{code}\n{fence}"]

    def _rows(self, table: Node) -> list[list[Node]]:
        rows = []
        for k in table.kids:
            if isinstance(k, str):
                continue
            trs = [k] if k.tag == "tr" else [
                t for t in k.kids if not isinstance(t, str) and t.tag == "tr"
            ] if k.tag in ("thead", "tbody", "tfoot") else []
            for tr in trs:
                cells = [c for c in tr.kids if not isinstance(c, str) and c.tag in ("td", "th")]
                if cells:
                    rows.append(cells)
        return rows

    def table_block(self, n: Node) -> list[str]:
        rows = self._rows(n)
        out = []
        for k in n.kids:
            if not isinstance(k, str) and k.tag == "caption":
                out.extend(self.blocks(k))
        if not rows:
            return out + self.blocks(n)
        ncols = max(len(r) for r in rows)
        cells = [[self.blocks(c) for c in r] for r in rows]
        layout = (ncols <= 1 or n.attr("role").lower() in ("presentation", "none")
                  or any(m.tag == "table" for r in rows for c in r for m in _walk(c))
                  or any(len(b) > 3 or any("```" in x for x in b) for r in cells for b in r))
        if layout:
            for r in cells:
                if all(len(b) <= 1 and "\n" not in "".join(b) for b in r):
                    # Joined, not squashed: a nested data table keeps its tabs.
                    if (line := " ".join(x for b in r for x in b).strip()):
                        out.append(line)
                else:
                    out.extend(x for b in r for x in b)
            return out
        pairs = [r for r in rows if len(r) == 2]
        header_rows = sum(1 for r in rows if all(c.tag == "th" for c in r) and len(r) > 1)
        keyish = sum(1 for r, cb in zip(rows, cells) if len(r) == 2
                     and len(_one_line(cb[0])) <= _KV_KEY_MAX)
        if ncols == 2 and header_rows == 0 and pairs and keyish >= 0.8 * len(pairs):
            lines = []
            for r, cb in zip(rows, cells):
                if len(r) != 2:
                    if (line := _one_line([x for b in cb for x in b])):
                        lines.append(line)
                    continue
                key, val = _one_line(cb[0]), cb[1]
                if not val:
                    if key:
                        lines.append(key)
                elif not key:
                    lines.append("\n".join(val))
                elif len(val) == 1 and "\n" not in val[0]:
                    lines.append(f"{key}: {val[0]}")
                else:
                    lines.append(f"{key}:\n" + "\n".join(val))
            return out + ["\n".join(lines)] if lines else out
        lines = ["\t".join(_one_line(b) for b in r) for r in cells]
        lines = [ln for ln in lines if ln.strip()]
        return out + [_escape("\n".join(lines))] if lines else out

    def dl_block(self, n: Node) -> list[str]:
        out: list[str] = []
        short: list[str] = []      # consecutive 'term: definition' lines, kept tight
        term: str | None = None

        def flush_short():
            if short:
                out.append("\n".join(short))
                short.clear()

        for k in n.kids:
            if isinstance(k, str):
                continue
            if k.tag == "dt":
                if term:
                    flush_short()
                    out.append(term)
                term = _one_line(self.blocks(k))
            elif k.tag == "dd":
                val = self.blocks(k)
                if term and len(val) == 1 and "\n" not in val[0] and len(val[0]) <= 200 \
                        and len(term) <= _KV_KEY_MAX:
                    short.append(f"{term}: {val[0]}")
                else:
                    flush_short()
                    if term:
                        out.append(term)
                    out.extend(val)
                term = None
            else:
                flush_short()
                out.extend(self.block(k) if self.is_block(k) else self.blocks(k))
        flush_short()
        if term:
            out.append(term)
        return out

    # inline

    def inline_seq(self, items: list) -> str:
        parts: list[str] = []
        prev_link = False
        for it in items:
            if isinstance(it, str):
                parts.append(it)
                prev_link = False
                continue
            s = self.inline(it)
            # <a>client</a><a>cookies</a>: separate on screen (CSS), glued in text.
            if (it.tag == "a" and prev_link and s and parts and parts[-1]
                    and not parts[-1][-1].isspace() and not s[0].isspace()):
                parts.append(" ")
            parts.append(s)
            prev_link = it.tag == "a"
        return "".join(parts)

    def inline(self, n: Node) -> str:
        tag = n.tag
        if tag == "br":
            return _BR
        if tag in ("img", "wbr", "input"):
            return ""
        if tag in ("code", "kbd", "samp", "tt"):
            text = _squash(_text(n))
            if not text:
                return ""
            run = max((len(m) for m in re.findall(r"`+", text)), default=0)
            ticks = "`" * (run + 1)
            pad = " " if text.startswith("`") or text.endswith("`") else ""
            return f"{ticks}{pad}{text}{pad}{ticks}"
        return self.inline_seq(n.kids)


def _heading_level(block: str) -> int:
    m = re.match(r"(#{1,6}) \S[^\n]*$", block)
    return len(m.group(1)) if m else 0


def _tidy(blocks: list[str]) -> list[str]:
    # Drop a heading with no body before the next heading of its level or
    # above (GitHub's '## Releases', '## Used by' …), then repeated blocks.
    kept: list[str] = []
    for b in reversed(blocks):
        lvl = _heading_level(b)
        if lvl and kept and 0 < _heading_level(kept[-1]) <= lvl:
            continue
        kept.append(b)
    kept.reverse()
    out: list[str] = []
    for b in kept:
        if not out or b != out[-1]:
            out.append(b)
    return out


def _plain(s: str) -> str:
    return _squash(s.replace("`", "")).lower()


def _with_title(blocks: list[str], title: str) -> list[str]:
    """Prepend '# title' unless the body already opens with the same h1 —
    'Markdown' under the title 'Markdown - Wikipedia'."""
    if not title:
        return blocks
    t = _plain(title)
    for b in blocks[:3]:
        if (m := re.match(r"# (.+)$", b)):
            h = _plain(m.group(1))
            if h and (t == h or re.split(r"\s+[-|·—–:]\s+", t)[0] == h or t.startswith(h + " - ")):
                return blocks
            break
    return [f"# {title}"] + blocks


def _render(body: Node, soft: bool) -> tuple[str, Node]:
    _prune(body, soft)
    root = _choose_root(body) if soft else body
    # block(), not blocks(): the root may itself be a table or a list.
    blocks = _tidy(_Renderer().block(root))
    return "\n\n".join(blocks), root


def _find(root: Node, tag: str) -> Node | None:
    return next((n for n in _walk(root) if n.tag == tag), None)


def _title(root: Node) -> str:
    for n in _walk(root, skip=frozenset({"svg", "math"})):
        if n.tag == "title":
            return _squash(_text(n))
    return ""


def convert(html: str, base_url: str = "") -> Page:
    """The page as plain text, its title, and an index of its links."""
    root = parse(html)
    title = _title(root)
    base = base_url
    if (b := _find(root, "base")) is not None and b.attr("href"):
        base = urljoin(base_url, b.attr("href"))
    page = clean_url(base_url.split("#", 1)[0]) if base_url else ""
    body = _find(root, "body") or root
    anchors = _anchors(body, base, page)

    text, content = _render(body, soft=True)
    in_content = {id(n) for n in _walk(content) if n.tag == "a"}
    if len(text) < 200 and len(html) > 5000:
        # A soft drop that took the page with it: try again with tag drops only.
        root2 = parse(html)
        text2, _ = _render(_find(root2, "body") or root2, soft=False)
        if len(text2) > len(text):
            text, in_content = text2, {id(n) for n, _, _ in anchors}

    if title:
        blocks = text.split("\n\n") if text else []
        text = "\n\n".join(_with_title(blocks, title))

    # One entry per URL.  Where the content links it too, the content's
    # wording wins over the sidebar's: it is what the model just read.
    links: dict[str, Link] = {}
    for n, label, url in anchors:
        inside = id(n) in in_content
        if url not in links or (inside and not links[url].in_content):
            links[url] = Link(label[:200], url, inside)
    return Page(text=text, title=title, links=list(links.values()))


# ── markdown bodies ───────────────────────────────────────────────────────────

_MD_LINK = re.compile(r"(!?)\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(\s*<?([^\s()<>]*(?:\([^\s()]*\))?[^\s()<>]*)>?"
                      r"(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_MD_REFDEF = re.compile(r"^ {0,3}\[([^\]]+)\]:\s+<?(\S+?)>?(?:\s+.*)?$")
_MD_REFLINK = re.compile(r"\[([^\[\]]+)\]\[[^\[\]]*\]")


def delink_markdown(text: str, base_url: str) -> tuple[str, list[Link]]:
    """A markdown body with link targets taken out: '[text](url)' becomes
    'text', images their alt text, and reference definitions are removed —
    the same shape html pages get.  Code (fenced and inline) is untouched."""
    page = clean_url(base_url.split("#", 1)[0]) if base_url else ""
    links: dict[str, Link] = {}

    def add(label: str, href: str):
        url = resolve(href, base_url, page)
        label = _squash(label)
        if url and label and url not in links:
            links[url] = Link(label[:200], url, True)

    def repl(m: re.Match) -> str:
        label = _MD_LINK.sub(repl, m.group(2))        # [![badge](img)](url)
        if not m.group(1):
            add(label, m.group(3))
        return label

    out, fence = [], ""
    for line in text.split("\n"):
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = ""
            out.append(line)
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            out.append(line)
            continue
        if (m := _MD_REFDEF.match(line)):
            add(m.group(1), m.group(2))
            continue
        parts = re.split(r"(`+[^`]*`+)", line)
        for i in range(0, len(parts), 2):
            parts[i] = _MD_REFLINK.sub(r"\1", _MD_LINK.sub(repl, parts[i]))
        out.append("".join(parts))
    return "\n".join(out), list(links.values())
