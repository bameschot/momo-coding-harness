# Skill: Document Editing

## Core rules

- Read the whole document first. Identify its goal (inform, instruct, persuade) and audience before editing.
- When editing someone else's writing, preserve their voice — fix clarity and structure, don't impose a new style.
- One idea per paragraph. Lead each paragraph with its main point.
- Default to active voice: "The system logs the error," not "The error is logged by the system."
- Cut filler: "very", "really", "basically", "in order to", "it is important to note that".
- Prefer concrete nouns over abstract ones. Use the same term for the same concept throughout.
- Break sentences over ~25 words if they split cleanly.

## Editing passes (in order)

1. **Structure** — is the flow logical? Are sections in the right order?
2. **Content** — anything missing, redundant, or tangential?
3. **Clarity** — are sentences direct? Is jargon explained?
4. **Polish** — grammar, spelling, consistent terminology and capitalisation.

## Before / after

- "It is important to note that utilisation of this methodology may result in performance degradation in certain edge cases."
  → "This approach can slow performance in edge cases."
- "In the event that the connection fails, the system will attempt to retry a maximum of three times."
  → "If the connection fails, the system retries up to three times."

## Structure devices

- Headings for anything over 3–4 paragraphs; headings describe content ("How context windows work"), not just label it.
- Numbered lists for ordered steps; bullets for unordered items; tables for comparisons across consistent attributes.
- Define acronyms on first use, then use the short form.

## Document types (lead with the reader)

- **README** — first paragraph: what it does and who it's for. Prerequisites before usage. Minimal working example first.
- **API docs** — document every parameter, return value, and error; include a request/response example.
- **Runbook** — symptoms → causes → remediation. Paste exact commands and expected output.
- **RFC** — problem statement first, then alternatives considered, decision + rationale, open questions.

## Markdown

- ATX headings (`#`, `##`). Fenced code blocks with a language tag. Backticks for code, commands, filenames.
- Use bold only for defined terms or warnings, not decoration.
