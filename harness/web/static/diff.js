// A file diff as the conversation shows it: header (compact or git style), a
// line-number gutter, word-level marks, and the first `max` lines with a
// "show more".  ev: {path, dst?, op, is_new?, added, removed, body: [[kind,
// old_no, new_no, text]]}, as the harness sends a diff event (diff.py).

import { el } from "./dom.js";

// onChanges: when given, the header gets a button that opens the Changes drawer.
export function diffBox(ev, { style = "compact", link = (p) => el("span", "", p), max = 40,
                              onChanges = null } = {}) {
  const box = el("div", "diff");
  if (ev.op === "move") {
    const h = el("div", "diff-head");
    h.append("± renamed ", ev.path, " → ", link(ev.dst));
    box.append(h);
    return box;
  }
  const head = el("div", "diff-head");
  const pathEl = ev.op === "delete" ? el("span", "", ev.path) : link(ev.path);
  if (style === "git") {
    head.classList.add("git");
    const first = el("div", "", "diff --git ");
    first.append(pathEl);
    head.append(first,
                el("div", "minus", `--- ${ev.is_new ? "/dev/null" : "a/" + ev.path}`),
                el("div", "plus", `+++ ${ev.op === "delete" ? "/dev/null" : "b/" + ev.path}`));
  } else {
    const name = el("span", "", "± ");
    name.append(pathEl);
    head.append(name);
    const stat = el("span", "stat");
    if (ev.is_new) stat.innerHTML = `(new file <span class="add">+${ev.added}</span>)`;
    else if (ev.op === "delete") stat.innerHTML = `(deleted <span class="del">−${ev.removed}</span>)`;
    else stat.innerHTML = `(<span class="add">+${ev.added}</span> <span class="del">−${ev.removed}</span>)`;
    head.append(stat);
  }
  if (onChanges) {
    const b = el("button", "link-btn diff-changes", "changes");
    b.type = "button";
    b.title = "Open in the Changes drawer: every change since the last keep, with undo";
    b.onclick = (e) => { e.stopPropagation(); onChanges(); };
    (style === "git" ? head.firstChild : head).append(b);
  }
  box.append(head);

  const body = el("div", "diff-body");
  const numW = Math.max(1, ...ev.body.flatMap(([, o, n]) => [o, n]).filter((x) => x != null).map((x) => String(x).length));
  const fmt = (n) => (n == null ? "" : String(n)).padStart(numW);
  const words = pairWords(ev.body);
  const addLines = (from, to) => {
    for (let i = from; i < to; i++) {
      const [kind, o, n, text] = ev.body[i];
      const row = el("div", `dl ${kind}`);
      const txt = el("span", "txt");
      const parts = words.get(i);
      if (parts) {
        txt.append(text[0]);
        for (const [piece, changed] of parts) txt.append(changed ? el("span", "dw", piece) : piece);
      } else txt.textContent = text;
      row.append(el("span", "gut", `${fmt(o)} ${fmt(n)}`), txt);
      body.append(row);
    }
  };
  addLines(0, Math.min(max, ev.body.length));
  box.append(body);
  if (ev.body.length > max) {
    const more = el("button", "link-btn more", `show ${ev.body.length - max} more diff lines`);
    more.type = "button";
    more.onclick = () => { addLines(max, ev.body.length); more.remove(); };
    box.append(more);
  }
  return box;
}


// Word-level changes: where k removed lines are followed directly by k added
// ones, line j of each pair is compared token by token and the tokens that
// differ are marked.  index in body → [[text, changed], …] (without the +/-).
const WORD_RX = /\w+|\s+|[^\w\s]/g;
function pairWords(body) {
  const out = new Map();
  for (let i = 0; i < body.length;) {
    if (body[i][0] !== "del") { i++; continue; }
    let d = i;
    while (d < body.length && body[d][0] === "del") d++;
    let a = d;
    while (a < body.length && body[a][0] === "add") a++;
    if (a - d === d - i) {
      for (let k = 0; k < d - i; k++) {
        const parts = tokenDiff(body[i + k][3].slice(1), body[d + k][3].slice(1));
        if (parts) { out.set(i + k, parts[0]); out.set(d + k, parts[1]); }
      }
    }
    i = Math.max(a, i + 1);
  }
  return out;
}
function tokenDiff(a, b) {
  const x = a.match(WORD_RX) || [], y = b.match(WORD_RX) || [];
  if (!x.length || !y.length || x.length * y.length > 160_000) return null;
  // LCS table, then walk it to mark what is not shared.
  const m = x.length, n = y.length;
  const L = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    }
  }
  const keepX = new Array(m).fill(false), keepY = new Array(n).fill(false);
  for (let i = 0, j = 0; i < m && j < n;) {
    if (x[i] === y[j]) { keepX[i++] = true; keepY[j++] = true; }
    else if (L[i + 1][j] >= L[i][j + 1]) i++;
    else j++;
  }
  // Lines that share little are a rewrite: marking every word says nothing.
  const shared = keepX.filter(Boolean).length;
  if (shared < 0.4 * Math.max(m, n)) return null;
  const merge = (toks, keep) => {
    const parts = [];
    toks.forEach((t, i) => {
      const changed = !keep[i] && t.trim() !== "";
      const last = parts[parts.length - 1];
      if (last && last[1] === changed) last[0] += t;
      else parts.push([t, changed]);
    });
    return parts;
  };
  return [merge(x, keepX), merge(y, keepY)];
}
