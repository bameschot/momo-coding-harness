# Skill: Testing and Test-Driven Development

## TDD Cycle

- **Red → Green → Refactor**. Write a failing test first. Write the minimum code to make it pass. Then clean up the code without breaking the test.
- Tests drive the design — they force you to think about the interface before the implementation. If a test is hard to write, the design is telling you something.
- Never write more production code than is required to pass the current test.
- Never refactor while a test is failing.

## What to Test

- Test behaviour, not implementation. Tests should describe what the code does from the outside, not how it does it internally. If a test breaks when you refactor internals without changing behaviour, it is testing the wrong thing.
- Test the edges: empty input, maximum size, boundary values (off-by-one), null/None, unexpected types.
- Test failures and error paths, not just the happy path. What happens when a dependency fails? When input is invalid?
- Do not test private methods directly — test them through the public API. If a private method feels important enough to test directly, it probably belongs in its own class.

## Test Design

- One assertion per test is a guideline, not a rule. One concept per test is the real rule. A test for a complex object may need multiple assertions to fully verify that concept.
- Name tests descriptively: `should_return_empty_list_when_no_items_match`, not `testFilter`. The name is the documentation.
- Arrange-Act-Assert (AAA): set up state, perform the action, assert the result. Keep each section distinct and minimal.
- Tests should be independent: no shared mutable state, no ordering dependency. Any test should be runnable in isolation.
- Tests should be fast. Slow tests are not run frequently. Unit tests should complete in milliseconds.

## Mocks and Test Doubles

- **Stub**: returns a fixed value. Use for replacing a dependency's output.
- **Mock**: records calls and lets you assert what was called. Use sparingly — mocks tie tests to implementation.
- **Fake**: a working but simplified implementation (in-memory database, fake clock). Prefer fakes over mocks for complex dependencies.
- **Spy**: wraps a real object and records calls. Use only when you need real behaviour plus call verification.
- Mock at the boundary (external services, file system, clock, randomness). Do not mock your own domain logic — test it directly.

## Test Coverage

- 100% coverage does not mean the code is correct. Coverage measures which lines were executed, not whether behaviour was verified.
- Aim for high coverage of business logic and edge cases; accept lower coverage of glue code, configuration, and framework boilerplate.
- Mutation testing (e.g. PIT for Java, mutmut for Python) reveals tests that pass even when the code is deliberately broken. Run it occasionally to assess test quality.

## Test Pyramid

- **Unit tests** (many): test a single unit in isolation. Fast, cheap to write, run in milliseconds.
- **Integration tests** (some): test that components work together — e.g. a service + its repository against a real database.
- **End-to-end / system tests** (few): test the full stack. Valuable but slow and fragile. Keep the number small.
- Invest in the unit layer. A failing E2E test tells you something is broken; a failing unit test tells you exactly what.

## Refactoring Under Tests

- Tests are the safety net for refactoring. Before refactoring untested code, write tests first.
- When a refactor reveals a better design, update the tests to match the new design — do not preserve tests that enforce the old design.
- If changing tests and code at the same time is unavoidable, do it in the smallest possible steps and commit after each green state.

## TDD in Practice

- Start with the simplest possible test. The first test often just checks that the thing can be instantiated.
- When stuck, ask: what is the smallest piece of observable behaviour I can test right now?
- If the code under test is hard to set up, it has too many dependencies. Simplify the design.
- Keep test code clean. Tests that are hard to read are tests that do not get maintained.
