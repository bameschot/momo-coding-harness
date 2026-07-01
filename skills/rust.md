# Skill: Rust

## Core rules

- Use `Result<T, E>` for recoverable errors; `panic!` only for real bugs/invariant violations.
- Propagate errors with `?`, not `match`. Add context: `op().context("reading config")?`.
- Use `anyhow` in application code, `thiserror` in libraries (callers match on variants).
- Avoid `.clone()` to satisfy the borrow checker — restructure ownership or use references first.
- Use `?` in production; reserve `unwrap()` / `expect()` for tests and prototypes.
- Take `&str` parameters, not `&String`. Return owned `String` only when ownership is needed.
- Run `cargo clippy -- -D warnings` and `cargo fmt`. Clippy catches most common mistakes.

## Ownership (the model this is built on)

- One owner per value; dropped when the owner leaves scope.
- Borrow rule: many `&T` OR one `&mut T`, never both at once. Enforced at compile time.
- Prefer combinators over match for simple cases: `opt.map(...)`, `opt.unwrap_or(...)`, `opt.and_then(...)`.
- `if let` / `while let` to handle a single variant.

## Common mistakes

- Integer overflow panics in debug, wraps in release — use `checked_add` / `saturating_add` when overflow is possible.
- Do NOT hold a `MutexGuard` across an `.await`. Use `tokio::sync::Mutex` and drop the guard before awaiting.
- `iter()` yields `&T`, `into_iter()` yields `T` (consumes), `iter_mut()` yields `&mut T`.

```rust
// Collect into Result — stops at the first error
let nums: Result<Vec<i32>, _> = strings.iter().map(|s| s.parse::<i32>()).collect();

// Shared mutable state: single-thread Rc<RefCell<T>>, threads Arc<Mutex<T>>
```

## Testing

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adds() { assert_eq!(add(2, 3), 5); }

    #[test]
    #[should_panic(expected = "divide by zero")]
    fn panics_on_zero() { divide(5, 0); }
}
```

- Tests returning `Result<(), Box<dyn std::error::Error>>` can use `?`.
- Integration tests live in `tests/`; they see only the public API.
