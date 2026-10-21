"""Acoustic evidence bounds plus synthetic short-repeat and non-speech controls."""
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np

from speech_verification_acoustic import get_short_acoustic_evidence


class ShortAcousticEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'audio.wav'

    def write(self, values, channels=1, rate=16000):
        with wave.open(str(self.path), 'wb') as stream:
            stream.setnchannels(channels); stream.setsampwidth(2); stream.setframerate(rate)
            stream.writeframes((np.asarray(values) * 32767).astype('<i2').tobytes())

    def get_modulated_clip(self, reverse=False):
        # A changing-frequency, changing-envelope synthetic control, not speech.
        t = np.arange(16000) / 16000
        frequency = np.repeat([200, 600, 1500, 350, 1000], 3200)
        envelope = .25 * (np.sin(np.pi * t) ** 2) + .05
        clip = np.sin(2 * np.pi * np.cumsum(frequency) / 16000) * envelope
        return clip[::-1] if reverse else clip

    def test_exact_separated_short_repeat_is_suspicion_without_lexical_verdict(self):
        clip = self.get_modulated_clip()
        self.write(np.concatenate([clip, np.zeros(4000), clip]))
        result = get_short_acoustic_evidence(self.path)
        self.assertEqual(2, len(result['voiced_islands']))
        self.assertEqual(1, len(result['repetition_candidates']))
        self.assertEqual('review', result['status'])
        self.assertNotIn('failure', result)

    def test_flat_tones_silence_and_distinct_envelopes_do_not_claim_repeated_speech(self):
        t = np.arange(16000) / 16000
        tone = .2 * np.sin(2 * np.pi * 300 * t)
        for clip in [np.zeros(16000), np.concatenate([tone, np.zeros(4000), tone]),
                     np.concatenate([self.get_modulated_clip(), np.zeros(4000), self.get_modulated_clip(True)])]:
            with self.subTest(frames=len(clip)):
                self.write(clip)
                self.assertEqual([], get_short_acoustic_evidence(self.path)['repetition_candidates'])

    def test_breath_sized_impulses_and_stutter_are_not_utterance_count(self):
        pulse = self.get_modulated_clip()[:1000]
        self.write(np.concatenate([pulse, np.zeros(3000), pulse, np.zeros(3000), pulse]))
        result = get_short_acoustic_evidence(self.path)
        self.assertEqual(3, len(result['voiced_islands']))
        self.assertEqual([], result['repetition_candidates'])

    def test_duration_channel_rate_and_cancellation_are_explicit(self):
        for values, channels, rate in [(np.zeros(240001), 1, 16000), (np.zeros(100), 2, 16000),
                                        (np.zeros(100), 1, 8000), ([], 1, 16000)]:
            self.write(values, channels, rate)
            with self.assertRaises(ValueError):
                get_short_acoustic_evidence(self.path)
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            get_short_acoustic_evidence(self.path, lambda: True)

    def test_pair_budget_refuses_excess_islands_without_partial_candidate_pass(self):
        pulse = self.get_modulated_clip()[:2500]
        self.write(np.tile(np.concatenate([pulse, np.zeros(2500)]), 17))
        result = get_short_acoustic_evidence(self.path)
        self.assertTrue(result['island_limit_exceeded'])
        self.assertEqual(16, len(result['voiced_islands']))
        self.assertEqual('unavailable_island_limit', result['status'])
        self.assertEqual([], result['repetition_candidates'])


if __name__ == '__main__':
    unittest.main()
