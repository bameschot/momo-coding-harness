# Skill: Rust

## Ownership and borrowing

- Each value has exactly one owner. When the owner goes out of scope, the value is dropped.
- Move vs copy: types that implement `Copy` (integers, booleans, `char`, tuples of `Copy` types) are copied on assignment. Everything else is moved — the original binding becomes invalid.
- Borrow rules enforced at compile time: any number of shared (`&T`) references OR exactly one mutable (`&mut T`) reference, never both at the same time.
- Lifetimes express that a reference must not outlive the data it points to. The compiler infers most lifetimes; explicit annotations (`'a`) are only needed when the compiler cannot infer them.

## Error handling

- Use `Result<T, E>` for recoverable errors and `panic!` for unrecoverable programming errors (invariant violations, out-of-bounds in tests).
- The `?` operator propagates errors: `let val = operation()?;` — equivalent to `match ... { Ok(v) => v, Err(e) => return Err(e.into()) }`.
- Use `anyhow::Error` for application code where context matters more than the error type. Use `thiserror` for library code where callers need to match on variants.
- Always add context: `operation().context("while reading config")?`.

```rust
use anyhow::{Context, Result};
use thiserror::Error;

// Library error — callers can match on variants
#[derive(Error, Debug)]
pub enum ConfigError {
    #[error("missing field: {0}")]
    MissingField(&'static str),
    #[error("invalid value for {field}: {value}")]
    InvalidValue { field: &'static str, value: String },
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

// Application error — use anyhow for rich context chains
fn load_config(path: &str) -> Result<Config> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("reading config from {path}"))?;
    let cfg: Config = toml::from_str(&text)
        .context("parsing config TOML")?;
    Ok(cfg)
}
```

## Traits

```rust
// Define a trait
trait Summary {
    fn summarize(&self) -> String;
    fn preview(&self) -> String {         // default implementation
        format!("{}...", &self.summarize()[..50])
    }
}

// Implement for your type
struct Article { title: String, body: String }
impl Summary for Article {
    fn summarize(&self) -> String {
        format!("{}: {}", self.title, &self.body[..100])
    }
}

// Trait bounds — generic function that accepts any Summary
fn print_summary(item: &impl Summary) {           // impl Trait syntax
    println!("{}", item.summarize());
}
fn print_summary_generic<T: Summary>(item: &T) {  // generic syntax — equivalent
    println!("{}", item.summarize());
}

// Multiple bounds
fn process<T: Summary + Clone + Send>(item: T) { ... }

// Where clause — cleaner for complex bounds
fn complex<T, U>(t: T, u: U) -> String
where
    T: Summary + Clone,
    U: std::fmt::Display + std::fmt::Debug,
{ ... }

// Returning a trait — use `impl Trait` for concrete types, `Box<dyn Trait>` for dynamic dispatch
fn make_summary() -> impl Summary { Article { ... } }
fn make_dynamic(flag: bool) -> Box<dyn Summary> {
    if flag { Box::new(Article { ... }) } else { Box::new(Tweet { ... }) }
}
```

## Generics and smart pointers

```rust
// Generic struct
struct Pair<T> {
    first: T,
    second: T,
}
impl<T: Clone + std::fmt::Display> Pair<T> {
    fn show(&self) { println!("{} {}", self.first, self.second); }
}

// Smart pointers
use std::rc::Rc;
use std::sync::Arc;
use std::cell::RefCell;

// Rc<T> — shared ownership, single-threaded only
let shared = Rc::new(vec![1, 2, 3]);
let clone = Rc::clone(&shared);  // increments ref count, does not copy data

// Arc<T> — shared ownership, thread-safe (atomic ref counting)
let arc_shared = Arc::new(vec![1, 2, 3]);

// RefCell<T> — interior mutability: enforces borrow rules at runtime, not compile time
let data = RefCell::new(vec![1, 2, 3]);
data.borrow_mut().push(4);   // panics if already borrowed mutably

// Common combination: Rc<RefCell<T>> for shared mutable state (single-threaded)
let shared_mutable = Rc::new(RefCell::new(0));

// Box<T> — heap allocation; use for recursive types and large stack values
enum List {
    Cons(i32, Box<List>),
    Nil,
}
```

## Closures

```rust
// Closures capture their environment
let x = 5;
let add_x = |n| n + x;            // captures x by reference
let add_x_owned = move |n| n + x; // captures x by value (needed for threads)

// Fn, FnMut, FnOnce
fn apply<F: Fn(i32) -> i32>(f: F, val: i32) -> i32 { f(val) }
fn apply_mut<F: FnMut(i32) -> i32>(mut f: F, val: i32) -> i32 { f(val) }
fn consume<F: FnOnce() -> String>(f: F) -> String { f() }

// Common iterator methods accept closures
let doubled: Vec<i32> = vec![1, 2, 3].iter().map(|&x| x * 2).collect();
let evens: Vec<i32> = (0..10).filter(|&x| x % 2 == 0).collect();
let sum: i32 = vec![1, 2, 3].iter().sum();
let product: i32 = vec![1, 2, 3, 4].iter().fold(1, |acc, &x| acc * x);
```

## Async with Tokio

```rust
use tokio::time::{sleep, Duration};
use tokio::sync::{mpsc, Mutex};
use std::sync::Arc;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let result = fetch_data("https://example.com").await?;
    println!("{result}");
    Ok(())
}

// Run tasks concurrently
async fn parallel_fetch(urls: &[String]) -> Vec<String> {
    let handles: Vec<_> = urls.iter()
        .map(|url| tokio::spawn(fetch_data(url.clone())))
        .collect();
    let mut results = Vec::new();
    for h in handles {
        results.push(h.await.unwrap().unwrap_or_default());
    }
    results
}

// tokio::join! — wait for multiple futures at once
let (a, b) = tokio::join!(fetch_a(), fetch_b());

// Channel — communicate between tasks
let (tx, mut rx) = mpsc::channel::<String>(32);
tokio::spawn(async move {
    tx.send("hello".to_string()).await.unwrap();
});
while let Some(msg) = rx.recv().await {
    println!("{msg}");
}

// Shared mutable state across tasks
let counter = Arc::new(Mutex::new(0u64));
let c = Arc::clone(&counter);
tokio::spawn(async move {
    *c.lock().await += 1;
});
```

## Idiomatic patterns

```rust
// Option combinators — prefer over match for simple transformations
let doubled = opt.map(|v| v * 2);
let with_default = opt.unwrap_or(0);
let fallback = opt.unwrap_or_else(|| expensive_default());
let chained = opt.and_then(|v| if v > 0 { Some(v) } else { None });
let or_else = opt.or_else(|| alternative_source());

// Collect into Result — stops at first error
let results: Result<Vec<i32>, _> = strings.iter()
    .map(|s| s.parse::<i32>())
    .collect();

// Struct update syntax
let updated = Config { timeout: 30, ..config };

// Pattern matching with guards
match value {
    n if n < 0 => println!("negative"),
    0           => println!("zero"),
    n           => println!("positive: {n}"),
}

// if let — match a single variant
if let Some(val) = maybe_value {
    use_val(val);
}

// while let — iterate while a pattern matches
while let Some(item) = queue.pop_front() {
    process(item);
}
```

## Strings

- `String` is an owned, growable UTF-8 string on the heap. `&str` is a borrowed string slice.
- Function parameters that don't need ownership should take `&str`, not `&String` — `&String` coerces to `&str` automatically, but `&str` is more general.
- `format!("{} {}", a, b)` for string construction.
- `String::from("literal")` or `"literal".to_string()` to create owned strings.

```rust
// String manipulation
let s = String::from("hello world");
let upper = s.to_uppercase();
let words: Vec<&str> = s.split_whitespace().collect();
let trimmed = "  hello  ".trim();
let replaced = s.replace("world", "Rust");
let starts = s.starts_with("hello");
let contains = s.contains("world");

// String formatting
let msg = format!("{name} is {age} years old");
let padded = format!("{:>10}", "right");   // right-align in 10 chars
let hex = format!("{:#010x}", 42);         // 0x0000002a
```

## Testing

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_basic() {
        assert_eq!(add(2, 3), 5);
        assert_ne!(add(2, 2), 5);
    }

    #[test]
    #[should_panic(expected = "divide by zero")]
    fn test_panic() {
        divide(5, 0);
    }

    // Integration test: in tests/ directory, not inside src/
    // tests/integration_test.rs — has access to only public API
}

// Test with Result return — ? operator works in tests
#[test]
fn test_parse() -> Result<(), Box<dyn std::error::Error>> {
    let val: i32 = "42".parse()?;
    assert_eq!(val, 42);
    Ok(())
}
```

## Common pitfalls

- Cloning excessively to satisfy the borrow checker is a smell — try restructuring ownership or using references before reaching for `.clone()`.
- `unwrap()` and `expect()` panic on `None`/`Err` — fine in tests and prototypes, but use `?` in production code.
- Integer overflow panics in debug mode, wraps silently in release mode. Use `checked_add`, `saturating_add`, or `wrapping_add` when overflow is possible.
- Do not hold a `MutexGuard` across an `await` point — use `tokio::sync::Mutex` in async contexts, and drop the guard before awaiting.
- `Vec::iter()` yields `&T`, `Vec::into_iter()` yields `T` (consuming), `Vec::iter_mut()` yields `&mut T`.

## Cargo conventions

- `Cargo.lock` committed for binaries, not for libraries.
- `cargo clippy -- -D warnings` as a CI step — clippy catches many common mistakes.
- `cargo fmt` for formatting; `cargo test` runs all tests including doc tests.
- Use workspace `[dependencies]` to unify versions across crates in a monorepo.
- Feature flags (`[features]`) for optional functionality — keep the default feature set minimal.
- `cargo build --release` for production; the debug build is significantly slower.
