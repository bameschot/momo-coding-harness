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

// Briefly swap a button's label (its last text span, or the whole button) — the
// one copy/attach feedback everywhere.  ok=false marks a failure (no green).
export function flash(btn, text, ok = true) {
  const target = btn.querySelector(":scope > span:last-child") || btn;
  const orig = target.dataset.label || target.textContent;
  target.dataset.label = orig;
  target.textContent = text;
  btn.classList.toggle("done", ok);
  clearTimeout(btn._flashTimer);
  btn._flashTimer = setTimeout(() => { target.textContent = orig; btn.classList.remove("done"); }, 1400);
}

// Copy text and say so on the button that asked.
export async function copyWithFeedback(btn, text) {
  try { await copyText(text); flash(btn, "Copied ✓"); } catch { flash(btn, "Copy failed", false); }
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

// ── transient notes ───────────────────────────────────────────────────────────
// One place for messages that are not part of the conversation (a setting the
// server refused, a file that can't be previewed, notification permissions).
// kind: "info" | "error".  Click a note to dismiss it.
export function toast(msg, kind = "info", ms = 4500) {
  const box = $("#toast");
  const n = el("div", `toast ${kind}`, msg);
  n.setAttribute("role", kind === "error" ? "alert" : "status");
  n.onclick = () => n.remove();
  box.append(n);
  while (box.childElementCount > 3) box.firstElementChild.remove();
  setTimeout(() => n.remove(), ms);
}

// ── inline confirmation ───────────────────────────────────────────────────────
// Show "question [yes] [Cancel]" in `host` in place of its controls until one is
// clicked; resolves true for yes.  Used instead of the browser's blocking
// confirm().  The controls are hidden, not removed: code that updates them by id
// (a status event arriving meanwhile) still finds them.
export function confirmInline(host, question, yesLabel = "Delete", danger = true) {
  return new Promise((resolve) => {
    const kids = [...host.children].filter((k) => !k.classList.contains("inline-confirm"));
    const wasHidden = kids.map((k) => k.hidden);
    const box = el("div", "inline-confirm");
    const yes = el("button", `btn small ${danger ? "danger" : "primary"}`, yesLabel);
    const no = el("button", "btn small", "Cancel");
    yes.type = no.type = "button";
    const done = (ok) => {
      box.remove();
      kids.forEach((k, i) => { k.hidden = wasHidden[i]; });
      resolve(ok);
    };
    yes.onclick = () => done(true);
    no.onclick = () => done(false);
    box.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } });
    box.append(el("span", "", question), el("span", "spacer"), yes, no);
    kids.forEach((k) => { k.hidden = true; });
    host.append(box);
    yes.focus();
  });
}

// ── layers (popovers, drawers): focus in on open, back to the opener on close ──
const openers = new WeakMap();
const FOCUSABLE = "input:not([disabled]), select:not([disabled]), textarea:not([disabled]), " +
                  "button:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])";

export function openLayer(layer, opener = document.activeElement, focusFirst = true) {
  openers.set(layer, opener);
  layer.hidden = false;
  if (!layer.hasAttribute("tabindex")) layer.tabIndex = -1;   // focusable while it loads
  if (!focusFirst) return;
  // After the browser has laid it out: hidden children can't take focus.
  requestAnimationFrame(() => {
    const first = [...layer.querySelectorAll(FOCUSABLE)].find((e) => e.offsetParent !== null);
    (first || layer).focus({ preventScroll: true });
  });
}

export function closeLayer(layer) {
  if (layer.hidden) return;
  const hadFocus = layer.contains(document.activeElement) || document.activeElement === document.body;
  layer.hidden = true;
  const opener = openers.get(layer);
  openers.delete(layer);
  if (hadFocus && opener?.isConnected && opener.offsetParent !== null) opener.focus({ preventScroll: true });
}

// ── resizable drawers ─────────────────────────────────────────────────────────
// A grip on the drawer's inner edge ("right" for a left drawer, "left" for a
// right one); the width is remembered under storageKey.  Double-click resets.
export function makeResizable(drawer, edge, storageKey, min) {
  const grip = el("div", `drawer-resize ${edge}-edge`);
  grip.setAttribute("role", "separator");
  grip.setAttribute("aria-orientation", "vertical");
  grip.setAttribute("aria-label", "Resize panel");
  grip.title = "Drag to resize · double-click to reset";
  drawer.append(grip);
  const setW = (w) => { drawer.style.width = `${Math.round(Math.max(min, Math.min(w, innerWidth)))}px`; };
  const saved = loadJSON(storageKey, 0);
  if (saved) setW(saved);
  grip.onpointerdown = (e) => {
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    grip.classList.add("dragging");
    grip.onpointermove = (m) => setW(edge === "right" ? m.clientX : innerWidth - m.clientX);
    grip.onpointerup = () => {
      grip.onpointermove = grip.onpointerup = null;
      grip.classList.remove("dragging");
      saveJSON(storageKey, Math.round(drawer.getBoundingClientRect().width));
    };
  };
  grip.ondblclick = () => { drawer.style.removeProperty("width"); saveJSON(storageKey, undefined); };
}
