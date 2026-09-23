#include <stdio.h>
#include <stdlib.h>
#include "util.h"
#include "score.h"

static int helper(int v) {
    return v + 1;
}

/* Compare two players by score, for qsort. */
static int by_score(const void *a, const void *b) {
    const player_t *pa = a, *pb = b;
    return pa->score - pb->score;
}

int play_round(player_t *p) {
    int bonus = helper(p->score);
    p->score = clamp(bonus, 0, 100);
    if (g_verbose) printf("%s\n", p->name);
    return SQUARE(p->score);
}

void rank(player_t *ps, size_t n) {
    cmp_fn cmp = by_score;
    qsort(ps, n, sizeof *ps, cmp);
}
