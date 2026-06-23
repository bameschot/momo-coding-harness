# Skill: Software Design

## Principles

- **Single Responsibility**: each module, class, or function has one reason to change. When something needs to change, only one unit should be affected.
- **Open/Closed**: extend behaviour through composition and new types, not by modifying existing code.
- **Dependency Inversion**: depend on abstractions (interfaces, protocols, abstract base classes), not concrete implementations. Pass dependencies in; don't construct them inside.
- **Composition over inheritance**: prefer composing behaviours from small, focused pieces over deep inheritance hierarchies.
- **Explicit over implicit**: make dependencies, data flow, and side effects visible in function signatures and module structure.

## Decomposition

- Start from use cases and work inward — identify what the system must do before deciding how.
- Name things after what they do in the domain, not after their technical role (e.g. `OrderRepository`, not `DataManager`).
- Keep layers separate: presentation, application logic, domain logic, infrastructure. Dependencies should only point inward (toward the domain).
- Identify boundaries: what crosses a boundary (a network call, a file write, a database query) should be isolated behind an interface so it can be swapped or tested.

## API and Interface Design

- Design interfaces from the caller's perspective, not the implementer's. Ask: what does the caller need to provide and what do they get back?
- Prefer narrow interfaces — small sets of related methods — over wide ones.
- Make illegal states unrepresentable: use the type system to encode constraints where possible.
- Return values should carry intent. Avoid boolean success flags; prefer result types, exceptions with meaning, or typed error envelopes.
- Avoid output parameters and mutation of arguments.

## Data Modelling

- Identify the core entities and their invariants. Document what must always be true about each entity.
- Distinguish between entities (identity matters) and value objects (only content matters).
- Be explicit about mutability. Prefer immutable data structures; mutate at the boundary, not in the core.
- Normalise data at rest; denormalise for read performance only when measurement justifies it.

## System-Level Concerns

- **Error handling strategy**: decide upfront whether errors are exceptional (use exceptions) or expected (use result types / error codes). Be consistent.
- **Configuration**: separate configuration from code. Validate configuration at startup.
- **Observability**: design for logging and metrics from the start. Structure logs so they can be queried.
- **Idempotency**: design operations to be safely retried where possible, especially anything that crosses a network or writes persistent state.

## Design Process

- Sketch the data model and key interfaces before writing any implementation.
- Write a one-paragraph description of each major component's responsibility. If you cannot write it in one paragraph, the component is doing too much.
- Question every dependency: does this module really need to know about that one?
- Revisit the design after the first working implementation — the first cut reveals what you got wrong.
