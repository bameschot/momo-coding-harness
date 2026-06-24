# Skill: TypeScript

## Type system

- Enable `"strict": true` in `tsconfig.json`. It turns on `noImplicitAny`, `strictNullChecks`, and several others — all of which catch real bugs.
- Prefer `interface` for object shapes that may be extended; prefer `type` for unions, intersections, and computed types.
- Use `readonly` on properties that should not be mutated after construction.
- Avoid `any`. Use `unknown` for genuinely dynamic values — it forces you to narrow before use. Use `never` to mark branches that should be unreachable.
- `as SomeType` assertions bypass the type checker — only use them when you know more than the compiler. Add a comment explaining why.

## Narrowing and discriminated unions

```typescript
// Discriminated unions — exhaustiveness checked by never
type Shape =
    | { kind: "circle"; r: number }
    | { kind: "rect"; w: number; h: number }
    | { kind: "triangle"; base: number; height: number };

function area(s: Shape): number {
    switch (s.kind) {
        case "circle":   return Math.PI * s.r ** 2;
        case "rect":     return s.w * s.h;
        case "triangle": return 0.5 * s.base * s.height;
        default: {
            const _exhaustive: never = s; // compile error if a case is missing
            throw new Error(`Unhandled shape: ${_exhaustive}`);
        }
    }
}

// Type guards
function isString(v: unknown): v is string {
    return typeof v === "string";
}

function isUser(v: unknown): v is User {
    return (
        typeof v === "object" && v !== null &&
        "id" in v && typeof (v as any).id === "number" &&
        "name" in v && typeof (v as any).name === "string"
    );
}

// Assertion function
function assertDefined<T>(val: T | null | undefined, msg?: string): asserts val is T {
    if (val == null) throw new Error(msg ?? "Expected defined value");
}
```

## Generics

```typescript
// Basic generic function
function first<T>(arr: T[]): T | undefined {
    return arr[0];
}

// Constrained generics
function getProperty<T, K extends keyof T>(obj: T, key: K): T[K] {
    return obj[key];
}

// Generic class
class Repository<T extends { id: number }> {
    private items: T[] = [];
    add(item: T): void { this.items.push(item); }
    findById(id: number): T | undefined {
        return this.items.find(i => i.id === id);
    }
}

// Conditional types
type NonNullable<T> = T extends null | undefined ? never : T;
type Flatten<T> = T extends Array<infer U> ? U : T;
type ReturnType<F extends (...args: any) => any> = F extends (...args: any) => infer R ? R : never;

// Mapped types
type Readonly<T> = { readonly [K in keyof T]: T[K] };
type Optional<T> = { [K in keyof T]?: T[K] };
type Mutable<T> = { -readonly [K in keyof T]: T[K] };
type Nullable<T> = { [K in keyof T]: T[K] | null };

// Template literal types
type EventName = "click" | "focus" | "blur";
type Handler = `on${Capitalize<EventName>}`;  // "onClick" | "onFocus" | "onBlur"

type HttpMethod = "GET" | "POST" | "PUT" | "DELETE";
type Endpoint = `/${string}`;
type Route = `${HttpMethod} ${Endpoint}`;
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
| `Awaited<T>` | Unwrap a Promise type |
| `Extract<T, U>` | Members of T assignable to U |
| `Exclude<T, U>` | Members of T not assignable to U |

```typescript
// Practical utility type usage
type CreateUserDto = Omit<User, "id" | "createdAt">;
type UpdateUserDto = Partial<Omit<User, "id">>;
type UserSummary = Pick<User, "id" | "name" | "email">;

// Deep readonly
type DeepReadonly<T> = {
    readonly [K in keyof T]: T[K] extends object ? DeepReadonly<T[K]> : T[K];
};
```

## Async patterns

```typescript
// async/await over raw Promises — stack traces are readable
async function fetchUser(id: number): Promise<User> {
    const res = await fetch(`/api/users/${id}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    return res.json() as Promise<User>;
}

// Parallel requests
const [users, posts] = await Promise.all([fetchUsers(), fetchPosts()]);

// allSettled — results even when some fail
const results = await Promise.allSettled(urls.map(fetch));
const succeeded = results
    .filter((r): r is PromiseFulfilledResult<Response> => r.status === "fulfilled")
    .map(r => r.value);

// Type-safe error handling
async function safeParseUser(id: number): Promise<User | null> {
    try {
        return await fetchUser(id);
    } catch (err) {
        console.error("Failed to fetch user:", err);
        return null;
    }
}
```

## Runtime validation

Use `zod` or `valibot` to validate data at system boundaries (API responses, env vars, form input).

```typescript
import { z } from "zod";

// Define schema
const UserSchema = z.object({
    id: z.number().int().positive(),
    name: z.string().min(1).max(100),
    email: z.string().email(),
    role: z.enum(["admin", "user", "guest"]),
    createdAt: z.string().datetime(),
});

// Infer the TypeScript type from the schema — single source of truth
type User = z.infer<typeof UserSchema>;

// Validate — throws ZodError with detailed message on failure
const user = UserSchema.parse(unknownData);

// Safe parse — returns { success, data } or { success, error }
const result = UserSchema.safeParse(unknownData);
if (result.success) {
    console.log(result.data.name);
} else {
    console.error(result.error.flatten());
}

// Environment variable validation
const EnvSchema = z.object({
    DATABASE_URL: z.string().url(),
    PORT: z.string().transform(Number).pipe(z.number().positive()),
    NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
});
const env = EnvSchema.parse(process.env);
```

## Common pitfalls

- `for...of` over arrays; `for...in` iterates keys (strings), not values — avoid it on arrays.
- `===` not `==` everywhere.
- Object spread `{ ...obj, key: val }` creates a shallow copy; nested objects are still shared.
- Optional chaining `?.` short-circuits on `null`/`undefined`; nullish coalescing `??` provides defaults for `null`/`undefined` only (unlike `||` which also fires on `0`, `""`, `false`).
- Avoid `enum` in library code — prefer `as const` objects with `typeof` unions, which are simpler in emitted JS and work better with tree-shaking.
- `typeof null === "object"` — always check `val !== null` when checking for objects.
- Type assertions (`as Foo`) silently bypass the type checker. Use type guards instead.

```typescript
// Prefer as const over enum
const Direction = {
    Up: "UP",
    Down: "DOWN",
    Left: "LEFT",
    Right: "RIGHT",
} as const;
type Direction = typeof Direction[keyof typeof Direction];
// "UP" | "DOWN" | "LEFT" | "RIGHT"
```

## Project setup

```json
// tsconfig.json — recommended strict baseline
{
    "compilerOptions": {
        "strict": true,
        "target": "ES2022",
        "module": "NodeNext",
        "moduleResolution": "NodeNext",
        "outDir": "./dist",
        "rootDir": "./src",
        "declaration": true,
        "sourceMap": true,
        "noUncheckedIndexedAccess": true,
        "exactOptionalPropertyTypes": true
    }
}
```

- `"target": "ES2022"` or later for top-level `await`, `Array.at()`, `Object.hasOwn()`.
- `"noUncheckedIndexedAccess": true` — array indexing returns `T | undefined`, not `T`. Safer.
- `"exactOptionalPropertyTypes": true` — `{ x?: string }` means `x` is `string | undefined`, not also explicitly `undefined`.
- Use `tsx` for running TypeScript directly in development: `npx tsx src/index.ts`.
- `tsc --noEmit` as a typecheck-only step in CI.
