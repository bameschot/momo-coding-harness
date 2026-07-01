# Skill: Java Programming

## Core rules

- Program to interfaces: declare `List<T>` not `ArrayList<T>`, `Map<K,V>` not `HashMap<K,V>`.
- Prefer composition over inheritance. Mark classes `final` unless meant to be extended.
- Prefer `final` fields and constructor injection. Minimise mutability; avoid setters.
- Use `Optional<T>` for return types where absence is meaningful. NEVER return `null` from a collection method — return `List.of()`.
- Always use try-with-resources for anything closeable.
- `camelCase` methods/variables, `PascalCase` types, `UPPER_SNAKE_CASE` constants. One public class per file.

## Modern Java (17+) — prefer these

```java
// Records for immutable data, with validation
record Point(int x, int y) {
    Point { if (x < 0) throw new IllegalArgumentException("negative"); }
}

// Sealed hierarchy + switch pattern matching — no default needed
sealed interface Shape permits Circle, Rect {}
double area(Shape s) {
    return switch (s) {
        case Circle c -> Math.PI * c.radius() * c.radius();
        case Rect r   -> r.w() * r.h();
    };
}

// instanceof pattern, var, text blocks
if (obj instanceof String str && !str.isBlank()) { use(str); }
var users = new ArrayList<User>();
```

## Streams

Use for transformation pipelines; keep them readable.

```java
List<String> emails = users.stream()
    .filter(User::isActive)
    .map(User::email)
    .distinct()
    .toList();

Map<String, List<User>> byRole = users.stream()
    .collect(Collectors.groupingBy(User::role));
```

`Collectors.toMap` throws on duplicate keys — supply a merge function if keys can repeat.

## Errors & null

- Checked exceptions for conditions callers must handle; `RuntimeException` for programming errors.
- Wrap and rethrow with context: `throw new ProcessingException("failed for " + id, e);`.
- Validate at public boundaries: `Objects.requireNonNull(order, "order")`.

## Testing (JUnit 5 + AssertJ)

```java
@Test
void findUser_returnsUser_whenExists() {
    when(repo.findById(1)).thenReturn(Optional.of(new User(1, "Alice")));
    assertThat(service.findById(1)).isPresent();
    verify(repo).findById(1);
}
```

- Use `@ParameterizedTest` for multiple cases; `assertThatThrownBy(...)` for exceptions.

## Build

- Maven or Gradle (Kotlin DSL for new projects). Manage versions in one place (BOM/platform).
- Run `./gradlew test check` in CI.
