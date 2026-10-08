"""Execute production voice-card code rather than mocked state API rows."""
from pathlib import Path
import subprocess
import unittest


class StatePersonaCardTests(unittest.TestCase):
    def test_real_cards_and_actions_keep_version_and_book_identity(self):
        tests = Path(__file__).parent
        result = subprocess.run(['node', str(tests / 'state_persona_cards_fixture.js'),
                                 str(tests.parent / 'static/js/app-core.js')],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
