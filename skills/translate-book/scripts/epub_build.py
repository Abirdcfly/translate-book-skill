#!/usr/bin/env python3
"""Stage and package an EPUB correctly.

Two modes:

  # Repackage an existing book, overlaying translated files
  epub_build.py out.epub --from <source_epub_tree> --overlay <translated_dir>

  # Package an already-assembled tree (built from scratch, e.g. from a PDF)
  epub_build.py out.epub --stage <tree_dir>

  # Scaffold a minimal tree for a from-scratch build, then fill it in
  epub_build.py out.epub --init <tree_dir> --title T [--author A]

The staging copy exists for one reason: **never write into the source tree.**
A builder that overlays translations in place turns the next extract into
garbage, because it will parse the translated output as source text.
"""
import argparse
import html
import os
import re
import shutil
import sys
import zipfile


def zip_epub(tree, out_path):
    """mimetype must be the first entry and stored uncompressed."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(tree, "mimetype"), "mimetype",
                 compress_type=zipfile.ZIP_STORED)
        for root, _, files in os.walk(tree):
            for f in sorted(files):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, tree)
                if rel == "mimetype":
                    continue
                zf.write(full, rel)
    size = os.path.getsize(out_path) / 1024 / 1024
    with zipfile.ZipFile(out_path) as zf:
        bad = zf.testzip()
        names = zf.namelist()
    print(f"packaged {out_path}")
    print(f"  {size:.1f} MB   {len(names)} entries   "
          f"mimetype first: {names[0] == 'mimetype'}   "
          f"intact: {bad is None}")


def overlay(tree, src):
    """Replace same-named files in the tree with files from src (recursive)."""
    n = 0
    for root, _, files in os.walk(src):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, src)
            dst = os.path.join(tree, rel)
            candidates = [dst]
            # tolerate overlay dirs named like the tree's own subdirs
            for sub in ("Text", "OEBPS/Text", "Styles", "OEBPS/Styles",
                        "Images", "OEBPS/Images"):
                candidates.append(os.path.join(tree, sub, f))
            target = next((c for c in candidates if os.path.exists(c)), dst)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy(full, target)
            n += 1
    print(f"  overlaid {n} files")
    return n


CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def init_tree(tree, title, author, lang):
    """Scaffold a minimal EPUB tree for a from-scratch build."""
    for sub in ("META-INF", "OEBPS/Text", "OEBPS/Styles", "OEBPS/Images"):
        os.makedirs(os.path.join(tree, sub), exist_ok=True)
    open(os.path.join(tree, "mimetype"), "w").write("application/epub+zip")
    open(os.path.join(tree, "META-INF", "container.xml"), "w", encoding="utf-8").write(CONTAINER)
    print(f"initialised {tree}")
    print("  add Text/*.xhtml, Styles/*.css, Images/*,")
    print("  then run with --finalize to write content.opf and package")


def finalize(tree, title, author, lang):
    """Write content.opf and a nav page from whatever is in OEBPS/.

    Also creates the two files EPUB conformance requires, so a tree assembled
    from scratch needs nothing but its chapters.
    """
    oebps = os.path.join(tree, "OEBPS")
    text_dir = os.path.join(oebps, "Text")
    if not os.path.isdir(text_dir):
        sys.exit(f"{text_dir} not found — put chapters under <tree>/OEBPS/Text/")
    mt = os.path.join(tree, "mimetype")
    if not os.path.exists(mt):
        open(mt, "w").write("application/epub+zip")
    meta_dir = os.path.join(tree, "META-INF")
    os.makedirs(meta_dir, exist_ok=True)
    cont = os.path.join(meta_dir, "container.xml")
    if not os.path.exists(cont):
        open(cont, "w", encoding="utf-8").write(CONTAINER)

    chapters = sorted(f for f in os.listdir(text_dir)
                      if f.endswith((".xhtml", ".html")) and not f.startswith("nav"))
    items, spine, nav = [], [], []
    for f in chapters:
        cid = re.sub(r"[^\w.-]", "_", f.rsplit(".", 1)[0])
        items.append(f'<item id="{cid}" href="Text/{f}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{cid}"/>')
        t = ""
        head = open(os.path.join(text_dir, f), encoding="utf-8").read(4000)
        m = re.search(r"<title>(.*?)</title>", head, re.S)
        if m:
            t = m.group(1).strip()
        nav.append((t or cid, f))
    for sub, mime in (("Styles", "text/css"), ("Images", None)):
        d = os.path.join(oebps, sub)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            cid = re.sub(r"[^\w.-]", "_", sub + "_" + f)
            if mime:
                items.append(f'<item id="{cid}" href="{sub}/{f}" media-type="{mime}"/>')
            else:
                ext = f.rsplit(".", 1)[-1].lower()
                mt = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                      "gif": "image/gif", "svg": "image/svg+xml",
                      "webp": "image/webp"}.get(ext, "image/jpeg")
                items.append(f'<item id="{cid}" href="{sub}/{f}" media-type="{mt}"/>')
    navbody = "".join(f'<p><a href="{html.escape(f)}">{html.escape(t)}</a></p>'
                      for t, f in nav)
    navdoc = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
              '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="%s">\n'
              '<head><title>Contents</title></head>\n<body><h1>Contents</h1>%s</body>\n</html>\n'
              % (lang, navbody))
    open(os.path.join(text_dir, "nav.xhtml"), "w", encoding="utf-8").write(navdoc)
    items.append('<item id="nav" href="Text/nav.xhtml" media-type="application/xhtml+xml"/>')
    spine.insert(0, '<itemref idref="nav"/>')

    opf = (f'<?xml version="1.0" encoding="utf-8"?>\n'
           f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           f'<dc:title>{html.escape(title)}</dc:title>\n'
           f'<dc:creator>{html.escape(author)}</dc:creator>\n'
           f'<dc:language>{lang}</dc:language>\n'
           f'<dc:identifier id="bookid">{re.sub(r"[^a-z0-9]+", "-", title.lower())}</dc:identifier>\n'
           f'</metadata>\n<manifest>\n' + "\n".join(items) +
           '\n</manifest>\n<spine>\n' + "\n".join(spine) + '\n</spine>\n</package>\n')
    open(os.path.join(oebps, "content.opf"), "w", encoding="utf-8").write(opf)
    print(f"wrote content.opf: {len(chapters)} chapters, {len(items)} manifest items")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--from", dest="src", help="source EPUB tree to copy")
    ap.add_argument("--overlay", help="directory whose files replace same-named ones")
    ap.add_argument("--stage", help="package this tree directly")
    ap.add_argument("--init", dest="init_dir", help="scaffold a new tree here")
    ap.add_argument("--finalize", action="store_true",
                    help="write content.opf/nav for the staged tree before packaging")
    ap.add_argument("--keep-stage", help="staging dir to keep (default: temp beside output)")
    ap.add_argument("--title", default="Untitled")
    ap.add_argument("--author", default="Unknown")
    ap.add_argument("--lang", default="zh-CN")
    args = ap.parse_args()

    if args.init_dir:
        init_tree(args.init_dir, args.title, args.author, args.lang)
        return

    if args.stage:
        tree = args.stage
    else:
        if not args.src:
            ap.error("need --from, --stage, or --init")
        tree = args.keep_stage or (args.out + ".stage")
        if os.path.exists(tree):
            shutil.rmtree(tree)
        shutil.copytree(args.src, tree)
        print(f"staged {args.src} -> {tree}")
        if args.overlay:
            overlay(tree, args.overlay)
    if args.finalize:
        finalize(tree, args.title, args.author, args.lang)
    zip_epub(tree, args.out)
    if not args.stage and not args.keep_stage:
        shutil.rmtree(tree, ignore_errors=True)


if __name__ == "__main__":
    main()
