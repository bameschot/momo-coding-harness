# Skill: Testing and Test-Driven Development

## TDD Cycle

- **Red → Green → Refactor**. Write a failing test first. Write the minimum code to make it pass. Then clean up the code without breaking the test.
- Tests drive the design — they force you to think about the interface before the implementation. If a test is hard to write, the design is telling you something.
- Never write more production code than is required to pass the current test.
- Never refactor while a test is failing.

## What to Test

- Test behaviour, not implementation. Tests should describe what the code does from the outside, not how it does it internally.
- Test the edges: empty input, maximum size, boundary values (off-by-one), null/None, unexpected types.
- Test failures and error paths, not just the happy path.
- Do not test private methods directly — test them through the public API. If a private method feels important enough to test directly, it probably belongs in its own class.

## Test Design

- One concept per test is the real rule. A test for a complex object may need multiple assertions to fully verify that concept.
- Name tests descriptively: `should_return_empty_list_when_no_items_match` not `testFilter`. The name is the documentation.
- Arrange-Act-Assert (AAA): set up state, perform the action, assert the result. Keep each section distinct and minimal.
- Tests should be independent: no shared mutable state, no ordering dependency.
- Tests should be fast. Slow tests are not run frequently. Unit tests should complete in milliseconds.

## Test Antipatterns

| Antipattern | Problem | Fix |
|---|---|---|
| Test that asserts `assertTrue(true)` | Tests nothing | Assert the actual output |
| Test that mocks everything | Doesn't test real behaviour | Mock only at the boundary |
| Test tied to implementation detail | Breaks on refactor | Test observable output |
| One giant test for a whole workflow | Hard to diagnose failure | Split into focused unit tests |
| Tests that depend on execution order | Intermittent failures | Make each test fully independent |
| Tests that sleep to wait for async code | Slow and flaky | Use proper async test utilities |
| Identical assertions across many tests | Coverage illusion | Use parametrized tests |

## Mocks and Test Doubles

- **Stub**: returns a fixed value. Use for replacing a dependency's output.
- **Mock**: records calls and lets you assert what was called. Use sparingly — mocks tie tests to implementation.
- **Fake**: a working but simplified implementation (in-memory database, fake clock). Prefer fakes over mocks for complex dependencies.
- **Spy**: wraps a real object and records calls. Use only when you need real behaviour plus call verification.

Mock at the boundary (external services, file system, clock, randomness). Do not mock your own domain logic — test it directly.

```python
# Prefer a fake over a mock for complex collaborators
class InMemoryUserRepository:
    def __init__(self):
        self._store: dict[int, User] = {}

    def save(self, user: User) -> None:
        self._store[user.id] = user

    def find_by_id(self, id: int) -> User | None:
        return self._store.get(id)

# The service under test receives the fake through its interface
def test_user_service():
    repo = InMemoryUserRepository()
    service = UserService(repo)
    service.create("Alice", "alice@example.com")
    assert repo.find_by_id(1) is not None
```

## Test Coverage

- 100% coverage does not mean the code is correct. Coverage measures which lines were executed, not whether behaviour was verified.
- Aim for high coverage of business logic and edge cases; accept lower coverage of glue code, configuration, and framework boilerplate.
- Mutation testing reveals tests that pass even when the code is deliberately broken — run it occasionally to assess test quality.

## Test Pyramid

- **Unit tests** (many): test a single unit in isolation. Fast, cheap, run in milliseconds.
- **Integration tests** (some): test that components work together — service + real database, HTTP handler + serialisation.
- **End-to-end / system tests** (few): test the full stack. Valuable but slow and fragile. Keep the number small.
- A failing E2E test tells you something is broken; a failing unit test tells you exactly what.

## Property-Based Testing

Instead of writing specific input/output pairs, define properties that must hold for all inputs. The framework generates hundreds of random cases.

```python
from hypothesis import given, strategies as st

@given(st.lists(st.integers()))
def test_sort_is_idempotent(lst):
    # sorting twice should give same result as sorting once
    assert sorted(sorted(lst)) == sorted(lst)

@given(st.text())
def test_encode_decode_roundtrip(s):
    assert decode(encode(s)) == s
```

Good properties to test: round-trips (encode/decode, serialize/deserialize), invariants (sorted output is sorted, all items present), commutativity (order of operations doesn't matter when it shouldn't).

## Refactoring Under Tests

- Tests are the safety net for refactoring. Before refactoring untested code, write tests first.
- When a refactor reveals a better design, update the tests to match the new design — do not preserve tests that enforce the old design.
- If changing tests and code at the same time is unavoidable, do it in the smallest possible steps and commit after each green state.
- Green-bar commits: commit at every point where all tests pass. This makes the history a series of safe states you can return to.

## TDD in Practice

- Start with the simplest possible test. The first test often just checks that the thing can be instantiated.
- When stuck, ask: what is the smallest piece of observable behaviour I can test right now?
- If the code under test is hard to set up, it has too many dependencies. Simplify the design.
- Keep test code clean. Tests that are hard to read are tests that do not get maintained.
- If a test requires 20 lines of setup, extract a builder or factory to reduce duplication — test code is still code.
