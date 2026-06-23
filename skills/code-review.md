# Skill: Code Review and Analysis

## Review Mindset

- The goal of a review is to improve the code, not to prove the author wrong. Be precise about what the problem is and why it matters.
- Distinguish between objective issues (correctness, security, performance) and preferences (style, naming). Label them clearly. Block on the former; suggest on the latter.
- Assume good intent and reasonable competence. Ask questions before asserting mistakes: "I'm not sure I follow why X — can you explain the reasoning?" is better than "X is wrong."
- Review the code, not the person. "This function does too much" not "you made this too complex."

## What to Look For

### Correctness
- Does the code do what it is supposed to do in all cases, including edge cases?
- Are error paths handled? What happens when a dependency fails, returns null, or throws?
- Are there race conditions, deadlocks, or TOCTOU (time-of-check/time-of-use) issues in concurrent code?
- Does the code handle the full range of valid inputs, including empty collections, zero, negative numbers, unicode, very long strings?
- Are there off-by-one errors in loops, slicing, or index arithmetic?

### Security
- Is user input validated and sanitised before use? Check for injection risks (SQL, shell, path traversal, XSS).
- Are secrets, credentials, or PII handled correctly? Never logged, never returned in API responses, never stored in plaintext.
- Is authentication/authorisation enforced at every entry point, not just the happy path?
- Are cryptographic operations using well-known libraries and recommended algorithms? No custom crypto.
- Are file paths normalised and sandboxed to prevent directory traversal?

### Design and Maintainability
- Does each function/class/module do one thing? Is the level of abstraction consistent within a unit?
- Are dependencies explicit (passed in) or hidden (global state, hard-coded instantiation)?
- Would someone unfamiliar with this codebase understand what this code does and why?
- Is there duplicated logic that should be extracted?
- Are magic numbers and magic strings named as constants?

### Performance
- Are there obvious algorithmic inefficiencies — O(n²) where O(n log n) or O(n) is feasible?
- Are expensive operations (I/O, network calls, heavy computation) called in a loop unnecessarily?
- Are large objects copied when they could be referenced?
- Are database queries inside loops (N+1 problem)?
- Only raise performance issues when the impact is material or the fix is straightforward — do not micro-optimise.

### Tests
- Are the new or changed behaviours covered by tests?
- Do the tests actually verify the behaviour, or do they just execute the code path?
- Are the tests independent, fast, and deterministic?
- Are edge cases and failure modes tested, not just the happy path?

## Giving Feedback

- Be specific: point to the exact line, explain the issue, and suggest a concrete alternative where possible.
- Separate blockers from suggestions. Use consistent labels: `[blocker]`, `[suggestion]`, `[nit]`, `[question]`.
- If you are raising a concern without a clear solution, say so: "I am not sure of the best fix here but this pattern worries me because..."
- Acknowledge good work. If something is particularly clean or clever, say so — reviews should not be purely critical.
- Group related comments rather than leaving one comment per line on the same underlying issue.

## Analysing Unfamiliar Code

- Start with the entry points: `main`, request handlers, event listeners, public API surface.
- Trace data flow from input to output. Where does data come in? Where is it written? Where is it returned?
- Identify the invariants: what must always be true? Look for where those invariants are established and where they could be violated.
- Look for the seams: where are external dependencies called? These are the most likely places for errors, latency, and coupling.
- Read the tests before the implementation. Tests describe intended behaviour and reveal what the author thought the edge cases were.
- Identify what is NOT handled — missing error cases, unhandled input ranges, absent logging.
