#!/usr/bin/env python3
"""Rebuild a structured document from a PDF's glyph coordinates and fonts.

PDFs carry no structure — only positioned glyphs. Recovery runs in two stages:
survey a sample to learn the book's font roles, then extract with that table.

Usage
-----
  # 1. Learn the layout. Prints font x size counts and left-margin histograms,
  #    plus a starter config you can edit.
  pdf_layout.py survey book.pdf --probe 40 --out-config layout.json

  # 2. Extract. Runs pdftohtml once for the whole book (per-page invocations
  #    cost ~2s each and dominate runtime), then writes one JSON per chapter.
  pdf_layout.py extract book.pdf out_dir --config layout.json [--xml cached.xml]

Config format — ordered rules, first match wins, default is "body":

  {
    "drop_above": 62, "drop_below": 900,
    "max_hgap": 55, "line_tol": 5, "para_gap": 26,
    "rules": [
      ["code",      "family", "contains", "Courier"],
      ["callout",   "family", "contains", "Swister"],
      ["caption",   "family", "contains", "Univers"],
      ["heading",   "size",   ">=",       24],
      ["table",     "size",   "==",       15],
      ["body",      "size",   "==",       17],
      ["body",      "family", "contains", "Minion"],
      ["furniture", "size",   "==",       14]
    ],
    "chapters": [{"title": "1 Intro", "start": 33, "end": 54}, ...]
  }

Without a "chapters" list, the PDF outline is used (top-level numeric entries).
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

# ------------------------------------------------------------------ parsing

PAGE_RE = re.compile(r'<page number="(\d+)"')
FONT_RE = re.compile(r'<fontspec id="(\d+)" size="([\d.]+)" family="([^"]+)"')
TEXT_RE = re.compile(r'<text top="(-?\d+)" left="(-?\d+)" width="(\d+)" '
                     r'height="(\d+)" font="(\d+)">(.*?)</text>')
IMG_RE = re.compile(r'<image top="(-?\d+)" left="(-?\d+)" width="(\d+)" '
                    r'height="(\d+)" src="([^"]+)"')


def decode_entities(s):
    """Numeric entities first, named second, &amp; LAST.

    Wrong order leaves `&amp;#34;` as a literal `&#34;` in the text.
    """
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"&#[xX]([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&apos;", "'"), ("&amp;", "&")):
        s = s.replace(a, b)
    return s


def run_pdftohtml(pdf, xml_cache=None, images_dir=None):
    """Extract the whole book to XML once. Returns the XML path."""
    if xml_cache and os.path.exists(xml_cache):
        return xml_cache
    out = xml_cache or (os.path.splitext(pdf)[0] + ".xml")
    cmd = ["pdftohtml", "-xml", "-f", "1", "-l", "99999"]
    if images_dir:
        os.makedirs(images_dir, exist_ok=True)
        cmd += ["-c"]                                  # also write images
        target = os.path.join(images_dir, "_doc")
    else:
        cmd += ["-i"]                                  # skip images
        target = os.path.splitext(out)[0] + "_tmp"
    cmd += [pdf, target]
    sys.stderr.write("running pdftohtml (whole book, this takes a minute)...\n")
    subprocess.run(cmd, check=True, capture_output=True)
    produced = target + ".xml"
    if produced != out:
        os.replace(produced, out)
    return out


def parse_xml(path):
    """Return (pages, fonts).

    `fonts` is GLOBAL — pdftohtml declares the table once near the top and every
    page references it. Resetting per page leaves every run unclassified.
    """
    pages, fonts = {}, {}
    cur, texts, images = None, [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = PAGE_RE.search(line)
            if m:
                if cur is not None:
                    pages[cur] = (texts, images)
                cur, texts, images = int(m.group(1)), [], []
                continue
            m = FONT_RE.search(line)
            if m:
                fonts[m.group(1)] = (float(m.group(2)), m.group(3).split("+")[-1])
                continue
            m = TEXT_RE.search(line)
            if m and cur is not None:
                txt = re.sub(r"<[^>]+>", "", decode_entities(m.group(6)))
                if txt.strip():
                    texts.append({"top": int(m.group(1)), "left": int(m.group(2)),
                                  "w": int(m.group(3)), "h": int(m.group(4)),
                                  "font": m.group(5), "text": txt})
                continue
            m = IMG_RE.search(line)
            if m and cur is not None:
                images.append({"top": int(m.group(1)), "left": int(m.group(2)),
                               "w": int(m.group(3)), "h": int(m.group(4)),
                               "src": m.group(5)})
    if cur is not None:
        pages[cur] = (texts, images)
    return pages, fonts


# ------------------------------------------------------------------ roles

OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
       "==": lambda a, b: a == b, ">": lambda a, b: a > b, "<": lambda a, b: a < b,
       "contains": lambda a, b: b in a, "startswith": lambda a, b: a.startswith(b)}


def role_of(rec, fonts, rules):
    size, family = fonts.get(rec["font"], (0.0, ""))
    for rule in rules:
        role, field, op, value = rule
        actual = family if field == "family" else size
        if field == "family" and isinstance(value, str):
            actual = family
        if OPS[op](actual, value):
            return role
    return "body"


# ------------------------------------------------------------------ layout

def make_lines(recs, tol, max_hgap):
    """Group runs into lines.

    Two guards matter. Vertical tolerance alone merges unrelated streams that
    share a baseline (code on the left, margin annotations on the right), so a
    horizontal-gap ceiling breaks the line whenever the streams are far apart.
    """
    recs = sorted(recs, key=lambda r: (r["top"], r["left"]))
    lines = []
    for r in recs:
        if lines:
            last = lines[-1]
            if (abs(r["top"] - last["top"]) <= tol
                    and r["left"] - last["right"] <= max_hgap):
                last["items"].append(r)
                last["right"] = max(last["right"], r["left"] + r["w"])
                continue
        lines.append({"top": r["top"], "items": [r], "right": r["left"] + r["w"]})
    for ln in lines:
        ln["items"].sort(key=lambda x: x["left"])
        parts, prev = [], None
        for it in ln["items"]:
            if prev is not None and it["left"] - prev > 6:
                parts.append(" ")
            parts.append(it["text"])
            prev = it["left"] + it["w"]
        ln["text"] = "".join(parts).strip()
        ln["left"] = min(i["left"] for i in ln["items"])
        ln["size"] = max(i["size"] for i in ln["items"])
    return lines


def cluster_by_left(recs, tol=40):
    """Split a role into x-columns. Margin annotations often sit side by side."""
    recs = sorted(recs, key=lambda r: r["left"])
    cols = []
    for r in recs:
        if cols and abs(r["left"] - cols[-1][0]["left"]) <= tol:
            cols[-1].append(r)
        else:
            cols.append([r])
    return cols


def make_blocks(lines, role, gap, check_left=True):
    blocks = []
    for ln in lines:
        if blocks:
            b = blocks[-1]
            if (ln["top"] - b["last_top"] <= gap
                    and (not check_left or abs(ln["left"] - b["left"]) <= 32)):
                b["lines"].append(ln); b["last_top"] = ln["top"]
                continue
        blocks.append({"role": role, "lines": [ln], "left": ln["left"],
                       "top": ln["top"], "last_top": ln["top"]})
    return blocks


def build_table(lines, tol):
    """Recover a grid. Columns are found by x-axis gaps: project every span onto
    the axis; uncovered runs are separators. Clustering left edges fails because
    cells centre or right-align.

    Multi-row headers and merged cells still misalign — treat as approximate.
    """
    rows = []
    for ln in lines:
        if rows and abs(ln["top"] - rows[-1]["top"]) <= tol:
            rows[-1]["items"].extend(ln["items"])
        else:
            rows.append({"top": ln["top"], "items": list(ln["items"])})
    for r in rows:
        r["items"].sort(key=lambda x: x["left"])
    spans = sorted((it["left"], it["left"] + it["w"]) for r in rows for it in r["items"])
    if not spans:
        return None
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1] + 8:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    cols = merged
    if len(cols) > 12:
        cols = [[spans[0][0], spans[-1][1]]]

    def colof(cx):
        return min(range(len(cols)),
                   key=lambda i: 0 if cols[i][0] <= cx <= cols[i][1]
                   else min(abs(cx - cols[i][0]), abs(cx - cols[i][1])))
    grid = []
    for r in rows:
        cells = [""] * len(cols)
        for it in r["items"]:
            c = colof(it["left"] + it["w"] / 2)
            cells[c] = (cells[c] + " " + it["text"]).strip()
        if any(cells):
            grid.append(cells)
    return grid


DEBRIS_TOKENS = re.compile(r"[\s|]+")


def is_figure_debris(text):
    """Vector-drawn figure labels extracted as prose.

    A tokenisation diagram's labels come out as `e r t h h e o n i c in g`.
    Detect and drop; the figure's raster already carries this content.
    """
    toks = [t for t in DEBRIS_TOKENS.split(text) if t]
    if len(toks) < 8 or any(c in text for c in "=(){}[];<>"):
        return False
    if max(len(t) for t in toks) > 6:
        return False
    return sum(1 for t in toks if len(t) <= 4) / len(toks) > 0.9


TERMINAL = re.compile(r'[.?!:;"’”)）\]】]\s*$')


def absorb(prev, b):
    prev["lines"].extend(b["lines"])
    prev["last_top"] = b["last_top"]
    if b.get("callouts"):
        prev["callouts"] = prev.get("callouts", []) + b["callouts"]


def page_blocks(pages, fonts, pno, cfg):
    texts, images = pages.get(pno, ([], []))
    rules = cfg["rules"]
    recs = []
    for t in texts:
        size, family = fonts.get(t["font"], (0.0, ""))
        if t["top"] < cfg["drop_above"] or t["top"] > cfg["drop_below"]:
            continue
        role = role_of(t, fonts, rules)
        if role == "furniture":
            continue
        recs.append({**t, "role": role, "size": size, "family": family})

    by = defaultdict(list)
    for r in recs:
        by[r["role"]].append(r)

    blocks = []
    for role in ("heading", "body", "caption"):
        if role not in by:
            continue
        lines = make_lines(by[role], cfg["line_tol"], cfg["max_hgap"])
        if role == "heading":
            for ln in lines:
                blocks.append({"role": "heading", "lines": [ln], "left": ln["left"],
                               "top": ln["top"], "last_top": ln["top"], "size": ln["size"]})
        else:
            blocks.extend(make_blocks(lines, role, cfg["para_gap"]))
    if "code" in by:
        blocks.extend(make_blocks(make_lines(by["code"], cfg["line_tol"], cfg["max_hgap"]),
                                  "code", gap=40, check_left=False))
    if "table" in by:
        blocks.extend(make_blocks(make_lines(by["table"], cfg["line_tol"], cfg["max_hgap"]),
                                  "table", gap=34, check_left=False))

    callouts = []
    if "callout" in by:
        for col in cluster_by_left(by["callout"]):
            for b in make_blocks(make_lines(col, cfg["line_tol"], cfg["max_hgap"]),
                                 "callout", gap=30, check_left=False):
                callouts.append({"top": b["top"], "last_top": b["last_top"],
                                 "left": b["left"],
                                 "text": " ".join(l["text"] for l in b["lines"])})

    figures = [{"role": "figure", "top": im["top"], "left": im["left"], "src": im["src"],
                "w": im["w"], "h": im["h"], "last_top": im["top"] + im["h"]}
               for im in images if im["w"] >= 40 and im["h"] >= 40]
    return blocks, callouts, figures


def attach_callouts(blocks, callouts):
    """Callouts sit in the margin with no leader line — proximity is the only
    signal. Attach each to the nearest code block, else to the previous block."""
    codes = [b for b in blocks if b["role"] == "code"]
    for c in callouts:
        if codes:
            target = min(codes, key=lambda b: min(
                abs(c["top"] - b["top"]), abs(c["top"] - b["last_top"]),
                0 if b["top"] - 60 <= c["top"] <= b["last_top"] + 60 else 10 ** 6))
        else:
            prev = [b for b in blocks if b["top"] < c["top"]]
            target = prev[-1] if prev else (blocks[0] if blocks else None)
        if target is not None:
            target.setdefault("callouts", []).append(c["text"])


# ------------------------------------------------------------------ assembly

def outline_chapters(pdf):
    try:
        import pypdf
    except ImportError:
        return []
    r = pypdf.PdfReader(pdf)
    flat = []

    def walk(o, depth=0):
        for it in o:
            if isinstance(it, list):
                walk(it, depth + 1)
            else:
                try:
                    pg = r.get_destination_page_number(it) + 1
                except Exception:
                    pg = None
                flat.append({"depth": depth, "title": it.title, "page": pg})
    try:
        walk(r.outline)
    except Exception:
        return []
    chaps = []
    for i, o in enumerate(flat):
        if o["depth"] == 0 and o["page"] and re.match(r"^\d+\s", o["title"] or ""):
            end = next((flat[j]["page"] - 1 for j in range(i + 1, len(flat))
                        if flat[j]["depth"] == 0 and flat[j]["page"]), None)
            chaps.append({"title": o["title"], "start": o["page"], "end": end})
    return chaps


def assemble(pages, fonts, cfg, out_dir, image_dir):
    chaps = cfg.get("chapters") or outline_chapters(cfg["_pdf"])
    if not chaps:
        sys.exit("no chapters: supply a \"chapters\" list in the config, or ensure "
                 "the PDF has a top-level numeric outline")
    os.makedirs(out_dir, exist_ok=True)
    index = []
    for ci, ch in enumerate(chaps, 1):
        end = ch.get("end") or max(pages)
        pending = []
        for pno in range(ch["start"], end + 1):
            blocks, callouts, figures = page_blocks(pages, fonts, pno, cfg)
            attach_callouts(blocks, callouts)
            for b in sorted(blocks + figures, key=lambda x: x["top"]):
                b["page"] = pno
                pending.append(b)

        merged = []
        for b in pending:
            prev = merged[-1] if merged else None
            if prev is not None and prev["role"] == b["role"]:
                nextpage = b["page"] == prev["page"] + 1 and b["top"] < 260
                if b["role"] == "code" and nextpage:
                    absorb(prev, b); continue
                # Without this, ~15% of paragraphs end mid-sentence at a page break
                if b["role"] == "body" and (nextpage or b["page"] == prev["page"]):
                    tail = prev["lines"][-1]["text"].rstrip()
                    if tail and not TERMINAL.search(tail):
                        absorb(prev, b); continue
            merged.append(b)

        out = []
        clean = re.sub(r"^\d+\s+", "", ch["title"] or "").strip()
        out.append({"type": "h1", "text": f"{ci} {clean}", "page": ch["start"]})
        for b in merged:
            probe = " ".join(l["text"] for l in b.get("lines", []))
            if is_figure_debris(probe):
                continue
            role = b["role"]
            if role == "heading":
                txt = b["lines"][0]["text"]
                tail = re.split(r":\s*", ch["title"], maxsplit=1)
                tail = re.sub(r"^\d+\s+", "", tail[-1]).strip()
                if b["page"] == ch["start"] and txt.strip().lower() == tail.lower():
                    continue                            # chapter title, already emitted
                out.append({"type": "h2" if b["size"] >= 24 else "h3",
                            "text": txt, "page": b["page"]})
            elif role == "body":
                out.append({"type": "para",
                            "text": " ".join(l["text"] for l in b["lines"]),
                            "page": b["page"]})
            elif role == "code":
                out.append({"type": "code", "lines": [l["text"] for l in b["lines"]],
                            "callouts": b.get("callouts", []), "page": b["page"]})
            elif role == "table":
                grid = build_table(b["lines"], cfg["line_tol"])
                if grid and len(grid) == 1 and len(grid[0]) == 1:
                    out.append({"type": "label", "text": grid[0][0], "page": b["page"]})
                elif grid:
                    out.append({"type": "table", "rows": grid, "page": b["page"]})
            elif role == "caption":
                out.append({"type": "caption",
                            "text": " ".join(l["text"] for l in b["lines"]),
                            "page": b["page"]})
            elif role == "figure":
                src = os.path.basename(b["src"])
                if image_dir:
                    dst = os.path.join(image_dir, src)
                    if os.path.exists(b["src"]) and not os.path.exists(dst):
                        os.replace(b["src"], dst)
                    src = os.path.basename(dst)
                out.append({"type": "figure", "src": src, "page": b["page"]})

        fn = f"chapter-{ci:02d}.json"
        json.dump({"chapter": ci, "title": ch["title"], "start": ch["start"],
                   "end": end, "blocks": out},
                  open(os.path.join(out_dir, fn), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        words = sum(len(b.get("text", "").split()) for b in out if b["type"] == "para")
        nfig = sum(1 for b in out if b["type"] == "figure")
        index.append({"file": fn, "chapter": ci, "title": ch["title"],
                      "pages": end - ch["start"] + 1, "blocks": len(out),
                      "words": words, "figures": nfig})
        print(f"  {fn}  {clean[:38]:40s} p{ch['start']}-{end}  "
              f"blocks={len(out):4d}  words={words:6d}  figures={nfig:3d}")
    json.dump(index, open(os.path.join(out_dir, "_index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n{len(index)} chapters, {sum(i['words'] for i in index)} body words")


# ------------------------------------------------------------------ survey

DEFAULT_RULES = [
    ["code", "family", "contains", "Courier"],
    ["callout", "family", "contains", "Swister"],
    ["caption", "family", "contains", "Univers"],
    ["heading", "size", ">=", 24],
    ["table", "size", "==", 15],
    ["body", "size", "==", 17],
    ["body", "family", "contains", "Minion"],
    ["furniture", "size", "==", 14],
]


def cmd_survey(args):
    xml = run_pdftohtml(args.pdf, args.xml)
    pages, fonts = parse_xml(xml)
    total = max(pages)
    step = max(1, total // max(args.probe, 1))
    fam_sz, lefts, tops = Counter(), defaultdict(Counter), Counter()
    for p in range(1, total + 1, step):
        for t in pages.get(p, ([], []))[0]:
            size, family = fonts.get(t["font"], (0.0, ""))
            fam_sz[(family, size)] += 1
            lefts[family][t["left"] // 10 * 10] += 1
            tops[t["top"] // 20 * 20] += 1
    print(f"\n{total} pages surveyed every {step}th\n")
    print("family x size:")
    for (fam, size), n in fam_sz.most_common(20):
        print(f"   {fam:32s} {size:5.0f}pt  {n:5d}")
    print("\nleft-margin histograms (body vs margin columns):")
    for fam in {f for f, _ in fam_sz}:
        print(f"   {fam:32s} {lefts[fam].most_common(5)}")
    print("\nvertical histogram (find header/footer bands):")
    print(f"   {tops.most_common(10)}")
    print("\nstarter config written to " + (args.out_config or "layout.json"))
    cfg = {"drop_above": 62, "drop_below": 900, "max_hgap": 55,
           "line_tol": 5, "para_gap": 26, "rules": DEFAULT_RULES}
    json.dump(cfg, open(args.out_config or "layout.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("Edit the rules to match the table above, add a \"chapters\" list, "
          "then run extract.")


def cmd_extract(args):
    cfg = json.load(open(args.config, encoding="utf-8"))
    cfg["_pdf"] = args.pdf
    xml = run_pdftohtml(args.pdf, args.xml, args.images)
    print("parsing XML...")
    pages, fonts = parse_xml(xml)
    print(f"  {len(pages)} pages, {len(fonts)} fonts")
    assemble(pages, fonts, cfg, args.out_dir, args.images)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("survey", help="learn the book's font roles")
    p.add_argument("pdf")
    p.add_argument("--probe", type=int, default=12, help="pages to sample")
    p.add_argument("--xml", help="cache the XML here")
    p.add_argument("--out-config", help="where to write the starter config")
    p.set_defaults(func=cmd_survey)

    p = sub.add_parser("extract", help="rebuild structured chapters")
    p.add_argument("pdf")
    p.add_argument("out_dir")
    p.add_argument("--config", required=True)
    p.add_argument("--xml", help="cache/reuse the XML here")
    p.add_argument("--images", help="directory for extracted figures")
    p.set_defaults(func=cmd_extract)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
