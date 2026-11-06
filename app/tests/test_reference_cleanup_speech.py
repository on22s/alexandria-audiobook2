"""Validate waveform-integrity controls on locally generated synthetic speech.

These controls detect unauthorized sample changes, not semantic recognition.
ASR and speaker identity remain unmeasured, including on the spoken controls.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import soundfile as sf

from reference_cleanup import apply_affine_cleanup, validate_cleanup_plan, verify_affine_candidate
from voice_reference_import import check_reference_audio, measure_reference_audio


class SpokenCleanupControls(unittest.TestCase):
    def setUp(self):
        fixture_root = Path(__file__).parent / "fixtures" / "reference_cleanup_speech"
        manifest = json.loads((fixture_root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(["red", "blue"], manifest["changed_word"])
        self.assertEqual("UNMEASURED", manifest["asr"])
        samples = []
        for record in manifest["controls"]:
            fixture = fixture_root / record["file"]
            self.assertEqual(record["sha256"], hashlib.sha256(fixture.read_bytes()).hexdigest())
            waveform, rate = sf.read(fixture, dtype="float64")
            self.assertEqual(24000, rate)
            self.assertEqual(1, waveform.ndim)
            samples.append(waveform)
        self.assertEqual(2, len(samples))
        frame_count = max(waveform.size for waveform in samples)
        original = np.pad(samples[0], (0, frame_count - samples[0].size))
        replacement = np.pad(samples[1], (0, frame_count - samples[1].size))
        original *= 0.15 / np.max(np.abs(original))
        replacement *= np.sqrt(np.sum(original ** 2) / np.sum(replacement ** 2))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.original = self.directory / "original.wav"
        self.candidate = self.directory / "candidate.wav"
        self.rate = 24000
        sf.write(self.original, original, self.rate, subtype="PCM_16")
        self.replacement = replacement
        self.plan = validate_cleanup_plan(1.25, True)
        apply_affine_cleanup(self.original, self.candidate, self.plan)

    def test_spoken_original_is_admissible_and_affine_control_passes(self):
        self.assertEqual([], check_reference_audio(measure_reference_audio(self.original)))
        valid, problems, records = verify_affine_candidate(self.original, self.candidate, self.plan)
        self.assertTrue(valid, problems)
        self.assertEqual("UNMEASURED", records["asr"])
        self.assertEqual("UNMEASURED", records["speaker_identity"])
        self.assertEqual(sf.info(self.original).frames, records["samples_checked"])

    def test_changed_word_same_frames_and_energy_is_rejected(self):
        original, _ = sf.read(self.original, dtype="float64")
        self.assertEqual(original.size, self.replacement.size)
        self.assertAlmostEqual(float(np.sum(original ** 2)), float(np.sum(self.replacement ** 2)), delta=0.1)
        replacement_path = self.directory / "replacement.wav"
        sf.write(replacement_path, self.replacement, self.rate, subtype="PCM_16")
        apply_affine_cleanup(replacement_path, self.candidate, self.plan)
        valid, problems, _ = verify_affine_candidate(self.original, self.candidate, self.plan)
        self.assertFalse(valid)
        self.assertTrue(any("sample mismatch" in problem for problem in problems), problems)

    def test_removed_spoken_span_same_frame_count_is_rejected(self):
        candidate, _ = sf.read(self.candidate, dtype="int16")
        center = int(np.argmax(np.abs(candidate.astype(np.int32))))
        candidate[max(0, center - 1200):center + 1200] = 0
        sf.write(self.candidate, candidate, self.rate, subtype="PCM_16")
        self.assertEqual(sf.info(self.original).frames, sf.info(self.candidate).frames)
        valid, problems, _ = verify_affine_candidate(self.original, self.candidate, self.plan)
        self.assertFalse(valid)
        self.assertTrue(any("sample mismatch" in problem for problem in problems), problems)

    def test_clipped_spoken_candidate_is_rejected(self):
        candidate, _ = sf.read(self.candidate, dtype="float64")
        candidate = np.clip(candidate * 20, -1.0, 1.0)
        sf.write(self.candidate, candidate, self.rate, subtype="PCM_16")
        self.assertTrue(check_reference_audio(measure_reference_audio(self.candidate)))
        valid, problems, _ = verify_affine_candidate(self.original, self.candidate, self.plan)
        self.assertFalse(valid)
        self.assertTrue(any("sample mismatch" in problem for problem in problems), problems)
