"""AA2 read-along sidecar (#1720) — builder, validator, negative controls."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from readalong_sidecar import (
    build_readalong_sidecar,
    get_validated_sidecar,
    map_word_spans_to_frames,
    MAX_SIDECAR_BYTES,
)


def _binding():
    return {"audio_sha256": hashlib.sha256(b"exported-mp3").hexdigest(),
            "audio_bytes": 12, "sample_rate": 24000, "channels": 1,
            "sample_width": 2, "frames": 48000}


def _entries():
    return [
        {"index": 0, "section": "sec-0001", "speaker": "Narrator",
         "text": "Hello world.", "start_frame": 0, "end_frame": 24000,
         "pause_frames_after": 240, "status": "missing", "words": None},
        {"index": 1, "section": "sec-0001", "speaker": "Narrator",
         "text": "Second line.", "start_frame": 24240, "end_frame": 48000,
         "pause_frames_after": 0, "status": "missing", "words": None},
    ]


class ReadalongSidecarTests(unittest.TestCase):
    def test_roundtrip_without_text(self):
        doc, raw = build_readalong_sidecar(entries=_entries(), binding=_binding(),
                                           includes_text=False)
        self.assertEqual("aa2-readalong-sidecar", doc["schema"])
        self.assertFalse(doc["includes_text"])
        self.assertTrue(all(e["text"] is None for e in doc["entries"]))
        parsed = get_validated_sidecar(raw, expected_audio_sha256=_binding()["audio_sha256"])
        self.assertEqual(2, len(parsed["entries"]))
        self.assertEqual(1, len(parsed["sections"]))

    def test_roundtrip_with_text_and_words(self):
        entries = _entries()
        entries[0] = {**entries[0], "status": "aligned-estimated",
                      "words": [{"from": 0, "to": 5, "start_frame": 100, "end_frame": 1000}]}
        doc, raw = build_readalong_sidecar(entries=entries, binding=_binding(), includes_text=True)
        self.assertTrue(doc["includes_text"])
        self.assertEqual("Hello world.", doc["entries"][0]["text"])
        get_validated_sidecar(raw)

    def test_wrong_audio_rejected(self):
        _doc, raw = build_readalong_sidecar(entries=_entries(), binding=_binding(), includes_text=False)
        with self.assertRaises(ValueError):
            get_validated_sidecar(raw, expected_audio_sha256=hashlib.sha256(b"other").hexdigest())

    def test_malformed_and_oversized_rejected(self):
        with self.assertRaises(ValueError):
            get_validated_sidecar(b"not json", expected_audio_sha256=None)
        with self.assertRaises(ValueError):
            get_validated_sidecar(b"x" * (MAX_SIDECAR_BYTES + 1))

    def test_corrupted_frames_rejected(self):
        _doc, raw = build_readalong_sidecar(entries=_entries(), binding=_binding(), includes_text=False)
        doc = json.loads(raw)
        doc["entries"][0]["end_frame"] = 999999
        with self.assertRaises(ValueError):
            get_validated_sidecar(json.dumps(doc).encode())

    def test_overlapping_entries_rejected(self):
        _doc, raw = build_readalong_sidecar(entries=_entries(), binding=_binding(), includes_text=False)
        doc = json.loads(raw)
        doc["entries"][1]["start_frame"] = 100
        with self.assertRaises(ValueError):
            get_validated_sidecar(json.dumps(doc).encode())

    def test_forbidden_disclosure_rejected(self):
        _doc, raw = build_readalong_sidecar(entries=_entries(), binding=_binding(), includes_text=False)
        doc = json.loads(raw)
        doc["entries"][0]["audio_path"] = "C:/secret/book.mp3"
        with self.assertRaises(ValueError):
            get_validated_sidecar(json.dumps(doc).encode())

    def test_unsupported_schema_rejected(self):
        with self.assertRaises(ValueError):
            get_validated_sidecar(json.dumps({"schema": "other", "version": 1,
                                              "binding": {"audio_sha256": "0" * 64, "frames": 10},
                                              "entries": []}).encode())

    def test_saturation_ambiguous_never_invents_words(self):
        cached = {"spans": [{"from": 0, "to": 5, "start": 0.0, "end": 1.0},
                            {"from": 6, "to": 11, "start": 0.0, "end": 1.0}],
                  "duration": 2.0}
        words, status = map_word_spans_to_frames(cached, None, (1, 24000, 2), 0, 24000)
        self.assertIsNone(words)
        self.assertEqual("ambiguous", status)

    def test_word_map_bounds(self):
        cached = {"spans": [{"from": 0, "to": 5, "start": 0.25, "end": 1.0}], "duration": 2.0}
        words, status = map_word_spans_to_frames(cached, None, (1, 24000, 2), 1000, 1000 + 48000,
                                                 trim_head_seconds=0.0)
        self.assertEqual("aligned-estimated", status)
        self.assertTrue(words[0]["start_frame"] >= 1000)
        self.assertTrue(words[0]["end_frame"] <= 49000)

    def test_word_map_without_true_trim_is_ambiguous(self):
        cached = {"spans": [{"from": 0, "to": 5, "start": 0.25, "end": 1.0}], "duration": 2.0}
        words, status = map_word_spans_to_frames(cached, None, (1, 24000, 2), 1000, 1000 + 48000)
        self.assertIsNone(words)
        self.assertEqual("ambiguous", status)


class ReadalongExportTimelineTests(unittest.TestCase):
    def test_sidecar_build_binds_to_timeline_frames(self):
        from pydub import AudioSegment
        from project import ProjectManager

        def clip(frames=2400):
            return AudioSegment(data=b"\x00\x20" * frames, sample_width=2, frame_rate=24000, channels=1)

        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            chunks = [{"id": i, "speaker": "Narrator", "text": "Hello world.", "uid": f"u{i}",
                       "audio_path": f"clip{i}.wav", "audio_revision": f"r{i}"} for i in range(2)]
            # Actual source state so the required app receipt can bind.
            (Path(tmp) / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
            book = Path(tmp) / "book.txt"
            book.write_bytes(b"Hello world. Hello world.")
            (Path(tmp) / "state.json").write_text(
                json.dumps({"input_file_path": str(book)}), encoding="utf-8")
            (Path(tmp) / "annotated_script.json").write_text("[]", encoding="utf-8")
            manager._load_chunks_with_audio = lambda **kwargs: (
                [(chunks[0], clip()), (chunks[1], clip())], 0)
            manager._load_pause_defaults = lambda: (10, 10)
            manager.load_chunks = lambda: chunks
            (Path(tmp) / "cloned_audiobook.mp3").write_bytes(b"exported-mp3")
            stale_sha = hashlib.sha256(b"exported-mp3").hexdigest()
            from readalong_export import build_sidecar_export
            ok, msg = build_sidecar_export(manager, lambda: False, includes_text=False,
                                           audio_path=str(Path(tmp) / "cloned_audiobook.mp3"))
            self.assertTrue(ok, msg)
            from readalong_export import get_sidecar_output_path
            raw_sidecar = Path(get_sidecar_output_path(tmp)).read_bytes()
            data = json.loads(raw_sidecar.decode("utf-8"))
            self.assertEqual(2, len(data["entries"]))
            # Fresh-encode authority: binding matches the freshly published
            # MP3 bytes, never the pre-existing stale file.
            fresh_bytes = (Path(tmp) / "cloned_audiobook.mp3").read_bytes()
            self.assertNotEqual(stale_sha, hashlib.sha256(fresh_bytes).hexdigest())
            self.assertEqual(hashlib.sha256(fresh_bytes).hexdigest(),
                             data["binding"]["audio_sha256"])
            get_validated_sidecar(raw_sidecar, expected_audio_sha256=data["binding"]["audio_sha256"])
            # Pause of 10ms at 24000 Hz: fresh-state ratecv rounding gives 238.
            from audio_streaming import get_silence_frame_count
            self.assertEqual(get_silence_frame_count(10, 24000),
                             data["entries"][0]["pause_frames_after"])
            self.assertTrue(data["entries"][1]["start_frame"] >= data["entries"][0]["end_frame"])


if __name__ == "__main__":
    unittest.main()
