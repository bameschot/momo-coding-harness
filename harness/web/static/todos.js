// The model's todo list (the todos tool, coding and momo modes): a panel above
// the input that shows the list while an item is open and updates in place
// from each status, and the `todo 1/3` chip in the header.  Expanded or
// collapsed is remembered per browser.
import { $, el, loadJSON, saveJSON } from "./dom.js";

const hooks = { send: async () => false };
const MARKS = { pending: "○", in_progress: "▸", done: "✓" };
let items = [];
let shown = "";       // the list last rendered, so a status that didn't change it costs nothing
let open = loadJSON("momo.todosOpen", true);

const hasOpen = () => items.some((t) => t.status !== "done");

function render() {
  const panel = $("#todos-panel");
  panel.hidden = !hasOpen();
  if (panel.hidden) return;
  const done = items.filter((t) => t.status === "done").length;
  $("#todos-count").textContent = `${done}/${items.length}`;
  const current = items.find((t) => t.status === "in_progress") || items.find((t) => t.status === "pending");
  $("#todos-current").textContent = open ? "" : current?.text || "";
  $("#todos-head").setAttribute("aria-expanded", String(open));
  panel.classList.toggle("collapsed", !open);
  const list = $("#todos-list");
  list.hidden = !open;
  list.replaceChildren(...items.map((t) => {
    const li = el("li", `todo ${t.status}`);
    li.append(el("span", "todo-mark", MARKS[t.status] || MARKS.pending), el("span", "todo-text", t.text));
    return li;
  }));
}

export function applyTodosStatus(s) {
  const next = s.todos || [];
  const sig = JSON.stringify(next);
  const chip = $("#todos-chip");
  chip.hidden = !s.todos_progress;
  chip.textContent = `todo ${s.todos_progress || ""}`;
  chip.title = next.length && next.every((t) => t.status === "done")
    ? "All done — click to show the list (/todos)" : "Show or hide the todo list";
  if (sig === shown) return;
  shown = sig;
  items = next;
  render();
}

function toggle() {
  open = !open;
  saveJSON("momo.todosOpen", open);
  render();
}

export function initTodos(h) {
  Object.assign(hooks, h);
  $("#todos-head").onclick = toggle;
  // With an item open the chip toggles the panel; a finished list has no panel,
  // so the chip prints it in the chat instead.
  $("#todos-chip").onclick = () => (hasOpen() ? toggle() : hooks.send("/todos"));
}
