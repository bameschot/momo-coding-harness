# Skill: HTML & JavaScript

## HTML

### Structure
- Use semantic elements: `<nav>`, `<main>`, `<article>`, `<section>`, `<aside>`, `<header>`, `<footer>` — not `<div>` for everything.
- Every page needs `<!DOCTYPE html>`, `<html lang="en">`, `<meta charset="UTF-8">`, and `<meta name="viewport" content="width=device-width, initial-scale=1">`.
- Heading hierarchy matters: one `<h1>` per page, followed by `<h2>`–`<h6>` in order. Do not skip levels.
- `<button>` for actions; `<a href>` for navigation. Never use `<div onclick>` as an interactive element.
- Form inputs need associated `<label for="id">` elements or `aria-label`. Do not rely on placeholder text as a label.

### Attributes and accessibility
- Images always need `alt`. Use `alt=""` for decorative images; use a description for meaningful ones.
- Interactive elements need visible focus styles. Do not remove `:focus` outlines without a replacement.
- Use `aria-*` attributes only when semantic HTML cannot convey the meaning — ARIA supplements HTML, it does not replace it.
- `tabindex="0"` makes an element keyboard-focusable; `tabindex="-1"` removes it from the tab order but allows programmatic focus.

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

## JavaScript

### Language
- Use `const` by default; `let` when reassignment is needed; never `var`.
- `===` not `==` everywhere.
- Optional chaining `?.` for nullable property access; nullish coalescing `??` for defaults (not `||`, which also triggers on `0`, `""`, `false`).
- Destructuring: `const { name, age } = user;` / `const [first, ...rest] = items;`
- Template literals over concatenation: `` `Hello, ${name}!` ``

### Functions and async
```javascript
// Prefer arrow functions for callbacks
const doubled = nums.map(n => n * 2);

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
```

### DOM
```javascript
// Query
const btn = document.querySelector("#submit-btn");
const items = document.querySelectorAll(".item");

// Events — use addEventListener, not onclick attributes
btn.addEventListener("click", handleClick);

// Delegation for dynamic content
document.querySelector("#list").addEventListener("click", e => {
    if (e.target.matches(".item")) handleItemClick(e.target);
});

// Creating elements
const el = document.createElement("li");
el.textContent = "Safe text";          // XSS-safe
el.innerHTML = sanitize(htmlString);   // only when HTML is needed, sanitize first
parent.append(el);

// Removing
el.remove();
```

**Never use `innerHTML` with unsanitized user input — it causes XSS.**

### Modules
```javascript
// ES modules (preferred)
export function add(a, b) { return a + b; }
export default class MyClass { ... }

import MyClass, { add } from "./math.js";
import * as utils from "./utils.js";
```

Use `<script type="module">` in HTML to get ES module semantics, deferred loading, and strict mode by default.

### Events and cleanup
- Remove event listeners when elements are removed to avoid memory leaks: store the handler reference and call `removeEventListener`.
- Debounce expensive handlers on `resize`, `scroll`, and `input`:
```javascript
function debounce(fn, ms) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}
window.addEventListener("resize", debounce(handleResize, 150));
```

### Common pitfalls
- `this` in callbacks is not the element — use arrow functions or `.bind(this)` when you need the outer context.
- `setTimeout` / `setInterval` IDs should be stored and cleared with `clearTimeout` / `clearInterval` when the component unmounts.
- `Array.forEach` does not return a new array; `Array.map` does. `Array.find` returns the first match or `undefined`; `Array.filter` returns all matches.
- `typeof null === "object"` — check for null explicitly: `val !== null && typeof val === "object"`.
- Floating-point arithmetic: `0.1 + 0.2 !== 0.3`. Use integer cents for money; compare with a tolerance for measurements.
