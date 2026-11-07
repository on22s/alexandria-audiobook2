"""R4 bounded audio/frame/publication repair (#1720/#1779).

Production-builder tests with ProjectManager persistent rows, the real
audio loader, real PCM assembly and the real ffmpeg encoder. Faults are
injected only at OS boundaries (os.replace, cancellation, staleness).
"""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from pydub import AudioSegment

from project import ProjectManager
from readalong_export import (
    build_epub_export,
    build_sidecar_export,
    get_epub_receipt_output_path,
    get_validated_epub_receipt,
    get_sidecar_output_path,
)
from readalong_sidecar import (
    build_readalong_sidecar,
    get_validated_sidecar,
)


def _clip(tmp, name, data, width=2, rate=24000, channels=1):
    segment = AudioSegment(data=data, sample_width=width, frame_rate=rate, channels=channels)
    with segment.export(str(Path(tmp) / name), format="wav"):
        pass
    return segment


def _setup_book(tmp, specs, *, texts=None, pause_afters=None, state=True):
    """specs: [(frames, rate, channels, width)] per row; writes real WAVs + rows."""
    chunks = []
    for i, (frames, rate, channels, width) in enumerate(specs):
        if width == 1:
            data = b"\xc0" * (frames * channels)
        elif width == 4:
            data = b"\x00\x40\x00\x00" * (frames * channels)
        else:
            # Alternating tone that is never silent under the -45 dB edge rule.
            if channels == 2:
                data = b"\x00\x20\x00\x40" * frames
            else:
                data = b"\x00\x20" * frames
        name = f"clip{i}.wav"
        _clip(tmp, name, data, width=width, rate=rate, channels=channels)
        text = (texts[i] if texts else f"Line {i}.")
        row = {"id": i, "uid": f"u{i}", "speaker": "Narrator", "text": text,
               "audio_path": name, "audio_revision": f"r{i}", "status": "done"}
        if pause_afters and pause_afters[i] is not None:
            row["pause_after"] = pause_afters[i]
        chunks.append(row)
    (Path(tmp) / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
    book = Path(tmp) / "book.txt"
    book.write_bytes((" ".join(c["text"] for c in chunks)).encode("utf-8"))
    if state:
        (Path(tmp) / "state.json").write_text(
            json.dumps({"input_file_path": str(book)}), encoding="utf-8")
    (Path(tmp) / "annotated_script.json").write_text("[]", encoding="utf-8")
    return chunks


def _independent_plan(tmp, *, pause_ms=250, same_speaker_pause_ms=250):
    """Recompute the shared plan from the same trimmed sources."""
    from audio_pcm_producer import PCMSourceStore
    from audio_streaming import get_combined_source_plan, save_trimmed_pcm_sources
    from tts import iter_timeline

    manager = ProjectManager(tmp)
    pause_ms, same_speaker_pause_ms = manager._load_pause_defaults()
    with PCMSourceStore(tmp, lambda: None) as store:
        pairs, skipped = manager._load_chunks_with_audio(segment_transform=store.save_segment)
        assert skipped == 0, skipped
        decoded = ((c, store.load_segment(t)) for c, t in pairs)
        timeline = []
        for chunk, segment, _start in iter_timeline(
                decoded, pause_ms, same_speaker_pause_ms, cancel_check=None):
            timeline.append((chunk, store.save_segment(segment)))
        segments = [s for _c, s in timeline]
        speakers = [c["speaker"] for c, _s in timeline]
        overrides = [c.get("pause_after") for c, _s in timeline]
        trimmed = save_trimmed_pcm_sources(segments, None, store)
        plan = get_combined_source_plan(
            trimmed, speakers, pause_ms, same_speaker_pause_ms, overrides, None)
        return plan, timeline


def _sidecar_doc(tmp):
    return json.loads((Path(tmp) / "readalong.sidecar.json").read_text(encoding="utf-8"))


def _make_source_epub_nested():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        archive.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="id">urn:test:1</dc:identifier>'
            '<dc:title>Synthetic Title</dc:title><dc:creator>Test Author</dc:creator>'
            '<dc:language>en</dc:language></metadata>'
            '<manifest>'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>'
            '</manifest>'
            '<spine><itemref idref="c1"/></spine></package>')
        archive.writestr(
            "OEBPS/nav.xhtml",
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Nav</title></head><body>'
            '<nav epub:type="toc"><ol><li><a href="ch1.xhtml">Chapter One</a></li></ol></nav></body></html>')
        archive.writestr(
            "OEBPS/ch1.xhtml",
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            '<head><title>Chapter One</title></head><body>'
            '<section><div><h1>Chapter One</h1><p>Hello synthetic world.</p>'
            '<p>Second paragraph here.</p></div></section>'
            '<blockquote><p>Quoted line here.</p></blockquote>'
            '<ul><li>First list item.</li></ul>'
            '</body></html>')
        archive.writestr("OEBPS/cover.jpg", b"\xff\xd8\xff\xe0\xff\xd9")
    return buffer.getvalue()


def _setup_epub_book(tmp, texts):
    chunks = []
    for i, text in enumerate(texts):
        _clip(tmp, f"clip{i}.wav", b"\x00\x20" * 4800, width=2, rate=24000, channels=1)
        chunks.append({"id": i, "uid": f"u{i}", "speaker": "Narrator", "text": text,
                       "audio_path": f"clip{i}.wav", "audio_revision": f"r{i}", "status": "done"})
    (Path(tmp) / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
    book = Path(tmp) / "book.txt"
    book.write_bytes((" ".join(texts)).encode("utf-8"))
    (Path(tmp) / "state.json").write_text(
        json.dumps({"input_file_path": str(book)}), encoding="utf-8")
    (Path(tmp) / "annotated_script.json").write_text("[]", encoding="utf-8")
    epub_bytes = _make_source_epub_nested()
    book_bytes = book.read_bytes()
    text_sha = hashlib.sha256(book_bytes).hexdigest()
    origin_sha = hashlib.sha256(epub_bytes).hexdigest()
    origin_id = hashlib.sha256(
        f"AA2_epub_publication_v1:{origin_sha}:{text_sha}".encode()).hexdigest()
    candidate = {
        "version": 1, "id": origin_id,
        "origin": {"kind": "epub", "filename": "source.epub",
                   "sha256": origin_sha, "text_sha256": text_sha},
        "fields": {"title": ["Synthetic Title"], "author": ["Test Author"]},
        "other_creators": [], "metadata_status": "available",
        "cover": None, "cover_status": "not_declared", "warnings": [],
    }
    from publication_source import get_new_upload_publication_bytes
    raw = get_new_upload_publication_bytes(text_sha, candidate, 1024 * 1024)
    (Path(tmp) / "book.txt.publication.json").write_bytes(raw)
    (Path(tmp) / "book.source.epub").write_bytes(epub_bytes)
    return epub_bytes


class R4CoverageTests(unittest.TestCase):
    def test_missing_middle_audio_fails_closed_first_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(4800, 24000, 1, 2)] * 3)
            (Path(tmp) / "clip1.wav").unlink()
            manager = ProjectManager(tmp)
            loaded, skipped = manager._load_chunks_with_audio()
            self.assertEqual(1, skipped)
            self.assertEqual(2, len(loaded))
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertFalse(ok)
            self.assertIn("refused", msg.lower())
            self.assertFalse((Path(tmp) / "readalong.sidecar.json").exists())
            self.assertFalse((Path(tmp) / "cloned_audiobook.mp3").exists())

    def test_missing_audio_preserves_prior_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(4800, 24000, 1, 2)] * 2)
            manager = ProjectManager(tmp)
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertTrue(ok, msg)
            old_mp3 = (Path(tmp) / "cloned_audiobook.mp3").read_bytes()
            old_side = (Path(tmp) / "readalong.sidecar.json").read_bytes()
            (Path(tmp) / "clip1.wav").unlink()
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertFalse(ok)
            self.assertEqual(old_mp3, (Path(tmp) / "cloned_audiobook.mp3").read_bytes())
            self.assertEqual(old_side, (Path(tmp) / "readalong.sidecar.json").read_bytes())

    def test_corrupt_audio_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(4800, 24000, 1, 2)] * 2)
            (Path(tmp) / "clip0.wav").write_bytes(b"not a wav file at all")
            manager = ProjectManager(tmp)
            loaded, skipped = manager._load_chunks_with_audio()
            self.assertEqual(1, skipped)
            ok, _msg = build_sidecar_export(manager, lambda: False)
            self.assertFalse(ok)

    def test_lying_skipped_counter_does_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            chunks = _setup_book(tmp, [(4800, 24000, 1, 2)] * 2)
            manager = ProjectManager(tmp)
            real_loader = manager._load_chunks_with_audio

            def lying_loader(**kwargs):
                loaded, _skipped = real_loader(**kwargs)
                return loaded[:1], 0  # claims full coverage, drops a row

            with patch.object(manager, "_load_chunks_with_audio", side_effect=lying_loader):
                ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertFalse(ok)
            self.assertIn("refused", msg.lower())
            self.assertEqual(2, len(chunks))  # admitted rows were two


class R4FrameTruthTests(unittest.TestCase):
    def test_mixed_rate_channel_width_bounds_exact_to_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(4800, 24000, 1, 2), (9600, 48000, 2, 2), (4800, 24000, 1, 1)])
            manager = ProjectManager(tmp)
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertTrue(ok, msg)
            doc = _sidecar_doc(tmp)
            plan, _timeline = _independent_plan(tmp)
            binding = doc["binding"]
            self.assertEqual(
                (plan["channels"], plan["frame_rate"], plan["sample_width"]),
                (binding["channels"], binding["sample_rate"], binding["sample_width"]))
            self.assertEqual(plan["frames"], binding["frames"])
            bounds = [(e["start_frame"], e["end_frame"]) for e in doc["entries"]]
            self.assertEqual([tuple(r) for r in plan["source_frame_ranges"]], bounds)
            # Pause gaps are the actual PCM gaps between plan ranges.
            for prev, cur, entry in zip(bounds, bounds[1:], doc["entries"]):
                self.assertEqual(cur[0] - prev[1], entry["pause_frames_after"])
            # Real artifacts: MP3 bytes bind to the sidecar binding.
            mp3 = (Path(tmp) / "cloned_audiobook.mp3").read_bytes()
            self.assertEqual(hashlib.sha256(mp3).hexdigest(), binding["audio_sha256"])
            self.assertEqual(len(mp3), binding["audio_bytes"])
            get_validated_sidecar(
                (Path(tmp) / "readalong.sidecar.json").read_bytes(),
                expected_audio_sha256=binding["audio_sha256"])

    def test_non_integer_ms_lengths_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 24010 frames at 24 kHz = 1000.416 ms; 12005 frames = 500.208 ms.
            _setup_book(tmp, [(24010, 24000, 1, 2), (12005, 24000, 1, 2)])
            manager = ProjectManager(tmp)
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertTrue(ok, msg)
            doc = _sidecar_doc(tmp)
            plan, _timeline = _independent_plan(tmp)
            bounds = [(e["start_frame"], e["end_frame"]) for e in doc["entries"]]
            self.assertEqual([tuple(r) for r in plan["source_frame_ranges"]], bounds)
            self.assertEqual(plan["frames"], doc["binding"]["frames"])
            # Widths are not whole milliseconds after promotion.
            self.assertGreater(bounds[0][1] - bounds[0][0], 0)
            self.assertNotEqual((bounds[0][1] - bounds[0][0]) * 1000 % 24000, 0)

    def test_long_pause_accumulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(2400, 24000, 1, 2)] * 3,
                        pause_afters=[2000, 2000, None])
            manager = ProjectManager(tmp)
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertTrue(ok, msg)
            doc = _sidecar_doc(tmp)
            plan, _timeline = _independent_plan(tmp)
            bounds = [(e["start_frame"], e["end_frame"]) for e in doc["entries"]]
            self.assertEqual([tuple(r) for r in plan["source_frame_ranges"]], bounds)
            gaps = [cur[0] - prev[1] for prev, cur in zip(bounds, bounds[1:])]
            self.assertEqual(gaps, [e["pause_frames_after"] for e in doc["entries"][:-1]])
            # Each 2000 ms pause accumulates near 48000 frames at 24 kHz.
            for gap in gaps:
                self.assertGreater(gap, 47000)
                self.assertLess(gap, 49000)
            self.assertEqual(int(plan["frames"]), doc["binding"]["frames"])

    def test_head_tail_trim_bounds_are_trimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            silence_head = b"\x00\x00" * 4800  # 200 ms
            silence_tail = b"\x00\x00" * 7200  # 300 ms
            tone = b"\x00\x20" * 12000  # 500 ms
            segment = (AudioSegment(data=silence_head, sample_width=2, frame_rate=24000, channels=1)
                       + AudioSegment(data=tone, sample_width=2, frame_rate=24000, channels=1)
                       + AudioSegment(data=silence_tail, sample_width=2, frame_rate=24000, channels=1))
            segment.export(str(Path(tmp) / "clip0.wav"), format="wav")
            chunks = [{"id": 0, "uid": "u0", "speaker": "Narrator", "text": "Hello.",
                       "audio_path": "clip0.wav", "audio_revision": "r0", "status": "done"}]
            (Path(tmp) / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
            book = Path(tmp) / "book.txt"
            book.write_bytes(b"Hello.")
            (Path(tmp) / "state.json").write_text(
                json.dumps({"input_file_path": str(book)}), encoding="utf-8")
            (Path(tmp) / "annotated_script.json").write_text("[]", encoding="utf-8")
            manager = ProjectManager(tmp)
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertTrue(ok, msg)
            doc = _sidecar_doc(tmp)
            entry = doc["entries"][0]
            plan, _timeline = _independent_plan(tmp)
            self.assertEqual([tuple(r) for r in plan["source_frame_ranges"]],
                             [(entry["start_frame"], entry["end_frame"])])
            # Trimmed length (~620 ms), not the original 1000 ms take.
            self.assertLess(entry["end_frame"] - entry["start_frame"], 24000)
            self.assertGreater(entry["end_frame"] - entry["start_frame"], 12000)
            self.assertEqual(plan["frames"], doc["binding"]["frames"])

    def test_mp3_decode_within_codec_delay_tolerance(self):
        """Decoded MP3 frames are within codec delay tolerance of PCM frames.

        MP3 priming/padding and encoder delay mean decoded samples are
        not claimed equal to PCM frames; the test asserts proximity,
        not equality.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(24000, 48000, 1, 2)])
            manager = ProjectManager(tmp)
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertTrue(ok, msg)
            doc = _sidecar_doc(tmp)
            decoded = AudioSegment.from_file(
                str(Path(tmp) / "cloned_audiobook.mp3"), format="mp3")
            decoded_frames = len(decoded.raw_data) // (decoded.channels * decoded.sample_width)
            pcm_frames = doc["binding"]["frames"]
            # Encoder delay (typically ~1k samples) plus MP3 frame padding
            # (1152-sample frames) is far below one second at 48 kHz.
            self.assertLessEqual(abs(decoded_frames - pcm_frames), 4096)


class R4PublicationTests(unittest.TestCase):
    def _fail_once_on_target(self, target_suffix):
        import readalong_export
        orig_replace = readalong_export.os.replace
        state = {"failed": False}

        def flaky(src, dst):
            if (not state["failed"] and str(dst).endswith(target_suffix)
                    and str(src).endswith(target_suffix)):
                state["failed"] = True
                raise OSError("injected target failure")
            return orig_replace(src, dst)
        return flaky

    def test_second_target_failure_rolls_back_existing_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(4800, 24000, 1, 2)] * 2)
            manager = ProjectManager(tmp)
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertTrue(ok, msg)
            old_mp3 = (Path(tmp) / "cloned_audiobook.mp3").read_bytes()
            old_side = (Path(tmp) / "readalong.sidecar.json").read_bytes()
            chunks = json.loads((Path(tmp) / "chunks.json").read_text(encoding="utf-8"))
            chunks[0]["pause_after"] = 500
            (Path(tmp) / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
            import readalong_export
            with patch.object(readalong_export.os, "replace",
                              side_effect=self._fail_once_on_target("readalong.sidecar.json")):
                ok, _msg = build_sidecar_export(manager, lambda: False)
            self.assertFalse(ok)
            self.assertEqual(old_mp3, (Path(tmp) / "cloned_audiobook.mp3").read_bytes())
            self.assertEqual(old_side, (Path(tmp) / "readalong.sidecar.json").read_bytes())

    def test_second_target_failure_first_build_leaves_neither(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(4800, 24000, 1, 2)] * 2)
            manager = ProjectManager(tmp)
            import readalong_export
            with patch.object(readalong_export.os, "replace",
                              side_effect=self._fail_once_on_target("readalong.sidecar.json")):
                ok, _msg = build_sidecar_export(manager, lambda: False)
            self.assertFalse(ok)
            self.assertFalse((Path(tmp) / "cloned_audiobook.mp3").exists())
            self.assertFalse((Path(tmp) / "readalong.sidecar.json").exists())

    def test_epub_receipt_failure_restores_all_prior_outputs(self):
        texts = ["Chapter One", "Hello synthetic world.", "Second paragraph here.",
                 "Quoted line here.", "First list item."]
        names = ("cloned_audiobook.mp3", "readaloud.epub", "readaloud.receipt.json")
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                _setup_epub_book(tmp, texts)
                manager = ProjectManager(tmp)
                if existing:
                    ok, msg = build_epub_export(manager, lambda: False)
                    self.assertTrue(ok, msg)
                before = {name: (Path(tmp) / name).read_bytes() for name in names
                          if (Path(tmp) / name).exists()}
                chunks_path = Path(tmp) / "chunks.json"
                chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
                chunks[0]["pause_after"] = 500
                chunks_path.write_text(json.dumps(chunks), encoding="utf-8")
                import readalong_export
                with patch.object(readalong_export.os, "replace",
                                  side_effect=self._fail_once_on_target("readaloud.receipt.json")):
                    ok, msg = build_epub_export(manager, lambda: False)
                self.assertFalse(ok)
                self.assertIn("injected target failure", msg)
                for name in names:
                    path = Path(tmp) / name
                    if name in before:
                        self.assertEqual(before[name], path.read_bytes(), name)
                    else:
                        self.assertFalse(path.exists(), name)

    def test_cancellation_between_publications_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(4800, 24000, 1, 2)] * 2)
            manager = ProjectManager(tmp)
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertTrue(ok, msg)
            old_mp3 = (Path(tmp) / "cloned_audiobook.mp3").read_bytes()
            old_side = (Path(tmp) / "readalong.sidecar.json").read_bytes()
            chunks = json.loads((Path(tmp) / "chunks.json").read_text(encoding="utf-8"))
            chunks[0]["pause_after"] = 500
            (Path(tmp) / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
            import readalong_export
            orig_ensure = readalong_export.ensure_audio_export_active
            orig_replace = readalong_export.os.replace
            published = []

            def flaky_replace(src, dst):
                result = orig_replace(src, dst)
                if str(dst) == str(Path(tmp) / "cloned_audiobook.mp3"):
                    published.append(True)
                return result

            def cancel_after_publish(check=None):
                if published:
                    from tts import ExportCancelled
                    raise ExportCancelled("cancelled between publications")
                return orig_ensure(check)

            with patch.object(readalong_export.os, "replace", side_effect=flaky_replace), \
                    patch.object(readalong_export, "ensure_audio_export_active",
                                 side_effect=cancel_after_publish):
                ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertFalse(ok)
            self.assertIn("cancel", msg.lower())
            self.assertEqual(old_mp3, (Path(tmp) / "cloned_audiobook.mp3").read_bytes())
            self.assertEqual(old_side, (Path(tmp) / "readalong.sidecar.json").read_bytes())

    def test_stale_admission_between_publications_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(4800, 24000, 1, 2)] * 2)
            manager = ProjectManager(tmp)
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertTrue(ok, msg)
            old_mp3 = (Path(tmp) / "cloned_audiobook.mp3").read_bytes()
            old_side = (Path(tmp) / "readalong.sidecar.json").read_bytes()
            import readalong_export
            orig_replace = readalong_export.os.replace
            mutated = []

            def stale_after_mp3(src, dst):
                result = orig_replace(src, dst)
                if str(dst) == str(Path(tmp) / "cloned_audiobook.mp3") and not mutated:
                    mutated.append(True)
                    chunks = json.loads((Path(tmp) / "chunks.json").read_text(encoding="utf-8"))
                    chunks[0]["text"] = "Changed after admission."
                    (Path(tmp) / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
                return result

            with patch.object(readalong_export.os, "replace", side_effect=stale_after_mp3):
                ok, _msg = build_sidecar_export(manager, lambda: False)
            self.assertFalse(ok)
            self.assertEqual(old_mp3, (Path(tmp) / "cloned_audiobook.mp3").read_bytes())
            self.assertEqual(old_side, (Path(tmp) / "readalong.sidecar.json").read_bytes())

    def test_rollback_failure_retains_recovery_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(4800, 24000, 1, 2)] * 2)
            manager = ProjectManager(tmp)
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertTrue(ok, msg)
            old_mp3 = (Path(tmp) / "cloned_audiobook.mp3").read_bytes()
            chunks = json.loads((Path(tmp) / "chunks.json").read_text(encoding="utf-8"))
            chunks[0]["pause_after"] = 500
            (Path(tmp) / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
            import readalong_export
            orig_replace = readalong_export.os.replace
            second = {"failed": False}

            def flaky(src, dst):
                if (not second["failed"] and str(dst).endswith("readalong.sidecar.json")
                        and str(src).endswith("readalong.sidecar.json")):
                    second["failed"] = True
                    raise OSError("injected second-target failure")
                if "old" in str(src) and str(dst).endswith("cloned_audiobook.mp3"):
                    raise OSError("injected rollback failure")
                return orig_replace(src, dst)

            with patch.object(readalong_export.os, "replace", side_effect=flaky):
                ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertFalse(ok)
            self.assertIn("rollback failed", msg.lower())
            self.assertIn("recovery files kept at", msg)
            # Old MP3 bytes survive in a retained backup under the staging parent.
            kept = [p for p in Path(tmp).rglob("*") if p.is_file() and p.read_bytes() == old_mp3
                    and "old" in str(p)]
            self.assertTrue(kept, "backup with prior MP3 bytes must be retained")


class R4ReceiptTests(unittest.TestCase):
    def test_missing_source_receipt_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(4800, 24000, 1, 2)], state=False)
            manager = ProjectManager(tmp)
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertFalse(ok)
            self.assertIn("identity", msg.lower())
            self.assertFalse((Path(tmp) / "readalong.sidecar.json").exists())

    def test_app_receipt_binds_book_rows_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_book(tmp, [(4800, 24000, 1, 2)] * 2)
            manager = ProjectManager(tmp)
            ok, msg = build_sidecar_export(manager, lambda: False)
            self.assertTrue(ok, msg)
            doc = _sidecar_doc(tmp)
            source = doc.get("source")
            self.assertIsNotNone(source)
            from editor_poll_snapshot import get_snapshot_digest
            from book_state_transaction import get_book_operation_identity
            self.assertEqual(
                source["book_token"],
                get_snapshot_digest(get_book_operation_identity(tmp)))
            book = (Path(tmp) / "book.txt").read_bytes()
            self.assertEqual(source["source_sha256"], hashlib.sha256(book).hexdigest())
            self.assertEqual(64, len(source["rows_digest"]))
            get_validated_sidecar(
                (Path(tmp) / "readalong.sidecar.json").read_bytes(),
                expected_audio_sha256=doc["binding"]["audio_sha256"])

    def test_portable_helper_keeps_optional_receipt(self):
        binding = {"audio_sha256": hashlib.sha256(b"exported-mp3").hexdigest(),
                   "audio_bytes": 12, "sample_rate": 24000, "channels": 1,
                   "sample_width": 2, "frames": 48000}
        entries = [{"index": 0, "section": "sec-0001", "speaker": "Narrator",
                    "text": "Hello world.", "start_frame": 0, "end_frame": 24000,
                    "pause_frames_after": 0, "status": "missing", "words": None}]
        doc, _raw = build_readalong_sidecar(
            entries=entries, binding=binding, includes_text=False, source_receipt=None)
        self.assertNotIn("source", doc)

    def test_epub_receipt_binds_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            texts = ["Chapter One", "Hello synthetic world.", "Second paragraph here.",
                     "Quoted line here.", "First list item."]
            retained = _setup_epub_book(tmp, texts)
            manager = ProjectManager(tmp)
            ok, msg = build_epub_export(manager, lambda: False)
            self.assertTrue(ok, msg)
            receipt_raw = (Path(tmp) / "readaloud.receipt.json").read_bytes()
            receipt = get_validated_epub_receipt(receipt_raw)
            epub_bytes = (Path(tmp) / "readaloud.epub").read_bytes()
            mp3_bytes = (Path(tmp) / "cloned_audiobook.mp3").read_bytes()
            self.assertEqual(receipt["epub_sha256"], hashlib.sha256(epub_bytes).hexdigest())
            self.assertEqual(receipt["audio_sha256"], hashlib.sha256(mp3_bytes).hexdigest())
            self.assertEqual(receipt["retained_epub_sha256"], hashlib.sha256(retained).hexdigest())
            self.assertEqual(
                receipt["source_sha256"],
                hashlib.sha256((Path(tmp) / "book.txt").read_bytes()).hexdigest())
            self.assertEqual(64, len(receipt["book_token"]))
            self.assertEqual(64, len(receipt["rows_digest"]))
            from epub_readaloud import validate_epub_readaloud
            validate_epub_readaloud(epub_bytes)


class R4ClipClockTests(unittest.TestCase):
    def test_ten_one_frame_spans_refused_not_padded(self):
        from epub_readaloud import _clip_bounds_ms
        mapping = [{"start_frame": i, "end_frame": i + 1} for i in range(10)]
        with self.assertRaises(ValueError) as ctx:
            _clip_bounds_ms(mapping, 24000)
        self.assertIn("quantum", str(ctx.exception).lower())

    def test_single_frame_clip_refused(self):
        from epub_readaloud import _clip_bounds_ms
        with self.assertRaises(ValueError):
            _clip_bounds_ms([{"start_frame": 0, "end_frame": 1}], 24000)

    def test_overlapping_frames_refused_not_shifted(self):
        from epub_readaloud import _clip_bounds_ms
        mapping = [{"start_frame": 0, "end_frame": 24000},
                   {"start_frame": 12000, "end_frame": 36000}]
        with self.assertRaises(ValueError) as ctx:
            _clip_bounds_ms(mapping, 24000)
        self.assertIn("overlap", str(ctx.exception).lower())

    def test_short_normal_clip_still_builds(self):
        from epub_readaloud import build_epub_readaloud
        source = _make_source_epub_nested()
        # One short (1 ms = 24 frames) but representable clip + normal clips.
        entries = [
            {"index": 0, "prepared_text": "Chapter One", "start_frame": 0, "end_frame": 24, "sample_rate": 24000},
            {"index": 1, "prepared_text": "Hello synthetic world.", "start_frame": 48, "end_frame": 24048, "sample_rate": 24000},
            {"index": 2, "prepared_text": "Second paragraph here.", "start_frame": 24072, "end_frame": 36072, "sample_rate": 24000},
            {"index": 3, "prepared_text": "Quoted line here.", "start_frame": 36096, "end_frame": 48096, "sample_rate": 24000},
            {"index": 4, "prepared_text": "First list item.", "start_frame": 48120, "end_frame": 60000, "sample_rate": 24000},
        ]
        epub_bytes, _report = build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b"\xff\xfbx",
            audio_filename="audiobook.mp3", script_entries=entries)
        from epub_readaloud import validate_epub_readaloud
        validate_epub_readaloud(epub_bytes)


if __name__ == "__main__":
    unittest.main()
