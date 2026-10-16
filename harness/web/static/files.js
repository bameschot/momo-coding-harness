// The workspace drawer: a lazily loaded file tree, and a preview with
// highlighting (Markdown rendered) from which a file is attached or its path inserted.

import { $, copyText, el, flash, formatSize, icon, loadJSON, makeResizable, saveJSON, toast } from "./dom.js";
import { esc, highlight, langFromPath } from "./highlight.js";
import { addCopyButtons, renderMarkdown } from "./markdown.js";

// What app.js supplies: attach a file ({name, text, chars, pages, truncated,
// kind}), insert a path into the input box, and open the (left) drawer slot.
const hooks = { attach: () => {}, insertPath: () => {}, open: () => true };

async function loadDir(path, container) {
  container.replaceChildren(el("div", "muted small", "Loading…"));
  let data;
  try {
    const r = await fetch(`api/files?path=${encodeURIComponent(path)}&hidden=${$("#files-hidden").checked ? 1 : 0}`);
    data = await r.json();
    if (!r.ok) throw new Error(data.error);
  } catch (e) { container.replaceChildren(el("div", "muted small", e.message)); return; }
  if (!data.entries.length) { container.replaceChildren(el("div", "muted small", "(empty)")); return; }
  container.replaceChildren(...data.entries.map((e) => {
    const rel = path ? `${path}/${e.name}` : e.name;
    const row = el("div", `tree-item ${e.type}`);
    const btn = el("button", "tree-row");
    btn.type = "button";
    btn.title = rel;
    const ti = el("span", "tree-icon");
    ti.append(icon(e.type === "dir" ? "chevron-right" : "file", "icon sm"));
    btn.append(ti, el("span", "tree-name", e.name),
               el("span", "tree-size", e.type === "file" ? (e.size < 1024 ? `${e.size} B` : formatSize(e.size)) : ""));
    row.append(btn);
    if (e.type === "dir") {
      const kids = el("div", "tree-kids");
      kids.hidden = true;
      row.append(kids);
      btn.onclick = () => {
        kids.hidden = !kids.hidden;
        row.classList.toggle("open", !kids.hidden);
        if (!kids.hidden && !kids.childElementCount) loadDir(rel, kids);
      };
    } else {
      btn.onclick = () => openPreview(rel);
    }
    return row;
  }));
}



const PREVIEW_MAX_CHARS = 200_000;  // also highlight.js's limit: beyond it, text is shown plain
let previewFile = null;
// Show rel in the preview; false (and a note) when it can't be read.
async function openPreview(rel) {
  let f;
  try {
    const r = await fetch(`api/file?path=${encodeURIComponent(rel)}`);
    f = await r.json();
    if (!r.ok) throw new Error(f.error);
  } catch (e) { toast(e.message, "error"); return false; }
  previewFile = f;
  $("#files-tree").hidden = true;
  $("#file-preview").hidden = false;
  $("#preview-name").textContent = rel;
  $("#preview-name").title = rel;
  const lines = f.text.split("\n").length;
  $("#preview-meta").textContent = `${f.pages ? f.pages + " pages · " : ""}${lines} lines${f.truncated ? " · truncated" : ""}`;
  // A big file shows its start first: the gutter and the highlighted copy of a
  // multi-megabyte file would block the page.
  const cut = f.text.length > PREVIEW_MAX_CHARS ? f.text.lastIndexOf("\n", PREVIEW_MAX_CHARS) : -1;
  renderPreviewCode(f, rel, cut > 0 ? f.text.slice(0, cut) : f.text);
  const all = $("#preview-all");
  all.hidden = cut <= 0;
  all.textContent = `Show all ${lines.toLocaleString("en-US")} lines`;
  all.onclick = () => { renderPreviewCode(f, rel, f.text); all.hidden = true; };
  const isMd = f.kind !== "pdf" && /\.(md|markdown|mdx)$/i.test(rel);
  $("#preview-render").hidden = !isMd;
  const md = $(".preview-md");
  md.replaceChildren();
  if (isMd) {
    md.innerHTML = renderMarkdown(f.text);
    addCopyButtons(md);
  }
  setPreviewRendered(isMd && loadJSON("momo.previewRendered", true));
  return true;
}
function renderPreviewCode(f, rel, text) {
  $(".preview-scroll .line-mark")?.remove();
  const n = text.split("\n").length;
  $(".preview-gutter").textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
  $(".preview-code code").innerHTML = f.kind === "pdf" ? esc(text) : highlight(text, langFromPath(rel) || "none");
  $(".preview-scroll").scrollTop = 0;
}

// Markdown files open rendered by default; the toggle choice is remembered.
function setPreviewRendered(on) {
  $(".preview-md").hidden = !on;
  $(".preview-scroll").hidden = on;
  $(".preview-md").scrollTop = 0;
  const b = $("#preview-render");
  b.textContent = on ? "Plain" : "Rendered";
  b.title = on ? "Show plain text" : "Show rendered Markdown";
}

// Open rel from a path shown in the conversation (`src/app.py`, `app.py:120`):
// the drawer, the file, and — with a line — the plain text scrolled to it.
export async function showFile(rel, line = 0) {
  if (!hooks.open()) hooks.open();       // the slot toggles: make sure it ends open
  if (!$("#files-tree").childElementCount) loadDir("", $("#files-tree"));  // for "back"
  if (!(await openPreview(rel))) return;
  if (!line) return;
  setPreviewRendered(false);
  if (!$("#preview-all").hidden && line > $(".preview-gutter").textContent.split("\n").length) {
    $("#preview-all").click();           // the line is past the shown start
  }
  const scroll = $(".preview-scroll"), code = $(".preview-code");
  const lh = parseFloat(getComputedStyle(code).lineHeight) || 18;
  const top = parseFloat(getComputedStyle(code).paddingTop) + (line - 1) * lh;
  const mark = el("div", "line-mark");
  mark.style.top = `${top}px`;
  mark.style.height = `${lh}px`;
  scroll.append(mark);
  scroll.scrollTop = Math.max(0, top - scroll.clientHeight / 3);
}

// The drawer was opened: the tree from the top.
export function openFiles() {
  $("#file-preview").hidden = true;
  $("#files-tree").hidden = false;
  loadDir("", $("#files-tree"));
}

export function initFiles(h) {
  Object.assign(hooks, h);
  $("#files-hidden").onchange = () => loadDir("", $("#files-tree"));
  makeResizable($("#files-drawer"), "right", "momo.filesWidth", 420);
  $("#preview-render").onclick = () => {
    const on = $(".preview-md").hidden;
    setPreviewRendered(on);
    saveJSON("momo.previewRendered", on);
  };
  $("#preview-back").onclick = () => { $("#file-preview").hidden = true; $("#files-tree").hidden = false; };
  $("#preview-attach").onclick = () => {
    const f = previewFile;
    if (!f) return;
    hooks.attach({ name: f.name, text: f.text, chars: f.chars, pages: f.pages,
                   truncated: f.truncated, kind: f.kind });
    flash($("#preview-attach"), "Attached ✓");
  };
  $("#preview-insert").onclick = () => { if (previewFile) hooks.insertPath(previewFile.path); };
  $("#preview-copy").onclick = async () => {
    if (!previewFile) return;
    try { await copyText(previewFile.text); flash($("#preview-copy"), "Copied ✓"); } catch { flash($("#preview-copy"), "Copy failed"); }
  };
}
