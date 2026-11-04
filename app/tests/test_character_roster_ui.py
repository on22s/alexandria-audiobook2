"""Run actual frontend handlers with the existing dependency-free DOM fixture."""
from pathlib import Path
import json
import subprocess
import sys
import unittest


class CharacterRosterUiTests(unittest.TestCase):
    def test_import_target_mutations_preserve_explicit_mapping(self):
        root = Path(__file__).resolve().parents[2]
        for action in ('split', 'retire'):
            with self.subTest(action=action):
                result = subprocess.run(
                    ['node', str(Path(__file__).with_name('character_roster_ui_fixture.js')),
                     str(root), action, sys.executable], capture_output=True, text=True, timeout=20)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertTrue(json.loads(result.stdout)['passed'])

    def test_identity_actions_import_mapping_and_request_book_fences(self):
        root=Path(__file__).resolve().parents[2]
        result=subprocess.run(['node',str(Path(__file__).with_name('character_roster_ui_fixture.js')),str(root)],
                              capture_output=True,text=True,timeout=20)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        receipt=json.loads(result.stdout)
        self.assertTrue(receipt['passed']);self.assertGreaterEqual(receipt['assertions'],30)


if __name__=='__main__':unittest.main()
