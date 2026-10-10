# translate-book

[English](README.md) · [简体中文](README.zh-CN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![Agent Skill](https://img.shields.io/badge/Claude%20Code-Skill-8a63d2)](https://code.claude.com/docs/en/skills)

Translate an entire **EPUB or PDF technical book** into another language — and get
back a valid EPUB that still has its chapters, code blocks, links, figures, tables
and code callouts exactly where the author put them.

The point is not the translation. An LLM can translate a paragraph. The point is
that a book has thousands of structural dependencies, and every naive pipeline
breaks them silently: code gets "translated", anchors die, figures land in the
wrong chapter, and you only find out after 40,000 words of work.

## The governing rule

> **Never regenerate a book from plain text.** Parse it into translatable units,
> replace only the text, write it back into the original structure, then package.

Everything in this repo exists to make that rule enforceable.

## How it works

Each chapter is parsed into **units** — one block-level element whose content is
text plus inline markup: a paragraph, heading, list item, table cell, caption.
Inline markup inside a unit is carried two ways, and conflating them is the single
most common defect:

| | Written as | A translator may | Example |
|---|---|---|---|
| **Token** | `⟧n⟦…⟧/n⟦` | translate the inner text, keep both markers | a link, `<em>`, `<strong>` |
| **Atom** | `⟨n⟩` | reposition the whole marker, never alter or duplicate it | `<code>json.loads</code>`, inline image |

So prose wrapped around inline code gets translated, the code does not, and the
link keeps pointing where it pointed. Before writing anything back, every unit is
validated: no invented tokens, open/close pairs must match, atoms must be an
identical multiset. A unit that fails validation **keeps its source text** — a
missing translation shows up in verification, corrupted markup does not.

## Pipeline

| Step | Command | What it does |
|---|---|---|
| 1. Extract | `units.py extract` | one JSON per chapter of units |
| 2. Self-test | `units.py write --identity --strict` | write the source text back, diff — catches every index-alignment bug in minutes |
| 3. Batch | `units.py batch` | ~1600-word chunks, plus a `_plan.json` hashed over every planned unit |
| 4. Glossary | `units.py glossary` | seed `glossary.json` so parallel batches share one terminology |
| 5. Dispatch | `units.py claim` | hand a worker an unclaimed slice; it writes into its own run directory |
| 6. Promote | `units.py complete` | validate the run directory, then atomically copy only the files that pass |
| 7. Merge | `units.py merge` | fold finished batches into per-chapter files; refuses an incomplete plan |
| 8. Write back | `units.py write` | re-inject translations into the structure, validate per unit |
| 9. Package | `epub_build.py` | stage a copy, overlay, zip a conformant EPUB |
| 10. Verify | `epub_verify.py` | structure diff, coverage, untranslated-unit report |
| 11. Sample | `units.py sample` | fixed-size review sample per chapter, flags empty/untranslated units |

Steps 8–11 are free to re-run. Re-running step 1 invalidates existing translations,
which is why `units.py rekey` exists — it re-matches by source text and typically
recovers 95%+ of prior work. `units.py status` is the single source of truth for
what is done, missing, invalid, claimed, or stale.

## Install

### As a Claude Code plugin (recommended)

```bash
/plugin marketplace add Abirdcfly/translate-book-skill
/plugin install translate-book@translate-book-skill
```

### Manually, as a personal skill

Copy the **skill directory**, not the repo root — `SKILL.md` has to sit at the top
of what you install:

```bash
git clone https://github.com/Abirdcfly/translate-book-skill.git
cp -r translate-book-skill/skills/translate-book ~/.claude/skills/
```

Or symlink it, so `git pull` keeps your installed skill current:

```bash
ln -s "$PWD/translate-book-skill/skills/translate-book" ~/.claude/skills/translate-book
```

### Other agents

`SKILL.md` is plain procedure and [`AGENTS.md`](skills/translate-book/AGENTS.md) is a
plain prompt template — both are portable. The four Python scripts are ordinary
CLIs with no agent-specific dependency, so any agent or human that can run `bash`
can drive the pipeline.

## Requirements

- Python 3.8+
- [`lxml`](https://lxml.de/) — `pip install lxml`
- `pdftohtml` from **poppler-utils**, for the PDF path only:
  `brew install poppler` (macOS) / `apt-get install poppler-utils` (Debian, Ubuntu)

## Quick start — EPUB

```bash
mkdir -p work/src && unzip -q book.epub -d work/src

# SRC is the directory holding the book's XHTML files — usually OEBPS/Text/,
# but check your book: some use EPUB/text/, Text/, or the tree root.
SRC=work/src/OEBPS/Text

# 1. Extract one JSON of units per chapter
python3 scripts/units.py extract $SRC work/units

# 2. Self-test BEFORE translating: write source text back and diff (must exit 0)
python3 scripts/units.py write $SRC work/units work/trans work/out --identity --strict
diff -r $SRC work/out

# 3. Batch for subagents — _plan.json carries a hash of every planned unit
python3 scripts/units.py batch work/units work/batches

# 4. Seed the glossary, fill in the empty values, then dispatch. Each worker gets
#    its own run directory; nothing is written straight into work/done.
python3 scripts/units.py glossary work/units --out work/glossary.json
python3 scripts/units.py claim work/batches work/done --worker w1 --count 6

# 5. Promote a worker's output only after it validates
python3 scripts/units.py complete work/batches work/run/w1 work/done --units-dir work/units
python3 scripts/units.py status work/batches work/done --units-dir work/units

# 6. Fold finished batches into per-chapter translations
python3 scripts/units.py merge work/units work/done work/trans --batches-dir work/batches

# 7. Write translations back into the structure (per-unit validation)
python3 scripts/units.py write $SRC work/units work/trans work/out

# 8. Review a fixed sample per chapter before packaging
python3 scripts/units.py sample work/units work/trans --out work/sample.json

# 9. Package and verify
python3 scripts/epub_build.py book.zh.epub --from work/src --overlay work/out
python3 scripts/epub_verify.py book.zh.epub --source book.epub
```

Step 2 is the one that earns its keep. Expect the diff to show only serializer
normalizations — re-wrapped whitespace, `'` instead of `"` in the XML declaration,
a dropped DOCTYPE, no trailing newline. **Any difference in actual words is a bug
in your setup**, and you want to find it now rather than after translating.

`epub_verify.py` prints every unit that is still untranslated so you can confirm
each one is a deliberate hold-out — a proper noun, an identifier, an English model
output — rather than something that got silently dropped.

## Quick start — PDF

PDFs carry no structure, only positioned glyphs. You rebuild it, and **the layout
rules are per-book**, so survey before you code:

```bash
# 1. Learn the book's font roles: family x size counts, left-margin histograms
python3 scripts/pdf_layout.py survey book.pdf --probe 40 --out-config layout.json

# 2. Edit layout.json: map the fonts you just saw onto roles
#    (body / heading / code / callout / caption / table / furniture)
#    and add a "chapters" list.

# 3. Rebuild typed blocks per chapter, plus figures
python3 scripts/pdf_layout.py extract book.pdf work/pdf \
    --config layout.json --xml work/book.xml --images work/images

# 4. Render the blocks into OEBPS/Text/*.xhtml, then scaffold, fill and package
python3 scripts/epub_build.py book.epub --init work/tree --title "T" --author "A"
python3 scripts/epub_build.py book.epub --stage work/tree --finalize

# 5. Continue with the EPUB pipeline above, with SRC=work/tree/OEBPS/Text
python3 scripts/units.py extract work/tree/OEBPS/Text work/units
```

Then run the EPUB pipeline against the tree you just built.
[`REFERENCE.md`](skills/translate-book/REFERENCE.md) has the extraction order that
works (split by role before building lines, merge paragraphs across page breaks,
attach margin callouts by proximity) and the failure catalogue of what happens when
you skip a step.

## What it will not do

Stated plainly, because a translation that quietly omits things is worse than one
that says what is missing:

- **Text baked into figures stays untranslated.** It would need the figure redrawn.
- **Inline formulas often have no text layer.** The unit keeps its visible gap
  rather than inventing content to fill it.
- **Reconstructed PDF tables are approximate.** Multi-row headers and merged cells
  will misalign; the script says so instead of pretending otherwise.
- **Code is never translated**, including code comments inside `<pre>` blocks.
- Published books contain production defects — double-encoded entities, a stray
  styled character mid-word. The pipeline translates the intent and records the
  exception; it neither reproduces the defect nor drops it silently.

## Repository layout

```
.
├── .claude-plugin/           # plugin + marketplace manifests
├── .github/workflows/ci.yml  # byte-compiles the scripts, checks SKILL.md
├── skills/
│   └── translate-book/
│       ├── SKILL.md          # the procedure (loaded when the skill is invoked)
│       ├── REFERENCE.md      # unit model, failure catalogue, PDF recovery
│       ├── AGENTS.md         # subagent prompt template, fill in the <slots>
│       └── scripts/
│           ├── units.py          # extract / write / batch / glossary / claim
│           │                     # / validate-batches / complete / status
│           │                     # / merge / sample / rekey
│           ├── pdf_layout.py     # PDF → structured blocks (coordinates + fonts)
│           ├── epub_build.py     # stage + package a valid EPUB
│           └── epub_verify.py    # integrity, coverage, structure diff
├── LICENSE
└── README.md
```

## Scope

One book, not one article. For a single web page or article, use a lighter
translation skill — this one is built for volume, resumability and structure, and
its overhead only pays off across a whole manuscript.

## Notes for maintainers

- `.claude-plugin/plugin.json` pins `version`. Plugin users only receive updates
  when that string changes, so bump it with every release.
- `skills/translate-book/SKILL.md` needs YAML frontmatter on line 1 — the manifest
  is read only then, and the skill's directory name determines its command name.
- CI byte-compiles the scripts and checks that every `scripts/*.py` mentioned in
  `SKILL.md` actually exists.

## License

[MIT](LICENSE) © 2026 Abirdcfly
