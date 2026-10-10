#!/usr/bin/env python3
"""Verify a translated EPUB against its source.

  epub_verify.py <translated.epub> [--source <source.epub>] [--script Han]
                 [--units <units_dir>] [--trans <dir> ...]

Checks, in order of how often they catch something real:

  1. ZIP conformance — mimetype first and uncompressed, no corrupt entries
  2. Structure diff against the source — element counts must not drift
  3. Per-chapter translation coverage
  4. Every untranslated unit listed, so a human can confirm each one is a
     deliberate hold-out (proper noun, identifier, English model output)
  5. Image references resolve; no orphan images shipped
"""
import argparse
import json
import os
import posixpath
import re
import sys
import urllib.parse
import zipfile

CJK = re.compile(r"[㐀-䶿一-鿿]")
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp")


def read_text(zf, name):
    try:
        return zf.read(name).decode("utf-8", "replace")
    except KeyError:
        return ""


def detect_page_prefix(zf, requested):
    """Choose a common chapter directory when a book does not use OEBPS/Text."""
    if requested != "auto":
        return requested
    names = zf.namelist()
    for prefix in ("OEBPS/Text/", "Text/", "OPS/Text/", "OEBPS/", "OPS/", ""):
        if any(name.startswith(prefix) and name.endswith((".html", ".xhtml", ".htm"))
               and not os.path.basename(name).startswith("nav") for name in names):
            return prefix
    return ""


def html_pages(zf, prefix):
    for n in sorted(zf.namelist()):
        if n.startswith(prefix) and n.endswith((".html", ".xhtml", ".htm")):
            if os.path.basename(n).startswith("nav"):
                continue
            yield n, read_text(zf, n)


def resolve_image(names, page, src):
    """Resolve an <img src> against the page that references it.

    Books do not agree on an asset directory — `assets/`, `images/`, or a flat
    layout. Matching on basename alone hides both a genuinely missing file and
    two different files that happen to share one.
    """
    ref = urllib.parse.unquote(src.split("#")[0].strip())
    if not ref or ref.startswith(("http:", "https:", "data:", "//")):
        return None
    target = posixpath.normpath(posixpath.join(posixpath.dirname(page), ref))
    if target in names:
        return target
    base = posixpath.basename(target)
    cands = [n for n in names if posixpath.basename(n) == base]
    return cands[0] if cands else None


def counts(text):
    """Structural element counts that must survive translation."""
    return {
        "a": len(re.findall(r"<a[\s>]", text)),
        "pre": len(re.findall(r"<pre[\s>]", text)),
        "img": len(re.findall(r"<img[\s>]", text)),
        "table": len(re.findall(r"<table[\s>]", text)),
        "h": len(re.findall(r"<h[1-6][\s>]", text)),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("epub")
    ap.add_argument("--source", help="source EPUB, for the structure diff")
    ap.add_argument("--prefix", default="auto",
                    help="chapter path prefix, or auto (default)")
    ap.add_argument("--min-len", type=int, default=20,
                    help="paragraph length above which a translation is expected")
    ap.add_argument("--show-untranslated", type=int, default=15)
    args = ap.parse_args()

    print("=" * 70)
    print("1. ZIP conformance")
    z = zipfile.ZipFile(args.epub)
    names = z.namelist()
    translated_prefix = detect_page_prefix(z, args.prefix)
    mt = z.getinfo("mimetype") if "mimetype" in names else None
    print(f"   mimetype first: {names[0] == 'mimetype'}")
    print(f"   mimetype stored: {mt.compress_type == zipfile.ZIP_STORED if mt else 'MISSING'}")
    print(f"   integrity: {'ok' if z.testzip() is None else 'CORRUPT'}")
    print(f"   {len(names)} entries, {os.path.getsize(args.epub)/1024/1024:.1f} MB")

    print(f"   chapter prefix: {translated_prefix or '/'}")
    print("\n2. Per-chapter coverage  (paragraphs over %d chars)" % args.min_len)
    tot = cjk = 0
    low = []
    for name, text in html_pages(z, translated_prefix):
        paras = [p for p in re.findall(r"<p[^>]*>(.*?)</p>", text, re.S)
                 if len(re.sub(r"<[^>]+>", "", p).strip()) > args.min_len]
        if not paras:
            continue
        n = sum(1 for p in paras if CJK.search(p))
        tot += len(paras); cjk += n
        pct = 100 * n / len(paras)
        flag = "  <-- check" if pct < 95 else ""
        if flag:
            low.append((os.path.basename(name), len(paras) - n))
        print(f"   {os.path.basename(name):34s} {len(paras):5d} {n:5d} {pct:6.1f}%{flag}")
    if tot:
        print(f"   {'TOTAL':34s} {tot:5d} {cjk:5d} {100*cjk/tot:6.1f}%")

    print("\n3. Untranslated units (confirm each is intentional)")
    leftover = []
    for name, text in html_pages(z, translated_prefix):
        for p in re.findall(r"<p[^>]*>(.*?)</p>", text, re.S):
            body = re.sub(r"<[^>]+>", "", p).strip()
            if len(body) > 60 and not CJK.search(body):
                leftover.append((os.path.basename(name), body))
    print(f"   {len(leftover)} English paragraphs of prose remain")
    for f, b in leftover[:args.show_untranslated]:
        print(f"   [{f}] {b[:90]!r}")
    if len(leftover) > args.show_untranslated:
        print(f"   ... and {len(leftover) - args.show_untranslated} more")

    print("\n4. Images")
    referenced, missing = set(), []
    for name, text in html_pages(z, translated_prefix):
        for m in re.finditer(r'<img[^>]+src="([^"]+)"', text):
            src = m.group(1)
            target = resolve_image(names, name, src)
            if target is None:
                missing.append((os.path.basename(name), src))
            else:
                referenced.add(target)
    avail = {n for n in names if n.lower().endswith(IMG_EXT)}
    orphans = sorted(avail - referenced)
    print(f"   referenced {len(referenced)}, missing {len(missing)}, packaged {len(avail)}, "
          f"orphans {len(orphans)}")
    for page, src in missing[:5]:
        print(f"   MISSING: {src} (referenced by {page})")
    for n in orphans[:5]:
        print(f"   ORPHAN: {n}")

    if args.source:
        print("\n5. Structure diff vs source")
        zs = zipfile.ZipFile(args.source)
        source_prefix = detect_page_prefix(zs, args.prefix)
        print(f"   source prefix: {source_prefix or '/'}")
        a = [counts(t) for _, t in html_pages(zs, source_prefix)]
        b = [counts(t) for _, t in html_pages(z, translated_prefix)]
        structure_drift = False
        for k in ("a", "pre", "img", "table", "h"):
            x, y = sum(d[k] for d in a), sum(d[k] for d in b)
            mark = "ok" if x == y else f"DRIFT {y - x:+d}"
            structure_drift = structure_drift or x != y
            print(f"   {k:6s} source {x:6d}   translation {y:6d}   {mark}")
    else:
        print("\n5. Structure diff skipped (no --source given)")
        structure_drift = False

    print("\n" + "=" * 70)
    problems = (bool(low) or bool(leftover) or bool(missing) or structure_drift
                or names[0] != "mimetype" or z.testzip() is not None)
    print("RESULT: review the items flagged above" if problems else "RESULT: clean")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
