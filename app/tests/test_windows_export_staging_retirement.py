"""Windows retirement state-machine checks; native handles are qualified on Windows."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import export_staging
from export_staging import MARKER, get_encoder_staging_source_root
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
    def test_admitted_stage_constructor_uses_export_parent(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            with WindowsRetirement(root, wait=True, create=True) as retirement:
                path, expected, marker, owner, stream, slot = retirement.initialize_creation('mp3')
            try:
                with stage_directory(path, expected) as authority:
                    with WindowsRetirement(authority=authority, create=False) as admitted:
                        self.assertEqual(root, admitted.parent)
                        admitted.open_slot(slot)
                        _, created = admitted._read_creation()
                        self.assertEqual('mp3', created['kind'])
            finally:
                owner.__exit__(None, None, None)

    @unittest.skipIf(os.name == 'nt', 'Creating a test symlink needs Windows developer privileges')
    def test_linked_receipt_cannot_authorize_recovery(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            with WindowsRetirement(root, wait=True, create=True) as retirement:
                path, expected, marker, owner, stream, slot = retirement.initialize_creation('mp3')
                (path / 'payload.bin').write_bytes(b'preserve')
                owner.__exit__(None, None, None)
            receipt = root / DIRECTORY / slot / 'allocation.json'
            external = root / 'external-receipt.json'
            external.write_bytes(receipt.read_bytes())
            receipt.unlink()
            receipt.symlink_to(external)
            reclaimed = reclaim_windows_retired_slots(root, max_remove=10)
            self.assertFalse(reclaimed['removed'])
            self.assertEqual(b'preserve', (path / 'payload.bin').read_bytes())
            self.assertTrue(receipt.is_symlink())

    def test_encoder_refuses_populated_rollback_backup(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            with WindowsRetirement(root, wait=True, create=True) as retirement:
                path, expected, marker, owner, stream, slot = retirement.initialize_creation('encode')
            try:
                (path / 'old').mkdir()
                (path / 'old' / 'pending.wav').write_bytes(b'rollback')
                windows_os = type('WindowsOs', (), {
                    'name': 'nt', '__getattr__': lambda _self, name: getattr(os, name)})()
                with patch.object(export_staging, 'os', windows_os):
                    with self.assertRaisesRegex(ValueError, 'awaits recovery'):
                        get_encoder_staging_source_root(path)
            finally:
                owner.__exit__(None, None, None)

    def test_windows_top_level_marker_collision_still_uses_private_stage(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            (root / MARKER).write_bytes(b'user project file')
            legacy = root / '.mp3-merge-old'
            legacy.mkdir()
            (legacy / MARKER).write_bytes(b'legacy stage marker')
            private = root / DIRECTORY / ('a' * 32) / 'stage'
            private.mkdir(parents=True)
            private_work = private / 'work'
            private_work.mkdir()
            legacy_work = legacy / 'work'
            legacy_work.mkdir()
            windows_os = type('WindowsOs', (), {'name': 'nt'})()
            with patch.object(export_staging, 'os', windows_os), \
                    patch.object(export_staging, '_create_windows_export_staging', return_value='private') as create_private, \
                    patch.object(export_staging, '_create_public_export_staging', return_value='public') as create_public:
                self.assertEqual('private', export_staging.create_export_staging(root, 'mp3'))
                self.assertEqual('public', export_staging.create_export_staging(legacy, 'encode'))
                self.assertEqual('public', export_staging.create_export_staging(private, 'encode'))
                self.assertEqual('public', export_staging.create_export_staging(legacy_work, 'encode'))
                self.assertEqual('public', export_staging.create_export_staging(private_work, 'encode'))
                self.assertEqual(1, create_private.call_count)
                self.assertEqual(4, create_public.call_count)

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
                # Keep the original inode alive so the filesystem cannot
                # recycle it for the replacement directory.
                path.rename(root / 'retained-original-stage')
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

    def test_empty_slot_validates_every_receipt_before_removing_any(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            with WindowsRetirement(root, wait=True, create=True) as retirement:
                retirement.allocate_slot()
                slot = retirement.registry / retirement.slot
                retirement._write_receipt('allocation.json', {
                    'schema': 1, 'kind': 'mp3', 'token': 'a' * 32,
                    'parent_identity': list(_stat_identity(os.stat(root))),
                    'slot_identity': list(_stat_identity(os.stat(slot))),
                })
                (slot / 'creation.json').write_bytes(b'{malformed')
            reclaimed = reclaim_windows_retired_slots(root, max_remove=10)
            self.assertFalse(reclaimed['removed'])
            self.assertTrue((slot / 'allocation.json').exists())
            self.assertEqual(b'{malformed', (slot / 'creation.json').read_bytes())

            (slot / 'creation.json').unlink()
            (slot / 'owner.retained').write_bytes(b'foreign')
            reclaimed = reclaim_windows_retired_slots(root, max_remove=10)
            self.assertFalse(reclaimed['removed'])
            self.assertTrue((slot / 'allocation.json').exists())
            self.assertEqual(b'foreign', (slot / 'owner.retained').read_bytes())

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
