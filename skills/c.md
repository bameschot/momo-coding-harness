# Skill: C Programming

## Style and Formatting

- Follow K&R brace style. 4-space indentation, no tabs.
- Name functions and variables in `snake_case`. Constants and macros in `UPPER_SNAKE_CASE`. Types (structs, typedefs) in `PascalCase` or `snake_case_t` depending on project convention.
- Limit line length to 80 characters. C code is often read in split terminals or over serial consoles.
- Every public function in a header file gets a one-line comment describing what it does, its preconditions, and what it returns.

## Memory Management

- Every `malloc` must have a corresponding `free`. Document ownership clearly: who allocates, who frees.
- Check every `malloc`/`calloc` return for `NULL`. Failing to do so is undefined behaviour waiting to happen.
- Prefer `calloc` over `malloc` when zero-initialisation is needed — it avoids accidental reads of uninitialised memory.
- Use `valgrind` (or AddressSanitizer with `-fsanitize=address`) to catch memory errors during development.
- Avoid double-free by setting pointers to `NULL` after freeing. A `free(NULL)` is a no-op; a double-free is undefined.

## Pointers and Types

- Make pointer ownership and lifetime explicit in naming or comments. If a pointer is borrowed (not owned), note it.
- Use `const` on pointer parameters whenever the function does not modify the pointed-to data: `void print_msg(const char *msg)`.
- Prefer `size_t` for sizes and indices. Avoid signed/unsigned comparison warnings.
- Do not cast the return of `malloc` in C — it masks missing `#include <stdlib.h>` and is not needed in C (unlike C++).
- Use `ptrdiff_t` for pointer arithmetic results.

## Error Handling

- C has no exceptions. Every function that can fail must communicate failure through its return value or an out-parameter.
- Use a consistent convention: return 0 on success and a negative errno value on failure, OR return a pointer and set `errno`, OR return a result struct. Pick one and be consistent.
- Always check return values from system calls (`read`, `write`, `open`, `malloc`, etc.).
- Use `perror()` or `strerror(errno)` to produce human-readable error messages.

## Safety and Undefined Behaviour

- Avoid undefined behaviour (UB): signed integer overflow, out-of-bounds access, use-after-free, reading uninitialised values, null pointer dereference. UB is not just wrong — it makes the compiler's optimiser produce actively hostile code.
- Use `-Wall -Wextra -Werror` during development. Treat warnings as bugs.
- Prefer bounded string functions: `strncpy`, `snprintf`, `strncat`. Never use `gets`. Be careful with `strcpy` and `sprintf`.
- Compile with `-fsanitize=address,undefined` during testing to catch UB and memory errors at runtime.

## Structure and Modularity

- One `.c` file per logical module. Expose only what is needed in the corresponding `.h` — keep internal functions `static`.
- Use `#include` guards (or `#pragma once`) in every header file.
- Keep header files self-contained: they should compile cleanly after being included first with no prior includes.
- Avoid circular dependencies between modules. The dependency graph should be a DAG.

## Build

- Use a `Makefile` for any project with more than one file. Define `CC`, `CFLAGS`, `LDFLAGS` as variables.
- Separate object file compilation from linking. Use automatic dependency tracking (`-MMD -MP`).
- Target: `debug` (with `-g -O0 -fsanitize=address,undefined`) and `release` (with `-O2 -DNDEBUG`).
