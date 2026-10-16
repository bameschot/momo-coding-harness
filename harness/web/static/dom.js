// DOM and browser helpers shared by the web UI's modules.

// An element by CSS selector.  `#id` lookups are cached: every id'd element is in
// index.html and is never replaced, and a status event (several a second while a
// reply streams) touches dozens of them.
const byId = new Map();
export function $(sel) {
  if (/^#[\w-]+$/.test(sel)) {
    let e = byId.get(sel);
    if (!e?.isConnected) {
      e = document.querySelector(sel);
      if (e) byId.set(sel, e);
    }
    return e;
  }
  return document.querySelector(sel);
}

export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// An icon from the SVG sprite in index.html (same size and stroke everywhere).
export function icon(name, cls = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

// localStorage, which throws in some private modes and when storage is full.
export function loadJSON(key, dflt) {
  try { return JSON.parse(localStorage.getItem(key)) ?? dflt; } catch { return dflt; }
}
export function saveJSON(key, value) {
  try {
    if (value === undefined) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch { /* private mode */ }
}

export async function copyText(text) {
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

// Briefly swap a button's label (its last text span, or the whole button).
export function flash(btn, text) {
  const target = btn.querySelector(":scope > span:last-child") || btn;
  const orig = target.dataset.label || target.textContent;
  target.dataset.label = orig;
  target.textContent = text;
  setTimeout(() => { target.textContent = orig; }, 1400);
}

// Bytes as net.format_size writes them (same units and rounding), without the
// space, so a settings field shows a value /net-max-bytes and /index-max-mem take.
export function formatSize(n) {
  for (const [unit, size] of [["GB", 1024 ** 3], ["MB", 1024 ** 2], ["KB", 1024]]) {
    if (n >= size) {
      const v = n / size;
      return (v >= 10 || v === Math.floor(v)) ? `${Math.round(v)}${unit}` : `${v.toFixed(1)}${unit}`;
    }
  }
  return `${n}`;
}
