// The Setup badge in the header and its drawer: where momo runs, where the
// model server runs and, in a momo-box, the box's live status.  The drawer
// reads GET api/setup every few seconds while it is open; the badge follows
// the status event.  Show-only: nothing here changes a setting.

import { $, closeLayer } from "./dom.js";
import { badge, renderSetup } from "./setup-render.js";

const hooks = {
  status: () => ({}),      // the last status event
  open: () => {},          // open the drawer (app.js: the right-hand slot)
};

const POLL_MS = 3000;
let tab = "setup";
let timer = null;
let inflight = false;

export function applySetupStatus(s) {
  const btn = $("#setup-badge");
  if (!btn || !s) return;
  const { text, state } = badge(s);
  btn.hidden = false;
  btn.textContent = text;
  btn.classList.toggle("warn", state === "warn");
  btn.classList.toggle("bad", state === "bad");
  btn.title = s.setup_kind === "box"
    ? "This momo runs in a momo-box — click for its setup, network and sync"
    : "Where momo and its model server run — click for details";
}

async function refresh() {
  const drawer = $("#setup-drawer");
  if (drawer.hidden) { stop(); return; }
  if (inflight) return;
  inflight = true;
  const body = $("#setup-body");
  try {
    const r = await fetch("api/setup", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const v = await r.json();
    $("#setup-kind").textContent = v.kind === "box" ? `box ${v.box?.setup?.name || ""}`.trim() : "local";
    tab = renderSetup(v, body, { tab, onTab: (t) => { tab = t; }, scroller: body });
  } catch (e) {
    if (!body.childElementCount) body.textContent = `Could not load the setup: ${e.message}`;
  } finally {
    inflight = false;
  }
}

function stop() {
  if (timer) clearInterval(timer);
  timer = null;
}

export function openSetup() {
  hooks.open();
  refresh();
  stop();
  timer = setInterval(refresh, POLL_MS);
}

export function closeSetup() {
  stop();
  closeLayer($("#setup-drawer"));
}

export function initSetup(h) {
  Object.assign(hooks, h);
  $("#setup-badge").onclick = () => ($("#setup-drawer").hidden ? openSetup() : closeSetup());
  $("#setup-close").onclick = closeSetup;
  // "synced 2m" ages without a status event.
  setInterval(() => applySetupStatus(hooks.status()), 30000);
}
