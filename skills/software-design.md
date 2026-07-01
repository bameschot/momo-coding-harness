# Skill: Software Design

## Core rules

- One responsibility per module/class/function — one reason to change.
- Depend on abstractions (interfaces/protocols), not concrete types. Pass dependencies in; don't construct them inside.
- Prefer composition over inheritance.
- Make dependencies, data flow, and side effects explicit in signatures — implicit is a smell.
- Isolate anything that crosses a boundary (network, file, DB) behind an interface so it can be swapped and tested.
- Name things for their domain role (`OrderRepository`, not `DataManager`).
- Make illegal states unrepresentable; model failure in the type system (`Result`/`Optional`), not with `null` or bool flags.

## Decomposition

- Start from use cases: decide what the system must do before deciding how.
- Keep layers separate (presentation → application → domain → infrastructure); dependencies point inward, toward the domain.
- If you cannot describe a component's responsibility in one sentence, it does too much.

## Patterns — when to reach for them

- **Strategy** — swap algorithms at runtime (sort orders, pricing, output formats).
- **Repository** — hide data access behind a domain interface; the domain never sees SQL/HTTP.
- **Factory** — encapsulate creation when the concrete type varies. Don't add it without a reason.
- **Observer/Event** — decouple producers from consumers. Risk: hidden control flow — keep the contract clear.
- **Adapter** — wrap an incompatible third-party interface to match what your code expects.

Don't add indirection without a reason — a plain `new()` is fine when the type is fixed.

## API design

- Design from the caller's side: what must they provide, what do they get back?
- Prefer narrow interfaces. Return values that carry intent (typed errors/results), not bare booleans.
- Don't mutate caller-provided arguments. Make writes idempotent where possible.

## System concerns

- Decide upfront: are errors exceptional (exceptions) or expected (result types)? Be consistent per boundary.
- Validate all configuration at startup; fail loudly on missing/invalid config.
- Log at action boundaries (received, processed, sent) with enough structure to query.
- Before changing a public interface, check all callers. Add new, deprecate old, remove last.

## Process

1. Write one paragraph per component's responsibility.
2. Sketch the data model and key interfaces before implementing.
3. Question every dependency — does this module really need to know about that one?
4. Revisit the design after the first working version; it reveals what you got wrong.
5. Record significant decisions as a short ADR (Context / Decision / Consequences) in the repo.
