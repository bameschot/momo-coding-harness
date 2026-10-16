// The sessions drawer: click a row to load that session; the trash button (or
// Select mode with checkboxes) deletes saved sessions after an inline confirmation.
// The open session can't be deleted, and the server refuses it too.

import { $, confirmInline, el, icon } from "./dom.js";

// What app.js supplies: send a line as if typed, close the drawer, POST JSON.
const hooks = { send: async () => false, close: () => {}, post: async () => ({}) };

const ago = (t) => {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return new Date(t * 1000).toLocaleDateString();
};

const sessionsUI = { data: null, selecting: false, selected: new Set(), note: "", filter: "" };

async function loadSessions() {
  const list = $("#sessions-list");
  list.replaceChildren(el("div", "muted", "Loading…"));
  try { sessionsUI.data = await (await fetch("api/sessions")).json(); } catch (e) { list.firstChild.textContent = e.message; return; }
  const names = new Set(sessionsUI.data.sessions.map((x) => x.name));
  for (const n of [...sessionsUI.selected]) if (!names.has(n)) sessionsUI.selected.delete(n);
  renderSessions();
}

function iconButton(name, label, cls = "icon-btn") {
  const b = el("button", cls);
  b.type = "button";
  b.title = label;
  b.setAttribute("aria-label", label);
  b.append(icon(name));
  return b;
}

// The open session's title, edited in place: Enter sends /title (empty: back
// to automatic titles), Esc keeps the old one.
function renameInPlace(item, title) {
  const saved = [...item.childNodes];
  const field = el("input", "rename-field");
  field.type = "text";
  field.value = title;
  field.placeholder = "Title — empty for an automatic one";
  field.setAttribute("aria-label", "Session title");
  const restore = () => item.replaceChildren(...saved);
  field.onkeydown = async (e) => {
    if (e.key === "Escape") { e.stopPropagation(); restore(); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const v = field.value.trim();
      await hooks.send(v ? `/title ${v}` : "/title auto");
      loadSessions();
    }
  };
  field.onblur = restore;
  item.replaceChildren(field);
  field.focus();
  field.select();
}

// Whether a row matches the filter box: title, first message, folder, model, name.
function matches(x, q) {
  return !q || [x.title, x.preview, x.workdir, x.model, x.mode, x.name]
    .some((v) => String(v || "").toLowerCase().includes(q));
}

async function deleteSessions(names) {
  let res;
  try { res = await hooks.post("api/sessions/delete", { names }); } catch (e) { sessionsUI.note = `Delete failed: ${e.message}`; renderSessions(); return; }
  for (const n of res.deleted) sessionsUI.selected.delete(n);
  const n = res.deleted.length;
  sessionsUI.note = (n ? `Deleted ${n} session${n === 1 ? "" : "s"}.` : "")
    + res.skipped.map((x) => ` Kept ${x.name}: ${x.reason}.`).join("");
  await loadSessions();
}

function renderSessions() {
  const { data, selecting, selected } = sessionsUI;
  const list = $("#sessions-list");
  const items = [];
  if (sessionsUI.note) items.push(el("div", "session-note", sessionsUI.note.trim()));
  const q = sessionsUI.filter.trim().toLowerCase();
  const shown = data.sessions.filter((x) => matches(x, q));
  if (!data.sessions.length) items.push(el("div", "muted", "No saved sessions yet."));
  else if (!shown.length) items.push(el("div", "muted", "No session matches the filter."));
  for (const x of shown) {
    const current = x.name === data.current;
    const item = el("div", "session-item");
    const row = el("button", `session-row${current ? " current" : ""}`);
    row.type = "button";
    const label = x.title || x.preview || "(no messages)";
    const first = x.title && x.preview ? `\n${x.preview}` : "";
    row.title = `${x.name}${current ? " (open now)" : ""}${first}\n${x.workdir}`;
    row.append(el("span", "session-preview", label),
               el("span", "session-meta", `${x.mode} · ${x.model} · ${x.messages} msgs · ${ago(x.mtime)}`));
    if (selecting) {
      const cb = el("input");
      cb.type = "checkbox";
      cb.disabled = current;
      cb.checked = selected.has(x.name);
      cb.setAttribute("aria-label", `Select ${x.title || x.preview || x.name}`);
      cb.onchange = () => { cb.checked ? selected.add(x.name) : selected.delete(x.name); updateBulk(); };
      row.onclick = () => { if (!current) { cb.checked = !cb.checked; cb.onchange(); } };
      item.append(cb, row);
    } else {
      row.onclick = () => {
        if (current) return;
        hooks.close();
        hooks.send(`/session ${x.name}`);
      };
      item.append(row);
      if (current) {
        const ren = iconButton("pencil", "Rename this session", "icon-btn session-del");
        ren.onclick = () => renameInPlace(item, x.title || "");
        item.append(ren);
      } else {
        const del = iconButton("trash", "Delete this session", "icon-btn session-del");
        del.onclick = async () => {
          if (await confirmInline(item, "Delete this session?")) deleteSessions([x.name]);
        };
        item.append(del);
      }
    }
    items.push(item);
  }
  list.replaceChildren(...items);
  updateBulk();
}

function updateBulk() {
  const { selecting, selected } = sessionsUI;
  $("#sessions-bulk").hidden = !selecting;
  $("#select-sessions").textContent = selecting ? "Done" : "Select";
  $("#select-sessions").setAttribute("aria-pressed", String(selecting));
  $("#sessions-count").textContent = `${selected.size} selected`;
  $("#sessions-delete").disabled = selected.size === 0;
}

// The drawer was opened: show the list fresh.
export function openSessions() {
  sessionsUI.note = "";
  loadSessions();
  $("#sessions-filter").focus();
}

export function initSessions(h) {
  Object.assign(hooks, h);
  $("#sessions-filter").oninput = (e) => {
    sessionsUI.filter = e.target.value;
    if (sessionsUI.data) renderSessions();
  };
  $("#select-sessions").onclick = () => {
    sessionsUI.selecting = !sessionsUI.selecting;
    sessionsUI.selected.clear();
    sessionsUI.note = "";
    if (sessionsUI.data) renderSessions();
  };
  $("#sessions-all").onclick = () => {
    const { data, selected } = sessionsUI;
    // The ones the filter shows: "select all" never reaches rows you can't see.
    const q = sessionsUI.filter.trim().toLowerCase();
    const all = data.sessions.filter((x) => x.name !== data.current && matches(x, q)).map((x) => x.name);
    const every = all.length && all.every((n) => selected.has(n));
    selected.clear();
    if (!every) for (const n of all) selected.add(n);
    renderSessions();
  };
  $("#sessions-delete").onclick = async () => {
    const names = [...sessionsUI.selected];
    const bar = $("#sessions-bulk");
    if (await confirmInline(bar, `Delete ${names.length} session${names.length === 1 ? "" : "s"}? This can't be undone.`)) {
      await deleteSessions(names);
    }
  };
  $("#new-session").onclick = () => { hooks.close(); hooks.send("/new"); };
}
