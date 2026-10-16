// Find in the conversation (Ctrl/⌘+F): a bar under the status bar that searches
// every message, tool call and result, diff and thinking block — including the
// collapsed ones and the lines a long tool output hides — and opens what is
// around each hit.  The browser's own find can't see inside closed <details>.

import { $ } from "./dom.js";

// The full text of a clamped tool output (app.js clampedPre): pre → expand(), which
// puts the hidden lines back so a hit in them can be shown.
export const clamped = new WeakMap();

const MAX_HITS = 2000;
let hits = [];
let cur = -1;
let opener = null;
let timer = null;

const transcript = () => $("#transcript");
const SKIP = ".msg-actions, .code-bar, .welcome, button, .turn-meta";

function clear() {
  const root = transcript();
  const parents = new Set();
  for (const m of root.querySelectorAll("mark.find-hit")) {
    parents.add(m.parentNode);
    m.replaceWith(...m.childNodes);
  }
  for (const p of parents) p.normalize();
  hits = [];
  cur = -1;
}

function count() {
  $("#find-count").textContent = !$("#find-input").value.trim() ? ""
    : hits.length ? `${cur + 1} / ${hits.length}${hits.length >= MAX_HITS ? "+" : ""}` : "no matches";
}

function search(keepPosition = false) {
  const was = cur;
  clear();
  const q = $("#find-input").value.trim().toLowerCase();
  if (!q) return count();
  const root = transcript();
  // Clamped outputs that hold the text get their hidden lines back first.
  for (const pre of root.querySelectorAll("pre")) {
    const c = clamped.get(pre);
    if (c && c.text.toLowerCase().includes(q)) c.expand();
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement?.closest(SKIP) || !n.data.toLowerCase().includes(q)
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    let rest = node;
    let at;
    while (hits.length < MAX_HITS && (at = rest.data.toLowerCase().indexOf(q)) >= 0) {
      const hit = rest.splitText(at);
      rest = hit.splitText(q.length);
      const mark = document.createElement("mark");
      mark.className = "find-hit";
      hit.replaceWith(mark);
      mark.append(hit);
      hits.push(mark);
    }
  }
  if (hits.length) go(keepPosition && was >= 0 ? Math.min(was, hits.length - 1) : 0);
  else count();
}

function go(i) {
  if (!hits.length) return;
  hits[cur]?.classList.remove("current");
  cur = (i + hits.length) % hits.length;
  const m = hits[cur];
  m.classList.add("current");
  for (let d = m.parentElement.closest("details"); d; d = d.parentElement?.closest("details")) d.open = true;
  m.closest(".code-block.folded")?.querySelector(".code-more")?.click();   // a long code block
  m.scrollIntoView({ block: "center" });
  count();
}

export function openFind() {
  const bar = $("#find-bar");
  if (bar.hidden) opener = document.activeElement;
  bar.hidden = false;
  const inp = $("#find-input");
  inp.focus();
  inp.select();
  if (inp.value.trim()) search(true);
}

export function closeFind() {
  const bar = $("#find-bar");
  if (bar.hidden) return;
  clear();
  bar.hidden = true;
  if (opener?.isConnected) opener.focus();
}

export const findOpen = () => !$("#find-bar").hidden;

// The transcript was re-rendered (a view toggle): mark the hits again.
export function refreshFind() {
  if (findOpen() && $("#find-input").value.trim()) search(true);
}

export function initFind() {
  const inp = $("#find-input");
  inp.oninput = () => { clearTimeout(timer); timer = setTimeout(() => search(), 150); };
  inp.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); clearTimeout(timer); if (!hits.length) search(); else go(cur + (e.shiftKey ? -1 : 1)); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeFind(); }
  };
  $("#find-next").onclick = () => go(cur + 1);
  $("#find-prev").onclick = () => go(cur - 1);
  $("#find-close").onclick = closeFind;
}
