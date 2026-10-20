// MCP servers: the header button and its overview popover, and Settings → MCP servers (server
// list with each server's tools and resources, add/remove).  Everything is read
// from GET api/mcp; changes go through /mcp commands (like typing them), except
// adding/removing a server, which posts the config entry as JSON.

import { $, confirmInline, el, toast } from "./dom.js";

// What app.js supplies.
const hooks = {
  send: async () => false,       // send a line as if typed
  post: async () => ({}),        // POST JSON, returns the parsed reply (throws on HTTP errors)
  status: () => ({}),            // the last status event
  openSettings: () => {},
  closeMenu: () => {},
  attach: () => {},              // add {name, text, chars} to the composer's attachments
};

const STATE_LABEL = {
  ready: "connected", connecting: "starting…", error: "failed", disabled: "off",
  "needs-approval": "needs approval", denied: "denied",
};

let view = null;              // the last api/mcp answer
let loading = null;

async function load() {
  if (!loading) {
    loading = fetch("api/mcp").then((r) => r.json()).finally(() => { loading = null; });
  }
  view = await loading;
  return view;
}

function dot(state) {
  const d = el("span", "dot");
  if (state === "ready") d.classList.add("on");
  else if (state === "connecting") d.classList.add("on", "offline");
  else if (state === "disabled" || state === "denied") d.classList.add("muted-dot");
  return d;
}

function button(label, onClick, cls = "link-btn") {
  const b = el("button", cls, label);
  b.type = "button";
  b.onclick = onClick;
  return b;
}

// The commands run, then the status event refreshes whichever view is open.
const run = (cmd) => hooks.send(cmd);

function serverActions(s) {
  const out = [];
  if (s.state === "needs-approval" || s.state === "denied") {
    out.push(button("Approve", () => run(`/mcp approve ${s.name}`)));
    if (s.state === "needs-approval") out.push(button("Deny", () => run(`/mcp deny ${s.name}`)));
    return out;
  }
  if (s.state === "disabled") {
    out.push(button("Turn on", () => run(`/mcp enable ${s.name}`)));
  } else {
    out.push(button("Restart", () => run(`/mcp restart ${s.name}`)));
    out.push(button("Turn off", () => run(`/mcp disable ${s.name}`)));
  }
  return out;
}

function detail(s) {
  if (s.state === "ready") {
    return `${s.tools} tool${s.tools === 1 ? "" : "s"}` + (s.resources ? " · resources" : "");
  }
  if (s.state === "needs-approval") return `project server — runs: ${s.target}`;
  return s.error || STATE_LABEL[s.state] || s.state;
}

// ── header button and overview ───────────────────────────────────────────────
// The plug button left of the wrench: a badge with the connected count (warn
// colour when a server failed or waits for approval), and a popover listing
// every server with its state, tools and resources.

export function applyMcpStatus(s) {
  const btn = $("#mcp-btn");
  const count = $("#mcp-count");
  const total = s.mcp_total || 0, ready = s.mcp_ready || 0;
  const off = s.mcp_enabled === false;
  const attention = !!s.mcp_attention && !off;
  count.hidden = off || (!total && !attention);
  count.textContent = attention && !ready ? "!" : String(ready);
  count.classList.toggle("warn", attention);
  btn.classList.toggle("muted-btn", off || !total);
  const failed = (s.mcp || []).filter((m) => m.state === "error" || m.state === "needs-approval");
  const label = off ? "MCP servers: off (/mcp on)"
    : !total ? "MCP servers: none configured"
    : `MCP servers: ${ready} of ${total} connected`
      + (failed.length ? ` — ${failed.map((m) => `${m.name} ${STATE_LABEL[m.state]}`).join(", ")}` : "");
  btn.title = label;
  btn.setAttribute("aria-label", label);
  const confirm = $("#mcp-confirm");
  if (confirm) confirm.checked = s.mcp_confirm !== false;
  const on = $("#mcp-on");
  if (on) on.checked = !off;
  // Re-render only when something shown changed: status events stream in
  // during a reply, and a re-render would take focus from a checkbox.
  const sig = JSON.stringify([s.mcp, s.mcp_enabled, s.mcp_confirm, s.tools_off, s.mode]);
  if (sig === lastSig) return;
  lastSig = sig;
  if (!$("#mcp-menu").hidden) scheduleRefresh(refreshMcpMenu);
  if (settingsOpen()) scheduleRefresh(renderMcpSettings);
}
let lastSig = "";

const timers = new Map();
function scheduleRefresh(fn) {
  if (timers.has(fn)) return;
  timers.set(fn, setTimeout(() => { timers.delete(fn); fn(); }, 400));
}

const collapsed = new Set();      // servers folded in the overview (all open by default)

function overviewServer(s) {
  const d = el("details", "mcp-ov-server");
  d.open = !collapsed.has(s.name);
  d.ontoggle = () => (d.open ? collapsed.delete(s.name) : collapsed.add(s.name));
  const sum = el("summary");
  const name = el("strong", "mono", s.name);
  const meta = [STATE_LABEL[s.state] || s.state, s.transport];
  if (s.server) meta.push(s.server);
  if (s.source !== "global") meta.push(s.source);
  sum.append(dot(s.state), name, el("span", "muted small", meta.join(" · ")));
  const body = el("div", "mcp-ov-body");
  if (s.state === "ready") {
    const facts = [`${s.tools} tool${s.tools === 1 ? "" : "s"}`
                   + (s.read_only ? ` (${s.read_only} read-only)` : ""),
                   `asks: ${s.confirm === "writes" ? "for writes" : s.confirm}`];
    if (s.resource_count) facts.push(`${s.resource_count} resource${s.resource_count === 1 ? "" : "s"}`);
    if (s.untrusted) facts.push("results fenced");
    body.append(el("div", "muted small", facts.join(" · ")));
    const tools = el("div", "mcp-tools");
    tools.append(...s.tool_list.map((t) => toolRow(t, s)));
    if (!s.tool_list.length) tools.append(el("span", "muted small", "No tools."));
    body.append(tools);
  } else {
    body.append(el("div", `mcp-detail small${s.state === "error" ? " err" : ""}`, detail(s)));
  }
  const acts = el("div", "mcp-actions");
  acts.append(...serverActions(s));
  body.append(acts);
  d.append(sum, body);
  return d;
}

export async function refreshMcpMenu() {
  const menu = $("#mcp-menu");
  if (menu.hidden) return;
  let v;
  try {
    v = await load();
  } catch (e) {
    menu.replaceChildren(el("div", "menu-title", "MCP servers"), el("div", "muted", `Could not load: ${e.message}`));
    return;
  }
  if (menu.hidden) return;
  const ready = v.servers.filter((s) => s.state === "ready");
  const tools = ready.reduce((n, s) => n + s.tools, 0);
  const title = el("div", "menu-title mcp-ov-title");
  title.append(el("span", "", "MCP servers"));
  if (v.enabled && v.servers.length) {
    title.append(el("span", "muted small", `${ready.length}/${v.servers.length} connected · ${tools} tools`));
  }
  const children = [title];
  if (!v.enabled) {
    children.push(el("div", "note", "MCP is off: no servers run and the model gets no MCP tools."),
                  button("Turn MCP on", () => { hooks.closeMenu(); run("/mcp on"); }, "btn primary small"));
  } else if (!v.servers.length) {
    children.push(el("div", "note", "No servers configured yet."),
                  button("Add a server…", () => { hooks.closeMenu(); hooks.openSettings("mcp"); }, "btn primary small"));
  } else {
    children.push(...v.servers.map(overviewServer));
  }
  for (const e of v.errors) children.push(el("div", "mcp-detail small err", e));
  if (v.schema_tokens) {
    const pct = Math.round((v.schema_tokens * 100) / Math.max(v.context_limit, 1));
    children.push(el("div", `small ${pct >= 25 ? "mcp-detail err" : "muted"} mcp-ov-cost`,
                     `Tool schemas: ~${v.schema_tokens.toLocaleString()} tokens (${pct}% of the context limit)`));
  }
  const foot = el("div", "menu-foot");
  if (v.enabled) foot.append(button("Reload config", () => run("/mcp reload")));
  foot.append(button("Manage servers…", () => { hooks.closeMenu(); hooks.openSettings("mcp"); }));
  children.push(foot);
  menu.replaceChildren(...children);
}

// ── settings section ─────────────────────────────────────────────────────────

const openServers = new Set();     // <details> kept open across re-renders

function settingsOpen() {
  const d = document.querySelector('#settings-drawer details[data-section="mcp"]');
  return d && d.open && !$("#settings-drawer").hidden;
}

function toolRow(t, s) {
  const st = hooks.status();
  const off = (st.tools_off || []).includes(t.name);
  const offered = t.read_only || st.mode === "coding";
  const lab = el("label", "mcp-tool");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = offered && !off;
  cb.disabled = !offered || st.mcp_enabled === false;
  cb.onchange = () => run(`/tools ${t.name} ${cb.checked ? "on" : "off"}`);
  lab.title = t.desc + (offered ? "" : "\n\nChanges things, so it is only offered in coding mode.");
  const tag = t.read_only ? "read-only" : t.destructive ? "destructive" : "writes";
  lab.append(cb, " ", el("span", "mono", t.tool), " ", el("span", `mcp-tag ${tag}`, tag));
  return lab;
}

async function showResources(box, s) {
  box.replaceChildren(el("div", "muted small", "Loading resources…"));
  let r;
  try {
    r = await (await fetch(`api/mcp/resources?server=${encodeURIComponent(s.name)}`)).json();
  } catch (e) {
    r = { error: e.message };
  }
  if (r.error) { box.replaceChildren(el("div", "mcp-detail small err", r.error)); return; }
  if (!r.resources.length) { box.replaceChildren(el("div", "muted small", "No resources.")); return; }
  box.replaceChildren(...r.resources.map((res) => {
    const row = el("div", "mcp-res");
    const name = el("span", "mono small", res.name);
    name.title = res.uri + (res.desc ? `\n${res.desc}` : "");
    const att = button("Attach", async () => {
      att.disabled = true;
      try {
        const q = `server=${encodeURIComponent(s.name)}&uri=${encodeURIComponent(res.uri)}`;
        const j = await (await fetch(`api/mcp/resource?${q}`)).json();
        if (j.error) throw new Error(j.error);
        hooks.attach({ name: j.name, text: j.text, chars: j.chars });
        toast(`Attached ${j.name} to your next message`);
      } catch (e) {
        toast(e.message, "error");
      } finally {
        att.disabled = false;
      }
    });
    row.append(name, el("span", "spacer"), att);
    return row;
  }));
}

const ASKS = { writes: "for tools that change things", always: "before every call", never: "never" };

function chevron() {
  return el("span", "mcp-chev", "›");
}

function kv(grid, key, value, title) {
  const v = typeof value === "string" ? el("span", "", value) : value;
  if (title) v.title = title;
  grid.append(el("span", "mcp-k", key), v);
}

function serverCard(s) {
  const d = el("details", "mcp-card");
  d.dataset.state = s.state;
  d.open = openServers.has(s.name);
  d.ontoggle = () => (d.open ? openServers.add(s.name) : openServers.delete(s.name));

  const sum = el("summary");
  sum.append(dot(s.state), el("strong", "mono mcp-name", s.name),
             el("span", `mcp-pill ${s.state}`, STATE_LABEL[s.state] || s.state),
             el("span", "mcp-meta", [s.transport, s.source === "global" ? "" : s.source]
               .filter(Boolean).join(" · ")),
             el("span", "spacer"));
  if (s.state === "ready") sum.append(el("span", "mcp-meta", `${s.tools} tool${s.tools === 1 ? "" : "s"}`));
  sum.append(chevron());

  const body = el("div", "mcp-card-body");
  const grid = el("div", "mcp-kv-grid");
  kv(grid, s.transport === "http" ? "URL" : "Runs", el("span", "mono mcp-ellipsis", s.target), s.target);
  kv(grid, "Config", s.source === "global" ? "global (mcp.json)" : s.source === "project" ? "project (.mcp.json)" : s.source);
  kv(grid, "Asks", ASKS[s.confirm] || s.confirm);
  if (s.server) kv(grid, "Server", s.server);
  if (s.resource_count) {
    const res = el("span", "mcp-res-cell");
    const list = el("div", "mcp-resources");
    res.append(`${s.resource_count} `, button("Show…", () => showResources(list, s)));
    kv(grid, "Resources", res);
    body.append(grid, list);
  } else {
    body.append(grid);
  }

  if (s.error) {
    const err = el("div", "mcp-error");
    err.append(el("div", "", s.error));
    if (s.log?.length) err.append(el("pre", "mcp-log", s.log.join("\n")));
    body.append(err);
  }

  if (s.state === "ready") {
    body.append(el("div", "mcp-h", `Tools (${s.tool_list.length})`));
    const tools = el("div", "mcp-tools mcp-tools-box");
    tools.append(...s.tool_list.map((t) => toolRow(t, s)));
    if (!s.tool_list.length) tools.append(el("span", "muted small", "This server offers no tools."));
    body.append(tools);
  }

  const acts = el("div", "mcp-card-actions");
  acts.append(...serverActions(s), el("span", "spacer"));
  if (s.source === "global") {
    const rm = button("Remove…", async () => {
      if (!(await confirmInline(acts, `Remove ${s.name} from mcp.json?`, "Remove"))) return;
      try {
        await hooks.post("api/mcp/servers", { action: "remove", name: s.name });
        toast(`Removed ${s.name}`);
      } catch (e) {
        toast(e.message, "error");
      }
    }, "link-btn danger");
    acts.append(rm);
  }
  body.append(acts);
  d.append(sum, body);
  return d;
}

function addForm() {
  const card = el("details", "mcp-add-card");
  card.innerHTML = `
    <summary><span class="mcp-plus">+</span> Add server <span class="mcp-meta">to ~/.momo-harness/mcp.json</span></summary>
    <form class="mcp-form" autocomplete="off">
      <div class="mcp-form-grid">
        <span class="mcp-k">Type</span>
        <div class="seg" role="radiogroup" aria-label="Transport">
          <label><input type="radio" name="mcp-kind" value="stdio" checked> Command</label>
          <label><input type="radio" name="mcp-kind" value="http"> HTTP URL</label>
        </div>
        <label class="mcp-k" for="mcp-f-name">Name</label>
        <div><input id="mcp-f-name" name="name" required pattern="[A-Za-z0-9_\\-]{1,40}" placeholder="github">
          <div class="mcp-hint">Letters, digits, - and _ — tools are named mcp__<i>name</i>__…</div></div>
        <label class="mcp-k stdio" for="mcp-f-command">Command</label>
        <div class="stdio"><input id="mcp-f-command" name="command" class="mono" placeholder="uvx mcp-server-git --repository ."></div>
        <label class="mcp-k http" for="mcp-f-url" hidden>URL</label>
        <div class="http" hidden><input id="mcp-f-url" name="url" class="mono" placeholder="https://example.com/mcp"></div>
        <label class="mcp-k" for="mcp-f-kv"><span class="stdio">Environment</span><span class="http" hidden>Headers</span></label>
        <div><textarea id="mcp-f-kv" name="kv" rows="3" class="mono" placeholder="KEY=value"></textarea>
          <div class="mcp-hint">One per line. <code>\${VAR}</code> reads it from your environment, so secrets stay out of the file.</div></div>
        <label class="mcp-k" for="mcp-f-confirm">Ask</label>
        <div><select id="mcp-f-confirm" name="confirm">
          <option value="writes">for tools that change things</option>
          <option value="always">before every call</option>
          <option value="never">never</option></select></div>
      </div>
      <div class="mcp-form-error" hidden></div>
      <div class="mcp-form-actions">
        <button type="button" class="btn small" data-act="cancel">Cancel</button>
        <button type="submit" class="btn small primary">Add server</button>
      </div>
    </form>`;
  const form = card.querySelector("form");
  const errBox = card.querySelector(".mcp-form-error");
  const kind = () => form.querySelector("input[name=mcp-kind]:checked").value;
  const sync = () => {
    for (const n of form.querySelectorAll(".stdio")) n.hidden = kind() !== "stdio";
    for (const n of form.querySelectorAll(".http")) n.hidden = kind() !== "http";
  };
  const showError = (msg) => { errBox.textContent = msg; errBox.hidden = !msg; };
  const reset = () => { form.reset(); sync(); showError(""); };
  for (const r of form.querySelectorAll("input[name=mcp-kind]")) r.onchange = sync;
  form.querySelector("[data-act=cancel]").onclick = () => { reset(); card.open = false; };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    const kv = {};
    for (const line of String(f.get("kv") || "").split("\n")) {
      const i = line.indexOf("=");
      if (i > 0) kv[line.slice(0, i).trim()] = line.slice(i + 1).trim();
    }
    let entry;
    if (kind() === "http") {
      const url = String(f.get("url") || "").trim();
      if (!url) { showError("Enter the server's URL."); return; }
      entry = { type: "http", url };
      if (Object.keys(kv).length) entry.headers = kv;
    } else {
      const words = splitArgs(String(f.get("command") || ""));
      if (!words.length) { showError("Enter the command that starts the server."); return; }
      entry = { command: words[0], args: words.slice(1) };
      if (Object.keys(kv).length) entry.env = kv;
    }
    if (f.get("confirm") !== "writes") entry.confirm = f.get("confirm");
    const name = String(f.get("name")).trim();
    try {
      await hooks.post("api/mcp/servers", { action: "add", name, entry });
      toast(`Added ${name} — starting it`);
      reset();
      card.open = false;
      openServers.add(name);
    } catch (err) {
      showError(err.message.replace(/^\d+\s*/, ""));
    }
  };
  return card;
}

// A command line into words: whitespace-separated, with "double" or 'single' quotes.
export function splitArgs(line) {
  const out = [];
  const re = /"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+)/g;
  let m;
  while ((m = re.exec(line))) out.push(m[1] !== undefined ? m[1].replace(/\\(.)/g, "$1") : m[2] ?? m[3]);
  return out;
}

let form = null;

export async function renderMcpSettings() {
  const box = $("#mcp-servers");
  if (!box) return;
  let v;
  try {
    v = await load();
  } catch (e) {
    box.replaceChildren(el("div", "muted", `Could not load: ${e.message}`));
    return;
  }
  const head = el("div", "mcp-list-head");
  const ready = v.servers.filter((s) => s.state === "ready").length;
  head.append(el("span", "mcp-h", "Servers"),
              el("span", "mcp-meta", v.servers.length ? `${ready} of ${v.servers.length} connected` : ""),
              el("span", "spacer"));
  if (v.enabled) head.append(button("Reload", () => run("/mcp reload")));
  const kids = [head];
  if (!v.enabled) kids.push(el("div", "mcp-empty", "MCP is off — no servers run and the model gets no MCP tools."));
  else if (!v.servers.length) kids.push(el("div", "mcp-empty", "No servers yet. Add one below, or put them in mcp.json."));
  const list = el("div", "mcp-list");
  list.append(...v.servers.map(serverCard));
  kids.push(list);
  for (const e of v.errors) kids.push(el("div", "mcp-error", e));
  const info = el("div", "mcp-info");
  if (v.schema_tokens) {
    const pct = Math.round((v.schema_tokens * 100) / Math.max(v.context_limit, 1));
    const cost = el("div", pct >= 25 ? "warn" : "",
                    `Tool schemas ~${v.schema_tokens.toLocaleString()} tokens · ${pct}% of the context limit`);
    if (pct >= 25) cost.title = "Turn tools off above, or list the ones you need under \"tools\": {\"include\": […]} in the config.";
    info.append(cost);
  }
  const files = el("div", "");
  const g = el("span", "mono", v.files.global);
  const p = el("span", "mono", "./.mcp.json");
  p.title = v.files.project;
  files.append("Config ", g, " · project ", p);
  info.append(files);
  kids.push(info);
  box.replaceChildren(...kids);
  // The form lives outside the re-rendered list, so typing in it survives updates.
  if (!form) {
    form = addForm();
    $("#mcp-add-box").replaceChildren(form);
  }
}

export function initMcp(h) {
  Object.assign(hooks, h);
  const sec = document.querySelector('#settings-drawer details[data-section="mcp"]');
  sec?.addEventListener("toggle", () => sec.open && renderMcpSettings());
}
