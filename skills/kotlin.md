# Skill: Kotlin Programming

## Core rules

- Prefer `val` over `var` — immutability by default.
- Handle null with `?.` and `?:`. Use `!!` only when you have proven the value is non-null.
- Use `data class` for data, `sealed class`/`interface` for closed hierarchies (exhaustive `when`, no `else`).
- Use expression bodies for one-liners: `fun double(x: Int) = x * 2`.
- Use trailing-lambda syntax: `list.filter { it > 0 }`.
- Model meaningful absence with a sealed `Result`/state type, not `null`.
- `camelCase` functions/properties, `PascalCase` types, `UPPER_SNAKE_CASE` for `const val`.

## Nullability

```kotlin
val length = name?.length ?: 0
user?.let { sendWelcomeEmail(it) }
val email = requireNotNull(user.email) { "user ${user.id} has no email" }
```

## Idioms

```kotlin
data class User(val id: Int, val name: String)
val updated = user.copy(name = "Bob")   // copy for changes

// Exhaustive when over a sealed type
fun handle(r: ApiResponse): String = when (r) {
    is ApiResponse.Success -> r.data
    is ApiResponse.Error   -> "error ${r.code}"
    ApiResponse.Loading    -> "loading"
}

// Scope functions: apply (configure), let (non-null block), run (compute + return)
val config = Config().apply { host = "localhost"; port = 5432 }
```

## Coroutines

- Launch in a lifecycle-bound scope (`viewModelScope`) for structured concurrency.
- Pick the dispatcher: `Dispatchers.IO` (blocking I/O), `Default` (CPU work), `Main` (UI).
- Run in parallel with `async`/`await` inside `coroutineScope`.
- NEVER swallow `CancellationException` — always rethrow it.

```kotlin
val (users, posts) = coroutineScope {
    val u = async { userRepo.findAll() }
    val p = async { postRepo.findAll() }
    u.await() to p.await()
}
```

## Error handling

```kotlin
val result = runCatching { fetchUser(id) }
    .map { it.toDto() }
    .getOrElse { defaultUserDto() }
```

Catch specific exceptions only; rethrow `CancellationException`.

## Testing

```kotlin
@Test
fun `findById returns user when found`() {
    every { repo.findById(1) } returns user
    assertThat(service.findById(1)).isEqualTo(user)
}
```

- Use `runTest { }` for coroutine tests. Backtick test names read as sentences.
