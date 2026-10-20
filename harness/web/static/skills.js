// Skills: the header's book button and its popover.  Every skill in skills/ with
// a checkbox (/load-skill, /unload-skill), its title, what it covers and what it
// costs in the system prompt.  Active skills come first, in prompt order.

import { $, el } from "./dom.js";

// What app.js supplies.
const hooks = {
  send: async () => false,       // send a line as if typed
  status: () => ({}),            // the last status event
  closeMenu: () => {},
  openPrompt: () => {},          // the System prompt drawer
  changed: () => {},             // a skill was (un)loaded: refresh other views of the list
};

let skills = [];                 // the last api/skills answer
let filter = "";
let lastSig = "";

const fmtTokens = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));

export function applySkillsStatus(s) {
  const active = s.skills || [];
  const count = $("#skills-count");
  count.hidden = !active.length;
  count.textContent = String(active.length);
  const label = active.length ? `Skills: ${active.join(", ")}` : "Skills: none active";
  $("#skills-btn").title = label;
  $("#skills-btn").setAttribute("aria-label", label);
  const sig = active.join(",");
  if (sig === lastSig) return;
  lastSig = sig;
  if (!$("#skills-menu").hidden) refreshSkillsMenu();
}

function ordered() {
  const active = hooks.status().skills || [];
  const on = active.map((n) => skills.find((s) => s.name === n)).filter(Boolean);
  return [...on, ...skills.filter((s) => !active.includes(s.name))];
}

function row(s) {
  const lab = el("label", "skill-row");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = s.active;
  cb.onchange = async () => {
    cb.disabled = true;
    await hooks.send(`/${cb.checked ? "load" : "unload"}-skill ${s.name}`);
    hooks.changed();
    refreshSkillsMenu();          // also re-enables the box if the command was refused
  };
  const text = el("span", "skill-text");
  const head = el("span", "skill-head");
  head.append(el("span", "skill-title", s.title), el("span", "skill-name mono", s.name));
  text.append(head);
  if (s.summary) text.append(el("span", "skill-summary", s.summary));
  const cost = el("span", "skill-cost mono", `~${fmtTokens(s.tokens)}`);
  cost.title = `About ${s.tokens.toLocaleString()} tokens in the system prompt while active`;
  lab.append(cb, text, cost);
  return lab;
}

function renderList(menu) {
  const box = menu.querySelector(".skills-box");
  const q = filter.trim().toLowerCase();
  const shown = ordered().filter((s) => !q || `${s.name} ${s.title} ${s.summary}`.toLowerCase().includes(q));
  const active = shown.filter((s) => s.active);
  const rest = shown.filter((s) => !s.active);
  const kids = [];
  if (active.length) kids.push(el("div", "menu-sub", "Active"), ...active.map(row));
  if (rest.length) kids.push(el("div", "menu-sub", active.length ? "Available" : "Skills"), ...rest.map(row));
  if (!shown.length) kids.push(el("div", "muted small skills-none", q ? `No skill matches “${filter}”.` : "No skills found in skills/."));
  box.replaceChildren(...kids);
  const on = skills.filter((s) => s.active);
  const total = on.reduce((n, s) => n + s.tokens, 0);
  menu.querySelector(".skills-meta").textContent = on.length
    ? `${on.length} active · ~${fmtTokens(total)} tokens` : `${skills.length} available`;
  menu.querySelector(".skills-prompt").hidden = !on.length;
}

function skeleton(menu) {
  filter = "";                    // each opening starts unfiltered
  const title = el("div", "menu-title skills-title");
  title.append(el("span", "", "Skills"), el("span", "muted small skills-meta"));
  const search = el("input", "skills-filter");
  search.type = "search";
  search.placeholder = "Filter skills";
  search.setAttribute("aria-label", "Filter skills");
  search.value = filter;
  search.oninput = () => { filter = search.value; renderList(menu); };
  const foot = el("div", "menu-foot");
  const prompt = el("button", "link-btn skills-prompt", "Show in the system prompt…");
  prompt.type = "button";
  prompt.onclick = () => { hooks.closeMenu(); hooks.openPrompt(); };
  foot.append(el("span", "muted small", "Skills add their rules to the system prompt."), prompt);
  menu.replaceChildren(title, search, el("div", "skills-box"), foot);
  search.hidden = skills.length <= 8;
}

export async function refreshSkillsMenu() {
  const menu = $("#skills-menu");
  if (menu.hidden) return;
  let r;
  try {
    r = await (await fetch("api/skills")).json();
  } catch (e) {
    menu.replaceChildren(el("div", "menu-title", "Skills"), el("div", "muted", `Could not load: ${e.message}`));
    return;
  }
  if (menu.hidden) return;
  skills = r.skills || [];
  // Built once per opening, so the filter keeps its text and focus on refreshes.
  if (!menu.querySelector(".skills-box")) skeleton(menu);
  renderList(menu);
}

export function initSkills(h) { Object.assign(hooks, h); }
