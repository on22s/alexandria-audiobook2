"""Windows retirement state-machine checks; native handles are qualified on Windows."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from export_staging import MARKER
from export_staging_directory import identity as _stat_identity, stage_directory, stage_owner_lock
from windows_export_staging_retirement import (DIRECTORY, WindowsRetirement,
                                                reclaim_windows_retired_slots)


CREATE_CHILD = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
from windows_export_staging_retirement import WindowsRetirement
with WindowsRetirement(sys.argv[2], wait=True, create=True) as retirement:
    path, identity, marker, owner, stream, slot = retirement.initialize_creation('mp3')
    (path / 'payload.bin').write_bytes(b'private payload')
    print(retirement.registry / slot, flush=True)
    os._exit(76)
'''


class WindowsRetirementTests(unittest.TestCase):
    def test_hard_exit_recovery_removes_only_the_abandoned_private_slot(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            result = subprocess.run(
                [sys.executable, '-c', CREATE_CHILD, str(Path(__file__).parents[1]), str(root)],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1])})
            self.assertEqual(76, result.returncode, result.stderr[-2000:])
            slot = Path(result.stdout.strip().splitlines()[-1])
            self.assertTrue(slot.is_dir())
            self.assertTrue((slot / 'stage' / 'payload.bin').is_file())
            reclaimed = reclaim_windows_retired_slots(root, max_remove=10)
            self.assertEqual([str(slot / 'stage')], reclaimed['removed'])
            self.assertFalse(slot.exists())

    def test_live_owner_is_preserved_then_finished(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            with WindowsRetirement(root, wait=True, create=True) as retirement:
                path, identity, marker, owner, stream, slot = retirement.initialize_creation('mp3')
            (path / 'payload.bin').write_bytes(b'live payload')
            reclaimed = reclaim_windows_retired_slots(root, max_remove=10)
            self.assertFalse(reclaimed['removed'])
            self.assertEqual(b'live payload', (path / 'payload.bin').read_bytes())
            with WindowsRetirement(root, wait=True, create=False) as retirement:
                retirement.open_slot(slot)
                stage = _stage(path, identity)
                try:
                    retirement.cleanup_initialized_creation(
                        stage, stream, release_owner=lambda: owner.__exit__(None, None, None))
                finally:
                    stage.__exit__(None, None, None)
                retirement.discard_empty_slot()
            self.assertFalse(path.exists())
            self.assertFalse((root / DIRECTORY / slot).exists())

    def test_replaced_empty_stage_is_preserved(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            with WindowsRetirement(root, wait=True, create=True) as retirement:
                path, identity, marker, owner, stream, slot = retirement.initialize_creation('mp3')
                owner.__exit__(None, None, None)
                for entry in path.iterdir():
                    entry.unlink()
                path.rmdir()
                path.mkdir()
            reclaimed = reclaim_windows_retired_slots(root, max_remove=10)
            self.assertFalse(reclaimed['removed'])
            self.assertTrue(path.is_dir())

    def test_foreign_and_unowned_slots_are_preserved(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            with WindowsRetirement(root, wait=True, create=True) as retirement:
                retirement.allocate_slot()
                slot = retirement.registry / retirement.slot
                (slot / 'foreign.txt').write_bytes(b'foreign')
            reclaimed = reclaim_windows_retired_slots(root, max_remove=10)
            self.assertFalse(reclaimed['removed'])
            self.assertEqual(b'foreign', (slot / 'foreign.txt').read_bytes())

    def test_partial_creation_and_owner_only_stage_are_recovered(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            with WindowsRetirement(root, wait=True, create=True) as retirement:
                retirement.allocate_slot()
                slot = retirement.slot
                slot_path = retirement.registry / slot
                allocation = {
                    'schema': 1, 'kind': 'mp3', 'token': 'a' * 32,
                    'parent_identity': list(_stat_identity(os.stat(root))),
                    'slot_identity': list(_stat_identity(os.stat(slot_path))),
                }
                retirement._write_receipt('allocation.json', allocation)
                (slot_path / 'stage').mkdir()
                creation = {**allocation, 'stage_identity': list(_stat_identity(os.stat(slot_path / 'stage')))}
                retirement._write_receipt('creation.json', creation)
            reclaimed = reclaim_windows_retired_slots(root, max_remove=10)
            self.assertEqual([str(root / DIRECTORY / slot)], reclaimed['removed'])
            self.assertFalse((root / DIRECTORY / slot).exists())

    def test_owner_only_stage_after_interrupted_cleanup_is_recovered(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            with WindowsRetirement(root, wait=True, create=True) as retirement:
                path, identity, marker, owner, stream, slot = retirement.initialize_creation('mp3')
            (path / 'payload.bin').write_bytes(b'payload')
            (path / MARKER).unlink()
            (path / 'payload.bin').unlink()
            owner.__exit__(None, None, None)
            reclaimed = reclaim_windows_retired_slots(root, max_remove=10)
            self.assertEqual([str(root / DIRECTORY / slot / 'stage')], reclaimed['removed'])
            self.assertFalse((root / DIRECTORY / slot).exists())

    def test_malformed_creation_receipt_preserves_the_slot(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            with WindowsRetirement(root, wait=True, create=True) as retirement:
                path, identity, marker, owner, stream, slot = retirement.initialize_creation('mp3')
                (path / 'payload.bin').write_bytes(b'private payload')
                slot_path = retirement.registry / slot
                owner.__exit__(None, None, None)
            (slot_path / 'creation.json').write_text('{broken', encoding='utf-8')
            reclaimed = reclaim_windows_retired_slots(root, max_remove=10)
            self.assertFalse(reclaimed['removed'])
            self.assertTrue((slot_path / 'creation.json').exists())
            self.assertEqual(b'private payload', (slot_path / 'stage' / 'payload.bin').read_bytes())


def _stage(path, identity):
    context = stage_directory(path, identity)
    context.__enter__()
    return context


if __name__ == '__main__':
    unittest.main()
