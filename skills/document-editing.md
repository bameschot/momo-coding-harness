# Skill: Document Editing

## Before editing

- Read the document first. Understand its current structure, tone, and audience before making any changes.
- Identify the document's goal: inform, instruct, persuade, or narrate? Every edit should serve that goal.
- When editing someone else's writing, preserve their voice. Fix clarity and structure; do not impose a different style.

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
- Eliminate filler: remove "very", "really", "quite", "basically", "in order to", "it is important to note that", and similar hedges unless they carry meaning.
- Prefer concrete nouns over abstract ones. "The API returns a 404" is clearer than "The system exhibits failure behaviour."

## Editing passes

Run the document through these passes in order:
1. **Structure pass** — does the document have a logical flow? Are sections in the right order?
2. **Content pass** — is anything missing? Is anything redundant or tangential?
3. **Clarity pass** — are sentences clear and direct? Is jargon explained?
4. **Polish pass** — grammar, spelling, consistent capitalisation and terminology.

## Technical documents

- Define acronyms on first use: "Large Language Model (LLM)". Use the short form thereafter.
- Use the same term for the same concept throughout. Do not alternate between "user", "customer", and "client" unless they mean different things.
- Code, commands, filenames, and exact strings in backtick formatting.
- In READMEs and guides, lead with what the user can do, not what the system does ("Run `make test` to check all tests pass" not "The test suite is run with `make test`").

## Markdown

- ATX headings (`#`, `##`) not underline style.
- One blank line between paragraphs; two blank lines before top-level sections if the document is long.
- Fenced code blocks with language identifiers (` ```python `).
- Avoid bold for decoration — use it only for terms being defined or warnings that require attention.
- Keep line lengths under 100 characters in source for diff readability.
