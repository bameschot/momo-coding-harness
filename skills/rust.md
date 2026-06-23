# Skill: Rust

## Ownership and borrowing

- Each value has exactly one owner. When the owner goes out of scope, the value is dropped.
- Move vs copy: types that implement `Copy` (integers, booleans, `char`, tuples of `Copy` types) are copied on assignment. Everything else is moved — the original binding becomes invalid.
- Borrow rules enforced at compile time: you can have any number of shared (`&T`) references OR exactly one mutable (`&mut T`) reference, never both at the same time.
- Lifetimes express that a reference must not outlive the data it points to. The compiler infers most lifetimes; explicit annotations (`'a`) are only needed when the compiler cannot infer them.

## Error handling

- Use `Result<T, E>` for recoverable errors and `panic!` for unrecoverable programming errors (invariant violations, index out of bounds in tests).
- The `?` operator propagates errors up: `let val = operation()?;` — equivalent to `match ... { Ok(v) => v, Err(e) => return Err(e.into()) }`.
- Use `anyhow::Error` (via the `anyhow` crate) for application code where error context matters more than type. Use typed errors (`thiserror`) for library code where callers need to match on variants.
- Always add context: `operation().context("while reading config")?`.

## Idiomatic patterns

```rust
// Option combinators — prefer over match for simple cases
let doubled = opt.map(|v| v * 2);
let with_default = opt.unwrap_or(0);
let fallback = opt.unwrap_or_else(|| expensive_default());
let chained = opt.and_then(|v| if v > 0 { Some(v) } else { None });

// Iterators — prefer over manual index loops
let sum: i32 = vec.iter().filter(|&&x| x > 0).sum();
let mapped: Vec<_> = items.iter().map(|x| x.transform()).collect();

// Struct update syntax
let updated = Config { timeout: 30, ..config };

// Pattern matching with guards
match value {
    n if n < 0 => println!("negative"),
    0 => println!("zero"),
    n => println!("positive: {n}"),
}
```

## Strings

- `String` is an owned, growable UTF-8 string on the heap. `&str` is a borrowed string slice (a view into a `String` or a string literal).
- Function parameters that don't need ownership should take `&str`, not `&String` — `&String` coerces to `&str` automatically, but `&str` is more general.
- `format!("{} {}", a, b)` for string construction.
- `String::from("literal")` or `"literal".to_string()` to create owned strings.

## Common pitfalls

- Cloning excessively to satisfy the borrow checker is a smell — try restructuring ownership or using references before reaching for `.clone()`.
- `unwrap()` and `expect()` panic on `None`/`Err` — fine in tests and prototypes, but propagate errors with `?` in production code.
- Integer overflow panics in debug mode, wraps silently in release mode. Use `checked_add`, `saturating_add`, or `wrapping_add` when overflow is possible.
- `Vec` and `HashMap` allocate on the heap — for hot paths, consider `SmallVec`, stack arrays, or arena allocators.
- Avoid holding a `MutexGuard` across an `await` point in async code — it may deadlock. Use `tokio::sync::Mutex` in async contexts.

## Cargo conventions

- `Cargo.lock` committed for binaries, not for libraries.
- `cargo clippy -- -D warnings` as a CI step — clippy catches many common mistakes.
- Use workspace `[dependencies]` to unify versions across crates in a monorepo.
- Feature flags (`[features]`) for optional functionality — keep the default feature set minimal.
