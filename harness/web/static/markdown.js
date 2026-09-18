// Escape-first Markdown renderer (same feature set as md_render.py).
// Output is safe for innerHTML: all source text is escaped before any markup is added.
import { esc, highlight } from "./highlight.js";

export function mdInline(src) {
  const codes = [];
  let s = esc(src).replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
  s = s
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/__(.+?)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*(?!\s)(.+?)\*(?!\w)/g, "$1<em>$2</em>")
    .replace(/(^|[^_\w])_(?!\s)(.+?)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/~~(.+?)~~/g, "<del>$1</del>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
      // url is already escaped; allow only http(s), mailto and relative links.
      const raw = url.replace(/&amp;/g, "&");
      if (/^[a-z][a-z0-9+.-]*:/i.test(raw) && !/^(https?|mailto):/i.test(raw)) return text;
      return `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`;
    });
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
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
      while (i < lines.length && lines[i].trim() &&
             !/^(#{1,6}\s|\s*(```|~~~)|\s*>|\s*([-*+]|\d+[.)])\s+)/.test(lines[i])) body.push(lines[i++]);
      out.push(`<p>${body.map(mdInline).join("<br>")}</p>`);
    }
  }
  return out.join("\n");
}

function renderList(lines, start, setIndex) {
  const indentOf = (l) => l.match(/^\s*/)[0].length;
  const base = indentOf(lines[start]);
  const ordered = /^\s*\d+[.)]/.test(lines[start]);
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
  return `<${tag}>` + items.map((it) => {
    const task = it.text.match(/^\[([ xX])\]\s+(.*)$/);
    const body = task ? `${task[1] === " " ? "☐" : "☑"} ${mdInline(task[2])}` : mdInline(it.text);
    return `<li>${body}${it.sub}</li>`;
  }).join("") + `</${tag}>`;
}

