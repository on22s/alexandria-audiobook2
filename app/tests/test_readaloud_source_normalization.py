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
    get_valid_recorded_normalizations,
    map_recorded_source_normalizations,
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


def _three_pass_manifest_doc(source_path, changes, *, status="complete",
                             strip_front_matter=True):
    """A complete three-pass manifest bound to the source file at source_path."""
    from three_pass_generate import get_prepared_source

    book, _report = get_prepared_source(
        str(source_path), strip_front_matter=strip_front_matter,
        report=lambda message: None)
    script_sha256 = hashlib.sha256(b"[]").hexdigest()
    return {
        "status": status,
        "fingerprint": {"source_sha256": hashlib.sha256(book.encode("utf-8")).hexdigest()},
        "completion": {"status": status, "run_id": "a" * 32,
                       "script_sha256": script_sha256, "entries": 0},
        "source_normalizations": changes,
    }


def _quality_manifest_doc(source_path, changes):
    """A complete single-pass quality manifest bound to the source file."""
    from script_selection_binding import _source_digest

    return {
        "status": "complete",
        "completion_artifact": {
            "version": 1,
            "input_sha256": _source_digest(str(source_path)),
            "output_sha256": hashlib.sha256(b"[]").hexdigest(),
        },
        "source_normalizations": changes,
    }


def _malformed_record_variants(record):
    """Gate-4 L1 repro shapes: JSON-supplied fields that are never hashed as-is."""
    return (
        dict(record, offset=[record["offset"]]),
        dict(record, offset={"start": record["offset"]}),
        dict(record, offset=True),
        dict(record, rule=[record["rule"]]),
        dict(record, rule={}),
    )


class RecordedNormalizationHelperTests(unittest.TestCase):
    """Shared helper applies only valid, unconsumed recorded changes."""

    def test_valid_repetition_change_maps_once_and_does_not_mutate_consumed(self):
        repeated = " ".join(["Hello"] * EXTREME_PHRASE_REPEAT_MIN)
        _normalized, changes = normalize_extreme_phrase_repetitions(f"“{repeated}?”")
        self.assertEqual(1, len(changes))
        consumed = set()
        mapped, used = map_recorded_source_normalizations(
            f"“{repeated}?”", changes, consumed=consumed
        )
        self.assertEqual("“Hello Hello Hello…?”", mapped)
        self.assertEqual((0,), used)
        self.assertEqual(set(), consumed)  # pure: caller commits on match

        # Once consumed, the same single record cannot map a second span.
        consumed.update(used)
        mapped_again, used_again = map_recorded_source_normalizations(
            f"“{repeated}?”", changes, consumed=consumed
        )
        self.assertEqual(f"“{repeated}?”", mapped_again)
        self.assertEqual((), used_again)

    def test_shorter_recorded_run_does_not_match_a_longer_run(self):
        # L1: the 20-run record must not consume the first 20 of a 21-run.
        hello20 = " ".join(["Hello"] * 20)
        hello21 = " ".join(["Hello"] * 21)
        _n20, r20 = normalize_extreme_phrase_repetitions(f"“{hello20}?”")
        _n21, r21 = normalize_extreme_phrase_repetitions(f"“{hello21}?”")
        records = [dict(r20[0]), dict(r21[0])]

        mapped, used = map_recorded_source_normalizations(f"“{hello21}?”", records)
        self.assertEqual("“Hello Hello Hello…?”", mapped)
        self.assertEqual((1,), used)
        self.assertEqual(f"“{hello21}?”",
                         map_recorded_source_normalizations(f"“{hello21}?”", [records[0]])[0])

        # A later whole 20-run in the same text is still found after the
        # 21-run prefix match is refused.
        mapped_tail, used_tail = map_recorded_source_normalizations(
            f"“{hello21}?” then “{hello20}?”", records)
        self.assertEqual("“Hello Hello Hello…?” then “Hello Hello Hello…?”", mapped_tail)
        self.assertEqual({0, 1}, set(used_tail))

    def test_duplicate_records_authorize_only_distinct_recorded_changes(self):
        # L2: the same record twice is one recorded change, not two.
        repeated = " ".join(["Hello"] * 20)
        _normalized, changes = normalize_extreme_phrase_repetitions(f"“{repeated}?”")
        duplicated = [changes[0], dict(changes[0])]
        mapped, used = map_recorded_source_normalizations(f"“{repeated}?”", duplicated)
        self.assertEqual("“Hello Hello Hello…?”", mapped)
        self.assertEqual((0,), used)
        self.assertEqual(
            (),
            map_recorded_source_normalizations(
                f"“{repeated}?”", duplicated, consumed={0})[1])

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
        mapped, used = map_recorded_source_normalizations(source, forged)
        self.assertEqual(source, mapped)
        self.assertEqual((), used)

    def test_record_fields_are_type_checked_before_identity_hashing(self):
        # L1: JSON can supply a list or an object for a field that joins the
        # duplicate-identity tuple. Those records are dropped before
        # ``identity in seen``, which would raise TypeError: unhashable type.
        repeated = " ".join(["Hello"] * 20)
        _normalized, changes = normalize_extreme_phrase_repetitions(f"“{repeated}?”")
        valid = changes[0]
        self.assertEqual(1, len(get_valid_recorded_normalizations([valid])))
        for forged in _malformed_record_variants(valid):
            with self.subTest(fields=sorted(forged)):
                self.assertEqual([], get_valid_recorded_normalizations([forged]))
                # A valid record next to the malformed one still authorizes its
                # own collapse in either order; only the malformed entry is
                # dropped, so no unambiguous synchronization is lost.
                self.assertEqual(
                    1, len(get_valid_recorded_normalizations([valid, forged])))
                self.assertEqual(
                    1, len(get_valid_recorded_normalizations([forged, valid])))
                mapped, used = map_recorded_source_normalizations(
                    f"“{repeated}?”", [forged])
                self.assertEqual(f"“{repeated}?”", mapped)
                self.assertEqual((), used)

    def test_other_normalization_rules_keep_existing_behavior_and_map_when_recorded(self):
        padding = "The patient gardener carefully tended the tall flowers beside the stone wall. " * 20
        # Known corruption + illustration caption
        known_src = padding + "Take саге. Illustration from Volume 1, coloring by Artist (source) Stay."
        known_norm, known_changes = normalize_known_source_corruptions(known_src)
        self.assertIn("Take care.", known_norm)
        self.assertNotIn("Illustration from Volume", known_norm)
        mapped_known, used_known = map_recorded_source_normalizations(
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
        mapped_homo, used_homo = map_recorded_source_normalizations(
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

    def test_recorded_trailing_run_does_not_authorize_the_tail_of_a_longer_run(self):
        # M21: a genuine 20-repeat record must not authorize the last 20 words
        # of a 60-repeat run. finditer's candidate at words 40-60 has no
        # repetition after it, so only the neighbour-before check refuses it.
        run60 = " ".join(["Hello"] * 60)
        run20 = " ".join(["Hello"] * 20)
        run40 = " ".join(["Hello"] * 40)
        body = (
            "<h1>Chapter One</h1>"
            f"<p>“{run60}?”</p>"
            "<p>Then silence.</p>"
            f"<p>“{run20}?”</p>"
        )
        epub = _make_epub([body])
        _extracted, _normalized, changes = _extract_and_normalize(epub)
        self.assertEqual(2, len(changes))
        trailing_20 = [change for change in changes
                       if len(change["before"].split()) == 20]
        self.assertEqual(1, len(trailing_20))
        # The mapper must refuse the 60-run outright: accepting the trailing
        # candidate would bind this paragraph's record to another paragraph.
        self.assertEqual(
            (f"“{run60}?”", ()),
            map_recorded_source_normalizations(f"“{run60}?”", trailing_20))

        # The script keeps 40 raw Hellos plus the collapsed tail in the first
        # paragraph; the only record belongs to the second paragraph. That is
        # an unrecorded mismatch and must be refused in both modes.
        entries, aligned = _make_entries_from_texts(
            ["Chapter One", f"{run40} Hello Hello Hello…?", "Then silence.",
             f"“{run20}?”"])
        for aligned_rows in (None, aligned):
            with self.subTest(qwen=aligned_rows is not None):
                with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
                    build_epub_readaloud(
                        retained_epub_bytes=epub,
                        audio_bytes=b"\xff\xfbmp3",
                        audio_filename="audiobook.mp3",
                        script_entries=entries,
                        aligned_rows=aligned_rows,
                        source_normalizations=trailing_20,
                    )

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

    def test_duplicate_record_cannot_authorize_second_identical_collapse(self):
        # L2: two identical records authorize one distinct change, not two
        # occurrences. Only a forged manifest can contain the duplicate.
        hello20 = " ".join(["Hello"] * 20)
        body = f"<p>“{hello20}?”</p><p>“{hello20}?”</p>"
        epub = _make_epub([body])
        _extracted, _normalized, changes = _extract_and_normalize(epub)
        self.assertEqual(2, len(changes))
        entries, _ = _make_entries_from_texts(["Hello Hello Hello…?", "Hello Hello Hello…?"])
        with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
            build_epub_readaloud(
                retained_epub_bytes=epub,
                audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3",
                script_entries=entries,
                source_normalizations=[changes[0], changes[0]],
            )

    def test_shorter_run_record_does_not_block_a_longer_run_build(self):
        # L1 end-to-end: paragraph 1 restored to its raw 20 Hellos in the
        # script (the #2440 workflow), paragraph 2 is a 21-Hello collapse.
        hello20 = " ".join(["Hello"] * 20)
        hello21 = " ".join(["Hello"] * 21)
        epub = _make_epub([
            "<h1>Chapter One</h1>"
            f"<p>“{hello20}?”</p>"
            f"<p>“{hello21}?”</p>",
        ])
        _extracted, _normalized, changes = _extract_and_normalize(epub)
        self.assertEqual([20, 21], [len(change["before"].split()) for change in changes])
        entries, aligned = _make_entries_from_texts(
            ["Chapter One", f"“{hello20}?”", "Hello Hello Hello…?"])
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
                texts = [text for _id, text, _b, _e in _highlighted_texts(built, "ch0")]
                self.assertIn(f"“{hello20}?”", texts)
                self.assertIn(f"“{hello21}?”", texts)

    def test_caption_only_spine_document_is_skipped_in_both_modes(self):
        # L3/M1: a document whose only paragraph is an illustration caption
        # maps to no rows; it must be skipped, not turned into an empty SMIL.
        caption = "Illustration from Volume 1, coloring by Artist (source)"
        epub = _make_epub([
            "<p>The lantern flickered.</p>",
            f"<p>{caption}</p>",
            "<p>Nobody answered.</p>",
        ])
        _extracted, _normalized, changes = _extract_and_normalize(epub)
        self.assertEqual(1, len(changes))
        entries, aligned = _make_entries_from_texts(
            ["The lantern flickered.", "Nobody answered."])
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
                with zipfile.ZipFile(io.BytesIO(built)) as archive:
                    names = archive.namelist()
                    ch1 = archive.read("OEBPS/ch1.xhtml")
                self.assertNotIn("OEBPS/ch1.smil", names)
                self.assertNotIn(b"media-overlay", ch1)
                self.assertEqual(
                    ["The lantern flickered."],
                    [text for _id, text, _b, _e in _highlighted_texts(built, "ch0")])
                self.assertEqual(
                    ["Nobody answered."],
                    [text for _id, text, _b, _e in _highlighted_texts(built, "ch2")])

    def test_script_exhausted_caption_consumes_its_record(self):
        # L3/M2: a caption-only segment after the script runs out consumes its
        # record, so a second identical caption cannot reuse that one change.
        caption = "Illustration from Volume 1, coloring by Artist (source)"
        _normalized, changes = normalize_known_source_corruptions(caption)
        self.assertEqual(1, len(changes))
        segment = {"fragment": None, "text": caption}
        entry = {"index": 0, "prepared_text": "Spoken elsewhere.",
                 "start_frame": 0, "end_frame": 1000, "sample_rate": 24000}
        mapping, cursor, consumed = map_spine_to_chunks(
            [segment], [entry], start=1, source_normalizations=changes,
            initial_consumed_normalizations=set())
        self.assertEqual([], mapping)
        self.assertEqual(1, cursor)
        self.assertEqual({0}, consumed)

        with self.assertRaisesRegex(ValueError, "script exhausted"):
            map_spine_to_chunks([segment, dict(segment)], [entry], start=1,
                                source_normalizations=changes)

    def test_span_group_commits_consumed_change_on_exact_chunk_boundary(self):
        # L3/M3: group 1 closes exactly on a chunk boundary after mapping the
        # first collapse; the second identical collapse must not reuse it.
        hello20 = " ".join(["Hello"] * 20)
        _normalized, changes = normalize_extreme_phrase_repetitions(f"“{hello20}?”")
        self.assertEqual(1, len(changes))
        segments = [
            {"fragment": None, "text": f"“{hello20}?”"},
            {"fragment": None, "text": "The hall went still."},
            {"fragment": None, "text": f"“{hello20}?”"},
        ]
        entries, _ = _make_entries_from_texts([
            "Hello Hello Hello…? The hall went still.",
            "Hello Hello Hello…?",
        ])
        with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
            map_spine_to_chunks(
                segments, entries, allow_span=True,
                source_normalizations=changes)

        # Control: both distinct records map both collapses.
        second = dict(changes[0])
        second["offset"] = changes[0]["offset"] + 1000
        mapping, cursor, consumed = map_spine_to_chunks(
            segments, entries, allow_span=True,
            source_normalizations=[changes[0], second])
        self.assertEqual(2, cursor)
        self.assertEqual({0, 1}, consumed)
        self.assertEqual(3, len(mapping))

    def test_forged_repetition_after_is_refused(self):
        # L1/M17: a repetition record is accepted only when its ``after`` is
        # exactly what the normalizer would produce on its ``before``. Without
        # the contract this forged record would authorize "Hello…?" for a
        # 20-Hello run and the build would succeed.
        repeated = " ".join(["Hello"] * 20)
        _normalized, changes = normalize_extreme_phrase_repetitions(f"“{repeated}?”")
        forged = dict(changes[0])
        forged["after"] = "Hello…"
        source = f"“{repeated}?”"
        self.assertEqual((source, ()), map_recorded_source_normalizations(source, [forged]))

        epub = _make_epub([f"<h1>Chapter One</h1><p>{source}</p>"])
        entries, _ = _make_entries_from_texts(["Chapter One", "Hello…?"])
        with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
            build_epub_readaloud(
                retained_epub_bytes=epub,
                audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3",
                script_entries=entries,
                source_normalizations=[forged],
            )

    def test_span_group_does_not_reuse_records_consumed_earlier(self):
        # L1/M22: a group attempt must start from the records already consumed.
        # Otherwise one record authorizes both collapses when a chunk spans the
        # second collapse and the paragraph after it.
        hello20 = " ".join(["Hello"] * 20)
        epub = _make_epub([
            f"<h1>Chapter One</h1><p>“{hello20}?”</p>"
            f"<p>“{hello20}?”</p><p>Then silence.</p>",
        ])
        _extracted, _normalized, changes = _extract_and_normalize(epub)
        self.assertEqual(2, len(changes))
        entries, aligned = _make_entries_from_texts(
            ["Chapter One", "Hello Hello Hello…?", "Hello Hello Hello…? Then silence."])
        for aligned_rows in (None, aligned):
            with self.subTest(qwen=aligned_rows is not None):
                with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
                    build_epub_readaloud(
                        retained_epub_bytes=epub,
                        audio_bytes=b"\xff\xfbmp3",
                        audio_filename="audiobook.mp3",
                        script_entries=entries,
                        aligned_rows=aligned_rows,
                        source_normalizations=changes[:1],
                    )
                # Control: both recorded changes map both collapses.
                built, _ = build_epub_readaloud(
                    retained_epub_bytes=epub,
                    audio_bytes=b"\xff\xfbmp3",
                    audio_filename="audiobook.mp3",
                    script_entries=entries,
                    aligned_rows=aligned_rows,
                    source_normalizations=changes,
                )
                validate_epub_readaloud(built)
                texts = [text for _id, text, _b, _e in _highlighted_texts(built, "ch0")]
                self.assertEqual(2, sum(hello20 in text for text in texts))

    def test_caption_inside_span_group_consumes_its_record(self):
        # L1/M19: a caption-only member skipped while finishing a carried span
        # group must consume its record; otherwise the identical second caption
        # can be deleted with the same record and one recorded change authorizes
        # two deletions.
        caption = "Illustration from Volume 1, coloring by Artist (source)"
        epub = _make_epub([
            "<h1>Chapter One</h1><p>Alpha beta.</p>",
            f"<p>{caption}</p><p>Gamma delta.</p><p>Epsilon.</p>",
            f"<p>{caption}</p><p>Zeta.</p>",
        ])
        _extracted, _normalized, changes = _extract_and_normalize(epub)
        self.assertEqual(2, len(changes))
        entries, aligned = _make_entries_from_texts(
            ["Chapter One", "Alpha beta. Gamma delta.", "Epsilon.", "Zeta."])
        with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
            build_epub_readaloud(
                retained_epub_bytes=epub,
                audio_bytes=b"\xff\xfbmp3",
                audio_filename="audiobook.mp3",
                script_entries=entries,
                aligned_rows=aligned,
                source_normalizations=changes[:1],
            )
        # Control: both recorded deletions build and keep the visible text.
        built, _ = build_epub_readaloud(
            retained_epub_bytes=epub,
            audio_bytes=b"\xff\xfbmp3",
            audio_filename="audiobook.mp3",
            script_entries=entries,
            aligned_rows=aligned,
            source_normalizations=changes,
        )
        validate_epub_readaloud(built)
        self.assertEqual(
            ["Chapter One", "Alpha beta."],
            [text for _id, text, _b, _e in _highlighted_texts(built, "ch0")])
        self.assertEqual(
            ["Gamma delta.", "Epsilon."],
            [text for _id, text, _b, _e in _highlighted_texts(built, "ch1")])
        self.assertEqual(
            ["Zeta."],
            [text for _id, text, _b, _e in _highlighted_texts(built, "ch2")])

    def test_no_spine_text_guard_requires_consumed_progress(self):
        # NIT 4/M20: a spine with no readable words still fails closed, while a
        # document skipped only because it consumed a recorded change is not
        # "no spine text" at all.
        blank = [{"fragment": None, "text": "—"}]
        entry = {"index": 0, "prepared_text": "Spoken elsewhere.",
                 "start_frame": 0, "end_frame": 1000, "sample_rate": 24000}
        with self.assertRaisesRegex(ValueError, "No spine text could be synchronized"):
            map_spine_to_chunks(blank, [entry], start=1)
        caption = "Illustration from Volume 1, coloring by Artist (source)"
        _normalized, changes = normalize_known_source_corruptions(caption)
        mapping, cursor, consumed = map_spine_to_chunks(
            [{"fragment": None, "text": caption}], [entry], start=1,
            source_normalizations=changes)
        self.assertEqual([], mapping)
        self.assertEqual(1, cursor)
        self.assertEqual({0}, consumed)

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
        mapping, cursor, consumed = map_spine_to_chunks(
            segments, entries, source_normalizations=changes,
            initial_consumed_normalizations=consumed
        )
        self.assertEqual(2, cursor)
        self.assertEqual({0}, consumed)
        self.assertNotIn("spoken_text", mapping[0])
        self.assertEqual("Hello Hello Hello…?", mapping[1]["spoken_text"])


def _write_saved_book_with_records(scripts_dir, changes, entries, *, name="book"):
    """Write a saved script and a companion .meta.json bound to its bytes."""
    script_path = Path(scripts_dir) / f"{name}.json"
    script_bytes = json.dumps(entries, ensure_ascii=False, indent=2).encode("utf-8")
    script_path.write_bytes(script_bytes)
    meta = {"book_id": f"book-{name}"}
    meta.update(readalong_export.get_saved_script_normalization_metadata(changes, script_bytes))
    (Path(scripts_dir) / f"{name}.meta.json").write_text(
        json.dumps(meta), encoding="utf-8")
    return script_path, script_bytes


class SavedScriptNormalizationWriterTests(unittest.TestCase):
    """Every writer that rewrites a saved script rebinds its .meta.json records."""

    def _read_repro_book(self):
        repeated = " ".join(["Hello"] * 20)
        body = (
            "<h1>Chapter One</h1>"
            "<p>The lantern flickered in the hall.</p>"
            f"<p>“{repeated}?”</p>"
            "<p>Nobody answered from the stairs.</p>"
        )
        epub = _make_epub([body])
        _extracted, normalized, changes = _extract_and_normalize(epub)
        chunks = _script_chunks_from_normalized(normalized)
        return repeated, epub, changes, [dict(row) for row in chunks]

    def _prepare_saved_book_export(self, root, script_path, entries):
        """Write the chunks, state, config and audio ``build_epub_export`` needs.

        The saved script is the selected source; the Editor rows are the
        normalized (collapsed) rows a loaded saved book renders.
        """
        (root / "annotated_script.json").write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "state.json").write_text(json.dumps(
            {"input_file_path": str(script_path), "active_book_id": "book-2440"}),
            encoding="utf-8")
        (root / "config.json").write_text(
            json.dumps({"generation": {}}), encoding="utf-8")
        manager = ProjectManager(str(root))
        chunks = manager.load_chunks()
        audio_dir = root / "audio"
        audio_dir.mkdir(exist_ok=True)
        for idx, chunk in enumerate(chunks):
            wav_path = audio_dir / f"chunk_{idx}.wav"
            AudioSegment.silent(duration=500, frame_rate=24000).export(
                str(wav_path), format="wav").close()
            chunk["audio_path"] = str(wav_path)
            chunk["status"] = "completed"
        manager.save_chunks(chunks)
        return manager

    def _build_saved_book_export(self, manager, root, script_path, epub):
        # Only the saved-book retained-EPUB walk and the published-selection
        # gate are stubbed; the real export still reads and validates the
        # companion, assembles audio, maps rows and writes the EPUB.
        with patch.object(readalong_export, "_locate_retained_epub",
                          lambda *args, **kwargs: (epub, str(script_path))), \
             patch("script_selection_binding.require_published_selection",
                   lambda *args, **kwargs: None), \
             patch.object(core, "UPLOADS_DIR", str(root / "uploads")):
            return readalong_export.build_epub_export(manager, lambda: False)

    def test_saved_repair_refreshes_the_digest_and_still_builds_readaloud(self):
        from routers import scripts_library

        repeated, epub, changes, entries = self._read_repro_book()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            script_path, _old_bytes = _write_saved_book_with_records(
                scripts_dir, changes, entries)
            # Reordered keys are a real rewrite with unchanged spoken content.
            repaired = [dict(reversed(list(row.items()))) for row in entries]
            with patch.object(scripts_library, "DATA_DIR", str(root)), \
                 patch.object(scripts_library, "SCRIPTS_DIR", str(scripts_dir)):
                scripts_library._publish_saved_repair(str(script_path), repaired)
                self.assertEqual(
                    changes,
                    readalong_export.get_recorded_source_normalizations(None, str(script_path)))
                new_bytes = script_path.read_bytes()
                meta = json.loads((scripts_dir / "book.meta.json").read_text(encoding="utf-8"))
                self.assertEqual(hashlib.sha256(new_bytes).hexdigest(),
                                 meta["source_normalizations_script_sha256"])
                self.assertEqual("book-book", meta["book_id"])

                entries_from_audio, _aligned = _make_entries_from_texts(
                    [row["text"] for row in entries])
                built, _ = build_epub_readaloud(
                    retained_epub_bytes=epub,
                    audio_bytes=b"\xff\xfbmp3",
                    audio_filename="audiobook.mp3",
                    script_entries=entries_from_audio,
                    source_normalizations=readalong_export.get_recorded_source_normalizations(
                        None, str(script_path)),
                )
                validate_epub_readaloud(built)
                self.assertTrue(any(text == f"“{repeated}?”"
                                    for _id, text, _b, _e in _highlighted_texts(built, "ch0")))

                # A hand edit (not a repair path) leaves the digest stale and
                # is refused, so records are dropped and the build fails closed.
                script_path.write_bytes(json.dumps(repaired, ensure_ascii=False, indent=4).encode())
                with self.assertLogs("readalong_export", level="WARNING") as captured:
                    self.assertEqual([], readalong_export.get_recorded_source_normalizations(
                        None, str(script_path)))
                self.assertIn("different saved script", "\n".join(captured.output))
                with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
                    build_epub_readaloud(
                        retained_epub_bytes=epub,
                        audio_bytes=b"\xff\xfbmp3",
                        audio_filename="audiobook.mp3",
                        script_entries=entries_from_audio,
                    )

                # A companion that cannot be parsed is left unchanged; it keeps
                # failing closed at read time and does not block the rewrite.
                (scripts_dir / "book.meta.json").write_text("{not json", encoding="utf-8")
                with self.assertLogs("readalong_export", level="WARNING") as captured:
                    self.assertEqual([], readalong_export.get_recorded_source_normalizations(
                        None, str(script_path)))
                self.assertIn("not valid JSON", "\n".join(captured.output))
                with self.assertLogs("readalong_export", level="WARNING") as captured:
                    scripts_library._publish_saved_repair(
                        str(script_path), [dict(reversed(list(row.items()))) for row in entries])
                self.assertIn("cannot be refreshed", "\n".join(captured.output))
                self.assertEqual("{not json",
                                 (scripts_dir / "book.meta.json").read_text(encoding="utf-8"))

    def test_saved_repair_after_a_hand_edit_drops_the_records_permanently(self):
        # R6: the refresh must read the records through the validated saved-meta
        # path. Trusting the companion's ``source_normalizations`` directly
        # would re-bind records whose digest the hand edit invalidated.
        from routers import scripts_library

        _repeated, epub, changes, entries = self._read_repro_book()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            script_path, saved_bytes = _write_saved_book_with_records(
                scripts_dir, changes, entries)
            # Formatting-only hand edit: the spoken content is unchanged, so a
            # laundered digest would still authorize the collapse.
            script_path.write_bytes(
                json.dumps(entries, ensure_ascii=False, indent=4).encode("utf-8"))
            # Reordered keys make the repair write bytes other than
            # ``saved_bytes``, so restoring ``saved_bytes`` at the end is a real
            # rewrite rather than a no-op.
            repaired = [dict(reversed(list(row.items()))) for row in entries]
            with patch.object(scripts_library, "DATA_DIR", str(root)), \
                 patch.object(scripts_library, "SCRIPTS_DIR", str(scripts_dir)), \
                 self.assertLogs("readalong_export", level="WARNING") as captured:
                scripts_library._publish_saved_repair(str(script_path), repaired)
            self.assertIn("different saved script", "\n".join(captured.output))
            self.assertNotEqual(saved_bytes, script_path.read_bytes())
            self.assertEqual(
                [],
                readalong_export.get_recorded_source_normalizations(None, str(script_path)))
            meta = json.loads((scripts_dir / "book.meta.json").read_text(encoding="utf-8"))
            self.assertNotIn("source_normalizations", meta)
            self.assertNotIn("source_normalizations_script_sha256", meta)
            self.assertEqual("book-book", meta["book_id"])
            entries_from_audio, _aligned = _make_entries_from_texts(
                [row["text"] for row in entries])
            with self.assertRaisesRegex(ValueError, "No unambiguous synchronization"):
                build_epub_readaloud(
                    retained_epub_bytes=epub,
                    audio_bytes=b"\xff\xfbmp3",
                    audio_filename="audiobook.mp3",
                    script_entries=entries_from_audio,
                    source_normalizations=readalong_export.get_recorded_source_normalizations(
                        None, str(script_path)),
                )
            # Restoring the exact bytes the records were originally bound to
            # does not bring them back: only re-saving from an active
            # generation records them again.
            script_path.write_bytes(saved_bytes)
            self.assertEqual(saved_bytes, script_path.read_bytes())
            self.assertEqual(
                [],
                readalong_export.get_recorded_source_normalizations(None, str(script_path)))

    def test_saved_repair_proceeds_when_the_companion_has_an_oversized_integer(self):
        # An integer longer than CPython's 4,300-digit conversion limit makes
        # json.loads raise a plain ValueError, not JSONDecodeError. The read must
        # treat that companion as unreadable: drop the records, log, and let the
        # repair proceed as on main instead of escaping as an unhandled error.
        from routers import scripts_library

        _repeated, _epub, changes, entries = self._read_repro_book()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            script_path, saved_bytes = _write_saved_book_with_records(
                scripts_dir, changes, entries)
            meta_path = scripts_dir / "book.meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            oversized = (json.dumps(meta)[:-1]
                         + ', "legacy_counter": 1' + "1" * 4400 + "}")
            meta_path.write_text(oversized, encoding="utf-8")
            repaired = [dict(reversed(list(row.items()))) for row in entries]
            with patch.object(scripts_library, "DATA_DIR", str(root)), \
                 patch.object(scripts_library, "SCRIPTS_DIR", str(scripts_dir)), \
                 self.assertLogs("readalong_export", level="WARNING") as captured:
                scripts_library._publish_saved_repair(str(script_path), repaired)
            self.assertIn("is not valid JSON (ValueError)", "\n".join(captured.output))
            self.assertEqual(
                json.dumps(repaired, ensure_ascii=False, indent=2).encode("utf-8"),
                script_path.read_bytes())
            self.assertNotEqual(saved_bytes, script_path.read_bytes())
            # The unreadable companion is untouched, so it keeps failing closed.
            self.assertEqual(oversized, meta_path.read_text(encoding="utf-8"))
            with self.assertLogs("readalong_export", level="WARNING") as captured:
                self.assertEqual(
                    [],
                    readalong_export.get_recorded_source_normalizations(
                        None, str(script_path)))
            self.assertIn("is not valid JSON (ValueError)", "\n".join(captured.output))

    def test_saved_meta_with_oversized_integer_does_not_block_build_epub_export(self):
        # The real export reads the saved companion before it builds
        # (readalong_export.py:1918). An oversized integer used to escape that
        # read as an unhandled ValueError, failing a book main exports
        # unchanged. The last assertions verify the build published a valid
        # read-aloud EPUB and its receipt, not merely that a file was untouched.
        body = (
            "<h1>Chapter One</h1>"
            "<p>The lantern flickered in the hall.</p>"
            "<p>Nobody answered from the stairs.</p>"
        )
        epub = _make_epub([body])
        _extracted, normalized, changes = _extract_and_normalize(epub)
        self.assertEqual([], changes)
        entries = _script_chunks_from_normalized(normalized)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            script_path, saved_bytes = _write_saved_book_with_records(
                scripts_dir, [], entries)
            (scripts_dir / "book.meta.json").write_text(
                '{"book_id": "book-book", "legacy_counter": 1' + "1" * 4400 + "}",
                encoding="utf-8")
            manager = self._prepare_saved_book_export(root, script_path, entries)
            with self.assertLogs("readalong_export", level="WARNING") as captured:
                ok, message = self._build_saved_book_export(
                    manager, root, script_path, epub)
            self.assertIn("is not valid JSON (ValueError)", "\n".join(captured.output))
            # Same as main: no readable records, and the ordinary book builds.
            self.assertTrue(ok, message)
            built_bytes = (root / "readaloud.epub").read_bytes()
            self.assertTrue(built_bytes)
            validate_epub_readaloud(built_bytes)
            self.assertTrue((root / "readaloud.receipt.json").is_file())
            self.assertEqual(saved_bytes, script_path.read_bytes())

    def test_saved_meta_with_malformed_record_fields_fails_closed_in_build_epub_export(self):
        # L1 export path: the gate's digest-valid companion with a list/dict
        # ``offset`` or ``rule`` (and a bool offset) must not raise TypeError
        # out of the real export; the build must refuse with main's exact
        # synchronization error and publish nothing.
        repeated, epub, changes, entries = self._read_repro_book()
        valid = changes[0]
        for forged in _malformed_record_variants(valid):
            with self.subTest(fields=sorted(forged)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scripts_dir = root / "scripts"
                scripts_dir.mkdir()
                script_path, _saved = _write_saved_book_with_records(
                    scripts_dir, [forged], entries)
                manager = self._prepare_saved_book_export(root, script_path, entries)
                ok, message = self._build_saved_book_export(
                    manager, root, script_path, epub)
                self.assertFalse(ok, message)
                self.assertIn("No unambiguous synchronization", message)
                self.assertNotIn("unhashable type", message)
                self.assertFalse((root / "readaloud.epub").exists())
                # The same malformed entry beside the valid record still
                # authorizes the valid collapse and builds normally.
                _write_saved_book_with_records(
                    scripts_dir, [valid, forged], entries)
                ok, message = self._build_saved_book_export(
                    manager, root, script_path, epub)
                self.assertTrue(ok, message)
                built_bytes = (root / "readaloud.epub").read_bytes()
                validate_epub_readaloud(built_bytes)
                self.assertTrue(any(text == f"“{repeated}?”"
                                    for _id, text, _b, _e in _highlighted_texts(built_bytes)))

    def test_saved_repair_proceeds_when_the_companion_nests_past_the_decoder_limit(self):
        # L2: json.loads raises RecursionError past the C decoder's nesting
        # limit. The reader must report that as unreadable, not escape, so the
        # repair still proceeds and the companion keeps failing closed.
        from routers import scripts_library

        _repeated, _epub, changes, entries = self._read_repro_book()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            script_path, saved_bytes = _write_saved_book_with_records(
                scripts_dir, changes, entries)
            meta_path = scripts_dir / "book.meta.json"
            nested = ('{"book_id": "book-book", "x": '
                      + "[" * 100000 + "]" * 100000 + "}")
            meta_path.write_text(nested, encoding="utf-8")
            repaired = [dict(reversed(list(row.items()))) for row in entries]
            with patch.object(scripts_library, "DATA_DIR", str(root)), \
                 patch.object(scripts_library, "SCRIPTS_DIR", str(scripts_dir)), \
                 self.assertLogs("readalong_export", level="WARNING") as captured:
                scripts_library._publish_saved_repair(str(script_path), repaired)
            self.assertIn("is not valid JSON (RecursionError)", "\n".join(captured.output))
            self.assertEqual(
                json.dumps(repaired, ensure_ascii=False, indent=2).encode("utf-8"),
                script_path.read_bytes())
            self.assertNotEqual(saved_bytes, script_path.read_bytes())
            # The unreadable companion is untouched, so it keeps failing closed.
            self.assertEqual(nested, meta_path.read_text(encoding="utf-8"))
            with self.assertLogs("readalong_export", level="WARNING") as captured:
                self.assertEqual(
                    [],
                    readalong_export.get_recorded_source_normalizations(
                        None, str(script_path)))
            self.assertIn("is not valid JSON (RecursionError)", "\n".join(captured.output))

    def test_saved_repair_proceeds_when_records_have_malformed_field_types(self):
        # L1 repair path: a digest-valid companion can carry a record whose
        # ``offset`` or ``rule`` is a JSON list/object or a bool offset. The
        # repair must proceed (main rewrites the script) and the build must
        # refuse with main's error instead of raising TypeError.
        from routers import scripts_library

        repeated, epub, changes, entries = self._read_repro_book()
        valid = changes[0]
        entries_from_audio, _aligned = _make_entries_from_texts(
            [row["text"] for row in entries])
        for forged in _malformed_record_variants(valid):
            with self.subTest(fields=sorted(forged)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scripts_dir = root / "scripts"
                scripts_dir.mkdir()
                script_path, _old_bytes = _write_saved_book_with_records(
                    scripts_dir, [forged], entries)
                repaired = [dict(reversed(list(row.items()))) for row in entries]
                with patch.object(scripts_library, "DATA_DIR", str(root)), \
                     patch.object(scripts_library, "SCRIPTS_DIR", str(scripts_dir)):
                    scripts_library._publish_saved_repair(str(script_path), repaired)
                    # The repaired companion still binds the malformed record.
                    self.assertEqual(
                        [forged],
                        readalong_export.get_recorded_source_normalizations(
                            None, str(script_path)))
                    with self.assertRaisesRegex(
                            ValueError, "No unambiguous synchronization"):
                        build_epub_readaloud(
                            retained_epub_bytes=epub,
                            audio_bytes=b"\xff\xfbmp3",
                            audio_filename="audiobook.mp3",
                            script_entries=entries_from_audio,
                            source_normalizations=(
                                readalong_export.get_recorded_source_normalizations(
                                    None, str(script_path))),
                        )
                    built, _report = build_epub_readaloud(
                        retained_epub_bytes=epub,
                        audio_bytes=b"\xff\xfbmp3",
                        audio_filename="audiobook.mp3",
                        script_entries=entries_from_audio,
                        source_normalizations=[valid, forged],
                    )
                validate_epub_readaloud(built)
                self.assertTrue(any(text == f"“{repeated}?”"
                                    for _id, text, _b, _e in _highlighted_texts(built)))

    def test_speaker_repair_refreshes_the_digest(self):
        from routers import scripts_library

        repeated, _epub, changes, entries = self._read_repro_book()
        # Reordered keys are a real rewrite with unchanged spoken content.
        repaired = [dict(reversed(list(row.items()))) for row in entries]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            script_path, old_bytes = _write_saved_book_with_records(
                scripts_dir, changes, entries)
            request = SimpleNamespace(
                expected_sha256=hashlib.sha256(old_bytes).hexdigest(),
                selections=[SimpleNamespace(model_dump=lambda: {"index": 0})])
            with patch.object(scripts_library, "DATA_DIR", str(root)), \
                 patch.object(scripts_library, "SCRIPTS_DIR", str(scripts_dir)), \
                 patch.object(scripts_library, "_saved_mutation_admission",
                              lambda: contextlib.nullcontext()), \
                 patch.object(scripts_library, "_require_saved_book_idle", lambda: None), \
                 patch.object(scripts_library, "saved_script_is_active_source",
                              new=lambda *a, **k: False), \
                 patch.object(scripts_library, "_saved_repair_binding",
                              new=lambda *a, **k: None), \
                 patch.object(scripts_library, "apply_speaker_selections",
                              new=lambda current, selections: {
                                  "changes": [{"index": 0}], "entries": repaired}):
                result = scripts_library._apply_speaker_repair_sync("book", request)
            self.assertEqual("repaired", result["status"])
            self.assertEqual(
                changes,
                readalong_export.get_recorded_source_normalizations(None, str(script_path)))
            meta = json.loads((scripts_dir / "book.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(script_path.read_bytes()).hexdigest(),
                             meta["source_normalizations_script_sha256"])

    def test_saved_review_publisher_refreshes_the_digest(self):
        from script_selection_binding import publish_saved_script_edit

        repeated, _epub, changes, entries = self._read_repro_book()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            script_path, old_bytes = _write_saved_book_with_records(
                scripts_dir, changes, entries)
            new_bytes = json.dumps(entries, ensure_ascii=False, indent=3).encode("utf-8")
            with patch("book_state_transaction.reuse_book_state",
                       lambda *a, **k: contextlib.nullcontext()), \
                 patch("book_state_transaction.ensure_saved_book_state",
                       lambda *a, **k: contextlib.nullcontext()):
                publish_saved_script_edit(
                    str(root), str(scripts_dir), str(script_path), old_bytes, new_bytes)
            self.assertEqual(new_bytes, script_path.read_bytes())
            meta = json.loads((scripts_dir / "book.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(new_bytes).hexdigest(),
                             meta["source_normalizations_script_sha256"])
            self.assertEqual(
                changes,
                readalong_export.get_recorded_source_normalizations(None, str(script_path)))

    def test_saved_repair_proceeds_when_the_companion_holds_a_lone_surrogate_escape(self):
        # L1 round 6: a companion that parses but holds a hand-edited lone
        # surrogate (``"title": "A\\ud800B"``) used to raise UnicodeEncodeError
        # from the refresh's own ``ensure_ascii=False`` write, so the repair
        # failed where main completes it. The refresh now serializes the way
        # Save writes the companion (ASCII escapes), so the surrogate round-trips
        # and the repair applies with and without a stale digest.
        from routers import scripts_library

        _repeated, _epub, changes, entries = self._read_repro_book()
        repaired = [dict(reversed(list(row.items()))) for row in entries]
        for label, stale_digest in (("records", False), ("stale-digest", True)):
            with self.subTest(companion=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scripts_dir = root / "scripts"
                scripts_dir.mkdir()
                script_path, saved_bytes = _write_saved_book_with_records(
                    scripts_dir, changes, entries)
                meta_path = scripts_dir / "book.meta.json"
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if stale_digest:
                    meta["source_normalizations_script_sha256"] = "0" * 64
                meta["title"] = "A\ud800B"
                meta_path.write_text(json.dumps(meta), encoding="utf-8")
                with patch.object(scripts_library, "DATA_DIR", str(root)), \
                     patch.object(scripts_library, "SCRIPTS_DIR", str(scripts_dir)):
                    scripts_library._publish_saved_repair(str(script_path), repaired)
                self.assertEqual(
                    json.dumps(repaired, ensure_ascii=False, indent=2).encode("utf-8"),
                    script_path.read_bytes())
                self.assertNotEqual(saved_bytes, script_path.read_bytes())
                meta_text = meta_path.read_text(encoding="utf-8")
                self.assertIn("\\ud800", meta_text)
                rewritten = json.loads(meta_text)
                self.assertEqual("A\ud800B", rewritten["title"])
                if stale_digest:
                    # The stale digest is dropped, not laundered.
                    self.assertNotIn("source_normalizations", rewritten)
                    self.assertNotIn("source_normalizations_script_sha256", rewritten)
                    self.assertEqual(
                        [],
                        readalong_export.get_recorded_source_normalizations(
                            None, str(script_path)))
                else:
                    self.assertEqual(
                        changes,
                        readalong_export.get_recorded_source_normalizations(
                            None, str(script_path)))

    def test_saved_review_publisher_proceeds_when_the_companion_holds_a_lone_surrogate_escape(self):
        # L1 round 6, saved-review path: a completed review used to fail at
        # publish time on the same ``ensure_ascii=False`` write. The publisher
        # must complete as on main and keep the hand-edited field verbatim.
        from script_selection_binding import publish_saved_script_edit

        _repeated, _epub, changes, entries = self._read_repro_book()
        new_bytes = json.dumps(entries, ensure_ascii=False, indent=3).encode("utf-8")
        for label, stale_digest in (("records", False), ("stale-digest", True)):
            with self.subTest(companion=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scripts_dir = root / "scripts"
                scripts_dir.mkdir()
                script_path, old_bytes = _write_saved_book_with_records(
                    scripts_dir, changes, entries)
                meta_path = scripts_dir / "book.meta.json"
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if stale_digest:
                    meta["source_normalizations_script_sha256"] = "0" * 64
                meta["title"] = "A\ud800B"
                meta_path.write_text(json.dumps(meta), encoding="utf-8")
                with patch("book_state_transaction.reuse_book_state",
                           lambda *a, **k: contextlib.nullcontext()), \
                     patch("book_state_transaction.ensure_saved_book_state",
                           lambda *a, **k: contextlib.nullcontext()):
                    publish_saved_script_edit(
                        str(root), str(scripts_dir), str(script_path),
                        old_bytes, new_bytes)
                self.assertEqual(new_bytes, script_path.read_bytes())
                meta_text = meta_path.read_text(encoding="utf-8")
                self.assertIn("\\ud800", meta_text)
                rewritten = json.loads(meta_text)
                self.assertEqual("A\ud800B", rewritten["title"])
                if stale_digest:
                    self.assertNotIn("source_normalizations", rewritten)
                    self.assertNotIn("source_normalizations_script_sha256", rewritten)
                    self.assertEqual(
                        [],
                        readalong_export.get_recorded_source_normalizations(
                            None, str(script_path)))
                else:
                    self.assertEqual(hashlib.sha256(new_bytes).hexdigest(),
                                     rewritten["source_normalizations_script_sha256"])
                    self.assertEqual(
                        changes,
                        readalong_export.get_recorded_source_normalizations(
                            None, str(script_path)))


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
                readalong_export.get_recorded_source_normalizations(str(output), str(source)),
            )

    def test_get_recorded_source_normalizations_reads_bound_complete_sources_only(self):
        repeated = " ".join(["Hello"] * 20)
        _norm, changes = normalize_extreme_phrase_repetitions(repeated)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "book.txt"
            source_path.write_text(f"The lantern flickered.\n\n{repeated}?\n", encoding="utf-8")
            script_path = root / "annotated_script.json"
            script_path.write_text("[]", encoding="utf-8")

            # Incomplete three-pass manifest is ignored.
            tp_manifest = Path(tp.three_pass_manifest_path(str(script_path)))
            tp_manifest.write_text(json.dumps(
                _three_pass_manifest_doc(source_path, changes, status="incomplete")),
                encoding="utf-8")
            self.assertEqual([], readalong_export.get_recorded_source_normalizations(
                str(script_path), str(source_path)))

            # A complete manifest bound to a DIFFERENT source is refused.
            other_source = root / "other.txt"
            other_source.write_text("A completely different book.\n", encoding="utf-8")
            tp_manifest.write_text(json.dumps(_three_pass_manifest_doc(other_source, changes)),
                                   encoding="utf-8")
            self.assertEqual([], readalong_export.get_recorded_source_normalizations(
                str(script_path), str(source_path)))

            # The same manifest bound to this source is accepted.
            tp_manifest.write_text(json.dumps(_three_pass_manifest_doc(source_path, changes)),
                                   encoding="utf-8")
            self.assertEqual(changes, readalong_export.get_recorded_source_normalizations(
                str(script_path), str(source_path)))

            # A run that kept its removable front matter also binds: the
            # reader tries both preparation policies.
            front_matter_source = root / "front_matter_book.txt"
            front_matter_source.write_text(
                "Manifesto.\n\nA compiler note.\n\n"
                "Original Web Novel Chapter - Complete.\n"
                "Original Translation by Someone.\n"
                f"The lantern flickered.\n\n{repeated}?\n",
                encoding="utf-8")
            tp_manifest.write_text(json.dumps(_three_pass_manifest_doc(
                front_matter_source, changes, strip_front_matter=False)),
                encoding="utf-8")
            self.assertEqual(changes, readalong_export.get_recorded_source_normalizations(
                str(script_path), str(front_matter_source)))

            # Complete single-pass quality manifest is read when three-pass is absent.
            tp_manifest.unlink()
            gq_manifest = Path(get_generation_quality_path(str(script_path)))
            gq_manifest.write_text(json.dumps(_quality_manifest_doc(source_path, changes)),
                                   encoding="utf-8")
            self.assertEqual(changes, readalong_export.get_recorded_source_normalizations(
                str(script_path), str(source_path)))

            # A quality manifest bound to another source is refused.
            gq_manifest.write_text(json.dumps(_quality_manifest_doc(other_source, changes)),
                                   encoding="utf-8")
            self.assertEqual([], readalong_export.get_recorded_source_normalizations(
                str(script_path), str(source_path)))
            gq_manifest.unlink()

            # Saved script .meta.json is read when its digest binds the records
            # to the exact saved script bytes.
            saved = root / "saved_book.json"
            saved.write_text("[]", encoding="utf-8")
            meta_path = root / "saved_book.meta.json"
            meta_path.write_text(json.dumps({
                "book_id": "b1",
                "source_normalizations": changes,
                "source_normalizations_script_sha256": hashlib.sha256(b"[]").hexdigest(),
            }), encoding="utf-8")
            self.assertEqual(
                changes,
                readalong_export.get_recorded_source_normalizations(str(script_path), str(saved)),
            )

            # A meta copied over another saved script (stale digest) is refused.
            saved.write_text(json.dumps([{"speaker": "NARRATOR"}]), encoding="utf-8")
            self.assertEqual([], readalong_export.get_recorded_source_normalizations(
                str(script_path), str(saved)))

            # Records without any script binding are refused.
            meta_path.write_text(json.dumps({
                "book_id": "b1", "source_normalizations": changes,
            }), encoding="utf-8")
            self.assertEqual([], readalong_export.get_recorded_source_normalizations(
                str(script_path), str(saved)))

    def test_recorded_normalization_bindings_reject_malformed_metadata(self):
        repeated = " ".join(["Hello"] * 20)
        _norm, changes = normalize_extreme_phrase_repetitions(repeated)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "book.txt"
            source_path.write_text(f"The lantern flickered.\n\n{repeated}?\n", encoding="utf-8")
            script_path = root / "annotated_script.json"
            script_path.write_text("[]", encoding="utf-8")
            tp_manifest = Path(tp.three_pass_manifest_path(str(script_path)))
            good = _three_pass_manifest_doc(source_path, changes)

            three_pass_variants = {
                "no completion": {**good, "completion": None},
                "bad script digest": {**good, "completion": {**good["completion"],
                                                            "script_sha256": "not-a-digest"}},
                "no fingerprint": {**good, "fingerprint": None},
                "bad fingerprint": {**good, "fingerprint": {"source_sha256": "nope"}},
            }
            for label, doc in three_pass_variants.items():
                with self.subTest(three_pass=label):
                    tp_manifest.write_text(json.dumps(doc), encoding="utf-8")
                    self.assertEqual([], readalong_export.get_recorded_source_normalizations(
                        str(script_path), str(source_path)))
            tp_manifest.unlink()

            gq_manifest = Path(get_generation_quality_path(str(script_path)))
            quality = _quality_manifest_doc(source_path, changes)
            quality_variants = {
                "no artifact": {**quality, "completion_artifact": None},
                "bad version": {**quality, "completion_artifact": {
                    **quality["completion_artifact"], "version": 2}},
                "bad output digest": {**quality, "completion_artifact": {
                    **quality["completion_artifact"], "output_sha256": "nope"}},
                "bad input digest": {**quality, "completion_artifact": {
                    **quality["completion_artifact"], "input_sha256": "nope"}},
                "wrong status": {**quality, "status": "verified"},
            }
            for label, doc in quality_variants.items():
                with self.subTest(quality=label):
                    gq_manifest.write_text(json.dumps(doc), encoding="utf-8")
                    self.assertEqual([], readalong_export.get_recorded_source_normalizations(
                        str(script_path), str(source_path)))
            gq_manifest.unlink()

            saved = root / "saved_book.json"
            saved.write_text("[]", encoding="utf-8")
            meta_path = root / "saved_book.meta.json"
            meta_path.write_text("{not json", encoding="utf-8")
            with self.assertLogs("readalong_export", level="WARNING") as captured:
                self.assertEqual([], readalong_export.get_recorded_source_normalizations(
                    str(script_path), str(saved)))
            self.assertIn("not valid JSON", "\n".join(captured.output))

            # A tampered/stale manifest and the unreadable one above both speak
            # in the log instead of reading as "this book recorded nothing".
            tp_manifest.write_text(json.dumps({
                **_three_pass_manifest_doc(source_path, changes),
                "fingerprint": {"source_sha256": "f" * 64},
            }), encoding="utf-8")
            with self.assertLogs("readalong_export", level="WARNING") as captured:
                self.assertEqual([], readalong_export.get_recorded_source_normalizations(
                    str(script_path), str(source_path)))
            self.assertIn("different source", "\n".join(captured.output))

    def test_running_generation_normalizations_come_from_the_current_source(self):
        repeated = " ".join(["Hello"] * 20)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.txt"
            source.write_text(f"The lantern flickered.\n\n{repeated}?\n", encoding="utf-8")
            changes = readalong_export.get_running_generation_source_normalizations(str(source))
            self.assertEqual(1, len(changes))
            self.assertEqual("extreme_phrase_repetition", changes[0]["rule"])

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

                # With completed three-pass manifest carrying recorded
                # normalizations bound to this source -> succeeds.
                Path(tp.three_pass_manifest_path(str(script_path))).write_text(
                    json.dumps(_three_pass_manifest_doc(source_txt, changes)),
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
            source_path = root / "book.txt"
            source_path.write_text(f"The lantern flickered.\n\n{repeated}?\n", encoding="utf-8")
            (root / "state.json").write_text(
                json.dumps({"active_book_id": "book-2440", "input_file_path": str(source_path)}),
                encoding="utf-8",
            )
            manifest_path = Path(tp.three_pass_manifest_path(str(script_path)))
            manifest_path.write_text(
                json.dumps(_three_pass_manifest_doc(source_path, changes)),
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
                saved_script_bytes = (scripts_dir / "normalized_book.json").read_bytes()
                self.assertEqual(
                    hashlib.sha256(saved_script_bytes).hexdigest(),
                    saved_meta["source_normalizations_script_sha256"])
                self.assertEqual(
                    changes,
                    readalong_export.get_recorded_source_normalizations(
                        str(script_path), str(scripts_dir / "normalized_book.json")))

                # Without recorded normalizations, .meta.json omits both keys.
                manifest_path.unlink()
                req_plain = SimpleNamespace(name="plain_book", expected_book_token=None)
                scripts_library._save_script_sync(req_plain)
                plain_meta = json.loads((scripts_dir / "plain_book.meta.json").read_text(encoding="utf-8"))
                self.assertNotIn("source_normalizations", plain_meta)
                self.assertNotIn("source_normalizations_script_sha256", plain_meta)

    def test_save_passes_the_books_front_matter_policy_to_the_record_read(self):
        # R12: the binding prepares the source under the book's own policy
        # first; Save must hand that policy to the record read.
        from routers import scripts_library

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            script_path = root / "annotated_script.json"
            script_path.write_text(
                json.dumps([{"speaker": "NARRATOR", "text": "Hello.", "instruct": ""}]),
                encoding="utf-8",
            )
            source_path = root / "book.txt"
            source_path.write_text("Hello.\n", encoding="utf-8")
            (root / "state.json").write_text(
                json.dumps({"active_book_id": "book-n2",
                            "input_file_path": str(source_path)}),
                encoding="utf-8",
            )
            calls = []
            original = readalong_export.get_recorded_source_normalizations

            def spy(*args, **kwargs):
                calls.append(kwargs.get("strip_front_matter"))
                return original(*args, **kwargs)

            req = SimpleNamespace(name="policy_book", expected_book_token=None)
            with patch.object(scripts_library, "DATA_DIR", str(root)), \
                 patch.object(scripts_library, "SCRIPTS_DIR", str(scripts_dir)), \
                 patch.object(scripts_library, "SCRIPT_PATH", str(script_path)), \
                 patch.object(scripts_library, "VOICE_CONFIG_PATH",
                              str(root / "voice_config.json")), \
                 patch.object(scripts_library, "_saved_book_strip_front_matter",
                              new=lambda *args, **kwargs: False), \
                 patch.object(readalong_export, "get_recorded_source_normalizations", spy):
                result = scripts_library._save_script_sync(req)
            self.assertEqual("saved", result["status"])
            self.assertEqual([False], calls)
            meta = json.loads((scripts_dir / "policy_book.meta.json").read_text(
                encoding="utf-8"))
            self.assertIs(False, meta["strip_front_matter"])

    def test_running_snapshot_meta_persists_recorded_normalizations(self):
        from routers import script as script_router
        from routers import scripts_library

        repeated = " ".join(["Hello"] * 20)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            source_path = data_dir / "book.txt"
            source_bytes = f"The lantern flickered.\n\n{repeated}?\n".encode("utf-8")
            source_path.write_bytes(source_bytes)
            source_sha256 = hashlib.sha256(source_bytes).hexdigest()
            state = {"input_file_path": str(source_path),
                     "script_generation_run_id": "run-1",
                     "script_generation_selection": {"book_token": "book-token"}}
            checkpoint = {"annotated": [{"speaker": "NARRATOR", "text": "x", "instruct": ""}],
                          "named": [{"name": "NARRATOR"}],
                          "segmented": [{"text": "x"}], "chunks_done": 1, "stage": "instruct"}

            def use(stack, target, name, value=None, *, new=None):
                return stack.enter_context(
                    patch.object(target, name, new if value is None else value))

            nullcontext = contextlib.nullcontext

            def snapshot(captured):
                replacements = {}
                state["script_generation_selection"] = captured
                with contextlib.ExitStack() as stack:
                    use(stack, script_router, "SCRIPTS_DIR", str(scripts_dir))
                    use(stack, script_router, "DATA_DIR", str(data_dir))
                    use(stack, script_router, "VOICE_CONFIG_PATH", str(data_dir / "voice.json"))
                    use(stack, scripts_library, "SCRIPTS_DIR", str(scripts_dir))
                    use(stack, scripts_library, "DATA_DIR", str(data_dir))
                    use(stack, core, "SCRIPTS_DIR", str(scripts_dir))
                    use(stack, script_router, "process_state",
                        {"script": {"running": True, "cancel": False}})
                    use(stack, script_router, "ensure_saved_book_destination_available",
                        lambda dest: nullcontext())
                    use(stack, script_router, "ensure_book_state",
                        lambda *args, **kwargs: nullcontext())
                    use(stack, script_router, "ensure_saved_book_state",
                        lambda *args, **kwargs: nullcontext())
                    use(stack, script_router, "file_lock", lambda path: nullcontext())
                    use(stack, script_router, "safe_load_json", new=lambda *a, **k: state)
                    stack.enter_context(patch(
                        "script_selection_binding.capture_generation_selection",
                        return_value=captured))
                    use(stack, script_router, "get_current_generation_checkpoint",
                        new=lambda *a, **k: checkpoint)
                    use(stack, core, "_gpu_lock", nullcontext())
                    use(stack, core, "_task_claims",
                        {"script": {"phase": "started", "id": "run-1"}})
                    use(stack, script_router, "get_active_book_id", new=lambda: "book-2440")
                    use(stack, scripts_library, "_capture_sound_companions",
                        new=lambda *a, **k: (None, None))
                    use(stack, script_router, "get_saved_publication_source_bytes",
                        new=lambda *a, **k: None)
                    use(stack, script_router, "apply_book_state_locked",
                        new=lambda directory, files, removals: replacements.update(files))
                    result = script_router._snapshot_script_sync(SimpleNamespace(name="snap"))
                return result, replacements

            result, replacements = snapshot(
                {"book_token": "book-token", "source_path": str(source_path),
                 "source_sha256": source_sha256})
            self.assertEqual("saved", result["status"])
            meta_bytes = replacements["snap.meta.json"]
            meta = json.loads(meta_bytes)
            recorded = readalong_export.get_running_generation_source_normalizations(
                str(source_path))
            self.assertEqual(recorded, meta["source_normalizations"])
            self.assertEqual(1, len(recorded))
            script_bytes = replacements["snap.json"]
            self.assertEqual(hashlib.sha256(script_bytes).hexdigest(),
                             meta["source_normalizations_script_sha256"])

            # The route's captured source digest fences the recompute: bytes
            # that changed after the capture are refused, not recorded.
            with self.assertRaises(ValueError):
                readalong_export.get_running_generation_source_normalizations(
                    str(source_path), expected_source_sha256="0" * 64)
            with self.assertLogs("AlexandriaUI", level="WARNING") as captured:
                _result, stale = snapshot(
                    {"book_token": "book-token", "source_path": str(source_path),
                     "source_sha256": "0" * 64})
            self.assertIn("Snapshot cannot read recorded source normalizations",
                          "\n".join(captured.output))
            stale_meta = json.loads(stale["snap.meta.json"])
            self.assertNotIn("source_normalizations", stale_meta)
            self.assertNotIn("source_normalizations_script_sha256", stale_meta)

    def test_three_pass_binding_stops_at_the_first_matching_policy(self):
        # NIT 5: a book without removable front matter prepares once per read.
        repeated = " ".join(["Hello"] * 20)
        _norm, changes = normalize_extreme_phrase_repetitions(repeated)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "book.txt"
            source_path.write_text(f"The lantern flickered.\n\n{repeated}?\n", encoding="utf-8")
            script_path = root / "annotated_script.json"
            script_path.write_text("[]", encoding="utf-8")
            Path(tp.three_pass_manifest_path(str(script_path))).write_text(
                json.dumps(_three_pass_manifest_doc(source_path, changes)), encoding="utf-8")
            calls = []
            original = tp.get_prepared_source

            def counted(path, *args, **kwargs):
                calls.append(kwargs.get("strip_front_matter"))
                return original(path, *args, **kwargs)

            with patch.object(tp, "get_prepared_source", counted):
                self.assertEqual(changes,
                                 readalong_export.get_recorded_source_normalizations(
                                     str(script_path), str(source_path),
                                     strip_front_matter=False))
            self.assertEqual([False], calls)


if __name__ == "__main__":
    unittest.main()
