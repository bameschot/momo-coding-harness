#include <stdlib.h>
#include <stdarg.h>
#include "list.h"

enum { LIST_MAX = 128 };

node_t *list_push(node_t *head, int value) {
    node_t *n = malloc(sizeof *n);
    n->value = value;
    n->next = head;
    return n;
}

int list_sum(const node_t *head) {
    int total = 0;
    const node_t *n;
    LIST_FOREACH(n, head) {
        total += n->value;
    }
    return total;
}

#ifdef FAST_MATH
int scaled(int v) { return v << 1; }
#else
int scaled(int v) { return v * 2; }
#endif

int apply(struct ops *op, int v) {
    return op->run(scaled(v));
}

int sum_all(int count, ...) {
    va_list ap;
    int s = 0;
    va_start(ap, count);
    for (int i = 0; i < count; i++) s += va_arg(ap, int);
    va_end(ap);
    return list_empty(NULL) ? s : list_sum(NULL);
}
