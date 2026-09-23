"""Ground truth for the C synthetic project (evals/lang/c/project).  See
evals/lang/python/expect.py for the format.  Headers (.h) are parsed with the
C++ grammar, as in the harness."""

DEFINITIONS = [
    ("MAX_NAME",    "constant", "include/util.h", "#define MAX_NAME"),
    ("SQUARE",      "macro",    "include/util.h", "#define SQUARE"),
    ("player_t",    "typedef",  "include/util.h", "} player_t;"),
    ("cmp_fn",      "typedef",  "include/util.h", "typedef int (*cmp_fn)"),
    ("color",       "enum",     "include/util.h", "enum color"),
    ("RED",         "constant", "include/util.h", "enum color"),
    ("g_verbose",   "variable", "src/util.c",     "int g_verbose = 0"),
    ("helper",      "function", "src/util.c",     "static int helper"),
    ("clamp",       "function", "src/util.c",     "int clamp(int v, int lo, int hi) {"),
    ("name_len",    "function", "src/util.c",     "size_t name_len(const player_t *p) {"),
    ("helper",      "function", "src/game.c",     "static int helper"),
    ("by_score",    "function", "src/game.c",     "static int by_score"),
    ("play_round",  "function", "src/game.c",     "int play_round(player_t *p) {"),
    ("rank",        "function", "src/game.c",     "void rank(player_t *ps, size_t n) {"),
    ("total_score", "function", "src/score.c",    "int total_score("),
    ("best",        "function", "src/score.c",    "int best("),
]

ABSENT = [
    ("clamp",      "include/util.h"),     # a prototype, not a definition
    ("play_round", "include/score.h"),
    ("g_verbose",  "include/util.h"),     # extern declaration
    ("bonus",      "src/game.c"),         # local
    ("sum",        "src/score.c"),
]

SEARCHES = [
    ("clamp",             {},                        "clamp", "src/util.c"),
    ("helper",            {"path": "src/game.c"},    "helper", "src/game.c"),
    ("player",            {},                        "player_t"),
    ("square",            {},                        "SQUARE"),
    ("max name",          {},                        "MAX_NAME"),
    ("compare by score",  {},                        "by_score"),
    (("src/game.c", "return SQUARE"), {},            "play_round"),
]

CALLERS = {
    "clamp": {
        ("src/game.c",  "p->score = clamp(bonus",  "call"),
        ("src/score.c", "sum += clamp(",           "call"),
    },
    # Two different static helpers; each file calls its own.  By name both are
    # uses of "helper", which is the truth for a name query.
    "helper": {
        ("src/util.c",  "return helper(v) / 2",    "call"),
        ("src/game.c",  "int bonus = helper(",     "call"),
    },
    "play_round": {
        ("src/score.c", "return play_round(",      "call"),
    },
}

CHAINS = {
    "clamp": {"best"},                     # play_round <- best
}

IMPORTS = {
    "include/util.h":  {"src/util.c", "src/game.c", "include/score.h"},
    "include/score.h": {"src/game.c", "src/score.c"},
    "src/util.c":      set(),              # nothing includes a .c file
}

FRESH = ("src/score.c", "\nint fresh_marker(void) { return 1; }\n", "fresh_marker")

KNOWN_GAPS = {}
