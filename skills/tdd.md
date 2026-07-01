# Skill: Testing and Test-Driven Development

## Core rules

- Red → Green → Refactor: write a failing test, write the minimum code to pass, then clean up.
- NEVER write more production code than the current test requires. NEVER refactor while a test is failing.
- Test behaviour, not implementation. Test through the public API, not private methods.
- Test the edges: empty, boundary/off-by-one, null, and error paths — not just the happy path.
- Make each test independent: no shared mutable state, no ordering dependency.
- One concept per test. Name it for the behaviour: `should_return_empty_when_no_match`.
- Follow Arrange-Act-Assert. Keep tests fast (milliseconds).

## Mocks and doubles

- Mock only at the boundary: network, file system, clock, randomness. Do NOT mock your own domain logic.
- Prefer a **fake** (a working simplified implementation, e.g. in-memory repo) over a **mock** for complex collaborators.

```python
class InMemoryUserRepository:
    def __init__(self): self._store = {}
    def save(self, user): self._store[user.id] = user
    def find_by_id(self, id): return self._store.get(id)
```

## Antipatterns to avoid

| Antipattern | Fix |
|---|---|
| Test tied to an implementation detail | Assert observable output |
| Mocking everything | Mock only at the boundary |
| One giant test for a whole workflow | Split into focused tests |
| Test depends on execution order | Make each test independent |
| Sleeping to wait for async code | Use proper async test utilities |
| Identical assertions copied across tests | Use a parametrized test |

## Coverage

- 100% coverage does not mean correct — it measures lines executed, not behaviour verified.
- Cover business logic and edge cases well; accept lower coverage on glue/config code.

## In practice

- Start with the simplest test (often "it can be constructed"). When stuck, ask: what is the smallest observable behaviour I can test now?
- If a test is hard to set up, the design has too many dependencies — simplify it.
- Before refactoring untested code, write tests first. Commit at every green state.
