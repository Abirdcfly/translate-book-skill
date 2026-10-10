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
   - Code/images/MathML → `⟨n⟩` atoms. Element preserved verbatim, **never** translated.
   - `<pre>`/`<code>` subtrees are skipped entirely.
2. **Self-test** — `units.py write --identity --strict`, then `diff -r` against
   the source. Text must match except for intentional normalizations, and the
   command must exit 0. **Do this before any translation.** It catches every
   index-alignment bug in minutes.
3. **Batch** — `units.py batch` into ~1600-word chunks. It writes `_plan.json`
   carrying a hash of every planned unit; the hash is what later detects outputs
   produced against an older plan.
4. **Glossary and register** — `units.py glossary <units>` seeds `glossary.json`
   from the source (capitalized terms, acronyms, `a.b` symbols). Fill in the
   empty values, then write the register note once. Both travel with every
   dispatch; without them parallel batches invent competing terms.
5. **Dispatch** — `units.py claim <batches> <done> --worker ID` prints the next
   unclaimed batch files. A worker writes into **its own run directory**, never
   into `done`.
6. **Promote** — `units.py complete <batches> <run_dir> <done> --units-dir <units>`
   validates the run directory and atomically copies only the files that pass.
   A worker's own "done" report is not evidence.
7. **Merge** — `units.py merge <units> <done> <trans> --batches-dir <batches>`
   refuses an incomplete plan by default; use `--allow-incomplete` only for an
   explicitly approved partial delivery.
8. **Write back** — `units.py write`. Validates tokens/atoms per unit and refuses
   to write a unit that fails.
9. **Package** — `epub_build.py`.
10. **Verify** — `epub_verify.py`, diffing element counts against the source.
11. **Sample** — `units.py sample <units> <trans> --out sample.json` draws a fixed
    review sample per chapter (long prose, headings, cross-references, units with
    tokens or atoms) and flags empty, untranslated, and marker-damaged units.

Re-run steps 8–11 freely. Re-running 1 is expensive once translations exist — see
*Reusing translations* in [REFERENCE.md](REFERENCE.md).

## Interrupted runs

`units.py status <batches> <done> --units-dir <units>` is the only source of
truth: `planned/complete/missing/invalid/claimed/stale`. Recover in this order:

- `missing` — re-claim those batches and re-dispatch: `units.py claim ... `
- `invalid` — the file exists but fails validation; re-dispatch it, do not edit it.
- `claimed` but the worker died — `units.py claim --worker ID --release <dead worker>`.
- `stale` — the plan changed since those outputs were promoted; `rekey` first,
  then re-dispatch the residue.

## Non-negotiables

- **The writer must not mutate the source.** Copy to a staging dir, overlay
  translations there, zip the staging dir. A builder that writes into the source
  turns the next extract into garbage (it parses the translated output as English).
- **Extractor and writer share one traversal function.** Enumerate elements
  independently and any disagreement silently shifts every index.
- **Materialize the element list before mutating the tree.** lxml iterators skip
  elements when children change mid-iteration — you get one paragraph per chapter.
- **Code is never translated.** Prose wrapped around inline code is.
- **MathML is never translated.** Inline equations are atoms; their surrounding
  prose remains a unit.
- **Validate before writing, never after.** Failed units keep their source text.
  Batch validation and merge must stop before a missing translation can become a
  finished EPUB.
- **A worker's self-report is not a merge gate.** Only `validate-batches` /
  `complete` decide. Sessions that trusted the report shipped books with whole
  batches still in English.
- **One worker, one directory.** Workers write to private run directories;
  promotion is a separate, atomic step. Two workers sharing `done` overwrite
  each other's half-finished files.

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

Use [AGENTS.md](AGENTS.md) as the prompt template. Give each worker a private
run directory, take its file list from `units.py claim`, and promote it with
`units.py complete`. The token/atom rules matter more than style guidance — a
subagent that mangles a token corrupts the book.

Two subagents at a time is the usual ceiling. Group batches by chapter so
terminology stays consistent within a run, and hand every worker the same
`glossary.json` and register note — terminology cannot be re-derived per batch.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/units.py` | extract / write / batch / glossary / claim / validate-batches / complete / status / merge / sample / rekey |
| `scripts/pdf_layout.py` | PDF → structured document (coordinates + fonts) |
| `scripts/epub_build.py` | stage + package a valid EPUB |
| `scripts/epub_verify.py` | integrity, coverage, structure diff |
