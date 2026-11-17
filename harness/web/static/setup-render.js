// The Setup panel's content: where momo runs, where its model server runs and,
// in a momo-box, the box's network, sync, toolchain and safeguards.  Shared by
// the web UI (setup.js) and the host dashboard (`momo-box dash`), so it has no
// imports.  Everything is built with textContent: part of the data comes from
// inside a box.

const SVGNS = "http://www.w3.org/2000/svg";

function h(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

function s(tag, attrs = {}, text) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) n.setAttribute(k, v);
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

// ── small formatters ─────────────────────────────────────────────────────────

export { mb };

export function ago(ts, now = Date.now() / 1000) {
  if (typeof ts !== "number") return "never";
  const d = Math.max(0, now - ts);
  if (d < 45) return "just now";
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
}

function short(ts, now) {
  return ago(ts, now).replace(" ago", "");
}

function clock(ts) {
  if (typeof ts !== "number") return "";
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function kTokens(n) {
  if (typeof n !== "number" || !n) return "?";
  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

function bytes(n) {
  if (typeof n !== "number") return "?";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
}

function uptime(started, now) {
  if (typeof started !== "number") return "";
  const d = Math.max(0, now - started);
  if (d < 3600) return `${Math.max(1, Math.round(d / 60))}m`;
  if (d < 86400) return `${Math.round(d / 3600)}h`;
  return `${Math.round(d / 86400)}d`;
}

function base(path) {
  return String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || String(path || "");
}

async function copy(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = h("textarea");
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try { document.execCommand("copy"); } catch { /* nothing more to try */ }
    ta.remove();
  }
  if (btn) {
    const was = btn.textContent;
    btn.textContent = "copied";
    setTimeout(() => { btn.textContent = was; }, 1200);
  }
}

function copyCmd(cmd) {
  const row = h("span", "su-cmd");
  row.append(h("code", "", cmd));
  const b = h("button", "su-copy", "copy");
  b.type = "button";
  b.title = "Copy the command — run it on your machine, outside the box";
  b.onclick = () => copy(cmd, b);
  row.append(b);
  return row;
}

// ── states ───────────────────────────────────────────────────────────────────

const SYNC_TEXT = {
  ok: "work landed",
  pending: "turn ended, not landed",
  incoming: "landed beside your branch",
  error: "last sync failed",
  "syncer-silent": "syncer not running",
  unknown: "no sync data yet",
};

export function modelState(m) {
  if (!m) return "unknown";
  if (m.reachable === false || m.latency?.ok === false) return "bad";
  if (typeof m.latency?.ms === "number" && m.latency.ms > 1000) return "warn";
  return m.reachable === null && m.latency?.ok == null ? "unknown" : "ok";
}

// The sync state in words; `brief` for the diagram's narrow node.  A turn's
// work lands when the turn ends, so changes made during it are not on the
// branch yet: "running" says so, "pending" means a finished turn is late.
export function syncText(st, v, brief = false) {
  const turn = v?.turn ?? 0;
  if (st === "running") return brief ? `turn ${turn} running` : `turn ${turn} running · lands when it ends`;
  if (st === "ok" && !v?.box?.sync?.last?.ts) return "up to date";
  return SYNC_TEXT[st] || st;
}

// What landed: synced turns that changed nothing are not "nothing yet".
function noChanges(turn, brief = false) {
  const turns = turn === 1 ? "turn 1" : `turns 1–${turn}`;
  return brief ? `${turns}: no changes` : `${turns} made no changes`;
}

function landedLine(sync, now) {
  if (sync.last?.ts) return `landed ${ago(sync.last.ts, now)}`;
  return typeof sync.turn === "number" && sync.turn >= 1 ? noChanges(sync.turn, true) : "nothing landed yet";
}

export function syncClass(state) {
  return { ok: "ok", running: "ok", pending: "ok", incoming: "warn", error: "bad", "syncer-silent": "bad" }[state] || "unknown";
}

function dot(state) {
  return h("span", `su-dot st-${state}`);
}

function stateLine(state, text) {
  const p = h("p", "su-state");
  p.append(dot(state), h("span", "", text));
  return p;
}

// ── the diagram ──────────────────────────────────────────────────────────────
// The docsite's momo-box picture (.topo), laid out from live data: nodes carry
// a state dot, edges a count.

function node(svg, { x, y, w, hgt, cls, title, lines, state }) {
  const g = s("g", { class: `node ${cls}` });
  g.append(s("rect", { x, y, width: w, height: hgt, rx: 8 }));
  g.append(s("text", { class: "t", x: x + 12, y: y + 19 }, title));
  if (state) g.append(s("circle", { class: `st st-${state}`, cx: x + w - 14, cy: y + 14, r: 5 }));
  const max = Math.floor((w - 24) / 6.9);
  lines.slice(0, Math.floor((hgt - 26) / 17)).forEach((ln, i) => {
    const text = String(ln);
    const t = s("text", { class: "s", x: x + 12, y: y + 39 + i * 17 },
      text.length > max ? text.slice(0, max - 1) + "…" : text);
    if (text.length > max) t.append(s("title", {}, text));
    g.append(t);
  });
  svg.append(g);
}

// An edge is a straight line or, with `via`, a route of horizontal and
// vertical segments through the gaps between nodes: [x, y] corner points.
// Every corner is a right angle and no segment crosses a node.
// `tone`: "restricted" (orange: only some of the outside) or "off" (red, dashed;
// with `cross`, a ✕: no way out at all).
function edge(svg, { x1, y1, x2, y2, via = [], cls = "", label, lx, ly, tone, cross = true }) {
  const pts = [[x1, y1], ...via, [x2, y2]];
  for (let i = 1; i < pts.length; i++) {
    const [ax, ay] = pts[i - 1], [bx, by] = pts[i];
    if (ax !== bx && ay !== by) console.warn(`setup diagram: diagonal segment ${ax},${ay} → ${bx},${by}`);
  }
  const off = tone === "off";
  svg.append(s("path", { class: off ? "blocked" : `link ${cls}${tone ? ` ${tone}` : ""}`, fill: "none",
                         d: "M" + pts.map(([x, y]) => `${x} ${y}`).join(" L"),
                         "marker-end": `url(#su-arrow${tone ? `-${tone}` : ""})` }));
  if (off && cross) svg.append(s("text", { class: "x", x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 6 }, "✕"));
  if (label) svg.append(s("text", { class: "sub", x: lx ?? (x1 + x2) / 2, y: ly ?? (y1 + y2) / 2 - 7 }, label));
}

function zone(svg, x, y, w, hgt, label, solid) {
  svg.append(s("rect", { class: `zone${solid ? " vm" : ""}`, x, y, width: w, height: hgt, rx: 10 }));
  svg.append(s("text", { class: "zone-t", x: x + 14, y: y + 18 }, label));
}

function svgRoot(height, label) {
  const svg = s("svg", { viewBox: `0 0 760 ${height}`, role: "img", "aria-label": label });
  const defs = s("defs");
  const m = s("marker", { id: "su-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7,
                         markerHeight: 7, orient: "auto-start-reverse" });
  m.append(s("path", { class: "ah", d: "M0 0 10 5 0 10z" }));
  defs.append(m);
  for (const tone of ["restricted", "off"]) {        // a marker can't take its line's colour
    const t = s("marker", { id: `su-arrow-${tone}`, viewBox: "0 0 10 10", refX: 9, refY: 5,
                            markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
    t.append(s("path", { class: `ah ah-${tone}`, d: "M0 0 10 5 0 10z" }));
    defs.append(t);
  }
  svg.append(defs);
  return svg;
}

// The server as the user named it: inside a box momo's own URL is the gateway.
function serverAddr(m) {
  const loc = m.location || {};
  if (loc.host) return `${loc.host}${loc.port ? `:${loc.port}` : ""}${loc.https ? " (https)" : ""}`;
  return (m.url || "").replace(/^https?:\/\//, "");
}

function modelLines(m) {
  const lat = m.latency || {};
  const speed = m.perf?.tok_s ? `${m.perf.tok_s} tok/s` : "";
  const reach = m.reachable === false || lat.ok === false ? "not reachable"
    : typeof lat.ms === "number" ? `${lat.ms} ms` : "";
  return [
    m.location?.label || "",
    serverAddr(m),
    m.model || "",
    `ctx ${kTokens(m.context?.window || m.context?.limit)} · ${m.context?.pct ?? 0}% used`,
    [reach, speed].filter(Boolean).join(" · "),
  ];
}

const pick = (lines, idx) => idx.map((i) => lines[i]).filter(Boolean);

function localDiagram(v, now) {
  const svg = svgRoot(330, "Where momo runs and what it reaches");
  const m = v.model || {};
  const mach = v.machine || {};
  const git = v.workspace?.git || {};
  zone(svg, 10, 8, 520, 312, `this machine · ${mach.host || ""}`, true);
  svg.append(s("text", { class: "zone-t", x: 556, y: 26 }, "outside"));
  const web = v.web;
  node(svg, { x: 26, y: 40, w: 300, hgt: 148, cls: "host", title: "momo", state: "ok", lines: [
    `${mach.os || ""} · pid ${mach.pid ?? "?"} · up ${uptime(mach.started, now)}`,
    web ? `web UI ${web.bind}:${web.port} · ${web.token ? "token" : "no token"}` : "",
    `workspace ${base(v.workspace?.path)}`,
    git.repo ? `git ${git.branch} · ${git.changed} changed` : "not a git repository",
    `commands run as you${typeof mach.uid === "number" ? ` (uid ${mach.uid})` : ""}`,
    `not isolated${v.memory?.current ? ` · mem ${mb(v.memory.current)}` : ""}`,
  ] });
  const mcp = v.network?.mcp || [];
  node(svg, { x: 26, y: 214, w: 300, hgt: 92, cls: "ext", title: "MCP servers",
    state: mcp.some((r) => r.state === "error") ? "bad" : mcp.length ? "ok" : null,
    lines: mcp.length ? mcp.slice(0, 3).map((r) => `${r.name} · ${r.transport} · ${r.state}`)
      .concat(mcp.length > 3 ? [`+${mcp.length - 3} more`] : []) : ["none configured"] });
  edge(svg, { x1: 176, y1: 188, x2: 176, y2: 212, cls: "host" });
  const mState = modelState(m);
  const near = m.location?.kind === "local";
  const mx = near ? 346 : 556;
  node(svg, { x: mx, y: 40, w: near ? 170 : 194, hgt: 148, cls: "box-c", title: m.provider || "model server",
    state: mState, lines: modelLines(m) });
  edge(svg, { x1: 326, y1: 110, x2: mx - 2, y2: 110, cls: "box-c",
    label: near ? null : "inference", lx: (326 + mx) / 2, ly: 100 });
  const net = v.network?.web_tools || "off";
  node(svg, { x: 556, y: 214, w: 194, hgt: 92, cls: "ext", title: "internet", lines: [
    `web tools: ${net}`, "shell commands: any host",
    (v.network?.recent || []).length ? `${v.network.recent.length} host(s) fetched` : "",
  ] });
  // /net: off is red, but without a ✕: momo isn't isolated, shell commands still go
  // anywhere; on (public sites only) is orange; local (also the LAN) stays grey.
  edge(svg, { x1: 300, y1: 188, via: [[300, 201], [653, 201]], x2: 653, y2: 212, cls: "ext",
    tone: net === "off" ? "off" : net === "on" ? "restricted" : undefined, cross: false,
    label: net === "off" ? "commands only" : "web tools + commands", lx: 440, ly: 218 });
  return svg;
}

function boxDiagram(v, now) {
  const b = v.box || {};
  const setup = b.setup || {};
  const net = b.net || {};
  const m = v.model || {};
  const mode = setup.net || "?";
  const gated = mode === "pull" || mode === "locked";
  const svg = svgRoot(360, "The box, its gateway and what it reaches");
  zone(svg, 10, 8, 524, 344, "your machine", false);
  zone(svg, 232, 32, 290, 308, gated ? "container engine · internal network" : "container engine · open network", true);
  svg.append(s("text", { class: "zone-t", x: 556, y: 26 }, "outside"));
  const lim = setup.limits || {};
  node(svg, { x: 246, y: 58, w: 262, hgt: 124, cls: "box-c", title: `box ${setup.name || ""}`, state: "ok", lines: [
    `momo · turn ${v.turn ?? 0} · ${mode}`,
    `${setup.image || ""}`,
    memLine(v.memory) || [lim.memory && `mem ${lim.memory}${lim.low_mem ? " (low)" : ""}`, lim.cpus && `${lim.cpus} CPU`, lim.pids && `${lim.pids} pids`].filter(Boolean).join(" · "),
    "read-only root · uid 1000",
    "/workspace /home/momo /in (ro)",
  ] });
  // your repo, where the work lands
  const sync = b.sync || {};
  const last = sync.last || {};
  const sState = b.sync_state || "unknown";
  node(svg, { x: 24, y: 58, w: 196, hgt: 104, cls: "host", title: "your repo", state: syncClass(sState), lines: [
    setup.source?.name || "",
    `→ ${last.branch || setup.land_branch || "momo/" + (setup.name || "")}`,
    landedLine(sync, now),
    syncText(sState, v, true),
  ] });
  edge(svg, { x1: 246, y1: 110, x2: 222, y2: 110, cls: `host st-${syncClass(sState)}` });
  const mState = modelState(m);
  const near = ["local", "host"].includes(m.location?.kind);
  if (gated) {
    const llm = net.llm || {};
    node(svg, { x: 246, y: 214, w: 262, hgt: 108, cls: "gw", title: "gateway", state: "ok", lines: [
      `:9000 model · ${llm.inference ?? 0} chats · ${llm.refused ?? 0} refused`,
      mode === "pull" ? `:3128 allowlist · ${net.totals?.allowed ?? 0} ✓ ${net.totals?.denied ?? 0} ✗`
        : ":3128 closed (locked)",
      ":8765 web UI → 127.0.0.1",
      `no other route out${v.memory?.gateway?.current ? ` · mem ${mb(v.memory.gateway.current)}` : ""}`,
    ] });
    edge(svg, { x1: 377, y1: 182, x2: 377, y2: 212, cls: "gw" });
    // registries and the allowlist
    const rules = setup.allowlist?.rules || [];
    node(svg, { x: 556, y: 214, w: 194, hgt: 108, cls: "ext", title: mode === "pull" ? "allowed hosts" : "registries",
      state: mode === "pull" ? (b.recent_denied ? "warn" : "ok") : null, lines: mode === "pull" ? [
        `${rules.filter((r) => r.kind !== "!").length} allow rules`,
        `${rules.filter((r) => r.kind === "!").length} denied on purpose`,
        `${b.recent_denied || 0} refused, last 10 min`,
      ] : ["nothing: locked", "deps came from prepare"] });
    // the way out: orange through the allowlist (pull), red when there is none (locked)
    edge(svg, { x1: 508, y1: 268, x2: 554, y2: 268, cls: "ext",
                tone: mode === "pull" ? "restricted" : "off" });
    // the model server
    if (near) {
      node(svg, { x: 24, y: 214, w: 196, hgt: 108, cls: "box-c", title: m.provider || "model", state: mState,
        lines: pick(modelLines(m), [0, 1, 2, 4]) });
      edge(svg, { x1: 246, y1: 250, x2: 222, y2: 250, cls: "box-c" });
    } else {
      node(svg, { x: 556, y: 58, w: 194, hgt: 124, cls: "box-c", title: m.provider || "model", state: mState,
        lines: modelLines(m) });
      edge(svg, { x1: 508, y1: 240, via: [[528, 240], [528, 120]], x2: 554, y2: 120, cls: "box-c" });
    }
  } else {
    node(svg, { x: 246, y: 214, w: 262, hgt: 108, cls: "ext", title: "no gateway", state: "warn", lines: [
      "open network: the box reaches",
      "the internet and your LAN",
      ":8765 web UI → 127.0.0.1",
    ] });
    node(svg, { x: 556, y: 214, w: 194, hgt: 108, cls: "ext", title: "internet", state: "warn",
      lines: ["anything", "no allowlist"] });
    edge(svg, { x1: 508, y1: 150, via: [[528, 150], [528, 268]], x2: 554, y2: 268, cls: "ext" });
    if (near) {
      node(svg, { x: 24, y: 214, w: 196, hgt: 108, cls: "box-c", title: m.provider || "model", state: mState,
        lines: pick(modelLines(m), [0, 1, 2, 4]) });
      edge(svg, { x1: 300, y1: 182, via: [[300, 198], [122, 198]], x2: 122, y2: 212, cls: "box-c" });
    } else {
      node(svg, { x: 556, y: 58, w: 194, hgt: 124, cls: "box-c", title: m.provider || "model", state: mState,
        lines: modelLines(m) });
      edge(svg, { x1: 508, y1: 110, x2: 554, y2: 110, cls: "box-c" });
    }
  }
  return svg;
}

export function diagram(v, now = Date.now() / 1000) {
  const fig = h("figure", "topo su-topo");
  const scroll = h("div", "su-scroll");
  scroll.append(v.kind === "box" ? boxDiagram(v, now) : localDiagram(v, now));
  fig.append(scroll);
  return fig;
}

// ── memory ───────────────────────────────────────────────────────────────────

function mb(n) {
  if (typeof n !== "number") return "?";
  return n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(n >= 10 * 1024 ** 3 ? 0 : 1)} GB`
    : `${Math.round(n / 1024 ** 2)} MB`;
}

// The box node's memory line: "mem 87 MB / 1.0 GB · peak 364 MB".
function memLine(mem) {
  if (!mem || typeof mem.current !== "number") return "";
  return `mem ${mb(mem.current)}${mem.max ? ` / ${mb(mem.max)}` : ""}`
    + (mem.swap > 1024 ** 2 ? ` · swap ${mb(mem.swap)}` : mem.peak ? ` · peak ${mb(mem.peak)}` : "");
}

function memorySection(v) {
  const mem = v.memory;
  if (!mem) return null;
  if (!mem.available) {
    return section("Memory", note(v.kind === "box"
      ? "The box's memory use is not readable (no cgroup v2 here)." : "momo's memory use is not available here."));
  }
  const kids = [];
  const of = mem.max ? mem.max : (mem.engine_total || mem.machine_total);
  const pct = of ? Math.min(100, Math.round((mem.current / of) * 100)) : null;
  const state = mem.state || "ok";
  const what = v.kind === "box"
    ? `${mb(mem.current)}${mem.max ? ` of its ${mb(mem.max)} cap` : of ? ` of the engine's ${mb(of)} (no cap)` : ""}`
    : `momo uses ${mb(mem.current)}${of ? ` of this machine's ${mb(of)}` : ""}`;
  kids.push(stateLine(state, `${what}${pct !== null ? ` (${pct}%)` : ""}${mem.oom_kills ? ` · ${mem.oom_kills} OOM kill${mem.oom_kills === 1 ? "" : "s"}` : ""}`));
  if (pct !== null) {
    const bar = h("div", `meter su-meter su-mem st-${state}`);
    const fill = h("span");
    fill.style.width = `${pct}%`;
    bar.append(fill);
    kids.push(bar);
  }
  const hist = (mem.history || []).map((r) => r[1]);
  if (hist.length >= 2) {
    const mins = Math.max(1, Math.round((mem.history.at(-1)[0] - mem.history[0][0]) / 60));
    kids.push(spark(hist, `highest ${mb(Math.max(...hist))} in the last ${mins} min`));
  }
  kids.push(facts(v.kind === "box" ? [
    ["peak", mem.peak ? `${mb(mem.peak)} since the box started` : ""],
    ["of which", typeof mem.anon === "number" ? `${mb(mem.anon)} programs · ${mb(mem.file)} file cache (given back when needed) · ${mb(mem.kernel)} kernel` : ""],
    ["swap", typeof mem.swap === "number" && (mem.swap || mem.swap_max)
      ? `${mb(mem.swap)} in use${mem.swap_max ? ` of ${mb(mem.swap_max)} allowed` : ""}${mem.swap > (mem.max || Infinity) / 10 ? " — the box is out of memory and slows down" : ""}` : ""],
    ["gateway", mem.gateway ? `${mb(mem.gateway.current)} (its own container)` : ""],
    ["engine", mem.engine_total ? `${mb(mem.engine_total)} in total` : ""],
  ] : [
    ["peak", mem.peak ? mb(mem.peak) : ""],
    ["this machine", mem.machine_total ? mb(mem.machine_total) : ""],
  ]));
  if ((mem.processes || []).length) {
    const shown = mem.processes.filter((r) => r.rss >= 1024 ** 2);
    if (shown.length) kids.push(table(["process", "in memory"], shown.map((r) => [r.label, mb(r.rss)]), "su-procs"));
  }
  return section("Memory", ...kids);
}

// ── tables and lists ─────────────────────────────────────────────────────────

function table(head, rows, cls = "") {
  const t = h("table", `su-table ${cls}`);
  const tr = h("tr");
  for (const c of head) tr.append(h("th", "", c));
  const thead = h("thead");
  thead.append(tr);
  t.append(thead);
  const tb = h("tbody");
  for (const r of rows) {
    const row = h("tr");
    for (const c of r) {
      const td = h("td");
      if (c instanceof Node) td.append(c); else td.textContent = c ?? "";
      row.append(td);
    }
    tb.append(row);
  }
  t.append(tb);
  const wrap = h("div", "su-table-wrap");
  wrap.append(t);
  return wrap;
}

function facts(pairs) {
  const dl = h("dl", "su-facts");
  for (const [k, v] of pairs) {
    if (v === undefined || v === null || v === "") continue;
    dl.append(h("dt", "", k));
    const dd = h("dd");
    if (v instanceof Node) dd.append(v); else dd.textContent = v;
    dl.append(dd);
  }
  return dl;
}

function section(title, ...kids) {
  const sec = h("section", "su-sec");
  sec.append(h("h3", "", title), ...kids.filter(Boolean));
  return sec;
}

function note(text, cls = "") {
  return h("p", `su-note ${cls}`, text);
}

function yesNo(ok) {
  const span = h("span", `su-mark ${ok ? "ok" : "bad"}`, ok ? "✓" : "✕");
  span.setAttribute("aria-label", ok ? "yes" : "no");
  return span;
}

// ── tabs ─────────────────────────────────────────────────────────────────────

function setupTab(v, now) {
  const out = [diagram(v, now)];
  if (v.kind === "box") {
    const b = v.box || {};
    if (!b.available) {
      out.push(note(b.folder === false
        ? "This box has no status folder: it was created before momo-box wrote one. Recreate it (momo-box rm, then web) to see its sync and network here."
        : "Waiting for the first status from momo-box on your machine…", "warn"));
    }
    const st = b.setup || {};
    out.push(facts([
      ["box", st.name], ["network", st.net], ["image", st.image],
      ["source", st.source ? `${st.source.name || ""}${st.source.ref ? ` @ ${st.source.ref}` : ""}` : ""],
      ["created", st.created ? ago(st.created, now) : ""],
      ["sync", SYNC_TEXT[b.sync_state] || b.sync_state],
    ]));
  } else {
    const m = v.model || {};
    out.push(facts([
      ["momo", `${v.machine?.host || ""} · ${v.machine?.os || ""} · Python ${v.machine?.python || ""}`],
      ["web UI", v.web ? `${v.web.url}${v.web.token ? " (token required)" : ""}` : ""],
      ["model server", `${m.provider || ""} · ${m.location?.label || ""} · ${m.url || ""}`],
      ["isolation", "none: momo-box runs momo in a container"],
    ]));
  }
  out.push(memorySection(v));
  return out;
}

function networkTab(v, now) {
  if (v.kind !== "box") {
    const n = v.network || {};
    const recent = n.recent || [];
    return [
      section("momo's web tools", facts([
        ["fetch_url / web_search", n.web_tools === "off" ? "off (/net on turns them on)" : n.web_tools],
        ["confirm writes", n.net_confirm ? "on" : "off"],
        ["shell commands", "your full network: momo does not filter them"],
      ])),
      section("Hosts reached lately", recent.length
        ? table(["host", "requests", "last", ""], recent.map((r) => [r.host, r.count, ago(r.ts, now), yesNo(r.ok)]))
        : note("none yet")),
      section("MCP servers", (n.mcp || []).length
        ? table(["name", "transport", "target", "state", "tools"],
            n.mcp.map((r) => [r.name, r.transport, r.target, r.state, r.tools]))
        : note("none configured")),
    ];
  }
  const b = v.box || {};
  const st = b.setup || {};
  const al = st.allowlist || {};
  const net = b.net || {};
  const name = st.name || "NAME";
  const out = [];
  const mode = st.net;
  out.push(stateLine(mode === "open" ? "warn" : "ok",
    mode === "pull" ? "pull: only the hosts below, through the gateway's allowlist (host and port; no TLS interception)"
      : mode === "locked" ? "locked: no route out; dependencies came from prepare"
      : mode === "open" ? "open: the box reaches the internet and your LAN"
      : "network mode unknown"));
  if (al.instructions) out.push(section("Instructions from .momo/network.json", h("pre", "su-pre", al.instructions)));
  const KIND = { "+": "yours", "!": "denied", "": "allowed" };
  const rules = al.rules || [];
  if (mode !== "open") {
    out.push(section(`Allowlist${al.replace_defaults ? " (the repo replaced the default registries)" : ""}`,
      rules.length ? table(["", "rule", "why", "from"], rules.map((r) => [
        r.overridden_by ? h("span", "su-kind ko", "overridden")
          : h("span", `su-kind k${r.kind === "+" ? "y" : r.kind === "!" ? "d" : "a"}`, KIND[r.kind] ?? r.kind),
        r.rule, r.overridden_by ? `denied by ${r.overridden_by}${r.reason ? ` (was: ${r.reason})` : ""}` : r.reason,
        r.source]), "su-rules") : note("nothing is allowed")));
  }
  const hosts = (net.hosts || []).slice().sort((a, b2) => (b2.last_ts || 0) - (a.last_ts || 0));
  if (mode === "pull" || hosts.length) {
    const refused = hosts.filter((x) => x.denied).sort((a, b2) => b2.denied - a.denied).slice(0, 5);
    if (refused.length) {
      const list = h("ul", "su-refused");
      for (const r of refused) {
        const li = h("li");
        li.append(h("strong", "", `${r.host}${r.port && r.port !== 443 ? `:${r.port}` : ""}`),
          h("span", "muted", ` ${r.denied}× refused${r.reason ? ` — ${r.reason}` : ""}`),
          copyCmd(`momo-box allow ${name} ${r.host}${r.port && r.port !== 443 && r.port !== 80 ? `:${r.port}` : ""}`));
        list.append(li);
      }
      out.push(section("Refused most", list,
        note("Allowing is up to you, on your machine: nothing in the box can widen its network.", "muted")));
    }
    out.push(section("Hosts", hosts.length
      ? table(["host", "✓", "✕", "last", "rule"], hosts.map((x) => [
          `${x.host}${x.port ? `:${x.port}` : ""}`, x.allowed || 0, x.denied || 0, ago(x.last_ts, now),
          x.rule ? `${x.rule}${x.reason ? ` — ${x.reason}` : ""}` : ""]))
      : note("no requests yet")));
    const recent = (net.recent || []).slice(-40).reverse();
    out.push(section("Activity", recent.length
      ? table(["time", "", "request", "rule"], recent.map((e) => [
          clock(e.ts), yesNo(e.allowed),
          e.kind === "llm" ? `model ${e.method || ""} ${e.path || ""}` : `${e.method || ""} ${e.host || ""}:${e.port || ""}`,
          e.rule ? `${e.rule}${e.reason ? ` — ${e.reason}` : ""}` : (e.status ? `HTTP ${e.status}` : "")]), "su-feed")
      : note("nothing yet")));
  }
  return out;
}

function syncTab(v, now) {
  if (v.kind !== "box") {
    const w = v.workspace || {};
    const g = w.git || {};
    return [
      section("Workspace", facts([
        ["path", w.path],
        ["git", g.repo ? `${g.branch} · ${g.changed} changed file${g.changed === 1 ? "" : "s"}` : "not a git repository"],
        ["last commit", g.last ? `${g.last.sha} ${g.last.subject} (${ago(g.last.ts, now)})` : ""],
        ["momo's changes", `${w.changes_files || 0} file(s) in ${w.undo_count || 0} checkpoint(s) since the last /keep`],
      ])),
      note("momo works in this folder directly; nothing is synced."),
    ];
  }
  const b = v.box || {};
  const sync = b.sync || {};
  const last = sync.last || {};
  const st = b.sync_state || "unknown";
  const out = [stateLine(syncClass(st), `${syncText(st, v)}${last.ts ? ` · last land ${ago(last.ts, now)}` : ""}`)];
  if (st === "syncer-silent") {
    out.push(note("The host-side syncer has not checked in for over a minute. `momo-box ls` on your machine restarts it; work stays in the box meanwhile.", "warn"));
  }
  if (sync.error && st === "error") out.push(note(`${clock(sync.error.ts)} ${sync.error.text}`, "bad"));
  if (last.ts) {
    out.push(section("Last land", facts([
      ["branch", last.branch], ["commit", last.head ? String(last.head).slice(0, 12) : ""],
      ["changes", last.shortstat], ["commits", last.commits],
      ["turn", last.turn],
      ["beside your branch", last.incoming ? (last.reason || "yes") : ""],
      ["left out (too large)", (last.skipped_large || []).join(", ")],
    ])));
  }
  const hist = (sync.history || []).slice().reverse();
  out.push(section("History", hist.length
    ? table(["time", "branch", "commits", "changes"], hist.map((r) => [
        `${clock(r.ts)} (${short(r.ts, now)})`,
        r.incoming ? `${r.branch} ⚠` : r.branch, r.commits ?? "", r.shortstat || ""]), "su-feed")
    : note(typeof sync.turn === "number" && sync.turn >= 1
      ? `Nothing landed yet: ${noChanges(sync.turn)}.` : "nothing landed yet")));
  return out;
}

function spark(vals, label) {
  const w = 300, hgt = 56, top = 16, max = Math.max(...vals) || 1;
  const svg = s("svg", { class: "su-spark", viewBox: `0 0 ${w} ${hgt}`, role: "img", "aria-label": label });
  const step = w / Math.max(1, vals.length - 1);
  const pts = vals.map((x, i) => `${(i * step).toFixed(1)},${(hgt - 3 - (x / max) * (hgt - top - 3)).toFixed(1)}`);
  svg.append(s("line", { class: "base", x1: 0, y1: hgt - 3, x2: w, y2: hgt - 3 }));
  svg.append(s("polyline", { points: pts.join(" ") }));
  svg.append(s("text", { x: 0, y: 10 }, label));
  return svg;
}

function sparkline(rows) {
  const vals = rows.map((r) => r.tok_s).filter((x) => typeof x === "number");
  if (vals.length < 2) return note("Speed shows after a few replies.");
  return spark(vals, `peak ${Math.round(Math.max(...vals))} tok/s · last ${vals.length} replies`);
}

function modelTab(v) {
  const m = v.model || {};
  const lat = m.latency || {};
  const ctx = m.context || {};
  const perf = m.perf || {};
  const out = [stateLine(modelState(m), m.reachable === false || lat.ok === false ? "not reachable"
    : typeof lat.ms === "number" ? `reachable · ${lat.ms} ms` : "reachable")];
  out.push(facts([
    ["server", `${m.provider || ""} · ${m.url || ""}`],
    ["where", `${m.location?.label || ""}${m.location?.https ? " · https" : m.location?.kind === "internet" ? " · not encrypted" : ""}`],
    ["model", m.model],
    ["context", `${kTokens(ctx.used)} of ${kTokens(ctx.limit)} (${ctx.pct ?? 0}%) · window ${kTokens(ctx.window)}`],
  ]));
  const bar = h("div", "meter su-meter");
  const fill = h("span");
  fill.style.width = `${Math.min(100, ctx.pct || 0)}%`;
  bar.append(fill);
  out.push(bar);
  out.push(section("Speed", sparkline(perf.rows || []), facts([
    ["generation", perf.tok_s ? `${perf.tok_s} tok/s (median, last 5)` : ""],
    ["first token", typeof perf.ttft === "number" ? `${perf.ttft.toFixed(2)} s` : ""],
    ["replies measured", perf.calls || ""],
  ])));
  const load = m.load;
  if (load?.kind === "ollama") {
    out.push(section("Server load", load.models.length
      ? table(["in memory", "size", "VRAM", "ctx"], load.models.map((r) => [r.name, bytes(r.size),
          r.vram == null ? "?" : r.size ? `${Math.round((r.vram / r.size) * 100)}%` : bytes(r.vram), kTokens(r.context)]))
      : note("no model loaded")));
  } else if (load?.kind === "llama.cpp") {
    out.push(section("Server load", load.slots == null ? note(load.note || "unknown")
      : facts([["slots", `${load.busy} busy of ${load.slots}`]])));
  }
  if (v.kind === "box") {
    const llm = v.box?.net?.llm;
    const st = v.box?.setup?.llm || {};
    out.push(section("Through the gateway", facts([
      ["filter", st.filter === false ? "off: every request passes" : `inference paths only (${(st.paths || []).join(", ")})`],
      ["requests", llm ? `${llm.requests} (${llm.inference ?? 0} chat, the rest health checks)` : ""],
      ["refused", llm ? `${llm.refused}${llm.last_refused ? ` · last: ${llm.last_refused}` : ""}` : ""],
      ["last error", llm?.last_error || ""],
      ["if it is down", st.hint || ""],
    ])));
  }
  return out;
}

function toolchainTab(v, now) {
  const b = v.box || {};
  const t = b.setup?.toolchain || {};
  const p = b.prepare || {};
  const name = b.setup?.name || "NAME";
  const out = [facts([
    ["from", t.source], ["presets", (t.presets || []).join(", ")],
    ["base image", t.base ? `${t.base}${t.base_variant === "slim" ? " (slim: no C compiler; --full-base for the full image)" : t.base_variant === "full" ? " (full)" : ""}` : ""],
    ["memory", b.setup?.limits?.memory ? `${b.setup.limits.memory} cap${b.setup.limits.low_mem ? ", low-memory mode (no Gradle daemon, small code index)" : ""}` : "no cap"],
    ["packages", (t.packages || []).join(" ")],
  ])];
  if ((p.commands || []).length) {
    const lines = [p.unknown ? stateLine("unknown", "whether it is up to date shows in the box's own web UI")
      : stateLine(p.stale ? "warn" : p.ran ? "ok" : "unknown",
        p.stale ? "inputs changed since prepare ran" : p.ran ? `ran ${ago(p.ran, now)}` : "has not run")];
    if (p.stale) lines.push(copyCmd(`momo-box prepare ${name}`));
    out.push(section("Prepare", ...lines, table(["command"], p.commands.map((c) => [c])),
      (p.inputs || []).length ? note(`inputs: ${p.inputs.join(", ")}`, "muted") : null));
  } else {
    out.push(section("Prepare", note("no prepare commands")));
  }
  if ((t.offline_hints || []).length) out.push(section("Offline hints", table(["hint"], t.offline_hints.map((x) => [x]))));
  if ((t.warnings || []).length) out.push(section("Warnings", table(["warning"], t.warnings.map((x) => [x]))));
  return out;
}

// The docsite's rings, each ticked against what `docker inspect` reported.
const RING_GROUPS = [
  ["network", "network", ["internal-network", "proxy", "llm-filter"]],
  ["container", "container", ["read-only-root", "cap-drop", "no-new-privileges", "not-privileged",
                              "no-engine-socket", "limits"]],
  ["user", "user", ["uid"]],
  ["mounts", "what it reads", ["inbox-read-only", "status-read-only"]],
];

function safeguardsTab(v) {
  const rows = v.safeguards || [];
  if (v.kind !== "box") {
    return [
      note("momo runs directly on this machine. Nothing here is a sandbox: run it in a momo-box for that.", "warn"),
      table(["", "guard", "what it means"], rows.map((r) => [yesNo(r.ok), r.label, r.detail])),
    ];
  }
  if (!rows.length) return [note("No safeguard report yet.")];
  const by = Object.fromEntries(rows.map((r) => [r.id, r]));
  let outer = null, inner = null;
  for (const [, title, ids] of RING_GROUPS) {
    const got = ids.map((id) => by[id]).filter(Boolean);
    if (!got.length) continue;
    const ok = got.every((r) => r.ok);
    const ring = h("div", `ring su-ring ${ok ? "ok" : "bad"}`);
    const t = h("div", "ring-t");
    t.append(yesNo(ok), h("span", "", ` ${title}`));
    ring.append(t);
    const ul = h("ul", "su-checks");
    for (const r of got) {
      const li = h("li");
      li.append(yesNo(r.ok), h("span", "", ` ${r.label}`), h("span", "muted", r.detail ? ` — ${r.detail}` : ""));
      ul.append(li);
    }
    ring.append(ul);
    if (inner) inner.append(ring); else outer = ring;
    inner = ring;
  }
  const fig = h("figure", "rings su-rings");
  if (outer) fig.append(outer);
  const known = new Set(RING_GROUPS.flatMap((g) => g[2]));
  const rest = rows.filter((r) => !known.has(r.id));
  return [fig, rest.length ? table(["", "guard", ""], rest.map((r) => [yesNo(r.ok), r.label, r.detail])) : null,
    note("Checked against the running container (docker inspect), not just the flags momo-box meant to pass.", "muted")];
}

export function tabsFor(v) {
  return v.kind === "box"
    ? [["setup", "Setup"], ["network", "Network"], ["sync", "Sync"], ["model", "Model"],
       ["toolchain", "Toolchain"], ["safeguards", "Safeguards"]]
    : [["setup", "Setup"], ["network", "Network"], ["sync", "Workspace"], ["model", "Model"],
       ["safeguards", "Safeguards"]];
}

const RENDER = { setup: setupTab, network: networkTab, sync: syncTab, model: modelTab,
                 toolchain: toolchainTab, safeguards: safeguardsTab };

// Render view `v` into `root` with a tab bar; `opts.tab` picks the tab and
// `opts.onTab(name)` hears a change.  Keeps the scroll position on refresh.
export function renderSetup(v, root, opts = {}) {
  const now = opts.now ?? Date.now() / 1000;
  const tabs = tabsFor(v);
  const tab = tabs.some(([k]) => k === opts.tab) ? opts.tab : "setup";
  const bar = h("div", "su-tabs");
  bar.setAttribute("role", "tablist");
  for (const [key, label] of tabs) {
    const b = h("button", "su-tab", label);
    b.type = "button";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(key === tab));
    b.onclick = () => { opts.onTab?.(key); renderSetup(v, root, { ...opts, tab: key }); };
    bar.append(b);
  }
  const body = h("div", "su-body");
  body.setAttribute("role", "tabpanel");
  for (const n of RENDER[tab](v, now)) if (n) body.append(n);
  const scroller = opts.scroller || root;
  const top = scroller.scrollTop;
  root.replaceChildren(bar, body);
  scroller.scrollTop = top;
  return tab;
}

// The header badge's text and state for a status event.
export function badge(st, now = Date.now() / 1000) {
  if (st.setup_kind === "box") {
    const sync = st.setup_sync;
    const parts = [`BOX ${st.setup_label || ""}`];
    if (sync === "syncer-silent") parts.push("syncer stopped");
    else if (sync === "error") parts.push("sync failed");
    else if (sync === "incoming") parts.push("landed beside");
    else if (typeof st.box_sync_ts === "number") parts.push(`synced ${short(st.box_sync_ts, now)}`);
    if (st.box_denied_recent) parts.push(`${st.box_denied_recent} refused`);
    if (st.setup_mem_oom) parts.push("OOM killed");
    else if (typeof st.setup_mem_pct === "number" && st.setup_mem_pct >= 80) parts.push(`mem ${st.setup_mem_pct}%`);
    return { text: parts.join(" · "), state: st.setup_state || "unknown" };
  }
  const parts = ["LOCAL", st.setup_label || st.provider || "model"];
  if (st.backend_reachable === false) parts.push("down");
  else if (typeof st.setup_latency_ms === "number") parts.push(`${st.setup_latency_ms} ms`);
  return { text: parts.join(" · "), state: st.setup_state || "ok" };
}
