# Skill: TypeScript

## Type system

- Enable `"strict": true` in `tsconfig.json`. It turns on `noImplicitAny`, `strictNullChecks`, and several others — all of which catch real bugs.
- Prefer `interface` for object shapes that may be extended; prefer `type` for unions, intersections, and computed types.
- Use `readonly` on properties that should not be mutated after construction.
- Avoid `any`. Use `unknown` for genuinely dynamic values — it forces you to narrow before use. Use `never` to mark branches that should be unreachable.
- `as SomeType` assertions bypass the type checker — only use them when you know more than the compiler. Add a comment explaining why.

## Narrowing

```typescript
// Discriminated unions — exhaustiveness checked by `never`
type Shape = { kind: "circle"; r: number } | { kind: "rect"; w: number; h: number };

function area(s: Shape): number {
    switch (s.kind) {
        case "circle": return Math.PI * s.r ** 2;
        case "rect":   return s.w * s.h;
        default: {
            const _: never = s;  // compile error if a case is missing
            throw new Error(`Unknown shape: ${_}`);
        }
    }
}

// Type guard
function isString(v: unknown): v is string {
    return typeof v === "string";
}
```

## Utility types

| Type | Use |
|---|---|
| `Partial<T>` | All props optional (patch/update objects) |
| `Required<T>` | All props required |
| `Readonly<T>` | All props readonly |
| `Pick<T, K>` | Subset of props |
| `Omit<T, K>` | Props minus K |
| `Record<K, V>` | Object with keys K and values V |
| `ReturnType<F>` | Infer return type of a function |
| `Parameters<F>` | Infer parameter tuple |
| `NonNullable<T>` | Remove null and undefined |

## Async patterns

- Use `async`/`await` over raw Promises — stack traces are readable and control flow is obvious.
- Handle errors with `try/catch`; do not swallow errors with empty catch blocks.
- `Promise.all([...])` for independent parallel requests; `Promise.allSettled` if you need results even when some fail.
- Type async function return as `Promise<ReturnType>` explicitly in public APIs.

## Common pitfalls

- `for...of` over arrays; `for...in` iterates keys (strings), not values — avoid it on arrays.
- `===` not `==` everywhere.
- Object spread (`{ ...obj, key: val }`) creates a shallow copy; nested objects are still shared.
- Optional chaining `?.` short-circuits; nullish coalescing `??` provides defaults for `null`/`undefined` only (unlike `||` which also fires on `0`, `""`, `false`).
- Avoid `enum` in library code — prefer `as const` objects with `typeof` unions, which are simpler in emitted JS and work better with tree-shaking.

## Project setup

- `"moduleResolution": "bundler"` (TS 5+) or `"node16"` for modern ESM.
- `"target": "ES2022"` or later for top-level `await`, `Array.at()`, `Object.hasOwn()`.
- Use `zod` or `valibot` for runtime validation of external data (API responses, env vars).
- `ts-node` or `tsx` for running TypeScript directly in development.
