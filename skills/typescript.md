# Skill: TypeScript

## Core rules

- Enable `"strict": true` in `tsconfig.json`.
- NEVER use `any`. Use `unknown` for genuinely dynamic values and narrow before use.
- Avoid `as` type assertions — they bypass the checker. Use type guards (`v is Foo`) instead. If you must assert, comment why.
- `interface` for object shapes; `type` for unions, intersections, and computed types.
- Use `readonly` for properties that must not change after construction.
- `===` not `==`. `const` by default, `let` when reassigned, never `var`.
- Use `?.` for nullable access and `??` for defaults (not `||`, which fires on `0`/`""`/`false`).

## Discriminated unions (prefer for modelling variants)

```typescript
type Shape =
  | { kind: "circle"; r: number }
  | { kind: "rect"; w: number; h: number };

function area(s: Shape): number {
  switch (s.kind) {
    case "circle": return Math.PI * s.r ** 2;
    case "rect":   return s.w * s.h;
    default: {
      const _exhaustive: never = s;  // compile error if a case is missing
      throw new Error(`unhandled: ${_exhaustive}`);
    }
  }
}
```

## Useful utility types

`Partial<T>`, `Required<T>`, `Pick<T,K>`, `Omit<T,K>`, `Record<K,V>`, `ReturnType<F>`, `NonNullable<T>`, `Awaited<T>`.

```typescript
type UpdateUserDto = Partial<Omit<User, "id">>;
```

## Async

```typescript
async function fetchUser(id: number): Promise<User> {
  const res = await fetch(`/api/users/${id}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<User>;
}
const [users, posts] = await Promise.all([fetchUsers(), fetchPosts()]);
```

## Runtime validation

Validate data at boundaries (API responses, env vars) with `zod`, and infer the type from the schema so it stays single-source:

```typescript
const UserSchema = z.object({ id: z.number(), name: z.string().min(1) });
type User = z.infer<typeof UserSchema>;
const user = UserSchema.parse(unknownData);  // throws on bad data
```

## Common mistakes

- `for...in` iterates keys as strings — use `for...of` on arrays.
- Object spread is a shallow copy; nested objects are still shared.
- `typeof null === "object"` — check `val !== null` before treating something as an object.
- Prefer `as const` objects with `typeof` unions over `enum` in library code.
