# Skill: Software Design

## Principles

- **Single Responsibility**: each module, class, or function has one reason to change. When something needs to change, only one unit should be affected.
- **Open/Closed**: extend behaviour through composition and new types, not by modifying existing code.
- **Dependency Inversion**: depend on abstractions (interfaces, protocols, abstract base classes), not concrete implementations. Pass dependencies in; don't construct them inside.
- **Composition over inheritance**: prefer composing behaviours from small, focused pieces over deep inheritance hierarchies.
- **Explicit over implicit**: make dependencies, data flow, and side effects visible in function signatures and module structure.

## Decomposition

- Start from use cases and work inward — identify what the system must do before deciding how.
- Name things after what they do in the domain, not after their technical role (`OrderRepository`, not `DataManager`).
- Keep layers separate: presentation, application logic, domain logic, infrastructure. Dependencies should only point inward (toward the domain).
- Identify boundaries: anything that crosses a network call, file write, or database query should be isolated behind an interface so it can be swapped or tested.

## Design patterns — when to apply

### Strategy
Swap algorithms at runtime. Use when a class does one thing but several ways to do it.
```
Context holds a reference to a Strategy interface.
Each Strategy implements the algorithm differently.
The Context delegates to whichever Strategy is injected.
```
Use for: sort orders, pricing rules, authentication methods, output formats.

### Repository
Isolate data access behind a domain-oriented interface. The domain layer never knows about SQL, HTTP, or files.
```
interface OrderRepository:
    findById(id) → Order
    findByCustomer(id) → [Order]
    save(order) → void

SqlOrderRepository implements OrderRepository
InMemoryOrderRepository implements OrderRepository  ← test fake
```

### Factory / Factory Method
Encapsulate object creation logic, especially when the concrete type depends on runtime conditions.
```
Use Factory when: construction is complex, the type varies, or you want to decouple callers from concrete types.
Avoid when: simple new() is fine — don't add indirection without a reason.
```

### Observer / Event
Decouple producers from consumers. The producer emits events; consumers subscribe independently.
```
Use for: UI events, domain events (OrderPlaced, UserRegistered), plugin hooks.
Risk: hidden control flow, hard to trace who handles what. Keep the event contract clear.
```

### Command
Encapsulate an operation as an object. Enables undo/redo, queuing, logging, and retry.
```
Command interface: execute(), undo()
Invoker calls execute() without knowing what it does.
```

### Adapter
Wrap a third-party or legacy interface to match the interface your code expects.
```
Target interface ← your code depends on this
Adapter implements Target, delegates to Adaptee
Adaptee ← existing class with incompatible interface
```

## API and interface design

- Design from the caller's perspective, not the implementer's. Ask: what does the caller need to provide, and what do they get back?
- Prefer narrow interfaces — small sets of related methods — over wide ones.
- Make illegal states unrepresentable: use the type system to encode constraints where possible.
- Return values should carry intent. Avoid boolean success flags; prefer result types, typed errors, or exceptions with meaning.
- Avoid output parameters and mutation of caller-provided arguments.
- Idempotent operations are safer to retry. Design writes to be idempotent where possible.

## Data modelling

- Identify the core entities and their invariants. Document what must always be true about each entity.
- Distinguish entities (identity matters, even if two have same data) from value objects (only content matters, interchangeable).
- Be explicit about mutability. Prefer immutable data structures; mutate at the boundary, not in the core.
- Model failure states explicitly in the type system rather than returning null or a sentinel value.

```
Good: Optional<User> / Result<User, NotFound>
Bad: User | null where callers forget to check
```

## System-level concerns

- **Error handling strategy**: decide upfront whether errors are exceptional (use exceptions) or expected (use result types / error codes). Be consistent within a service boundary.
- **Configuration**: separate configuration from code. Validate all configuration at startup — fail loudly if required config is missing or invalid.
- **Observability**: log at action boundaries (received, processed, sent). Structure logs so they can be queried. Include correlation IDs for distributed systems.
- **Idempotency**: design operations to be safely retried where possible, especially anything that crosses a network or writes persistent state.
- **Backward compatibility**: before removing or changing a public interface, check all callers. Add new things; deprecate old things; remove last.

## Architecture patterns

### Hexagonal (Ports and Adapters)
The application core defines interfaces (ports) for everything it needs (database, email, HTTP). Adapters implement those interfaces. This makes the core testable without infrastructure.

```
Core ← domain logic, knows nothing about DB or HTTP
  ↑ Port (interface)
  └── Adapter (SQL implementation, HTTP client, in-memory fake)
```

### CQRS (Command Query Responsibility Segregation)
Separate the write model (commands that mutate state) from the read model (queries that return data). Read models can be denormalized for query performance without affecting the write model.

Use when: read and write patterns diverge significantly (e.g., complex queries on heavily normalized data).

## Design process

1. Write a one-paragraph description of each major component's responsibility. If you cannot write it in one paragraph, the component is doing too much.
2. Sketch the data model and key interfaces before writing any implementation.
3. Question every dependency: does this module really need to know about that one?
4. Write the tests (or test scenarios) before the implementation — they force the interface design.
5. Revisit the design after the first working implementation — the first cut reveals what you got wrong.

## Architecture Decision Records (ADRs)

When making a significant design decision, record it as a short ADR:
- **Context**: what situation forced this decision?
- **Decision**: what was chosen and why?
- **Consequences**: what does this make easier and harder going forward?

ADRs belong in the repo alongside the code they describe. They make the design legible to future maintainers and prevent re-litigating settled decisions.
