#ifndef LIST_H
#define LIST_H

#define LIST_FOREACH(n, head) \
    for ((n) = (head); (n) != NULL; (n) = (n)->next)

typedef struct node {
    int value;
    struct node *next;
} node_t;

struct ops {
    int (*run)(int);
    const char *name;
};

union number {
    int i;
    double d;
};

static inline int list_empty(const node_t *head) {
    return head == NULL;
}

node_t *list_push(node_t *head, int value);
int list_sum(const node_t *head);

#endif
