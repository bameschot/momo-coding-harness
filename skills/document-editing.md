# Skill: Document Editing

## Before Editing

- Read the document first. Understand its current structure, tone, and audience before making any changes.
- Identify the document's goal: inform, instruct, persuade, or narrate? Every edit should serve that goal.
- When editing someone else's writing, preserve their voice. Fix clarity and structure; do not impose a different style.
- Identify the audience: are they experts in this domain or newcomers? The vocabulary, assumed context, and level of explanation should match.

## Structure

- One idea per paragraph. If a paragraph covers two ideas, split it.
- Lead with the main point. Put the most important sentence first in each paragraph (inverted pyramid).
- Use headings to break documents longer than 3–4 paragraphs into navigable sections. Headings should describe content, not label it ("How context windows work" not "Context windows").
- Numbered lists for sequences where order matters; bullet lists for unordered items. Do not use bullets for things that flow naturally as prose.
- Tables for comparisons with consistent attributes across multiple items.

## Clarity

- Short sentences are usually clearer. Break sentences longer than 25 words if they can be split without losing meaning.
- Vary sentence length for rhythm — a sequence of short sentences becomes choppy; occasional longer ones add flow.
- Active voice: "The system logs the error" not "The error is logged by the system."
- Eliminate filler: remove "very", "really", "quite", "basically", "in order to", "it is important to note that". If these words add nothing, delete them.
- Prefer concrete nouns over abstract ones. "The API returns a 404" is clearer than "The system exhibits failure behaviour."

## Editing Passes

Run the document through these passes in order:
1. **Structure pass** — does the document have a logical flow? Are sections in the right order?
2. **Content pass** — is anything missing? Is anything redundant or tangential?
3. **Clarity pass** — are sentences clear and direct? Is jargon explained?
4. **Polish pass** — grammar, spelling, consistent capitalisation and terminology.

## Before/After Examples

**Before:** "It is important to note that the utilisation of the aforementioned methodology may result in potential performance degradation in certain edge case scenarios."

**After:** "This approach can slow down performance in edge cases."

---

**Before:** "Users should be aware that in the event that the connection fails, the system will attempt to retry the operation a maximum of three times before returning an error."

**After:** "If the connection fails, the system retries up to three times before returning an error."

---

**Before:** "There are a number of different ways in which this problem can be approached."

**After:** "This problem can be approached in several ways."

## Technical Documents

- Define acronyms on first use: "Large Language Model (LLM)". Use the short form thereafter.
- Use the same term for the same concept throughout. Do not alternate between "user", "customer", and "client" unless they mean different things.
- Code, commands, filenames, and exact strings in backtick formatting.
- In READMEs and guides, lead with what the user can do, not what the system does: "Run `make test` to check all tests pass" not "The test suite is run with `make test`".

## Document Types

### README
- First paragraph: what does this do and who is it for?
- Requirements and setup before usage. Do not bury prerequisites.
- Show the minimal working example first, then options and advanced usage.
- Keep it current — a wrong README is worse than no README.

### API Documentation
- Document every public parameter, return value, and error condition.
- Include a request/response example for every endpoint.
- Document the error codes and what they mean, not just that they exist.
- Rate limits, authentication, and pagination belong near the top, not buried.

### Runbook / Operational Guide
- Describe symptoms first, then causes, then remediation steps.
- Steps must be executable as written — paste the actual commands, not summaries.
- Include expected output for verification steps.
- Note what NOT to do as prominently as what to do.

### RFC / Design Proposal
- Problem statement first — what is broken or missing and why does it matter?
- Alternatives considered — show you evaluated other approaches.
- Decision and rationale — what was chosen and why (tradeoffs, not just features).
- Open questions — unresolved issues belong here, not scattered through the prose.

## Markdown

- ATX headings (`#`, `##`) not underline style.
- One blank line between paragraphs; two blank lines before top-level sections if the document is long.
- Fenced code blocks with language identifiers (` ```python `).
- Avoid bold for decoration — use it only for terms being defined or warnings that require attention.
- Keep line lengths under 100 characters in source for diff readability.
- Use reference-style links for URLs that appear multiple times or are very long.
