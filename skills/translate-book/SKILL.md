---
name: translate-book
description: Translate an EPUB or PDF technical book into another language while preserving its structure — chapters, code blocks, links, figures, tables, code callouts — and rebuild a valid EPUB. Use when the user asks to 翻译整本书 / 翻译这本书 / translate a book / translate an EPUB or PDF book / 把 PDF 转成中文 epub / 转成 epub 格式, or hands over an .epub or .pdf book file with translation intent. Not for single articles or web pages — use baoyu-translate for those.
license: MIT
---

# Translate a Book

Structure-preserving book translation. The governing rule:

> **Never regenerate a book from plain text.** Parse it into translatable units,
> replace only the text, write it back into the original structure, then package.

Regenerating loses styles, anchors, figure placement, and code formatting. A book
has thousands of structural dependencies; markdown round-trips break them silently.

## Pipeline

1. **Extract** — `units.py extract` turns each chapter into units. Point it at the
   directory holding the chapter XHTML (usually `OEBPS/Text/`), and at the
   directory that holds only those files — the tree root will find nothing.
   - Inline markup → `⟧n⟦…⟧/n⟦` tokens (links, emphasis). Inner text **is** translated.
   - Code/images → `⟨n⟩` atoms. Element preserved verbatim, **never** translated.
   - `<pre>`/`<code>` subtrees are skipped entirely.
2. **Self-test** — `units.py write --identity`, then diff against the source.
   Text must match except for intentional normalizations. **Do this before any
   translation.** It catches every index-alignment bug in minutes.
3. **Batch** — `units.py batch` into ~1600-word chunks.
4. **Merge** — `units.py merge` folds finished batch outputs into per-chapter files.
5. **Write back** — `units.py write`. Validates tokens/atoms per unit and refuses
   to write a unit that fails.
6. **Package** — `epub_build.py`.
7. **Verify** — `epub_verify.py`, diffing element counts against the source.

Re-run steps 5–7 freely. Re-running 1 is expensive once translations exist — see
*Reusing translations* in [REFERENCE.md](REFERENCE.md).

## Non-negotiables

- **The writer must not mutate the source.** Copy to a staging dir, overlay
  translations there, zip the staging dir. A builder that writes into the source
  turns the next extract into garbage (it parses the translated output as English).
- **Extractor and writer share one traversal function.** Enumerate elements
  independently and any disagreement silently shifts every index.
- **Materialize the element list before mutating the tree.** lxml iterators skip
  elements when children change mid-iteration — you get one paragraph per chapter.
- **Code is never translated.** Prose wrapped around inline code is.
- **Validate before writing, never after.** Failed units keep their source text.
  A missing translation beats corrupted markup, and it shows up in verification.

## Knowing when the source is lying

Published books contain production defects: double-encoded entities, a stray
styled character mid-word, duplicated margin labels. Translate the intent and
record the exception — don't reproduce the defect, and don't silently drop it.
See the failure catalogue in [REFERENCE.md](REFERENCE.md).

## Reading order is the hard part (PDF only)

EPUB sources carry structure. PDFs do not — you rebuild it from glyph coordinates
and font names, and **the layout rules are per-book**: sample pages first, build a
font→role table, then extract. Recipe and pitfalls: [REFERENCE.md](REFERENCE.md).

## Delegating translation

Use [AGENTS.md](AGENTS.md) as the prompt template. The token/atom rules and the
JSON-escaping rule matter more than style guidance — a subagent that mangles a
token corrupts the book, and one that emits a raw `"` corrupts the file.

Two subagents at a time is the usual ceiling. Group batches by chapter so
terminology stays consistent within a run.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/units.py` | extract / write / batch / merge / rekey |
| `scripts/pdf_layout.py` | PDF → structured document (coordinates + fonts) |
| `scripts/epub_build.py` | stage + package a valid EPUB |
| `scripts/epub_verify.py` | integrity, coverage, structure diff |
