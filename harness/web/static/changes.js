// The Changes drawer: every file momo changed since the last /keep, per turn
// (newest first) or as the net change per file, each with its diff.  From here
// a turn is undone (it and every later one), one file is reverted, or all of it
// is kept.  The reports go to the transcript like /undo's.

import { $, confirmInline, copyText, el, flash, icon, makeResizable, toast } from "./dom.js";
import { diffBox } from "./diff.js";

// What app.js supplies: open the (left) drawer slot, POST JSON, show a file in
// the workspace preview, the diff style, and whether a turn is running.
const hooks = { open: () => true, post: async () => ({}), showFile: () => {},
                diffStyle: () => "compact", busy: () => false };

const ui = { data: null, view: "turns", open: new Set(), seen: new Set(), shown: null, timer: null };
const OPS = { A: "added", M: "modified", D: "deleted", R: "renamed" };

async function load() {
  try {
    const r = await fetch("api/changes");
    ui.data = await r.json();
    if (!r.ok) throw new Error(ui.data.error);
  } catch (e) {
    $("#changes-list").replaceChildren(el("div", "muted small", e.message));
    return;
  }
  // The newest checkpoint starts open; ones the user folded stay folded.
  const newest = ui.data.turns[0]?.id;
  if (newest != null && !ui.seen.has(newest)) ui.open.add(newest);
  for (const t of ui.data.turns) ui.seen.add(t.id);
  render();
}

function render() {
  const d = ui.data;
  if (!d) return;
  for (const b of document.querySelectorAll("#changes-views button")) {
    b.setAttribute("aria-pressed", String(b.dataset.view === ui.view));
  }
  const list = $("#changes-list");
  const files = d.since_keep;
  const added = files.reduce((n, f) => n + f.added, 0), removed = files.reduce((n, f) => n + f.removed, 0);
  $("#changes-summary").textContent = files.length
    ? `${files.length} file${files.length !== 1 ? "s" : ""} changed since keep · +${added} −${removed}`
      + ` · ${d.turns.length} checkpoint${d.turns.length !== 1 ? "s" : ""}`
      + (d.undo_max ? ` (keeps ${d.undo_max})` : "")
    : "";
  $("#changes-keep").disabled = !d.turns.length || hooks.busy();
  if (!d.turns.length) {
    list.replaceChildren(el("div", "changes-empty muted",
      "Nothing to undo — no file changes since the last keep. Files the model writes show up here, per turn."));
    return;
  }
  list.replaceChildren(...(ui.view === "turns" ? d.turns.map(turnItem) : keepItems(files)));
}

function turnItem(t) {
  const item = el("div", "tree-item change-turn");
  const open = ui.open.has(t.id);
  item.classList.toggle("open", open);
  const head = el("div", "change-turn-head");
  const btn = el("button", "tree-row");
  btn.type = "button";
  const ti = el("span", "tree-icon");
  ti.append(icon("chevron-right", "icon sm"));
  const what = `turn ${t.turn}` + (t.step ? ` · ${t.step}` : "");
  const name = el("span", "tree-name");
  name.append(el("span", "change-id", `#${t.id}`), ` ${what} · `, el("span", "change-label", t.label || "(no message)"));
  btn.title = `${what}: ${t.label}`;
  btn.append(ti, name, el("span", "tree-size", `${t.files.length} file${t.files.length !== 1 ? "s" : ""}`));
  if (t.ran_commands) {
    const warn = el("span", "change-warn", "cmd");
    warn.title = "This turn ran commands: what they changed is not tracked, and undo can't take it back";
    btn.append(warn);
  }
  const kids = el("div", "tree-kids");
  kids.hidden = !open;
  kids.append(...t.files.map((f) => fileRow(f, t.id)));
  btn.onclick = () => {
    kids.hidden = !kids.hidden;
    item.classList.toggle("open", !kids.hidden);
    if (kids.hidden) ui.open.delete(t.id); else ui.open.add(t.id);
  };
  const undo = el("button", "icon-btn change-undo");
  undo.type = "button";
  undo.title = `Undo #${t.id}${t.id !== ui.data.turns[0].id ? " and every later checkpoint" : ""}: put its files back as before`;
  undo.setAttribute("aria-label", undo.title);
  undo.append(icon("retry", "icon sm"));
  undo.disabled = hooks.busy();
  undo.onclick = () => askUndo(t, head);
  head.append(btn, undo);
  item.append(head, kids);
  return item;
}

function keepItems(files) {
  return [el("div", "changes-note muted small",
             "The net change per file since the last keep: what Keep all accepts."),
          ...files.map((f) => fileRow(f, null))];
}

function fileRow(f, checkpoint) {
  const btn = el("button", "tree-row change-file");
  btn.type = "button";
  const op = el("span", `change-op op-${f.op}`, f.op);
  op.title = OPS[f.op];
  const slash = f.rel.lastIndexOf("/");
  const name = el("span", "tree-name");
  if (f.from) name.append(el("span", "muted", `${f.from} → `));
  name.append(f.rel.slice(slash + 1));
  if (slash > 0) name.append(el("span", "muted", `  ${f.rel.slice(0, slash)}`));
  const counts = el("span", "tree-size");
  if (f.added || f.removed) counts.append(el("span", "add", `+${f.added}`), " ", el("span", "del", `−${f.removed}`));
  if (!f.restorable) counts.append(el("span", "change-warn", "no copy"));
  btn.title = `${OPS[f.op]}: ${f.from ? f.from + " → " : ""}${f.rel}` + (f.restorable ? "" : " — too big to save (or a folder): only an undo that moves it back restores it");
  btn.append(op, name, counts);
  btn.onclick = () => openDiff(f, checkpoint);
  return btn;
}

async function askUndo(t, host) {
  let plan;
  try { plan = await (await fetch(`api/changes/preview?checkpoint=${t.id}`)).json(); }
  catch (e) { toast(e.message, "error"); return; }
  const n = plan.ids.length;
  const scope = n > 1 ? `${n} checkpoints (#${t.id} and later)` : `#${t.id}`;
  let question, force = false;
  if (!plan.restore.length && plan.conflicts.length) {
    question = `Changed since momo wrote ${plan.conflicts.length > 1 ? "them" : "it"}: ${plan.conflicts.join(", ")}. Overwrite with the version from before?`;
    force = true;
  } else {
    question = `Undo ${scope}? Restores ${plan.restore.join(", ") || "nothing"}`
      + (plan.conflicts.length ? `; skips ${plan.conflicts.join(", ")} (changed since)` : "")
      + (plan.lost.length ? `; can't restore ${plan.lost.join(", ")}` : "")
      + (plan.ran_commands ? ". Command changes aren't tracked." : ".");
  }
  if (plan.plan_progress) question += ` ${plan.plan_progress}.`;
  if (!(await confirmInline(host, question, force ? "Overwrite" : "Undo"))) return;
  try { await hooks.post("api/undo", { checkpoint: t.id, force }); }
  catch (e) { toast(e.message, "error"); }
  await load();
}

// ── the diff of one file ─────────────────────────────────────────────────────
async function openDiff(f, checkpoint) {
  const q = `path=${encodeURIComponent(f.path)}` + (checkpoint != null ? `&checkpoint=${checkpoint}` : "");
  let d;
  try {
    const r = await fetch(`api/changes/diff?${q}`);
    d = await r.json();
    if (!r.ok) throw new Error(d.error);
  } catch (e) { toast(e.message, "error"); return; }
  ui.shown = { ...d, checkpoint };
  $("#changes-list").hidden = $("#changes-foot").hidden = true;
  $("#change-preview").hidden = false;
  $("#change-name").textContent = f.from ? `${f.from} → ${d.rel}` : d.rel;
  $("#change-name").title = d.path;
  $("#change-meta").textContent = `${OPS[d.op]}` + (checkpoint != null ? ` in #${checkpoint}` : " since keep");
  $("#change-open").hidden = d.op === "D";
  $("#change-revert").disabled = !d.restorable || hooks.busy();
  $("#change-revert").title = d.restorable
    ? "Put this file back as it was at the last keep (in every checkpoint)"
    : "Too big to save, or a folder: only an undo that moves it back restores it";
  const ev = { path: d.rel, op: d.op === "D" ? "delete" : "edit", is_new: d.op === "A",
               added: d.added, removed: d.removed, body: d.body };
  const box = d.body.length ? diffBox(ev, { style: hooks.diffStyle(), max: 400 })
    : el("div", "muted small changes-note", d.op === "R" ? "Renamed, content unchanged." : "No line changes.");
  $("#change-diff").replaceChildren(box);
  $("#change-diff").scrollTop = 0;
}

function closeDiff() {
  ui.shown = null;
  $("#change-preview").hidden = true;
  $("#changes-list").hidden = $("#changes-foot").hidden = false;
}

function diffText(d) {
  const head = `--- ${d.op === "A" ? "/dev/null" : "a/" + (d.from || d.rel)}\n+++ ${d.op === "D" ? "/dev/null" : "b/" + d.rel}\n`;
  return head + d.body.map((r) => r[3]).join("\n") + "\n";
}

async function revertShown(force = false) {
  const d = ui.shown;
  if (!d) return;
  const q = force ? `${d.rel} changed since momo wrote it. Overwrite it with the version from the last keep?`
                  : `Put ${d.rel} back as it was at the last keep?`;
  if (!(await confirmInline($("#change-actions"), q, force ? "Overwrite" : "Revert"))) return;
  let res;
  try { res = await hooks.post("api/changes/revert", { path: d.path, force }); }
  catch (e) { toast(e.message, "error"); return; }
  if (!force && /changed since/.test(res.message || "")) { revertShown(true); return; }
  closeDiff();
  await load();
}

// ── from app.js ──────────────────────────────────────────────────────────────

// The drawer was opened (optionally at one file's diff, from a diff in the chat).
export async function openChanges(rel = null) {
  closeDiff();
  await load();
  if (!rel || !ui.data) return;
  for (const t of ui.data.turns) {
    const f = t.files.find((x) => x.rel === rel);
    if (f) { ui.open.add(t.id); render(); await openDiff(f, t.id); return; }
  }
}

// A status event: the badge, and a refresh of the open drawer when the stack moved.
let last = "";
export function applyChangesStatus(s) {
  const n = s.changes_files || 0;
  const badge = $("#changes-count");
  badge.hidden = !n;
  badge.textContent = n > 99 ? "99+" : String(n);
  const label = n ? `Changes: ${n} file${n !== 1 ? "s" : ""} in ${s.undo_count} checkpoint${s.undo_count !== 1 ? "s" : ""} since the last keep`
                  : "Changes (nothing to undo)";
  $("#changes-btn").title = label;
  $("#changes-btn").setAttribute("aria-label", label);
  const key = `${s.undo_count}/${n}`;
  if (key !== last) { last = key; changesDirty(); }
}

// Something may have changed on disk (a diff event): reload if open, soon.
export function changesDirty() {
  if ($("#changes-drawer").hidden) return;
  clearTimeout(ui.timer);
  ui.timer = setTimeout(() => { if (!ui.shown) load(); }, 250);
}

export function initChanges(h) {
  Object.assign(hooks, h);
  makeResizable($("#changes-drawer"), "right", "momo.changesWidth", 420);
  for (const b of document.querySelectorAll("#changes-views button")) {
    b.onclick = () => { ui.view = b.dataset.view; render(); };
  }
  $("#changes-keep").onclick = async () => {
    const files = ui.data?.since_keep.length || 0;
    if (!(await confirmInline($("#changes-foot"), `Keep all changes${files ? ` to ${files} file${files !== 1 ? "s" : ""}` : ""}? Undo can't go back past this point.`, "Keep all", false))) return;
    try { await hooks.post("api/keep"); } catch (e) { toast(e.message, "error"); }
    await load();
  };
  $("#change-back").onclick = () => { closeDiff(); render(); };
  $("#change-revert").onclick = () => revertShown(false);
  $("#change-open").onclick = () => { if (ui.shown) hooks.showFile(ui.shown.rel); };
  $("#change-copy").onclick = async () => {
    if (!ui.shown) return;
    try { await copyText(diffText(ui.shown)); flash($("#change-copy"), "Copied ✓"); }
    catch { flash($("#change-copy"), "Copy failed", false); }
  };
}
