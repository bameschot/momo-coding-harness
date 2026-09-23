#include "score.h"

int total_score(const player_t *ps, size_t n) {
    int sum = 0;
    for (size_t i = 0; i < n; i++) {
        sum += clamp(ps[i].score, 0, 100);
    }
    return sum;
}

int best(player_t *ps, size_t n) {
    rank(ps, n);
    return play_round(&ps[0]);
}
