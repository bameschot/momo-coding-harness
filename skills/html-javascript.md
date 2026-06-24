# Skill: HTML & JavaScript

## HTML

### Structure
- Use semantic elements: `<nav>`, `<main>`, `<article>`, `<section>`, `<aside>`, `<header>`, `<footer>` — not `<div>` for everything.
- Every page needs `<!DOCTYPE html>`, `<html lang="en">`, `<meta charset="UTF-8">`, and `<meta name="viewport" content="width=device-width, initial-scale=1">`.
- Heading hierarchy matters: one `<h1>` per page, followed by `<h2>`–`<h6>` in order. Do not skip levels.
- `<button>` for actions; `<a href>` for navigation. Never use `<div onclick>` as an interactive element.
- Form inputs need associated `<label for="id">` elements or `aria-label`. Do not rely on placeholder text as a label.

### Attributes and Accessibility
- Images always need `alt`. Use `alt=""` for decorative images; a description for meaningful ones.
- Interactive elements need visible focus styles. Do not remove `:focus` outlines without a replacement.
- Use `aria-*` attributes only when semantic HTML cannot convey the meaning — ARIA supplements HTML, it does not replace it.
- `tabindex="0"` makes an element keyboard-focusable; `tabindex="-1"` removes it from the tab order but allows programmatic focus.
- Test with a screen reader (NVDA, VoiceOver) and keyboard-only navigation before calling a feature complete.

### Forms
```html
<form>
  <label for="email">Email</label>
  <input type="email" id="email" name="email" required autocomplete="email">
  <button type="submit">Submit</button>
</form>
```
- Use appropriate `type` attributes (`email`, `number`, `tel`, `url`, `password`, `search`) — browsers provide built-in validation and mobile keyboard hints.
- `<fieldset>` + `<legend>` groups related inputs (radio buttons, checkboxes).
- Provide clear error messages next to the relevant field, not only at the top of the form.

## JavaScript

### Language
- Use `const` by default; `let` when reassignment is needed; never `var`.
- `===` not `==` everywhere.
- Optional chaining `?.` for nullable property access; nullish coalescing `??` for defaults (not `||`, which also triggers on `0`, `""`, `false`).
- Destructuring: `const { name, age } = user;` / `const [first, ...rest] = items;`
- Template literals over concatenation: `` `Hello, ${name}!` ``

### Functions and Async
```javascript
// async/await over raw Promises — stack traces are readable
async function fetchUser(id) {
    const res = await fetch(`/api/users/${id}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
}

// Error handling
try {
    const user = await fetchUser(1);
} catch (err) {
    console.error("Failed to fetch user:", err);
}

// Parallel requests
const [users, posts] = await Promise.all([fetchUsers(), fetchPosts()]);

// allSettled — results even when some fail
const results = await Promise.allSettled(urls.map(fetch));
const succeeded = results
    .filter(r => r.status === "fulfilled")
    .map(r => r.value);
```

### DOM
```javascript
// Query
const btn = document.querySelector("#submit-btn");
const items = document.querySelectorAll(".item");

// Events — use addEventListener, not onclick attributes
btn.addEventListener("click", handleClick);
// Clean up when the element is removed
btn.removeEventListener("click", handleClick);

// Event delegation — one listener for dynamic content
document.querySelector("#list").addEventListener("click", e => {
    const item = e.target.closest(".item");
    if (item) handleItemClick(item);
});

// Creating elements
const el = document.createElement("li");
el.textContent = "Safe text";            // XSS-safe
el.setAttribute("data-id", "42");
parent.append(el);                       // append vs appendChild — append accepts strings too

// Bulk DOM updates — build in a fragment first to avoid repeated reflows
const frag = document.createDocumentFragment();
items.forEach(item => {
    const li = document.createElement("li");
    li.textContent = item.name;
    frag.append(li);
});
list.append(frag);
```

**Never use `innerHTML` with unsanitized user input — it causes XSS.**

### State Management Patterns

```javascript
// Simple observable state — notify listeners on change
class Store {
    #state;
    #listeners = new Set();

    constructor(initial) { this.#state = initial; }

    get state() { return this.#state; }

    setState(partial) {
        this.#state = { ...this.#state, ...partial };
        this.#listeners.forEach(fn => fn(this.#state));
    }

    subscribe(fn) {
        this.#listeners.add(fn);
        return () => this.#listeners.delete(fn);  // returns unsubscribe function
    }
}

const store = new Store({ count: 0, user: null });
const unsub = store.subscribe(state => render(state));
store.setState({ count: 1 });
unsub();  // clean up
```

### Browser APIs

```javascript
// localStorage / sessionStorage — synchronous, strings only
localStorage.setItem("token", JSON.stringify(payload));
const token = JSON.parse(localStorage.getItem("token") ?? "null");

// IntersectionObserver — lazy loading, infinite scroll
const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
        if (entry.isIntersecting) loadMore();
    });
}, { threshold: 0.1 });
observer.observe(sentinel);

// ResizeObserver — react to element size changes
const ro = new ResizeObserver(entries => {
    for (const entry of entries) {
        adjustLayout(entry.contentRect.width);
    }
});
ro.observe(container);

// AbortController — cancel fetch requests
const controller = new AbortController();
fetch(url, { signal: controller.signal });
setTimeout(() => controller.abort(), 5000);  // cancel after 5s

// URLSearchParams — parse and build query strings
const params = new URLSearchParams(location.search);
const page = params.get("page") ?? "1";
params.set("q", "hello world");
history.pushState({}, "", `?${params}`);
```

### Performance

```javascript
// Debounce — delay execution until input pauses
function debounce(fn, ms) {
    let timer;
    return (...args) => {
        clearTimeout(timer);
        timer = setTimeout(() => fn(...args), ms);
    };
}
window.addEventListener("resize", debounce(handleResize, 150));
input.addEventListener("input", debounce(search, 300));

// Throttle — execute at most once per interval
function throttle(fn, ms) {
    let last = 0;
    return (...args) => {
        const now = Date.now();
        if (now - last >= ms) { last = now; fn(...args); }
    };
}
window.addEventListener("scroll", throttle(updateNavbar, 50));

// requestAnimationFrame for visual updates — synced to browser paint cycle
function animate() {
    updatePositions();
    requestAnimationFrame(animate);
}
requestAnimationFrame(animate);
```

### Modules
```javascript
// ES modules (preferred)
export function add(a, b) { return a + b; }
export default class MyClass { ... }

import MyClass, { add } from "./math.js";
import * as utils from "./utils.js";

// Dynamic import — code splitting
const { render } = await import("./heavy-module.js");
```

Use `<script type="module">` in HTML to get ES module semantics, deferred loading, and strict mode.

### Common Pitfalls
- `this` in callbacks is not the element — use arrow functions or `.bind(this)`.
- `setTimeout` / `setInterval` IDs should be stored and cleared with `clearTimeout` / `clearInterval` when the component unmounts.
- `Array.forEach` does not return a new array; `Array.map` does. `Array.find` returns the first match or `undefined`; `Array.filter` returns all matches.
- `typeof null === "object"` — check for null explicitly: `val !== null && typeof val === "object"`.
- Floating-point arithmetic: `0.1 + 0.2 !== 0.3`. Use integer cents for money.
- Event listeners added but not removed are a common memory leak. Store the reference and call `removeEventListener` when done.
