# Reference

## The unit model

A **unit** is one block-level element whose content is pure text plus inline
markup — a paragraph, heading, list item, table cell, figure caption.

```json
{"i": 42, "raw": "Use ⟧0⟦this link⟧/0⟦ with ⟨1⟩ to parse.",
 "tags": ["<a href=\"https://…\">"],
 "atoms": ["<code>json.loads</code>"]}
```

Two kinds of inline content, and conflating them is the single most common defect:

| | Written as | Translator may | Rationale |
|---|---|---|---|
| **Token** | `⟧n⟦…⟧/n⟦` | translate inner text, keep both markers | links and emphasis must survive, but their text is prose |
| **Atom** | `⟨n⟩` | reposition the whole marker, never alter or duplicate it | `<code>x</code>`, `<img>` — content is not prose |

Never let a token share the same delimiter family as an atom. `⟦⟧` and `⟨⟩` are
visually distinct so a token-bound regex cannot swallow an atom.

**Elements stripped with text kept**: `<span>` and other pure-styling containers.
Keep their text, drop the tag. Carrying them through multiplies tokens for no gain.

## Validation rules

Run per unit, before writing. Reject → keep source text → the cut shows up in verify.

1. No token in the translation that wasn't in the source (no invented tags).
2. Every token used must be open/close paired.
3. Atoms: identical multiset, in the translation as in the source.
4. Exception: allow **fewer** tokens than the source. Source text often splits an
   English word across two emphasis spans (`⟧0⟦checkin⟧/0⟦⟧1⟦g⟧/1⟦`); the merged
   Chinese word needs only one. Treating this as a failure rejects correct output.

Record genuine source defects in an exceptions file rather than loosening rules 1–3.

## Failure catalogue

Every one of these shipped silently at least once. Check for them explicitly.

### Extraction

| Symptom | Cause |
|---|---|
| Whole paragraphs present in source, absent in output | Inline `<code>` treated as "skip subtree" instead of an atom — drops the *entire* paragraph |
| Images vanish from body text | `<img>` inside `<p><strong><img/></strong></p>` treated as a styling wrapper |
| Garbled strings like `e r t h h e o n i c in g` | Vector-drawn figure text extracted as body prose. Detect: mostly 1–4 char fragments, no long tokens, no code symbols → drop (the figure's raster already carries it) |
| `&#34;` appears literally in text | Entity decode order wrong. Decode numeric first, named second, `&amp;` **last** |
| Margin annotations interleaved into one sentence | Line building grouped by vertical position only. Two columns share a baseline — cluster callouts by x-offset first |
| `high- fidelity`, `breadth- first` | Hyphenated line wrap joined with a space. Only worth fixing if the unit stays untranslated; Chinese drops the hyphen anyway |

### Write-back

| Symptom | Cause |
|---|---|
| Only the first paragraph of each chapter translated | Generator not materialized — tree mutated mid-iteration |
| Translations land on the wrong elements | Extractor and writer enumerate elements by separate logic; any filter difference (no-English, contains-`<pre>`) shifts every index |
| `<think>` and similar disappear | Escaped text re-parsed as a tag. Escape the literal-text portions of the rendered fragment |
| `<a>` count doubles | One tag table used for both open and close tags |

### Packaging

| Symptom | Cause |
|---|---|
| Second run produces garbage | Builder wrote into the source dir; next extract parsed translated output as English |
| Reader rejects the file | `mimetype` not first entry, or compressed |

### Translation agents

| Symptom | Cause |
|---|---|
| JSON parse error mid-file | Unescaped `"` in Chinese output. Forbid raw straight quotes; require `“ ”` |
| File missing units | Agent truncated a long batch. Assert unit count and non-empty `out` per file |

## Reusing translations

Editing the extractor invalidates every index but usually leaves most **text**
unchanged. Match old→new by `raw` text instead of re-translating:

1. Move the finished batch outputs aside (`translated_prev/`).
2. Re-extract, re-batch.
3. Build `{src_text: out}` from the old outputs; write matches into the new ones.
4. Only the residue goes to subagents.

Match rates above 95% are typical for a fix that touches one rule. Keep the
reuse output in a **separate directory** from fresh agent output and have the
builder read both — an agent overwriting a half-filled file loses the reused half.

## PDF layout recovery

PDFs carry no structure — only positioned glyphs. Rebuild it in this order.

### 1. Survey before coding

Pull `pdftohtml -xml` for a sample of pages and tabulate `family × size` counts
and `left`-margin histograms. Every book yields a different table. A typical
technical book:

```
body      MinionPro 17pt      left ≈ 82
list      MyriadPro 17pt      left ≈ 95, starts with •
heading   MyriadPro ≥24pt
subhead   MyriadPro 21pt
code      CourierNewPSMT
callout   Swister (margin annotations)
caption   Univers 11pt
table     MyriadPro 15pt      grid-aligned columns
header    MyriadPro 14pt      top < 60  → drop
footer    MyriadPro 14pt      top > 900 → drop
```

Extract the whole book to XML **once** (`pdftohtml -xml file.pdf out`) — per-page
invocations cost ~2s each and dominate runtime.

### 2. Parse notes

- `fontspec` is **global** — declared once near the top, referenced by every page.
  Resetting it per page leaves every text run unclassified.
- Pages are one `<page>` element each; `<text>` carries `top/left/width/font`.
- `<image>` carries `top/left/width/height/src`.

### 3. Assemble blocks — in this order

1. **Filter** headers, footers, and furniture by vertical position.
2. **Split by role first, then build lines within each role.** On one horizontal
   band you may have code on the left and margin annotations on the right; grouping
   by `top` alone interleaves two unrelated text streams into one string.
3. Also break a line when horizontal gap exceeds ~55 units — same reason.
4. **Merge across pages** when the previous block does not end in terminal
   punctuation. Without this, ~15% of paragraphs end mid-sentence at a page break.
5. **Attach callouts** to the nearest code block by vertical distance. The book
   prints them in the margin with no leader line; proximity is the only signal.

### 4. Tables

Split columns by **x-axis gaps**: project every cell's span onto the x axis; runs
with no coverage are column separators. Clustering left edges fails because cells
centre or right-align. Multi-row headers and merged cells will still misalign —
call it approximate and say so, rather than pretending it's exact.

### 5. Figures

`pdftohtml` without `-i` extracts raster images and their placement rectangles.
Compress before packaging — screenshots at print resolution run 90 MB+; a
1400 px cap at JPEG q82 typically lands near 40 MB, and drop images no block
references.

## Verification checklist

Compare against the **source file**, not against expectations.

- [ ] `mimetype` first, uncompressed; `testzip()` clean
- [ ] Counts match the source: `<a>`, `<pre>`, `<img>`, table count
- [ ] Per-chapter translated-unit coverage; list every untranslated unit and
      confirm each is a proper noun, an identifier, or an intentional English
      quotation (model output samples, code listings)
- [ ] Code blocks byte-identical to source lines
- [ ] No English prose paragraphs surviving in the body
- [ ] Every `<img src>` resolves to a packaged file; no orphan images
- [ ] Spot-read one page of rendered Chinese start to finish

Report limitations plainly: in-image text is untranslatable without redrawing,
inline formulas frequently have no text layer, and reconstructed tables are
approximate. These are properties of the source, not defects to hide.
