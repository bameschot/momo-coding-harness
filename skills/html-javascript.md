# Skill: HTML & JavaScript

## HTML core rules

- Use semantic elements (`<nav>`, `<main>`, `<article>`, `<header>`, `<footer>`), not `<div>` for everything.
- Every page needs `<!DOCTYPE html>`, `<html lang="en">`, `<meta charset="UTF-8">`, and the viewport meta.
- `<button>` for actions, `<a href>` for navigation. NEVER use `<div onclick>` as an interactive control.
- Every input needs a `<label for="id">` (or `aria-label`). Placeholder text is not a label.
- Every image needs `alt` (`alt=""` for decorative). Keep one `<h1>`; don't skip heading levels.
- Use `aria-*` only when semantic HTML cannot convey meaning.

## JavaScript core rules

- `const` by default, `let` when reassigned, NEVER `var`.
- `===` not `==`.
- `?.` for nullable access; `??` for defaults (not `||`, which fires on `0`/`""`/`false`).
- Prefer `async`/`await` over raw `.then()` chains.
- NEVER put unsanitised user input into `innerHTML` — that is XSS. Use `textContent`.

```javascript
async function fetchUser(id) {
  const res = await fetch(`/api/users/${id}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
const [users, posts] = await Promise.all([fetchUsers(), fetchPosts()]);
```

## DOM

```javascript
const btn = document.querySelector("#submit");
btn.addEventListener("click", handleClick);   // not onclick attributes

// Event delegation — one listener for dynamic children
list.addEventListener("click", e => {
  const item = e.target.closest(".item");
  if (item) handleItemClick(item);
});

// Safe insertion
const li = document.createElement("li");
li.textContent = userInput;   // XSS-safe
parent.append(li);
```

## Common mistakes

- `this` in a callback is not the element — use arrow functions.
- Store `setTimeout`/`setInterval` IDs and clear them; remove event listeners you added (both are leak sources).
- `map` returns a new array, `forEach` does not. `find` returns first match or `undefined`.
- `typeof null === "object"` — check `val !== null` first.
- Floating point: `0.1 + 0.2 !== 0.3`. Use integer cents for money.

## Performance helpers

- Debounce input handlers (search, resize); throttle scroll handlers.
- Use `requestAnimationFrame` for visual updates. Build bulk DOM in a `DocumentFragment` before appending.
