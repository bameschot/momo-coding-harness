#include <string.h>
#include "util.h"

int g_verbose = 0;

static int helper(int v) {
    return v * 2;
}

int clamp(int v, int lo, int hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return helper(v) / 2;
}

size_t name_len(const player_t *p) {
    return strlen(p->name);
}
