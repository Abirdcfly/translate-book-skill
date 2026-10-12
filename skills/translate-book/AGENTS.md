# Translation agent prompt template

Dispatch one subagent per group of batches. Fill the `<>` slots. The rules that
prevent corruption (tokens, atoms, JSON) are not negotiable; style guidance should
be tuned per book.

Two subagents at a time is the usual ceiling. Group batches by chapter so a run
keeps its own terminology consistent.

---

```
Translate the assigned batches of <BOOK TITLE> into <TARGET LANGUAGE>.

## Paths
- Input:  <batches_dir>
- Output: <run_dir>   (create if missing; a private directory for this worker)
- Glossary: <glossary.json>   (same file for every worker — use its terms verbatim)
- Register: <style.md>   (one or two sentences on this book's voice)

## Your batches (<N> files)
<file list from `units.py claim` — do not invent or expand this list>

## Input format
{"file": "<chapter>.json", "batch": <n>,
 "units": [{"key": "5.text", "src": "<source text>"},
           {"key": "9.callouts.0", "src": "…"}]}

## Output format
Write <done_dir>/<same filename>, identical structure, adding only an "out" field:
{"units": [{"key": "5.text", "src": "<source text>", "out": "<translation>"}]}

`key` and `src` must be preserved **character for character** — the pipeline
matches on `src` downstream. Never add, drop, merge, or reorder units.

## Markers that must survive translation

### Tokens: ⟧n⟦ … ⟧/n⟦
Mark the boundaries of inline markup (links, emphasis). Translate the text
between them; keep both markers. Sample:
  src: `see ⟧0⟦chapter 6⟧/0⟦ for the derivation`
  out: `参见⟧0⟦第 6 章⟧/0⟦的推导`

An **empty** pair `⟧0⟦⟧/0⟦` is an anchor with no visible text — usually an
index target. Copy it unchanged and in place. Every link token is mandatory;
dropping it rejects the unit.

### Atoms: ⟨n⟩
An inseparable inline element — `<code>json.loads</code>`, an inline icon.
Rules: the multiset of atoms must be identical to the source. Reposition them
freely so the sentence reads naturally; never delete, duplicate, renumber, or
translate inside one.
  src: `call the ⟨0⟩ function on the ⟨1⟩ dataset`
  out: `对⟨1⟩数据集调用⟨0⟩函数`

If a token or atom has no natural place in your sentence, place it at the
closest position that preserves meaning. Never drop one to make a sentence flow.

## Terminology
- <glossary.json> is authoritative. Use its `terms` verbatim; entries in
  `never_translate` stay in the source language, untouched, in every batch.
- If a term is missing from the glossary, translate it consistently and report it
  so the glossary can be extended for the following batches.
- Use the target-language term on first occurrence with the source term and
  abbreviation in parentheses, then either alone: 粒子群优化（particle swarm
  optimization, PSO）.
- Never translate: code, identifiers, function/class names, commands, filenames,
  API and library names, URLs, mathematical symbols. Leave numbers and units in
  tables untouched.
- Numbered references: `figure 7.19` → 图 7.19; `Table 7.2` → 表 7.2;
  `chapter 6` → 第 6 章; `listing 7.4` → 代码清单 7.4.

## Register
<one or two sentences on this book's voice — e.g. "the author teaches through
everyday analogies (ant colonies, bird flocks, mixing desks); keep that
conversational teaching tone rather than a dry textbook register">

If the source uses "we"/"you" to address the reader, natural target-language
ellipsis is fine; keep the sense of someone explaining.

## Unit kinds
- `N.callouts.M` — annotations extracted from a code listing's margin. Usually
  phrases or fragments. Translate them short; do not inflate into full sentences.
- `N.rows.R.C` — a table cell. Often a few words; translate only the words.
- `N.text` on a caption block — a figure or table caption. Translate it whole.
- Source fragments that are visibly incomplete (a missing inline formula leaves
  a gap, e.g. `based on a given .`) — preserve the gap. Do not invent content.

## JSON safety
Write valid JSON with a serializer. Straight quotes are allowed in translated
prose only when correctly JSON-escaped; never hand-write an unescaped `"`.

Modify **only** the `out` field. Never touch `key` or `src`.

## Self-check before reporting
Run the deterministic validator against your own run directory and paste its
output verbatim:

    python3 <skill>/scripts/units.py validate-batches <batches_dir> <run_dir> \
      --units-dir <units_dir> --files <file1> <file2>

It checks identity, non-empty output, marker syntax/nesting, stray marker
glyphs, atoms, and link anchors. Your files are promoted with `complete`, which
re-runs this check —
a batch that fails is not merged, so fix it here rather than reporting around it.
Do not claim “no English prose” from a regex; flag retained English for human
review when it is not code, an identifier, a name, a quotation, or a title.

## Report
List the files handled, unit and word counts, and the self-check output. Flag any
unit you could not translate cleanly rather than leaving it silently blank, and
list any term you had to decide yourself so the glossary can absorb it.
```

---

## Tuning notes

**Chapter coherence beats even load.** A run that spans a whole chapter keeps its
terminology self-consistent; a perfectly balanced split across chapters does not.
Balance to roughly 15–20k words per run (`claim --count 8 --words 20000`).

**Never point two workers at the same file.** Claims exist because "the subtask
reported success but wrote nothing" is the most common failure — take the list
from `claim`, and release a dead worker's claims with `--release` before
re-dispatching.

**Ask for the failures.** The final line matters: without it, unparseable units
get skipped silently and surface only at verification.

**Don't merge batches to save dispatches.** A run that truncates a long batch
loses units the self-check will catch — costing more than the saved round trip.
