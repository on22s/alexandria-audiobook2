"""Run the casting research module (#1532) with the production app handlers.

The fixture drives app-casting-research.js and app-casting.js together against
the existing dependency-free DOM; it is handler coverage, not browser QA.
"""
from pathlib import Path
import json
import shutil
import subprocess
import unittest


NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'node is not installed')
class CastingResearchJsTests(unittest.TestCase):
    def run_fixture(self, name):
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run([NODE, str(Path(__file__).with_name(name)), str(root)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_research_module_covers_filters_bulk_undo_views_paging_and_playback(self):
        receipt = self.run_fixture('casting_research_fixture.js')
        self.assertTrue(receipt['passed'])
        self.assertGreaterEqual(receipt['assertions'], 100)

    def test_app_casting_still_works_without_the_research_module(self):
        receipt = self.run_fixture('casting_ui_fixture.js')
        self.assertTrue(receipt['passed'])
        self.assertGreaterEqual(receipt['assertions'], 70)


if __name__ == '__main__':
    unittest.main()
