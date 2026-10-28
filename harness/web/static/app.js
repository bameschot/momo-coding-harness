// momo web UI — mirrors the curses TUI (harness/tui.py) over SSE + JSON POSTs.
// No dependencies. All model/tool text is HTML-escaped before it reaches the DOM.
// This module holds the transcript, status bar, composer and popovers; the
// drawers, notifications and the companion live in their own modules.

import { initChips, refreshChipMenus, renderChipMenu } from "./chips.js";
import { applyMcpStatus, initMcp, refreshMcpMenu } from "./mcp.js";
import { applySkillsStatus, initSkills, refreshSkillsMenu } from "./skills.js";
import { applyCompanion, setCompanionFrames, startCompanion } from "./companion.js";
import { $, closeLayer, confirmInline, copyText, copyWithFeedback, el, formatSize, icon, loadJSON,
         makeResizable, openLayer, saveJSON, toast } from "./dom.js";
import { applyChangesStatus, changesDirty, initChanges, openChanges } from "./changes.js";
import { diffBox } from "./diff.js";
import { initFiles, openFiles, showFile } from "./files.js";
import { clamped, closeFind, findOpen, initFind, openFind, refreshFind } from "./find.js";
import { addCopyButtons, renderMarkdown } from "./markdown.js";
import { initNotify, noteActivity, notify, signal, syncNotifyControls } from "./notify.js";
import { initPalette, openPalette } from "./palette.js";
import { initSessions, openSessions } from "./sessions.js";

const transcript = $("#transcript");
const input = $("#input");

// ── view options (per browser, like the TUI's local toggles) ──────────────────
const VIEW_DEFAULTS = { tools: true, group: true, think: true, md: true, diff: true, diffStyle: "compact",
                        companion: true, size: "m", wide: false };
const view = { ...VIEW_DEFAULTS, ...loadJSON("momo.view", {}) };
// CommandResult view fields → our keys
const VIEW_MAP = { tool_output: "tools", think_output: "think", md_render: "md",
                   diff_output: "diff", diff_style: "diffStyle", companion: "companion" };

const saveView = () => saveJSON("momo.view", view);

// ── theme and text size (per browser) ────────────────────────────────────────
// momo.theme holds "light" or "dark"; absent = follow the system.  index.html
// applies a saved choice before the first paint.
const darkQuery = matchMedia("(prefers-color-scheme: dark)");
const THEMES = ["system", "light", "dark"];
let themeChoice = ["light", "dark"].includes(loadJSON("momo.theme", null)) ? loadJSON("momo.theme", null) : "system";
function currentTheme() {
  return themeChoice === "system" ? (darkQuery.matches ? "dark" : "light") : themeChoice;
}
function setTheme(choice) {
  themeChoice = THEMES.includes(choice) ? choice : "system";
  if (themeChoice === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = themeChoice;
  saveJSON("momo.theme", themeChoice === "system" ? undefined : themeChoice);
  renderThemeBtn();
}
// The button cycles System → Light → Dark; its icon and title say which is on.
function renderThemeBtn() {
  const next = THEMES[(THEMES.indexOf(themeChoice) + 1) % THEMES.length];
  const now = themeChoice === "system" ? `follows the system (${currentTheme()} now)` : themeChoice;
  const label = `Theme: ${now} — click for ${next}`;
  const btn = $("#theme-btn");
  btn.title = label;
  btn.setAttribute("aria-label", label);
  btn.querySelector("use").setAttribute("href",
    { system: "#i-contrast", light: "#i-sun", dark: "#i-moon" }[themeChoice]);
  for (const r of document.querySelectorAll("input[name=theme]")) r.checked = r.value === themeChoice;
}
$("#theme-btn").onclick = () => setTheme(THEMES[(THEMES.indexOf(themeChoice) + 1) % THEMES.length]);
for (const r of document.querySelectorAll("input[name=theme]")) r.onchange = () => setTheme(r.value);
darkQuery.addEventListener("change", renderThemeBtn);
renderThemeBtn();

// Text size (S/M/L) and the wide transcript.  Medium leaves the stylesheet's
// size, which is smaller on a phone.
const TEXT_SIZES = { s: "14px", l: "17px" };
function applyAppearance() {
  const root = document.documentElement.style;
  if (TEXT_SIZES[view.size]) root.setProperty("--base-fs", TEXT_SIZES[view.size]);
  else root.removeProperty("--base-fs");
  document.body.classList.toggle("wide", !!view.wide);
  for (const r of document.querySelectorAll("input[name=text-size]")) r.checked = r.value === (view.size || "m");
}
function setTextSize(size) {
  view.size = size;
  saveView();
  applyAppearance();
}
for (const r of document.querySelectorAll("input[name=text-size]")) r.onchange = () => setTextSize(r.value);

// ── shared state ──────────────────────────────────────────────────────────────
let events = [];            // transcript events, for re-rendering on view changes
let state = null;           // last /api/state snapshot
let status = {};            // last status event
let busy = false, waiting = false;
let history = [], histIdx = -1, histStash = "";
let queue = [];             // messages typed while busy: {text, atts}
let editing = null;         // {attachments: [names]} while editing the last message
let connectedAt = 0;        // ignore backlog replay for notifications
let busySince = 0;
// From /api/state, with the values they have today until it arrives: the tools
// whose result can come as diffs instead (harness.DIFF_TOOLS), and the ending of
// a /command's y/N question (controller.CONFIRM_SUFFIX).
let diffTools = new Set(["append_to_file", "delete_file", "edit_file", "edit_files",
                         "move_file", "replace_symbol", "write_file"]);
let confirmSuffix = " [y/N]";
let lastServerError = null;   // {text, at}: the newest ERROR line, for settings feedback

// ── helpers ───────────────────────────────────────────────────────────────────
async function post(path, body = {}) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    // JSON errors carry {error}; the rest are plain text.
    const text = await r.text();
    let msg = text;
    try { msg = JSON.parse(text).error || text; } catch { /* plain text */ }
    throw new Error(`${r.status} ${msg}`.trim());
  }
  return r.json();
}

// ── transcript rendering ──────────────────────────────────────────────────────
// Render context: tool calls waiting for their result, the open ask card, and
// the group the current stretch of tool calls goes into (view.group).
let pendingTools = [];
let openAsk = null;
let group = null;   // {el, body, calls, names: Map, files: Set, errors}

function resetRenderContext() { pendingTools = []; openAsk = null; group = null; }

// Tool calls, their results and diffs — and the thinking between them — collect
// in one collapsible group per stretch of a turn; any other event ends it.
const GROUPED = new Set(["tool_call", "tool_result", "diff", "think"]);

function renderEvent(ev) {
  if (!GROUPED.has(ev.type)) closeGroup();
  const node = renderOne(ev);
  if (!node || !view.group || !GROUPED.has(ev.type)) return node;
  if (!group && ev.type === "think") return node;   // thinking before a plain reply
  const fresh = !group;
  if (fresh) group = newGroup();
  group.body.append(node);
  if (ev.type === "tool_call") {
    group.calls++;
    group.names.set(ev.name, (group.names.get(ev.name) || 0) + 1);
    const pending = pendingTools[pendingTools.length - 1];
    if (pending?.el === node) pending.group = group;
  } else if (ev.type === "diff" && ev.path) {
    group.files.add(ev.path);
  }
  updateGroup(group);
  return fresh ? group.el : null;
}

function renderOne(ev) {
  const node = renderKind(ev);
  if (node && ev.ts) stampTime(node, ev.ts);
  return node;
}

// When it happened (EventBus stamps live events; a loaded session has no times).
function timeLabel(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === now.toDateString() ? hm
    : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${hm}`;
}
function stampTime(node, ts) {
  const when = timeLabel(ts);
  const full = new Date(ts * 1000).toLocaleString();
  if (node.classList.contains("assistant")) {
    const t = el("span", "msg-time", when);
    t.title = full;
    node.querySelector(".msg-actions")?.prepend(t);
  } else if (node.classList.contains("user")) {
    node.querySelector(".bubble").title = full;
  } else if (node.matches("details.tool, .ask")) {
    (node.querySelector("summary") || node).title = full;
  } else if (node.classList.contains("turn-meta")) {
    node.append(` · ${when}`);
  }
}

function renderKind(ev) {
  switch (ev.type) {
    case "user": return renderUser(ev.text);
    case "chat": return renderChat(ev.role, ev.text);
    case "error": return renderChat("system", `ERROR: ${ev.text}`, "error");
    case "think": return renderThink(ev.text);
    case "tool_call": return renderToolCall(ev.name, ev.args);
    case "tool_result": return renderToolResult(ev.name, ev.result);
    case "diff": return renderDiff(ev);
    case "ask_user": return renderAsk(ev.question);
    case "done": return renderTurnMeta(ev);
  }
  return null;
}

function newGroup() {
  const d = el("details", "tool-group");
  d.open = true;                      // open while its turn runs
  const sum = el("summary");
  sum.append(el("span", "group-count"), el("span", "group-names"), el("span", "group-err"));
  const body = el("div", "tool-group-body");
  d.append(sum, body);
  return { el: d, sum, body, calls: 0, names: new Map(), files: new Set(), errors: 0 };
}

function updateGroup(g) {
  const s = (n) => (n === 1 ? "" : "s");
  const files = g.files.size ? ` · ${g.files.size} file${s(g.files.size)} changed` : "";
  g.sum.querySelector(".group-count").textContent = `${g.calls} tool call${s(g.calls)}${files}`;
  g.sum.querySelector(".group-names").textContent =
    [...g.names].map(([n, k]) => (k > 1 ? `${n} ×${k}` : n)).join(", ");
  g.sum.querySelector(".group-err").textContent = g.errors ? `${g.errors} failed` : "";
  g.el.classList.toggle("single", g.calls <= 1);
}

// The stretch is over: a long, clean one folds up to its summary line.
function closeGroup() {
  if (!group) return;
  if (group.calls >= 3 && !group.errors) group.el.open = false;
  group = null;
}

function renderUser(text) {
  const wasAnswer = !!openAsk;
  if (openAsk) { markAnswered(openAsk, text); openAsk = null; }
  const wrap = el("div", "msg user");
  // Answers to questions and /commands can't be retried or edited.
  if (wasAnswer || text.startsWith("/")) wrap.dataset.noActions = "1";
  const bubble = el("div", "bubble");
  // "📎 name (N chars)" lines are attachment summaries — show them as chips.
  text.split("\n").forEach((line, i) => {
    if (i) bubble.append("\n");
    if (line.startsWith("📎 ")) {
      const chip = el("span", "att-line");
      chip.append(icon("paperclip", "icon sm"), line.slice(3));
      bubble.append(chip);
    } else bubble.append(line);
  });
  wrap.append(bubble);
  return wrap;
}

function renderChat(role, text, extra = "") {
  const wrap = el("div", `msg ${role} ${extra}`.trim());
  if (role === "assistant") {
    if (view.md) {
      wrap.classList.add("md");
      wrap.innerHTML = renderMarkdown(text);
      addCopyButtons(wrap);
    } else {
      wrap.classList.add("plain");
      wrap.textContent = text;
    }
    wrap.append(msgActions([["copy", "Copy", "Copy this reply as Markdown", (btn) => copyWithFeedback(btn, text)]]));
    return wrap;
  }
  wrap.textContent = text;
  if (role === "system" && text.startsWith("ERROR")) wrap.classList.add("error");
  // `[y/N]` confirmation prompts from /commands get quick-answer buttons.
  if (isConfirmPrompt(role, text)) return askCard(text, true, "Confirm", false);
  return wrap;
}

// A /command confirmation (Controller._pending_confirm) arrives as a plain system
// message, not as an ask_user event — both the renderer and the notifier need to
// recognise it, so the test lives in one place.
const isConfirmPrompt = (role, text) => role === "system" && text.endsWith(confirmSuffix);

function renderThink(text) {
  if (!view.think) return null;
  const d = el("details", "think");
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  d.append(el("summary", "", `thinking (${words} words)`), el("div", "think-body", text));
  return d;
}

function argsBrief(args, skip = []) {
  const parts = Object.entries(args || {}).filter(([k]) => !skip.includes(k)).map(([k, v]) => {
    let s = typeof v === "string" ? v : JSON.stringify(v);
    if (s.length > 60) s = s.slice(0, 57) + "…";
    return `${k}=${JSON.stringify(s)}`;
  });
  return parts.join("  ");
}

// A clickable workspace path (app-wide [data-path] handler opens the preview).
function pathLink(path, line = 0, cls = "tpath") {
  const a = el("span", cls, path);
  a.dataset.path = path;
  if (line > 0) a.dataset.line = String(line);
  a.title = "Open in the workspace preview";
  return a;
}

// mcp__github__create_issue → "github › create_issue" with an MCP tag.
function toolName(name) {
  const m = /^mcp__(.+?)__(.+)$/.exec(name);
  if (!m) return el("span", "tname", name);
  const span = el("span", "tname");
  span.title = name;
  span.append(el("span", "mcp-tag", "MCP"), ` ${m[1]} › ${m[2]}`);
  return span;
}

function renderToolCall(name, args) {
  if (!view.tools) {
    // Collapsed tool output: one abbreviated line, like the TUI.
    const full = `▶ ${name}(${JSON.stringify(args)})`;
    const row = el("div", "tool");
    const head = el("div", "tool-head");
    head.append(el("span", "targs", full.length > 80 ? full.slice(0, 80) + "…" : full));
    row.append(head);
    return row;
  }
  const d = el("details", "tool");
  const s = el("summary");
  const st = el("span", "tstate run", "running");
  const path = typeof args?.path === "string" ? "path" : typeof args?.src === "string" ? "src" : null;
  s.append(el("span", "", "▶"), toolName(name));
  if (path) s.append(pathLink(args[path], Number(args.start_line) || 0));
  s.append(el("span", "targs", argsBrief(args, path ? [path] : [])), st);
  const body = el("div", "tool-body");
  body.append(el("pre", "args", JSON.stringify(args, null, 2)));
  d.append(s, body);
  pendingTools.push({ name, el: d, state: st, body });
  return d;
}

function clampedPre(text, maxLines) {
  const frag = document.createDocumentFragment();
  const lines = String(text).split("\n");
  const pre = el("pre", "", lines.slice(0, maxLines).join("\n") || "(empty)");
  frag.append(pre);
  if (lines.length > maxLines) {
    const more = el("button", "link-btn more", `show all (${lines.length - maxLines} more lines)`);
    more.type = "button";
    const expand = () => { pre.textContent = text; more.remove(); clamped.delete(pre); };
    more.onclick = expand;
    clamped.set(pre, { text, expand });   // find.js searches the hidden lines too
    frag.append(more);
  }
  return frag;
}

function copyButton(text, label = "Copy output") {
  const b = el("button", "btn small quiet", label);
  b.type = "button";
  b.onclick = () => copyWithFeedback(b, text);
  return b;
}

function renderToolResult(name, result) {
  if (!view.tools) return null;
  const idx = pendingTools.findIndex((t) => t.name === name);
  const text = String(result ?? "");
  const isErr = /^ERROR/.test(text);
  const lines = text.split("\n").length;
  if (idx >= 0) {
    const t = pendingTools.splice(idx, 1)[0];
    t.state.className = `tstate ${isErr ? "err" : "ok"}`;
    t.state.textContent = isErr ? "✗ error" : `${lines} line${lines === 1 ? "" : "s"}`;
    t.body.append(clampedPre(text, 20), copyButton(text));
    if (isErr) {
      t.el.open = true;
      if (t.group) { t.group.errors++; updateGroup(t.group); }
    }
    return null;
  }
  // Orphan result (e.g. replay without its call) — render standalone.
  const d = el("details", "tool");
  const s = el("summary");
  s.append(el("span", "", "→"), el("span", "tname", name || "result"),
           el("span", "targs", ""), el("span", `tstate ${isErr ? "err" : "ok"}`, `${lines} lines`));
  const body = el("div", "tool-body");
  body.append(clampedPre(text, 20), copyButton(text));
  d.append(s, body);
  return d;
}

const DIFF_MAX = 40;
function renderDiff(ev) {
  // A diff replaces the result of the file-changing call it follows (the latest
  // such call still open; edit_files sends one diff per file after its call).
  const idx = pendingTools.findLastIndex((t) => diffTools.has(t.name));
  if (idx >= 0 && view.tools) {
    const t = pendingTools.splice(idx, 1)[0];
    t.state.className = "tstate ok";
    t.state.textContent = "applied";
  }
  if (!view.diff) return null;
  return diffBox(ev, { style: view.diffStyle, link: (p) => pathLink(p, 0, "dpath"), max: DIFF_MAX,
                       onChanges: ev.op === "move" ? null : () => showChanges(ev.path) });
}

// What a finished turn cost, under its reply (DoneEvent; zero for compaction).
function renderTurnMeta(ev) {
  if (!ev.secs && !ev.eval_tokens && !ev.tool_calls) return null;
  const parts = [ev.secs >= 0.1 ? `${ev.secs.toFixed(1)} s` : "<0.1 s"];
  if (ev.prompt_tokens || ev.eval_tokens) parts.push(`${fmtTok(ev.prompt_tokens)} → ${fmtTok(ev.eval_tokens)} tokens`);
  if (ev.tool_calls) parts.push(`${ev.tool_calls} tool call${ev.tool_calls === 1 ? "" : "s"}`);
  const meta = el("div", "turn-meta", parts.join(" · "));
  meta.title = "Time for the turn · the last request's prompt → tokens generated in the turn · tool calls";
  return meta;
}

function renderAsk(question) {
  const yn = /Reply 'y'|y = run it|\[y\/N\]/.test(question);
  return askCard(question, yn, "momo asks", true, /'a' to always allow/.test(question));
}

// A question card.  yn: Yes/No buttons; answerable: a field for a typed answer
// (ask_user can take any text — guidance, a choice, plan feedback).
function askCard(question, yn, title, answerable, always = false) {
  const card = el("div", "ask");
  card.append(el("div", "ask-title", `? ${title}`), el("div", "ask-q", question));
  const controls = el("div", "ask-controls");
  if (answerable) {
    const form = el("div", "ask-answer");
    const field = el("input");
    field.type = "text";
    field.placeholder = yn ? "…or type an answer" : "Type your answer";
    field.setAttribute("aria-label", "Your answer");
    const go = el("button", "btn small primary", "Answer");
    go.type = "button";
    const answer = () => { if (field.value.trim()) send(field.value.trim()); };
    go.onclick = answer;
    field.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); answer(); } };
    form.append(field, go);
    controls.append(form);
  }
  if (yn) {
    const actions = el("div", "ask-actions");
    const yes = el("button", "btn primary", "Yes (y)");
    const no = el("button", "btn", "No");
    yes.type = no.type = "button";
    yes.onclick = () => send("y");
    no.onclick = () => send("n");
    actions.append(yes);
    if (always) {
      const all = el("button", "btn", "Always (a)");
      all.type = "button";
      all.title = "Allow this tool for the rest of the session";
      all.onclick = () => send("a");
      actions.append(all);
    }
    actions.append(no);
    controls.append(actions);
  }
  if (controls.childElementCount) card.append(controls);
  openAsk = card;
  return card;
}

function markAnswered(card, answer) {
  card.classList.add("answered");
  card.querySelector(".ask-controls")?.remove();
  card.querySelector(".ask-title").textContent += ` — answered: ${answer.length > 40 ? answer.slice(0, 40) + "…" : answer}`;
}

// ── message actions ───────────────────────────────────────────────────────────
function msgActions(items, cls = "") {
  const bar = el("div", `msg-actions ${cls}`.trim());
  for (const [ic, label, title, fn] of items) {
    const b = el("button", "btn small quiet");
    b.append(icon(ic, "icon sm"), el("span", "", label));
    b.type = "button";
    b.title = title;
    b.onclick = () => fn(b);
    bar.append(b);
  }
  return bar;
}

// Retry / Edit sit on the last message you typed (not answers or /commands).
function refreshUserActions() {
  transcript.querySelectorAll(".user-actions").forEach((n) => n.remove());
  const users = [...transcript.querySelectorAll(".msg.user:not([data-no-actions])")];
  const last = users[users.length - 1];
  if (!last) return;
  last.append(msgActions([
    ["retry", "Retry", "Send this message again and replace the reply", () => post("api/retry").catch(showError)],
    ["pencil", "Edit", "Edit this message and send it again", startEdit],
  ], "user-actions"));
}

async function startEdit() {
  let info;
  try {
    const r = await fetch("api/last-user");
    info = await r.json();
  } catch (e) { return showError(e); }
  if (!info || info.typed === undefined) return;
  showEdit(info.typed, info.attachments || []);
}

function showEdit(typed, attachmentNames) {
  editing = { attachments: attachmentNames };
  input.value = typed;
  $("#edit-atts").textContent = attachmentNames.length ? ` (keeps ${attachmentNames.join(", ")})` : "";
  $("#edit-banner").hidden = false;
  autosize();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function stopEdit() {
  editing = null;
  $("#edit-banner").hidden = true;
}
$("#edit-cancel").onclick = () => { stopEdit(); input.value = ""; autosize(); input.focus(); };

function showError(e) {
  handleEvents([{ type: "error", text: `could not reach momo: ${e.message || e}` }]);
}

// ── scrolling ─────────────────────────────────────────────────────────────────
const nearBottom = () => transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;

function appendNodes(nodes) {
  const pinned = nearBottom();
  let lastAssistant = null;
  for (const n of nodes) {
    if (live$) transcript.insertBefore(n, live$.box);
    else transcript.append(n);
    if (n.classList?.contains("assistant")) lastAssistant = n;
  }
  if (!nodes.length) return;
  refreshUserActions();
  if (pinned) {
    // Long replies: show their beginning rather than their end (as the TUI does).
    if (lastAssistant && lastAssistant.offsetHeight > transcript.clientHeight * 0.8) {
      transcript.scrollTop = lastAssistant.offsetTop - 12;
    } else {
      transcript.scrollTop = transcript.scrollHeight;
    }
  } else {
    $("#new-msgs").hidden = false;
  }
}

transcript.addEventListener("scroll", () => { if (nearBottom()) $("#new-msgs").hidden = true; });
$("#new-msgs").onclick = () => { transcript.scrollTop = transcript.scrollHeight; $("#new-msgs").hidden = true; };

function rerenderAll() {
  const atBottom = nearBottom();
  const keep = live$?.box;  // a view toggle mid-stream keeps the live preview
  transcript.replaceChildren();
  resetRenderContext();
  const frag = document.createDocumentFragment();
  for (const ev of events) {
    const n = renderEvent(ev);
    if (n) frag.append(n);
  }
  transcript.append(frag);
  if (keep) transcript.append(keep);
  if (!busy) closeGroup();   // a finished turn's last stretch folds like the others
  renderWelcome();
  refreshFind();
  refreshUserActions();
  if (openAsk && !waiting && !openAsk.classList.contains("answered")) {
    // Replayed question that has since been answered elsewhere — keep, but no buttons.
    openAsk.querySelector(".ask-controls")?.remove();
  }
  if (atBottom) transcript.scrollTop = transcript.scrollHeight;
}

// ── status / busy ─────────────────────────────────────────────────────────────
const SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
let spinI = 0;

// Green: connected to momo and momo reaches the model server; orange: connected
// but the model server is not reachable; red: the connection to momo is down.
let connected = false;
function updateConnDot() {
  const dot = $("#conn-dot");
  const offline = connected && status.backend_reachable === false;
  dot.classList.toggle("on", connected);
  dot.classList.toggle("offline", offline);
  dot.title = !connected ? "Disconnected — retrying"
    : offline ? `Connected — model server not reachable at ${status.host}`
    : "Connected";
}

function applyStatus(s) {
  const planChanged = s.plan_progress !== status.plan_progress;
  const modeChanged = s.mode !== status.mode;
  status = s;
  if (modeChanged) renderWelcome();
  const modeSel = $("#mode");
  if (modeSel.value !== s.mode) modeSel.value = s.mode;
  const pp = $("#plan-progress");
  pp.hidden = !s.plan_progress;
  pp.textContent = s.plan_progress;
  updatePlanKind();
  const offline = s.backend_reachable === false;
  $("#model").textContent = offline ? `${s.model} · offline` : s.model;
  $("#model-btn").classList.toggle("offline", offline);
  $("#model-btn").title = offline
    ? `Server not reachable at ${s.host} — showing the last known model. /host <url> points momo elsewhere.`
    : "Switch model";
  $("#host").textContent = `${s.provider}@${s.host.replace(/^https?:\/\//, "")}`;
  applyChangesStatus(s);
  updateConnDot();
  $("#workdir").textContent = s.workdir;
  $("#workdir").title = `${s.workdir} — click to browse, right-click to copy`;
  $("#ctx-pct").textContent = `${s.ctx_pct}%`;
  $("#ctx-fill").style.width = `${Math.min(100, s.ctx_pct)}%`;
  $(".ctx").className = `ctx ${s.ctx_color}`;
  if (!$("#ctx-menu").hidden) scheduleCtxRefresh();
  if (!$("#index-menu").hidden) scheduleIndexRefresh();
  if (!$("#tools-menu").hidden) scheduleToolsRefresh();
  if (!$("#prompt-drawer").hidden) schedulePromptRefresh();
  const off = s.tools_off || [];
  $("#tools-off").hidden = !off.length;
  $("#tools-off").textContent = String(off.length);
  const waiting = s.workspace_pending || [];
  $("#tools-pending").hidden = !waiting.length;
  $("#tools-pending").textContent = String(waiting.length);
  const toolsLabel = (off.length ? `Tools for this mode — ${off.length} off: ${off.join(", ")}`
                                 : "Tools for this mode (/tools)")
    + (waiting.length ? ` — ${waiting.length} workspace tool${waiting.length === 1 ? "" : "s"} `
                        + `waiting for approval: ${waiting.join(", ")}` : "");
  $("#tools-btn").title = toolsLabel;
  $("#tools-btn").setAttribute("aria-label", toolsLabel);
  $("#tools-badge").hidden = s.tools_enabled;
  // Chip colours: .active = a setting you turned on, .warn = something off or
  // less safe than the default.
  const run = $("#run-badge");
  run.textContent = s.run_confirm ? "RUN: confirm" : "RUN: auto";
  run.classList.toggle("active", s.run_confirm);
  run.title = s.run_confirm ? "Running commands — momo asks before each one" : "Running commands — without asking";
  const netOn = s.net_access !== "off";
  const net = $("#net-badge");
  net.classList.toggle("active", netOn && s.net_confirm);
  net.classList.toggle("warn", netOn && !s.net_confirm);
  net.textContent = netOn ? `NET: ${s.net_access}${s.net_confirm ? "" : " (writes: auto)"}` : "NET: off";
  const cached = (s.net_cache || []).length;
  net.title = (netOn ? `Internet access: ${s.net_access === "local" ? "public and local" : "public"}` : "Internet access: off")
    + (cached ? ` · ${cached} page${cached === 1 ? "" : "s"} cached` : "");
  $("#run-confirm").checked = !!s.run_confirm;
  $("#net-on").checked = netOn;
  $("#net-local").checked = s.net_access === "local";
  $("#net-local").disabled = !netOn;
  $("#net-confirm").checked = s.net_confirm;
  $("#guides").checked = s.guides;
  $("#run-mode").checked = s.run_mode !== "classic";
  const rl = $("#run-output-limit");
  if (document.activeElement !== rl && s.run_output_limit != null)
    rl.value = s.run_output_limit ? String(s.run_output_limit) : "off";
  const um = $("#undo-max");
  if (document.activeElement !== um && s.undo_max != null) um.value = s.undo_max ? String(s.undo_max) : "off";
  const mo = $("#max-output-tokens");
  if (document.activeElement !== mo && s.max_output_tokens != null)
    mo.value = s.max_output_tokens ? String(s.max_output_tokens) : "off";
  $("#net-confirm").disabled = !netOn;
  const mb = $("#net-max-bytes");
  if (document.activeElement !== mb) mb.value = formatSize(s.net_max_bytes);
  mb.disabled = !netOn;
  const mc = $("#net-max-chars");
  if (document.activeElement !== mc) mc.value = s.net_max_chars ?? "";
  mc.disabled = !netOn;
  applyIndexStatus(s);
  applyThinkStatus(s);
  applyCavecatStatus(s);
  applyMcpStatus(s);
  applySkillsStatus(s);
  refreshChipMenus();
  updateTitle();
  if (planChanged) refreshState();
  else if (!$("#plan-drawer").hidden) schedulePlanRefresh();
}

// The THINK badge and its menu: the chosen level, what the model is actually
// sent when that differs, and one radio per value this model takes (plus the
// chosen one, kept for a model that does take it).
function applyThinkStatus(s) {
  const level = s.think_level || "on", eff = s.think_effective || level;
  const choices = s.think_choices || [];
  const badge = $("#think-badge");
  const clamped = eff !== level && eff !== "n/a";
  badge.textContent = eff === "n/a" ? "THINK: n/a" : clamped ? `THINK: ${level}→${eff}` : `THINK: ${level}`;
  badge.classList.toggle("warn", clamped);
  badge.classList.toggle("muted", eff === "n/a");
  const levels = choices.filter((c) => !["off", "on"].includes(c));
  const note = !s.think_known ? `The server doesn't say what ${s.model} takes, so on/off is sent.`
    : eff === "n/a" ? `${s.model} doesn't take a thinking setting.`
    : clamped ? `${s.model} gets ${eff}: it ${level === "off" ? "can't turn thinking off"
        : levels.length ? "takes " + levels.join("/") : "takes on/off only"}.`
    : `${s.model} takes: ${choices.join(", ")}.`;
  badge.title = `Thinking level (/think) — ${note}`;
  $("#think-note").textContent = note;
  const values = choices.includes(level) ? choices : [...choices, level];
  for (const r of document.querySelectorAll("input[name=think-history]")) {
    r.checked = r.value === (s.think_history || "turn");
  }
  for (const r of document.querySelectorAll("input[name=think-keep]")) {
    r.checked = r.value === (s.think_keep || "lean");
  }
  for (const r of document.querySelectorAll("input[name=tool-ref]")) {
    r.checked = r.value === (s.tool_ref || "minimal");
  }
  $("#think-levels").replaceChildren(...values.map((v) => {
    const r = el("input");
    r.type = "radio"; r.name = "think-level"; r.value = v; r.checked = v === level;
    r.onchange = () => send(`/think ${v}`);
    const label = el("label");
    label.append(r, ` ${v}`);
    if (!choices.includes(v)) {
      label.classList.add("unavailable");
      label.title = `${s.model} doesn't take ${v}; it gets ${eff}`;
    }
    return label;
  }));
}

// The CAVECAT chip, its menu and the View → Context radios: one setting, /cavecat.
function applyCavecatStatus(s) {
  const level = s.cavecat || "off";
  const chip = $("#cavecat-badge");
  chip.textContent = `CAVECAT: ${level}`;
  chip.classList.toggle("active", level !== "off");
  for (const r of document.querySelectorAll("input[name=cavecat], input[name=cavecat-view]")) {
    r.checked = r.value === level;
  }
}

// The INDEX badge and the View → Code index section, mirroring the NET ones.
function applyIndexStatus(s) {
  const on = !!s.index_enabled;
  const trouble = on && (s.index_degraded || s.index_state === "stopped");
  const badge = $("#index-badge");
  badge.classList.toggle("warn", trouble);
  badge.textContent = !on ? "INDEX: off"
    : s.index_progress ? `INDEX: ${s.index_state} ${s.index_progress}`
    : s.index_state === "stopped" ? "INDEX: stopped"
    : `INDEX: ${s.index_files} files · ${formatSize(s.index_mem)}/${formatSize(s.index_max_bytes)}`;
  badge.title = !on ? "Code index: off"
    : (s.index_degraded ? "Code index is over its memory budget and dropped a part. " : "")
      + "Code index — what it holds";
  $("#index-on").checked = on;
  $("#index-persist").checked = !!s.index_persist;
  $("#index-route").checked = s.index_route !== false;
  const im = $("#index-max-mem");
  if (document.activeElement !== im) im.value = formatSize(s.index_max_bytes);
  const imf = $("#index-max-files");
  if (document.activeElement !== imf && s.index_max_files) imf.value = String(s.index_max_files);
  const iw = $("#index-workers");
  if (document.activeElement !== iw) iw.value = s.index_workers ? String(s.index_workers) : "auto";
  $("#index-save").disabled = !on;
  $("#index-load").disabled = !on;
}


function updateTitle() {
  const name = status.title ? ` · ${status.title}` : "";
  document.title = `${notify.unseen ? "(•) " : ""}momo · ${status.mode || ""}${name}${busy ? " …" : ""}`;
  setFaviconDot(notify.unseen || waiting);
}

// The tab icon gets a dot while momo waits for you or did something while you
// were away — visible in a crowded tab strip where the title is cut off.
const favicon = { link: document.querySelector("link[rel=icon]"), plain: null, dotted: null, on: false };
fetch("favicon.svg").then((r) => r.text()).then((svg) => {
  favicon.plain = favicon.link.href;
  favicon.dotted = "data:image/svg+xml," + encodeURIComponent(svg.replace(/<\/svg>\s*$/,
    '<circle cx="25" cy="7" r="6" fill="#f08c00" stroke="#fff" stroke-width="1.5"/></svg>'));
  setFaviconDot(favicon.on);
}).catch(() => {});
function setFaviconDot(on) {
  favicon.on = on;
  if (favicon.dotted) favicon.link.href = on ? favicon.dotted : favicon.plain;
}

// ── notifications (notify.js does the telling) ────────────────────────────────
const live = () => Date.now() - connectedAt > 2000;  // skip the backlog replay after (re)connect
const NOTIFY_MIN_TURN_MS = 2000;  // below this a turn is an echo, not news

function lastAssistantText() {
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].type === "chat" && events[i].role === "assistant") return events[i].text;
  }
  return "";
}

function applyBusy(b, w) {
  const wasBusy = busy;
  if (b && !wasBusy) {
    busySince = Date.now();
    activity = { label: "thinking", since: busySince };
  }
  busy = b; waiting = w;
  document.body.classList.toggle("is-busy", busy);
  if (busy !== wasBusy) changesDirty();     // its undo buttons wait while a turn runs
  if (wasBusy && !busy) {
    if (Date.now() - busySince > NOTIFY_MIN_TURN_MS) {
      signal("finish", "momo finished", lastAssistantText().split("\n").find((l) => l.trim()) || "The turn is complete.");
    }
    // Send the next queued message once the turn is over.
    if (queue.length) setTimeout(sendNextQueued, 150);
  }
  $("#stop").hidden = !(busy && !waiting);
  // Enter while momo works queues the message: the button says so.
  $("#send-label").textContent = busy && !waiting ? "Queue" : "Send";
  $("#send").title = busy && !waiting ? "momo is working — this is sent when it's done" : "Send (Enter)";
  if (wasBusy && !busy) closeGroup();
  updatePlanKind();
  const bz = $("#busy");
  bz.classList.toggle("waiting", waiting);
  if (!busy) bz.textContent = "";
  else if (waiting) bz.textContent = "? waiting for your answer";
  input.placeholder = waiting ? "Answer momo…" : busy ? "momo is working… (Esc to interrupt)" : "Message momo…  (/ for commands)";
  if (!waiting && openAsk && !openAsk.classList.contains("answered")) openAsk.querySelector(".ask-controls")?.remove();
  // A question with an answer field takes the focus; otherwise the input box.
  // After this batch is on the page: the card may be in the same batch.
  if (waiting) setTimeout(() => (openAsk?.querySelector(".ask-answer input") || input).focus(), 0);
  updateTitle();
}

// What momo is doing right now, from the events: "running run_command",
// "writing", "thinking" — with the time spent on it.
let activity = { label: "thinking", since: Date.now() };
function setActivity(label) {
  if (activity.label !== label) activity = { label, since: Date.now() };
}
const clock = (ms) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
setInterval(() => {
  if (!busy || waiting) return;
  const now = Date.now();
  const bz = $("#busy");
  bz.textContent = `${SPIN[spinI++ % SPIN.length]} ${activity.label} · ${clock(now - activity.since)}`;
  bz.title = `This turn: ${clock(now - busySince)}`;
}, 100);

// ── event stream ──────────────────────────────────────────────────────────────
// Batched event delivery coalesces a burst into one render. It must NOT go through
// requestAnimationFrame alone: browsers pause rAF entirely in a hidden tab or a
// minimised window, so every event — including the turn-finished one a notification
// is meant to announce — would sit in the batch until you came back and looked,
// which is exactly when you no longer need telling. Fall back to a timer there.
const schedule = (fn) => (document.hidden ? setTimeout(fn, 0) : requestAnimationFrame(fn));

// The server sends a ping event every 15 s when there is nothing else.  A stream
// that stays silent much longer is dead without the browser noticing (a laptop
// that slept, Wi-Fi that went): drop it and connect again.
const STALL_MS = 40_000;
let es = null;
let lastHeard = 0;
let retryTimer = null;

function lostConnection() {
  es?.close();
  es = null;
  connected = false;
  updateConnDot();
  showOffline();
  renderQueue();
  clearTimeout(retryTimer);
  retryTimer = setTimeout(connect, 2000);
}

setInterval(() => {
  if (es && Date.now() - lastHeard > STALL_MS) lostConnection();
}, 5000);

function connect() {
  clearTimeout(retryTimer);
  es?.close();
  es = new EventSource("api/events");
  lastHeard = Date.now();
  es.addEventListener("ping", () => { lastHeard = Date.now(); });
  es.onopen = () => {
    lastHeard = Date.now();
    // Every (re)connect replays the full backlog: start from a clean slate.  That
    // includes a half-streamed reply: its stream_end is never replayed.
    connectedAt = Date.now();
    endStream();
    events = [];
    rerenderAll();
    connected = true;
    updateConnDot();
    showOnline();
    renderQueue();
    // Messages typed while offline go once momo is back and idle.
    refreshState().then(() => { if (queue.length && !busy) sendNextQueued(); });
  };
  es.onerror = () => {
    // The browser retries by itself unless it gave up (an HTTP error status).
    if (es.readyState === EventSource.CLOSED) return lostConnection();
    connected = false;
    updateConnDot();
    showOffline();
  };
  let batch = [];
  let scheduled = false;
  es.onmessage = (m) => {
    lastHeard = Date.now();
    batch.push(JSON.parse(m.data));
    if (!scheduled) {
      scheduled = true;
      schedule(() => {
        scheduled = false;
        const evs = batch;
        batch = [];
        handleEvents(evs);
      });
    }
  };
}

// A lost connection gets a bar over the input box (after a short grace: a
// reconnect within 1.5 s is not worth a flash), and "Reconnected" when it's back.
const offline = { timer: null, shown: false };
function showOffline() {
  if (offline.timer || offline.shown) return;
  offline.timer = setTimeout(() => {
    offline.timer = null;
    offline.shown = true;
    const bar = $("#offline-bar");
    bar.textContent = "Connection to momo lost — reconnecting… What you send now waits until it's back.";
    bar.classList.remove("back");
    bar.hidden = false;
  }, 1500);
}
function showOnline() {
  clearTimeout(offline.timer);
  offline.timer = null;
  if (!offline.shown) return;
  offline.shown = false;
  const bar = $("#offline-bar");
  bar.textContent = "Reconnected.";
  bar.classList.add("back");
  setTimeout(() => { if (connected && !offline.shown) bar.hidden = true; }, 2000);
}

function handleEvents(evs) {
  const nodes = [];
  for (const ev of evs) {
    switch (ev.type) {
      case "status": applyStatus(ev); break;
      case "busy": applyBusy(ev.busy, ev.waiting); break;
      case "companion": applyCompanion(ev); break;
      case "done": {                 // the turn footer (kept in events[] for re-renders)
        events.push(ev);
        const n = renderEvent(ev);
        if (n) nodes.push(n);
        break;
      }
      case "delta": streamDelta(ev); break;
      case "stream_end": endStream(); break;
      case "reset":
        endStream();
        events = [];
        transcript.replaceChildren();
        nodes.length = 0;
        resetRenderContext();
        renderWelcome();
        break;
      default: {
        events.push(ev);
        if (ev.type === "user") $(".welcome")?.remove();
        if (ev.type === "tool_call") setActivity(`running ${ev.name}`);
        else if (ev.type === "tool_result" || ev.type === "diff") setActivity("thinking");
        if (ev.type === "diff") changesDirty();
        if (ev.type === "error" || (ev.type === "chat" && ev.role === "system" && ev.text.startsWith("ERROR"))) {
          lastServerError = { text: ev.type === "error" ? ev.text : ev.text.replace(/^ERROR:\s*/, ""), at: Date.now() };
        }
        if (ev.type === "ask_user") signal("ask", "momo has a question", ev.question);
        else if (ev.type === "chat" && isConfirmPrompt(ev.role, ev.text)) {
          signal("ask", "momo needs a confirmation", ev.text);
        } else noteActivity();
        const n = renderEvent(ev);
        if (n) nodes.push(n);
      }
    }
  }
  appendNodes(nodes);
}

// ── streaming preview ─────────────────────────────────────────────────────────
// Deltas render into a live block that is not part of events[]; when the stream
// ends it is removed and the final think/chat events render as usual.  Each part
// grows one Text node with appendData: `textContent +=` re-serialises the whole
// reply on every delta, which is quadratic on a long reasoning stream.
let live$ = null;

function streamDelta(ev) {
  const pinned = nearBottom();
  if (!live$) {
    live$ = { box: el("div", "live-stream"), think: null, thinkText: null, content: null, contentText: null };
    transcript.append(live$.box);
    // A screen reader would read the reply piece by piece; it reads the final
    // message once the stream is over.
    transcript.setAttribute("aria-busy", "true");
  }
  setActivity(ev.kind === "thinking" ? "thinking" : "writing");
  if (ev.kind === "thinking") {
    if (!view.think) return;
    if (!live$.think) {
      live$.think = el("details", "think live");
      live$.think.open = true;
      live$.thinkText = document.createTextNode("");
      const body = el("div", "think-body");
      body.append(live$.thinkText);
      live$.think.append(el("summary", "", "thinking…"), body);
      live$.box.prepend(live$.think);
    }
    live$.thinkText.appendData(ev.text);
  } else {
    if (!live$.content) {
      live$.content = el("div", "msg assistant plain streaming");
      live$.contentText = document.createTextNode("");
      live$.content.append(live$.contentText);
      live$.box.append(live$.content);
      if (live$.think) live$.think.open = false;  // the answer started; fold the reasoning
    }
    live$.contentText.appendData(ev.text);
  }
  if (pinned) transcript.scrollTop = transcript.scrollHeight;
}

function endStream() {
  live$?.box.remove();
  live$ = null;
  transcript.removeAttribute("aria-busy");
}

async function refreshState() {
  try {
    const r = await fetch("api/state");
    if (!r.ok) return;
    state = await r.json();
  } catch { return; }
  const sel = $("#mode");
  if (!sel.options.length) {
    for (const m of state.modes) sel.append(new Option(m, m));
  }
  // The server's history (typed text, /token masked) is the source of truth: a
  // reconnect replays the backlog, so collecting user events would repeat it.
  if (histIdx === -1) history = [...state.history];
  applyStatus(state.status);
  applyBusy(state.busy, state.waiting);
  const tools = new Set(state.diff_tools || []);
  confirmSuffix = state.confirm_suffix || confirmSuffix;
  if (tools.size && (tools.size !== diffTools.size || [...tools].some((t) => !diffTools.has(t)))) {
    diffTools = tools;
    rerenderAll();
  }
  renderPlan();
  setCompanionFrames(state.companion);
  renderSkills();
  restoreDraft();
}

// The plan drawer: the plan, and the actions its phase allows.
function renderPlan() {
  $("#plan-btn").hidden = !state.plan;
  $("#plan-body").innerHTML = state.plan ? renderMarkdown(state.plan) : "<p class='muted'>No active plan.</p>";
  addCopyButtons($("#plan-body"));
  const phase = state.plan_phase || "";
  $("#plan-phase").textContent = phase;
  const executing = phase === "executing";
  $("#plan-run").hidden = !state.plan || executing;
  $("#plan-resume").hidden = !executing;
  $("#plan-cancel").hidden = !state.plan;
  if (!$("#plan-drawer").hidden) $("#plan-body .task-current")?.scrollIntoView({ block: "nearest" });
}

function renderSkills() {
  const box = $("#skills-list");
  const { available = [], active = [] } = state?.skills || {};
  if (!available.length) { box.replaceChildren(el("span", "muted", "No skills found.")); return; }
  box.replaceChildren(...available.map((name) => {
    const lab = el("label");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = active.includes(name);
    cb.onchange = async () => {
      await send(`/${cb.checked ? "load" : "unload"}-skill ${name}`);
      refreshState();  // the command has run by the time submit answers
    };
    lab.append(cb, " " + name);
    return lab;
  }));
}

// ── sending ───────────────────────────────────────────────────────────────────
async function send(text, atts = []) {
  if (!text.trim() && !atts.length) return false;
  try {
    const r = await post("api/submit", { text, attachments: atts });
    applyView(r.view || {});
    return true;
  } catch (e) {
    // fetch() rejects with a TypeError when momo can't be reached at all (not on
    // an HTTP error): the stream is likely dead too, whatever it says.
    if (e instanceof TypeError) lostConnection();
    else handleEvents([{ type: "error", text: `could not reach momo: ${e.message}` }]);
    return false;
  }
}

function applyView(v) {
  if (v.show_system_prompt) openPromptDrawer();
  if (v.tool_picker && $("#tools-menu").hidden) {
    toggleMenu({ stopPropagation() {} }, "#tools-menu", "Tools", refreshToolsMenu);
  }
  if (v.mcp_picker) openSettings("mcp");
  if (v.changes_menu) showChanges();
  let changed = false;
  for (const [k, val] of Object.entries(v)) {
    const key = VIEW_MAP[k];
    if (key && view[key] !== val) { view[key] = val; changed = true; }
  }
  if (changed) { saveView(); syncViewMenu(); rerenderAll(); }
}

function toggleView(key) {
  if (key === "diffStyle") view.diffStyle = view.diffStyle === "git" ? "compact" : "git";
  else view[key] = !view[key];
  saveView(); syncViewMenu(); rerenderAll();
}

function pushHistory(text) {
  if (text.startsWith("/token ")) return;
  if (history[history.length - 1] !== text) history.push(text);
}

// ── composer ──────────────────────────────────────────────────────────────────
function autosize() {
  input.style.height = "auto";
  input.style.height = input.scrollHeight + "px";
  input.classList.toggle("cmd", input.value.startsWith("/"));
  saveDraft();
}

// What you were typing survives a reload, per session (not while editing a
// sent message: that text is already in the conversation).
const draftKey = () => (state?.session ? `momo.draft.${state.session}` : null);
let draftTimer = null;
function saveDraft() {
  if (editing) return;
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    const key = draftKey();
    if (key) saveJSON(key, input.value.trim() ? input.value : undefined);
  }, 300);
}
function restoreDraft() {
  const key = draftKey();
  if (!key || input.value || editing) return;
  const draft = loadJSON(key, "");
  if (draft) { input.value = draft; autosize(); }
}

async function submitInput() {
  const text = input.value;
  const isCmd = text.trim().startsWith("/");
  if (text.trim()) pushHistory(text.trim());
  const ready = attachments.filter((a) => a.status === "ready");
  if (!isCmd && attachments.some((a) => a.status === "loading")) {
    attNote("Still converting attachments — send again in a moment.");
    return;
  }
  if (editing && !isCmd) {
    if (ready.length) {
      // api/edit replaces the typed text only; new files would be left behind.
      attNote("An edited message keeps its own attachments — send new files with a new message.");
      return;
    }
    const was = editing;
    input.value = "";
    autosize();
    stopEdit();
    let ok = false;
    try { ok = (await post("api/edit", { text })).ok; } catch (e) { showError(e); }
    // Refused (momo is busy: the reason is in the transcript) or failed: the
    // edit is still yours to send.
    if (!ok && !input.value) showEdit(text, was.attachments);
    return;
  }
  if (!text.trim() && !ready.length) return;
  // Busy with a turn, or offline: queue plain messages instead of bouncing them.
  if (!isCmd && ((busy && !waiting) || !connected)) {
    queueMessage(text, ready, !connected);
    return;
  }
  input.value = "";
  histIdx = -1; histStash = "";
  autosize();
  closeSuggest();
  // Commands never carry attachments; they stay queued for the next message.
  const atts = isCmd ? [] : ready.map((a) => ({ name: a.name, text: a.text }));
  const ok = await send(text, atts);
  if (ok && atts.length) {
    attachments = attachments.filter((a) => a.status === "loading");
    renderChips();
  } else if (!ok && !isCmd && !connected) {
    queueMessage(text, ready, true);   // the connection dropped as it was sent
  } else if (!ok && !input.value) {
    input.value = text;  // keep what the user typed if the request failed
    autosize();
  }
}

function queueMessage(text, ready, offline) {
  if (offline) toast("Queued — it is sent when the connection is back.");
  queue.push({ text, atts: ready.map((a) => ({ name: a.name, text: a.text })) });
  attachments = attachments.filter((a) => a.status !== "ready");
  if (input.value === text) input.value = "";
  histIdx = -1; histStash = "";
  autosize();
  closeSuggest();
  renderChips();
  renderQueue();
}

// ── queued messages ───────────────────────────────────────────────────────────
async function sendNextQueued() {
  if (busy || !connected || !queue.length) return;
  const item = queue.shift();
  renderQueue();
  const ok = await send(item.text, item.atts);
  if (!ok) { queue.unshift(item); renderQueue(); }
}

function renderQueue() {
  const box = $("#queue");
  box.replaceChildren(...queue.map((q, i) => {
    const chip = el("span", "att queued");
    const preview = (q.text.trim() || q.atts.map((a) => a.name).join(", ")).replace(/\s+/g, " ");
    const name = el("span", "att-name");
    name.append(icon("clock", "icon sm"), preview);
    name.title = q.text;
    const meta = el("span", "att-meta", i > 0 ? `#${i + 1} in queue`
      : connected ? "sends when momo is done" : "sends when reconnected");
    const edit = el("button", "att-x");
    edit.append(icon("pencil", "icon sm"));
    edit.type = "button";
    edit.title = "Edit (moves it back into the input box)";
    // Look the item up at click time: sendNextQueued may have shifted the queue.
    const drop = () => { const at = queue.indexOf(q); if (at >= 0) queue.splice(at, 1); return at >= 0; };
    edit.onclick = () => {
      if (!drop()) return renderQueue();
      input.value = q.text;
      for (const a of q.atts) attachments.push({ id: ++attSeq, status: "ready", name: a.name, text: a.text, chars: a.text.length });
      renderQueue(); renderChips(); autosize(); input.focus();
    };
    const x = el("button", "att-x");
    x.append(icon("x", "icon sm"));
    x.type = "button";
    x.title = "Remove from queue";
    x.onclick = () => { drop(); renderQueue(); };
    chip.append(name, meta, edit, x);
    return chip;
  }));
  box.hidden = !queue.length;
}

// ── attachments ───────────────────────────────────────────────────────────────
// Files are converted to text by the server (/api/upload: text decoding, PDF
// extraction) as soon as they are picked, then sent with the next message.
let attachments = [];   // {id, name, status: "loading"|"ready"|"error", text, chars, pages, truncated, error}
let attSeq = 0;

// A count, shortened: 950, 1.2k, 34k.
const fmtCount = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));

function addFiles(files) {
  for (const f of files) uploadOne(f);
}

async function uploadOne(file) {
  const a = { id: ++attSeq, name: file.name || "pasted.txt", status: "loading" };
  attachments.push(a);
  renderChips();
  try {
    const max = state?.max_upload;  // the server enforces it too; this saves the upload
    if (max && file.size > max) throw new Error(`too large (max ${max / 1e6} MB)`);
    const r = await fetch("api/upload", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(a.name) },
      body: file,
    });
    const j = await r.json().catch(() => ({ error: `upload failed (${r.status})` }));
    if (!r.ok) throw new Error(j.error || `upload failed (${r.status})`);
    Object.assign(a, { status: "ready", text: j.text, chars: j.chars, pages: j.pages, truncated: j.truncated, kind: j.kind });
  } catch (e) {
    Object.assign(a, { status: "error", error: e.message });
  }
  renderChips();
}

function renderChips() {
  const box = $("#attachments");
  box.replaceChildren(...attachments.map((a) => {
    const chip = el("span", `att ${a.status}`);
    const name = el("span", "att-name");
    name.append(icon("file", "icon sm"), a.name);
    name.title = a.name;
    let meta;
    if (a.status === "loading") meta = /\.pdf$/i.test(a.name) ? "converting PDF…" : "reading…";
    else if (a.status === "error") meta = a.error;
    else {
      const tokens = Math.ceil(a.chars / 4);  // rough estimate, same heuristic as the harness
      meta = `${a.pages ? `${a.pages} pages · ` : ""}${fmtCount(a.chars)} chars · ~${fmtCount(tokens)} tokens${a.truncated ? " · truncated" : ""}`;
      const limit = status.context_limit || state?.context_limit || 0;
      if (limit && tokens > limit * 0.25) {
        chip.classList.add("warn");
        chip.title = `Large attachment: about ${Math.round((tokens / limit) * 100)}% of the context window`;
      }
    }
    const x = el("button", "att-x");
    x.append(icon("x", "icon sm"));
    x.type = "button";
    x.title = `Remove ${a.name}`;
    x.setAttribute("aria-label", `Remove ${a.name}`);
    x.onclick = () => { attachments = attachments.filter((b) => b !== a); renderChips(); input.focus(); };
    chip.append(name, el("span", "att-meta", meta), x);
    return chip;
  }));
  box.hidden = !attachments.length;
}

let attNoteTimer;
function attNote(msg) {
  const box = $("#attachments");
  box.querySelector(".att-note")?.remove();
  const n = el("span", "att-note", msg);
  box.append(n);
  box.hidden = false;
  clearTimeout(attNoteTimer);
  attNoteTimer = setTimeout(() => { n.remove(); box.hidden = !attachments.length; }, 3000);
}

$("#attach").onclick = () => $("#file-input").click();
$("#file-input").onchange = (e) => { addFiles([...e.target.files]); e.target.value = ""; input.focus(); };

// Paste files (e.g. copied in Finder) straight into the input box.
input.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); addFiles(files); }
});

// Drag and drop anywhere on the page.
let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
document.addEventListener("dragenter", (e) => { if (hasFiles(e)) { dragDepth++; $("#drop-hint").hidden = false; } });
document.addEventListener("dragleave", (e) => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; $("#drop-hint").hidden = true; } });
document.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
document.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();  // never let the browser navigate to the dropped file
  dragDepth = 0;
  $("#drop-hint").hidden = true;
  addFiles([...e.dataTransfer.files]);
  input.focus();
});

let sugg = [], suggIdx = -1;
let pathTimer = null;
const AT_RX = /(?:^|\s)@([\w./~+-]*)$/;

// Suggestions: "/" completes commands, "@" completes workspace paths.
function updateSuggest() {
  const v = input.value;
  const at = v.slice(0, input.selectionStart).match(AT_RX);
  if (at) return schedulePathSearch(at[1]);
  clearTimeout(pathTimer);
  const arg = v.match(/^(\/[\w-]+) (\S*)$/);
  if (state && arg && state.commands.some((c) => c.cmd === arg[1].toLowerCase())) {
    return suggestArgs(arg[1].toLowerCase(), arg[2]);
  }
  if (!state || !v.startsWith("/") || v.includes("\n") || /\s\S*\s/.test(v)) return closeSuggest();
  const word = v.split(/\s/)[0].toLowerCase();
  sugg = state.commands.filter((c) => c.cmd.startsWith(word) || c.usage.startsWith(v));
  if (!sugg.length || (sugg.length === 1 && sugg[0].usage === v.trim())) return closeSuggest();
  renderSuggest();
}

// Values a command takes after its name: the `a|b|c` and literal words of its
// /help usages, plus live lists for models, sessions and skills.
let argSeq = 0;
const argCache = new Map();   // source → {at, values}
async function cachedValues(key, load) {
  const hit = argCache.get(key);
  if (hit && Date.now() - hit.at < 10_000) return hit.values;
  const values = await load();
  argCache.set(key, { at: Date.now(), values });
  return values;
}
async function argValues(cmd) {
  const out = new Map();     // value → description
  for (const c of state.commands.filter((x) => x.cmd === cmd)) {
    // "on|off", "[new|classic]", "auto|<n>", "[n|off]": the literal choices.  A
    // lone bracketed word ("[filename]"), <placeholders> and "n" are not values.
    for (const tok of c.usage.split(/\s+/).slice(1)) {
      const opt = /^\[.*\]$/.test(tok);
      if (opt && !tok.includes("|")) continue;
      for (const v of (opt ? tok.slice(1, -1) : tok).split("|")) {
        if (v && !/[<>[\]]/.test(v) && v !== "n" && !out.has(v)) out.set(v, c.desc);
      }
    }
  }
  try {
    if (cmd === "/model") {
      for (const m of await cachedValues("models", async () => (await (await fetch("api/models")).json()).models || [])) {
        out.set(m, m === status.model ? "the model in use" : "");
      }
    } else if (cmd === "/session") {
      const rows = await cachedValues("sessions", async () => (await (await fetch("api/sessions")).json()).sessions || []);
      for (const s of rows) out.set(s.name, s.title || s.preview || "");
    } else if (cmd === "/load-skill" || cmd === "/unload-skill") {
      const { available = [], active = [] } = state.skills || {};
      out.clear();
      for (const s of cmd === "/load-skill" ? available.filter((x) => !active.includes(x)) : active) out.set(s, "");
    } else if (cmd === "/think") {
      for (const v of status.think_choices || []) out.set(v, `${status.model} takes this`);
    } else if (cmd === "/mcp") {
      for (const m of status.mcp || []) out.set(m.name, `${m.state}${m.tools ? ` · ${m.tools} tools` : ""}`);
    }
  } catch { /* offline: the static choices still help */ }
  return [...out];
}
async function suggestArgs(cmd, partial) {
  const seq = ++argSeq;
  const values = await argValues(cmd);
  if (seq !== argSeq) return;                       // typed on meanwhile
  const p = partial.toLowerCase();
  sugg = values.filter(([v]) => v.toLowerCase().includes(p) && v !== partial)
    .sort(([a], [b]) => (b.toLowerCase().startsWith(p) - a.toLowerCase().startsWith(p)))
    .slice(0, 30)
    .map(([v, d]) => ({ usage: `${cmd} ${v}`, desc: d, fill: `${cmd} ${v}` }));
  if (!sugg.length) return closeSuggest();
  renderSuggest();
}

function schedulePathSearch(q) {
  clearTimeout(pathTimer);
  pathTimer = setTimeout(async () => {
    let results = [];
    try {
      const r = await fetch(`api/files/search?q=${encodeURIComponent(q)}`);
      results = (await r.json()).results || [];
    } catch { return; }
    const now = input.value.slice(0, input.selectionStart).match(AT_RX);
    if (!now || now[1] !== q) return;  // the user typed on — a newer search is coming
    sugg = results.map((p) => ({ usage: p, desc: "", path: p }));
    if (!sugg.length) return closeSuggest();
    suggIdx = Math.min(suggIdx, sugg.length - 1);
    renderSuggest();
  }, 150);
}

function renderSuggest() {
  const box = $("#suggest");
  suggIdx = Math.min(suggIdx, sugg.length - 1);
  box.replaceChildren(...sugg.map((c, i) => {
    const row = el("div", i === suggIdx ? "sel" : "");
    row.setAttribute("role", "option");
    row.append(el("span", "u", c.path ? `@ ${c.usage}` : c.usage), el("span", "d", c.desc));
    row.onmousedown = (e) => { e.preventDefault(); pickSuggest(i); };
    return row;
  }));
  box.hidden = false;
  box.querySelector(".sel")?.scrollIntoView({ block: "nearest" });
}
function closeSuggest() { $("#suggest").hidden = true; sugg = []; suggIdx = -1; clearTimeout(pathTimer); }
function pickSuggest(i) {
  const c = sugg[i];
  if (c.path) {
    insertPath(c.path, true);
  } else if (c.fill) {
    input.value = c.fill;
  } else {
    const takesArg = c.usage.includes(" ") && !c.usage.includes(" | ");
    input.value = c.cmd + (takesArg ? " " : "");
  }
  closeSuggest();
  autosize();
  input.focus();
}

// Put a workspace path into the input: replace a pending "@query", else insert at the caret.
function insertPath(path, replaceAt = false) {
  const pos = input.selectionStart ?? input.value.length;
  let before = input.value.slice(0, pos);
  const after = input.value.slice(pos);
  const token = `\`${path}\``;
  if (replaceAt && AT_RX.test(before)) before = before.replace(/@[\w./~+-]*$/, token);
  else before += (before && !/\s$/.test(before) ? " " : "") + token;
  const glue = after.startsWith(" ") ? "" : " ";
  input.value = before + glue + after;
  const caret = before.length + glue.length;
  input.setSelectionRange(caret, caret);
  autosize();
}

input.addEventListener("input", () => { autosize(); suggIdx = -1; updateSuggest(); });

input.addEventListener("keydown", (e) => {
  const open = !$("#suggest").hidden;
  if (open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
    e.preventDefault();
    suggIdx = (suggIdx + (e.key === "ArrowDown" ? 1 : -1) + sugg.length) % sugg.length;
    renderSuggest();
    return;
  }
  if (open && (e.key === "Tab" || (e.key === "Enter" && suggIdx >= 0))) {
    e.preventDefault();
    pickSuggest(Math.max(0, suggIdx));
    return;
  }
  if (e.key === "Escape") {
    if (open) closeSuggest();
    else if (editing) $("#edit-cancel").click();
    else if (busy && !waiting) post("api/cancel").catch(() => {});
    return;
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    submitInput();
    return;
  }
  if (e.key === "Tab" && e.shiftKey) {
    e.preventDefault();
    cycleMode();
    return;
  }
  // History at the text boundaries, like the TUI.
  if (e.key === "ArrowUp" && input.selectionStart === 0 && input.selectionEnd === 0 && history.length) {
    e.preventDefault();
    if (histIdx === -1) { histStash = input.value; histIdx = history.length - 1; }
    else if (histIdx > 0) histIdx--;
    input.value = history[histIdx];
    autosize();
    input.setSelectionRange(0, 0);
  } else if (e.key === "ArrowDown" && histIdx !== -1 && input.selectionStart === input.value.length) {
    e.preventDefault();
    if (histIdx < history.length - 1) input.value = history[++histIdx];
    else { histIdx = -1; input.value = histStash; }
    autosize();
  }
});

$("#send").onclick = submitInput;
$("#stop").onclick = () => post("api/cancel").catch(() => {});

function cycleMode() {
  if (!state) return;
  const modes = state.modes;
  const next = modes[(modes.indexOf(status.mode) + 1) % modes.length];
  post("api/mode", { mode: next }).catch(() => {});
}
$("#mode").onchange = (e) => post("api/mode", { mode: e.target.value }).catch(() => {});
// Plan type: shown in plan mode; fixed while a turn runs or a plan executes.
function updatePlanKind() {
  const sel = $("#plan-kind");
  $("#plan-kind-pick").hidden = status.mode !== "plan";
  if (sel.value !== (status.plan_kind || "normal")) sel.value = status.plan_kind || "normal";
  sel.disabled = busy || (status.plan_progress || "").startsWith("exec");
}
$("#plan-kind").onchange = (e) => send(`/plan ${e.target.value}`);
// Every chip opens its popover (chips.js; THINK and INDEX here) — none toggles
// on a bare click.
for (const [chip, menu] of [["#run-badge", "#run-menu"], ["#net-badge", "#net-menu"],
                            ["#tools-badge", "#tools-off-menu"]]) {
  $(chip).onclick = (e) => toggleMenu(e, menu, null, () => renderChipMenu(menu));
}
$("#net-on").onchange = (e) => send(`/net ${e.target.checked ? "on" : "off"}`);
$("#net-local").onchange = (e) => send(`/net ${e.target.checked ? "local" : "on"}`);
$("#think-badge").onclick = (e) => toggleMenu(e, "#think-menu");
for (const r of document.querySelectorAll("input[name=think-history]")) {
  r.onchange = () => send(`/think-history ${r.value}`);
}
$("#cavecat-badge").onclick = (e) => toggleMenu(e, "#cavecat-menu");
for (const r of document.querySelectorAll("input[name=cavecat], input[name=cavecat-view]")) {
  r.onchange = () => send(`/cavecat ${r.value}`);
}
for (const r of document.querySelectorAll("input[name=tool-ref]")) {
  r.onchange = () => send(`/tool-ref ${r.value}`);
}
for (const r of document.querySelectorAll("input[name=think-keep]")) {
  r.onchange = () => send(`/think-keep ${r.value}`);
}
$("#index-badge").onclick = (e) => toggleMenu(e, "#index-menu", "Code index", refreshIndexMenu);
$("#mcp-btn").onclick = (e) => toggleMenu(e, "#mcp-menu", "MCP servers", refreshMcpMenu);
$("#skills-btn").onclick = (e) => toggleMenu(e, "#skills-menu", "Skills", refreshSkillsMenu);
// Settings controls name their command in data-cmd: a checkbox sends on/off, a
// text field sends its value.  Status events bring the server's value back.
for (const c of document.querySelectorAll("input[data-cmd]")) {
  c.onchange = async () => {
    const v = c.type === "checkbox" ? (c.checked ? "on" : "off") : c.value.trim();
    if (c.type !== "checkbox") c.blur();   // so the status below may write the field
    const sentAt = Date.now();
    if (v) await send(`${c.dataset.cmd} ${v}`);
    // A refused value would stay in the control: show the server's again (an
    // accepted one's status event has normally arrived by now), and say which.
    setTimeout(() => {
      applyStatus(status);
      const refused = lastServerError && lastServerError.at >= sentAt;
      if (refused) toast(lastServerError.text, "error");
      if (c.type === "checkbox") return;
      c.classList.add(refused ? "rejected" : "saved");
      setTimeout(() => c.classList.remove("rejected", "saved"), 1500);
    }, 400);
  };
}
$("#index-save").onclick = () => send("/index save");

// ── index filter drawer ───────────────────────────────────────────────────────
let filterLoaded = "";
function filterMsg(text, error = false) {
  const m = $("#filter-msg");
  m.textContent = text;
  m.classList.toggle("error", error);
}
async function loadFilter() {
  filterMsg("");
  try {
    const f = await (await fetch("api/index-filter")).json();
    filterLoaded = f.text;
    $("#filter-text").value = f.text;
    $("#filter-rules").textContent = `${f.rules} rules`;
    $("#filter-path").textContent = f.path;
  } catch (err) {
    filterMsg(`Could not load the filter: ${err.message}`, true);
  }
}
async function closeFilter() {
  const dirty = $("#filter-text").value !== filterLoaded;
  const bar = $("#filter-drawer .drawer-actions");
  if (dirty && !(await confirmInline(bar, "Discard your changes to the filter?", "Discard"))) return;
  closeLayer($("#filter-drawer"));
}
function openFilter() {
  closeMenu();
  openLayer($("#filter-drawer"), undefined, false);
  loadFilter().then(() => $("#filter-text").focus());
}
$("#index-filter-open").onclick = openFilter;
$("#filter-close").onclick = closeFilter;
$("#filter-cancel").onclick = closeFilter;
$("#filter-save").onclick = async () => {
  const text = $("#filter-text").value;
  try {
    const r = await post("api/index-filter", { text });
    filterLoaded = text;
    filterMsg(r.message);
    await loadFilter();
    filterMsg(r.message);
  } catch (err) {
    filterMsg(err.message, true);
  }
};
$("#filter-reset").onclick = async () => {
  const bar = $("#filter-drawer .drawer-actions");
  if (!(await confirmInline(bar, "Replace the filter with one built from the .gitignore files?", "Replace"))) return;
  await send("/index-filter reset");
  await loadFilter();
};
$("#filter-text").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.preventDefault(); closeFilter(); }
  else if (e.key === "s" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); $("#filter-save").click(); }
});
$("#index-load").onclick = () => send("/index load");

// Ctrl/Cmd+K opens the palette and Ctrl/Cmd+F the find bar, from anywhere.  A
// second Ctrl+F while the find bar has focus is the browser's own find.
document.addEventListener("keydown", (e) => {
  if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
  const k = e.key.toLowerCase();
  if (k === "k") {
    e.preventDefault();
    closeMenu();
    openPalette();
  } else if (k === "f" && document.activeElement !== $("#find-input")) {
    e.preventDefault();
    closeMenu();
    openFind();
  }
});

// Alt+↑ / Alt+↓: the previous / next message you sent.
function jumpTurn(dir) {
  const top = transcript.scrollTop;
  const users = [...transcript.querySelectorAll(".msg.user")];
  const target = dir < 0 ? users.filter((u) => u.offsetTop < top - 8).pop()
                         : users.find((u) => u.offsetTop > top + 8);
  if (target) transcript.scrollTop = target.offsetTop - 12;
}

// Esc from a field inside a popover or panel closes that popover or panel (the
// input box keeps its own Esc: interrupt; fields that handle Esc themselves win).
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.defaultPrevented || e.target === input
      || !e.target.matches?.("input, select, textarea")) return;
  if (MENUS.some(([, m]) => !$(m).hidden)) return closeMenu();
  const panel = e.target.closest("aside");
  if (panel && DRAWERS[`#${panel.id}`]) DRAWERS[`#${panel.id}`]();
});

// Shift+letter shortcuts when focus is outside the text box (TUI: chat focus).
document.addEventListener("keydown", (e) => {
  if (e.target === input || e.target.matches?.("input, select, textarea")) return;
  if (e.key === "Escape") {
    if (MENUS.some(([, m]) => !$(m).hidden)) return closeMenu();
    if (findOpen()) return closeFind();
    // Through each drawer's own close, so the filter's unsaved-changes check runs.
    for (const [d, close] of Object.entries(DRAWERS)) {
      if (!$(d).hidden) return close();
    }
    if (busy && !waiting) post("api/cancel").catch(() => {});
    return;
  }
  if (e.altKey && !e.metaKey && !e.ctrlKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
    e.preventDefault();
    return jumpTurn(e.key === "ArrowUp" ? -1 : 1);
  }
  if (e.key === "?" && !e.metaKey && !e.ctrlKey && !e.altKey) {
    e.preventDefault();
    return openPalette({ shortcuts: true });
  }
  if (!e.shiftKey || e.metaKey || e.ctrlKey || e.altKey) return;
  const map = { T: "think", M: "md", D: "diff", Q: "companion" };
  if (map[e.key]) { e.preventDefault(); toggleView(map[e.key]); }
  else if (e.key === "P") { e.preventDefault(); send(`/run-confirm ${status.run_confirm ? "off" : "on"}`); }
  else if (e.key === "C") { e.preventDefault(); post("api/cancel").catch(() => {}); }
  else if (e.key === "E") { e.preventDefault(); setAllOpen(true); }
  else if (e.key === "W") { e.preventDefault(); setAllOpen(false); }
  else if (e.key === "Tab") { e.preventDefault(); cycleMode(); }
});

// Expand or collapse every tool call, tool group and thinking block.
function setAllOpen(open) {
  for (const d of transcript.querySelectorAll("details.tool, details.tool-group, details.think")) d.open = open;
}
$("#expand-all").onclick = () => setAllOpen(true);
$("#collapse-all").onclick = () => setAllOpen(false);

// ── view menu ─────────────────────────────────────────────────────────────────
function syncViewMenu() {
  for (const cb of document.querySelectorAll("[data-view]")) cb.checked = !!view[cb.dataset.view];
  for (const r of document.querySelectorAll("input[name=diff-style]")) r.checked = r.value === view.diffStyle;
  $("#composer").classList.toggle("no-companion", !view.companion);
  $("#companion").hidden = !view.companion;
  applyAppearance();
  syncNotifyControls();
}
// Popover menus: [button, menu].  One is open at a time; a click outside closes it.
const MENUS = [["#view-btn", "#view-menu"], ["#model-btn", "#model-menu"],
               ["#ctx-btn", "#ctx-menu"], ["#index-badge", "#index-menu"],
               ["#tools-btn", "#tools-menu"], ["#think-badge", "#think-menu"],
               ["#cavecat-badge", "#cavecat-menu"],
               ["#run-badge", "#run-menu"], ["#net-badge", "#net-menu"],
               ["#tools-badge", "#tools-off-menu"], ["#mcp-btn", "#mcp-menu"], ["#skills-btn", "#skills-menu"]];
function closeMenu() {
  hideToolTip();
  for (const [b, m] of MENUS) {
    closeLayer($(m));
    $(b).setAttribute("aria-expanded", "false");
  }
}
// Open `menu` (or close it if it is open).  A menu with a loader shows "Loading…"
// under `title` first; the loader must check menu.hidden before painting, since
// the user may close the menu while it waits.
function toggleMenu(e, menu, title = null, load = null) {
  e.stopPropagation();
  const m = $(menu);
  if (!m.hidden) return closeMenu();
  closeMenu();
  if (title) m.replaceChildren(el("div", "menu-title", title), el("div", "muted", "Loading…"));
  const button = $(MENUS.find(([, x]) => x === menu)[0]);
  m.hidden = false;                 // loaders check menu.hidden before painting
  load?.();                         // a synchronous painter fills it before focus moves in
  openLayer(m, button);
  button.setAttribute("aria-expanded", "true");
}
$("#view-btn").onclick = (e) => toggleMenu(e, "#view-menu");
document.addEventListener("click", (e) => {
  if (!MENUS.some(([, m]) => $(m).contains(e.target))) closeMenu();
});
for (const cb of document.querySelectorAll("[data-view]")) {
  cb.onchange = () => { view[cb.dataset.view] = cb.checked; saveView(); syncViewMenu(); rerenderAll(); };
}
for (const r of document.querySelectorAll("input[name=diff-style]")) {
  r.onchange = () => { view.diffStyle = r.value; saveView(); rerenderAll(); };
}
$("#idle-recap-secs").onchange = (e) => {
  const n = Math.round(Number(e.target.value));
  if (Number.isFinite(n) && n >= 10) send(`/companion-idle-recap ${n}`);
  else e.target.value = Math.max(10, n || 90);
};

// ── plan drawer ───────────────────────────────────────────────────────────────
$("#plan-btn").onclick = (e) => { refreshState(); openLayer($("#plan-drawer"), e.currentTarget); };
$("#plan-progress").onclick = $("#plan-btn").onclick;
$("#plan-close").onclick = () => DRAWERS["#plan-drawer"]();
for (const b of document.querySelectorAll("#plan-drawer [data-cmd]")) {
  b.onclick = async () => {
    // Like deleting a session, discarding a plan asks first: it deletes .momo/plan.md.
    const bar = $("#plan-drawer .drawer-actions");
    if (b.id === "plan-cancel" && !(await confirmInline(bar, "Discard this plan and .momo/plan.md?", "Discard"))) return;
    send(b.dataset.cmd);
    DRAWERS["#plan-drawer"]();
  };
}
// While the drawer is open every status event may carry a plan change (a revised
// draft keeps its phase), so it re-reads the plan, at most twice a second.
let planRefreshTimer = null;
function schedulePlanRefresh() {
  if (planRefreshTimer) return;
  planRefreshTimer = setTimeout(() => { planRefreshTimer = null; refreshState(); }, 500);
}

// ── model picker ──────────────────────────────────────────────────────────────
$("#model-btn").onclick = (e) => toggleMenu(e, "#model-menu", "Model", refreshModelMenu);
async function refreshModelMenu() {
  const menu = $("#model-menu");
  let info;
  try {
    info = await (await fetch("api/models")).json();
  } catch (err) {
    if (!menu.hidden) menu.lastChild.textContent = `Could not list models: ${err.message}`;
    return;
  }
  if (menu.hidden) return;
  const rows = [el("div", "menu-title", `Model · ${info.provider}`)];
  if (!info.can_switch) {
    rows.push(el("div", "muted small", `${info.provider} serves one model per server — restart it to change models.`));
  }
  if (!info.models.length) rows.push(el("div", "muted", `No models found — is ${info.provider} reachable?`));
  for (const m of info.models) {
    const b = el("button", `menu-item${m === info.current ? " current" : ""}`, `${m === info.current ? "● " : ""}${m}`);
    b.type = "button";
    b.disabled = !info.can_switch || m === info.current;
    b.onclick = () => { closeMenu(); send(`/model ${m}`); };
    rows.push(b);
  }
  // A long list (Ollama can serve dozens) gets a filter field.
  let filter = null;
  if (info.models.length > 8) {
    filter = el("input", "menu-filter");
    filter.type = "search";
    filter.placeholder = "Filter models…";
    filter.setAttribute("aria-label", "Filter models");
    filter.oninput = () => {
      const q = filter.value.trim().toLowerCase();
      for (const b of menu.querySelectorAll(".menu-item")) b.hidden = !!q && !b.textContent.toLowerCase().includes(q);
    };
    rows.splice(1, 0, filter);
  }
  menu.replaceChildren(...rows);
  if (filter && (menu.contains(document.activeElement) || document.activeElement === document.body)) filter.focus();
}

// ── context breakdown ─────────────────────────────────────────────────────────
// Clicking the CTX meter shows where the context is spent. While it is open,
// status events (which arrive live as a reply streams in) re-fetch it.
const fmtTok = (n) => n.toLocaleString("en-US");
let ctxRefreshTimer = null;
function scheduleCtxRefresh() {
  if (ctxRefreshTimer) return;
  ctxRefreshTimer = setTimeout(() => { ctxRefreshTimer = null; refreshCtxMenu(); }, 500);
}
async function refreshCtxMenu() {
  const menu = $("#ctx-menu");
  if (menu.hidden) return;
  let b;
  try {
    b = await (await fetch("api/context")).json();
  } catch (err) {
    menu.replaceChildren(el("div", "menu-title", "Context"), el("div", "muted", `Could not load: ${err.message}`));
    return;
  }
  if (menu.hidden) return;
  renderCtxMenu(menu, b);
}
// One row of the CTX / INDEX popovers: a lead mark (swatch or mini bar), a name,
// a value and a share.
function statRow(extraCls, lead, name, value, share, title = "") {
  const row = el("div", `ctx-row${extraCls ? " " + extraCls : ""}`);
  if (title) row.title = title;
  row.append(lead, el("span", "ctx-name", name), el("span", "ctx-tok mono", value),
             el("span", "ctx-pc mono", share));
  return row;
}
function miniBar(frac) {
  const mini = el("span", "idx-mini");
  const f = el("span", "idx-mini-fill");
  f.style.width = `${frac * 100}%`;
  mini.append(f);
  return mini;
}

function renderCtxMenu(menu, b) {
  const pctOf = (n) => (b.limit ? (n / b.limit) * 100 : 0);
  const shown = b.categories.filter((c) => c.key !== "generating" || b.streaming);
  const sent = shown.filter((c) => c.sent);
  const bar = el("div", "ctx-stack");
  for (const c of sent) {
    if (!c.tokens) continue;
    const seg = el("span", `ctx-seg ctx-${c.key}`);
    seg.style.width = `${Math.min(100, pctOf(c.tokens))}%`;
    seg.title = `${c.label}: ~${fmtTok(c.tokens)} tokens`;
    bar.append(seg);
  }
  const rows = el("div", "ctx-rows");
  for (const c of shown) {
    rows.append(statRow(c.sent ? "" : "unsent", el("span", `ctx-swatch ctx-${c.key}`), c.label,
      fmtTok(c.tokens), c.sent ? `${pctOf(c.tokens).toFixed(0)}%` : "—",
      c.sent ? "" : "Kept in the transcript but not sent to the model"));
  }
  const meta = [`limit ${fmtTok(b.limit)}`];
  if (b.model_max) meta.push(`model max ${fmtTok(b.model_max)}`);
  if (b.measured != null) meta.push(`last measured ${fmtTok(b.measured)}`);
  menu.replaceChildren(
    el("div", "menu-title", `Context · ~${fmtTok(b.used)} tokens (${b.pct}%)${b.streaming ? " · streaming" : ""}`),
    bar, rows,
    el("div", "muted small", meta.join(" · ")),
    el("div", "muted small", `Categories are estimates (~4 chars/token), total ~${fmtTok(b.estimated)}. Thinking stays in the transcript; only what /think-history sends back counts.`),
    compactActions(b.pct),
    promptLink());
}

// Compact now — the summary kind and the instant one; suggested once the context
// is three-quarters full (compaction starts by itself at the limit).
function compactActions(pct) {
  const row = el("div", "menu-row");
  const high = pct >= 75;
  for (const [label, cmd, title] of [
    ["Compact", "/compact", "Drop old history, keeping a model-written summary of it"],
    ["Fast compact", "/fast-compact", "Drop old history without a summary (instant)"],
  ]) {
    const b = el("button", `btn small${high && cmd === "/compact" ? " primary" : ""}`, label);
    b.type = "button";
    b.title = title;
    b.disabled = busy;
    b.onclick = () => { closeMenu(); send(cmd); };
    row.append(b);
  }
  return row;
}
$("#ctx-btn").onclick = (e) => toggleMenu(e, "#ctx-menu", "Context", refreshCtxMenu);

// ── per-mode tools ────────────────────────────────────────────────────────────
// The wrench button lists the current mode's tools with a checkbox each; a change
// sends /tools <name> on|off, and the status event it causes re-fetches the list
// (net and index tools follow their toggles, and a mode switch swaps the list).
let toolsRefreshTimer = null;
function scheduleToolsRefresh() {
  if (toolsRefreshTimer) return;
  toolsRefreshTimer = setTimeout(() => { toolsRefreshTimer = null; refreshToolsMenu(); }, 200);
}
async function refreshToolsMenu() {
  const menu = $("#tools-menu");
  if (menu.hidden) return;
  let b;
  try {
    b = await (await fetch("api/tools")).json();
  } catch (err) {
    menu.replaceChildren(el("div", "menu-title", "Tools"), el("div", "muted", `Could not load: ${err.message}`));
    return;
  }
  if (menu.hidden) return;
  renderToolsMenu(menu, b);
}
function renderToolsMenu(menu, b) {
  const check = (checked, disabled, onchange) => {
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = checked;
    cb.disabled = disabled;
    cb.onchange = () => onchange(cb.checked);
    return cb;
  };
  const master = el("label");
  master.append(check(b.tools_enabled, false, (on) => send(`/tools ${on ? "on" : "off"}`)),
                " Tool calls", el("kbd", null, "/tools"));
  const discover = el("label");
  const pick = document.createElement("select");
  for (const [value, text] of [["off", "off"], ["extensions", "custom, workspace, MCP"],
                               ["all", "also rare built-ins"]]) {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = text;
    pick.append(o);
  }
  pick.value = b.tools_discover || "off";
  pick.disabled = !b.tools_enabled;
  pick.onchange = () => send(`/tools discover ${pick.value}`);
  discover.append("Load on demand ", pick, el("kbd", null, "/tools discover"));
  discover.title = "Tools listed in the prompt instead of declared; they load when the model "
    + "asks for them with find_tools or a result names them. Built-ins stay declared "
    + "unless you pick 'also rare built-ins'.";
  const rows = [el("div", "menu-title", `Tools — ${b.mode} mode`), master, discover];
  let group = null;
  for (const t of b.tools) {
    if (t.group !== group) { group = t.group; rows.push(el("div", "menu-sub", group)); }
    const lab = el("label", "mono");
    lab.dataset.name = t.name;
    lab.dataset.desc = t.desc;
    if (t.locked) lab.dataset.lock = `Can't be turned off: ${t.note}`;
    lab.setAttribute("aria-description", t.desc);
    lab.append(check(t.enabled, t.locked || !b.tools_enabled,
                     (on) => send(`/tools ${t.name} ${on ? "on" : "off"}`)), " " + t.name);
    if (t.state && t.state !== "core") lab.append(el("span", "muted small tool-state", t.state));
    if (t.note) lab.append(el("span", "muted small tool-note", t.note));
    rows.push(lab);
  }
  // Tools waiting for approval go on top, where the badge points; otherwise last.
  const ws = workspaceRows(b);
  if ((b.workspace || []).some((w) => w.status === "pending")) rows.splice(3, 0, ...ws);
  else rows.push(...ws);
  menu.replaceChildren(...rows);
  // A refresh (after a toggle) replaces the rows under the pointer: keep the
  // tooltip on the same tool.
  if (toolTipName) showToolTip(menu.querySelector(`label[data-name="${toolTipName}"]`), 0);
}
// The workspace's own tools (.momo/tools/) that have no checkbox above: waiting
// for approval, denied, disabled, broken, or not offered in this mode.  Approving
// runs the file's code on this machine, so each row offers its code first.
function workspaceRows(b) {
  const offered = new Set(b.tools.map((t) => t.name));
  const rest = (b.workspace || []).filter((w) => !offered.has(w.name));
  if (!rest.length) return [];
  const linkBtn = (label, onclick) => {
    const btn = el("button", "link-btn", label);
    btn.type = "button";
    btn.onclick = onclick;
    return btn;
  };
  const rows = [el("div", "menu-sub", "workspace — .momo/tools/")];
  for (const w of rest) {
    const d = el("span", "dot");
    if (w.status === "loaded") d.classList.add("on");
    else if (w.status === "pending") d.classList.add("on", "offline");
    else if (w.status !== "error") d.classList.add("muted-dot");
    const why = w.status === "loaded" ? `not offered in ${b.mode} mode (roles: ${w.roles.join(", ") || "none"})`
      : w.status === "pending" ? "waiting for approval" : w.status === "denied" ? "denied"
      : w.status === "disabled" ? "disabled in .momo/tools.json" : w.hint;
    const head = el("div", "ws-tool-head");
    head.append(d, el("span", "mono", w.name), el("span", "muted small tool-note", why));
    const acts = el("div", "ws-tool-actions");
    acts.append(linkBtn("View code", () => { closeMenu(); showFile(w.path); }));
    if (w.status === "pending" || w.status === "denied") {
      acts.append(linkBtn("Approve", () => send(`/tools approve ${w.name}`)));
    }
    if (w.status === "pending") acts.append(linkBtn("Deny", () => send(`/tools deny ${w.name}`)));
    const row = el("div", "ws-tool");
    if (w.desc) row.title = w.desc;
    row.append(head, acts);
    rows.push(row);
  }
  return rows;
}
$("#tools-btn").onclick = (e) => toggleMenu(e, "#tools-menu", "Tools", refreshToolsMenu);

// Tool descriptions on hover or keyboard focus.  A native title tooltip waits
// about a second and can't be hurried, so this one shows after a short beat,
// beside the menu (below the row when there's no room on the left).
let toolTipName = null;
let toolTipTimer = null;
function showToolTip(lab, delay = 60) {
  clearTimeout(toolTipTimer);
  const tip = $("#tool-tip");
  if (!lab) { toolTipName = null; tip.hidden = true; return; }
  toolTipTimer = setTimeout(() => {
    const menu = $("#tools-menu");
    if (menu.hidden || !lab.isConnected) return;
    toolTipName = lab.dataset.name;
    const lock = lab.dataset.lock ? [el("div", "tool-tip-lock", lab.dataset.lock)] : [];
    tip.replaceChildren(el("div", "tool-tip-name mono", lab.dataset.name), ...lock,
                        el("div", null, lab.dataset.desc));
    tip.hidden = false;
    const r = lab.getBoundingClientRect(), m = menu.getBoundingClientRect();
    const w = tip.offsetWidth, h = tip.offsetHeight, gap = 8;
    let left = m.left - gap - w, top = r.top - 6;
    if (left < gap) {                       // narrow window: under the row instead
      left = Math.max(gap, Math.min(r.left, innerWidth - w - gap));
      top = r.bottom + 4;
      if (top + h > innerHeight - gap) top = r.top - h - 4;
    }
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.max(gap, Math.min(top, innerHeight - h - gap))}px`;
  }, delay);
}
function hideToolTip() { showToolTip(null); }
{
  const menu = $("#tools-menu");
  menu.addEventListener("pointerover", (e) => {
    const lab = e.target.closest("label[data-desc]");
    if (lab) { if (lab.dataset.name !== toolTipName || $("#tool-tip").hidden) showToolTip(lab); }
    else hideToolTip();
  });
  menu.addEventListener("pointerleave", hideToolTip);
  menu.addEventListener("focusin", (e) => showToolTip(e.target.closest("label[data-desc]"), 0));
  menu.addEventListener("focusout", hideToolTip);
  menu.addEventListener("scroll", hideToolTip);
}

// ── system prompt ─────────────────────────────────────────────────────────────
// View → System prompt… (and the CTX popover, and /system-prompt) show the
// composed system prompt part by part: role, tool reference, guides, skills,
// index rules — then the tool schemas that go alongside it.  While the drawer is
// open, status events (mode, tool, net, index, skill changes) re-fetch it.
let promptText = "";
let promptRefreshTimer = null;
function schedulePromptRefresh() {
  if (promptRefreshTimer) return;
  promptRefreshTimer = setTimeout(() => { promptRefreshTimer = null; loadPrompt(); }, 300);
}
function promptLink() {
  const b = el("button", "link-btn", "View system prompt");
  b.type = "button";
  b.onclick = openPromptDrawer;
  return b;
}
function openPromptDrawer() {
  closeMenu();
  openRight("#prompt-drawer", false);
  loadPrompt();
}
async function loadPrompt() {
  const body = $("#prompt-body");
  let v;
  try {
    v = await (await fetch("api/system-prompt")).json();
  } catch (err) {
    body.replaceChildren(el("div", "muted", `Could not load: ${err.message}`));
    return;
  }
  if ($("#prompt-drawer").hidden) return;
  // Keep what the user opened or closed across refreshes.
  const open = new Map([...body.querySelectorAll("details")].map((d) => [d.dataset.key, d.open]));
  promptText = v.text;
  $("#prompt-mode").textContent = `${v.mode} mode`;
  $("#prompt-tokens").textContent = `~${fmtTok(v.total_tokens)} tokens`;
  const part = (key, label, tokens, text, defOpen, cls = "") => {
    const d = el("details", cls);
    d.dataset.key = key;
    d.open = open.has(key) ? open.get(key) : defOpen;
    const s = el("summary");
    s.append(el("span", null, label), el("span", "muted mono", `~${fmtTok(tokens)}`));
    d.append(s, el("pre", null, text));
    return d;
  };
  body.replaceChildren(
    ...v.sections.map((s) => part(s.key, s.label, s.tokens, s.text, s.key === "role" || s.key === "tools")),
    part("schemas", v.schemas.label, v.schemas.tokens, v.schemas.text, false, "aside"));
}
$("#prompt-open").onclick = openPromptDrawer;
$("#prompt-close").onclick = () => DRAWERS["#prompt-drawer"]();
$("#prompt-copy").onclick = () => copyWithFeedback($("#prompt-copy"), promptText);

// ── code index composition ────────────────────────────────────────────────────
// Clicking the INDEX badge (while the index is on) shows what the index's memory
// is spent on: a meter against the budget, then a stacked bar and one row per
// category, then per language — the CTX popover's layout. Status events
// (progress while building) re-fetch it while it is open.
let indexRefreshTimer = null;
function scheduleIndexRefresh() {
  if (indexRefreshTimer) return;
  indexRefreshTimer = setTimeout(() => { indexRefreshTimer = null; refreshIndexMenu(); }, 500);
}
async function refreshIndexMenu() {
  const menu = $("#index-menu");
  if (menu.hidden) return;
  let b;
  try {
    b = await (await fetch("api/index")).json();
  } catch (err) {
    menu.replaceChildren(el("div", "menu-title", "Code index"), el("div", "muted", `Could not load: ${err.message}`));
    return;
  }
  if (menu.hidden) return;
  if (!b.enabled) return renderIndexOff(menu);
  renderIndexMenu(menu, b);
}
// The INDEX chip while the index is off: what it is, and the switch.
function renderIndexOff(menu) {
  const on = el("button", "btn primary small", "Turn the code index on");
  on.type = "button";
  on.onclick = () => { closeMenu(); send("/index on"); };
  const more = el("button", "link-btn", "Index settings…");
  more.type = "button";
  more.onclick = () => { closeMenu(); openSettings("index"); };
  const foot = el("div", "menu-foot");
  foot.append(more);
  menu.replaceChildren(
    el("div", "menu-title", "Code index: off"),
    el("div", "menu-note", "With the index on, the model finds definitions, callers and text in one call "
      + "(index_* tools) instead of grepping file by file."),
    on, foot);
  on.focus();
}
function renderIndexMenu(menu, b) {
  const used = b.used || 1;
  const pc = (n) => `${((n / used) * 100).toFixed(0)}%`;
  const budget = el("div", "ctx-stack");
  const fill = el("span", `ctx-seg ${b.degraded ? "idx-over" : "idx-used"}`);
  fill.style.width = `${Math.min(100, b.pct)}%`;
  fill.title = `${formatSize(b.used)} of ${formatSize(b.limit)}`;
  budget.append(fill);

  const stack = el("div", "ctx-stack");
  const rows = el("div", "ctx-rows");
  for (const c of b.categories) {
    if (c.enabled && c.bytes) {
      const seg = el("span", `ctx-seg idx-${c.key}`);
      seg.style.width = `${(c.bytes / used) * 100}%`;
      seg.title = `${c.label}: ${formatSize(c.bytes)} — ${c.detail}`;
      stack.append(seg);
    }
    rows.append(statRow(c.enabled ? "" : "unsent", el("span", `ctx-swatch idx-${c.key}`), c.label,
      c.enabled ? formatSize(c.bytes) : "dropped", c.enabled ? pc(c.bytes) : "—",
      c.enabled ? c.detail : "Dropped to stay inside the memory budget — raise it to rebuild"));
  }

  const langs = el("div", "ctx-rows");
  for (const l of b.languages.slice(0, 8)) {
    langs.append(statRow("idx-lang", miniBar(l.bytes / used), `${l.lang} · ${l.files}`,
      formatSize(l.bytes), pc(l.bytes), `${l.files} file${l.files === 1 ? "" : "s"}`));
  }
  if (b.shared) {
    langs.append(statRow("idx-lang", miniBar(b.shared / used), "shared names", formatSize(b.shared),
      pc(b.shared), "Distinct identifier names, shared by every file that uses them"));
  }
  if (b.languages.length > 8) {
    const rest = b.languages.slice(8);
    langs.append(el("div", "muted small", `… ${rest.length} more languages, `
      + `${rest.reduce((n, l) => n + l.files, 0)} files, ${formatSize(rest.reduce((n, l) => n + l.bytes, 0))}`));
  }

  const phase = b.progress ? `${b.state} ${b.progress}` : b.state;
  const actions = el("div", "menu-row");
  const act = (label, cmd) => {
    const btn = el("button", "link-btn", label);
    btn.type = "button";
    btn.onclick = () => send(cmd);
    return btn;
  };
  const filterBtn = el("button", "link-btn", "Filter…");
  filterBtn.type = "button";
  filterBtn.title = "Choose which files are indexed (gitignore syntax)";
  filterBtn.onclick = openFilter;
  actions.append(filterBtn, act("Rebuild", "/index rebuild"), act("Save now", "/index save"), act("Turn off", "/index off"));

  const notes = [];
  if (b.partial) notes.push("Partial: the budget was reached, so further files are not indexed.");
  if (Object.keys(b.skipped).length) {
    // "limit" is a flag, not a count: the listing stops at the file limit.
    notes.push("Skipped: " + Object.entries(b.skipped).map(([k, v]) => k === "limit"
      ? `files past the ${b.max_files.toLocaleString()}-file limit`
      : `${v} ${k.replace("_", " ")}`).join(", "));
  }
  if (b.error) notes.push(`Error: ${b.error}`);
  menu.replaceChildren(
    el("div", "menu-title", `Code index · ${b.files.toLocaleString("en-US")} files · ${phase}`),
    el("div", "muted small", `Memory ${formatSize(b.used)} of ${formatSize(b.limit)} (${b.pct}%)`),
    budget,
    el("div", "menu-sub", "Made of"), stack, rows,
    el("div", "menu-sub", "By language"), langs,
    ...notes.map((n) => el("div", "muted small", n)),
    el("div", "muted small", `Sizes are estimates of the index's own data. `
      + (b.filter ? `Filter: ${b.filter.rules} rules. ` : "")
      + `Save/load to disk: ${b.persist ? "on" : "off"}. `
      + `grep/find answered from the index: ${b.route === false ? "off" : "on"}.`),
    actions);
}

// ── drawers ───────────────────────────────────────────────────────────────────
// Each drawer's close action, in the order Esc closes them.  The filter's asks
// before discarding unsaved edits.  Closing gives focus back to what opened it.
const hideDrawer = (id) => () => closeLayer($(id));
const DRAWERS = {
  "#filter-drawer": closeFilter,
  "#settings-drawer": hideDrawer("#settings-drawer"),
  "#prompt-drawer": hideDrawer("#prompt-drawer"),
  "#plan-drawer": hideDrawer("#plan-drawer"),
  "#sessions-drawer": hideDrawer("#sessions-drawer"),
  "#files-drawer": hideDrawer("#files-drawer"),
  "#changes-drawer": hideDrawer("#changes-drawer"),
};

// The left drawers (sessions, workspace files) share one slot: opening one
// closes the other, and the button of the open one closes it.  True when `id`
// ends up open.
const LEFT = ["#sessions-drawer", "#files-drawer", "#changes-drawer"];
function openDrawer(id) {
  const opening = $(id).hidden;
  for (const d of LEFT) if (d !== id) closeLayer($(d));
  if (opening) openLayer($(id), undefined, false);
  else closeLayer($(id));
  return opening;
}
// The right drawers (settings, system prompt, plan) share theirs the same way;
// an index filter with unsaved edits stays until it is closed on purpose.
const RIGHT = ["#settings-drawer", "#prompt-drawer", "#plan-drawer"];
function openRight(id, focusFirst = true) {
  for (const d of RIGHT) if (d !== id) closeLayer($(d));
  if ($(id).hidden) openLayer($(id), undefined, focusFirst);
}
for (const b of document.querySelectorAll(".drawer-close")) b.onclick = () => DRAWERS[`#${b.closest("aside").id}`]();

$("#sessions-btn").onclick = () => { if (openDrawer("#sessions-drawer")) openSessions(); };
// The workspace path in the status bar: click browses it, right-click copies it.
$("#workdir").onclick = () => { if (openDrawer("#files-drawer")) openFiles(); };
$("#workdir").oncontextmenu = async (e) => {
  e.preventDefault();
  try { await copyText(status.workdir); toast(`Copied ${status.workdir}`); } catch { toast("Copy failed", "error"); }
};
$("#files-btn").onclick = () => { if (openDrawer("#files-drawer")) openFiles(); };
$("#changes-btn").onclick = () => { if (openDrawer("#changes-drawer")) openChanges(); };
// The Changes drawer open (not toggled), optionally at one file's diff.
function showChanges(rel = null) {
  if ($("#changes-drawer").hidden) openDrawer("#changes-drawer");
  openChanges(rel);
}

// ── settings drawer ───────────────────────────────────────────────────────────
// Every setting, in collapsible sections whose open/closed state is remembered.
// openSettings(section) opens the drawer at one section (chip popovers, palette).
function openSettings(section = null) {
  closeMenu();
  openRight("#settings-drawer", !section);
  if (!section) return;
  const det = $("#settings-drawer").querySelector(`details[data-section="${section}"]`);
  if (!det) return;
  det.open = true;
  det.scrollIntoView({ block: "start" });
  det.classList.add("flash");
  setTimeout(() => det.classList.remove("flash"), 1200);
  requestAnimationFrame(() => det.querySelector("summary").focus());
}
{
  const sections = [...$("#settings-body").querySelectorAll("details")];
  const open = loadJSON("momo.settingsOpen", ["context", "run"]);
  for (const det of sections) {
    det.open = open.includes(det.dataset.section);
    det.addEventListener("toggle", () =>
      saveJSON("momo.settingsOpen", sections.filter((d) => d.open).map((d) => d.dataset.section)));
  }
}
$("#settings-open").onclick = () => openSettings();
$("#settings-close").onclick = () => DRAWERS["#settings-drawer"]();
$("#palette-open").onclick = () => { closeMenu(); openPalette(); };

// ── welcome card ──────────────────────────────────────────────────────────────
// Until the first message: what this mode is for, a few things to try, and the
// keys worth knowing.  Not part of events[]; re-made on mode changes.
const WELCOME = {
  design: ["Design mode", "Talk a feature or a project through: momo asks questions, then writes the spec.",
           ["Help me design a small CLI that …", "Review how this project is structured", "Write a spec for …"]],
  chat:   ["Chat mode", "Ask about the code or anything else: momo reads files but changes nothing.",
           ["Explain what this project does", "Where is … handled?", "How does … work?"]],
  plan:   ["Plan mode", "Describe a feature or a bug: momo investigates and proposes a plan you approve before anything changes.",
           ["Plan how to add …", "Find out why … fails and plan a fix", "/plan tdd"]],
  coding: ["Coding mode", "momo reads, edits and runs things in the workspace to do what you ask.",
           ["Run the tests and fix what fails", "Add … to …", "Refactor … so that …"]],
  momo:   ["Momo mode", "Keep the cat company — it helps when you ask.",
           ["Hi momo! What are you up to?", "Tell me about this project", "Cheer me on, I'm debugging"]],
};
function renderWelcome() {
  transcript.querySelector(".welcome")?.remove();
  if (events.some((e) => e.type === "user")) return;
  const [title, what, examples] = WELCOME[status.mode] || WELCOME.coding;
  const card = el("div", "welcome");
  const ex = el("div", "examples");
  for (const text of examples) {
    const b = el("button", "btn", text);
    b.type = "button";
    b.onclick = () => {
      input.value = text;
      autosize();
      input.focus();
      const gap = text.indexOf("…");      // select the blank so typing fills it
      if (gap >= 0) input.setSelectionRange(gap, gap + 1);
    };
    ex.append(b);
  }
  const tips = el("p", "tips");
  for (const [k, what2] of [["/", "commands"], ["@", "files"], ["Shift+Tab", "next mode"],
                             ["Ctrl+K", "palette"], ["?", "shortcuts"]]) {
    if (tips.childNodes.length) tips.append(" · ");
    tips.append(el("kbd", "", k), ` ${what2}`);
  }
  card.append(el("h2", "", title), el("p", "", what), ex, tips);
  transcript.prepend(card);
}

// ── clickable paths ───────────────────────────────────────────────────────────
// [data-path] marks a workspace path in a reply (markdown.js), a tool row or a
// diff header; a click opens it in the workspace preview, at its line if given.
for (const host of [transcript, $("#plan-body")]) {
  host.addEventListener("click", (e) => {
    const p = e.target.closest("[data-path]");
    if (!p) return;
    e.preventDefault();      // inside a <summary>: open the file, not the row
    e.stopPropagation();
    showFile(p.dataset.path, Number(p.dataset.line) || 0);
  });
}

// The composer's and the status bar's heights, for what floats next to them
// (new-messages pill, toasts, the find bar).
new ResizeObserver(() => {
  const root = document.documentElement.style;
  root.setProperty("--composer-h", `${$("#composer").offsetHeight}px`);
  root.setProperty("--status-h", `${$("#status").offsetHeight + 6}px`);
}).observe($("#composer"));
new ResizeObserver(() => {
  document.documentElement.style.setProperty("--status-h", `${$("#status").offsetHeight + 6}px`);
}).observe($("#status"));

// ── quote a selection ─────────────────────────────────────────────────────────
// Select text in the conversation and a "Quote in reply" button appears by it;
// it puts the text into the input box as a > quote.
{
  const btn = $("#quote-btn");
  let timer = null;
  const selected = () => {
    const sel = getSelection();
    if (!sel.rangeCount || sel.isCollapsed) return null;
    const range = sel.getRangeAt(0);
    if (!transcript.contains(range.commonAncestorContainer)) return null;
    const text = sel.toString().trim();
    return text ? { text, rect: range.getBoundingClientRect() } : null;
  };
  document.addEventListener("selectionchange", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const s = selected();
      btn.hidden = !s;
      if (!s) return;
      const w = btn.offsetWidth || 120;
      btn.style.left = `${Math.max(8, Math.min(s.rect.right - w, innerWidth - w - 8))}px`;
      btn.style.top = `${Math.max(8, s.rect.top - 36)}px`;
    }, 150);
  });
  transcript.addEventListener("scroll", () => { btn.hidden = true; });
  btn.onmousedown = (e) => e.preventDefault();       // keep the selection
  btn.onclick = () => {
    const s = selected();
    btn.hidden = true;
    if (!s) return;
    const quote = s.text.split("\n").map((l) => `> ${l}`).join("\n") + "\n\n";
    const pos = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, pos);
    const lead = before && !before.endsWith("\n") ? "\n\n" : "";
    input.value = before + lead + quote + input.value.slice(pos);
    getSelection().removeAllRanges();
    autosize();
    input.focus();
    const caret = (before + lead + quote).length;
    input.setSelectionRange(caret, caret);
  };
}

// ── command palette ───────────────────────────────────────────────────────────
const fakeClick = { stopPropagation() {} };
function paletteActions() {
  const show = (key, what, keys) => ({
    label: `${view[key] ? "Hide" : "Show"} ${what}`, desc: "View", keys, run: () => toggleView(key) });
  const sections = [["appearance", "Appearance"], ["companion", "Companion"], ["context", "Context"], ["run", "Running commands"],
                    ["network", "Network"], ["index", "Code index"], ["mcp", "MCP servers"], ["skills", "Skills"],
                    ["notifications", "Notifications"], ["other", "Conversation and downloads"]];
  return [
    { label: "Settings", desc: "Every setting, in sections", run: () => openSettings() },
    ...sections.map(([s, name]) => ({ label: `Settings: ${name}`, desc: "Open the settings here", run: () => openSettings(s) })),
    { label: "Sessions", desc: "Open, rename or delete saved sessions", run: () => $("#sessions-btn").click() },
    { label: "Workspace files", desc: "Browse and preview the workspace", run: () => $("#files-btn").click() },
    { label: "Changes", desc: "What momo changed since the last keep: diffs, undo, keep", run: () => showChanges() },
    ...(state?.plan ? [{ label: "Plan", desc: "The current plan", run: () => $("#plan-btn").click() }] : []),
    { label: "System prompt", desc: "What the model gets as its system message", run: openPromptDrawer },
    { label: "Context breakdown", desc: "Where the context is spent; compact it", run: () => toggleMenu(fakeClick, "#ctx-menu", "Context", refreshCtxMenu) },
    { label: "Tools for this mode", desc: "Turn single tools on or off", run: () => toggleMenu(fakeClick, "#tools-menu", "Tools", refreshToolsMenu) },
    { label: "Skills", desc: "Load or unload skills", run: () => toggleMenu(fakeClick, "#skills-menu", "Skills", refreshSkillsMenu) },
    { label: "Expand all", desc: "Tool calls, groups and thinking", keys: "Shift+E", run: () => setAllOpen(true) },
    { label: "Collapse all", desc: "Tool calls, groups and thinking", keys: "Shift+W", run: () => setAllOpen(false) },
    { label: view.group ? "Ungroup tool calls" : "Group tool calls", desc: "View", run: () => toggleView("group") },
    show("tools", "tool output"), show("think", "thinking", "Shift+T"), show("diff", "edit diffs", "Shift+D"),
    show("companion", "companion", "Shift+Q"),
    { label: view.md ? "Plain text replies" : "Markdown replies", desc: "View", keys: "Shift+M", run: () => toggleView("md") },
    { label: "Find in conversation", desc: "Also inside collapsed tool output", keys: "Ctrl+F", run: openFind },
    { label: "Previous message you sent", desc: "Scroll to it", keys: "Alt+↑", run: () => jumpTurn(-1) },
    { label: "Next message you sent", desc: "Scroll to it", keys: "Alt+↓", run: () => jumpTurn(1) },
    ...THEMES.map((t) => ({ label: `Theme: ${t[0].toUpperCase()}${t.slice(1)}`,
                            desc: t === themeChoice ? "current" : "Appearance", run: () => setTheme(t) })),
    ...[["s", "Small"], ["m", "Medium"], ["l", "Large"]].map(([s, name]) => ({
      label: `Text size: ${name}`, desc: s === (view.size || "m") ? "current" : "Appearance", run: () => setTextSize(s) })),
    { label: view.wide ? "Narrow transcript" : "Wide transcript", desc: "Appearance", run: () => toggleView("wide") },
    { label: "Download conversation", desc: "Markdown file", run: () => $("#export-link").click() },
    { label: "Download full log", desc: "With system prompt, thinking and tool calls", run: () => $("#export-full-link").click() },
    { label: "Keyboard shortcuts", desc: "Every key the UI knows", keys: "?", run: () => openPalette({ shortcuts: true }) },
  ];
}

// ── boot ──────────────────────────────────────────────────────────────────────
initNotify({ isLive: live, onUnseenChange: updateTitle });
initChips({
  send, status: () => status, openSettings, closeMenu,
  openToolsMenu: () => toggleMenu(fakeClick, "#tools-menu", "Tools", refreshToolsMenu),
});
initMcp({
  send, post, status: () => status, openSettings, closeMenu,
  attach: (f) => { attachments.push({ id: ++attSeq, status: "ready", ...f }); renderChips(); },
});
initSkills({ send, status: () => status, closeMenu, openPrompt: openPromptDrawer, changed: refreshState });
initFind();
initPalette({
  commands: () => state?.commands || [],
  actions: paletteActions,
  run: (cmd) => send(cmd),
  fill: (text) => { input.value = text; autosize(); input.focus(); },
});
makeResizable($("#sessions-drawer"), "right", "momo.sessionsWidth", 320);
for (const [id, min] of [["#settings-drawer", 360], ["#prompt-drawer", 420], ["#plan-drawer", 360], ["#filter-drawer", 420]]) {
  makeResizable($(id), "left", `momo.width.${id.slice(1)}`, min);
}
initSessions({ send, post, close: DRAWERS["#sessions-drawer"] });
initChanges({
  open: () => openDrawer("#changes-drawer"),
  post,
  showFile: (rel) => showFile(rel),
  diffStyle: () => view.diffStyle,
  busy: () => busy && !waiting,
});
initFiles({
  open: () => openDrawer("#files-drawer"),
  attach: (f) => {
    attachments.push({ id: ++attSeq, status: "ready", ...f });
    renderChips();
  },
  insertPath: (path) => { insertPath(path); input.focus(); },
});
startCompanion(() => ({ mode: status.mode, busy: busy && !waiting, enabled: view.companion }));
syncViewMenu();
autosize();
connect();
input.focus();
