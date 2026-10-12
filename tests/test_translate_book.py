import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "translate-book" / "scripts"
UNITS = SCRIPTS / "units.py"
VERIFY = SCRIPTS / "epub_verify.py"


def run(*args, cwd=None):
    return subprocess.run(
        [sys.executable, *map(str, args)], cwd=cwd, text=True,
        capture_output=True, check=False,
    )


def make_plan(root, count=3, name="chapter.xhtml", link_first=False):
    """Write a batches/units/run layout with `count` planned batches."""
    batches = root / "batches"
    done = root / "done"
    units = root / "units"
    run_dir = root / "run"
    for directory in (batches, done, units, run_dir):
        directory.mkdir(parents=True, exist_ok=True)
    segs, plan = [], []
    for b in range(count):
        if link_first and b == 0:
            src = "See ⟧0⟦the guide⟧/0⟦ now."
            tags = ['<a href="#guide">']
        else:
            src = f"Sentence {b} about things."
            tags = []
        segs.append({"i": b, "raw": src, "tags": tags, "atoms": []})
        fname = f"chapter-b{b:02d}.json"
        (batches / fname).write_text(json.dumps(
            {"file": name, "batch": b, "units": [{"key": f"{b}.text", "src": src}]}),
            encoding="utf-8")
        plan.append({"file": fname, "batch": b, "units": 1, "words": 4})
    (batches / "_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (units / "chapter.json").write_text(
        json.dumps({"file": name, "segs": segs}), encoding="utf-8")
    return batches, done, units, run_dir


class TranslateBookRegressionTests(unittest.TestCase):
    def test_identity_round_trip_preserves_utf8_without_charset(self):
        """A charset-less UTF-8 XHTML file must not be decoded as Latin-1."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            units = root / "units"
            output = root / "output"
            source.mkdir()
            (source / "chapter.xhtml").write_bytes(
                b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                + "<p>The client’s request is clear.</p>".encode("utf-8")
                + b"</body></html>"
            )

            self.assertEqual(run(UNITS, "extract", source, units).returncode, 0)
            result = run(UNITS, "write", source, units, units, output, "--identity")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("client’s request", (output / "chapter.xhtml").read_text("utf-8"))

    def test_inline_math_is_an_atom_not_a_separate_translatable_unit(self):
        """Inline MathML must preserve math while leaving surrounding prose translatable."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            units = root / "units"
            source.mkdir()
            (source / "chapter.xhtml").write_text(
                '<html><body><p>Let <math xmlns="http://www.w3.org/1998/Math/MathML">'
                '<mtext>if condition</mtext></math> be true.</p></body></html>',
                encoding="utf-8",
            )

            self.assertEqual(run(UNITS, "extract", source, units).returncode, 0)
            segs = json.loads((units / "chapter.json").read_text("utf-8"))["segs"]

            self.assertEqual(len(segs), 1)
            self.assertEqual(segs[0]["raw"], "Let ⟨0⟩ be true.")

    def test_validate_batches_rejects_lost_link_marker(self):
        """A worker cannot silently remove an anchor/link token from a batch."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            batches = root / "batches"
            done = root / "done"
            units = root / "units"
            batches.mkdir(); done.mkdir(); units.mkdir()
            batch = {
                "file": "chapter.xhtml", "batch": 0,
                "units": [{"key": "0.text", "src": "See ⟧0⟦the guide⟧/0⟦."}],
            }
            (batches / "chapter-b00.json").write_text(json.dumps(batch), encoding="utf-8")
            (batches / "_plan.json").write_text(
                json.dumps([{"file": "chapter-b00.json", "batch": 0, "units": 1, "words": 3}]),
                encoding="utf-8",
            )
            (units / "chapter.json").write_text(json.dumps({"file": "chapter.xhtml", "segs": [{
                "i": 0, "raw": "See ⟧0⟦the guide⟧/0⟦.",
                "tags": ['<a href="#guide">'], "atoms": [],
            }]}), encoding="utf-8")
            bad_output = {**batch, "units": [{**batch["units"][0], "out": "参见指南。"}]}
            (done / "chapter-b00.json").write_text(json.dumps(bad_output), encoding="utf-8")

            result = run(UNITS, "validate-batches", batches, done, "--units-dir", units)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("dropped link tokens", result.stdout)

            valid_output = {**batch, "units": [{**batch["units"][0], "out": "参见⟧0⟦指南⟧/0⟦。"}]}
            (done / "chapter-b00.json").write_text(json.dumps(valid_output), encoding="utf-8")
            result = run(UNITS, "validate-batches", batches, done, "--units-dir", units)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_merge_refuses_an_incomplete_plan_by_default(self):
        """Missing worker output must block merge instead of silently restoring English."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            batches = root / "batches"
            done = root / "done"
            units = root / "units"
            trans = root / "trans"
            for directory in (batches, done, units):
                directory.mkdir()
            batch = {"file": "chapter.xhtml", "batch": 0,
                     "units": [{"key": "0.text", "src": "A sentence."}]}
            (batches / "chapter-b00.json").write_text(json.dumps(batch), encoding="utf-8")
            (batches / "_plan.json").write_text(
                json.dumps([{"file": "chapter-b00.json", "batch": 0, "units": 1, "words": 2}]),
                encoding="utf-8",
            )
            (units / "chapter.json").write_text(json.dumps({"file": "chapter.xhtml", "segs": [{
                "i": 0, "raw": "A sentence.", "tags": [], "atoms": [],
            }]}), encoding="utf-8")

            result = run(UNITS, "merge", units, done, trans, "--batches-dir", batches)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("merge refused", result.stdout)

    def test_validate_batches_rejects_malformed_marker_glyphs(self):
        """A lookalike marker with the wrong bracket glyph cannot be treated as omission."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            batches = root / "batches"
            done = root / "done"
            batches.mkdir(); done.mkdir()
            batch = {
                "file": "chapter.xhtml", "batch": 0,
                "units": [{"key": "0.text", "src": "Use ⟧0⟦this note⟧/0⟦."}],
            }
            (batches / "chapter-b00.json").write_text(json.dumps(batch), encoding="utf-8")
            (batches / "_plan.json").write_text(
                json.dumps([{"file": "chapter-b00.json", "batch": 0, "units": 1, "words": 3}]),
                encoding="utf-8",
            )
            malformed = {**batch, "units": [{**batch["units"][0], "out": "使用⟦0⟦此注释⟦/0⟦。"}]}
            (done / "chapter-b00.json").write_text(json.dumps(malformed), encoding="utf-8")

            result = run(UNITS, "validate-batches", batches, done)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("malformed marker", result.stdout)

    def test_validate_batches_rejects_a_stray_marker_glyph(self):
        """A lone ⟧ typed into prose is not an omission — it renders in the book."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            batches = root / "batches"
            done = root / "done"
            batches.mkdir(); done.mkdir()
            batch = {
                "file": "chapter.xhtml", "batch": 0,
                "units": [{"key": "0.text", "src": "See the note."}],
            }
            (batches / "chapter-b00.json").write_text(json.dumps(batch), encoding="utf-8")
            (batches / "_plan.json").write_text(
                json.dumps([{"file": "chapter-b00.json", "batch": 0, "units": 1, "words": 3}]),
                encoding="utf-8",
            )
            stray = {**batch, "units": [{**batch["units"][0], "out": "参见注释⟧。"}]}
            (done / "chapter-b00.json").write_text(json.dumps(stray), encoding="utf-8")

            result = run(UNITS, "validate-batches", batches, done)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stray marker outside tokens", result.stdout)

            fixed = {**batch, "units": [{**batch["units"][0], "out": "参见注释。"}]}
            (done / "chapter-b00.json").write_text(json.dumps(fixed), encoding="utf-8")
            result = run(UNITS, "validate-batches", batches, done)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_identity_tolerates_marker_glyphs_the_source_carries(self):
        """The stray-marker rule must not fail the self-test on the source's own glyphs."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            units = root / "units"
            output = root / "output"
            source.mkdir()
            (source / "chapter.xhtml").write_text(
                '<html><body><p>The set is written ⟧ in the source text.</p></body></html>',
                encoding="utf-8",
            )

            self.assertEqual(run(UNITS, "extract", source, units).returncode, 0)
            result = run(UNITS, "write", source, units, units, output, "--identity", "--strict")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("⟧ in the source", (output / "chapter.xhtml").read_text("utf-8"))

    def test_flat_overlay_replaces_a_chapter_directly_under_oebps(self):
        """A flat writer output must replace OEBPS/chapter.xhtml, not create a root copy."""
        spec = importlib.util.spec_from_file_location("epub_build", SCRIPTS / "epub_build.py")
        epub_build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(epub_build)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tree = root / "tree"
            overlay = root / "overlay"
            (tree / "OEBPS").mkdir(parents=True)
            overlay.mkdir()
            (tree / "OEBPS" / "chapter.xhtml").write_text("source", encoding="utf-8")
            (overlay / "chapter.xhtml").write_text("translated", encoding="utf-8")

            epub_build.overlay(str(tree), str(overlay))

            self.assertEqual((tree / "OEBPS" / "chapter.xhtml").read_text("utf-8"), "translated")
            self.assertFalse((tree / "chapter.xhtml").exists())

    def test_verify_auto_detects_a_flat_oebps_prefix_for_both_books(self):
        """A flat OEBPS EPUB must not require callers to know its content prefix."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.epub"
            translated = root / "translated.epub"
            for path in (source, translated):
                with zipfile.ZipFile(path, "w") as zf:
                    zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                    zf.writestr(
                        "OEBPS/chapter.xhtml",
                        '<html><body><p><a href="#x">中文正文内容。</a></p></body></html>',
                    )

            result = run(VERIFY, translated, "--source", source)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("a      source      1   translation      1   ok", result.stdout)


    def test_write_strict_fails_when_a_unit_is_rejected(self):
        """The self-test must be a gate: --identity --strict cannot pass with rejections."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            units = root / "units"
            trans = root / "trans"
            output = root / "output"
            for directory in (source, trans):
                directory.mkdir()
            (source / "chapter.xhtml").write_text(
                '<html><body><p>See <a href="#guide">the guide</a> now.</p></body></html>',
                encoding="utf-8")
            self.assertEqual(run(UNITS, "extract", source, units).returncode, 0)
            segs = json.loads((units / "chapter.json").read_text("utf-8"))["segs"]
            segs[0]["out"] = "参见现在的指南。"
            (trans / "chapter.json").write_text(
                json.dumps({"file": "chapter.xhtml", "segs": segs}), encoding="utf-8")

            result = run(UNITS, "write", source, units, trans, output, "--strict")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("REJECTED", result.stdout)

    def test_verify_resolves_images_outside_a_standard_images_dir(self):
        """Books that ship assets under e.g. assets/ must still be inventoried correctly."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            book = root / "book.epub"
            with zipfile.ZipFile(book, "w") as zf:
                zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                zf.writestr("OEBPS/Text/chapter.xhtml",
                            '<html><body><p><img src="../assets/pic.png"/>中文正文内容。</p>'
                            '</body></html>')
                zf.writestr("OEBPS/assets/pic.png", b"\x89PNG")
                zf.writestr("OEBPS/images/unused.png", b"\x89PNG")

            result = run(VERIFY, book)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("packaged 2", result.stdout)
            self.assertIn("orphans 1", result.stdout)
            self.assertNotIn("MISSING:", result.stdout)

    def test_claim_never_hands_the_same_batch_to_two_workers(self):
        """Dispatching must be claim-based, so an interrupted worker cannot be re-run blindly."""
        with tempfile.TemporaryDirectory() as td:
            batches, done, units, _ = make_plan(Path(td), count=3)

            first = run(UNITS, "claim", batches, done, "--worker", "w1", "--count", "2")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn("chapter-b00.json", first.stdout)
            self.assertIn("chapter-b01.json", first.stdout)
            self.assertNotIn("chapter-b02.json", first.stdout)

            second = run(UNITS, "claim", batches, done, "--worker", "w2", "--count", "2")
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("chapter-b02.json", second.stdout)
            self.assertNotIn("chapter-b01.json", second.stdout)

            third = run(UNITS, "claim", batches, done, "--worker", "w3", "--count", "2")
            self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
            self.assertIn("claimed=0", third.stdout)

            released = run(UNITS, "claim", batches, done, "--worker", "w3",
                           "--count", "2", "--release", "w1")
            self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
            self.assertIn("chapter-b00.json", released.stdout)

    def test_complete_promotes_only_validated_files(self):
        """A worker's private run directory must be validated before it reaches the book."""
        with tempfile.TemporaryDirectory() as td:
            batches, done, units, run_dir = make_plan(Path(td), count=2, link_first=True)
            good = {"file": "chapter.xhtml", "batch": 1,
                    "units": [{"key": "1.text", "src": "Sentence 1 about things.",
                               "out": "关于事物的第 1 句话。"}]}
            lost_link = {"file": "chapter.xhtml", "batch": 0,
                         "units": [{"key": "0.text", "src": "See ⟧0⟦the guide⟧/0⟦ now.",
                                    "out": "参见现在的指南。"}]}
            (run_dir / "chapter-b00.json").write_text(json.dumps(lost_link), encoding="utf-8")
            (run_dir / "chapter-b01.json").write_text(json.dumps(good), encoding="utf-8")

            result = run(UNITS, "complete", batches, run_dir, done, "--units-dir", units)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("dropped link tokens", result.stdout)
            self.assertTrue((done / "chapter-b01.json").exists(), "valid batch must be promoted")
            self.assertFalse((done / "chapter-b00.json").exists(), "invalid batch must not land")

    def test_status_flags_outputs_produced_against_an_older_plan(self):
        """Re-batching after a re-extract must invalidate promoted outputs instead of merging them."""
        with tempfile.TemporaryDirectory() as td:
            batches, done, units, run_dir = make_plan(Path(td), count=1)
            batch = json.loads((batches / "chapter-b00.json").read_text("utf-8"))
            batch["units"][0]["out"] = "关于事物的第 0 句话。"
            (run_dir / "chapter-b00.json").write_text(json.dumps(batch), encoding="utf-8")

            self.assertEqual(
                run(UNITS, "complete", batches, run_dir, done, "--units-dir", units).returncode, 0)
            fresh = run(UNITS, "status", batches, done, "--units-dir", units)
            self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
            self.assertIn("complete=1", fresh.stdout)

            plan = json.loads((batches / "_plan.json").read_text("utf-8"))
            batches_list = plan["batches"] if isinstance(plan, dict) else plan
            (batches / "_plan.json").write_text(
                json.dumps({"hash": "stale", "batches": batches_list}), encoding="utf-8")

            stale = run(UNITS, "status", batches, done, "--units-dir", units)
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("stale", stale.stdout)

    def test_sample_flags_untranslated_and_marker_bearing_units(self):
        """Quality review must be a deterministic sample, not 'no English found'."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            units = root / "units"
            trans = root / "trans"
            units.mkdir(); trans.mkdir()
            segs = [
                {"i": 0, "raw": "The parser walks the token stream and rewrites every node it "
                               "finds, keeping the surrounding markup intact so that the final "
                               "document still validates against the original schema definition.",
                 "tags": [], "atoms": []},
                {"i": 1, "raw": "Parser Design", "tags": [], "atoms": []},
                {"i": 2, "raw": "See ⟧0⟦figure 3.1⟧/0⟦ for the derivation.",
                 "tags": ['<a href="#f31">'], "atoms": []},
                {"i": 3, "raw": "Call ⟨0⟩ to parse.", "tags": [], "atoms": ["<code>x</code>"]},
            ]
            (units / "chapter.json").write_text(
                json.dumps({"file": "chapter.xhtml", "segs": segs}), encoding="utf-8")
            (trans / "chapter.json").write_text(json.dumps({"file": "chapter.xhtml", "segs": [
                {**segs[0], "out": "解析器遍历记号流……"},
                {**segs[1], "out": "解析器设计"},
                {**segs[2], "out": ""},
                {**segs[3], "out": segs[3]["raw"]},
            ]}), encoding="utf-8")
            report = root / "sample.json"

            result = run(UNITS, "sample", units, trans, "--out", report)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            entries = json.loads(report.read_text("utf-8"))["chapters"][0]["entries"]
            kinds = {entry["kind"] for entry in entries}
            self.assertTrue({"long", "token", "atom"} & kinds)
            by_index = {entry["i"]: entry for entry in entries}
            self.assertIn("empty", by_index[2]["flags"])
            self.assertIn("untranslated", by_index[3]["flags"])

    def test_glossary_seeds_terms_and_never_translate_entries(self):
        """A shared glossary must be seedable from the source instead of retyped per batch."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            units = root / "units"
            units.mkdir()
            (units / "chapter.json").write_text(json.dumps({"file": "chapter.xhtml", "segs": [{
                "i": 0, "raw": "Particle Swarm Optimization (PSO) calls json.loads twice.",
                "tags": [], "atoms": [],
            }]}), encoding="utf-8")
            out = root / "glossary.json"

            result = run(UNITS, "glossary", units, "--out", out)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(out.read_text("utf-8"))
            self.assertIn("Particle Swarm Optimization", data["terms"])
            self.assertIn("json.loads", data["never_translate"])


if __name__ == "__main__":
    unittest.main()
