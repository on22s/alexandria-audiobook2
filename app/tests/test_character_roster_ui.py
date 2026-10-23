"""Run actual frontend handlers with the existing dependency-free DOM fixture."""
from pathlib import Path
import json
import subprocess
import unittest


class CharacterRosterUiTests(unittest.TestCase):
    def test_identity_actions_import_mapping_and_request_book_fences(self):
        root=Path(__file__).resolve().parents[2]
        result=subprocess.run(['node',str(Path(__file__).with_name('character_roster_ui_fixture.js')),str(root)],
                              capture_output=True,text=True,timeout=20)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        receipt=json.loads(result.stdout)
        self.assertTrue(receipt['passed']);self.assertGreaterEqual(receipt['assertions'],30)


if __name__=='__main__':unittest.main()
