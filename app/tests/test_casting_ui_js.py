"""Run the actual casting library handlers (#1532) with the existing dependency-free DOM fixture."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest


class CastingUiTests(unittest.TestCase):
    def test_filters_text_rendering_marks_cast_flow_and_review_keys(self):
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(['node', str(Path(__file__).with_name('casting_ui_fixture.js')), str(root)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt['passed'])
        self.assertGreaterEqual(receipt['assertions'], 70)

    def test_scale_fixture_pages_catalogue_and_counts_every_voice_once(self):
        root = Path(__file__).resolve().parents[2]
        voices = [{'id': 'designer/' + str(index), 'name': 'Voice ' + str(index), 'book_id': 'Synthetic',
                   'source': 'designer', 'description': 'A synthetic description.', 'ref_text': 'A synthetic line.',
                   'audio_present': False, 'preview_url': None,
                   'tags': {'gender': 'Male', 'ages': ['Adult'], 'tones': [], 'textures': []}} for index in range(828)]
        with tempfile.TemporaryDirectory() as directory:
            catalogue = Path(directory) / 'catalogue.json'
            catalogue.write_text(json.dumps({'voices': voices, 'skipped': []}), encoding='utf-8')
            result = subprocess.run(['node', str(Path(__file__).with_name('casting_scale_fixture.js')), str(root), str(catalogue)],
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(828, receipt['voices'])
        self.assertEqual(828, receipt['health']['missing_audio'])
        self.assertEqual(0, receipt['health']['duplicate_names'])


if __name__ == '__main__':
    unittest.main()
