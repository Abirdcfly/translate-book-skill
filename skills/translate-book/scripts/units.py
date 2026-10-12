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
  units.py glossary <units_dir> [--out glossary.json] [--top N]
  units.py claim   <batches_dir> <done_dir> --worker ID [--count N] [--words N]
                   [--release ID] [--units-dir DIR]
  units.py validate-batches <batches_dir> <done_dir> [--units-dir DIR] [--files FILE ...]
  units.py complete <batches_dir> <run_dir> <done_dir> [--units-dir DIR]
                    [--files FILE ...] [--worker ID]
  units.py status  <batches_dir> <done_dir> [--units-dir DIR] [--files FILE ...]
  units.py merge   <units_dir> <done_dir> <trans_dir> --batches-dir DIR
  units.py sample  <units_dir> <trans_dir> [--per-chapter N] [--out FILE]
  units.py rekey   <batches_dir> <old_done_dir> <new_done_dir>

trans_dir/<name>.json holds {"file":..., "segs":[{i, raw, tags, atoms, out}]}.
batches_dir/<name>-bNN.json holds {"file":..., "units":[{key, src}]} where
key is "<i>.text" | "<i>.callouts.<j>" | "<i>.rows.<r>.<c>".

Dispatch is claim-based: a worker translates into its own run directory, and
`complete` promotes a file into the book-level done directory only after the
deterministic validator passes. A worker's own report is never the merge gate.
"""
import argparse
from collections import Counter
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time

from lxml import etree
from lxml import html as LH

# Inline elements kept AND translated.
KEEP_INLINE = {"a", "em", "strong", "sup", "sub", "b", "i", "small", "mark"}
# Inline elements preserved whole, content verbatim.
#   code-like spans must be atoms, NOT "skip subtree" — skipping drops the
#   entire surrounding paragraph, not just the code.
ATOM_INLINE = {"code", "kbd", "samp", "var", "tt", "img", "math"}
# Subtree entirely excluded from translation units.
SKIP_SUBTREE = {"pre", "script", "style"}
INLINE_SET = (KEEP_INLINE | ATOM_INLINE |
              {"span", "br", "abbr", "cite", "q", "u", "s", "font", "big"})

OPEN_RE = re.compile(r"⟧(\d+)⟦")
CLOSE_RE = re.compile(r"⟧/(\d+)⟦")
ATOM_RE = re.compile(r"⟨(\d+)⟩")
TAGNAME_RE = re.compile(r"<([A-Za-z][\w:.-]*)")
TOKEN_SPLIT = re.compile(r"(⟧/?\d+⟦|⟨\d+⟩)")
MARKER_RE = re.compile(r"⟧(?P<close>/)?(?P<index>\d+)⟦")
MALFORMED_MARKER_RE = re.compile(r"(?:⟦/?\d+⟦|⟦/?\d+⟧|⟧/?\d+⟧)")
MARKER_GLYPHS = frozenset("⟦⟧⟨⟩")

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
        # root.iter() descends into atoms and skipped subtrees. MathML <mtext>
        # nodes are leaf elements, so without this guard they become prose units.
        if any(localname(parent) in ATOM_INLINE or localname(parent) in SKIP_SUBTREE
               for parent in el.iterancestors()):
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


def detect_encoding(data):
    """Return an explicit encoding for XHTML that omits a charset declaration."""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    declared = re.match(rb"^\s*<\?xml[^>]*encoding=[\"']([\w.-]+)[\"']", data[:200], re.I)
    if not declared:
        declared = re.search(rb"charset\s*=\s*[\"']?([\w.-]+)", data[:4096], re.I)
    if declared:
        return declared.group(1).decode("ascii", "replace")
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


def parse_file(path):
    with open(path, "rb") as fh:
        data = fh.read()
    tree = LH.parse(io.BytesIO(data), LH.HTMLParser(encoding=detect_encoding(data)))
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


def token_events(text):
    return [(match.group("index"), bool(match.group("close")))
            for match in MARKER_RE.finditer(text)]


def validate(template, translated, tags=None, check_atoms=True):
    """Return None if the unit is safe to write, else a reason string.

    Fewer tokens than the source is allowed: a hyphenated English word split
    across two emphasis spans merges into one word in most target languages.
    """
    malformed = MALFORMED_MARKER_RE.search(translated)
    if malformed:
        return f"malformed marker glyph: {malformed.group(0)}"

    source_events = Counter(token_events(template))
    translated_events = Counter(token_events(translated))
    extra = list((translated_events - source_events).elements())
    if extra:
        return f"introduced or duplicated tokens: {extra}"

    stack = []
    for index, is_close in token_events(translated):
        if is_close:
            if not stack or stack[-1] != index:
                return f"crossed or unopened token: {index}"
            stack.pop()
        else:
            stack.append(index)
    if stack:
        return f"unclosed tokens: {stack}"

    if tags:
        link_indices = {
            str(index) for index, tag in enumerate(tags)
            if (match := TAGNAME_RE.match(tag)) and match.group(1).lower() == "a"
        }
        missing = []
        for index in sorted(link_indices, key=int):
            for event in ((index, False), (index, True)):
                if translated_events[event] != source_events[event]:
                    missing.append(index)
                    break
        if missing:
            return f"dropped link tokens: {missing}"

    if check_atoms:
        source_atoms = Counter(ATOM_RE.findall(template))
        translated_atoms = Counter(ATOM_RE.findall(translated))
        if source_atoms != translated_atoms:
            return f"atom mismatch: source={sorted(source_atoms.elements())} translation={sorted(translated_atoms.elements())}"
    # A marker glyph surviving outside a token/atom passes every pairing check
    # above but renders as a visible bracket in the book. Glyphs the source
    # itself carries outside tokens are not the translator's doing — the
    # identity self-test must still pass on them.
    stray = set(TOKEN_SPLIT.sub("", translated)) & MARKER_GLYPHS
    stray -= set(TOKEN_SPLIT.sub("", template)) & MARKER_GLYPHS
    if stray:
        return f"stray marker outside tokens: {sorted(stray)[0]!r}"
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
        reason = validate(t["raw"], out, t.get("tags") or [],
                          check_atoms=(fname, i) not in exceptions)
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
    if all_fail and args.strict:
        raise SystemExit(1)


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
    digest = plan_digest(args.batches_dir, plan)
    json.dump({"hash": digest, "batches": plan},
              open(os.path.join(args.batches_dir, "_plan.json"), "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{len(plan)} batches  {sum(p['units'] for p in plan)} units  "
          f"{sum(p['words'] for p in plan)} words -> {args.batches_dir}")
    print(f"plan hash {digest}")


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


def plan_digest(batches_dir, entries):
    """Fingerprint the plan by its actual unit keys and source text.

    Re-extracting or re-batching changes the digest, which is how `status`
    detects promoted outputs that were produced against an older plan.
    """
    h = hashlib.sha256()
    for entry in entries:
        h.update(f"{entry.get('file')}\x00{entry.get('batch')}\x00".encode("utf-8"))
        path = os.path.join(batches_dir, entry["file"])
        if not os.path.exists(path):
            continue
        data = json.load(open(path, encoding="utf-8"))
        for unit in data.get("units", []):
            h.update(f"{unit.get('key')}\x00{unit.get('src')}\x00".encode("utf-8"))
    return h.hexdigest()[:16]


def load_plan(batches_dir):
    """Return (entries, hash). Accepts the older bare-list plan format.

    A plan written without a digest gets one computed here, so staleness
    detection works no matter which version of `batch` produced the plan.
    """
    data = json.load(open(os.path.join(batches_dir, "_plan.json"), encoding="utf-8"))
    if isinstance(data, dict):
        entries, digest = data.get("batches", []), data.get("hash")
    else:
        entries, digest = data, None
    return entries, digest or plan_digest(batches_dir, entries)


def read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


def load_claims(batches_dir):
    return read_json(os.path.join(batches_dir, "_claims.json"), {})


def load_state(done_dir):
    return read_json(os.path.join(done_dir, "_state.json"),
                     {"plan_hash": None, "promoted": {}})


def unit_metadata(units_dir):
    """Return (source-file, batch-key) -> extraction metadata for validation."""
    metadata = {}
    if not units_dir:
        return metadata
    for name in sorted(os.listdir(units_dir)):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        data = json.load(open(os.path.join(units_dir, name), encoding="utf-8"))
        for segment in data.get("segs", []):
            metadata[(data["file"], f"{segment['i']}.text")] = segment
    return metadata


def validate_batch_output(expected_path, output_path, metadata):
    """Return a list of user-actionable errors for one worker output file."""
    name = os.path.basename(expected_path)
    if not os.path.exists(output_path):
        return [f"{name}: MISSING"]
    try:
        expected = json.load(open(expected_path, encoding="utf-8"))
        actual = json.load(open(output_path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{name}: invalid JSON: {exc}"]

    if actual.get("file") != expected.get("file") or actual.get("batch") != expected.get("batch"):
        return [f"{name}: file or batch metadata changed"]
    source_units = expected.get("units", [])
    translated_units = actual.get("units", [])
    if len(source_units) != len(translated_units):
        return [f"{name}: unit count {len(translated_units)} != expected {len(source_units)}"]

    errors = []
    for position, (source, translated) in enumerate(zip(source_units, translated_units)):
        key = source.get("key")
        if translated.get("key") != key or translated.get("src") != source.get("src"):
            errors.append(f"{name}: unit {position} key/src changed")
            continue
        output = translated.get("out")
        if not isinstance(output, str) or not output.strip():
            errors.append(f"{name}: {key}: empty out")
            continue
        segment = metadata.get((expected.get("file"), key), {})
        reason = validate(source["src"], output, segment.get("tags"))
        if reason:
            errors.append(f"{name}: {key}: {reason}")
    return errors


def batch_states(batches_dir, done_dir, units_dir=None, selected=None):
    """Return (states, errors, expected_files).

    states[name] is "complete" | "missing" | "invalid" — the base for claim,
    status and complete. One classification, so the three cannot disagree.
    """
    plan, _ = load_plan(batches_dir)
    expected_files = [entry["file"] for entry in plan]
    if selected:
        unknown = sorted(set(selected) - set(expected_files))
        errors = [f"unknown planned batch: {name}" for name in unknown]
        expected_files = [name for name in expected_files if name in set(selected)]
    else:
        errors = []
        actual_files = ({name for name in os.listdir(done_dir) if name.endswith(".json")
                         and not name.startswith("_")}
                        if os.path.isdir(done_dir) else set())
        extras = sorted(actual_files - set(expected_files))
        errors.extend(f"unexpected worker output: {name}" for name in extras)

    metadata = unit_metadata(units_dir)
    states = {}
    for name in expected_files:
        file_errors = validate_batch_output(
            os.path.join(batches_dir, name), os.path.join(done_dir, name), metadata)
        if not file_errors:
            states[name] = "complete"
        elif len(file_errors) == 1 and file_errors[0].endswith(": MISSING"):
            states[name] = "missing"
        else:
            states[name] = "invalid"
        errors.extend(file_errors)
    return states, errors, expected_files


def validate_batch_outputs(batches_dir, done_dir, units_dir=None, selected=None):
    """Validate planned worker outputs and return (errors, expected_files)."""
    _states, errors, expected_files = batch_states(
        batches_dir, done_dir, units_dir, selected)
    return errors, expected_files


def promote_file(src, dst_dir, name):
    """Copy one validated output into the book-level done dir, atomically."""
    os.makedirs(dst_dir, exist_ok=True)
    tmp = os.path.join(dst_dir, f".{name}.{os.getpid()}.tmp")
    shutil.copy(src, tmp)
    os.replace(tmp, os.path.join(dst_dir, name))


def cmd_claim(args):
    """Hand out unclaimed pending batches so two workers never overlap."""
    plan, _ = load_plan(args.batches_dir)
    claims = load_claims(args.batches_dir)
    if args.release:
        freed = [name for name, c in claims.items() if c.get("worker") == args.release]
        for name in freed:
            del claims[name]
        if freed:
            print(f"released {len(freed)} batch(es) claimed by {args.release}")

    states, _errors, expected = batch_states(
        args.batches_dir, args.done_dir, args.units_dir)
    pending = [name for name in expected
               if states[name] != "complete"
               and claims.get(name, {}).get("worker") in (None, args.worker)]

    chosen, words = [], 0
    for name in pending:
        if args.count and len(chosen) >= args.count:
            break
        if chosen and args.words and words + _planned_words(plan, name) > args.words:
            break
        chosen.append(name)
        words += _planned_words(plan, name)
    for name in chosen:
        claims[name] = {"worker": args.worker, "at": time.time()}
    write_json(os.path.join(args.batches_dir, "_claims.json"), claims)

    print(f"worker={args.worker} claimed={len(chosen)} "
          f"pending={len(pending) - len(chosen)}")
    for name in chosen:
        print(f"   {name}")


def _planned_words(plan, name):
    for entry in plan:
        if entry.get("file") == name:
            return entry.get("words", 0)
    return 0


def cmd_complete(args):
    """Validate a worker's run directory, then promote only the files that pass."""
    _plan, plan_hash = load_plan(args.batches_dir)
    if args.files:
        selected = list(args.files)
    else:
        present = ({name for name in os.listdir(args.run_dir)
                    if name.endswith(".json") and not name.startswith("_")}
                   if os.path.isdir(args.run_dir) else set())
        _states, _errors, expected = batch_states(args.batches_dir, args.run_dir)
        selected = [name for name in expected if name in present]
        if not selected:
            print(f"nothing to promote: no planned batch output in {args.run_dir}")
            raise SystemExit(1)

    states, errors, expected = batch_states(
        args.batches_dir, args.run_dir, args.units_dir, selected)

    promoted, rejected = [], []
    for name in expected:
        if states[name] == "complete":
            promote_file(os.path.join(args.run_dir, name), args.done_dir, name)
            promoted.append(name)
        else:
            rejected.append(name)

    state = load_state(args.done_dir)
    stale = 0
    if promoted and state.get("plan_hash") != plan_hash:
        stale = len(state.get("promoted", {}))
        state["promoted"] = {}
    state["plan_hash"] = plan_hash
    for name in promoted:
        state.setdefault("promoted", {})[name] = {"worker": args.worker,
                                                  "at": time.time()}
    write_json(os.path.join(args.done_dir, "_state.json"), state)

    claims = load_claims(args.batches_dir)
    for name in promoted:
        claims.pop(name, None)
    write_json(os.path.join(args.batches_dir, "_claims.json"), claims)

    print(f"promoted {len(promoted)} batch(es) -> {args.done_dir}"
          + (f"  (replacing {stale} stale output(s))" if stale else ""))
    if rejected:
        print(f"REJECTED {len(rejected)} batch(es):")
        for error in errors:
            print(f"   {error}")
        raise SystemExit(1)


def cmd_validate_batches(args):
    errors, expected = validate_batch_outputs(
        args.batches_dir, args.done_dir, args.units_dir, args.files)
    if errors:
        print(f"INVALID {len(errors)} issue(s) across {len(expected)} planned batch(es):")
        for error in errors:
            print(f"   {error}")
        raise SystemExit(1)
    print(f"OK {len(expected)} planned batch(es) validated")


def cmd_status(args):
    """One line of truth for a run: what is done, what is missing, what is claimed."""
    _plan, plan_hash = load_plan(args.batches_dir)
    states, errors, expected = batch_states(
        args.batches_dir, args.done_dir, args.units_dir, args.files)
    claims = load_claims(args.batches_dir)
    state = load_state(args.done_dir)
    counts = Counter(states.values())
    stale = (bool(state.get("promoted")) and bool(state.get("plan_hash"))
             and state["plan_hash"] != plan_hash)
    print(f"planned={len(expected)} complete={counts['complete']} "
          f"missing={counts['missing']} invalid={counts['invalid']} "
          f"claimed={len(claims)} stale={len(state.get('promoted', {})) if stale else 0}")
    for error in errors:
        print(f"   {error}")
    if stale:
        print("   stale: outputs were promoted against an older plan — re-run the "
              "affected batches (rekey first if the extraction changed)")
    if counts["missing"] or counts["invalid"] or stale or any(
            error.startswith("unknown planned batch") or
            error.startswith("unexpected worker output") for error in errors):
        raise SystemExit(1)


def cmd_merge(args):
    """Fold only validated, planned batch outputs into per-chapter files."""
    errors, expected_files = validate_batch_outputs(
        args.batches_dir, args.done_dir, args.units_dir)
    if errors and not args.allow_incomplete:
        print("merge refused: validate batches first")
        for error in errors:
            print(f"   {error}")
        raise SystemExit(1)
    os.makedirs(args.trans_dir, exist_ok=True)
    groups = {}
    for f in expected_files:
        if not os.path.exists(os.path.join(args.done_dir, f)):
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


CJK_RE = re.compile(r"[㐀-䶿一-鿿]")
XREF_RE = re.compile(
    r"\b(figure|fig\.|table|chapter|section|listing|appendix|equation)\s*[\d.]+", re.I)
ASCII_RUN_RE = re.compile(r"(?:\b[A-Za-z]{2,}\b[\s,.;:()\-–]*){4,}")
PHRASE_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b")
TECH_RE = (re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)*\b"),   # PSO, API
           re.compile(r"\b[A-Z][a-z]+[A-Z][A-Za-z]*\b"),          # CamelCase
           re.compile(r"\b[a-zA-Z_]\w*(?:[.:][a-zA-Z_]\w*)+\b"))  # json.loads


def plain_text(raw):
    """Source text with markers removed, for term mining."""
    return ATOM_RE.sub(" ", OPEN_RE.sub(" ", CLOSE_RE.sub(" ", raw)))


def sample_units(segs, trans, per_chapter):
    """Deterministic per-chapter sample: long prose, short headings, xrefs, markers."""
    by_len = sorted(segs, key=lambda s: -len(s["raw"]))
    longs = [s for s in by_len if len(s["raw"]) >= 120][:2] or by_len[:2]
    picks = [(s, "long") for s in longs]
    for kind, pred in (("token", lambda s: "⟧" in s["raw"]),
                       ("atom", lambda s: "⟨" in s["raw"]),
                       ("xref", lambda s: XREF_RE.search(s["raw"])),
                       ("short", lambda s: len(s["raw"]) <= 60)):
        found = next((s for s in segs if pred(s)), None)
        if found is not None:
            picks.append((found, kind))

    entries, seen = [], set()
    for seg, kind in picks:
        if seg["i"] in seen:
            continue
        seen.add(seg["i"])
        out = (trans.get(seg["i"], {}).get("out") or "").strip()
        flags = []
        if not out:
            flags.append("empty")
        else:
            if out == seg["raw"]:
                flags.append("untranslated")
            reason = validate(seg["raw"], out, seg.get("tags") or [])
            if reason:
                flags.append(f"invalid: {reason}")
            if not CJK_RE.search(out):
                flags.append("no-cjk")
            if ASCII_RUN_RE.search(out):
                flags.append("english-leftover")
        entries.append({"i": seg["i"], "kind": kind, "src": seg["raw"],
                        "out": out, "flags": flags})
        if len(entries) >= per_chapter:
            break
    return entries


def cmd_sample(args):
    """Fixed-size review sample per chapter — a checklist, not a quality judgement."""
    chapters, flagged = [], 0
    print("sampled units for review")
    for name in sorted(os.listdir(args.units_dir)):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        data = json.load(open(os.path.join(args.units_dir, name), encoding="utf-8"))
        trans = load_trans_file(os.path.join(args.trans_dir, name))
        entries = sample_units(data.get("segs", []), trans, args.per_chapter)
        n = sum(1 for e in entries if e["flags"])
        flagged += n
        chapters.append({"file": data.get("file", name), "entries": entries})
        print(f"   {data.get('file', name):40s} sampled={len(entries):2d} flagged={n:2d}")
        for e in entries:
            if e["flags"]:
                print(f"      [{e['i']} {e['kind']}] {','.join(e['flags'])}: "
                      f"{e['src'][:60]}")
    report = {"chapters": chapters}
    if args.out:
        write_json(args.out, report)
        print(f"wrote {args.out}")
    print(f"{len(chapters)} chapters  "
          f"{sum(len(c['entries']) for c in chapters)} units sampled  {flagged} flagged")


def cmd_glossary(args):
    """Seed a book-level glossary so parallel batches share one terminology."""
    phrases, technical = Counter(), Counter()
    for name in sorted(os.listdir(args.units_dir)):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        data = json.load(open(os.path.join(args.units_dir, name), encoding="utf-8"))
        for seg in data.get("segs", []):
            text = plain_text(seg["raw"])
            for m in PHRASE_RE.finditer(text):
                phrases[m.group(0)] += 1
            for rx in TECH_RE:
                for m in rx.finditer(text):
                    technical[m.group(0)] += 1
    terms = [t for t, _ in phrases.most_common(args.top)]
    never = [t for t, _ in technical.most_common(args.top)]
    payload = {"terms": {t: "" for t in terms}, "never_translate": never,
               "counts": {**phrases, **technical}}
    write_json(args.out, payload)
    print(f"seeded {len(terms)} terms, {len(never)} never-translate entries -> {args.out}")
    print("fill in the empty values, then point every worker at this file")


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
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero if any unit was rejected")
    p.add_argument("--exceptions", help="JSON list of {file, seg} allowed to differ")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("batch")
    p.add_argument("units_dir"); p.add_argument("batches_dir")
    p.add_argument("--words", type=int, default=1600)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("glossary")
    p.add_argument("units_dir")
    p.add_argument("--out", default="glossary.json")
    p.add_argument("--top", type=int, default=60)
    p.set_defaults(func=cmd_glossary)

    p = sub.add_parser("claim")
    p.add_argument("batches_dir"); p.add_argument("done_dir")
    p.add_argument("--worker", required=True)
    p.add_argument("--count", type=int, default=6)
    p.add_argument("--words", type=int, default=0, help="stop after this many words")
    p.add_argument("--release", help="release another worker's claims first")
    p.add_argument("--units-dir")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("validate-batches")
    p.add_argument("batches_dir"); p.add_argument("done_dir")
    p.add_argument("--units-dir")
    p.add_argument("--files", nargs="*")
    p.set_defaults(func=cmd_validate_batches)

    p = sub.add_parser("complete")
    p.add_argument("batches_dir"); p.add_argument("run_dir"); p.add_argument("done_dir")
    p.add_argument("--units-dir")
    p.add_argument("--files", nargs="*")
    p.add_argument("--worker")
    p.set_defaults(func=cmd_complete)

    p = sub.add_parser("status")
    p.add_argument("batches_dir"); p.add_argument("done_dir")
    p.add_argument("--units-dir")
    p.add_argument("--files", nargs="*")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("merge")
    p.add_argument("units_dir"); p.add_argument("done_dir"); p.add_argument("trans_dir")
    p.add_argument("--batches-dir", required=True)
    p.add_argument("--allow-incomplete", action="store_true",
                   help="merge completed files but keep source text for missing batches")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("sample")
    p.add_argument("units_dir"); p.add_argument("trans_dir")
    p.add_argument("--per-chapter", type=int, default=6)
    p.add_argument("--out", help="write the review sheet as JSON here")
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("rekey")
    p.add_argument("batches_dir"); p.add_argument("old_done_dir"); p.add_argument("new_done_dir")
    p.set_defaults(func=cmd_rekey)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
