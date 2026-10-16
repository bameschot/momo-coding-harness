// The command palette (Ctrl/Cmd+K): every /command from /help plus the UI's own
// actions (view toggles, drawers, theme, settings sections), filtered as you
// type.  `?` opens it on the keyboard-shortcut list.

import { $, el } from "./dom.js";

// What app.js supplies.
const hooks = {
  commands: () => [],    // [{cmd, usage, desc}] from /api/state
  actions: () => [],     // [{label, desc, keys?, run}]
  run: () => {},         // send a command that takes no argument
  fill: () => {},        // put a command that needs one into the input box
};

export const SHORTCUTS = [
  ["Enter", "Send (queues while momo is working)"],
  ["Shift+Enter", "New line"],
  ["↑ ↓", "Earlier messages (at the start / end of the box)"],
  ["Esc", "Interrupt momo · close a menu or panel"],
  ["Shift+Tab", "Next mode"],
  ["/", "Commands, in the input box"],
  ["@", "Insert a workspace path, in the input box"],
  ["Ctrl+K  ⌘K", "This palette"],
  ["Ctrl+F  ⌘F", "Find in the conversation (twice: the browser's find)"],
  ["Alt+↑  Alt+↓", "Previous / next message you sent"],
  ["?", "These shortcuts"],
  ["Shift+T", "Thinking shown / hidden"],
  ["Shift+M", "Markdown on / off"],
  ["Shift+D", "Edit diffs shown / hidden"],
  ["Shift+Q", "Companion shown / hidden"],
  ["Shift+P", "Ask before each command"],
  ["Shift+E  Shift+W", "Expand / collapse all tool calls"],
  ["Shift+C", "Interrupt momo"],
];

let rows = [];          // the selectable rows, in order: {node, run}
let sel = 0;
let opener = null;

const takesArg = (usage) => usage.includes(" ") && !usage.includes(" | ");

function matches(text, terms) {
  const t = text.toLowerCase();
  return terms.every((w) => t.includes(w));
}

function section(list, title) { list.append(el("div", "p-sec", title)); }

function item(list, label, desc, run, keys = "") {
  const node = el("div", "p-item");
  node.setAttribute("role", "option");
  node.append(el("span", "p-label", label), el("span", "p-desc", desc || ""));
  if (keys) node.append(el("kbd", "p-keys", keys));
  const idx = rows.length;
  node.onmousemove = () => select(idx);
  node.onmousedown = (e) => e.preventDefault();               // keep the focus in the field
  // On click, and not further: the document listener would close a popover
  // the chosen action just opened.
  node.onclick = (e) => { e.stopPropagation(); choose(idx); };
  rows.push({ node, run });
  list.append(node);
}

function select(i, scroll = true) {
  if (!rows.length) return;
  sel = (i + rows.length) % rows.length;
  rows.forEach((r, k) => r.node.classList.toggle("sel", k === sel));
  if (scroll) rows[sel].node.scrollIntoView({ block: "nearest" });
}

function choose(i) {
  const r = rows[i];
  if (!r) return;
  closePalette(false);
  r.run();
}

function render(query, shortcutsFirst) {
  const list = $("#palette-list");
  list.replaceChildren();
  rows = [];
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (shortcutsFirst && !terms.length) {
    section(list, "Keyboard shortcuts");
    for (const [keys, what] of SHORTCUTS) {
      const r = el("div", "p-item static");
      r.append(el("kbd", "p-label", keys), el("span", "p-desc", what));
      list.append(r);
    }
  }
  const actions = hooks.actions().filter((a) => matches(`${a.label} ${a.desc || ""}`, terms));
  if (actions.length) {
    section(list, "Actions");
    for (const a of actions) item(list, a.label, a.desc, a.run, a.keys);
  }
  const seen = new Set();
  const cmds = hooks.commands().filter((c) => matches(`${c.usage} ${c.desc}`, terms) && !seen.has(c.usage) && seen.add(c.usage));
  if (cmds.length) {
    section(list, "Commands");
    for (const c of cmds) {
      item(list, c.usage, c.desc, () => (takesArg(c.usage) ? hooks.fill(`${c.cmd} `) : hooks.run(c.cmd)));
    }
  }
  if (!rows.length && !(shortcutsFirst && !terms.length)) list.append(el("div", "p-sec", "Nothing matches"));
  select(0, false);          // the top stays in view (the shortcut list, when asked for)
  list.scrollTop = 0;
}

export function openPalette({ query = "", shortcuts = false } = {}) {
  const back = $("#palette-backdrop");
  if (!back.hidden) return closePalette();
  opener = document.activeElement;
  back.hidden = false;
  const inp = $("#palette-input");
  inp.value = query;
  inp.dataset.shortcuts = shortcuts ? "1" : "";
  render(query, shortcuts);
  inp.focus();
}

export function closePalette(restoreFocus = true) {
  $("#palette-backdrop").hidden = true;
  if (restoreFocus && opener?.isConnected) opener.focus();
}

export function initPalette(h) {
  Object.assign(hooks, h);
  const inp = $("#palette-input");
  inp.oninput = () => render(inp.value, inp.dataset.shortcuts === "1");
  inp.onkeydown = (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); select(sel + (e.key === "ArrowDown" ? 1 : -1)); }
    else if (e.key === "Enter") { e.preventDefault(); choose(sel); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closePalette(); }
  };
  // On click (not mousedown): the click must not land on whatever is under the backdrop.
  $("#palette-backdrop").onclick = (e) => { if (e.target.id === "palette-backdrop") { e.stopPropagation(); closePalette(); } };
}
