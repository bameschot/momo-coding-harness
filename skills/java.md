# Skill: Java Programming

## Style and Formatting

- Follow Google Java Style or Oracle Code Conventions. Use `google-java-format` or `checkstyle` to enforce consistency.
- 4-space indentation. Opening braces on the same line. Line length up to 100 characters.
- `camelCase` for methods and variables. `PascalCase` for classes and interfaces. `UPPER_SNAKE_CASE` for `static final` constants.
- One public top-level class per file; the filename must match the class name.

## Modern Java (17+)

- Prefer records for immutable data carriers: `record Point(int x, int y) {}`.
- Use sealed classes and interfaces to model closed hierarchies. Pair with `switch` expressions using pattern matching for exhaustive handling.
- Use `var` for local variable type inference where the type is obvious from the right-hand side. Do not use `var` when the type adds clarity.
- Use text blocks for multi-line strings.
- Prefer `switch` expressions (yield) over `switch` statements.
- Use `instanceof` pattern matching: `if (obj instanceof String s) { ... }`.

## Object-Oriented Design

- Prefer composition over inheritance. Use interfaces to define behaviour; abstract classes only when sharing implementation is unavoidable.
- Program to interfaces: declare variables and parameters as the most general type that meets your needs (`List<T>` not `ArrayList<T>`).
- Minimise mutability: prefer `final` fields and constructor injection. Avoid setters where possible.
- Mark classes `final` unless you intend them to be extended.
- Keep classes small and focused. If a class has more than two or three reasons to change, split it.

## Null Handling

- Annotate nullable and non-null parameters with `@Nullable` / `@NonNull` (from `javax.annotation` or `org.jetbrains.annotations`).
- Use `Optional<T>` for method return types when absence is a meaningful result. Do not use `Optional` for fields or parameters.
- Validate parameters at the start of public methods: use `Objects.requireNonNull` with a message.
- Never return `null` from a method that returns a collection — return an empty collection instead.

## Collections and Streams

- Use the Collections factory methods for immutable collections: `List.of(...)`, `Map.of(...)`, `Set.of(...)`.
- Use Streams for collection transformation pipelines. Prefer method references over lambdas where they are clearer.
- Avoid side effects inside stream operations — keep lambdas pure.
- Use `Collectors.toUnmodifiableList()` / `toUnmodifiableMap()` to get immutable results.

## Concurrency

- Prefer high-level concurrency utilities (`ExecutorService`, `CompletableFuture`, `ConcurrentHashMap`) over raw `Thread` and `synchronized`.
- Use `CompletableFuture` for async pipelines. Chain with `thenApply`, `thenCompose`, `exceptionally`.
- Document thread-safety guarantees on every class accessed by multiple threads.
- Avoid `volatile` and `synchronized` blocks in application code unless you understand the memory model implications.

## Error Handling and Exceptions

- Use checked exceptions for recoverable conditions that callers must handle. Use unchecked exceptions for programming errors.
- Do not catch `Exception` or `Throwable` unless you are at a top-level boundary (e.g. request handler, `main`).
- Always close resources with try-with-resources: `try (var stream = Files.newInputStream(path)) { ... }`.
- Never swallow exceptions silently. At minimum, log them.

## Build and Dependencies

- Use Maven or Gradle. Prefer Gradle for new projects.
- Declare dependency versions in a BOM (`dependencyManagement` / `platform`) rather than repeating them.
- Use the minimum Java version your environment supports and target it explicitly: `sourceCompatibility = JavaVersion.VERSION_21`.
