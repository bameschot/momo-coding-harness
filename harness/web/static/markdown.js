// Escape-first Markdown renderer for chat replies, plans and previews: headings,
// lists (task items too), quotes, fenced code, tables, rules, links, bold, italic,
// strikethrough and inline code.  (md_render.py, the TUI's, does a smaller set.)
// Output is safe for innerHTML: all source text is escaped before any markup is added.
import { copyWithFeedback, el } from "./dom.js";
import { esc, highlight } from "./highlight.js";

// Placeholders for spans taken out before the emphasis pass: inline code
// (\u0000) and links (\u0001).  Neither character survives in esc() output's
// markup, and a stray one in the source is dropped.
const CODE_MARK = "\u0000", LINK_MARK = "\u0001";

function emphasis(s) {
  return s
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/__(.+?)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*(?!\s)(.+?)\*(?!\w)/g, "$1<em>$2</em>")
    .replace(/(^|[^_\w])_(?!\s)(.+?)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/~~(.+?)~~/g, "<del>$1</del>");
}

export function mdInline(src) {
  const codes = [], links = [];
  let s = esc(String(src).replace(/[\u0000\u0001]/g, ""))
    .replace(/`([^`]+)`/g, (_, c) => `${CODE_MARK}${codes.push(c) - 1}${CODE_MARK}`)
    // Links come out before emphasis, so `__init__.py` in a URL stays a URL.
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
      // url is already escaped; allow only http(s), mailto and relative links.
      const raw = url.replace(/&amp;/g, "&");
      const label = emphasis(text);
      const html = /^[a-z][a-z0-9+.-]*:/i.test(raw) && !/^(https?|mailto):/i.test(raw) ? label
        : `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
      return `${LINK_MARK}${links.push(html) - 1}${LINK_MARK}`;
    });
  s = emphasis(s).replace(/\u0001(\d+)\u0001/g, (_, i) => links[+i]);
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => codeSpan(codes[+i]));
}

// Inline code that names a workspace file (`src/app.py`, `README.md:12`) can be
// clicked to open it (app.js handles [data-path]).  A known extension, or a
// folder part, keeps "e.g." and "v1.2" plain.
const FILE_EXT = /\.(py|pyi|js|mjs|cjs|jsx|ts|tsx|json|ya?ml|toml|md|txt|rst|html?|css|scss|java|kt|kts|rs|go|c|h|cc|cpp|hpp|sh|bash|sql|cfg|ini|xml|gradle|rb|php|swift|lock|env|conf|csv|dockerfile)$/i;
function codeSpan(c) {
  const m = c.match(/^((?:[\w.-]+\/)*[\w.-]+\.\w{1,10})(?::(\d+))?$/);
  if (m && !m[1].startsWith(".") && (m[1].includes("/") || FILE_EXT.test(m[1])) && !/^\d/.test(m[1])) {
    return `<code data-path="${m[1]}"${m[2] ? ` data-line="${m[2]}"` : ""}>${c}</code>`;
  }
  return `<code>${c}</code>`;
}

export function renderMarkdown(text) {
  const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

  while (i < lines.length) {
    const line = lines[i];
    let m;
    if ((m = line.match(/^\s*(```+|~~~+)\s*([\w+-]*)/))) {           // fenced code
      const fence = m[1];
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence)) body.push(lines[i++]);
      i++;
      out.push(`<pre><code${m[2] ? ` data-lang="${esc(m[2])}"` : ""}>${highlight(body.join("\n"), m[2])}</code></pre>`);
    } else if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {               // heading
      out.push(`<h${m[1].length}>${mdInline(m[2])}</h${m[1].length}>`);
      i++;
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {             // rule
      out.push("<hr>");
      i++;
    } else if (/^\s*>/.test(line)) {                                  // quote
      const body = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) body.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${renderMarkdown(body.join("\n"))}</blockquote>`);
    } else if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {  // table
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
      // Own scroll container: wide tables scroll sideways instead of squeezing columns.
      out.push('<div class="table-scroll" tabindex="0" role="region" aria-label="Table (scrollable)">' +
        "<table><thead><tr>" + head.map((c) => `<th>${mdInline(c)}</th>`).join("") +
        "</tr></thead><tbody>" +
        rows.map((r) => "<tr>" + r.map((c) => `<td>${mdInline(c)}</td>`).join("") + "</tr>").join("") +
        "</tbody></table></div>");
    } else if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {                 // list
      out.push(renderList(lines, i, (n) => (i = n)));
    } else if (!line.trim()) {
      i++;
    } else {                                                          // paragraph
      const body = [];
      const tableStarts = (j) => lines[j].includes("|") && j + 1 < lines.length && isTableSep(lines[j + 1]);
      // A paragraph ends where another block starts, a table included: models
      // write "Results:" on the line right above one.
      while (i < lines.length && lines[i].trim() && !tableStarts(i) &&
             !/^(#{1,6}\s|\s*(```|~~~)|\s*>|\s*([-*+]|\d+[.)])\s+)/.test(lines[i])) body.push(lines[i++]);
      out.push(`<p>${body.map(mdInline).join("<br>")}</p>`);
    }
  }
  return out.join("\n");
}

function renderList(lines, start, setIndex) {
  const indentOf = (l) => l.match(/^\s*/)[0].length;
  const base = indentOf(lines[start]);
  const first = lines[start].match(/^\s*(\d+)[.)]/);
  const ordered = !!first;
  const items = [];
  let i = start;
  while (i < lines.length) {
    const l = lines[i];
    const m = l.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (m && m[1].length === base) {
      items.push({ text: m[3], sub: "" });
      i++;
    } else if (m && m[1].length > base && items.length) {
      let subEnd = i;
      items[items.length - 1].sub += renderList(lines, i, (n) => (subEnd = n));
      i = subEnd;
    } else if (l.trim() && indentOf(l) > base && items.length) {     // continuation
      items[items.length - 1].text += " " + l.trim();
      i++;
    } else break;
  }
  setIndex(i);
  const tag = ordered ? "ol" : "ul";
  const startAttr = ordered && +first[1] !== 1 ? ` start="${+first[1]}"` : "";
  return `<${tag}${startAttr}>` + items.map((it) => {
    const task = it.text.match(/^\[([ xX])\]\s+(.*)$/);
    const body = task ? `${task[1] === " " ? "☐" : "☑"} ${mdInline(task[2])}` : mdInline(it.text);
    return `<li>${body}${it.sub}</li>`;
  }).join("") + `</${tag}>`;
}

// A copy button on every code block under `root` (once per block).
export function addCopyButtons(root) {
  for (const pre of root.querySelectorAll("pre")) {
    const code = pre.querySelector("code");
    if (!code || pre.parentElement.classList.contains("code-block")) continue;
    const box = el("div", "code-block");
    pre.replaceWith(box);
    const bar = el("div", "code-bar");
    const lang = code.dataset.lang;
    const btn = el("button", "btn small quiet copy-btn", "Copy");
    btn.type = "button";
    btn.title = "Copy code";
    btn.setAttribute("aria-label", lang ? `Copy ${lang} code` : "Copy code");
    btn.onclick = () => copyWithFeedback(btn, code.textContent);
    bar.append(el("span", "code-lang", lang || ""), btn);
    box.append(bar, pre);
  }
}
