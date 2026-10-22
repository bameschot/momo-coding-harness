// The status chips' popovers (RUN, NET, TOOLS-off).  Every chip opens a popover
// — none toggles on a bare click — with its main controls, a line on what the
// setting does, and a way to the rest of its settings.  THINK and INDEX have
// their own popovers in app.js.  Rendered from the last status event, and again
// on every status event while open.

import { $, el, formatSize, icon } from "./dom.js";

// What app.js supplies.
const hooks = {
  send: async () => false,       // send a line as if typed
  status: () => ({}),            // the last status event
  openSettings: () => {},        // open the settings drawer at a section
  openToolsMenu: () => {},       // the per-mode tool picker
  closeMenu: () => {},
};

function check(label, checked, onChange, disabled = false) {
  const lab = el("label");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = checked;
  cb.disabled = disabled;
  cb.onchange = () => onChange(cb.checked);
  lab.append(cb, ` ${label}`);
  return lab;
}

function radio(name, value, label, checked, onChange) {
  const lab = el("label");
  const r = el("input");
  r.type = "radio";
  r.name = name;
  r.value = value;
  r.checked = checked;
  r.onchange = () => r.checked && onChange(value);
  lab.append(r, ` ${label}`);
  return lab;
}

function foot(...buttons) {
  const f = el("div", "menu-foot");
  f.append(...buttons);
  return f;
}

function link(label, onClick) {
  const b = el("button", "link-btn", label);
  b.type = "button";
  b.onclick = onClick;
  return b;
}

function settingsLink(section, label = "More settings…") {
  return link(label, () => { hooks.closeMenu(); hooks.openSettings(section); });
}

function renderRun(menu) {
  const s = hooks.status();
  menu.replaceChildren(
    el("div", "menu-title", "Running commands"),
    check("Ask before each command", !!s.run_confirm,
          (on) => hooks.send(`/run-confirm ${on ? "on" : "off"}`)),
    check("Save output to a log, return a view", s.run_mode !== "classic",
          (on) => hooks.send(`/run-mode ${on ? "new" : "classic"}`)),
    el("div", "note", s.run_confirm
      ? "momo asks you (y/N) before every shell command."
      : "Shell commands run without asking. Shift+P switches."),
    foot(settingsLink("run")));
}

function renderNet(menu) {
  const s = hooks.status();
  const access = s.net_access || "off";
  const set = (v) => hooks.send(`/net ${v}`);
  const notes = {
    off: "The model can't reach the internet (curl and wget are refused too).",
    on: "fetch_url and web_search reach public sites; local and private addresses stay blocked.",
    local: "Also localhost and the LAN — including this momo's own web UI. Writes there always ask.",
  };
  menu.replaceChildren(
    el("div", "menu-title", "Internet access"),
    radio("chip-net", "off", "Off", access === "off", set),
    radio("chip-net", "on", "Public internet", access === "on", set),
    radio("chip-net", "local", "Also localhost and LAN", access === "local", set),
    check("Ask before POST / PUT / PATCH / DELETE", !!s.net_confirm,
          (on) => hooks.send(`/net-confirm ${on ? "on" : "off"}`), access === "off"),
    el("div", "note", notes[access] || ""),
    ...renderPageCache(s),
    foot(settingsLink("network", "Download limits…")));
}

function ago(epochSeconds) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - epochSeconds));
  return s < 60 ? `${s}s ago` : `${Math.floor(s / 60)}m ago`;
}

// The pages fetch_url keeps for find=/offset=/links= follow-ups (/net-cache).
// Shown whatever the access level: a cache filled before /net off is still there.
function renderPageCache(s) {
  const pages = s.net_cache || [];
  const max = s.net_cache_max || 64;
  const bytes = pages.reduce((n, p) => n + p.bytes, 0);
  const title = el("div", "menu-sub cache-head", "Page cache");
  if (pages.length) {
    title.append(el("span", "muted",
      `${pages.length} of ${max} pages · ${formatSize(bytes)} of ${formatSize(s.net_cache_max_bytes || 0)}`));
  }
  if (!pages.length) {
    return [title, el("div", "note", `No pages cached. fetch_url and web_search keep the last ${max} pages `
      + `(up to ${formatSize(s.net_cache_max_bytes || 0)}), so find=, offset= and links= don't download again.`)];
  }
  const list = el("ol", "cache-list");
  pages.forEach((p, i) => {
    const row = el("li", "cache-row");
    const text = el("div", "cache-text");
    const url = el("div", "cache-url", p.url.replace(/^https?:\/\//, ""));
    url.title = p.final_url && p.final_url !== p.url ? `${p.url}\n→ ${p.final_url}` : p.url;
    const meta = [p.kind, formatSize(p.bytes), ago(p.fetched), `${p.hits} hit${p.hits === 1 ? "" : "s"}`];
    if (p.via === "web_search") meta.push("via search");
    if (p.local) meta.push("local");
    text.append(url, el("div", "cache-meta muted", meta.join(" · ")));
    const drop = el("button", "icon-btn sm");
    drop.type = "button";
    drop.title = "Remove from the cache";
    drop.setAttribute("aria-label", `Remove ${p.url} from the cache`);
    drop.append(icon("x", "icon sm"));
    drop.onclick = () => hooks.send(`/net-cache drop ${i + 1}`);
    row.append(text, drop);
    list.append(row);
  });
  const clear = el("button", "btn small", "Clear cache");
  clear.type = "button";
  clear.onclick = () => hooks.send("/net-cache clear");
  return [title, ...cacheBar(pages, max), list, clear];
}

const CACHE_KINDS = ["html", "json", "markdown", "text"];
const kindClass = (kind) => `cache-k-${CACHE_KINDS.includes(kind) ? kind : "other"}`;

// One segment per cached page out of the cache's slots, coloured by kind (the
// CTX/INDEX popovers' bar).  Pages never expire with time; the least recently
// used is pushed out first when the cache is full, so a page fades the closer
// it is to that end.  Then a legend.
function cacheBar(pages, max) {
  const bar = el("div", "ctx-stack");
  pages.forEach((p, i) => {
    const seg = el("span", `ctx-seg ${kindClass(p.kind)}`);
    seg.style.width = `${100 / max}%`;
    seg.style.opacity = (1 - 0.65 * (i / Math.max(1, max - 1))).toFixed(2);
    seg.title = `${p.url} · ${p.kind} · ${formatSize(p.bytes)} · ${ago(p.fetched)}`;
    bar.append(seg);
  });
  const groups = new Map();
  for (const p of pages) {
    const g = groups.get(p.kind) || { n: 0, bytes: 0 };
    g.n += 1;
    g.bytes += p.bytes;
    groups.set(p.kind, g);
  }
  const rows = el("div", "ctx-rows");
  for (const [kind, g] of groups) {
    const row = el("div", "ctx-row");
    row.append(el("span", `ctx-swatch ${kindClass(kind)}`), el("span", "ctx-name", kind),
               el("span", "ctx-tok mono", `${g.n} page${g.n === 1 ? "" : "s"}`),
               el("span", "ctx-pc mono", formatSize(g.bytes)));
    rows.append(row);
  }
  return [bar, rows];
}

function renderToolsOff(menu) {
  const on = el("button", "btn primary small", "Turn tool calls on");
  on.type = "button";
  on.onclick = () => { hooks.closeMenu(); hooks.send("/tools on"); };
  menu.replaceChildren(
    el("div", "menu-title", "Tool calls are off"),
    el("div", "note", "The model can only answer in text: it can't read files, edit or run anything."),
    on,
    foot(link("Choose tools for this mode…", () => { hooks.closeMenu(); hooks.openToolsMenu(); })));
}

export const CHIP_MENUS = {
  "#run-menu": renderRun,
  "#net-menu": renderNet,
  "#tools-off-menu": renderToolsOff,
};

export function renderChipMenu(id) {
  CHIP_MENUS[id]?.($(id));
}

// A status event arrived: repaint whichever chip popover is open.
export function refreshChipMenus() {
  for (const [id, render] of Object.entries(CHIP_MENUS)) if (!$(id).hidden) render($(id));
}

export function initChips(h) { Object.assign(hooks, h); }
