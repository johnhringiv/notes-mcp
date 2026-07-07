---
name: resource-ingestion
description: Import reference documents (articles, specs, papers, book excerpts) into the notes-mcp resources library with the right fidelity, chunked transfer, and citation metadata. Use when the user asks to save, import, or dump a document, article, spec, or web page into their notes.
---

# Importing documents into the notes resources library

Resources are reference documents, distinct from notes: imported rather than
authored, read in ranges, never edited in place. They live under
`resources/<topic>/<slug>.md` and are searchable via `search_notes` (hits
carry a `resource_id`).

## Decide fidelity first

- **verbatim** — the exact text matters (specs, papers the user will quote,
  standards). Only feasible if you can reproduce the text faithfully.
- **digest** — a structured distillation: summary, key sections quoted
  exactly, the rest condensed. Use when the document is long, when only
  parts matter, or when a faithful full transfer is not feasible.

When in doubt, ask the user which they want. Never silently abridge a
transfer that claims `fidelity: verbatim` — if the document is too long to
transfer faithfully, say so and offer a digest or the desktop path.

## Size gates (be honest about transfer limits)

- **< ~1,500 lines**: full verbatim transfer is realistic. Proceed.
- **~1,500–5,000 lines**: verbatim only if the user confirms; expect many
  append calls. Digest usually serves better.
- **> ~5,000 lines (books)**: do not attempt through chat. Tell the user to
  convert on their desktop and commit directly to `resources/` in the notes
  repo — the server picks it up on its next pull.

## Procedure

1. Obtain the text (fetch the URL yourself, or read the user's upload).
2. Choose an id: `<topic>/<slug>.md` (e.g. `maeve/austral-spec.md`).
   Check `list_resources` for collisions — `add_resource` without append
   REPLACES an existing resource.
3. First `add_resource` call: frontmatter + the opening chunk:

   ```markdown
   ---
   title: The Austral Specification
   source: https://austral-lang.org/spec/spec.html
   retrieved: 2026-07-06
   fidelity: verbatim
   ---

   (first ~150 lines of content)
   ```

4. Continue with `add_resource(..., append=true)` in chunks of ~100–200
   lines. Preserve the original text exactly for verbatim imports —
   headings, code blocks, everything.
5. **Verify**: the final response's `total_lines` should match your
   expectation from the source. Spot-check with `read_resource` at a
   couple of offsets (start, middle, end). If content is missing, say so
   and repair or restart the transfer.
6. Report to the user: id, fidelity, size, source, and one line on how to
   find it again.

## Digest structure (when fidelity: digest)

Lead with a 5–10 line summary, then per-section condensations, quoting
exactly (in blockquotes) the passages most likely to be cited later.
Record what was omitted at the end, so nobody mistakes the digest for the
whole document.
