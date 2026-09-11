"""The clone-reference gate: normalise, measure, refuse by named rule, record."""
import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parent.parent))

import voice_reference_import as vri
from routers import voice_design as voice_design_module
from tests.test_support import _Upload


def _tone(seconds, rate=24000, amp=0.5, hz=220.0):
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def _write(path, samples, rate=24000, subtype="PCM_16"):
    sf.write(path, samples, rate, subtype=subtype)
    return path


def _wav_bytes(samples, rate=24000):
    buf = io.BytesIO()
    sf.write(buf, samples, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class MeasureAndCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _measures(self, samples, rate=24000):
        return vri.measure_reference_audio(_write(os.path.join(self.dir, "x.wav"), samples, rate))

    def test_a_clean_sentence_length_clip_passes(self):
        m = self._measures(_tone(5.0))
        self.assertEqual([], vri.check_reference_audio(m))
        self.assertEqual((24000, 5.0), (m["sample_rate"], m["duration_s"]))
        self.assertAlmostEqual(m["peak"], 0.5, places=2)
        self.assertEqual(64, len(m["sha256"]))

    def test_every_violated_rule_is_named(self):
        self.assertIn("too short", vri.check_reference_audio(self._measures(_tone(1.0)))[0])
        self.assertIn("too long", vri.check_reference_audio(self._measures(_tone(40.0)))[0])
        square = np.sign(_tone(5.0)).astype(np.float32)             # +-1.0 everywhere
        self.assertTrue(any("clipped" in p for p in vri.check_reference_audio(self._measures(square))))
        mostly_silent = np.concatenate([_tone(0.5), np.zeros(int(4.5 * 24000), np.float32)])
        self.assertTrue(any("mostly silence" in p for p in vri.check_reference_audio(self._measures(mostly_silent))))
        self.assertTrue(any("near-empty" in p for p in vri.check_reference_audio(self._measures(_tone(5.0, amp=0.01)))))
        # a clip can fail several rules at once; all are reported, not just the first
        short_and_silent = np.zeros(int(1.0 * 24000), np.float32)
        problems = vri.check_reference_audio(self._measures(short_and_silent))
        self.assertEqual(3, len(problems), problems)

    def test_normalisation_yields_24k_mono_pcm16_from_any_input(self):
        stereo = np.stack([_tone(4.0, rate=44100), _tone(4.0, rate=44100, hz=330)], axis=1)
        src = _write(os.path.join(self.dir, "in.flac"), stereo, 44100, subtype="PCM_24")
        dst = vri.normalize_reference_audio(src, os.path.join(self.dir, "out.wav"))
        with wave.open(dst, "rb") as w:
            self.assertEqual((1, 2, 24000), (w.getnchannels(), w.getsampwidth(), w.getframerate()))
        self.assertAlmostEqual(vri.measure_reference_audio(dst)["duration_s"], 4.0, places=1)

    def test_import_refuses_non_audio_and_writes_nothing(self):
        src = os.path.join(self.dir, "not_audio.wav")
        Path(src).write_bytes(b"first")
        dst = os.path.join(self.dir, "out.wav")
        measures, problems = vri.import_reference_audio(src, dst)
        self.assertEqual({}, measures)
        self.assertIn("could not decode", problems[0])
        self.assertFalse(os.path.exists(dst))


class UploadRouteTests(unittest.TestCase):
    def _upload(self, tmp, content, **fields):
        async def go():
            source = _Upload([content])
            source.filename = "Same Voice.wav"
            kwargs = {"ref_text": "Hello there.", "rights_confirmed": True}
            kwargs.update(fields)
            return await voice_design_module.clone_voices_upload(source, **kwargs)
        with patch.object(voice_design_module, "CLONE_VOICES_DIR", tmp), \
             patch.object(voice_design_module, "CLONE_VOICES_MANIFEST", os.path.join(tmp, "manifest.json")):
            return asyncio.run(go())

    def test_a_valid_clip_is_normalised_and_recorded_with_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._upload(tmp, _wav_bytes(_tone(5.0, rate=44100), 44100),
                                  source_title="My recording", source_url="https://example/x",
                                  rights_basis="own recording")
            self.assertEqual("uploaded", result["status"])
            manifest = json.loads(Path(tmp, "manifest.json").read_text(encoding="utf-8"))
            entry = manifest[0]
            self.assertEqual(result["voice_id"], entry["id"])
            self.assertTrue(entry["filename"].endswith(".wav"))
            self.assertEqual(("Hello there.", "My recording", "https://example/x", "own recording", True),
                             (entry["ref_text"], entry["source_title"], entry["source_url"],
                              entry["rights_basis"], entry["rights_confirmed"]))
            self.assertEqual(24000, entry["sample_rate"])
            self.assertAlmostEqual(entry["duration_s"], 5.0, places=1)
            self.assertEqual(entry["sha256"], vri.measure_reference_audio(os.path.join(tmp, entry["filename"]))["sha256"])
            self.assertEqual([entry["filename"], "manifest.json"], sorted(os.listdir(tmp)))  # no temp upload left

    def test_refusals_leave_no_file_and_no_manifest_entry(self):
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as tmp:
            cases = [
                (_wav_bytes(_tone(1.0)), {}, "too short"),
                (b"first", {}, "could not decode"),
                (_wav_bytes(_tone(5.0)), {"rights_confirmed": False}, "rights_confirmed"),
                (_wav_bytes(_tone(5.0)), {"ref_text": "  "}, "ref_text"),
            ]
            for content, fields, expect in cases:
                with self.assertRaises(HTTPException) as ctx:
                    self._upload(tmp, content, **fields)
                self.assertEqual(400, ctx.exception.status_code)
                self.assertIn(expect, ctx.exception.detail)
            self.assertEqual([], os.listdir(tmp))


if __name__ == "__main__":
    unittest.main()
