"""Native PCM silence is unavailable pitch rather than a measured bass voice."""
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
import soundfile as sf
from voice_acoustics import get_pitch_gender_estimate

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('unvoiced_profiler', ROOT / 'tools/voice_lab/voice_profiler.py')
profiler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profiler)
NAME_SPEC = importlib.util.spec_from_file_location("unvoiced_naming", ROOT / "tools/voice_lab/name_voices.py")
naming = importlib.util.module_from_spec(NAME_SPEC)
NAME_SPEC.loader.exec_module(naming)


class UnvoicedProfileTests(unittest.TestCase):
    def analyze(self, signal):
        wav = io.BytesIO()
        sf.write(wav, signal, 22050, format='WAV', subtype='PCM_16')
        return profiler.analyze_ref_wav(wav.getvalue())

    def test_native_silence_artifact_and_csv_do_not_claim_male_or_bass(self):
        features = self.analyze(np.zeros(22050, dtype=np.float32))
        self.assertEqual(0., features['mean_f0'])
        summary = profiler.interpret_features(features)
        self.assertIn('unknown pitch', summary)
        self.assertNotIn('bass', summary)
        self.assertNotIn('male', summary)
        self.assertNotIn('50s', summary)
        entry = {'id': 'silence', 'voice_profile': summary, 'voice_features': features}
        row = profiler.profile_csv_row(entry)
        self.assertEqual('unknown', row['gender_est'])
        self.assertTrue(naming.derive_base_slug(summary, features['mean_f0']).endswith('_unknown'))
        self.assertEqual('unvoiced', features['pitch_status'])
        self.assertEqual(0, features['voiced_frame_count'])
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / 'features.json'
            profiler.atomic_json_write(entry, str(artifact))
            reread = json.loads(artifact.read_text())
            self.assertEqual('unvoiced', reread['voice_features']['pitch_status'])
            path = Path(tmp) / 'profiles.csv'
            profiler.atomic_csv_write([profiler.profile_csv_row(reread)], str(path))
            with path.open(newline='') as handle:
                self.assertEqual('unknown', next(csv.DictReader(handle))['gender_est'])

    def test_known_voiced_tone_remains_measured_and_near_its_frequency(self):
        times = np.arange(22050, dtype=np.float32) / 22050
        features = self.analyze(.2 * np.sin(2 * np.pi * 180 * times))
        self.assertEqual('measured', features['pitch_status'])
        self.assertGreater(features['voiced_frame_count'], 0)
        self.assertAlmostEqual(180., features['mean_f0'], delta=4.)
        self.assertNotIn('unknown pitch', profiler.interpret_features(features))

    def test_legacy_zero_and_invalid_pitch_do_not_export_as_a_known_gender(self):
        for value in (0, None, -1, float('nan'), float('inf'), True, '0'):
            with self.subTest(value=value):
                self.assertEqual('unknown', get_pitch_gender_estimate(value))
                row = profiler.profile_csv_row({'voice_profile': 'Legacy profile.', 'voice_features': {'mean_f0': value}})
                self.assertEqual('unknown', row['gender_est'])

    def test_naming_preserves_explicit_register_and_measured_pitch(self):
        self.assertEqual('warm_baritone_40s_m', naming.derive_base_slug('Warm baritone, 40s.', 0))
        self.assertEqual('warm_f', naming.derive_base_slug('Warm voice.', 180))
        self.assertEqual('warm_m', naming.derive_base_slug('Warm voice.', 120))
        self.assertEqual('warm_unknown', naming.derive_base_slug('Warm voice.', 0))
