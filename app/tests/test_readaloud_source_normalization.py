"""EPUB read-aloud mapping for recorded source normalizations (#2440).

Synthetic text only. Verifies that:
- extreme phrase repetitions (20+ repeats) stay collapsed in the TTS script
  while read-aloud maps through the recorded normalization to highlight the
  full original EPUB span (chunk-timed and Qwen-aligned modes);
- unrecorded mismatches, forged normalization records, and extra unrecorded
  occurrences still fail closed with the same error as on main;
- multiple collapses in one chapter (and in one paragraph) map 1-to-1;
- collapses at chapter and chunk boundaries (including cross-document spans)
  map cleanly;
- three-pass generation persists ``source_normalizations`` in its manifest,
  saved scripts preserve them in ``.meta.json``, and ``build_epub_export``
  reaches them while ignoring incomplete manifests;
- other normalization rules (known corruptions, homoglyphs, illustration
  captions, stripped apostrophes) keep their existing behavior and share the
  one recorded-change mapping helper.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydub import AudioSegment

import core
import project
import readalong_export
import three_pass_generate as tp
from epub_readaloud import (
    build_epub_readaloud,
    get_spoken_text,
    map_spine_to_chunks,
    validate_epub_readaloud,
)
from generate_script import get_generation_quality_path, get_preprocessed_source
from pass_quality import validate_segment_quality
from project import ProjectManager
from publication_source import get_publication_source_path
from routers.script import extract_epub_text
from source_normalization import (
    EXTREME_PHRASE_REPEAT_KEEP,
    EXTREME_PHRASE_REPEAT_MIN,
    apply_recorded_source_normalizations,
    normalize_extreme_phrase_repetitions,
    normalize_homoglyph_words,
    normalize_known_source_corruptions,
)
from source_upload_publication import get_retained_epub_path

SMIL = "{http://www.w3.org/ns/SMIL}"
QUOTE = re.compile(r"(“[^”]*”)")


def _make_epub(bodies, *, toc_label="Chapter One", lang="en"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:'
            'opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/'
            'content.opf"/></rootfiles></container>',
        )
        items = "".join(
            f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>'
            for i in range(len(bodies))
        )
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(bodies)))
        archive.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="UTF-8"?><package xmlns="http://www.idpf.org/'
            '2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://'
            'purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:test:2440</dc:identifier>'
            f'<dc:title>Synthetic Book</dc:title><dc:language>{lang}</dc:language></metadata>'
            '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
            f'properties="nav"/>{items}</manifest><spine>{spine}</spine></package>',
        )
        archive.writestr(
            "OEBPS/nav.xhtml",
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub='
            '"http://www.idpf.org/2007/ops"><head><title>Nav</title></head><body><nav '
            f'epub:type="toc"><ol><li><a href="ch0.xhtml">{toc_label}</a></li></ol></nav>'
            '</body></html>',
        )
        for i, body in enumerate(bodies):
            archive.writestr(
                f"OEBPS/ch{i}.xhtml",
                '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
                f'<title>Chapter {i + 1}</title></head><body>{body}</body></html>',
            )
    return buffer.getvalue()


def _fake_tokens(prepared, step=0.2):
    words = re.findall(r"[\w']+", prepared.replace("’", "'"))
    return [
        {"text": word, "start_time": i * step, "end_time": (i + 1) * step}
        for i, word in enumerate(words)
    ]


def _make_entries_from_texts(texts, *, rate=24000, step=0.2):
    entries, aligned, frame = [], [], 0
    for idx, text in enumerate(texts):
        prepared = get_spoken_text(text)
        tokens = _fake_tokens(prepared, step=step)
        length = int((max(1, len(tokens)) * step + 0.1) * rate)
        entries.append({
            "index": idx,
            "prepared_text": prepared,
            "start_frame": frame,
            "end_frame": frame + length,
            "sample_rate": rate,
        })
        aligned.append({"tokens": tokens})
        frame += length + rate // 10
    return entries, aligned


def _extract_and_normalize(epub_bytes):
    extracted = extract_epub_text("synthetic.epub", archive_bytes=epub_bytes)
    extracted = extracted if isinstance(extracted, str) else extracted[0]
    normalized, preprocessing = get_preprocessed_source(extracted, strip_front_matter=True)
    return extracted, normalized, preprocessing["source_normalizations"]


def _script_chunks_from_normalized(normalized):
    rows = []
    for paragraph in (line.strip() for line in normalized.splitlines() if line.strip()):
        for piece in QUOTE.split(paragraph):
            text = piece.strip()
            if not text:
                continue
            if QUOTE.fullmatch(text):
                rows.append({"speaker": "ALICE", "text": text[1:-1], "instruct": ""})
            else:
                rows.append({"speaker": "NARRATOR", "text": text, "instruct": ""})
    return project.group_into_chunks(rows)


def _highlighted_texts(epub_bytes, chapter_name="ch0"):
    with zipfile.ZipFile(io.BytesIO(epub_bytes)) as archive:
        xhtml = ET.fromstring(archive.read(f"OEBPS/{chapter_name}.xhtml"))
        smil = ET.fromstring(archive.read(f"OEBPS/{chapter_name}.smil"))
    by_id = {el.get("id"): "".join(el.itertext()) for el in xhtml.iter() if el.get("id")}
    return [
        (
            par.find(SMIL + "text").get("src").split("#")[1],
            by_id[par.find(SMIL + "text").get("src").split("#")[1]],
            par.find(SMIL + "audio").get("clipBegin"),
            par.find(SMIL + "audio").get("clipEnd"),
        )
        for par in smil.iter(SMIL + "par")
    ]


class RecordedNormalizationHelperTests(unittest.TestCase):
    """Shared helper applies only valid, unconsumed recorded changes."""

    def test_valid_repetition_change_maps_once_and_does_not_mutate_consumed(self):
        repeated = " ".join(["Hello"] * EXTREME_PHRASE_REPEAT_MIN)
        _normalized, changes = normalize_extreme_phrase_repetitions(f"“{repeated}?”")
        self.assertEqual(1, len(changes))
        consumed = set()
        mapped, used = apply_recorded_source_normalizations(
            f"“{repeated}?”", changes, consumed=consumed
        )
        self.assertEqual("“Hello Hello Hello…?”", mapped)
        self.assertEqual((0,), used)
        self.assertEqual(set(), consumed)  # pure: caller commits on match

        # Once consumed, the same single record cannot map a second span.
        consumed.update(used)
        mapped_again, used_again = apply_recorded_source_normalizations(
            f"“{repeated}?”", changes, consumed=consumed
        )
        self.assertEqual(f"“{repeated}?”", mapped_again)
        self.assertEqual((), used_again)

    def test_forged_or_unknown_normalization_records_are_refused(self):
        forged = [
            {"rule": "arbitrary", "before": "alpha", "after": "beta"},
            {"before": "alpha", "after": "beta"},  # not in KNOWN_SOURCE_CORRUPTIONS
            {"rule": "extreme_phrase_repetition", "before": "Hello Hello Hello Hello", "after": "Hello Hello Hello…"},
            {"rule": "extreme_phrase_repetition", "before": "alpha beta", "after": "alpha…"},
            {"rule": "homoglyph", "before": "alpha", "after": "beta"},
            {"rule": "illustration_caption", "before": "Normal story line.", "after": ""},
            {"rule": "stripped_apostrophe", "before": "two words here", "after": "two'words'here"},
        ]
        source = "alpha beta Hello Hello Hello Hello Normal story line. two words here"
        mapped, used = apply_recorded_source_normalizations(source, forged)
        self.assertEqual(source, mapped)
        self.assertEqual((), used)

    def test_other_normalization_rules_keep_existing_behavior_and_map_when_recorded(self):
        padding = "The patient gardener carefully tended the tall flowers beside the stone wall. " * 20
        # Known corruption + illustration caption
        known_src = padding + "Take саге. Illustration from Volume 1, coloring by Artist (source) Stay."
        known_norm, known_changes = normalize_known_source_corruptions(known_src)
        self.assertIn("Take care.", known_norm)
        self.assertNotIn("Illustration from Volume", known_norm)
        mapped_known, used_known = apply_recorded_source_normalizations(
            "Take саге. Illustration from Volume 1, coloring by Artist (source) Stay.",
            known_changes,
        )
        self.assertEqual("Take care. Stay.", mapped_known)
        self.assertEqual(2, len(used_known))

        # Full-paragraph illustration caption removed when recorded, refused when unrecorded.
        cap_body = (
            "<p>Illustration from Volume 1, coloring by Artist (source)</p>"
            "<p>Take саге on the bridge.</p>"
        )
        cap_epub = _make_epub([cap_body])
        cap_entries, cap_aligned = _make_entries_from_texts(["Take care on the bridge."])
        with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
            build_epub_readaloud(
                retained_epub_bytes=cap_epub,
                audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3",
                script_entries=cap_entries,
            )
        for mode_aligned in (None, cap_aligned):
            built, _ = build_epub_readaloud(
                retained_epub_bytes=cap_epub,
                audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3",
                script_entries=cap_entries,
                aligned_rows=mode_aligned,
                source_normalizations=known_changes,
            )
            validate_epub_readaloud(built)
            self.assertEqual(["Take саге on the bridge."], [t for _i, t, _b, _e in _highlighted_texts(built)])

        # Homoglyph repair
        homo_src = padding + "Аlice opened the gate."
        homo_norm, homo_changes = normalize_homoglyph_words(homo_src)
        self.assertIn("Alice opened the gate.", homo_norm)
        mapped_homo, used_homo = apply_recorded_source_normalizations(
            "Аlice opened the gate.", homo_changes
        )
        self.assertEqual("Alice opened the gate.", mapped_homo)
        self.assertEqual((0,), used_homo)

        # Threshold unchanged: 19 repeats stays untouched; 20 collapses to 3 + ellipsis.
        nineteen = " ".join(["Hello"] * (EXTREME_PHRASE_REPEAT_MIN - 1))
        twenty = " ".join(["Hello"] * EXTREME_PHRASE_REPEAT_MIN)
        self.assertEqual((nineteen, []), normalize_extreme_phrase_repetitions(nineteen))
        collapsed, rep_changes = normalize_extreme_phrase_repetitions(twenty)
        self.assertEqual(" ".join(["Hello"] * EXTREME_PHRASE_REPEAT_KEEP) + "…", collapsed)
        self.assertEqual("extreme_phrase_repetition", rep_changes[0]["rule"])


class ReadaloudNormalizationMappingTests(unittest.TestCase):
    """Issue #2440 reproduction, boundaries, multiple collapses, and strict refusals."""

    def test_repro_builds_and_highlights_full_original_span_in_both_modes(self):
        repeated = " ".join(["Hello"] * 20)
        body = (
            "<h1>Chapter One</h1>"
            "<p>The lantern flickered in the hall.</p>"
            f"<p>“{repeated}?”</p>"
            "<p>Nobody answered from the stairs.</p>"
        )
        epub = _make_epub([body])
        extracted, normalized, changes = _extract_and_normalize(epub)
        self.assertEqual(1, len(changes))
        self.assertEqual("extreme_phrase_repetition", changes[0]["rule"])
        self.assertIn("“Hello Hello Hello…?”", normalized)

        # Fidelity gate passes against normalized source (by design) and rejects
        # the same shortened entries against raw unnormalized source.
        seg_entries = [
            {"type": "SPOKEN", "text": line[1:-1]}
            if line.startswith("“") and line.endswith("”")
            else {"type": "NARRATOR", "text": line}
            for line in (row.strip() for row in normalized.splitlines() if row.strip())
        ]
        self.assertTrue(validate_segment_quality(normalized, seg_entries)["passed"])
        self.assertFalse(validate_segment_quality(extracted, seg_entries)["passed"])

        chunks = _script_chunks_from_normalized(normalized)
        entries, aligned = _make_entries_from_texts([c["text"] for c in chunks])

        # Without recorded changes -> still fails closed with exact main error.
        with self.assertRaisesRegex(
            ValueError,
            r"No unambiguous synchronization for spine text:.*EPUB 'hello', spoken 'nobody'",
        ):
            build_epub_readaloud(
                retained_epub_bytes=epub,
                audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3",
                script_entries=entries,
            )

        # With recorded changes -> succeeds in BOTH chunk-timed and Qwen modes
        # and highlights the full original 20-Hello <p> span.
        for aligned_rows in (None, aligned):
            with self.subTest(qwen=aligned_rows is not None):
                built, _report = build_epub_readaloud(
                    retained_epub_bytes=epub,
                    audio_bytes=b"\xff\xfbmp3",
                    audio_filename="audiobook.mp3",
                    script_entries=entries,
                    aligned_rows=aligned_rows,
                    source_normalizations=changes,
                )
                validate_epub_readaloud(built)
                clips = _highlighted_texts(built, "ch0")
                self.assertEqual(4, len(clips))
                self.assertEqual(f"“{repeated}?”", clips[2][1])

    def test_unrecorded_mismatch_and_wrong_spoken_text_still_fail(self):
        repeated = " ".join(["Hello"] * 20)
        body = f"<h1>Chapter One</h1><p>“{repeated}?”</p><p>The door shut.</p>"
        epub = _make_epub([body])
        _extracted, _normalized, changes = _extract_and_normalize(epub)

        # Recorded change exists, but spoken text dropped to only 2 Hellos -> must fail.
        wrong_entries, _ = _make_entries_from_texts(["Chapter One", "Hello Hello…?", "The door shut."])
        with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
            build_epub_readaloud(
                retained_epub_bytes=epub,
                audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3",
                script_entries=wrong_entries,
                source_normalizations=changes,
            )

        # Recorded change exists for Hello x20, but another paragraph has an
        # unrecorded word swap -> must still fail closed.
        swapped_entries, _ = _make_entries_from_texts(
            ["Chapter One", "Hello Hello Hello…?", "The gate shut."]
        )
        with self.assertRaisesRegex(ValueError, r"EPUB 'door', spoken 'gate'"):
            build_epub_readaloud(
                retained_epub_bytes=epub,
                audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3",
                script_entries=swapped_entries,
                source_normalizations=changes,
            )

    def test_multiple_collapses_in_one_chapter_and_one_paragraph(self):
        hello20 = " ".join(["Hello"] * 20)
        wait22 = " ".join(["Wait"] * 22)
        run20 = " ".join(["Run away"] * 20)
        body = (
            "<h1>Chapter One</h1>"
            f"<p>“{hello20}?” he called, then whispered, “{wait22}!”</p>"
            "<p>Footsteps echoed in the yard.</p>"
            f"<p>“{run20}!”</p>"
        )
        epub = _make_epub([body])
        _extracted, normalized, changes = _extract_and_normalize(epub)
        self.assertEqual(3, len(changes))
        chunks = _script_chunks_from_normalized(normalized)
        entries, aligned = _make_entries_from_texts([c["text"] for c in chunks])

        for aligned_rows in (None, aligned):
            with self.subTest(qwen=aligned_rows is not None):
                built, _ = build_epub_readaloud(
                    retained_epub_bytes=epub,
                    audio_bytes=b"\xff\xfbmp3",
                    audio_filename="audiobook.mp3",
                    script_entries=entries,
                    aligned_rows=aligned_rows,
                    source_normalizations=changes,
                )
                validate_epub_readaloud(built)
                clips = _highlighted_texts(built, "ch0")
                texts = [text for _id, text, _b, _e in clips]
                self.assertIn(f"“{hello20}?” he called, then whispered, “{wait22}!”", texts)
                self.assertIn(f"“{run20}!”", texts)

        # Dropping one recorded change must fail closed (cannot reuse a change).
        with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
            build_epub_readaloud(
                retained_epub_bytes=epub,
                audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3",
                script_entries=entries,
                source_normalizations=changes[:-1],
            )

    def test_single_recorded_change_cannot_authorize_second_identical_collapse(self):
        hello20 = " ".join(["Hello"] * 20)
        body = f"<p>“{hello20}?”</p><p>“{hello20}?”</p>"
        epub = _make_epub([body])
        _extracted, _normalized, changes = _extract_and_normalize(epub)
        self.assertEqual(2, len(changes))
        entries, _ = _make_entries_from_texts(["Hello Hello Hello…?", "Hello Hello Hello…?"])

        # Only 1 recorded change supplied -> second identical collapse in same chapter must fail.
        with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
            build_epub_readaloud(
                retained_epub_bytes=epub,
                audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3",
                script_entries=entries,
                source_normalizations=changes[:1],
            )

        # Same across two separate spine documents: 1 recorded change in ch0
        # must not be reused to authorize an unrecorded identical collapse in ch1.
        two_ch_epub = _make_epub([f"<p>“{hello20}?”</p>", f"<p>“{hello20}?”</p>"])
        with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
            build_epub_readaloud(
                retained_epub_bytes=two_ch_epub,
                audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3",
                script_entries=entries,
                source_normalizations=changes[:1],
            )

    def test_collapse_at_chapter_and_chunk_boundaries(self):
        hello20 = " ".join(["Hello"] * 20)
        echo20 = " ".join(["Echo"] * 20)
        # ch0 ends with a collapsed paragraph; ch1 starts with a collapsed paragraph.
        epub = _make_epub([
            f"<h1>Chapter One</h1><p>The hall went still.</p><p>{hello20}.</p>",
            f"<p>{echo20}!</p><p>Morning finally arrived.</p>",
        ])
        _extracted, normalized, changes = _extract_and_normalize(epub)
        self.assertEqual(2, len(changes))

        # Case 1: chunk spans the end of ch0 into the start of ch1 (cross-document span group).
        entries, aligned = _make_entries_from_texts([
            "Chapter One",
            "The hall went still. Hello Hello Hello…. Echo Echo Echo…! Morning finally arrived.",
        ])
        built, _ = build_epub_readaloud(
            retained_epub_bytes=epub,
            audio_bytes=b"\xff\xfbmp3",
            audio_filename="audiobook.mp3",
            script_entries=entries,
            aligned_rows=aligned,
            source_normalizations=changes,
        )
        validate_epub_readaloud(built)
        ch0_clips = _highlighted_texts(built, "ch0")
        ch1_clips = _highlighted_texts(built, "ch1")
        self.assertEqual(f"{hello20}.", ch0_clips[-1][1])
        self.assertEqual(f"{echo20}!", ch1_clips[0][1])

        # Case 2: chunk boundary merges a normal paragraph with a collapsed paragraph in one chapter.
        single_ch = _make_epub([
            f"<p>{hello20}?</p><p>Nobody answered.</p>",
        ])
        _e2, _n2, c2 = _extract_and_normalize(single_ch)
        merged_entries, merged_aligned = _make_entries_from_texts([
            "Hello Hello Hello…? Nobody answered.",
        ])
        for mode_aligned in (None, merged_aligned):
            with self.subTest(merged_qwen=mode_aligned is not None):
                built_merged, _ = build_epub_readaloud(
                    retained_epub_bytes=single_ch,
                    audio_bytes=b"\xff\xfbmp3",
                    audio_filename="audiobook.mp3",
                    script_entries=merged_entries,
                    aligned_rows=mode_aligned,
                    source_normalizations=c2,
                )
                validate_epub_readaloud(built_merged)
                clips = _highlighted_texts(built_merged, "ch0")
                joined = " ".join(text for _id, text, _b, _e in clips)
                self.assertIn(hello20, joined)
                self.assertIn("Nobody answered.", joined)

        # Case 3: unrecorded cross-chapter reuse refuses when only 1 of 2 changes is recorded.
        with self.assertRaisesRegex(ValueError, "continues past the end"):
            build_epub_readaloud(
                retained_epub_bytes=epub,
                audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3",
                script_entries=entries,
                aligned_rows=aligned,
                source_normalizations=changes[:1],
            )

    def test_map_spine_to_chunks_prefers_raw_match_before_consuming_change(self):
        hello20 = " ".join(["Hello"] * 20)
        _norm, changes = normalize_extreme_phrase_repetitions(hello20)
        # First segment was restored to 20 Hellos in the script; second segment stays collapsed.
        segments = [
            {"fragment": None, "text": f"{hello20}?"},
            {"fragment": None, "text": f"{hello20}?"},
        ]
        entries, _ = _make_entries_from_texts([f"{hello20}?", "Hello Hello Hello…?"])
        consumed = set()
        mapping, cursor = map_spine_to_chunks(
            segments, entries, source_normalizations=changes, consumed_normalizations=consumed
        )
        self.assertEqual(2, cursor)
        self.assertEqual({0}, consumed)
        self.assertNotIn("spoken_text", mapping[0])
        self.assertEqual("Hello Hello Hello…?", mapping[1]["spoken_text"])


class ReadaloudNormalizationPersistenceTests(unittest.TestCase):
    """Persistence from three-pass generation to build_epub_export."""

    def test_three_pass_main_persists_source_normalizations_in_manifest(self):
        repeated = " ".join(["Hello"] * 20)
        raw = f"The lantern flickered.\n\n{repeated}?\n\nNobody answered."
        expected_norm, pre = get_preprocessed_source(raw, strip_front_matter=True)
        self.assertEqual(1, len(pre["source_normalizations"]))

        def create(**kwargs):
            prompt = kwargs["messages"][-1]["content"]
            match = re.search(r'\[\{"n":', prompt)
            if not match:
                raise AssertionError("Expected delivery request only: " + prompt)
            rows = json.JSONDecoder().raw_decode(prompt[match.start():])[0]
            payload = [
                {"n": entry["n"], "head": " ".join(entry["text"].split()[:3]), "instruct": "Neutral."}
                for entry in rows
            ]
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(payload)),
                    finish_reason="stop",
                )],
                usage=None,
            )

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        config = {
            "llm_mode": "local",
            "llm_local": {"model_name": "fixture", "structured_output": "off"},
            "generation": {"max_tokens": 500, "three_pass_segmentation": "quotes"},
        }
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            source = root / "book.txt"
            source.write_text(raw, encoding="utf-8")
            output = root / "annotated_script.json"
            with patch.dict(os.environ, {"ALEXANDRIA_DATA_DIR": tmp}), \
                 patch.object(tp.sys, "argv", ["generator", str(source), "--output", str(output)]), \
                 patch.object(tp, "load_app_config", return_value=config), \
                 patch.object(tp, "ensure_ideal_settings", return_value=(False, {"context_length": 32768}, "fixture")), \
                 patch.object(tp, "make_run_client", return_value=client), \
                 patch.object(tp, "DEFAULT_MODEL_PROFILES_PATH", str(Path(__file__).parent.parent / "three_pass_model_profiles.json")):
                tp.main()
            manifest = json.loads(Path(tp.three_pass_manifest_path(str(output))).read_text(encoding="utf-8"))
            self.assertEqual("complete", manifest["status"])
            self.assertEqual(pre["source_normalizations"], manifest["source_normalizations"])
            self.assertEqual(
                pre["source_normalizations"],
                readalong_export.get_recorded_source_normalizations(str(output)),
            )

    def test_get_recorded_source_normalizations_reads_complete_sources_only(self):
        repeated = " ".join(["Hello"] * 20)
        _norm, changes = normalize_extreme_phrase_repetitions(repeated)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_path = root / "annotated_script.json"
            script_path.write_text("[]", encoding="utf-8")

            # Incomplete three-pass manifest is ignored.
            tp_manifest = Path(tp.three_pass_manifest_path(str(script_path)))
            tp_manifest.write_text(
                json.dumps({"status": "incomplete", "source_normalizations": changes}),
                encoding="utf-8",
            )
            self.assertEqual([], readalong_export.get_recorded_source_normalizations(str(script_path)))

            # Complete single-pass quality manifest is read when three-pass is not complete.
            gq_manifest = Path(get_generation_quality_path(str(script_path)))
            gq_manifest.write_text(
                json.dumps({"status": "complete", "source_normalizations": changes}),
                encoding="utf-8",
            )
            self.assertEqual(changes, readalong_export.get_recorded_source_normalizations(str(script_path)))

            # Saved script .meta.json is read when active manifests are absent.
            tp_manifest.unlink()
            gq_manifest.unlink()
            saved = root / "saved_book.json"
            saved.write_text("[]", encoding="utf-8")
            (root / "saved_book.meta.json").write_text(
                json.dumps({"book_id": "b1", "source_normalizations": changes}),
                encoding="utf-8",
            )
            self.assertEqual(
                changes,
                readalong_export.get_recorded_source_normalizations(str(script_path), str(saved)),
            )

    def test_build_epub_export_end_to_end_uses_recorded_manifest_normalizations(self):
        repeated = " ".join(["Hello"] * 20)
        body = (
            "<h1>Chapter One</h1>"
            "<p>The lantern flickered in the hall.</p>"
            f"<p>“{repeated}?”</p>"
            "<p>Nobody answered from the stairs.</p>"
        )
        epub_bytes = _make_epub([body])
        extracted, normalized, changes = _extract_and_normalize(epub_bytes)
        self.assertEqual(1, len(changes))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / "uploads"
            uploads.mkdir()
            source_txt = uploads / "book.txt"
            source_txt.write_text(extracted, encoding="utf-8")
            retained_epub = Path(get_retained_epub_path(str(source_txt)))
            retained_epub.write_bytes(epub_bytes)

            epub_sha = hashlib.sha256(epub_bytes).hexdigest()
            txt_sha = hashlib.sha256(source_txt.read_bytes()).hexdigest()
            candidate_id = hashlib.sha256(
                f"AA2_epub_publication_v1:{epub_sha}:{txt_sha}".encode()
            ).hexdigest()
            pub_doc = {
                "version": 1,
                "binding": {"kind": "text", "sha256": txt_sha},
                "candidates": [{
                    "version": 1,
                    "id": candidate_id,
                    "origin": {
                        "kind": "epub",
                        "filename": "book.epub",
                        "sha256": epub_sha,
                        "text_sha256": txt_sha,
                    },
                    "fields": {"title": ["Synthetic Book"], "author": []},
                    "other_creators": [],
                    "metadata_status": "available",
                    "cover": None,
                    "cover_status": "not_declared",
                    "warnings": [],
                }],
            }
            Path(get_publication_source_path(str(source_txt))).write_text(
                json.dumps(pub_doc), encoding="utf-8"
            )

            script_rows = _script_chunks_from_normalized(normalized)
            script_path = root / "annotated_script.json"
            script_path.write_text(json.dumps(script_rows), encoding="utf-8")
            (root / "state.json").write_text(
                json.dumps({"input_file_path": str(source_txt), "active_book_id": "book-2440"}),
                encoding="utf-8",
            )
            (root / "config.json").write_text(json.dumps({"generation": {}}), encoding="utf-8")

            manager = ProjectManager(str(root))
            chunks = manager.load_chunks()
            audio_dir = root / "audio"
            audio_dir.mkdir(exist_ok=True)
            for idx, chunk in enumerate(chunks):
                wav_path = audio_dir / f"chunk_{idx}.wav"
                AudioSegment.silent(duration=500, frame_rate=24000).export(
                    str(wav_path), format="wav"
                ).close()
                chunk["audio_path"] = str(wav_path)
                chunk["status"] = "completed"
            manager.save_chunks(chunks)

            with patch.object(core, "UPLOADS_DIR", str(uploads)):
                # Without manifest normalizations -> fails closed.
                ok_without, msg_without = readalong_export.build_epub_export(manager, lambda: False)
                self.assertFalse(ok_without, msg_without)
                self.assertIn("No unambiguous synchronization", msg_without)

                # With completed three-pass manifest carrying recorded normalizations -> succeeds.
                Path(tp.three_pass_manifest_path(str(script_path))).write_text(
                    json.dumps({"status": "complete", "source_normalizations": changes}),
                    encoding="utf-8",
                )
                ok_with, msg_with = readalong_export.build_epub_export(manager, lambda: False)
                self.assertTrue(ok_with, msg_with)
                built_bytes = (root / "readaloud.epub").read_bytes()
                validate_epub_readaloud(built_bytes)
                clips = _highlighted_texts(built_bytes, "ch0")
                self.assertTrue(any(f"“{repeated}?”" == text for _id, text, _b, _e in clips))

    def test_save_script_preserves_recorded_normalizations_in_meta_json(self):
        from routers import scripts_library

        repeated = " ".join(["Hello"] * 20)
        _norm, changes = normalize_extreme_phrase_repetitions(repeated)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            script_path = root / "annotated_script.json"
            script_path.write_text(
                json.dumps([{"speaker": "NARRATOR", "text": "Hello Hello Hello…?", "instruct": ""}]),
                encoding="utf-8",
            )
            (root / "state.json").write_text(
                json.dumps({"active_book_id": "book-2440", "input_file_path": str(root / "book.txt")}),
                encoding="utf-8",
            )
            manifest_path = Path(tp.three_pass_manifest_path(str(script_path)))
            manifest_path.write_text(
                json.dumps({"status": "complete", "source_normalizations": changes}),
                encoding="utf-8",
            )
            req = SimpleNamespace(name="normalized_book", expected_book_token=None)
            with patch.object(scripts_library, "DATA_DIR", str(root)), \
                 patch.object(scripts_library, "SCRIPTS_DIR", str(scripts_dir)), \
                 patch.object(scripts_library, "SCRIPT_PATH", str(script_path)), \
                 patch.object(scripts_library, "VOICE_CONFIG_PATH", str(root / "voice_config.json")):
                result = scripts_library._save_script_sync(req)
                self.assertEqual("saved", result["status"])
                saved_meta = json.loads((scripts_dir / "normalized_book.meta.json").read_text(encoding="utf-8"))
                self.assertEqual(changes, saved_meta["source_normalizations"])

                # Without recorded normalizations, .meta.json omits the key.
                manifest_path.unlink()
                req_plain = SimpleNamespace(name="plain_book", expected_book_token=None)
                scripts_library._save_script_sync(req_plain)
                plain_meta = json.loads((scripts_dir / "plain_book.meta.json").read_text(encoding="utf-8"))
                self.assertNotIn("source_normalizations", plain_meta)


if __name__ == "__main__":
    unittest.main()
