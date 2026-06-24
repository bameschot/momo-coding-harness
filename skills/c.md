# Skill: C Programming

## Style and Formatting

- Follow K&R brace style. 4-space indentation, no tabs.
- Name functions and variables in `snake_case`. Constants and macros in `UPPER_SNAKE_CASE`. Types (structs, typedefs) in `PascalCase` or `snake_case_t` depending on project convention.
- Limit line length to 80 characters. C code is often read in split terminals or over serial consoles.
- Every public function in a header file gets a one-line comment describing what it does, its preconditions, and what it returns.

## Memory Management

- Every `malloc` must have a corresponding `free`. Document ownership clearly: who allocates, who frees.
- Check every `malloc`/`calloc` return for `NULL`.
- Prefer `calloc` over `malloc` when zero-initialisation is needed.
- Use `valgrind` or AddressSanitizer (`-fsanitize=address`) to catch memory errors during development.
- Avoid double-free by setting pointers to `NULL` after freeing — `free(NULL)` is a no-op.

```c
/* Ownership is explicit in naming: caller receives ownership and must free */
char *create_greeting(const char *name) {
    size_t len = strlen("Hello, ") + strlen(name) + 2;
    char *buf = malloc(len);
    if (!buf) return NULL;
    snprintf(buf, len, "Hello, %s!", name);
    return buf;
}

void use_greeting(void) {
    char *g = create_greeting("World");
    if (!g) { /* handle OOM */ return; }
    puts(g);
    free(g);
    g = NULL;  /* prevent double-free */
}
```

## Pointers and Types

- Make pointer ownership and lifetime explicit in naming or comments.
- Use `const` on pointer parameters whenever the function does not modify the pointed-to data: `void print_msg(const char *msg)`.
- Prefer `size_t` for sizes and indices. Avoid signed/unsigned comparison warnings.
- Do not cast the return of `malloc` in C — it masks missing `#include <stdlib.h>`.
- Use `ptrdiff_t` for pointer arithmetic results.

```c
/* const-correct function signatures */
size_t count_char(const char *str, char ch);   /* doesn't modify str */
void reverse(char *buf, size_t len);           /* modifies buf */

/* Avoid common pointer bugs */
int arr[10];
int *p = arr + 10;   /* one-past-end is valid, but *p is UB */
int *q = arr - 1;    /* always UB, even without dereference */
```

## Function pointers and callbacks

```c
/* Function pointer type alias — improves readability */
typedef int (*comparator_t)(const void *a, const void *b);

void sort(void *base, size_t nmemb, size_t size, comparator_t cmp) {
    qsort(base, nmemb, size, cmp);
}

int compare_ints(const void *a, const void *b) {
    int x = *(const int *)a;
    int y = *(const int *)b;
    return (x > y) - (x < y);  /* branchless, avoids overflow */
}

/* Callback with user data pointer — the standard pattern */
typedef void (*event_handler_t)(int event, void *user_data);

void register_handler(EventLoop *loop, event_handler_t cb, void *user_data);
```

## Struct patterns

```c
/* Opaque type — hide implementation in .c file, expose only pointer in .h */
/* In queue.h: */
typedef struct Queue Queue;
Queue *queue_new(size_t capacity);
void   queue_push(Queue *q, void *item);
void  *queue_pop(Queue *q);
void   queue_free(Queue *q);

/* In queue.c: */
struct Queue {
    void **items;
    size_t head, tail, capacity, count;
};

/* Embedded struct — composition without pointers */
typedef struct {
    int x, y;
} Point;

typedef struct {
    Point origin;     /* embedded, not a pointer */
    int width, height;
} Rect;

Rect r = { .origin = { 10, 20 }, .width = 100, .height = 50 };
```

## Macros — best practices

```c
/* Always parenthesise arguments and the whole expression */
#define MAX(a, b)       ((a) > (b) ? (a) : (b))
#define ARRAY_LEN(arr)  (sizeof(arr) / sizeof((arr)[0]))
#define UNUSED(x)       ((void)(x))

/* Multi-statement macros: wrap in do { } while (0) */
#define CHECK_NULL(ptr, ret)     \
    do {                         \
        if (!(ptr)) return (ret);\
    } while (0)

/* Prefer inline functions over macros when possible — type-safe, debuggable */
static inline int max_int(int a, int b) { return a > b ? a : b; }

/* Avoid side effects in macro arguments — MAX(x++, y++) evaluates x++ twice */
```

## Error handling

```c
/* C has no exceptions — use return values or out-parameters consistently */

/* Pattern 1: return 0 on success, -1 (or errno) on error */
int parse_config(const char *path, Config *out) {
    FILE *f = fopen(path, "r");
    if (!f) return -1;      /* errno set by fopen */
    /* ... */
    fclose(f);
    return 0;
}

/* Pattern 2: return pointer, NULL on error */
Config *config_load(const char *path);

/* Pattern 3: result struct (Go-style) */
typedef struct { Config value; int err; char msg[128]; } ConfigResult;
ConfigResult config_load(const char *path);

/* Always check return values */
if (write(fd, buf, len) < 0) {
    perror("write failed");
    exit(EXIT_FAILURE);
}
```

## Safety and Undefined Behaviour

- Avoid undefined behaviour: signed integer overflow, out-of-bounds access, use-after-free, reading uninitialised values, null pointer dereference.
- Use `-Wall -Wextra -Werror` during development. Treat warnings as bugs.
- Prefer bounded string functions: `strncpy`, `snprintf`, `strncat`. Never use `gets`. Prefer `strlcpy`/`strlcat` where available.
- Compile with `-fsanitize=address,undefined` during testing.

```c
/* Safe string copy pattern */
char dst[64];
snprintf(dst, sizeof(dst), "%s", src);  /* always NUL-terminates */

/* Safe integer arithmetic — check before, not after */
if (a > INT_MAX - b) { /* overflow would occur */ }
int sum = a + b;
```

## Structure and Modularity

- One `.c` file per logical module. Expose only what is needed in the corresponding `.h` — keep internal functions `static`.
- Use `#include` guards (or `#pragma once`) in every header file.
- Keep header files self-contained: they should compile cleanly when included first.
- Avoid circular dependencies between modules. The dependency graph should be a DAG.

```c
/* queue.h */
#ifndef QUEUE_H
#define QUEUE_H
/* or equivalently: #pragma once */

#include <stddef.h>   /* size_t — include what you use */

typedef struct Queue Queue;
Queue *queue_new(size_t capacity);
/* ... */

#endif /* QUEUE_H */
```

## Build

```makefile
CC      = gcc
CFLAGS  = -Wall -Wextra -Werror -std=c11 -MMD -MP
LDFLAGS =

# Debug vs release
debug:   CFLAGS += -g -O0 -fsanitize=address,undefined
release: CFLAGS += -O2 -DNDEBUG

SRCS = $(wildcard src/*.c)
OBJS = $(SRCS:src/%.c=build/%.o)
DEPS = $(OBJS:.o=.d)

-include $(DEPS)   # auto-generated dependency files

build/%.o: src/%.c
	$(CC) $(CFLAGS) -c $< -o $@

program: $(OBJS)
	$(CC) $(LDFLAGS) $^ -o $@
```

## Debugging tools

- `gdb` for interactive debugging: `gdb ./program`, `run`, `bt` (backtrace), `p var` (print variable).
- `valgrind --leak-check=full ./program` — memory leak and invalid access detection.
- `AddressSanitizer` (`-fsanitize=address`) — faster than valgrind, catches use-after-free, buffer overflows.
- `UBSanitizer` (`-fsanitize=undefined`) — catches undefined behaviour at runtime.
- `strace ./program` — trace system calls (Linux); useful for debugging I/O and permissions issues.
