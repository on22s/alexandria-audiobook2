"""Run the actual casting library handlers (#1532) with the existing dependency-free DOM fixture."""
from pathlib import Path
import json
import subprocess
import unittest


class CastingUiTests(unittest.TestCase):
    def test_filters_text_rendering_marks_cast_flow_and_review_keys(self):
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(['node', str(Path(__file__).with_name('casting_ui_fixture.js')), str(root)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt['passed'])
        self.assertGreaterEqual(receipt['assertions'], 35)


if __name__ == '__main__':
    unittest.main()
