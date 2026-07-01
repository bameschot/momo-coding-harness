# Skill: Code Review and Analysis

## Core rules

- Review the code, not the person. Say "this function does too much," not "you made this too complex."
- Separate objective issues (correctness, security) from preferences (style, naming). Block on the former; suggest on the latter.
- Be specific: point to the line, explain the problem, suggest a concrete fix.
- Only review what is in the diff. Note tangential tech debt separately; don't block on it.
- Label severity so it is unambiguous: `[blocker]`, `[suggestion]`, `[nit]`, `[question]`.
- Acknowledge good work, not just problems.

## What to look for

**Correctness** — edge cases (empty, zero, negative, very long, unicode), off-by-one errors, error/failure paths, race conditions and TOCTOU in concurrent code.

**Security** — validate/sanitise all user input (SQL/shell/path/XSS injection); never log or return secrets/PII; enforce auth at every entry point; normalise file paths; no custom crypto.

**Design** — one responsibility per unit; explicit dependencies (not global state); no duplicated logic; named constants instead of magic numbers.

**Performance** — flag only material issues: N+1 queries, expensive calls inside loops, O(n²) where O(n) is easy. Don't micro-optimise.

**Tests** — new behaviour covered; tests verify behaviour (not just execute the path); edge cases and failures tested.

## Common smells

| Smell | Fix |
|---|---|
| Long function (>30 lines) | Extract intent-named sub-functions |
| Deep nesting (>3 levels) | Early returns / guard clauses |
| Flag parameter `doThis=true` | Split into two functions |
| Boolean return for errors | Throw or return Result/Optional |
| Comment explaining *what* the code does | Rename/restructure so it's self-evident |
| Magic number `if x > 47` | Name it: `MAX_RETRIES = 47` |
| Catch-all exception handler | Catch specific types; rethrow the rest |

## Reading unfamiliar code

- Start at the entry points (`main`, handlers, public API). Trace data from input to output.
- Identify invariants (what must always be true) and where they could be violated.
- Read the tests before the implementation — they describe intended behaviour and edge cases.
- Look for what is NOT handled: missing error cases, unhandled input ranges.
