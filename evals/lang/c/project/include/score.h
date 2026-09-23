#ifndef SCORE_H
#define SCORE_H

#include "util.h"

int total_score(const player_t *ps, size_t n);
int play_round(player_t *p);
void rank(player_t *ps, size_t n);
int best(player_t *ps, size_t n);

#endif
