# Skill: Kotlin Programming

## Style and Formatting

- Follow the official Kotlin coding conventions. Use `ktlint` or `detekt` to enforce them.
- 4-space indentation. Maximum line length 120 characters.
- `camelCase` for functions, properties, and local variables. `PascalCase` for classes and objects. `UPPER_SNAKE_CASE` for top-level `const val`.
- Prefer expression bodies for single-expression functions: `fun double(x: Int) = x * 2`.
- Use trailing lambdas when the last parameter is a function: `list.filter { it > 0 }`.

## Nullability

```kotlin
// Safe call and Elvis operator — the everyday null-safety toolkit
val length = name?.length ?: 0
val upper = name?.uppercase() ?: return

// let — execute a block only if non-null
user?.let { sendWelcomeEmail(it) }

// Unsafe !! — only when you have proof the value cannot be null
val config = loadConfig() ?: error("Config must be present")  // prefer error() or require()
val value = config!!.getValue()  // use !! only when the above guarantee is proven

// requireNotNull / checkNotNull for assertion with messages
val email = requireNotNull(user.email) { "User ${user.id} has no email" }
val active = checkNotNull(session) { "Session must be set before calling this" }

// Nullable modelling: use sealed class, not null, for meaningful absence
sealed class Result<out T> {
    data class Success<T>(val value: T) : Result<T>()
    data class Failure(val error: Throwable) : Result<Nothing>()
}
```

## Idioms

```kotlin
// data class — equals, hashCode, copy, toString, destructuring for free
data class User(val id: Int, val name: String, val email: String)

val user = User(1, "Alice", "alice@example.com")
val updated = user.copy(name = "Bob")
val (id, name, _) = user  // destructuring declaration

// sealed class / interface — closed hierarchy; exhaustive when expressions
sealed class ApiResponse<out T> {
    data class Success<T>(val data: T) : ApiResponse<T>()
    data class Error(val code: Int, val message: String) : ApiResponse<Nothing>()
    object Loading : ApiResponse<Nothing>()
}

fun handle(response: ApiResponse<User>): String = when (response) {
    is ApiResponse.Success -> "Hello, ${response.data.name}"
    is ApiResponse.Error   -> "Error ${response.code}: ${response.message}"
    ApiResponse.Loading    -> "Loading..."
    // no else needed — sealed class is exhaustive
}

// Scope functions
val config = Config().apply {
    host = "localhost"
    port = 5432
    timeout = 30
}

val result = StringBuilder().run {
    append("Hello")
    append(", ")
    append("World")
    toString()
}

database.also { log.info("Connected to $it") }
    .use { db -> db.query("SELECT 1") }

with(user) {
    println("Name: $name")
    println("Email: $email")
}

// Prefer val over var — immutability by default
val items = listOf("a", "b", "c")   // read-only view
val mutable = mutableListOf("a")    // mutable
```

## Extension Functions

```kotlin
// Extend types you don't own
fun String.titleCase(): String =
    split(" ").joinToString(" ") { it.replaceFirstChar(Char::uppercase) }

fun <T> List<T>.second(): T {
    if (size < 2) throw IndexOutOfBoundsException("List has fewer than 2 elements")
    return this[1]
}

// Receiver functions for DSLs
class HtmlBuilder {
    private val sb = StringBuilder()
    fun div(block: HtmlBuilder.() -> Unit) {
        sb.append("<div>")
        this.block()
        sb.append("</div>")
    }
    fun text(t: String) { sb.append(t) }
    fun build() = sb.toString()
}

// Inline functions + reified type parameters — access generic type at runtime
inline fun <reified T : Any> deserialize(json: String): T =
    objectMapper.readValue(json, T::class.java)

val user: User = deserialize("""{"id":1,"name":"Alice"}""")
```

## Coroutines

```kotlin
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*

// Structured concurrency — launch in a scope tied to a lifecycle
class MyViewModel : ViewModel() {
    fun loadData() {
        viewModelScope.launch {                      // cancelled when ViewModel cleared
            val data = withContext(Dispatchers.IO) { repository.fetch() }
            updateUi(data)
        }
    }
}

// Dispatcher choice
withContext(Dispatchers.IO)      { /* blocking I/O: file, network, database */ }
withContext(Dispatchers.Default) { /* CPU-intensive: parsing, sorting, computation */ }
withContext(Dispatchers.Main)    { /* UI updates on Android/JavaFX main thread */ }

// Parallel execution — async/await
val (users, posts) = coroutineScope {
    val u = async { userRepo.findAll() }
    val p = async { postRepo.findAll() }
    u.await() to p.await()
}

// Flow — cold, lazy stream of values
fun userUpdates(id: Int): Flow<User> = flow {
    while (true) {
        emit(fetchUser(id))
        delay(5_000)
    }
}

// Collect and transform
userUpdates(42)
    .map { it.copy(name = it.name.uppercase()) }
    .filter { it.isActive }
    .onEach { log.info("User update: $it") }
    .catch { e -> log.error("Stream error", e) }
    .launchIn(scope)

// StateFlow — hot, always has a current value (replaces LiveData)
private val _state = MutableStateFlow<UiState>(UiState.Loading)
val state: StateFlow<UiState> = _state.asStateFlow()

// Handle cancellation in long-running loops
suspend fun longTask() {
    while (isActive) {     // check cancellation; throws CancellationException on cancel
        doWork()
        yield()            // explicit cancellation point
    }
}
```

## Error Handling

```kotlin
// runCatching — converts exceptions to Result
val result = runCatching { fetchUser(id) }
    .map { it.toDto() }
    .recover { e -> defaultUserDto(e) }
    .getOrThrow()

// Result chaining
fun loadUserProfile(id: Int): Result<UserProfile> =
    runCatching { fetchUser(id) }
        .mapCatching { user -> fetchProfile(user) }
        .onFailure { log.error("Failed to load profile for $id", it) }

// Only catch specific exceptions
try {
    process()
} catch (e: IOException) {
    log.warn("IO failure: ${e.message}")
} catch (e: CancellationException) {
    throw e  // never swallow CancellationException in coroutines!
}
```

## Testing

```kotlin
import org.junit.jupiter.api.*
import kotlinx.coroutines.test.*
import io.mockk.*

class UserServiceTest {

    private val repo = mockk<UserRepository>()
    private val service = UserService(repo)

    @Test
    fun `findById returns user when found`() {
        val user = User(1, "Alice", "alice@example.com")
        every { repo.findById(1) } returns user

        val result = service.findById(1)

        assertThat(result).isEqualTo(user)
        verify { repo.findById(1) }
    }

    @Test
    fun `findById returns null when not found`() {
        every { repo.findById(99) } returns null
        assertThat(service.findById(99)).isNull()
    }

    // Test coroutines with runTest
    @Test
    fun `loadProfile emits loading then success`() = runTest {
        coEvery { repo.fetch(1) } returns User(1, "Alice", "")

        val states = service.profileFlow(1).take(2).toList()

        assertThat(states[0]).isEqualTo(UiState.Loading)
        assertThat(states[1]).isInstanceOf(UiState.Success::class.java)
    }
}
```

## Java Interop

- Annotate public APIs with `@JvmStatic`, `@JvmOverloads`, `@JvmField` where needed for clean Java interop.
- Use `@file:JvmName("Utils")` to give a Kotlin file a clean class name from Java.
- Be careful with `Unit` return types — they become `void` in Java; `Nothing` has no Java equivalent.
- Kotlin nullable types annotate to `@Nullable` for Java callers automatically.
