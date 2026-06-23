# Skill: Kotlin Programming

## Style and Formatting

- Follow the official Kotlin coding conventions. Use `ktlint` or `detekt` to enforce them.
- 4-space indentation. Maximum line length 120 characters.
- `camelCase` for functions, properties, and local variables. `PascalCase` for classes and objects. `UPPER_SNAKE_CASE` for top-level `const val`.
- Prefer expression bodies for single-expression functions: `fun double(x: Int) = x * 2`.
- Use trailing lambdas when the last parameter is a function: `list.filter { it > 0 }`.

## Nullability

- The null-safety type system is Kotlin's most important feature. Use it fully — avoid `!!` except when you have proof the value cannot be null and the compiler cannot infer it.
- Prefer `?.let`, `?.also`, `?: return`, and safe casts (`as?`) over null checks.
- Use `requireNotNull` / `checkNotNull` with a message when asserting non-null at boundaries.
- Mark parameters and return types nullable only when null is a meaningful value. Absent/empty should often be modelled as an empty collection or a sealed class variant, not null.

## Idioms

- Prefer `data class` for plain data containers — you get `equals`, `hashCode`, `copy`, and `toString` for free.
- Use `sealed class` / `sealed interface` to model closed hierarchies. Exhaustive `when` expressions over sealed types are checked by the compiler.
- Use `object` for singletons and companion objects for factory methods and constants.
- Prefer `val` over `var`. Immutability should be the default.
- Use `apply`, `let`, `run`, `also`, `with` scope functions purposefully:
  - `apply` — configure an object and return it
  - `let` — transform a nullable or execute a block with the result
  - `run` — compute a result from a block
  - `also` — side effect, return the receiver
  - `with` — call multiple methods on the same object without repeating the receiver

## Coroutines

- Use `suspend` functions for anything that involves I/O, waiting, or concurrency. Do not block threads.
- Choose the right dispatcher: `Dispatchers.IO` for blocking I/O, `Dispatchers.Default` for CPU-intensive work, `Dispatchers.Main` for UI updates.
- Prefer structured concurrency: launch coroutines in a `CoroutineScope` tied to a lifecycle. Never use `GlobalScope` in production code.
- Use `Flow` for streams of values. Prefer cold flows; use `SharedFlow` / `StateFlow` for hot streams.
- Handle cancellation: check `isActive` in long-running loops, and let `CancellationException` propagate — never catch it silently.

## Extension Functions and DSLs

- Use extension functions to add utility to existing types without subclassing.
- Keep extension functions in a clearly named file (e.g. `StringExtensions.kt`). Do not scatter them across unrelated files.
- Use function types and receivers (`T.() -> Unit`) to build type-safe builders and DSLs.

## Error Handling

- Prefer explicit modelling of failure: use `Result<T>`, a sealed `Either`-style type, or typed exceptions.
- Use `runCatching` to convert throwing code to `Result`. Map and recover explicitly rather than letting exceptions propagate silently.
- Only catch specific exceptions. Never `catch (e: Exception)` without a very good reason.

## Interop with Java

- Annotate public APIs with `@JvmStatic`, `@JvmOverloads`, `@JvmField` where needed for clean Java interop.
- Use `@file:JvmName` to give a Kotlin file a clean name from Java.
- Be careful with `Unit` return types and `Nothing` — they behave differently from Java's `void`.
