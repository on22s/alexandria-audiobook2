"""Real process death during cast/state/receipt publication must roll back all."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books

from routers import script
from tests.test_recovery_validation_matrix import _Harness, _factory, _payloads


class ProcessDeathCastRollback(unittest.TestCase):
    def test_cast_is_published_inside_the_state_receipt_transaction(self):
        harness = _Harness({'case': 'atomic-cast'}, cast=True)
        self.addCleanup(harness.close)
        captured = {}
        original = script.apply_book_state_locked

        def capture(*args, **kwargs):
            captured['replacements'] = args[1]
            return original(*args, **kwargs)

        with patch.object(script, 'apply_book_state_locked', side_effect=capture):
            command, _, _, _ = harness.admit()
        self.assertIn('script_run_cast.json', captured['replacements'])
        self.assertIn('script_run_spec.json', captured['replacements'])
        self.assertIn('state.json', captured['replacements'])

    def test_death_before_and_between_rows_preserves_cast(self):
        child = Path(__file__).resolve().parent / '_review6_crash_child.py'
        appdir = str(Path(__file__).resolve().parent.parent)
        for boundary, code in (('before', 95), ('mid', 96)):
            with self.subTest(boundary=boundary):
                harness = _Harness({'case': 'crash-cast'}, cast=True)
                self.addCleanup(harness.close)
                command, _, _, _ = harness.admit()
                harness.worker(command, _factory(_payloads(), fail=('attribute',)))
                cast_path = harness.root / 'script_run_cast.json'
                old = harness.snapshot()
                old_cast = cast_path.read_bytes()
                next(iter((harness.root / 'cast_lists').glob('*.json'))).write_bytes(
                    b'{"cast":[{"name":"OTHER"}]}')
                result = subprocess.run(
                    [sys.executable, str(child), str(harness.root), boundary],
                    cwd=appdir, capture_output=True, text=True, timeout=60,
                    env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'})
                self.assertEqual(code, result.returncode, result.stderr[-500:])
                with books.ensure_book_state(str(harness.root)):
                    pass
                new = harness.snapshot()
                self.assertEqual(old, new, 'receipt/state/checkpoint/manifest must be unchanged')
                self.assertEqual(old_cast, cast_path.read_bytes(),
                                 'the replacement cast must roll back with the transaction')


if __name__ == '__main__':
    unittest.main()
