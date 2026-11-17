// `momo-box dash`: every box on this machine, each a card with its Setup view
// (setup-render.js, the same as the box's own web UI).  Polls api/boxes;
// shows, never changes.

import { ago, diagram, mb, renderSetup, syncClass } from "./setup-render.js";

const token = new URLSearchParams(location.search).get("token") || "";
history.replaceState(null, "", location.pathname);      // keep the token out of the address bar
const open = new Map();          // box name → the tab shown, for expanded cards
const POLL_MS = 3000;

function h(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

const SYNC = { ok: "synced", incoming: "landed beside your branch", error: "sync failed",
               "syncer-silent": "syncer not running", unknown: "no sync yet" };

function card(b, now) {
  const v = b.view;
  const sec = h("section", "dash-card");
  const head = h("div", "dash-card-head");
  head.append(h("strong", "", b.name), h("span", "chip", b.net || "?"),
    h("span", `chip ${b.state === "running" ? "active" : ""}`, b.state));
  const st = v.box.sync_state;
  const sync = h("span", "dash-sync");
  sync.append(h("span", `su-dot st-${syncClass(st)}`),
    h("span", "", `${SYNC[st] || st}${v.box.sync?.last?.ts ? ` ${ago(v.box.sync.last.ts, now)}` : ""}`));
  head.append(sync);
  if (v.box.recent_denied) head.append(h("span", "chip warn", `${v.box.recent_denied} refused`));
  const mem = v.memory || {};
  if (typeof mem.current === "number") {
    const pct = mem.max ? Math.round((mem.current / mem.max) * 100) : null;
    const chip = h("span", `chip${pct >= 95 ? " bad" : pct >= 80 ? " warn" : ""}`,
      `mem ${mb(mem.current)}${mem.max ? ` / ${mb(mem.max)}` : ""}`
      + (mem.gateway?.current ? ` + gw ${mb(mem.gateway.current)}` : ""));
    chip.title = "Memory use of the box (and its gateway), from docker stats";
    head.append(chip);
  }
  head.append(h("span", "spacer"));
  if (b.url) {
    const a = h("a", "btn small", "Open UI");
    a.href = b.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    head.append(a);
  }
  const more = h("button", "btn small", open.has(b.name) ? "Less" : "Details");
  more.type = "button";
  more.setAttribute("aria-expanded", String(open.has(b.name)));
  more.onclick = () => {
    if (open.has(b.name)) open.delete(b.name); else open.set(b.name, "setup");
    render(last);
  };
  head.append(more);
  sec.append(head);
  const meta = h("p", "dash-meta muted small",
    `lands in ${b.land || "?"}${b.syncer ? "" : b.state === "running" ? " · syncer not running" : ""}`
    + (b.last_line ? ` · ${b.last_line}` : ""));
  sec.append(meta);
  if (open.has(b.name)) {
    const body = h("div", "dash-body");
    renderSetup(v, body, { tab: open.get(b.name), onTab: (t) => open.set(b.name, t), now });
    sec.append(body);
  } else {
    sec.append(diagram(v, now));
  }
  return sec;
}

let last = null;

function render(data) {
  if (!data) return;
  const root = document.getElementById("dash-boxes");
  const now = data.now || Date.now() / 1000;
  const boxes = data.boxes || [];
  if (!boxes.length) {
    root.replaceChildren(h("p", "muted", "No boxes yet. `momo-box web --repo PATH` starts one."));
    return;
  }
  const y = window.scrollY;
  root.replaceChildren(...boxes.map((b) => card(b, now)));
  window.scrollTo(0, y);
}

async function load() {
  const out = document.getElementById("dash-updated");
  try {
    const r = await fetch("api/boxes", { headers: { "X-Momo-Token": token }, cache: "no-store" });
    if (r.status === 401) { out.textContent = "open the address momo-box dash printed (with its token)"; return; }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    last = await r.json();
    render(last);
    const m = last.memory || {};
    out.textContent = (m.engine_total ? `boxes use ${mb(m.boxes_used || 0)} of the engine's ${mb(m.engine_total)} · ` : "")
      + `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    out.textContent = `not reachable (${e.message}) — is momo-box dash still running?`;
  }
}

load();
setInterval(load, POLL_MS);
