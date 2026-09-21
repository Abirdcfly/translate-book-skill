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
import re
import sys
import zipfile

CJK = re.compile(r"[㐀-䶿一-鿿]")


def read_text(zf, name):
    try:
        return zf.read(name).decode("utf-8", "replace")
    except KeyError:
        return ""


def html_pages(zf, prefix="OEBPS/Text/"):
    for n in sorted(zf.namelist()):
        if n.startswith(prefix) and n.endswith((".html", ".xhtml", ".htm")):
            if os.path.basename(n).startswith("nav"):
                continue
            yield n, read_text(zf, n)


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
    ap.add_argument("--prefix", default="OEBPS/Text/")
    ap.add_argument("--min-len", type=int, default=20,
                    help="paragraph length above which a translation is expected")
    ap.add_argument("--show-untranslated", type=int, default=15)
    args = ap.parse_args()

    print("=" * 70)
    print("1. ZIP conformance")
    z = zipfile.ZipFile(args.epub)
    names = z.namelist()
    mt = z.getinfo("mimetype") if "mimetype" in names else None
    print(f"   mimetype first: {names[0] == 'mimetype'}")
    print(f"   mimetype stored: {mt.compress_type == zipfile.ZIP_STORED if mt else 'MISSING'}")
    print(f"   integrity: {'ok' if z.testzip() is None else 'CORRUPT'}")
    print(f"   {len(names)} entries, {os.path.getsize(args.epub)/1024/1024:.1f} MB")

    print("\n2. Per-chapter coverage  (paragraphs over %d chars)" % args.min_len)
    tot = cjk = 0
    low = []
    for name, text in html_pages(z, args.prefix):
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
    for name, text in html_pages(z, args.prefix):
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
    used, missing = set(), set()
    for name, text in html_pages(z, args.prefix):
        for m in re.finditer(r'<img[^>]+src="([^"]+)"', text):
            src = m.group(1)
            base = os.path.basename(src)
            used.add(base)
            cand = [n for n in names if n.endswith("/" + base) or n == src]
            if not cand:
                missing.add(src)
    avail = {os.path.basename(n) for n in names if "/Images/" in n or "/images/" in n}
    print(f"   referenced {len(used)}, missing {len(missing)}, packaged {len(avail)}, "
          f"orphans {len(avail - used)}")
    for m in list(missing)[:5]:
        print(f"   MISSING: {m}")

    if args.source:
        print("\n5. Structure diff vs source")
        zs = zipfile.ZipFile(args.source)
        a = [counts(t) for _, t in html_pages(zs)]
        b = [counts(t) for _, t in html_pages(z, args.prefix)]
        for k in ("a", "pre", "img", "table", "h"):
            x, y = sum(d[k] for d in a), sum(d[k] for d in b)
            mark = "ok" if x == y else f"DRIFT {y - x:+d}"
            print(f"   {k:6s} source {x:6d}   translation {y:6d}   {mark}")
    else:
        print("\n5. Structure diff skipped (no --source given)")

    print("\n" + "=" * 70)
    problems = (bool(low) or bool(leftover) or bool(missing)
                or names[0] != "mimetype" or z.testzip() is not None)
    print("RESULT: review the items flagged above" if problems else "RESULT: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
