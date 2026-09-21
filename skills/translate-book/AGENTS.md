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
- Output: <done_dir>   (create if missing)

## Your batches (<N> files)
<file list, one per line>

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

## JSON safety — read this twice
Never emit a raw ASCII double quote `"` inside a translation. Use typographic
quotes “ ”. A single unescaped `"` makes the file unparseable and the batch is lost.

Modify **only** the `out` field. Never touch `key` or `src`.

## Self-check before reporting
Run this and confirm every line prints OK:

    python3 -c "
    import json
    for f in ['<file1>', '<file2>']:
        a = json.load(open('<batches_dir>/' + f))['units']
        b = json.load(open('<done_dir>/' + f))['units']
        same  = len(a) == len(b) and all(
            x['key'] == y['key'] and x['src'] == y['src'] for x, y in zip(a, b))
        empty = [y for y in b if not y.get('out', '').strip()]
        print(f, len(b), 'OK' if same and not empty else
              f'MISMATCH={not same} EMPTY={len(empty)}')
    "

Also confirm, per unit: atom count matches the source, and no English prose is
left in `out`.

## Report
List the files handled, unit and word counts, and the self-check output. Flag any
unit you could not translate cleanly rather than leaving it silently blank.
```

---

## Tuning notes

**Chapter coherence beats even load.** A run that spans a whole chapter keeps its
terminology self-consistent; a perfectly balanced split across chapters does not.
Balance to roughly 15–20k words per run.

**Ask for the failures.** The final line matters: without it, unparseable units
get skipped silently and surface only at verification.

**Don't merge batches to save dispatches.** A run that truncates a long batch
loses units the self-check will catch — costing more than the saved round trip.
