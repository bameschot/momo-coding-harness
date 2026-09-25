// momo web UI — mirrors the curses TUI (harness/tui.py) over SSE + JSON POSTs.
// No dependencies. All model/tool text is HTML-escaped before it reaches the DOM.

import { esc, highlight, langFromPath } from "./highlight.js";
import { renderMarkdown } from "./markdown.js";

const $ = (sel) => document.querySelector(sel);
const transcript = $("#transcript");
const input = $("#input");

// ── view options (per browser, like the TUI's local toggles) ──────────────────
const VIEW_DEFAULTS = { tools: true, think: true, md: true, diff: true, diffStyle: "compact", companion: true };
const view = { ...VIEW_DEFAULTS, ...loadJSON("momo.view", {}) };
// CommandResult view fields → our keys
const VIEW_MAP = { tool_output: "tools", think_output: "think", md_render: "md",
                   diff_output: "diff", diff_style: "diffStyle", companion: "companion" };

function loadJSON(key, dflt) {
  try { return JSON.parse(localStorage.getItem(key)) ?? dflt; } catch { return dflt; }
}
function saveView() {
  try { localStorage.setItem("momo.view", JSON.stringify(view)); } catch { /* private mode */ }
}

// ── light/dark theme (per browser; index.html applies the saved choice before paint) ──
const darkQuery = matchMedia("(prefers-color-scheme: dark)");
function currentTheme() {
  return document.documentElement.dataset.theme || (darkQuery.matches ? "dark" : "light");
}
function renderThemeBtn() {
  const dark = currentTheme() === "dark";
  const label = dark ? "Switch to light mode" : "Switch to dark mode";
  const btn = $("#theme-btn");
  btn.title = label;
  btn.setAttribute("aria-label", label);
  btn.querySelector("use").setAttribute("href", dark ? "#i-sun" : "#i-moon");
}
$("#theme-btn").onclick = () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("momo.theme", JSON.stringify(next)); } catch { /* private mode */ }
  renderThemeBtn();
};
darkQuery.addEventListener("change", renderThemeBtn);  // follows the OS until the user picks
renderThemeBtn();

// ── shared state ──────────────────────────────────────────────────────────────
let events = [];            // transcript events, for re-rendering on view changes
let state = null;           // last /api/state snapshot
let status = {};            // last status event
let busy = false, waiting = false;
let history = [], histIdx = -1, histStash = "";
let queue = [];             // messages typed while busy: {text, atts}
let editing = null;         // {attachments: [names]} while editing the last message
let connectedAt = 0;        // ignore backlog replay for notifications
let unseen = false;         // new activity while the window was away
let busySince = 0;
let focused = document.hasFocus();  // a visible but unfocused window still counts as away

// ── helpers ───────────────────────────────────────────────────────────────────
// An icon from the SVG sprite in index.html (same size and stroke everywhere).
function icon(name, cls = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
async function post(path, body = {}) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    // JSON errors carry {error}; the rest are plain text.
    const body = await r.text();
    let msg = body;
    try { msg = JSON.parse(body).error || body; } catch { /* plain text */ }
    throw new Error(`${r.status} ${msg}`.trim());
  }
  return r.json();
}

// ── copy buttons on code blocks ───────────────────────────────────────────────
async function copyText(text) {
  // The async clipboard API needs a secure context: localhost is one, but plain
  // http on a LAN address (--web-host 0.0.0.0) is not — fall back to execCommand.
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = el("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
  document.body.append(ta);
  ta.select();
  try {
    if (!document.execCommand("copy")) throw new Error("copy command was refused");
  } finally {
    ta.remove();
  }
}

function addCopyButtons(root) {
  for (const pre of root.querySelectorAll("pre")) {
    const code = pre.querySelector("code");
    if (!code || pre.parentElement.classList.contains("code-block")) continue;
    const box = el("div", "code-block");
    pre.replaceWith(box);
    const bar = el("div", "code-bar");
    const lang = code.dataset.lang;
    const btn = el("button", "copy-btn", "Copy");
    btn.type = "button";
    btn.title = "Copy code";
    btn.setAttribute("aria-label", lang ? `Copy ${lang} code` : "Copy code");
    let timer;
    btn.onclick = async () => {
      try {
        await copyText(code.textContent);
        btn.textContent = "Copied ✓";
        btn.classList.add("done");
      } catch {
        btn.textContent = "Copy failed";
      }
      clearTimeout(timer);
      timer = setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("done"); }, 1600);
    };
    bar.append(el("span", "code-lang", lang || ""), btn);
    box.append(bar, pre);
  }
}

// ── transcript rendering ──────────────────────────────────────────────────────
// Render context: tool calls waiting for their result, and the open ask card.
let pendingTools = [];
let openAsk = null;

function resetRenderContext() { pendingTools = []; openAsk = null; }

function renderEvent(ev) {
  switch (ev.type) {
    case "user": return renderUser(ev.text);
    case "chat": return renderChat(ev.role, ev.text);
    case "error": return renderChat("system", `ERROR: ${ev.text}`, "error");
    case "think": return renderThink(ev.text);
    case "tool_call": return renderToolCall(ev.name, ev.args);
    case "tool_result": return renderToolResult(ev.name, ev.result);
    case "diff": return renderDiff(ev);
    case "ask_user": return renderAsk(ev.question);
  }
  return null;
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
    wrap.append(msgActions([["copy", "Copy", "Copy this reply as Markdown", async (btn) => {
      try { await copyText(text); flash(btn, "Copied ✓"); } catch { flash(btn, "Copy failed"); }
    }]]));
    return wrap;
  }
  wrap.textContent = text;
  // `[y/N]` confirmation prompts from /commands get quick-answer buttons.
  if (isConfirmPrompt(role, text)) return askCard(text, true, "Confirm");
  return wrap;
}

// A /command confirmation (Controller._pending_confirm) arrives as a plain system
// message, not as an ask_user event — both the renderer and the notifier need to
// recognise it, so the test lives in one place.
const isConfirmPrompt = (role, text) => role === "system" && / \[y\/N\]$/.test(text);

function renderThink(text) {
  if (!view.think) return null;
  const d = el("details", "think");
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  d.append(el("summary", "", `thinking (${words} words)`), el("div", "think-body", text));
  return d;
}

function argsBrief(args) {
  const parts = Object.entries(args || {}).map(([k, v]) => {
    let s = typeof v === "string" ? v : JSON.stringify(v);
    if (s.length > 60) s = s.slice(0, 57) + "…";
    return `${k}=${JSON.stringify(s)}`;
  });
  return parts.join("  ");
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
  s.append(el("span", "", "▶"), el("span", "tname", name), el("span", "targs", argsBrief(args)), st);
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
    const more = el("button", "more", `show all (${lines.length - maxLines} more lines)`);
    more.type = "button";
    more.onclick = () => { pre.textContent = text; more.remove(); };
    frag.append(more);
  }
  return frag;
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
    t.body.append(clampedPre(text, 20));
    if (isErr) t.el.open = true;
    return null;
  }
  // Orphan result (e.g. replay without its call) — render standalone.
  const d = el("details", "tool");
  const s = el("summary");
  s.append(el("span", "", "→"), el("span", "tname", name || "result"),
           el("span", "targs", ""), el("span", `tstate ${isErr ? "err" : "ok"}`, `${lines} lines`));
  const body = el("div", "tool-body");
  body.append(clampedPre(text, 20));
  d.append(s, body);
  return d;
}

const DIFF_MAX = 40;
const MUTATING = new Set(["edit_file", "append_to_file", "write_file", "delete_file", "move_file"]);
function renderDiff(ev) {
  // A diff replaces the terse result of its mutating tool call.
  const idx = pendingTools.findIndex((t) => MUTATING.has(t.name));
  if (idx >= 0 && view.tools) {
    const t = pendingTools.splice(idx, 1)[0];
    t.state.className = "tstate ok";
    t.state.textContent = "applied";
  }
  if (!view.diff) return null;
  const box = el("div", "diff");
  if (ev.op === "move") {
    const h = el("div", "diff-head");
    h.textContent = `± renamed ${ev.path} → ${ev.dst}`;
    box.append(h);
    return box;
  }
  const head = el("div", "diff-head");
  if (view.diffStyle === "git") {
    head.classList.add("git");
    head.append(el("div", "", `diff --git a/${ev.path} b/${ev.path}`),
                el("div", "minus", `--- ${ev.is_new ? "/dev/null" : "a/" + ev.path}`),
                el("div", "plus", `+++ ${ev.op === "delete" ? "/dev/null" : "b/" + ev.path}`));
  } else {
    head.append(el("span", "", `± ${ev.path}`));
    const stat = el("span", "stat");
    if (ev.is_new) stat.innerHTML = `(new file <span class="add">+${ev.added}</span>)`;
    else if (ev.op === "delete") stat.innerHTML = `(deleted <span class="del">−${ev.removed}</span>)`;
    else stat.innerHTML = `(<span class="add">+${ev.added}</span> <span class="del">−${ev.removed}</span>)`;
    head.append(stat);
  }
  box.append(head);

  const body = el("div", "diff-body");
  const numW = Math.max(1, ...ev.body.flatMap(([, o, n]) => [o, n]).filter((x) => x != null).map((x) => String(x).length));
  const fmt = (n) => (n == null ? "" : String(n)).padStart(numW);
  const addLines = (rows) => {
    for (const [kind, o, n, text] of rows) {
      const row = el("div", `dl ${kind}`);
      row.append(el("span", "gut", `${fmt(o)} ${fmt(n)}`), el("span", "txt", text));
      body.append(row);
    }
  };
  addLines(ev.body.slice(0, DIFF_MAX));
  box.append(body);
  if (ev.body.length > DIFF_MAX) {
    const more = el("button", "more", `show ${ev.body.length - DIFF_MAX} more diff lines`);
    more.type = "button";
    more.onclick = () => { addLines(ev.body.slice(DIFF_MAX)); more.remove(); };
    box.append(more);
  }
  return box;
}

function renderAsk(question) {
  const yn = /Reply 'y'|y = run it|\[y\/N\]/.test(question);
  return askCard(question, yn, "momo asks");
}

function askCard(question, yn, title) {
  const card = el("div", "ask");
  card.append(el("div", "ask-title", `? ${title}`), el("div", "ask-q", question));
  if (yn) {
    const actions = el("div", "ask-actions");
    const yes = el("button", "primary", "Yes (y)");
    const no = el("button", "", "No");
    yes.type = no.type = "button";
    yes.onclick = () => send("y");
    no.onclick = () => send("n");
    actions.append(yes, no);
    card.append(actions);
  }
  openAsk = card;
  return card;
}

function markAnswered(card, answer) {
  card.classList.add("answered");
  card.querySelector(".ask-actions")?.remove();
  card.querySelector(".ask-title").textContent += ` — answered: ${answer.length > 40 ? answer.slice(0, 40) + "…" : answer}`;
}

// ── message actions ───────────────────────────────────────────────────────────
function msgActions(items, cls = "") {
  const bar = el("div", `msg-actions ${cls}`.trim());
  for (const [ic, label, title, fn] of items) {
    const b = el("button");
    b.append(icon(ic, "icon sm"), el("span", "", label));
    b.type = "button";
    b.title = title;
    b.onclick = () => fn(b);
    bar.append(b);
  }
  return bar;
}

// Briefly swap a button's label (its last text span, or the whole button).
function flash(btn, text) {
  const target = btn.querySelector(":scope > span:last-child") || btn;
  const orig = target.dataset.label || target.textContent;
  target.dataset.label = orig;
  target.textContent = text;
  setTimeout(() => { target.textContent = orig; }, 1400);
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
  editing = { attachments: info.attachments || [] };
  input.value = info.typed;
  $("#edit-atts").textContent = editing.attachments.length
    ? ` (keeps ${editing.attachments.join(", ")})` : "";
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
  refreshUserActions();
  if (openAsk && !waiting && !openAsk.classList.contains("answered")) {
    // Replayed question that has since been answered elsewhere — keep, but no buttons.
    openAsk.querySelector(".ask-actions")?.remove();
  }
  if (atBottom) transcript.scrollTop = transcript.scrollHeight;
}

// ── status / busy ─────────────────────────────────────────────────────────────
const SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
let spinI = 0;

function applyStatus(s) {
  const planChanged = s.plan_progress !== status.plan_progress;
  status = s;
  const modeSel = $("#mode");
  if (modeSel.value !== s.mode) modeSel.value = s.mode;
  const pp = $("#plan-progress");
  pp.hidden = !s.plan_progress;
  pp.textContent = s.plan_progress;
  $("#model").textContent = s.model;
  $("#host").textContent = `${s.provider}@${s.host.replace(/^https?:\/\//, "")}`;
  $("#workdir").textContent = s.workdir;
  $("#workdir").title = s.workdir;
  $("#ctx-pct").textContent = `${s.ctx_pct}%`;
  $("#ctx-fill").style.width = `${Math.min(100, s.ctx_pct)}%`;
  $(".ctx").className = `ctx ${s.ctx_color}`;
  if (!$("#ctx-menu").hidden) scheduleCtxRefresh();
  if (!$("#index-menu").hidden) scheduleIndexRefresh();
  $("#tools-badge").hidden = s.tools_enabled;
  const run = $("#run-badge");
  run.textContent = s.run_confirm ? "RUN: confirm" : "RUN: auto";
  run.classList.toggle("on", s.run_confirm);
  const netOn = s.net_access !== "off";
  const net = $("#net-badge");
  net.classList.toggle("warn", netOn);
  net.textContent = netOn ? `NET: ${s.net_access}${s.net_confirm ? "" : " (writes: auto)"}` : "NET: off";
  net.title = netOn ? "Internet access is on — click to turn it off" : "Internet access is off — click to turn it on";
  $("#net-on").checked = netOn;
  $("#net-local").checked = s.net_access === "local";
  $("#net-local").disabled = !netOn;
  $("#net-confirm").checked = s.net_confirm;
  $("#guides").checked = s.guides;
  $("#net-confirm").disabled = !netOn;
  const mb = $("#net-max-bytes");
  if (document.activeElement !== mb) mb.value = formatSize(s.net_max_bytes);
  mb.disabled = !netOn;
  const mc = $("#net-max-chars");
  if (document.activeElement !== mc) mc.value = s.net_max_chars ?? "";
  mc.disabled = !netOn;
  applyIndexStatus(s);
  updateTitle();
  if (planChanged) refreshState();
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
  badge.title = !on ? "Code index is off — click to turn it on"
    : (s.index_degraded ? "Code index is over its memory budget and dropped a part. " : "")
      + "Code index — click for what it holds";
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

// Mirrors net.format_size in Python so the field shows what /net-max-bytes accepts.
function formatSize(n) {
  for (const [unit, size] of [["MB", 1048576], ["KB", 1024]]) {
    if (n >= size) {
      const v = n / size;
      return (v >= 10 || v === Math.floor(v)) ? `${Math.round(v)}${unit}` : `${v.toFixed(1)}${unit}`;
    }
  }
  return `${n}`;
}

function updateTitle() {
  document.title = `${unseen ? "(•) " : ""}momo · ${status.mode || ""}${busy ? " …" : ""}`;
}

// ── notifications (only while the window is away) ─────────────────────────────
// "Away" is hidden *or* unfocused: `document.hidden` alone misses the common
// layout of the browser sitting open next to the editor — visible, but not the
// window you are looking at.
const away = () => document.hidden || !focused;
const live = () => Date.now() - connectedAt > 2000;  // skip the backlog replay after (re)connect
const NOTIFY_MIN_TURN_MS = 2000;  // below this a turn is an echo, not news

// `momo.notify` held a bare boolean before the sound option existed.
function loadNotifyPrefs() {
  const raw = loadJSON("momo.notify", null);
  if (raw === true) return { desktop: true, sound: false };
  if (raw && typeof raw === "object") return { desktop: !!raw.desktop, sound: !!raw.sound };
  return { desktop: false, sound: false };
}
const notifyPrefs = loadNotifyPrefs();
let desktopOn = notifyPrefs.desktop && "Notification" in window && Notification.permission === "granted";

function saveNotifyPrefs() {
  try { localStorage.setItem("momo.notify", JSON.stringify(notifyPrefs)); } catch { /* private mode */ }
}

// A kitten's mew, synthesised so the front end stays asset-free. A kitten is
// high (~1 kHz), soft and nearly pure, so the source is a sine with a few soft
// overtones; a lowpass opening and closing on it is the mouth ("m-ew"), and a
// quick vibrato makes it wobble like a small voice. "ask" ends on an upturn (a
// question), "finish" falls away (a resolution), so they tell apart by ear alone.
const MEW = {
  //         pitch (Hz) at 0, 30%, 65%, 100% of the mew
  ask:    [900, 1200, 980, 1550],
  finish: [950, 1300, 1150, 800],
};
const MEW_LEN = 0.4;  // short: a long mew turns into a wail
let audioCtx = null;
let mewWave = null;
function audio() {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return audioCtx;
}

function chime(kind) {
  let ctx;
  try { ctx = audio(); } catch { return; }  // no WebAudio in this browser
  const play = () => {
    try {
      const at = ctx.currentTime, end = at + MEW_LEN;
      const [p0, p1, p2, p3] = MEW[kind] || MEW.finish;

      // Source: fundamental plus modest 2nd–4th harmonics — enough for the "ee"
      // formant to have something to lift. A full sawtooth reads as a horn, a
      // bare sine as a whistle.
      if (!mewWave) mewWave = ctx.createPeriodicWave(
        new Float32Array([0, 0, 0, 0, 0]), new Float32Array([0, 1, 0.35, 0.2, 0.08]));
      const osc = ctx.createOscillator();
      osc.setPeriodicWave(mewWave);
      osc.frequency.setValueAtTime(p0, at);
      osc.frequency.exponentialRampToValueAtTime(p1, at + MEW_LEN * 0.3);
      osc.frequency.exponentialRampToValueAtTime(p2, at + MEW_LEN * 0.65);
      osc.frequency.exponentialRampToValueAtTime(p3, end);
      // Vibrato in cents, so it scales with the pitch.
      const lfo = ctx.createOscillator();
      const lfoDepth = ctx.createGain();
      lfo.frequency.value = 6;
      lfoDepth.gain.value = 12;
      lfo.connect(lfoDepth).connect(osc.detune);

      // The mouth: closed ("m") muffles the overtones, open lets them through.
      const mouth = ctx.createBiquadFilter();
      mouth.type = "lowpass";
      mouth.Q.value = 0.7;  // no resonant peak: a peak is what made it shrill
      mouth.frequency.setValueAtTime(700, at);
      mouth.frequency.exponentialRampToValueAtTime(3800, at + MEW_LEN * 0.2);
      mouth.frequency.exponentialRampToValueAtTime(1600, end);
      // The vowel: a lift high up is "ee"; sliding it down turns it into "ew".
      // Without it the open mouth alone says "aw".
      const vowel = ctx.createBiquadFilter();
      vowel.type = "peaking";
      vowel.Q.value = 2;
      vowel.gain.value = 10;
      vowel.frequency.setValueAtTime(3600, at);
      vowel.frequency.setValueAtTime(3600, at + MEW_LEN * 0.2);
      vowel.frequency.exponentialRampToValueAtTime(1200, end);

      // Warmth: a gentle lift under the fundamental gives the voice some body.
      const warm = ctx.createBiquadFilter();
      warm.type = "lowshelf";
      warm.frequency.value = 1500;
      warm.gain.value = 6;

      // Soft onset for the "m", swell, then fade; ramped rather than switched,
      // because a bare start/stop clicks.
      const gain = ctx.createGain();
      gain.gain.setValueAtTime(0, at);
      gain.gain.linearRampToValueAtTime(0.025, at + 0.04);
      gain.gain.linearRampToValueAtTime(0.04, at + MEW_LEN * 0.25);
      gain.gain.setValueAtTime(0.04, at + MEW_LEN * 0.45);
      gain.gain.exponentialRampToValueAtTime(0.0001, end);

      osc.connect(warm).connect(vowel).connect(mouth).connect(gain).connect(ctx.destination);
      osc.start(at); lfo.start(at);
      osc.stop(end + 0.01); lfo.stop(end + 0.01);
    } catch { /* the device went away */ }
  };
  // Scheduling into a suspended context loses the sound outright, so resume
  // first and schedule in the callback.
  if (ctx.state === "suspended") ctx.resume().then(play, () => {});
  else play();
}

// A sound setting restored from an earlier visit has had no user gesture yet, and
// the browser blocks audio until there is one — the first click or keypress is it.
function unlockAudio() {
  if (notifyPrefs.sound) { try { audio().resume(); } catch { /* ignore */ } }
}
addEventListener("pointerdown", unlockAudio, { once: true });
addEventListener("keydown", unlockAudio, { once: true });

let notifyThrew = false;  // the service-worker-only complaint is made once

// kind: "ask" (momo needs an answer) | "finish" (the turn is over)
// The sound is deliberately NOT gated on focus: a focused window does not mean
// you are watching it, and being told without having to look is the whole point
// of a cue you can hear. The desktop notification is gated, because notifying
// about the window you are staring at is just noise.
function signal(kind, title, body) {
  if (!live()) return;
  if (notifyPrefs.sound) chime(kind);
  if (!away()) return;
  unseen = true;
  updateTitle();
  if (!desktopOn) return;
  try {
    const n = new Notification(title, { body: body.slice(0, 180), tag: "momo", renotify: true });
    n.onclick = () => { window.focus(); n.close(); };
  } catch {
    // A few browsers only allow notifications from a service worker. Say so once:
    // failing silently here is indistinguishable from the feature being broken.
    if (!notifyThrew) {
      notifyThrew = true;
      attNote("This browser needs a service worker for notifications — the sound still works.");
    }
  }
}

// Coming back clears the marker. Both events are wired: minimising fires blur,
// switching tabs fires visibilitychange, and which arrives varies by platform.
function onPresence() {
  if (!away() && unseen) { unseen = false; updateTitle(); }
}
document.addEventListener("visibilitychange", onPresence);
addEventListener("focus", () => { focused = true; onPresence(); });
addEventListener("blur", () => { focused = false; });

$("#notify-desktop").checked = desktopOn;
$("#notify-desktop").onchange = async (e) => {
  if (e.target.checked) {
    // Plain HTTP on a LAN address is not a secure context, so the API is simply
    // absent there — say so instead of blaming the browser's settings.
    if (!isSecureContext) {
      e.target.checked = false;
      return attNote("Notifications need a secure origin — open http://localhost or tunnel over SSH.");
    }
    if (!("Notification" in window)) { e.target.checked = false; return attNote("This browser has no notifications."); }
    const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    desktopOn = perm === "granted";
    e.target.checked = desktopOn;
    if (!desktopOn) attNote("Notifications are blocked for this page in the browser settings.");
  } else desktopOn = false;
  notifyPrefs.desktop = desktopOn;
  saveNotifyPrefs();
};

$("#notify-sound").checked = notifyPrefs.sound;
$("#notify-sound").onchange = (e) => {
  notifyPrefs.sound = e.target.checked;
  saveNotifyPrefs();
  if (notifyPrefs.sound) chime("ask");  // this click is the gesture that unlocks audio
};

function lastAssistantText() {
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].type === "chat" && events[i].role === "assistant") return events[i].text;
  }
  return "";
}

function applyBusy(b, w) {
  const wasBusy = busy;
  if (b && !wasBusy) busySince = Date.now();
  busy = b; waiting = w;
  document.body.classList.toggle("is-busy", busy);
  if (wasBusy && !busy) {
    if (Date.now() - busySince > NOTIFY_MIN_TURN_MS) {
      signal("finish", "momo finished", lastAssistantText().split("\n").find((l) => l.trim()) || "The turn is complete.");
    }
    // Send the next queued message once the turn is over.
    if (queue.length) setTimeout(sendNextQueued, 150);
  }
  $("#stop").hidden = !(busy && !waiting);
  const bz = $("#busy");
  bz.classList.toggle("waiting", waiting);
  if (!busy) bz.textContent = "";
  else if (waiting) bz.textContent = "? waiting for your answer";
  input.placeholder = waiting ? "Answer momo…" : busy ? "momo is working… (Esc to interrupt)" : "Message momo…  (/ for commands)";
  if (!waiting && openAsk && !openAsk.classList.contains("answered")) openAsk.querySelector(".ask-actions")?.remove();
  if (waiting) input.focus();
  updateTitle();
}

setInterval(() => {
  if (busy && !waiting) $("#busy").textContent = `${SPIN[spinI++ % SPIN.length]} thinking`;
}, 100);

// ── event stream ──────────────────────────────────────────────────────────────
// Batched event delivery coalesces a burst into one render. It must NOT go through
// requestAnimationFrame alone: browsers pause rAF entirely in a hidden tab or a
// minimised window, so every event — including the turn-finished one a notification
// is meant to announce — would sit in the batch until you came back and looked,
// which is exactly when you no longer need telling. Fall back to a timer there.
const schedule = (fn) => (document.hidden ? setTimeout(fn, 0) : requestAnimationFrame(fn));

let es;
function connect() {
  es = new EventSource("api/events");
  es.onopen = () => {
    // Every (re)connect replays the full backlog: start from a clean slate.
    connectedAt = Date.now();
    events = [];
    rerenderAll();
    $("#conn-dot").classList.add("on");
    $("#conn-dot").title = "Connected";
    refreshState();
  };
  es.onerror = () => {
    $("#conn-dot").classList.remove("on");
    $("#conn-dot").title = "Disconnected — retrying";
  };
  let batch = [];
  let scheduled = false;
  es.onmessage = (m) => {
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

function handleEvents(evs) {
  const nodes = [];
  for (const ev of evs) {
    switch (ev.type) {
      case "status": applyStatus(ev); break;
      case "busy": applyBusy(ev.busy, ev.waiting); break;
      case "companion": applyCompanion(ev); break;
      case "done": break;
      case "delta": streamDelta(ev); break;
      case "stream_end": endStream(); break;
      case "reset":
        endStream();
        events = [];
        transcript.replaceChildren();
        nodes.length = 0;
        resetRenderContext();
        break;
      default: {
        events.push(ev);
        if (ev.type === "ask_user") signal("ask", "momo has a question", ev.question);
        else if (ev.type === "chat" && isConfirmPrompt(ev.role, ev.text)) {
          signal("ask", "momo needs a confirmation", ev.text);
        } else if (away() && live() && !unseen) { unseen = true; updateTitle(); }
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
  }
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
  $("#think-mode").checked = state.think;
  $("#plan-btn").hidden = !state.plan;
  $("#plan-body").innerHTML = state.plan ? renderMarkdown(state.plan) : "<p class='muted'>No active plan.</p>";
  addCopyButtons($("#plan-body"));
  $("#plan-phase").textContent = state.plan_phase || "";
  companion.frames = state.companion;
  renderSkills();
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
    handleEvents([{ type: "error", text: `could not reach momo: ${e.message}` }]);
    return false;
  }
}

function applyView(v) {
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
    input.value = "";
    autosize();
    stopEdit();
    try { await post("api/edit", { text }); } catch (e) { showError(e); input.value = text; autosize(); }
    return;
  }
  if (!text.trim() && !ready.length) return;
  // Busy with a turn: queue plain messages instead of bouncing them.
  if (busy && !waiting && !isCmd) {
    queue.push({ text, atts: ready.map((a) => ({ name: a.name, text: a.text })) });
    attachments = attachments.filter((a) => a.status !== "ready");
    input.value = "";
    histIdx = -1; histStash = "";
    autosize();
    closeSuggest();
    renderChips();
    renderQueue();
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
  } else if (!ok && !input.value) {
    input.value = text;  // keep what the user typed if the request failed
    autosize();
  }
}

// ── queued messages ───────────────────────────────────────────────────────────
async function sendNextQueued() {
  if (busy || !queue.length) return;
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
    const meta = el("span", "att-meta", i === 0 ? "sends when momo is done" : `#${i + 1} in queue`);
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
      const limit = state?.context_limit || 0;
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
  if (!state || !v.startsWith("/") || v.includes("\n") || /\s\S*\s/.test(v)) return closeSuggest();
  const word = v.split(/\s/)[0].toLowerCase();
  sugg = state.commands.filter((c) => c.cmd.startsWith(word) || c.usage.startsWith(v));
  if (!sugg.length || (sugg.length === 1 && sugg[0].usage === v.trim())) return closeSuggest();
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
$("#run-badge").onclick = () => send(`/run-confirm ${status.run_confirm ? "off" : "on"}`);
$("#tools-badge").onclick = () => send("/tools on");
$("#net-badge").onclick = () => send(`/net ${status.net_access !== "off" ? "off" : "on"}`);
$("#net-on").onchange = (e) => send(`/net ${e.target.checked ? "on" : "off"}`);
$("#net-local").onchange = (e) => send(`/net ${e.target.checked ? "local" : "on"}`);
$("#index-badge").onclick = (e) => {
  if (!status.index_enabled) { e.stopPropagation(); return send("/index on"); }
  toggleMenu(e, "#index-menu", "Code index", refreshIndexMenu);
};
// Settings controls name their command in data-cmd: a checkbox sends on/off, a
// text field sends its value.  Status events bring the server's value back.
for (const c of document.querySelectorAll("input[data-cmd]")) {
  c.onchange = () => {
    if (c.type === "checkbox") return send(`${c.dataset.cmd} ${c.checked ? "on" : "off"}`);
    const v = c.value.trim();
    if (v) send(`${c.dataset.cmd} ${v}`);
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
function closeFilter() {
  const dirty = $("#filter-text").value !== filterLoaded;
  if (dirty && !confirm("Discard your changes to the index filter?")) return;
  $("#filter-drawer").hidden = true;
}
function openFilter() {
  closeMenu();
  $("#filter-drawer").hidden = false;
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
  if (!confirm("Replace the filter with one built from the project's current .gitignore files?")) return;
  await send("/index-filter reset");
  await loadFilter();
};
$("#filter-text").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.preventDefault(); closeFilter(); }
  else if (e.key === "s" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); $("#filter-save").click(); }
});
$("#index-load").onclick = () => send("/index load");

// Shift+letter shortcuts when focus is outside the text box (TUI: chat focus).
document.addEventListener("keydown", (e) => {
  if (e.target === input || e.target.matches?.("input, select, textarea")) return;
  if (e.key === "Escape") {
    if (MENUS.some(([, m]) => !$(m).hidden)) return closeMenu();
    // Through each drawer's own close, so the filter's unsaved-changes check runs.
    for (const [d, close] of Object.entries(DRAWERS)) {
      if (!$(d).hidden) return close();
    }
    if (busy && !waiting) post("api/cancel").catch(() => {});
    return;
  }
  if (!e.shiftKey || e.metaKey || e.ctrlKey || e.altKey) return;
  const map = { T: "think", M: "md", D: "diff", Q: "companion" };
  if (map[e.key]) { e.preventDefault(); toggleView(map[e.key]); }
  else if (e.key === "P") { e.preventDefault(); $("#run-badge").click(); }
  else if (e.key === "C") { e.preventDefault(); post("api/cancel").catch(() => {}); }
  else if (e.key === "Tab") { e.preventDefault(); cycleMode(); }
});

// ── view menu ─────────────────────────────────────────────────────────────────
function syncViewMenu() {
  for (const cb of document.querySelectorAll("[data-view]")) cb.checked = !!view[cb.dataset.view];
  for (const r of document.querySelectorAll("input[name=diff-style]")) r.checked = r.value === view.diffStyle;
  $("#composer").classList.toggle("no-companion", !view.companion);
  $("#companion").hidden = !view.companion;
  $("#notify-desktop").checked = desktopOn;
  $("#notify-sound").checked = notifyPrefs.sound;
}
// Popover menus: [button, menu].  One is open at a time; a click outside closes it.
const MENUS = [["#view-btn", "#view-menu"], ["#model-btn", "#model-menu"],
               ["#ctx-btn", "#ctx-menu"], ["#index-badge", "#index-menu"]];
function closeMenu() {
  for (const [b, m] of MENUS) {
    $(m).hidden = true;
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
  m.hidden = false;
  $(MENUS.find(([, x]) => x === menu)[0]).setAttribute("aria-expanded", "true");
  load?.();
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
$("#plan-btn").onclick = () => { refreshState(); $("#plan-drawer").hidden = false; };
$("#plan-progress").onclick = $("#plan-btn").onclick;
$("#plan-close").onclick = () => DRAWERS["#plan-drawer"]();
for (const b of document.querySelectorAll("#plan-drawer [data-cmd]")) {
  b.onclick = () => { send(b.dataset.cmd); DRAWERS["#plan-drawer"](); };
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
  menu.replaceChildren(...rows);
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
    el("div", "muted small", `Categories are estimates (~4 chars/token), total ~${fmtTok(b.estimated)}. Thinking stays in the transcript but is not re-sent.`));
}
$("#ctx-btn").onclick = (e) => toggleMenu(e, "#ctx-menu", "Context", refreshCtxMenu);

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
  if (!b.enabled) return closeMenu();
  renderIndexMenu(menu, b);
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
    const btn = el("button", "menu-link", label);
    btn.type = "button";
    btn.onclick = () => send(cmd);
    return btn;
  };
  const filterBtn = el("button", "menu-link", "Filter…");
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
// before discarding unsaved edits.
const hideDrawer = (id) => () => { $(id).hidden = true; };
const DRAWERS = {
  "#filter-drawer": closeFilter,
  "#plan-drawer": hideDrawer("#plan-drawer"),
  "#sessions-drawer": hideDrawer("#sessions-drawer"),
  "#files-drawer": hideDrawer("#files-drawer"),
};

// The left drawers (sessions, workspace files) share one slot: opening one closes the other.
function openDrawer(id) {
  for (const d of ["#sessions-drawer", "#files-drawer"]) $(d).hidden = d !== id || !$(d).hidden;
  return !$(id).hidden;
}
for (const b of document.querySelectorAll(".drawer-close")) b.onclick = () => DRAWERS[`#${b.closest("aside").id}`]();

const ago = (t) => {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return new Date(t * 1000).toLocaleDateString();
};

// Session drawer: click a row to load it; the trash button (or Select mode with
// checkboxes) deletes saved sessions after an inline confirmation. The open
// session can't be deleted, and the server refuses it too.
const sessionsUI = { data: null, selecting: false, selected: new Set(), note: "" };

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

// Replace `host`'s children with "question [Delete] [Cancel]"; resolves on click.
function inlineConfirm(host, question) {
  return new Promise((resolve) => {
    const saved = [...host.childNodes];
    const box = el("div", "session-confirm");
    const yes = el("button", "danger small", "Delete");
    const no = el("button", "small", "Cancel");
    yes.type = no.type = "button";
    const done = (ok) => { host.replaceChildren(...saved); resolve(ok); };
    yes.onclick = () => done(true);
    no.onclick = () => done(false);
    box.append(el("span", "", question), el("span", "spacer"), yes, no);
    host.replaceChildren(box);
    yes.focus();
  });
}

async function deleteSessions(names) {
  let res;
  try { res = await post("api/sessions/delete", { names }); } catch (e) { sessionsUI.note = `Delete failed: ${e.message}`; renderSessions(); return; }
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
  if (!data.sessions.length) items.push(el("div", "muted", "No saved sessions yet."));
  for (const x of data.sessions) {
    const current = x.name === data.current;
    const item = el("div", "session-item");
    const row = el("button", `session-row${current ? " current" : ""}`);
    row.type = "button";
    row.title = current ? `${x.name} (open now)\n${x.workdir}` : `${x.name}\n${x.workdir}`;
    row.append(el("span", "session-preview", x.preview || "(no messages)"),
               el("span", "session-meta", `${x.mode} · ${x.model} · ${x.messages} msgs · ${ago(x.mtime)}`));
    if (selecting) {
      const cb = el("input");
      cb.type = "checkbox";
      cb.disabled = current;
      cb.checked = selected.has(x.name);
      cb.setAttribute("aria-label", `Select ${x.preview || x.name}`);
      cb.onchange = () => { cb.checked ? selected.add(x.name) : selected.delete(x.name); updateBulk(); };
      row.onclick = () => { if (!current) { cb.checked = !cb.checked; cb.onchange(); } };
      item.append(cb, row);
    } else {
      row.onclick = () => {
        if (current) return;
        DRAWERS["#sessions-drawer"]();
        send(`/session ${x.name}`);
      };
      item.append(row);
      if (!current) {
        const del = iconButton("trash", "Delete this session", "icon-btn session-del");
        del.onclick = async () => {
          if (await inlineConfirm(item, "Delete this session?")) deleteSessions([x.name]);
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

$("#sessions-btn").onclick = () => {
  if (!openDrawer("#sessions-drawer")) return;
  sessionsUI.note = "";
  loadSessions();
};
$("#select-sessions").onclick = () => {
  sessionsUI.selecting = !sessionsUI.selecting;
  sessionsUI.selected.clear();
  sessionsUI.note = "";
  if (sessionsUI.data) renderSessions();
};
$("#sessions-all").onclick = () => {
  const { data, selected } = sessionsUI;
  const all = data.sessions.filter((x) => x.name !== data.current).map((x) => x.name);
  const every = all.length && all.every((n) => selected.has(n));
  selected.clear();
  if (!every) for (const n of all) selected.add(n);
  renderSessions();
};
$("#sessions-delete").onclick = async () => {
  const names = [...sessionsUI.selected];
  const bar = $("#sessions-bulk");
  if (await inlineConfirm(bar, `Delete ${names.length} session${names.length === 1 ? "" : "s"}? This can't be undone.`)) {
    await deleteSessions(names);
  }
};
$("#new-session").onclick = () => { DRAWERS["#sessions-drawer"](); send("/new"); };


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

$("#files-btn").onclick = () => {
  if (!openDrawer("#files-drawer")) return;
  $("#file-preview").hidden = true;
  $("#files-tree").hidden = false;
  loadDir("", $("#files-tree"));
};
$("#files-hidden").onchange = () => loadDir("", $("#files-tree"));

// Drag the right edge to widen the workspace drawer (CSS clamps to 420px min).
{
  const drawer = $("#files-drawer"), grip = drawer.querySelector(".drawer-resize");
  const setW = (w) => drawer.style.setProperty("--files-w", `${Math.round(w)}px`);
  const saved = loadJSON("momo.filesWidth", 0);
  if (saved) setW(saved);
  grip.onpointerdown = (e) => {
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    grip.classList.add("dragging");
    grip.onpointermove = (m) => setW(Math.max(420, Math.min(m.clientX, innerWidth)));
    grip.onpointerup = () => {
      grip.onpointermove = grip.onpointerup = null;
      grip.classList.remove("dragging");
      const w = drawer.getBoundingClientRect().width;
      try { localStorage.setItem("momo.filesWidth", JSON.stringify(Math.round(w))); } catch { /* private mode */ }
    };
  };
  grip.ondblclick = () => {
    drawer.style.removeProperty("--files-w");
    try { localStorage.removeItem("momo.filesWidth"); } catch { /* private mode */ }
  };
}

let previewFile = null;
async function openPreview(rel) {
  let f;
  try {
    const r = await fetch(`api/file?path=${encodeURIComponent(rel)}`);
    f = await r.json();
    if (!r.ok) throw new Error(f.error);
  } catch (e) { return attNote(e.message); }
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
}
const PREVIEW_MAX_CHARS = 200_000;  // also highlight.js's limit: beyond it, text is shown plain
function renderPreviewCode(f, rel, text) {
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
$("#preview-render").onclick = () => {
  const on = $(".preview-md").hidden;
  setPreviewRendered(on);
  try { localStorage.setItem("momo.previewRendered", JSON.stringify(on)); } catch { /* private mode */ }
};
$("#preview-back").onclick = () => { $("#file-preview").hidden = true; $("#files-tree").hidden = false; };
$("#preview-attach").onclick = () => {
  const f = previewFile;
  if (!f) return;
  attachments.push({ id: ++attSeq, status: "ready", name: f.name, text: f.text, chars: f.chars,
                     pages: f.pages, truncated: f.truncated, kind: f.kind });
  renderChips();
  flash($("#preview-attach"), "Attached ✓");
};
$("#preview-insert").onclick = () => { if (previewFile) { insertPath(previewFile.path); input.focus(); } };
$("#preview-copy").onclick = async () => {
  if (!previewFile) return;
  try { await copyText(previewFile.text); flash($("#preview-copy"), "Copied ✓"); } catch { flash($("#preview-copy"), "Copy failed"); }
};

// ── momo companion (same frames and speech lines as the TUI) ─────────────────
const companion = {
  frames: null, x: 4, dir: 1, st: "walk", sitTicks: 0, step: 0,
  blink: 0, mewTicks: 0, mew: "", frame: null,
  idle: false,
};
// Idle recap bubbles — mirrors companion.RecapPicker in Python: new recap lines are
// spoken once each, before canned lines; after that an older one only comes back
// occasionally, and never the same line within REPEAT_MS.
const recaps = {
  REPEAT_MS: 180_000, REUSE_CHANCE: 0.25, REUSE_POOL: 5, MAX_NEW: 5,  // MAX_NEW = MAX_RECAP_LINES
  lines: [], queue: [], lastShown: new Map(),
  update(lines) {
    const fresh = lines.slice(-this.MAX_NEW).filter((l) => !this.lines.includes(l) && !this.queue.includes(l));
    this.queue = [...this.queue, ...fresh].filter((l) => lines.includes(l));
    this.lines = [...lines];
  },
  // A line of at most maxLen chars (what fits beside momo right now), or null.
  // A queued line that doesn't fit stays queued for a roomier spot.
  pick(now, maxLen) {
    let line = this.queue.find((l) => l.length <= maxLen);
    if (line !== undefined) {
      this.queue.splice(this.queue.indexOf(line), 1);
    } else {
      if (Math.random() >= this.REUSE_CHANCE) return null;
      const pool = this.lines.slice(-this.REUSE_POOL)
        .filter((l) => l.length <= maxLen && now - (this.lastShown.get(l) ?? -Infinity) >= this.REPEAT_MS);
      if (!pool.length) return null;
      line = pool[Math.floor(Math.random() * pool.length)];
    }
    this.lastShown.set(line, now);
    return line;
  },
};
function applyCompanion(ev) {
  companion.idle = ev.idle;
  recaps.update(ev.lines);
  $("#idle-recap").checked = ev.enabled;
  const secs = $("#idle-recap-secs");
  if (document.activeElement !== secs) secs.value = ev.secs;  // don't clobber typing
  secs.disabled = !ev.enabled;
}
// Bubble layout — mirrors walk_max_x / bubble_dir / bubble_room in companion.py.
// The bubble sits beside the cat on its head row: right when facing right, left otherwise.
const bubbleFits = (x, cols, catW, dir, n) =>
  dir < 0 ? x >= n : x + 1 + catW + 1 + n < cols - 1;
const bubbleDir = (x, cols, catW, dir, n) =>
  [dir, -dir].find((d) => bubbleFits(x, cols, catW, d, n)) ?? null;
const bubbleRoom = (x, cols, catW) => Math.max(0, x, cols - 2 - (x + 1 + catW + 1));

// Measured monospace advance of the companion bar, so `cols` matches what fits.
let charW = 0;
function measureCharW(pre) {
  const ctx = document.createElement("canvas").getContext("2d");
  const cs = getComputedStyle(pre);  // longhands: the `font` shorthand can be "" (Firefox)
  ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  charW = ctx.measureText("M".repeat(10)).width / 10 || 6.6;
}
window.addEventListener("resize", () => { charW = 0; });

function tickCompanion() {
  const c = companion, F = c.frames, pre = $("#companion");
  if (!F || !view.companion || pre.offsetParent === null) return;
  if (!charW) measureCharW(pre);
  const cols = Math.max(20, Math.floor(pre.clientWidth / charW));
  const catW = F.cat_w, maxX = Math.max(0, cols - 2 - catW - (F.bubble_max + 2));
  if (c.st === "walk") {
    c.step ^= 1;
    c.x = Math.max(0, Math.min(c.x + c.dir, maxX));
    if (c.x === 0 || c.x === maxX || Math.random() < 0.02) {
      c.st = "sit";
      c.sitTicks = 50 + Math.floor(Math.random() * 50);
      c.blink = 0;
      // Only lines that fit beside momo here; it turns to face its bubble.
      const room = bubbleRoom(c.x, cols, catW);
      const recap = c.idle ? recaps.pick(Date.now(), room) : null;
      let text = recap;
      if (!text && Math.random() < 0.75) {
        const pool = (F.speech[`${status.mode}|${busy && !waiting ? 1 : 0}`] || F.speech_default)
          .filter((t) => t.length <= room);
        if (pool.length) text = pool[Math.floor(Math.random() * pool.length)];
      }
      if (text) {
        c.mew = text;
        // Longer lines stay up longer; recaps a little longer still.
        c.mewTicks = 18 + Math.floor(Math.random() * 15) + Math.floor(text.length / 2) + (recap ? 10 : 0);
        c.sitTicks = Math.max(c.sitTicks, c.mewTicks + 10);
        c.dir = bubbleDir(c.x, cols, catW, c.dir, text.length) ?? c.dir;
      }
    }
    if (c.blink > 0) {
      c.blink--;
      c.frame = c.dir > 0 ? F.walk_right_blink : F.walk_left_blink;
    } else {
      c.frame = (c.dir > 0 ? F.walk_right : F.walk_left)[c.step];
      if (Math.random() < 0.03) c.blink = 2;
    }
  } else {
    if (--c.sitTicks <= 0) {
      c.dir = c.x <= 2 ? 1 : c.x >= maxX - 2 ? -1 : (Math.random() < 0.5 ? -1 : 1);
      c.st = "walk";
      c.mewTicks = 0;
    }
    const sit = c.dir < 0 ? F.sit_left : F.sit;
    c.frame = sit[Math.random() < 0.08 ? 1 : 0];
    if (c.mewTicks > 0) c.mewTicks--;
  }
  const rows = c.frame.map((l) => " ".repeat(c.x + 1) + l);
  if (c.mewTicks > 0 && c.st === "sit") {
    if (c.dir < 0) {
      const t = c.mew.startsWith("< ") ? c.mew.slice(2) + " >" : c.mew;
      const mx = c.x + 1 - t.length - 1;
      if (mx >= 0) rows[1] = " ".repeat(mx) + t + " " + c.frame[1];
    } else if (bubbleFits(c.x, cols, catW, 1, c.mew.length)) {
      rows[1] = rows[1].padEnd(c.x + 1 + catW) + " " + c.mew;
    }
  }
  pre.textContent = rows.join("\n");
}
setInterval(tickCompanion, 120);

// ── boot ──────────────────────────────────────────────────────────────────────
syncViewMenu();
autosize();
connect();
input.focus();
