# Skill: Java Programming

## Style and Formatting

- Follow Google Java Style or Oracle Code Conventions. Use `google-java-format` or `checkstyle` to enforce consistency.
- 4-space indentation. Opening braces on the same line. Line length up to 100 characters.
- `camelCase` for methods and variables. `PascalCase` for classes and interfaces. `UPPER_SNAKE_CASE` for `static final` constants.
- One public top-level class per file; the filename must match the class name.

## Modern Java (17+)

```java
// Records — immutable data carriers
record Point(int x, int y) {
    // compact constructor for validation
    Point {
        if (x < 0 || y < 0) throw new IllegalArgumentException("Negative coordinate");
    }
    // can add methods
    double distanceTo(Point other) {
        return Math.hypot(x - other.x(), y - other.y());
    }
}

// Sealed classes — closed hierarchy, exhaustive switch
sealed interface Shape permits Circle, Rectangle, Triangle {}
record Circle(double radius) implements Shape {}
record Rectangle(double width, double height) implements Shape {}
record Triangle(double base, double height) implements Shape {}

double area(Shape shape) {
    return switch (shape) {                           // pattern matching switch (Java 21)
        case Circle c       -> Math.PI * c.radius() * c.radius();
        case Rectangle r    -> r.width() * r.height();
        case Triangle t     -> 0.5 * t.base() * t.height();
    };  // no default needed — sealed hierarchy is exhaustive
}

// Text blocks
String json = """
        {
            "name": "Alice",
            "age": 30
        }
        """;

// instanceof pattern matching
if (obj instanceof String s && s.length() > 5) {
    System.out.println(s.toUpperCase());
}

// var — local type inference
var users = new ArrayList<User>();  // type is clear from right side
var entry = map.entrySet().iterator().next();  // saves repetition
```

## Collections and Streams

```java
// Immutable factory methods (Java 9+)
var list = List.of("a", "b", "c");           // immutable, no nulls
var map  = Map.of("key", 42, "other", 7);    // immutable
var set  = Set.of(1, 2, 3);                  // immutable

// Mutable copy from immutable
var mutable = new ArrayList<>(list);

// Streams — prefer for transformation pipelines
List<String> result = users.stream()
    .filter(u -> u.isActive())
    .sorted(Comparator.comparing(User::name))
    .map(User::email)
    .distinct()
    .collect(Collectors.toUnmodifiableList());

// Grouping
Map<String, List<User>> byRole = users.stream()
    .collect(Collectors.groupingBy(User::role));

// Counting and summing
long activeCount = users.stream().filter(User::isActive).count();
int totalAge = users.stream().mapToInt(User::age).sum();
OptionalDouble avgAge = users.stream().mapToInt(User::age).average();

// flatMap — flatten nested streams
List<String> tags = posts.stream()
    .flatMap(p -> p.tags().stream())
    .distinct()
    .collect(Collectors.toList());

// Reduction
Optional<User> oldest = users.stream()
    .max(Comparator.comparingInt(User::age));

// toMap — watch for duplicate key exception
Map<Integer, User> byId = users.stream()
    .collect(Collectors.toMap(User::id, u -> u));
```

## Object-Oriented Design

- Prefer composition over inheritance. Use interfaces to define behaviour; abstract classes only when sharing implementation is unavoidable.
- Program to interfaces: declare variables as `List<T>` not `ArrayList<T>`, `Map<K, V>` not `HashMap<K, V>`.
- Minimise mutability: prefer `final` fields and constructor injection. Avoid setters where possible.
- Mark classes `final` unless you intend them to be extended.

## Null Handling

```java
// Optional<T> for return types when absence is meaningful
Optional<User> findUser(int id) {
    return users.stream().filter(u -> u.id() == id).findFirst();
}

// Chain Optional operations without null checks
String name = findUser(id)
    .filter(User::isActive)
    .map(User::name)
    .orElse("Unknown");

// Validate non-null at public API boundaries
public void process(Order order) {
    Objects.requireNonNull(order, "order must not be null");
    // or with Java 21+ Objects.requireNonNullElse, requireNonNullElseGet
}

// Never return null from collection methods — return empty collections
List<User> findByRole(String role) {
    if (!validRoles.contains(role)) return List.of();  // not null
    return users.stream().filter(u -> u.role().equals(role)).collect(...);
}
```

## Concurrency

```java
import java.util.concurrent.*;

// Prefer ExecutorService over raw Thread
ExecutorService pool = Executors.newFixedThreadPool(4);
Future<String> future = pool.submit(() -> fetchData(url));
String result = future.get(5, TimeUnit.SECONDS);
pool.shutdown();

// CompletableFuture — async pipelines
CompletableFuture<User> userFuture = CompletableFuture
    .supplyAsync(() -> fetchUser(id), pool)
    .thenApply(u -> enrich(u))
    .exceptionally(ex -> defaultUser());

// Parallel execution
CompletableFuture<Void> all = CompletableFuture.allOf(
    CompletableFuture.runAsync(task1),
    CompletableFuture.runAsync(task2)
);
all.join();

// ConcurrentHashMap for thread-safe maps
var cache = new ConcurrentHashMap<String, String>();
cache.computeIfAbsent(key, k -> expensiveCompute(k));
```

## Error Handling and Exceptions

```java
// Checked exceptions for conditions callers must handle
// Unchecked (RuntimeException) for programming errors

// Always use try-with-resources
try (var stream = Files.newInputStream(path);
     var reader = new BufferedReader(new InputStreamReader(stream))) {
    return reader.lines().collect(Collectors.joining("\n"));
} // both closed automatically, even if an exception is thrown

// Wrap and rethrow with context
try {
    process(data);
} catch (IOException e) {
    throw new ProcessingException("Failed to process data from " + source, e);
}

// Multi-catch
try {
    ...
} catch (IOException | ParseException e) {
    log.error("Data error", e);
}
```

## Testing with JUnit 5 and Mockito

```java
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.*;
import static org.mockito.Mockito.*;
import static org.assertj.core.api.Assertions.*;

class UserServiceTest {

    @Mock UserRepository repo;
    UserService service;

    @BeforeEach
    void setup() {
        MockitoAnnotations.openMocks(this);
        service = new UserService(repo);
    }

    @Test
    void findUser_returnsUser_whenExists() {
        when(repo.findById(1)).thenReturn(Optional.of(new User(1, "Alice")));

        Optional<User> result = service.findById(1);

        assertThat(result).isPresent().contains(new User(1, "Alice"));
        verify(repo).findById(1);
    }

    @Test
    void findUser_returnsEmpty_whenNotFound() {
        when(repo.findById(99)).thenReturn(Optional.empty());
        assertThat(service.findById(99)).isEmpty();
    }

    @ParameterizedTest
    @ValueSource(strings = {"", "  ", "\t"})
    void createUser_throwsOnBlankName(String name) {
        assertThatThrownBy(() -> service.create(name))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("name");
    }

    @Test
    void save_throwsOnRepositoryFailure() {
        doThrow(new RuntimeException("DB down")).when(repo).save(any());
        assertThatThrownBy(() -> service.create("Alice"))
            .isInstanceOf(ServiceException.class)
            .hasCauseInstanceOf(RuntimeException.class);
    }
}
```

## Build and Dependencies

- Use Maven or Gradle. Prefer Gradle with Kotlin DSL for new projects.
- Declare dependency versions in a BOM (`dependencyManagement` / `platform`) rather than repeating them.
- Use the minimum Java version your environment supports and target it explicitly.
- Run `./gradlew test` and `./gradlew check` (includes spotbugs, checkstyle) in CI.
