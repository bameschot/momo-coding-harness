// The plan drawer's timeline: every plan type as a vertical rail of steps (a
// stories plan's phases, with their stories on a branch; a TDD step's expected
// outcomes on its branch).  Nodes fold; the node being worked on is marked and
// followed; a wide view puts the selected node's details in a pane beside the
// rail.  Data: /api/state's `plan_view` (StoryRunnerMixin.plan_view).
//
// The drawer refreshes while a plan runs, so nothing here is rebuilt in place:
// a refresh that changes nothing is skipped, and one that does is patched into
// the existing elements (morph), so hover states, open sections and scroll
// stay put — no flicker.  Events are handled once, on the host (data-act).

import { el, loadJSON, saveJSON } from "./dom.js";
import { addCopyButtons, renderMarkdown } from "./markdown.js";

const DOT = { pending: "○", in_progress: "◉", done: "●", failed: "✗", replaced: "⊘", skipped: "⊘" };
// What the user folded, picked and toggled, kept across refreshes.
const ui = { open: new Map(), follow: true, wide: loadJSON("momo.planWide", false),
             selected: null, focus: null, timer: null, scrolling: false, hover: null,
             view: null, opts: {}, host: null, last: "", dirty: false };

// The node a transcript line starts: "▶ P1.S3 — …" (a story), "▶ Plan step 2/5: …"
// or "▶ Phase 2/5: …" (a step), else null.
export function lineKeyOf(text) {
  const m = /^▶ (?:(P\d+\.S\d+) — |Plan step (\d+)\/\d+: |Phase (\d+)\/\d+: )/.exec(text || "");
  return !m ? null : m[1] || `P${m[2] || m[3]}`;
}

// Open (and in the wide view, select) a node on the next render.
export function focusNode(key) {
  ui.open.set(key.split(".")[0], true);
  ui.open.set(key, true);
  ui.selected = key;
  ui.focus = key;
  ui.dirty = true;
}

export function stopPlanTimer() {
  clearInterval(ui.timer);
  ui.timer = null;
}

export const isWide = () => ui.wide;

export function setWide(on) {
  ui.wide = on;
  saveJSON("momo.planWide", on);
  ui.dirty = true;
}

const isOpen = (key, dflt) => (ui.open.has(key) ? ui.open.get(key) : dflt);

// Patch `to`'s children into `from`, keeping every element whose tag and
// data-key still match: only changed text and attributes are touched.
function morph(from, to) {
  for (const a of [...from.attributes]) if (!to.hasAttribute(a.name)) from.removeAttribute(a.name);
  for (const a of to.attributes) if (from.getAttribute(a.name) !== a.value) from.setAttribute(a.name, a.value);
  morphChildren(from, to);
}

function morphChildren(from, to) {
  const olds = [...from.childNodes], news = [...to.childNodes];
  news.forEach((n, i) => {
    const o = olds[i];
    if (!o) return from.append(n);
    const same = o.nodeType === n.nodeType && o.nodeName === n.nodeName
      && (o.nodeType !== 1 || o.dataset.key === n.dataset.key);
    if (!same) return from.replaceChild(n, o);
    if (o.nodeType === 1) morph(o, n);
    else if (o.nodeValue !== n.nodeValue) o.nodeValue = n.nodeValue;
  });
  for (let i = news.length; i < olds.length; i++) olds[i].remove();
}

function rerender() {
  ui.dirty = true;
  renderPlanView(ui.view, ui.host, ui.opts);
}

// One set of listeners per host: they read the current view from `ui`, so the
// elements a morph keeps never hold stale handlers.
function wire(host) {
  if (host._planWired) return;
  host._planWired = true;
  host.addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]");
    if (!act || !host.contains(act)) return;
    const key = act.closest("[data-key]")?.dataset.key;
    switch (act.dataset.act) {
      case "toggle": ui.open.set(key, act.getAttribute("aria-expanded") !== "true"); break;
      case "select": ui.selected = key; break;
      case "jump": e.stopPropagation(); ui.opts.onJump?.(key); return;
      case "follow": ui.follow = !ui.follow; break;
      case "expand": setAll(true); break;
      case "collapse": setAll(false); break;
      case "wide": setWide(!ui.wide); break;
      case "file": ui.opts.onPlanFile?.(); return;
      default: return;
    }
    rerender();
  });
  // <details> toggles do not bubble: catch them on the way down.
  host.addEventListener("toggle", (e) => {
    const d = e.target;
    if (d.classList?.contains("tl-section") && d.dataset.key) ui.open.set(d.dataset.key, d.open);
  }, true);
  host.addEventListener("mouseover", (e) => {
    const key = e.target.closest?.(".tl-sub[data-key]")?.dataset.key || null;
    if (key !== ui.hover) {
      ui.hover = key;
      applyHighlight(host);
    }
  });
  host.addEventListener("mouseleave", () => { ui.hover = null; applyHighlight(host); });
  host.addEventListener("scroll", () => {
    if (!ui.scrolling && ui.follow) {
      ui.follow = false;
      host.querySelector(".tl-follow")?.classList.remove("active");
    }
  });
}

function setAll(open) {
  for (const step of ui.view.steps) {
    ui.open.set(`P${step.n}`, open);
    for (const s of step.stories) ui.open.set(`P${step.n}.${s.id}`, open);
  }
}

function duration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60), h = Math.floor(m / 60);
  return h ? `${h}h ${m % 60}m` : `${m}m ${sec % 60}s`;
}

// "took 4m 2s", or the running time when it has not finished.
function timeText(item, running) {
  if (!item.started_at) return "";
  const start = Date.parse(item.started_at);
  const end = running || !item.finished_at ? Date.now() : Date.parse(item.finished_at);
  if (Number.isNaN(start) || Number.isNaN(end)) return "";
  return (running ? "" : "took ") + duration((end - start) / 1000);
}

function checkSummary(criteria) {
  const r = criteria.map((c) => c.result).filter(Boolean);
  if (!r.length) return "";
  const ok = r.filter((x) => x.passed === true).length, bad = r.filter((x) => x.passed === false).length;
  return `✓${ok}${bad ? ` ✗${bad}` : ""}`;
}

// Dependencies first, then by number: the order the harness runs stories in.
function runOrder(stories) {
  const byId = new Map(stories.map((s) => [s.id, s]));
  const num = (s) => Number(s.id.slice(1));
  const out = [], seen = new Set();
  const visit = (s) => {
    if (seen.has(s.id)) return;
    seen.add(s.id);
    for (const d of s.depends_on) if (byId.has(d)) visit(byId.get(d));
    out.push(s);
  };
  [...stories].sort((a, b) => num(a) - num(b)).forEach(visit);
  return out;
}
export function renderPlanView(view, host, opts = {}) {
  ui.view = view;
  ui.opts = opts;
  ui.host = host;
  wire(host);
  // A refresh that changes nothing does nothing (the drawer refreshes a lot).
  const same = JSON.stringify(view);
  if (same === ui.last && !ui.dirty && host.querySelector(".tl")) return;
  ui.last = same;
  ui.dirty = false;
  const cur = view.current || {};
  const curKey = cur.story ? `P${cur.step}.${cur.story}` : cur.step ? `P${cur.step}` : null;
  if (ui.wide && !ui.selected) ui.selected = curKey || "P1";
  host.closest("#plan-drawer")?.classList.toggle("plan-wide", ui.wide);
  const next = el("div");
  next.append(toolbar());
  const rail = el("ol", "tl");
  let after = false;
  for (const step of view.steps) {
    const isCur = cur.step === step.n;
    rail.append(stepNode(view, step, isCur, after));
    if (isCur) after = true;
  }
  if (ui.wide) {
    const grid = el("div", "tl-grid");
    const pane = el("div", "tl-pane");
    pane.append(...paneFor(view, ui.selected));
    grid.append(rail, pane);
    next.append(grid);
  } else {
    next.append(rail);
  }
  addCopyButtons(next);
  const top = host.scrollTop;
  morphChildren(host, next);
  host.scrollTop = top;
  applyHighlight(host);
  follow(host, curKey);
  if (curKey && !ui.timer) ui.timer = setInterval(() => tick(host), 1000);
  else if (!curKey) stopPlanTimer();
}

function toolbar() {
  const bar = el("div", "tl-toolbar");
  const btn = (label, title, act, cls = "") => {
    const b = el("button", `chip button ${cls}`.trim(), label);
    b.type = "button";
    b.title = title;
    b.dataset.act = act;
    bar.append(b);
  };
  btn("Follow", "Keep the step being worked on in view", "follow", `tl-follow${ui.follow ? " active" : ""}`);
  btn("Expand all", "Open every step and story", "expand");
  btn("Collapse all", "Fold every step and story", "collapse");
  bar.append(el("span", "tl-spacer"));
  btn(ui.wide ? "⤡ Narrow" : "⤢ Wide", ui.wide ? "Back to the drawer" : "A wide view with a details pane", "wide");
  if (ui.opts.onPlanFile) btn("Plan file", "Show the plan's markdown", "file");
  return bar;
}

function head(open) {
  const h = el("button", "tl-head");
  h.type = "button";
  h.dataset.act = ui.wide ? "select" : "toggle";
  h.setAttribute("aria-expanded", String(open));
  return h;
}

function stepNode(view, step, isCur, after) {
  const key = `P${step.n}`;
  const stories = view.kind === "stories";
  const li = el("li", `tl-node s-${step.status}${isCur ? " is-current" : ""}${after ? " after-current" : ""}`
    + (ui.wide && ui.selected === key ? " selected" : ""));
  li.dataset.key = key;
  if (isCur && step.started_at && !step.finished_at) li.dataset.start = step.started_at;
  const open = !ui.wide && isOpen(key, isCur || (step.status === "in_progress" && !view.current));
  const h = head(open);
  h.append(el("span", "tl-dot", DOT[isCur ? "in_progress" : step.status] || "○"),
           el("span", "tl-title", `${stories ? "Phase" : "Step"} ${step.n} · ${step.title}`));
  if (view.kind === "tdd" && step.stage !== "impl") h.append(el("span", "tl-tag", step.stage));
  const meta = el("span", "tl-meta");
  if (step.progress) {
    const [done, total] = step.progress.split("/").map(Number);
    const bar = el("span", "tl-bar");
    const fill = el("span", "tl-fill");
    fill.style.width = total ? `${Math.round((100 * done) / total)}%` : "0%";
    bar.append(fill);
    meta.append(el("span", "", step.progress), bar);
  }
  if (step.writing) meta.append(el("span", "tl-live", `writing stories · turn ${view.current.turns}`));
  else if (isCur && !stories && step.turns) meta.append(el("span", "tl-live", `turn ${step.turns}`));
  const t = timeText(step, isCur);
  if (t) meta.append(el("span", "tl-time", t));
  if (step.status === "pending" && !step.started_at) meta.append(el("span", "", "pending"));
  h.append(meta, el("span", "tl-caret", ui.wide ? "" : open ? "▾" : "▸"));
  li.append(h);
  if (open) li.append(...stepSections(view, step));
  if (stories && step.stories.length && (ui.wide || open || isCur)) {
    const branch = el("ol", "tl-branch");
    for (const s of runOrder(step.stories)) branch.append(storyNode(view, step, s));
    li.append(branch);
  } else if (view.kind === "tdd" && step.tests.length && open) {
    const branch = el("ol", "tl-branch");
    for (const e of step.tests) branch.append(el("li", "tl-sub tl-expect", `Expect: ${e}`));
    li.append(branch);
  }
  return li;
}

function storyNode(view, step, s) {
  const key = `P${step.n}.${s.id}`;
  const cur = view.current || {};
  const running = cur.step === step.n && cur.story === s.id;
  const li = el("li", `tl-sub s-${s.status}${running ? " is-current" : ""}`
    + (ui.wide && ui.selected === key ? " selected" : ""));
  li.dataset.key = key;
  if (running && s.started_at) li.dataset.start = s.started_at;
  const open = !ui.wide && isOpen(key, running);
  const h = head(open);
  h.append(el("span", "tl-dot", DOT[running ? "in_progress" : s.status] || "○"),
           el("span", "tl-id", s.id), el("span", "tl-title", s.title));
  if (s.depends_on.length) h.append(el("span", "tl-deps", `← ${s.depends_on.join(" ")}`));
  const meta = el("span", "tl-meta");
  const live = [];
  if (running || s.turns) live.push(`turn ${s.turns}/${view.cap}`);
  if (s.attempts) live.push(`attempt ${s.attempts + (s.status === "failed" ? 0 : 1)}`);
  if (s.refusals) live.push(`${s.refusals} refusal${s.refusals === 1 ? "" : "s"}`);
  const checks = checkSummary(s.criteria);
  if (checks) live.push(checks);
  if (live.length) meta.append(el("span", running ? "tl-live" : "", live.join(" · ")));
  const t = timeText(s, running);
  if (t) meta.append(el("span", "tl-time", t));
  h.append(meta);
  if (s.started_at && ui.opts.onJump) {
    const jump = el("span", "tl-jump", "↧");
    jump.title = "Show where this story's run starts in the transcript";
    jump.dataset.act = "jump";
    h.append(jump);
  }
  h.append(el("span", "tl-caret", ui.wide ? "" : open ? "▾" : "▸"));
  li.append(h);
  if (open) li.append(...storySections(key, s));
  return li;
}
// One foldable part of a node's details; its key keeps the fold state.
function section(key, title, dflt, fill) {
  const box = el("details", "tl-section");
  box.dataset.key = key;
  if (isOpen(key, dflt)) box.setAttribute("open", "");
  box.append(el("summary", "", title));
  fill(box);
  return box;
}

const md = (text) => {
  const d = el("div", "md");
  d.innerHTML = renderMarkdown(text);
  return d;
};
const list = (items) => {
  const ul = el("ul", "tl-list");
  for (const x of items) ul.append(el("li", "", x));
  return ul;
};
const cmd = (text) => {
  const pre = el("pre", "tl-cmd");
  pre.append(el("code", "", text));
  return pre;
};

function stepSections(view, step) {
  const key = `P${step.n}`;
  const out = [];
  if (step.details) out.push(section(`${key}:details`, "Details", !step.stories.length, (b) => b.append(md(step.details))));
  if (step.outcome) out.push(section(`${key}:outcome`, "Outcome", true, (b) => b.append(el("div", "", step.outcome))));
  if (step.checks.length) {
    const g = step.gate || {};
    const title = g.passed === undefined ? "Checks" : `Checks ${g.passed ? "✓ pass" : "✗ fail"}`;
    out.push(section(`${key}:checks`, title, g.passed === false, (b) => {
      for (const c of step.checks) b.append(cmd(c));
      if (g.passed === false && g.output) b.append(el("pre", "tl-output", g.output));
    }));
  }
  if (step.report) out.push(section(`${key}:report`, "Report", false, (b) => b.append(md(step.report))));
  if (step.note && step.status === "done") out.push(el("div", "tl-note", step.note));
  if (step.files.length) out.push(el("div", "tl-files", `Files: ${step.files.join(", ")}`));
  return out;
}

function storySections(key, s) {
  const out = [];
  const failing = s.criteria.some((c) => c.result && c.result.passed === false);
  out.push(section(`${key}:acceptance`, `Acceptance ${checkSummary(s.criteria)}`.trim(), true, (b) => {
    const ul = el("ul", "tl-criteria");
    for (const c of s.criteria) {
      const r = c.result;
      const state = !r || r.passed === null ? "" : r.passed ? "ok" : "bad";
      const li = el("li", state);
      li.append(el("span", "tl-mark", state === "ok" ? "✓" : state === "bad" ? "✗" : "–"),
                el("span", "", c.text));
      if (c.check) li.append(cmd(c.check));
      if (r && r.output && r.passed === false) li.append(el("pre", "tl-output", r.output));
      else if (r && r.output && !c.check) li.append(el("div", "tl-note", `evidence: ${r.output}`));
      ul.append(li);
    }
    b.append(ul);
  }));
  if (s.description) out.push(section(`${key}:description`, "Description", !failing, (b) => b.append(md(s.description))));
  if (s.technical_details) out.push(section(`${key}:technical`, "Technical details", false, (b) => b.append(md(s.technical_details))));
  const notes = [["Hints", s.hints], ["Pitfalls", s.pitfalls], ["Provides", s.provides]].filter(([, v]) => v.length);
  if (notes.length) out.push(section(`${key}:notes`, notes.map(([n]) => n).join(" · "), false, (b) => {
    for (const [name, items] of notes) b.append(el("div", "tl-label", name), list(items));
  }));
  const f = s.facts || {};
  if (s.delivered || (f.files || []).length) {
    out.push(section(`${key}:delivered`, "Delivered", false, (b) => {
      if (s.delivered) b.append(md(s.delivered));
      if ((f.files || []).length) b.append(el("div", "tl-files", `Files: ${f.files.join(", ")}`));
      if ((f.definitions || []).length) b.append(el("div", "tl-files", `Definitions: ${f.definitions.join(", ")}`));
    }));
  } else if (s.files.length) {
    out.push(el("div", "tl-files", `Files: ${s.files.join(", ")}`));
  }
  return out;
}

// The wide view's right-hand pane: the selected step's or story's details.
function paneFor(view, key) {
  const [stepKey, storyId] = (key || "").split(".");
  const step = view.steps.find((s) => `P${s.n}` === stepKey);
  if (!step) return [el("div", "muted", "Pick a step or story on the left.")];
  const story = storyId ? step.stories.find((s) => s.id === storyId) : null;
  const item = story || step;
  const title = story ? `${story.id} · ${story.title}`
    : `${view.kind === "stories" ? "Phase" : "Step"} ${step.n} · ${step.title}`;
  const top = el("div", "tl-pane-head");
  top.append(el("span", `tl-dot s-${item.status}`, DOT[item.status] || "○"), el("strong", "", title));
  return [top, ...(story ? storySections(key, story) : stepSections(view, step))];
}

// Keep the node being worked on (or one opened from the transcript) in view.
function follow(host, curKey) {
  const key = ui.focus || (ui.follow ? curKey : null);
  ui.focus = null;
  const node = key && host.querySelector(`[data-key="${key}"] > .tl-head`);
  if (!node) return;
  ui.scrolling = true;
  node.scrollIntoView({ block: "nearest" });
  requestAnimationFrame(() => { ui.scrolling = false; });
}

// The running node's time, every second, by changing only that text.
function tick(host) {
  if (!host.isConnected || host.closest("[hidden]")) return stopPlanTimer();
  for (const li of host.querySelectorAll(".is-current[data-start]")) {
    const t = li.querySelector(":scope > .tl-head .tl-time");
    const text = timeText({ started_at: li.dataset.start }, true);
    if (t && t.textContent !== text) t.textContent = text;
  }
}

// Mark what the story under the mouse needs (dep-of) and what waits on it
// (dep-by).  Classes change only when the hovered story does.
function applyHighlight(host) {
  const want = new Map();
  const [stepKey, id] = (ui.hover || "").split(".");
  const step = ui.view?.steps.find((s) => `P${s.n}` === stepKey);
  const s = step && id ? step.stories.find((x) => x.id === id) : null;
  if (s) {
    const full = (d) => (d.startsWith("P") ? d : `P${step.n}.${d}`);
    want.set(ui.hover, "dep-self");
    for (const d of s.depends_on) want.set(full(d), "dep-of");
    for (const d of s.dependents || []) want.set(full(d), "dep-by");
  }
  for (const li of host.querySelectorAll(".tl-sub[data-key]")) {
    const cls = want.get(li.dataset.key) || null;
    for (const c of ["dep-self", "dep-of", "dep-by"]) {
      if ((c === cls) !== li.classList.contains(c)) li.classList.toggle(c, c === cls);
    }
  }
}
