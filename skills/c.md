# Skill: C Programming

## Core rules

- Every `malloc`/`calloc` must have a matching `free`. Document who owns and who frees.
- Check every allocation for `NULL` before using it.
- After `free(p)`, set `p = NULL` to prevent double-free (`free(NULL)` is a no-op).
- Compile with `-Wall -Wextra -Werror`. Treat every warning as a bug.
- Test with `-fsanitize=address,undefined` and/or `valgrind`.
- Use `const` on pointer params you do not modify: `void print(const char *s)`.
- Use `size_t` for sizes and indices. Do NOT cast the return of `malloc`.
- `snake_case` for functions/variables, `UPPER_SNAKE_CASE` for macros/constants. 4-space indent, K&R braces.

## Safety (avoid undefined behaviour)

- No out-of-bounds access, use-after-free, uninitialised reads, or NULL deref.
- Use bounded string functions: `snprintf`, `strncpy`, `strncat`. NEVER use `gets`.
- Check integer overflow before it happens: `if (a > INT_MAX - b) { /* overflow */ }`.

```c
/* Safe copy — always NUL-terminates */
char dst[64];
snprintf(dst, sizeof(dst), "%s", src);

/* Ownership explicit in the name: caller must free the result */
char *create_greeting(const char *name);
```

## Modularity

- One `.c` per module. Keep internal functions `static`; expose only what belongs in the `.h`.
- Every header needs an include guard (`#ifndef X_H` … or `#pragma once`) and must compile standalone.
- Use opaque types (`typedef struct Queue Queue;` in the header, definition in the `.c`) to hide internals.

## Error handling

C has no exceptions — pick one convention and keep it:

```c
/* return 0 on success, -1 on error (errno set) */
int parse_config(const char *path, Config *out);
/* or return a pointer, NULL on error */
Config *config_load(const char *path);
```

Always check return values; send errors to stderr (`fprintf(stderr, ...)` / `perror`).

## Macros

- Parenthesise every argument and the whole body: `#define MAX(a,b) ((a) > (b) ? (a) : (b))`.
- Wrap multi-statement macros in `do { ... } while (0)`.
- Prefer `static inline` functions over macros — type-safe and debuggable.

## Tools

- `gdb` (interactive), `valgrind --leak-check=full` (leaks), ASan/UBSan (fast runtime checks), `strace` (syscalls).
