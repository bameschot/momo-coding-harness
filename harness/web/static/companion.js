// momo, the cat above the input box: the TUI's frames, speech lines and bubble
// layout (harness/companion.py), served in /api/state as `companion`.

import { $ } from "./dom.js";

let F = null;                      // frames, speech pools, bubble size, recap rules
let ctx = () => ({ mode: "", busy: false, enabled: true });

const cat = {
  x: 4, dir: 1, st: "walk", sitTicks: 0, step: 0,
  blink: 0, mewTicks: 0, mew: "", frame: null,
  idle: false,
};

// Idle recap bubbles — companion.RecapPicker's rules (F.recap): new recap lines
// are spoken once each, before canned lines; after that an older one only comes
// back occasionally, and never the same line within repeat_secs.
const recaps = {
  lines: [], queue: [], lastShown: new Map(),
  rule(key, dflt) { return F?.recap?.[key] ?? dflt; },
  update(lines) {
    const fresh = lines.slice(-this.rule("max_new", 5))
      .filter((l) => !this.lines.includes(l) && !this.queue.includes(l));
    this.queue = [...this.queue, ...fresh].filter((l) => lines.includes(l));
    this.lines = [...lines];
  },
  // A line of at most maxLen chars (what fits beside momo right now), or null.
  // A queued line that doesn't fit stays queued for a roomier spot.
  pick(now, maxLen) {
    let line = this.queue.find((l) => l.length <= maxLen);
    if (line !== undefined) {
      this.queue.splice(this.queue.indexOf(line), 1);
    } else {
      if (Math.random() >= this.rule("reuse_chance", 0.25)) return null;
      const repeatMs = this.rule("repeat_secs", 180) * 1000;
      const pool = this.lines.slice(-this.rule("reuse_pool", 5))
        .filter((l) => l.length <= maxLen && now - (this.lastShown.get(l) ?? -Infinity) >= repeatMs);
      if (!pool.length) return null;
      line = pool[Math.floor(Math.random() * pool.length)];
    }
    this.lastShown.set(line, now);
    return line;
  },
};

export function setCompanionFrames(frames) { F = frames; }

// A CompanionEvent: idle state, remembered recap lines, and the idle-recap settings.
export function applyCompanion(ev) {
  cat.idle = ev.idle;
  recaps.update(ev.lines);
  $("#idle-recap").checked = ev.enabled;
  const secs = $("#idle-recap-secs");
  if (document.activeElement !== secs) secs.value = ev.secs;  // don't clobber typing
  secs.disabled = !ev.enabled;
}

// Bubble layout — mirrors walk_max_x / bubble_dir / bubble_room in companion.py.
// The bubble sits beside the cat on its head row: right when facing right, left otherwise.
const bubbleFits = (x, cols, catW, dir, n) =>
  dir < 0 ? x >= n : x + 1 + catW + 1 + n < cols - 1;
const bubbleDir = (x, cols, catW, dir, n) =>
  [dir, -dir].find((d) => bubbleFits(x, cols, catW, d, n)) ?? null;
const bubbleRoom = (x, cols, catW) => Math.max(0, x, cols - 2 - (x + 1 + catW + 1));

// Measured monospace advance of the companion bar, so `cols` matches what fits.
let charW = 0;
function measureCharW(pre) {
  const c = document.createElement("canvas").getContext("2d");
  const cs = getComputedStyle(pre);  // longhands: the `font` shorthand can be "" (Firefox)
  c.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  charW = c.measureText("M".repeat(10)).width / 10 || 6.6;
}

function tick() {
  const c = cat, pre = $("#companion"), { mode, busy, enabled } = ctx();
  if (!F || !enabled || pre.offsetParent === null) return;
  if (!charW) measureCharW(pre);
  const cols = Math.max(20, Math.floor(pre.clientWidth / charW));
  const catW = F.cat_w, maxX = Math.max(0, cols - 2 - catW - (F.bubble_max + 2));
  if (c.st === "walk") {
    c.step ^= 1;
    c.x = Math.max(0, Math.min(c.x + c.dir, maxX));
    if (c.x === 0 || c.x === maxX || Math.random() < 0.02) {
      c.st = "sit";
      c.sitTicks = 50 + Math.floor(Math.random() * 50);
      c.blink = 0;
      // Only lines that fit beside momo here; it turns to face its bubble.
      const room = bubbleRoom(c.x, cols, catW);
      const recap = c.idle ? recaps.pick(Date.now(), room) : null;
      let text = recap;
      if (!text && Math.random() < 0.75) {
        const pool = (F.speech[`${mode}|${busy ? 1 : 0}`] || F.speech_default)
          .filter((t) => t.length <= room);
        if (pool.length) text = pool[Math.floor(Math.random() * pool.length)];
      }
      if (text) {
        c.mew = text;
        // Longer lines stay up longer; recaps a little longer still.
        c.mewTicks = 18 + Math.floor(Math.random() * 15) + Math.floor(text.length / 2) + (recap ? 10 : 0);
        c.sitTicks = Math.max(c.sitTicks, c.mewTicks + 10);
        c.dir = bubbleDir(c.x, cols, catW, c.dir, text.length) ?? c.dir;
      }
    }
    if (c.blink > 0) {
      c.blink--;
      c.frame = c.dir > 0 ? F.walk_right_blink : F.walk_left_blink;
    } else {
      c.frame = (c.dir > 0 ? F.walk_right : F.walk_left)[c.step];
      if (Math.random() < 0.03) c.blink = 2;
    }
  } else {
    if (--c.sitTicks <= 0) {
      c.dir = c.x <= 2 ? 1 : c.x >= maxX - 2 ? -1 : (Math.random() < 0.5 ? -1 : 1);
      c.st = "walk";
      c.mewTicks = 0;
    }
    const sit = c.dir < 0 ? F.sit_left : F.sit;
    c.frame = sit[Math.random() < 0.08 ? 1 : 0];
    if (c.mewTicks > 0) c.mewTicks--;
  }
  const rows = c.frame.map((l) => " ".repeat(c.x + 1) + l);
  if (c.mewTicks > 0 && c.st === "sit") {
    if (c.dir < 0) {
      const t = c.mew.startsWith("< ") ? c.mew.slice(2) + " >" : c.mew;
      const mx = c.x + 1 - t.length - 1;
      if (mx >= 0) rows[1] = " ".repeat(mx) + t + " " + c.frame[1];
    } else if (bubbleFits(c.x, cols, catW, 1, c.mew.length)) {
      rows[1] = rows[1].padEnd(c.x + 1 + catW) + " " + c.mew;
    }
  }
  pre.textContent = rows.join("\n");
}

// getContext() → {mode, busy (working, not waiting for you), enabled (View → Companion)}.
export function startCompanion(getContext) {
  ctx = getContext;
  addEventListener("resize", () => { charW = 0; });
  setInterval(tick, 120);
}
