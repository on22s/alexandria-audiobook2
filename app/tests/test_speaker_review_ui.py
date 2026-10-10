"""Exercise actual review UI handlers with a synthetic DOM and API."""
from pathlib import Path
import json
import subprocess
import unittest

class SpeakerReviewUiTests(unittest.TestCase):
    def test_review_lifecycle_and_stale_book_guards(self):
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(['node', str(Path(__file__).with_name('speaker_review_ui_fixture.js')), str(root)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(77, json.loads(result.stdout)['assertions'])
