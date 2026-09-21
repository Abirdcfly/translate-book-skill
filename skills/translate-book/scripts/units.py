#!/usr/bin/env python3
"""Structure-preserving book translation: extract, write back, batch, merge, reuse.

The unit model
--------------
A *unit* is a block-level element whose content is text plus inline markup:
a paragraph, heading, list item, table cell, caption.

Inline content is carried two ways, and conflating them is the most common defect:

  token  ⟧n⟦ ... ⟧/n⟦   link / emphasis. Inner text IS translated.
  atom   ⟨n⟩            <code>, <img>. Element preserved verbatim, never translated.

Extract and write must share one traversal (`iter_units`) — if they enumerate
elements by separate logic, any filter difference silently shifts every index.

Usage
-----
  units.py extract <src_dir> <units_dir>
  units.py write   <src_dir> <units_dir> <trans_dir> <out_dir> [--identity]
                   [--exceptions FILE]
  units.py batch   <units_dir> <batches_dir> [--words N]
  units.py merge   <units_dir> <done_dir> <trans_dir>
  units.py rekey   <batches_dir> <old_done_dir> <new_done_dir>

trans_dir/<name>.json holds {"file":..., "segs":[{i, raw, tags, atoms, out}]}.
batches_dir/<name>-bNN.json holds {"file":..., "units":[{key, src}]} where
key is "<i>.text" | "<i>.callouts.<j>" | "<i>.rows.<r>.<c>".
"""
import argparse
import json
import os
import re
import shutil
import sys

from lxml import etree
from lxml import html as LH

# Inline elements kept AND translated.
KEEP_INLINE = {"a", "em", "strong", "sup", "sub", "b", "i", "small", "mark"}
# Inline elements preserved whole, content verbatim.
#   code-like spans must be atoms, NOT "skip subtree" — skipping drops the
#   entire surrounding paragraph, not just the code.
ATOM_INLINE = {"code", "kbd", "samp", "var", "tt", "img"}
# Subtree entirely excluded from translation units.
SKIP_SUBTREE = {"pre", "script", "style"}
INLINE_SET = (KEEP_INLINE | ATOM_INLINE |
              {"span", "br", "abbr", "cite", "q", "u", "s", "font", "big"})

OPEN_RE = re.compile(r"⟧(\d+)⟦")
CLOSE_RE = re.compile(r"⟧/(\d+)⟦")
ATOM_RE = re.compile(r"⟨(\d+)⟩")
TAGNAME_RE = re.compile(r"<([A-Za-z][\w:.-]*)")
TOKEN_SPLIT = re.compile(r"(⟧/?\d+⟦|⟨\d+⟩)")

# Kept only where a citation would otherwise be re-derived wrongly.
CITATION_RE = re.compile(r"^\[[\d,\s–-]+\]$")


def localname(el):
    return LH.etree.QName(el).localname if isinstance(el.tag, str) else None


def norm_space(s):
    """Collapse whitespace and drop soft hyphens (no use in CJK output)."""
    s = s.replace("­", "").replace(" ", " ")
    return re.sub(r"[ \t\r\n]+", " ", s)


# ---------------------------------------------------------------- extraction

def seg_from_element(el):
    """Serialize an element's content to a template plus its tag/atom tables.

    Returns (text, tags, atoms) or None when the element contains a skip-subtree.
    """
    tags, atoms, parts = [], [], []

    def walk(node):
        if node.text:
            parts.append(norm_space(node.text))
        for ch in node:
            name = localname(ch)
            if name is None:                      # comment / PI
                pass
            elif name in SKIP_SUBTREE:
                return False
            elif name in ATOM_INLINE:
                atoms.append(LH.tostring(ch, encoding="unicode", with_tail=False))
                parts.append(f"⟨{len(atoms) - 1}⟩")
            elif name in KEEP_INLINE:
                raw = LH.tostring(ch, encoding="unicode", with_tail=False)
                tags.append(raw[:raw.index(">") + 1])
                # Capture the index NOW. Reading len(tags) after the recursive
                # walk yields the wrong number whenever the child adds tags of
                # its own, producing an unpaired close marker.
                idx = len(tags) - 1
                parts.append(f"⟧{idx}⟦")
                if not walk(ch):
                    return False
                parts.append(f"⟧/{idx}⟦")
            elif name == "br":
                parts.append(" ")
            else:                                 # styling wrapper: keep text, drop tag
                if not walk(ch):
                    return False
            if ch.tail:
                parts.append(norm_space(ch.tail))
        return True

    if not walk(el):
        return None
    return re.sub(r"[ \t]+", " ", "".join(parts)).strip(), tags, atoms


def is_leaf_block(el):
    if el.text is None and len(el) == 0:
        return False
    return all(localname(ch) in INLINE_SET for ch in el)


def iter_units(root):
    """Yield (i, element, seg) for every translatable unit, in document order.

    Shared by extract and write. Do not reimplement the filtering elsewhere.
    """
    i = 0
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        name = localname(el)
        if name in SKIP_SUBTREE or name in INLINE_SET:
            continue
        if not is_leaf_block(el):
            continue
        r = seg_from_element(el)
        if not r:
            continue
        text, tags, atoms = r
        if not re.search(r"[A-Za-z]{2}", text):
            continue          # nothing to translate (pure numbers, symbols, CJK)
        yield i, el, {"i": i, "raw": text, "tags": tags, "atoms": atoms}
        i += 1


def parse_file(path):
    tree = LH.parse(path)
    return tree, tree.getroot()


def cmd_extract(args):
    os.makedirs(args.units_dir, exist_ok=True)
    manifest, total_w, total_s = {}, 0, 0
    for fn in sorted(os.listdir(args.src_dir)):
        if not fn.endswith((".html", ".xhtml")):
            continue
        _, root = parse_file(os.path.join(args.src_dir, fn))
        segs = [seg for _, _, seg in iter_units(root)]
        if not segs:
            continue
        words = sum(len(s["raw"].split()) for s in segs)
        manifest[fn] = {"segs": len(segs), "words": words}
        total_w += words; total_s += len(segs)
        with open(os.path.join(args.units_dir, fn.rsplit(".", 1)[0] + ".json"),
                  "w", encoding="utf-8") as f:
            json.dump({"file": fn, "segs": segs}, f, ensure_ascii=False, indent=1)
        print(f"  {fn:44s} segs={len(segs):5d}  words={words:7d}")
    with open(os.path.join(args.units_dir, "_manifest.json"), "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"\n{len(manifest)} files  {total_s} units  {total_w} words -> {args.units_dir}")


# ---------------------------------------------------------------- write-back

def render_fragment(template, tags, atoms=None):
    """Template -> real HTML. Literal text is escaped.

    Escaping matters: source text may contain <think> as escaped text
    (&lt;think&gt;); leaving it raw lets the parser read it as a tag and drop it.
    """
    out = []
    for part in TOKEN_SPLIT.split(template):
        if not part:
            continue
        m = ATOM_RE.fullmatch(part)
        if m:
            k = int(m.group(1))
            out.append(atoms[k] if atoms and k < len(atoms) else part)
            continue
        m = CLOSE_RE.fullmatch(part)
        if m:
            k = int(m.group(1))
            if k < len(tags):
                nm = TAGNAME_RE.match(tags[k])
                out.append(f"</{nm.group(1)}>" if nm else "")
            continue
        m = OPEN_RE.fullmatch(part)
        if m:
            k = int(m.group(1))
            out.append(tags[k] if k < len(tags) else "")
            continue
        out.append(part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(out)


def validate(template, translated, n_atoms=0):
    """Return None if the unit is safe to write, else a reason string.

    Fewer tokens than the source is allowed: a hyphenated English word split
    across two emphasis spans merges into one word in most target languages.
    """
    wo, wc = sorted(OPEN_RE.findall(template)), sorted(CLOSE_RE.findall(template))
    go, gc = sorted(OPEN_RE.findall(translated)), sorted(CLOSE_RE.findall(translated))
    extra = [t for t in go if t not in wo] + [t for t in gc if t not in wc]
    if extra:
        return f"introduced tokens not in source: {extra}"
    used = set(go) | set(gc)
    unpaired = [t for t in used if (t in go) != (t in gc)]
    if unpaired:
        return f"unpaired tokens: {unpaired}"
    if n_atoms:
        wa, ga = sorted(ATOM_RE.findall(template)), sorted(ATOM_RE.findall(translated))
        if wa != ga:
            return f"atom mismatch: source={wa} translation={ga}"
    return None


def load_atom_exceptions(path):
    if not path or not os.path.exists(path):
        return set()
    return {(d["file"], int(d["seg"])) for d in json.load(open(path, encoding="utf-8"))}


def write_file(src_path, dst_path, trans, exceptions=frozenset()):
    """Write one file, replacing unit content. Returns list of (i, reason) failures."""
    tree, root = parse_file(src_path)
    fname = os.path.basename(src_path)
    failures = []
    # Materialize: mutating the tree mid-iteration makes lxml skip elements.
    for i, el, _seg in list(iter_units(root)):
        if i not in trans:
            continue
        t = trans[i]
        out = t.get("out")
        if not out or not out.strip():
            continue
        n_atoms = 0 if (fname, i) in exceptions else len(t.get("atoms") or [])
        reason = validate(t["raw"], out, n_atoms)
        if reason:
            failures.append((i, reason))
            continue                                 # keep source text
        frag = render_fragment(out, t.get("tags") or [], t.get("atoms") or [])
        wrapper = LH.fragment_fromstring(f"<div>{frag}</div>")
        el.text = wrapper.text
        for ch in list(el):
            el.remove(ch)
        for ch in wrapper:
            el.append(ch)
    with open(dst_path, "wb") as f:
        f.write(etree.tostring(root, encoding="utf-8", method="xml",
                               xml_declaration=True))
    return failures


def load_trans_file(path):
    if not os.path.exists(path):
        return {}
    d = json.load(open(path, encoding="utf-8"))
    return {s["i"]: s for s in d.get("segs", [])}


def cmd_write(args):
    os.makedirs(args.out_dir, exist_ok=True)
    exceptions = load_atom_exceptions(args.exceptions)
    all_fail, n = [], 0
    for fn in sorted(os.listdir(args.src_dir)):
        if not fn.endswith((".html", ".xhtml")):
            continue
        stem = fn.rsplit(".", 1)[0]
        src = os.path.join(args.src_dir, fn)
        dst = os.path.join(args.out_dir, fn)
        uf = os.path.join(args.units_dir, stem + ".json")
        if not os.path.exists(uf):
            shutil.copy(src, dst); n += 1
            continue
        units = json.load(open(uf, encoding="utf-8"))["segs"]
        if args.identity:
            trans = {s["i"]: {**s, "out": s["raw"]} for s in units}
        else:
            tf = load_trans_file(os.path.join(args.trans_dir, stem + ".json"))
            trans = {s["i"]: {**s, "out": (tf.get(s["i"], {}).get("out") or s["raw"])}
                     for s in units}
        fails = write_file(src, dst, trans, exceptions)
        all_fail += [(fn, i, r) for i, r in fails]
        n += 1
    print(f"wrote {n} files -> {args.out_dir}")
    if all_fail:
        print(f"REJECTED {len(all_fail)} units (source text kept):")
        for fn, i, r in all_fail[:20]:
            print(f"   {fn} seg {i}: {r}")
    else:
        print("all units passed validation")


# ---------------------------------------------------------------- batching

def units_of_chapter(data):
    """Flatten a chapter's units into (key, text) pairs."""
    out = []
    for i, b in enumerate(data.get("segments", data.get("blocks", []))):
        t = b.get("type", "text")
        if t == "figure":
            continue
        if t == "code":
            for j, c in enumerate(b.get("callouts") or []):
                out.append((f"{i}.callouts.{j}", c))
            continue
        if t == "table":
            for r, row in enumerate(b.get("rows") or []):
                for c, cell in enumerate(row):
                    if cell and re.search(r"[A-Za-z]{2}", cell):
                        out.append((f"{i}.rows.{r}.{c}", cell))
            continue
        txt = b.get("text", "")
        if txt and re.search(r"[A-Za-z]{2}", txt):
            out.append((f"{i}.text", txt))
    return out


def cmd_batch(args):
    os.makedirs(args.batches_dir, exist_ok=True)
    plan = []
    for fn in sorted(os.listdir(args.units_dir)):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        d = json.load(open(os.path.join(args.units_dir, fn), encoding="utf-8"))
        pairs = [(f"{s['i']}.text", s["raw"]) for s in d["segs"]]
        if not pairs:
            continue
        stem = fn.rsplit(".", 1)[0]
        cur, cw, bi = [], 0, 0
        for key, src in pairs:
            w = len(src.split())
            if cur and cw + w > args.words:
                bi = _flush(cur, cw, bi, stem, d, args.batches_dir, plan)
                cur, cw = [], 0
            cur.append({"key": key, "src": src}); cw += w
        bi = _flush(cur, cw, bi, stem, d, args.batches_dir, plan)
    json.dump(plan, open(os.path.join(args.batches_dir, "_plan.json"), "w",
                         encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{len(plan)} batches  {sum(p['units'] for p in plan)} units  "
          f"{sum(p['words'] for p in plan)} words -> {args.batches_dir}")


def _flush(cur, cw, bi, stem, d, out_dir, plan):
    if not cur:
        return bi
    name = f"{stem}-b{bi:02d}.json"
    json.dump({"file": d.get("file", stem), "batch": bi,
               "units": cur},
              open(os.path.join(out_dir, name), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    plan.append({"file": name, "batch": bi, "units": len(cur), "words": cw})
    return bi + 1


def cmd_merge(args):
    """Fold finished batch outputs into per-chapter translation files."""
    os.makedirs(args.trans_dir, exist_ok=True)
    groups = {}
    for f in sorted(os.listdir(args.done_dir)):
        if not f.endswith(".json"):
            continue
        stem = re.sub(r"-b\d+\.json$", "", f)
        groups.setdefault(stem, []).append(f)
    for stem, files in groups.items():
        uf = os.path.join(args.units_dir, stem + ".json")
        if not os.path.exists(uf):
            continue
        units = json.load(open(uf, encoding="utf-8"))
        by_key = {}
        for f in files:
            d = json.load(open(os.path.join(args.done_dir, f), encoding="utf-8"))
            for u in d.get("units", []):
                if (u.get("out") or "").strip():
                    by_key[u["key"]] = u["out"]
        segs = []
        for s in units["segs"]:
            out = by_key.get(f"{s['i']}.text", s["raw"])
            segs.append({**s, "out": out})
        json.dump({"file": units["file"], "segs": segs},
                  open(os.path.join(args.trans_dir, stem + ".json"), "w",
                       encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"merged {len(groups)} chapters -> {args.trans_dir}")


def cmd_rekey(args):
    """Reuse translations after an extraction change, matching on source text.

    Editing the extractor shifts every index but leaves most text intact, so
    matching by `src` recovers nearly all prior work. Typical match rate >95%.
    """
    old = {}
    for f in os.listdir(args.old_done_dir):
        if not f.endswith(".json"):
            continue
        d = json.load(open(os.path.join(args.old_done_dir, f), encoding="utf-8"))
        for u in d.get("units", []):
            if (u.get("out") or "").strip():
                old.setdefault(u["src"], u["out"])
    os.makedirs(args.new_done_dir, exist_ok=True)
    hit = miss = 0
    residue = []
    for f in sorted(os.listdir(args.batches_dir)):
        if not f.endswith(".json") or f.startswith("_"):
            continue
        d = json.load(open(os.path.join(args.batches_dir, f), encoding="utf-8"))
        matched = [{**u, "out": old[u["src"]]} for u in d["units"] if u["src"] in old]
        rest = [u for u in d["units"] if u["src"] not in old]
        hit += len(matched); miss += len(rest)
        if matched:
            json.dump({**d, "units": matched},
                      open(os.path.join(args.new_done_dir, f), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        if rest:
            residue.append((f, len(rest)))
    print(f"reused {hit} units; {miss} remain, across {len(residue)} batches")
    if residue:
        with open(os.path.join(args.new_done_dir, "_residue.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(residue, fh, ensure_ascii=False, indent=1)
        for f, n in residue[:15]:
            print(f"   {f:28s} missing {n}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("extract"); p.add_argument("src_dir"); p.add_argument("units_dir")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("write")
    p.add_argument("src_dir"); p.add_argument("units_dir")
    p.add_argument("trans_dir"); p.add_argument("out_dir")
    p.add_argument("--identity", action="store_true",
                   help="re-inject source text (self-test, needs no trans_dir)")
    p.add_argument("--exceptions", help="JSON list of {file, seg} allowed to differ")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("batch")
    p.add_argument("units_dir"); p.add_argument("batches_dir")
    p.add_argument("--words", type=int, default=1600)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("merge")
    p.add_argument("units_dir"); p.add_argument("done_dir"); p.add_argument("trans_dir")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("rekey")
    p.add_argument("batches_dir"); p.add_argument("old_done_dir"); p.add_argument("new_done_dir")
    p.set_defaults(func=cmd_rekey)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
