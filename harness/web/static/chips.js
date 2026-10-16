// The status chips' popovers (RUN, NET, TOOLS-off).  Every chip opens a popover
// — none toggles on a bare click — with its main controls, a line on what the
// setting does, and a way to the rest of its settings.  THINK and INDEX have
// their own popovers in app.js.  Rendered from the last status event, and again
// on every status event while open.

import { $, el } from "./dom.js";

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
    foot(settingsLink("network", "Download limits…")));
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
