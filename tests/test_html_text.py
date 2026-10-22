"""Tests for harness/html_text.py: the tree builder, chrome pruning, the
content-root choice, the plain-text serializer and the link index.

What fetch_url does with the result (windowing, the links= lookup, the page
cache) is in test_net.py; these drive convert() directly.

Run with:  python -m unittest discover tests
"""
import unittest

from harness import html_text
from harness.html_text import convert, delink_markdown, parse

BASE = "https://example.com/docs/page"
FILLER = "<p>" + "real article text, " * 40 + "</p>"


def text(html: str, base: str = BASE) -> str:
    return convert(html, base).text


def tags(node):
    """The element structure as nested (tag, [children]) for asserting on."""
    return [(k.tag, tags(k)) for k in node.kids if not isinstance(k, str)]


class TreeBuilder(unittest.TestCase):
    def body(self, html):
        root = parse(html)
        return html_text._find(root, "body") or root

    def test_unclosed_p_closed_by_block(self):
        self.assertEqual(tags(self.body("<body><p>a<div>b</div>")), [("p", []), ("div", [])])

    def test_unclosed_li_closed_by_sibling(self):
        ul = self.body("<body><ul><li>a<li>b</ul>").kids[0]
        self.assertEqual([k.tag for k in ul.kids], ["li", "li"])

    def test_unclosed_cells_and_rows(self):
        table = self.body("<body><table><tr><td>a<td>b<tr><td>c</table>").kids[0]
        self.assertEqual(tags(table), [("tr", [("td", []), ("td", [])]), ("tr", [("td", [])])])

    def test_dt_dd_close_each_other(self):
        dl = self.body("<body><dl><dt>t<dd>d<dt>u<dd>e</dl>").kids[0]
        self.assertEqual([k.tag for k in dl.kids], ["dt", "dd", "dt", "dd"])

    def test_stray_end_tag_ignored(self):
        body = self.body("<body><div>a</span>b</div><p>c</p>")
        self.assertEqual(tags(body), [("div", []), ("p", [])])

    def test_table_end_closes_open_cells(self):
        body = self.body("<body><table><tr><td>a</table><p>after</p>")
        self.assertEqual([k.tag for k in body.kids], ["table", "p"])

    def test_end_tag_does_not_cross_a_table_cell(self):
        # A stray </div> inside a cell must not close the div around the table.
        body = self.body("<body><div><table><tr><td>x</div>y</td></tr></table>z</div>")
        self.assertEqual(len(body.kids), 1)
        self.assertIn("z", html_text._text(body.kids[0]))

    def test_self_closed_button_closed_by_parent_end_tag(self):
        # <button/> stays open in HTML; the next </div> closes it, as in browsers.
        body = self.body("<body><div><button/></div><p>page</p>")
        self.assertEqual([k.tag for k in body.kids], ["div", "p"])

    def test_missing_head_end_tag(self):
        root = parse("<html><head><title>T</title><meta charset=utf-8><div>body</div>")
        head = html_text._find(root, "head")
        self.assertNotIn("div", [k.tag for k in head.kids if not isinstance(k, str)])

    def test_never_raises_and_depth_is_capped(self):
        root = parse("<div>" * 5000 + "deep" + "</b>" * 10)
        self.assertIn("deep", html_text._text(root))
        depth, n = 0, root
        while [k for k in n.kids if not isinstance(k, str)]:
            n = [k for k in n.kids if not isinstance(k, str)][0]
            depth += 1
        self.assertLessEqual(depth, html_text._MAX_DEPTH)
        self.assertIn("deep", text("<div>" * 5000 + "deep"))


class Pruning(unittest.TestCase):
    def test_hard_drops(self):
        out = text("<body><script>leak()</script><style>a{}</style><svg><text>S</text></svg>"
                   "<template>T</template><p>kept</p></body>")
        self.assertEqual(out, "kept")

    def test_soft_drops_by_tag_role_hidden_and_class(self):
        out = text("<body><nav>N</nav><div role=search>R</div><div hidden>H</div>"
                   "<div style='display: none'>D</div><div class='site-footer'>F</div>"
                   "<div id='cookie-banner'>C</div><span class='sr-only'>SR</span>"
                   "<p>kept</p></body>")
        self.assertEqual(out, "kept")

    def test_wikipedia_chrome(self):
        out = text('<body><h2>History<span class="mw-editsection">[edit]</span></h2>'
                   '<p>Fact.<sup class="reference">[1]</sup></p>'
                   '<div class="reflist"><ol class="references"><li>Cite</li></ol></div>'
                   '<div class="catlinks">Categories</div></body>')
        self.assertEqual(out, "## History\n\nFact.")

    def test_sphinx_reference_links_kept(self):
        # Sphinx marks every link class="reference": that must not read as a citation.
        out = text('<p>raises <a class="reference internal" href="#e"><code>TimeoutError</code></a>.</p>')
        self.assertEqual(out, "raises `TimeoutError`.")

    def test_class_rules_skip_code(self):
        out = text('<pre><span class="hljs-comment"># note</span>\nx = 1</pre>')
        self.assertIn("# note", out)

    def test_large_node_protected_from_soft_drop(self):
        # An unclosed <div role=navigation> wraps the page: keep it.
        out = text("<body><div role='navigation'>" + FILLER * 3)
        self.assertIn("real article text", out)

    def test_wrapper_around_main_protected(self):
        out = text(f"<body><div class='has-sidebar'><main>{FILLER}</main></div></body>")
        self.assertIn("real article text", out)

    def test_large_button_kept(self):
        out = text("<body><button>" + FILLER * 3)
        self.assertIn("real article text", out)


class ContentRoot(unittest.TestCase):
    def test_main_preferred(self):
        out = text(f"<body><div>SIDEBAR</div><main>{FILLER}</main><div>MORE</div></body>")
        self.assertNotIn("SIDEBAR", out)
        self.assertNotIn("MORE", out)

    def test_main_narrows_to_known_content(self):
        out = text(f"<body><main><div>FILE LIST " + "x " * 200 + "</div>"
                   f"<article class='markdown-body'>{FILLER}</article></main></body>")
        self.assertIn("real article text", out)
        self.assertNotIn("FILE LIST", out)

    def test_main_narrows_to_dominant_article(self):
        out = text(f"<body><main><p>teaser</p><article>{FILLER * 3}</article></main></body>")
        self.assertNotIn("teaser", out)

    def test_several_articles_keep_their_container(self):
        out = text(f"<body><main><article>{FILLER}</article><article>{FILLER}"
                   "<p>SECOND</p></article></main></body>")
        self.assertIn("SECOND", out)

    def test_link_heavy_container_scores_low(self):
        links = "".join(f"<a href='/x{i}'>link number {i}</a> " for i in range(80))
        out = text(f"<body><main><div id='content'>{links}{FILLER}</div></main></body>")
        self.assertIn("real article text", out)

    def test_paragraph_vote_without_markers(self):
        out = text(f"<body><div>{'menu entry ' * 30}</div><div class='x'>{FILLER * 3}</div></body>")
        self.assertNotIn("menu entry", out)

    def test_tiny_main_falls_back_to_body(self):
        out = text("<body><p>the real text</p><main>widget</main></body>")
        self.assertIn("the real text", out)


class Serializer(unittest.TestCase):
    def test_headings_and_paragraphs(self):
        self.assertEqual(text("<h2>Usage</h2><p>one</p><p>two</p>"), "## Usage\n\none\n\ntwo")

    def test_permalink_markers_stripped(self):
        self.assertEqual(text('<h2>Usage<a class="x" href="#u">¶</a></h2><p>b</p>'), "## Usage\n\nb")
        self.assertEqual(text('<h2><a href="#s">§</a>Serde</h2><p>b</p>'), "## Serde\n\nb")
        self.assertEqual(text("<h2>C#</h2><p>b</p>"), "## C#\n\nb")

    def test_soft_wraps_collapsed(self):
        self.assertEqual(text("<p>a group of\n  containers,\nwith shared</p>"),
                         "a group of containers, with shared")

    def test_br_is_a_line_break(self):
        self.assertEqual(text("<p>one<br>two</p>"), "one\ntwo")

    def test_tight_and_nested_lists(self):
        out = text("<ul><li><p>one</p></li><li>two<ul><li>inner</li></ul></li></ul>"
                   "<ol start=3><li>c</li><li>d</li></ol>")
        self.assertEqual(out, "- one\n- two\n  - inner\n\n3. c\n4. d")

    def test_empty_items_dropped(self):
        self.assertEqual(text("<ul><li><img src=x></li><li>kept</li></ul>"), "- kept")

    def test_code_fence_language_and_length(self):
        out = text('<div class="highlight-python3"><pre>print(1)</pre></div>'
                   "<pre>```\ninner\n```</pre>")
        self.assertIn("```python3\nprint(1)\n```", out)
        self.assertIn("````\n```\ninner\n```\n````", out)

    def test_code_in_list_item_not_indented(self):
        out = text("<ul><li>run<pre>make\nmake test</pre></li></ul>")
        self.assertEqual(out, "- run\n```\nmake\nmake test\n```")

    def test_inline_code_backticks_sized(self):
        self.assertEqual(text("<p>use <code>a`b</code></p>"), "use ``a`b``")

    def test_fake_headings_escaped(self):
        out = text("<p># not a heading</p><table><tr><th>Spec</th></tr>"
                   "<tr><td><a href='#x'># the-table-element</a></td></tr></table>")
        self.assertNotIn("\n# ", "\n" + out)
        self.assertIn("\\# not a heading", out)

    def test_glued_links_separated(self):
        self.assertEqual(text("<p><a href='/a'>client</a><a href='/b'>cookies</a></p>"),
                         "client cookies")

    def test_key_value_table(self):
        out = text("<table><tr><th>Developed by</th><td>John Gruber</td></tr>"
                   "<tr><th>Type</th><td><ul><li>a</li><li>b</li></ul></td></tr></table>")
        self.assertEqual(out, "Developed by: John Gruber\nType:\n- a\n- b")

    def test_data_table_tab_separated(self):
        out = text("<table><tr><th>Name</th><th>Age</th><th>City</th></tr>"
                   "<tr><td>Ann</td><td>3</td><td>X</td></tr></table>")
        self.assertEqual(out, "Name\tAge\tCity\nAnn\t3\tX")

    def test_layout_table(self):
        out = text("<table><tr><td><table><tr><td>inner a</td><td>inner b</td>"
                   "<td>c</td></tr></table></td></tr></table>")
        self.assertIn("inner a\tinner b\tc", out)

    def test_definition_list(self):
        out = text("<dl><dt>term</dt><dd>short def</dd><dt>sig(x)</dt>"
                   "<dd><p>para one</p><p>para two</p></dd></dl>")
        self.assertEqual(out, "term: short def\n\nsig(x)\n\npara one\n\npara two")

    def test_empty_headings_dropped(self):
        out = text("<h2>Releases</h2><h2>Used by</h2><h2>About</h2><p>text</p>")
        self.assertEqual(out, "## About\n\ntext")

    def test_repeated_blocks_collapsed(self):
        self.assertEqual(text("<div>docs</div><div>docs</div><p>x</p>"), "docs\n\nx")


class Title(unittest.TestCase):
    def test_title_prepended_when_body_lacks_it(self):
        self.assertEqual(text("<title>Guide</title><h1>Install</h1><p>x</p>"),
                         "# Guide\n\n# Install\n\nx")

    def test_title_dropped_when_h1_repeats_it(self):
        self.assertEqual(text("<title>Markdown - Wikipedia</title><h1>Markdown</h1><p>x</p>"),
                         "# Markdown\n\nx")
        self.assertEqual(text("<title>&lt;table&gt; element - HTML | MDN</title>"
                              "<h1><code>&lt;table&gt;</code> element</h1><p>x</p>"),
                         "# `<table>` element\n\nx")

    def test_svg_title_ignored(self):
        self.assertEqual(convert("<body><svg><title>icon</title></svg><p>x</p>").title, "")


class LinkIndex(unittest.TestCase):
    def test_absolute_deduplicated_and_cleaned(self):
        page = convert('<p><a href="/a?utm_source=x&id=2">A</a> <a href="/a?id=2">again</a>'
                       ' <a href="#top">top</a> <a href="javascript:x()">js</a>'
                       ' <a href="mailto:a@b.c">mail</a> <a href="page#sec">self</a>'
                       ' <a href="https://other.test/x">ext</a></p>', BASE)
        self.assertEqual([(lk.text, lk.url) for lk in page.links],
                         [("A", "https://example.com/a?id=2"),
                          ("ext", "https://other.test/x")])
        self.assertNotIn("http", page.text)

    def test_navigation_indexed_but_not_in_text(self):
        page = convert(f'<body><nav><a href="/next">Next chapter</a></nav>'
                       f'<main>{FILLER}<a href="/ref">reference</a></main></body>', BASE)
        by_text = {lk.text: lk for lk in page.links}
        self.assertNotIn("Next chapter", page.text)
        self.assertFalse(by_text["Next chapter"].in_content)
        self.assertTrue(by_text["reference"].in_content)

    def test_content_wording_wins_over_sidebar(self):
        page = convert(f'<body><nav><a href="/rt">Container Runtimes</a></nav><main>{FILLER}'
                       f'<a href="/rt">a container runtime</a></main></body>', BASE)
        self.assertEqual([(lk.text, lk.in_content) for lk in page.links],
                         [("a container runtime", True)])

    def test_image_link_uses_alt_text(self):
        page = convert('<a href="/home"><img src="logo.png" alt="Home"></a>', BASE)
        self.assertEqual(page.links[0].text, "Home")

    def test_base_element_respected(self):
        page = convert('<head><base href="https://cdn.test/v2/"></head><a href="x">X</a>', BASE)
        self.assertEqual(page.links[0].url, "https://cdn.test/v2/x")


class Markdown(unittest.TestCase):
    def test_links_and_images_delinked(self):
        md = ("See [the guide](/learn/guide \"Guide\") and ![logo](logo.png).\n"
              "[![badge](b.svg)](https://ci.test/x)\n"
              "Ref [docs][1].\n\n[1]: https://docs.test/\n")
        out, links = delink_markdown(md, "https://react.test/ref/page")
        self.assertEqual(out, "See the guide and logo.\nbadge\nRef docs.\n\n")
        self.assertEqual([(lk.text, lk.url) for lk in links],
                         [("the guide", "https://react.test/learn/guide"),
                          ("badge", "https://ci.test/x"), ("1", "https://docs.test/")])

    def test_code_untouched(self):
        md = "Use `[x](y)` here.\n```\n[a](b)\n```\n[c](https://c.test)"
        out, links = delink_markdown(md, "")
        self.assertEqual(out, "Use `[x](y)` here.\n```\n[a](b)\n```\nc")
        self.assertEqual([lk.url for lk in links], ["https://c.test"])


if __name__ == "__main__":
    unittest.main()


class Tabs(unittest.TestCase):
    def test_hidden_panels_kept_with_their_labels(self):
        out = text('<div role="tablist"><button role="tab" id="t1">pip</button>'
                   '<button role="tab" id="t2">conda</button></div>'
                   '<div role="tabpanel" aria-labelledby="t1"><pre>pip install x</pre></div>'
                   '<div role="tabpanel" aria-labelledby="t2" hidden><pre>conda install x</pre></div>')
        self.assertIn("**pip**\n\n```\npip install x\n```", out)
        self.assertIn("**conda**\n\n```\nconda install x\n```", out)

    def test_labels_by_position_without_ids(self):
        # Docusaurus: no ids, no aria-labelledby; panels in a sibling wrapper.
        out = text('<div class="tabs-container"><ul role="tablist">'
                   '<li role="tab">npm</li><li role="tab">yarn</li></ul>'
                   '<div><div role="tabpanel"><p>npm add x</p></div>'
                   '<div role="tabpanel" hidden><p>yarn add x</p></div></div></div>')
        self.assertIn("**npm**\n\nnpm add x", out)
        self.assertIn("**yarn**\n\nyarn add x", out)

    def test_other_hidden_content_still_dropped(self):
        out = text(f'<main>{FILLER}<div hidden>SECRET MENU</div></main>')
        self.assertNotIn("SECRET MENU", out)


class ClientRendered(unittest.TestCase):
    PROSE = "This guide explains how the scheduler assigns work to each worker in turn."

    def shell(self, data: str, kind: str = 'id="__NEXT_DATA__" type="application/json"') -> str:
        return (f'<html><head><title>App</title></head><body><div id="__next"></div>'
                f'<script {kind}>{data}</script><script src="/app.js"></script></body></html>')

    def test_next_data_page_props_read(self):
        import json
        data = json.dumps({"props": {"pageProps": {
            "title": "Scheduling basics", "slug": "scheduling",
            "body": f"<h2>How it works</h2><p>{self.PROSE}</p>",
            "url": "https://example.com/a long path"}}, "buildId": "abc"})
        page = convert(self.shell(data), BASE)
        self.assertIn("Scheduling basics", page.text)
        self.assertIn("## How it works", page.text)
        self.assertIn(self.PROSE, page.text)
        self.assertNotIn("abc", page.text)
        self.assertNotIn("example.com", page.text)
        self.assertIn("embedded in its HTML", page.note)

    def test_json_ld_article_read(self):
        import json
        data = json.dumps({"@type": "Article", "headline": "Release notes for 2.0",
                           "articleBody": self.PROSE})
        page = convert(self.shell(data, 'type="application/ld+json"'), BASE)
        self.assertIn("Release notes for 2.0", page.text)
        self.assertIn(self.PROSE, page.text)

    def test_empty_shell_explained(self):
        page = convert(self.shell("{}"), BASE)
        self.assertIn("may be built by JavaScript", page.note)

    def test_rendered_page_ignores_its_data(self):
        page = convert(f"<body><main>{FILLER}</main><script id='__NEXT_DATA__'>"
                       f'{{"props": {{"pageProps": {{"x": "{self.PROSE}"}}}}}}</script></body>', BASE)
        self.assertNotIn("scheduler", page.text)
        self.assertEqual(page.note, "")
