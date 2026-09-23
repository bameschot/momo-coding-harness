#ifndef UTIL_H
#define UTIL_H

#include <stddef.h>

#define MAX_NAME 32
#define SQUARE(x) ((x) * (x))

typedef struct {
    char name[MAX_NAME];
    int score;
} player_t;

typedef int (*cmp_fn)(const void *, const void *);

enum color { RED, GREEN, BLUE };

extern int g_verbose;

int clamp(int v, int lo, int hi);
size_t name_len(const player_t *p);

#endif
