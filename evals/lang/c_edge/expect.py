"""Edge cases for C: a multi-line macro, a named struct behind a typedef, a
struct with a function-pointer member, a union, static inline functions in a
header, functions returning pointers, #ifdef alternatives of one function,
anonymous enums, variadic functions and a call through a function pointer.
Format: see evals/lang/python/expect.py."""

H, S = "list.h", "list.c"

DEFINITIONS = [
    ("LIST_FOREACH", "macro",    H, "#define LIST_FOREACH"),
    ("node",         "struct",   H, "typedef struct node {"),
    ("node_t",       "typedef",  H, "} node_t;"),
    ("ops",          "struct",   H, "struct ops {"),
    ("number",       "union",    H, "union number {"),
    ("list_empty",   "function", H, "static inline int list_empty("),
    ("LIST_MAX",     "constant", S, "enum { LIST_MAX = 128 }"),
    ("list_push",    "function", S, "node_t *list_push(node_t *head, int value) {"),
    ("list_sum",     "function", S, "int list_sum(const node_t *head) {"),
    ("scaled",       "function", S, "int scaled(int v) { return v << 1; }"),
    ("scaled",       "function", S, "int scaled(int v) { return v * 2; }"),
    ("apply",        "function", S, "int apply(struct ops *op"),
    ("sum_all",      "function", S, "int sum_all(int count, ...)"),
]

ABSENT = [
    ("list_push", H),          # prototype
    ("total",     S),          # local
    ("ap",        S),
]

SEARCHES = [
    ("push",        {},                 "list_push"),
    ("sum list",    {},                 "list_sum"),
    ("foreach",     {},                 "LIST_FOREACH"),
    ("number",      {"kind": "union"},  "number"),
    ((S, "return op->run(scaled(v))"), {}, "apply"),
]

CALLERS = {
    "list_sum": {
        (S, "return list_empty(NULL) ? s : list_sum(NULL)", "call"),
        (S, "    return list_sum(NULL);", "call"),        # after the block's local list_sum
    },
    "scaled": {
        (S, "return op->run(scaled(v))", "call"),
    },
    "LIST_FOREACH": {
        (S, "LIST_FOREACH(n, head) {", "call"),
    },
    "list_empty": {
        (S, "return list_empty(NULL) ? s : list_sum(NULL)", "call"),
    },
}

CHAINS = {
    "scaled": set(),
}

IMPORTS = {
    H: {S},
    S: set(),
    "sub/list.h": set(),            # `#include "list.h"` in list.c means the one next to it
}

FRESH = (S, "\nint fresh_marker(void) { return 1; }\n", "fresh_marker")

KNOWN_GAPS = {}
