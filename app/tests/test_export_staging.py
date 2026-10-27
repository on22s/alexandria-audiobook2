"""Ownership-marked export staging survives ordinary exit and reclaims only abandoned work."""
import os
import json
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import export_staging

from export_staging import (ExportStaging, create_export_staging, reclaim_export_staging,
                            MARKER, OWNER_LOCK, PREFIXES)


APP = Path(__file__).resolve().parent.parent
HAS_POSIX_FD_SCAN = (os.name != 'nt' and os.scandir in os.supports_fd
                     and os.stat in os.supports_dir_fd
                     and os.stat in os.supports_follow_symlinks
                     and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW'))
CHILD = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
from export_staging import create_export_staging
staging = create_export_staging(sys.argv[2], sys.argv[3])
(staging.path / 'payload.bin').write_bytes(b'x' * 4096)
print(staging.path, flush=True)
if sys.argv[4] == 'recovery':
    (staging.path / 'old').mkdir()
    (staging.path / 'old' / 'backup.wav').write_bytes(b'keep')
    os._exit(76)
elif sys.argv[4] == 'hold':
    time.sleep(60)
else:
    os._exit(76)
'''


class ExportStagingTests(unittest.TestCase):
    @unittest.skipUnless(HAS_POSIX_FD_SCAN, 'POSIX descriptor-based legacy discovery')
    def test_saved_legacy_scan_identity_must_match_pinned_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target, saved = root / 'target', root / 'saved'
            target.mkdir()
            (target / 'old-only').write_bytes(b'old')
            old_identity = export_staging.identity(os.stat(target))
            old_entries = os.scandir(target)
            target.rename(saved)
            target.mkdir()
            (target / 'new-only').write_bytes(b'new')
            new_identity = export_staging.identity(os.stat(target))
            descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            real_stat = os.stat
            observed = []

            def old_parent_during_maintenance(path, *args, **kwargs):
                if os.fspath(path) == str(target) and not observed:
                    observed.append(True)
                    return real_stat(saved, *args, **kwargs)
                return real_stat(path, *args, **kwargs)

            proxy = type('SwappingOs', (), {
                '__getattr__': lambda _self, name: getattr(os, name),
                'stat': staticmethod(old_parent_during_maintenance)})()
            export_staging._close_scan_iterators()
            with export_staging._scan_lock:
                export_staging._scan_iterators[target] = (old_identity, old_entries)
            try:
                with patch.object(export_staging, 'os', proxy):
                    with export_staging._scan_entries(
                            target, 10, expected_parent_identity=new_identity,
                            pinned_parent_fd=descriptor) as window:
                        names = list(window)
                self.assertEqual([True], observed)
                self.assertEqual(['new-only'], names)
            finally:
                export_staging._close_scan_iterators()
                old_entries.close()
                os.close(descriptor)

    @unittest.skipUnless(HAS_POSIX_FD_SCAN, 'POSIX descriptor-based legacy discovery')
    def test_legacy_scan_missing_fd_capability_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve()
            stage = self._abandoned_public_stage(target)
            payload = stage.path / 'payload.bin'
            for capability in ('_SCANDIR_SUPPORTS_FD', '_STAT_SUPPORTS_DIR_FD',
                               '_STAT_SUPPORTS_NOFOLLOW'):
                with self.subTest(capability=capability):
                    export_staging._close_scan_iterators()
                    with patch.object(export_staging, capability, False):
                        result = reclaim_export_staging([str(target)], max_remove=1, max_scan=10)
                    self.assertEqual([], result['removed'])
                    self.assertTrue(payload.exists())
                    self.assertTrue(any('Safe export directory scanning is unavailable' in error
                                        for error in result['errors']))
                    self.assertNotIn(target, export_staging._scan_iterators)

    @unittest.skipUnless(HAS_POSIX_FD_SCAN, 'POSIX descriptor-based legacy discovery')
    def test_legacy_scan_parent_swap_during_open_recovers_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target, foreign, saved = (root / name for name in ('target', 'foreign', 'saved'))
            target.mkdir()
            foreign.mkdir()
            sentinel = foreign / 'foreign-payload'
            sentinel.write_bytes(b'foreign')
            stage = self._abandoned_public_stage(target)
            payload = stage.path / 'payload.bin'
            opened = []
            real_scandir = os.scandir

            def swap_only_during_open(path):
                if not opened and (isinstance(path, int) or os.fspath(path) == str(target)):
                    target.rename(saved)
                    foreign.rename(target)
                    try:
                        entries = real_scandir(path)
                    finally:
                        target.rename(foreign)
                        saved.rename(target)
                    opened.append(True)
                    return entries
                return real_scandir(path)

            proxy = type('SwappingOs', (), {
                '__getattr__': lambda _self, name: getattr(os, name),
                'scandir': staticmethod(swap_only_during_open)})()
            export_staging._close_scan_iterators()
            try:
                with patch.object(export_staging, 'os', proxy):
                    result = reclaim_export_staging([str(target)], max_remove=1, max_scan=10)
                self.assertEqual([True], opened)
                self.assertEqual([str(stage.path)], result['removed'])
                self.assertFalse(payload.exists())
                self.assertEqual(b'foreign', sentinel.read_bytes())
            finally:
                export_staging._close_scan_iterators()

    @unittest.skipUnless(HAS_POSIX_FD_SCAN, 'POSIX descriptor-based legacy discovery')
    def test_legacy_child_stat_parent_swap_recovers_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target, foreign, saved = (root / name for name in ('target', 'foreign', 'saved'))
            target.mkdir()
            foreign_stage = self._abandoned_public_stage(target, b'foreign')
            name = foreign_stage.path.name
            target.rename(foreign)
            target.mkdir()
            original_stage = self._abandoned_public_stage(target, b'original')
            original_stage.path.rename(target / name)
            original_payload = target / name / 'payload.bin'
            foreign_payload = foreign / name / 'payload.bin'
            real_stat = os.stat
            swapped = []

            def swap_only_during_child_stat(path, *args, **kwargs):
                if (not swapped and kwargs.get('follow_symlinks') is False
                        and (os.fspath(path) == str(target / name)
                             or (kwargs.get('dir_fd') is not None and os.fspath(path) == name))):
                    target.rename(saved)
                    foreign.rename(target)
                    try:
                        info = real_stat(path, *args, **kwargs)
                    finally:
                        target.rename(foreign)
                        saved.rename(target)
                    swapped.append(True)
                    return info
                return real_stat(path, *args, **kwargs)

            proxy = type('SwappingOs', (), {
                '__getattr__': lambda _self, attr: getattr(os, attr),
                'stat': staticmethod(swap_only_during_child_stat)})()
            export_staging._close_scan_iterators()
            try:
                with patch.object(export_staging, 'os', proxy):
                    result = reclaim_export_staging([str(target)], max_remove=1, max_scan=10)
                self.assertEqual([True], swapped)
                self.assertEqual([str(target / name)], result['removed'])
                self.assertFalse(original_payload.exists())
                self.assertEqual(b'foreign', foreign_payload.read_bytes())
            finally:
                export_staging._close_scan_iterators()

    def test_bound_retirement_rejects_registry_creation_before_side_effects(self):
        from export_staging_retirement import PosixRetirement, DIRECTORY
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'cannot create a registry'):
                PosixRetirement(parent=tmp, create=True, expected_registry_identity=(1, 1))
            self.assertFalse((Path(tmp) / DIRECTORY).exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX private retirement registry')
    def test_private_registry_preserves_parent_replaced_before_open(self):
        import export_staging_retirement as retirement
        with tempfile.TemporaryDirectory() as temporary:
            target, foreign, saved = (Path(temporary).resolve() / name for name in ('target', 'foreign', 'saved'))
            target.mkdir()

            def abandon(parent):
                stage = create_export_staging(parent, 'mp3')
                (stage.path / 'payload').write_bytes(b'owned')
                stage._lock.__exit__(None, None, None)
                stage._closed = True
                return stage.path.relative_to(parent)

            # Both registries occupied this pathname at different times, so
            # their marker parent strings alone cannot distinguish them.
            protected = abandon(target)
            target.rename(foreign)
            target.mkdir()
            original = abandon(target)
            real_reclaim = retirement.reclaim_retired_slots
            moved = []

            def swap_before_open(parent, **kwargs):
                if not moved:
                    target.rename(saved)
                    foreign.rename(target)
                    moved.append(True)
                return real_reclaim(parent, **kwargs)

            try:
                export_staging._close_scan_iterators()
                with patch.object(retirement, 'reclaim_retired_slots', side_effect=swap_before_open):
                    result = reclaim_export_staging([str(target)], max_remove=1, max_scan=1)
                self.assertTrue(moved)
                self.assertEqual((saved / original / 'payload').read_bytes(), b'owned')
                self.assertEqual((target / protected / 'payload').read_bytes(), b'owned')
                self.assertTrue(result['errors'], result)
            finally:
                export_staging._close_scan_iterators()

    @unittest.skipIf(os.name == 'nt', 'POSIX private retirement registry')
    def test_private_registry_preserves_parent_replaced_between_slot_opens(self):
        import export_staging_retirement as retirement
        with tempfile.TemporaryDirectory() as temporary:
            target, foreign, saved = (Path(temporary).resolve() / name for name in ('target', 'foreign', 'saved'))
            target.mkdir()

            def abandon(parent):
                with patch.object(retirement.secrets, 'token_hex', side_effect=lambda length: 'a' * (2 * length)):
                    stage = create_export_staging(parent, 'mp3')
                (stage.path / 'payload').write_bytes(b'owned')
                stage._lock.__exit__(None, None, None)
                stage._closed = True
                return stage.path.relative_to(parent)

            # Matching slot and marker names do not make these the same parent.
            protected = abandon(target)
            target.rename(foreign)
            target.mkdir()
            original = abandon(target)
            self.assertEqual(protected, original)
            real_enter = retirement.PosixRetirement.__enter__
            entered = []

            def swap_on_second_open(transaction):
                entered.append(True)
                if len(entered) == 2:
                    target.rename(saved)
                    foreign.rename(target)
                return real_enter(transaction)

            try:
                export_staging._close_scan_iterators()
                with patch.object(retirement.PosixRetirement, '__enter__', new=swap_on_second_open):
                    result = reclaim_export_staging([str(target)], max_remove=1, max_scan=1)
                self.assertGreaterEqual(len(entered), 2)
                self.assertEqual((saved / original / 'payload').read_bytes(), b'owned')
                self.assertEqual((target / protected / 'payload').read_bytes(), b'owned')
                self.assertTrue(result['errors'], result)
            finally:
                export_staging._close_scan_iterators()

    @unittest.skipIf(os.name == 'nt', 'POSIX private retirement registry')
    def test_private_registry_reports_parent_swap_before_discovery(self):
        import export_staging_retirement as retirement
        with tempfile.TemporaryDirectory() as temporary:
            target, foreign, saved = (Path(temporary).resolve() / name for name in ('target', 'foreign', 'saved'))
            target.mkdir()
            foreign.mkdir()
            (foreign / 'keep').write_bytes(b'foreign')
            stage = create_export_staging(target, 'mp3')
            payload = stage.path / 'payload'
            payload.write_bytes(b'original')
            stage._lock.__exit__(None, None, None)
            stage._closed = True
            bound = retirement._reclaim_retired_slots_bound
            swapped = []

            def swap_before_discovery(*args, **kwargs):
                target.rename(saved)
                foreign.rename(target)
                try:
                    return bound(*args, **kwargs)
                finally:
                    target.rename(foreign)
                    saved.rename(target)
                    swapped.append(True)

            with patch.object(retirement, '_reclaim_retired_slots_bound', side_effect=swap_before_discovery):
                result = retirement.reclaim_retired_slots(target, max_remove=1)
            self.assertTrue(swapped)
            self.assertTrue(result['errors'], result)
            self.assertEqual([], result['removed'])
            self.assertEqual(b'original', payload.read_bytes())
            self.assertEqual(b'foreign', (foreign / 'keep').read_bytes())
            self.assertFalse((foreign / retirement.DIRECTORY).exists())
            stable = retirement.reclaim_retired_slots(target, max_remove=1)
            self.assertEqual(1, len(stable['removed']), stable)

    @unittest.skipIf(os.name == 'nt', 'POSIX private retirement registry')
    def test_private_registry_recovery_does_not_recreate_a_moved_registry(self):
        import export_staging_retirement as retirement
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve()
            stage = create_export_staging(target, 'mp3')
            relative_payload = (stage.path / 'payload').relative_to(target / retirement.DIRECTORY)
            (stage.path / 'payload').write_bytes(b'original')
            stage._lock.__exit__(None, None, None)
            stage._closed = True
            registry = target / retirement.DIRECTORY
            saved = target / 'saved-registry'
            real_enter = retirement.PosixRetirement.__enter__
            opened = []

            def move_before_reopen(transaction):
                opened.append(True)
                if len(opened) == 2:
                    registry.rename(saved)
                return real_enter(transaction)

            with patch.object(retirement.PosixRetirement, '__enter__', new=move_before_reopen):
                result = retirement.reclaim_retired_slots(target, max_remove=1)
            self.assertGreaterEqual(len(opened), 2)
            self.assertTrue(result['errors'], result)
            self.assertEqual([], result['removed'])
            self.assertFalse(registry.exists())
            self.assertEqual(b'original', (saved / relative_payload).read_bytes())
            saved.rename(registry)
            stable = retirement.reclaim_retired_slots(target, max_remove=1)
            self.assertEqual(1, len(stable['removed']), stable)

    @unittest.skipIf(os.name == 'nt', 'POSIX private retirement registry')
    def test_private_recovery_preserves_replaced_registry(self):
        import export_staging_retirement as retirement
        for swap_on in (1, 2):
            with self.subTest(swap_on=swap_on), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                registry = root / retirement.DIRECTORY
                first, second = root / 'first', root / 'second'

                def abandon(payload):
                    with patch.object(retirement.secrets, 'token_hex', side_effect=lambda n: 'a' * (2 * n)):
                        stage = create_export_staging(root, 'mp3')
                    (stage.path / 'payload').write_bytes(payload)
                    stage._lock.__exit__(None, None, None)
                    stage._closed = True
                    return (stage.path / 'payload').relative_to(registry)

                original = abandon(b'original')
                registry.rename(first)
                protected = abandon(b'foreign')
                registry.rename(second)
                first.rename(registry)
                self.assertEqual(original, protected)
                real_enter = retirement.PosixRetirement.__enter__
                opened = []

                def swap_before_open(transaction):
                    opened.append(True)
                    if len(opened) == swap_on:
                        registry.rename(first)
                        second.rename(registry)
                    return real_enter(transaction)

                with patch.object(retirement.PosixRetirement, '__enter__', new=swap_before_open):
                    result = retirement.reclaim_retired_slots(root, max_remove=1)
                self.assertGreaterEqual(len(opened), swap_on)
                self.assertTrue(result['errors'], result)
                self.assertEqual([], result['removed'])
                self.assertEqual(b'original', (first / original).read_bytes())
                self.assertEqual(b'foreign', (registry / protected).read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX private retirement registry')
    def test_private_recovery_handles_empty_uninitialized_registry_conservatively(self):
        from contextlib import contextmanager
        import export_staging_retirement as retirement
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            registry = root / retirement.DIRECTORY
            registry.mkdir(mode=0o700)
            empty = retirement.reclaim_retired_slots(root, max_remove=1)
            self.assertEqual({'removed': [], 'preserved': [], 'errors': []}, empty)
            self.assertEqual([], list(registry.iterdir()))
            (registry / 'unknown').write_bytes(b'preserve')
            damaged = retirement.reclaim_retired_slots(root, max_remove=1)
            self.assertTrue(damaged['errors'], damaged)
            self.assertEqual(b'preserve', (registry / 'unknown').read_bytes())
            self.assertFalse((registry / retirement.LOCK).exists())
            (registry / 'unknown').unlink()

            # A normal creation can initialize the interrupted empty registry.
            stage = create_export_staging(root, 'mp3')
            stage.finish()
            self.assertTrue((registry / retirement.LOCK).exists())
            real_lock = retirement.file_lock

            @contextmanager
            def remove_acquired_lock(*args, **kwargs):
                with real_lock(*args, **kwargs):
                    (registry / retirement.LOCK).unlink()
                    yield

            # Losing a lock after acquisition is not the benign first-open case.
            with patch.object(retirement, 'file_lock', new=remove_acquired_lock):
                lost = retirement.reclaim_retired_slots(root, max_remove=1)
            self.assertTrue(lost['errors'], lost)
            self.assertFalse((registry / retirement.LOCK).exists())

            # A swap during the empty scan must be reported, even though the
            # descriptor still points to the original empty registry.
            saved = root / 'saved-registry'
            class SwapScandir:
                def __getattr__(self, name):
                    if name == 'supports_fd':
                        return set(os.supports_fd) | {self.scandir}
                    return getattr(os, name)

                def scandir(self, descriptor):
                    registry.rename(saved)
                    registry.mkdir(mode=0o700)
                    (registry / 'foreign').write_bytes(b'foreign')
                    return os.scandir(descriptor)

            with patch.object(retirement, 'os', SwapScandir()):
                changed = retirement.reclaim_retired_slots(root, max_remove=1)
            self.assertTrue(changed['errors'], changed)
            self.assertEqual(b'foreign', (registry / 'foreign').read_bytes())
            self.assertEqual([], list(saved.iterdir()))

    @unittest.skipIf(os.name == 'nt', 'POSIX private retirement registry')
    def test_other_reaper_completes_discovered_slot_without_phantom_error(self):
        from export_staging_retirement import PosixRetirement, reclaim_retired_slots
        with tempfile.TemporaryDirectory() as root:
            stage = create_export_staging(root, 'mp3')
            (stage.path / 'payload').write_bytes(b'abandoned owned output')
            stage._lock.__exit__(None, None, None)
            stage._closed = True
            close = PosixRetirement.__exit__
            peer = []
            def finish_discovery(registry, *error):
                close(registry, *error)
                if not peer:
                    peer.append(None)
                    peer[0] = reclaim_retired_slots(root, max_remove=1)
            with patch.object(PosixRetirement, '__exit__', new=finish_discovery):
                result = reclaim_retired_slots(root, max_remove=1)
            self.assertEqual([str(stage.path)], peer[0]['removed'])
            self.assertEqual({'removed':[], 'preserved':[], 'errors':[]}, result)
            self.assertFalse(stage.path.exists())

    def test_ordinary_render_named_destination_remains_usable(self):
        with tempfile.TemporaryDirectory() as root:
            parent = Path(root) / 'render-my-audiobook'
            parent.mkdir()
            sentinel = parent / 'book.txt'
            sentinel.write_bytes(b'original authored text')
            with create_export_staging(parent, 'mp3') as stage:
                (stage.path / 'encoded-part').write_bytes(b'owned bytes')
            self.assertFalse(stage.path.exists())
            self.assertEqual(b'original authored text', sentinel.read_bytes())
            if os.name != 'nt':
                # An actual owner fingerprint still refuses an unmarked stage,
                # even when its nonhidden prefix resembles an ordinary name.
                orphan = Path(root) / 'render-unmarked-owner'
                orphan.mkdir()
                (orphan / (OWNER_LOCK + '.lock')).touch()
                with self.assertRaisesRegex(OSError, 'marker is missing'):
                    create_export_staging(orphan, 'encode')
                self.assertEqual({OWNER_LOCK + '.lock'}, {p.name for p in orphan.iterdir()})

    @unittest.skipIf(os.name == 'nt', 'POSIX registry admission')
    def test_private_parent_recovery_guard_refuses_nested_allocation(self):
        with tempfile.TemporaryDirectory() as root:
            outer = create_export_staging(root, 'chapter')
            try:
                (outer.path / export_staging.RECOVERY).write_bytes(b'preserve recovery')
                (outer.path / 'keep.bin').write_bytes(b'preserve payload')
                before = {str(p.relative_to(outer.path)):p.read_bytes()
                          for p in outer.path.rglob('*') if p.is_file()}
                with self.assertRaisesRegex(OSError, 'Parent staging authority changed'):
                    create_export_staging(outer.path, 'encode')
                self.assertEqual(before, {str(p.relative_to(outer.path)):p.read_bytes()
                                         for p in outer.path.rglob('*') if p.is_file()})
                self.assertFalse((outer.path / '.aa2-export-retirement').exists())
            finally:
                (outer.path / export_staging.RECOVERY).unlink()
                outer.finish(remove=False)

    def test_windows_recursion_preserves_nested_registry_before_metadata_access(self):
        from windows_export_staging_directory import WindowsStageDirectory
        from export_staging_retirement import DIRECTORY
        with tempfile.TemporaryDirectory() as root:
            parent = Path(root)
            registry = parent / DIRECTORY
            stage = registry / ('1' * 32) / 'stage'
            stage.mkdir(parents=True)
            (registry / 'owner.lock').write_bytes(b'owned registry lock')
            (stage.parent / 'creation.json').write_bytes(b'creation receipt')
            (stage / MARKER).write_bytes(b'owned stage marker')
            (stage / 'keep.bin').write_bytes(b'live nested bytes')
            before = {str(p.relative_to(parent)):p.read_bytes()
                      for p in parent.rglob('*') if p.is_file()}
            authority = WindowsStageDirectory.__new__(WindowsStageDirectory)
            authority.check = lambda: None
            authority.api = None
            with patch('windows_export_staging_directory._Handle',
                       side_effect=AssertionError('registry must not be opened')), \
                    self.assertRaisesRegex(OSError, 'Nested export registry'):
                authority._remove_children(parent, frozenset(), 0)
            self.assertEqual(before, {str(p.relative_to(parent)):p.read_bytes()
                                     for p in parent.rglob('*') if p.is_file()})

    def test_windows_recursion_preserves_nested_stage_before_payload_access(self):
        from windows_export_staging_directory import WindowsStageDirectory
        with tempfile.TemporaryDirectory() as root:
            nested = Path(root) / 'nested'
            nested.mkdir()
            payload = nested / 'keep.bin'
            payload.write_bytes(b'live nested payload')
            authority = WindowsStageDirectory.__new__(WindowsStageDirectory)
            for marker in (MARKER, OWNER_LOCK + '.lock'):
                with self.subTest(marker=marker):
                    metadata = nested / marker
                    metadata.write_bytes(b'torn metadata is still a preservation signal')
                    with patch('windows_export_staging_directory._Handle',
                               side_effect=AssertionError('nested payload must not be opened')), \
                            self.assertRaisesRegex(OSError, 'Nested staging owner'):
                        authority._remove_children(nested, frozenset(), 1)
                    self.assertEqual(b'live nested payload', payload.read_bytes())
                    metadata.unlink()

    @unittest.skipIf(os.name == 'nt', 'POSIX registry admission')
    def test_preservation_guard_survives_registry_admission_failure(self):
        from export_staging_retirement import PosixRetirement
        with tempfile.TemporaryDirectory() as root:
            stage = create_export_staging(root, 'sound_delivery')
            payload = stage.path / 'keep.bin'
            payload.write_bytes(b'retained after failed admission')
            with patch.object(PosixRetirement, '__enter__',
                              side_effect=PermissionError('synthetic registry admission failure')):
                with self.assertRaisesRegex(PermissionError, 'registry admission failure'):
                    stage.finish(preserve_on_error=True)
            self.assertTrue((stage.path / export_staging.RECOVERY).exists())
            result = reclaim_export_staging([root])
            self.assertIn(str(stage.path), result['preserved'])
            self.assertEqual(b'retained after failed admission', payload.read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX legacy recovery fixture')
    def test_failed_public_cleanup_reports_preserved_directory(self):
        with tempfile.TemporaryDirectory() as root:
            stage = export_staging._create_public_export_staging(root, 'encode')
            payload = stage.path / 'keep.bin'
            payload.write_bytes(b'retained after failed cleanup')
            stage._lock.__exit__(None, None, None)
            with patch.object(export_staging, '_cleanup_admitted',
                              side_effect=PermissionError('synthetic cleanup failure')):
                result = reclaim_export_staging([root])
            self.assertIn(str(stage.path), result['preserved'])
            self.assertTrue(result['errors'])
            self.assertEqual([], result['removed'])
            self.assertEqual(b'retained after failed cleanup', payload.read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX nested registry ownership')
    def test_delayed_nested_allocator_refuses_replaced_parent_after_cleanup(self):
        import select
        child = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from export_staging import create_export_staging
from export_staging_retirement import PosixRetirement
original = PosixRetirement.__enter__
def delayed(self):
    if self.parent == Path(sys.argv[2]):
        print('waiting-for-owner-registry', flush=True)
        if sys.stdin.readline().strip() != 'continue':
            raise RuntimeError('Missing explicit test release')
    return original(self)
PosixRetirement.__enter__ = delayed
try:
    create_export_staging(Path(sys.argv[3]), 'encode')
except OSError as error:
    print(json.dumps({'status':'refused','error':type(error).__name__}), flush=True)
else:
    raise RuntimeError('Delayed allocator admitted replaced outer stage')
'''
        with tempfile.TemporaryDirectory() as root:
            outer = create_export_staging(root, 'chapter')
            process = subprocess.Popen([sys.executable, '-c', child, str(APP), root, str(outer.path)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(select.select([process.stdout], [], [], 15)[0])
                self.assertEqual('waiting-for-owner-registry', process.stdout.readline().strip())
                outer.finish()
                outer.path.mkdir(parents=True)
                (outer.path / 'keep.bin').write_bytes(b'foreign replacement')
                stdout, stderr = process.communicate('continue\n', timeout=25)
                self.assertEqual(0, process.returncode, stderr)
                self.assertEqual('refused', json.loads(stdout)['status'])
                self.assertEqual(b'foreign replacement', (outer.path / 'keep.bin').read_bytes())
                self.assertEqual({'keep.bin'}, {entry.name for entry in outer.path.iterdir()})
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
                outer.finish(remove=False)

    @unittest.skipIf(os.name == 'nt', 'POSIX nested registry ownership')
    def test_unlocked_nested_registry_with_payload_is_preserved(self):
        from export_staging_retirement import DIRECTORY, LOCK
        with tempfile.TemporaryDirectory() as root:
            outer = create_export_staging(root, 'chapter')
            with create_export_staging(outer.path, 'encode'):
                pass
            (outer.path / DIRECTORY / LOCK).unlink()
            retained = outer.path / DIRECTORY / 'keep.bin'
            retained.write_bytes(b'unproven registry payload')
            with self.assertRaisesRegex(OSError, 'retained contents'):
                outer.finish()
            self.assertEqual(b'unproven registry payload', retained.read_bytes())
            reclaim_export_staging([root])
            self.assertEqual(b'unproven registry payload', retained.read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX nested registry ownership')
    def test_outer_cleanup_preserves_live_public_nested_stage(self):
        with tempfile.TemporaryDirectory() as root:
            outer = create_export_staging(root, 'chapter')
            inner = export_staging._create_public_export_staging(outer.path, 'encode')
            payload = inner.path / 'live-payload'
            payload.write_bytes(b'live legacy payload')
            try:
                with self.assertRaisesRegex(OSError, 'Nested staging owner'):
                    outer.finish()
                self.assertEqual(b'live legacy payload', payload.read_bytes())
                inner.finish()
                result = reclaim_export_staging([root])
                self.assertEqual([], result['errors'])
                self.assertFalse(outer.path.exists())
            finally:
                inner.finish(remove=False)
                outer.finish(remove=False)

    @unittest.skipIf(os.name == 'nt', 'POSIX nested registry ownership')
    def test_staging_named_parent_without_marker_refuses_nested_allocation(self):
        with tempfile.TemporaryDirectory() as root:
            parent = Path(root) / '.pcm-encode-missing-marker'
            parent.mkdir()
            with self.assertRaisesRegex(OSError, 'Parent staging marker is missing'):
                create_export_staging(parent, 'encode')
            self.assertEqual([], list(parent.iterdir()))

    @unittest.skipIf(os.name == 'nt', 'POSIX nested registry ownership')
    def test_full_registry_keeps_a_bounded_nested_retirement_reserve(self):
        import export_staging_retirement as retirement
        from tests.export_staging_assertions import delivery_names
        with tempfile.TemporaryDirectory() as root, patch.object(retirement, 'MAX_SLOTS', 2):
            first = create_export_staging(root, 'chapter')
            second = create_export_staging(root, 'chapter')
            try:
                for outer in (first, second):
                    with create_export_staging(outer.path, 'encode'):
                        pass
                with self.assertRaisesRegex(OSError, 'capacity exhausted'):
                    create_export_staging(root, 'chapter')
                first.finish()
                second.finish()
                self.assertEqual(set(), delivery_names(self, root))
            finally:
                first.finish(remove=False)
                second.finish(remove=False)

    @unittest.skipIf(os.name == 'nt', 'POSIX nested registry ownership')
    def test_nested_quarantine_publication_crashes_are_recoverable(self):
        from tests.export_staging_assertions import delivery_names
        child = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
from export_staging import create_export_staging
from export_staging_nested_quarantine import PENDING_RECEIPT, PAYLOAD
from export_staging_retirement import DIRECTORY
outer = create_export_staging(sys.argv[2], 'chapter')
with create_export_staging(outer.path, 'encode'):
    pass
original = os.open
def interrupted(name, *args, **kwargs):
    descriptor = original(name, *args, **kwargs)
    if name == PENDING_RECEIPT and sys.argv[3] != 'moved':
        if sys.argv[3] == 'partial':
            os.write(descriptor, b'{')
            os.fsync(descriptor)
        os._exit(76)
    return descriptor
os.open = interrupted
os.supports_dir_fd = os.supports_dir_fd | {interrupted}
rename = os.rename
def moved(source, target, *args, **kwargs):
    result = rename(source, target, *args, **kwargs)
    if sys.argv[3] == 'moved' and source == DIRECTORY and target == PAYLOAD:
        os._exit(76)
    return result
os.rename = moved
os.supports_dir_fd = os.supports_dir_fd | {moved}
outer.finish()
raise RuntimeError('Expected pending-receipt hard exit')
'''
        for phase in ('opened', 'partial', 'moved'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                result = subprocess.run([sys.executable, '-c', child, str(APP), root, phase],
                                        capture_output=True, text=True, timeout=25)
                self.assertEqual(76, result.returncode, result.stderr)
                recovered = reclaim_export_staging([root])
                self.assertEqual([], recovered['errors'])
                self.assertEqual([], recovered['preserved'])
                self.assertEqual(set(), delivery_names(self, root))

    @unittest.skipIf(os.name == 'nt', 'POSIX nested registry ownership')
    def test_nested_registry_finalization_interruption_remains_recoverable(self):
        from export_staging_nested_quarantine import PAYLOAD
        with tempfile.TemporaryDirectory() as root:
            outer = create_export_staging(root, 'chapter')
            with create_export_staging(outer.path, 'encode'):
                pass
            remove = os.rmdir
            def interrupt_registry_remove(name, *args, **kwargs):
                if name == PAYLOAD:
                    raise OSError('synthetic interruption after nested owner retirement')
                return remove(name, *args, **kwargs)
            with patch('export_staging_nested.os.rmdir', side_effect=interrupt_registry_remove) as hooked, \
                    patch.object(os, 'supports_dir_fd', os.supports_dir_fd | {hooked}):
                with self.assertRaisesRegex(OSError, 'synthetic interruption'):
                    outer.finish()
            result = reclaim_export_staging([root])
            self.assertEqual([], result['errors'])
            self.assertFalse(outer.path.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX nested registry ownership')
    def test_nested_registry_replacement_before_quarantine_preserves_foreign_root(self):
        from export_staging_retirement import DIRECTORY
        from export_staging_directory import identity
        for unlocked in (False, True):
            with self.subTest(unlocked=unlocked), tempfile.TemporaryDirectory() as root:
                outer = create_export_staging(root, 'chapter')
                with create_export_staging(outer.path, 'encode'):
                    pass
                if unlocked:
                    (outer.path / DIRECTORY / 'owner.lock').unlink()
                rename = os.rename
                original = Path(root) / 'original-nested-root'
                foreign = []
                def substitute(source, target, *args, **kwargs):
                    if source == DIRECTORY:
                        rename(str(outer.path / DIRECTORY), str(original))
                        (outer.path / DIRECTORY).mkdir(mode=0o700)
                        foreign.append(identity((outer.path / DIRECTORY).stat()))
                    return rename(source, target, *args, **kwargs)
                with patch('export_staging_nested_quarantine.os.rename', side_effect=substitute) as hooked, \
                        patch.object(os, 'supports_dir_fd', os.supports_dir_fd | {hooked}):
                    with self.assertRaisesRegex(OSError, 'mismatch preserved'):
                        outer.finish()
                self.assertEqual(1, len(foreign))
                self.assertTrue(original.is_dir())
                def foreign_locations():
                    return [path for path in Path(root).rglob('*')
                            if path.is_dir() and identity(path.stat()) == foreign[0]]
                self.assertEqual(1, len(foreign_locations()))
                result = reclaim_export_staging([root])
                self.assertTrue(result['preserved'] or result['errors'])
                self.assertEqual(1, len(foreign_locations()))

    @unittest.skipIf(os.name == 'nt', 'POSIX nested registry ownership')
    def test_outer_cleanup_retires_completed_nested_registry(self):
        with tempfile.TemporaryDirectory() as root:
            outer = create_export_staging(root, 'chapter')
            try:
                with create_export_staging(outer.path, 'encode') as inner:
                    (inner.path / 'completed-payload').write_bytes(b'completed')
                outer.finish()
                self.assertFalse(outer.path.exists())
                from tests.export_staging_assertions import delivery_names
                self.assertEqual(set(), delivery_names(self, root))
            finally:
                outer.finish(remove=False)

    @unittest.skipIf(os.name == 'nt', 'POSIX nested registry ownership')
    def test_outer_cleanup_preserves_live_nested_encoder_owner(self):
        with tempfile.TemporaryDirectory() as root:
            outer = create_export_staging(root, 'chapter')
            inner = create_export_staging(outer.path, 'encode')
            payload = inner.path / 'live-payload'
            payload.write_bytes(b'encoder still owns these bytes')
            try:
                with self.assertRaises(OSError):
                    outer.finish()
                self.assertEqual(b'encoder still owns these bytes', payload.read_bytes())
            finally:
                # Release both real locks even when the regression deletes the
                # inner directory. Do not replace the original assertion error.
                try:
                    inner.finish(remove=False)
                except FileNotFoundError:
                    pass
                outer.finish(remove=False)


    @unittest.skipIf(os.name == 'nt', 'POSIX registry lock contention')
    def test_busy_registry_preserves_recovery_and_worker_waits_for_release(self):
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
        from export_staging import get_encoder_staging_source_root
        from export_staging_retirement import DIRECTORY
        child = '''
import sys
from export_staging_retirement import PosixRetirement
with PosixRetirement(parent=sys.argv[1], create=False):
 print('locked', flush=True)
 sys.stdin.readline()
'''
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            with create_export_staging(parent, 'encode') as stage:
                process = subprocess.Popen([sys.executable, '-c', child, str(parent)],
                    cwd=APP, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual('locked', process.stdout.readline().strip())
                    result = reclaim_export_staging([parent])
                    self.assertEqual([], result['errors'])
                    self.assertEqual([], result['removed'])
                    self.assertIn(str(parent / DIRECTORY), result['preserved'])
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(get_encoder_staging_source_root, stage.path)
                        with self.assertRaises(FutureTimeout):
                            future.result(timeout=0.1)
                        process.communicate('\n', timeout=5)
                        self.assertEqual(0, process.returncode)
                        self.assertEqual(parent, future.result(timeout=5))
                finally:
                    if process.poll() is None:
                        process.communicate('\n', timeout=5)
                    process.stdout.close()
                    process.stderr.close()

    @unittest.skipIf(os.name == 'nt', 'POSIX private encoding layout')
    def test_encoder_alias_parent_and_unbound_legacy_slot(self):
        from export_staging import get_encoder_staging_source_root
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / 'actual'; target.mkdir()
            alias = root / 'alias'; alias.symlink_to(target, target_is_directory=True)
            with create_export_staging(alias, 'encode') as stage:
                self.assertEqual(stage.path.resolve(), stage.path)
                self.assertEqual(target, get_encoder_staging_source_root(stage.path))
            fake = root / '.aa2-export-retirement' / ('a' * 32) / '.pcm-encode-unbound'
            fake.mkdir(parents=True)
            with self.assertRaises(ValueError):
                get_encoder_staging_source_root(fake)
            self.assertEqual([], list(fake.iterdir()))
            legacy = root / '.pcm-encode-legacy'; legacy.mkdir()
            self.assertEqual(root, get_encoder_staging_source_root(legacy))

    @unittest.skipIf(os.name == 'nt', 'POSIX private encoding layout')
    def test_encoder_source_root_requires_bound_creation(self):
        from export_staging import get_encoder_staging_source_root
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            with create_export_staging(parent, 'encode') as stage:
                before = {p: p.read_bytes() for p in parent.rglob('*') if p.is_file()}
                self.assertEqual(parent, get_encoder_staging_source_root(stage.path))
                self.assertEqual(before, {p: p.read_bytes() for p in parent.rglob('*') if p.is_file()})
                owner = stage.path.parent / 'owner.json'
                saved = owner.read_bytes()
                try:
                    value = json.loads(saved)
                    value['owner_identity'][1] += 1
                    owner.write_text(json.dumps(value))
                    with self.assertRaises(OSError):
                        get_encoder_staging_source_root(stage.path)
                finally:
                    owner.write_bytes(saved)
            with create_export_staging(parent, 'wave') as stage:
                with self.assertRaises(ValueError):
                    get_encoder_staging_source_root(stage.path)
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / '.aa2-export-retirement' / ('a' * 32) / 'stage'
            fake.mkdir(parents=True)
            with self.assertRaises(OSError):
                get_encoder_staging_source_root(fake)
            self.assertFalse((fake.parents[1] / 'owner.lock').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX durability ordering')
    def test_creation_owner_entry_durable_before_receipt(self):
        from export_staging_retirement import PosixRetirement
        from export_staging_directory import stage_directory, stage_owner_lock
        for mode in ('initialize', 'recover_partial'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                synced = []
                real_sync = os.fsync
                real_write = PosixRetirement._write_receipt
                checked = []
                def sync(fd):
                    real_sync(fd)
                    info = os.fstat(fd)
                    synced.append((info.st_dev, info.st_ino))
                def write(transaction, name, value):
                    if name == 'owner.json':
                        self.assertIn(tuple(value['stage_identity']), synced)
                        checked.append(name)
                    return real_write(transaction, name, value)
                with PosixRetirement(parent=Path(tmp)) as transaction:
                    if mode == 'recover_partial':
                        path, expected = transaction.allocate_creation('mp3', 'a' * 32)
                        with stage_directory(path, expected) as authority:
                            with stage_owner_lock(authority, create=True):
                                pass
                    with patch('os.fsync', side_effect=sync), patch.object(
                            PosixRetirement, '_write_receipt', new=write):
                        if mode == 'initialize':
                            path, expected, marker, owner, stream = transaction.initialize_creation('mp3')
                            owner.__exit__(None, None, None)
                        transaction.recover_creation_slot(transaction.slot)
                    self.assertEqual(['owner.json'], checked)
                    self.assertFalse(path.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX cross-directory retirement')
    def test_legacy_move_syncs_destination_before_source(self):
        from export_staging_retirement import PosixRetirement
        original = PosixRetirement.move
        synced = []
        def move(transaction, stream):
            real_rename, real_sync = os.rename, os.fsync
            moved = []
            def rename(source, destination, **kwargs):
                result = real_rename(source, destination, **kwargs)
                if destination == 'stage':
                    moved.append(True)
                return result
            def sync(fd):
                real_sync(fd)
                if moved and fd in (transaction.slot_fd, transaction.authority.parent_fd):
                    synced.append('destination' if fd == transaction.slot_fd else 'source')
            with patch('os.rename', side_effect=rename), patch('os.fsync', side_effect=sync):
                result = original(transaction, stream)
            self.assertEqual([True], moved)
            self.assertEqual(['destination', 'source'], synced[:2])
            return result
        with tempfile.TemporaryDirectory() as tmp:
            stage = export_staging._create_public_export_staging(tmp, 'mp3')
            try:
                with patch.object(PosixRetirement, 'move', new=move):
                    stage.finish()
            finally:
                stage._lock.__exit__(None, None, None)

    @unittest.skipIf(os.name == 'nt', 'POSIX durable receipt retirement')
    def test_receipt_removal_syncs_dependencies_before_next_unlink(self):
        from export_staging_retirement import PosixRetirement
        original = PosixRetirement.discard_empty_slot
        for factory, expected in ((create_export_staging,
                                   ['owner.json', 'creation.json', 'allocation.json']),
                                  (export_staging._create_public_export_staging,
                                   ['verified.json', 'reserved.json'])):
            with self.subTest(factory=factory.__name__), tempfile.TemporaryDirectory() as tmp:
                removed = []
                def discard(transaction, name):
                    pending = []
                    real_unlink, real_sync, real_rmdir = os.unlink, os.fsync, os.rmdir
                    def rmdir(path, *args, **kwargs):
                        if path == name and kwargs.get('dir_fd') == transaction.fd:
                            self.assertEqual([], pending,
                                             'Receipt deletion must be durable before slot removal')
                        return real_rmdir(path, *args, **kwargs)
                    def unlink(path, *args, **kwargs):
                        if kwargs.get('dir_fd') == transaction.slot_fd:
                            self.assertEqual([], pending,
                                             'Previous receipt deletion is not durable')
                            real_unlink(path, *args, **kwargs)
                            removed.append(path)
                            pending.append(path)
                        else:
                            real_unlink(path, *args, **kwargs)
                    def sync(fd):
                        real_sync(fd)
                        if fd == transaction.slot_fd:
                            pending.clear()
                    with patch('os.unlink', side_effect=unlink), patch('os.fsync', side_effect=sync), \
                            patch('os.rmdir', side_effect=rmdir):
                        original(transaction, name)
                    self.assertEqual([], pending)
                stage = factory(tmp, 'mp3')
                try:
                    with patch.object(PosixRetirement, 'discard_empty_slot', new=discard):
                        stage.finish()
                    self.assertEqual(expected, removed)
                finally:
                    stage._lock.__exit__(None, None, None)

    def test_replacement_during_cleanup_preserves_foreign_directory(self):
        for mode in ('normal', 'recovery'):
            for boundary in ('_remove_contents', 'remove_empty_directory'):
                with self.subTest(mode=mode, boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    parent = root / 'exports'; parent.mkdir()
                    foreign = root / 'foreign'; foreign.mkdir()
                    expected = {MARKER: b'foreign marker', OWNER_LOCK + '.lock': b'foreign lock',
                                'keep.wav': b'foreign audio fixture'}
                    for name, content in expected.items():
                        (foreign / name).write_bytes(content)
                    staging = create_export_staging(parent, 'mp3')
                    (staging.path / 'payload.bin').write_bytes(b'owned payload')
                    if mode == 'recovery':
                        staging._lock.__exit__(None, None, None)
                    from export_staging_directory import stage_directory
                    target = (export_staging if boundary == '_remove_contents' else
                              type(stage_directory(staging.path, staging._directory_identity)))
                    original = getattr(target, boundary)
                    calls = []
                    def retarget(subject, *args, **kwargs):
                        path = Path(subject) if boundary == '_remove_contents' else subject.path
                        calls.append(True)
                        if os.name == 'nt':
                            ancestor_before = parent.stat()
                            with self.assertRaises(OSError) as ancestor_denied:
                                parent.rename(root / 'admitted-export-parent')
                            self.assertIn(ancestor_denied.exception.winerror, (5, 32))
                            ancestor_after = parent.stat()
                            self.assertEqual((ancestor_before.st_dev, ancestor_before.st_ino),
                                             (ancestor_after.st_dev, ancestor_after.st_ino))
                            self.assertFalse((root / 'admitted-export-parent').exists())
                            before = path.stat()
                            with self.assertRaises(OSError) as denied:
                                path.rename(root / 'admitted-stage')
                            self.assertIn(denied.exception.winerror, (5, 32))
                            after = path.stat()
                            self.assertEqual((before.st_dev, before.st_ino),
                                             (after.st_dev, after.st_ino))
                            self.assertFalse((root / 'admitted-stage').exists())
                            # Preserve the interrupted-cleanup outcome while
                            # checking Windows denied the retarget operation.
                            raise denied.exception
                        path.rename(root / 'admitted-stage')
                        path.symlink_to(foreign, target_is_directory=True)
                        return original(subject, *args, **kwargs)
                    with patch.object(target, boundary, new=retarget):
                        if mode == 'normal':
                            try:
                                staging.finish()
                            except OSError:
                                pass
                        else:
                            result = reclaim_export_staging([parent])
                            self.assertEqual([], result['removed'])
                    self.assertEqual([True], calls)
                    if os.name == 'nt':
                        # Both sharing and access denial are valid while pinned.
                        # Prove this was admission, not a permanent ACL failure:
                        # after cleanup releases its handles both moves succeed.
                        parent_id = (parent.stat().st_dev, parent.stat().st_ino)
                        released_parent = root / 'released-export-parent'
                        parent.rename(released_parent)
                        self.assertEqual(parent_id, (released_parent.stat().st_dev,
                                                     released_parent.stat().st_ino))
                        # Windows top-level stages now live below a private
                        # registry slot, not directly below the export parent.
                        released_stage = released_parent / staging.path.relative_to(parent)
                        stage_id = (released_stage.stat().st_dev, released_stage.stat().st_ino)
                        released_stage.rename(root / 'released-stage')
                        moved = (root / 'released-stage').stat()
                        self.assertEqual(stage_id, (moved.st_dev, moved.st_ino))
                    self.assertEqual(expected, {name: (foreign / name).read_bytes()
                                     if (foreign / name).exists() else None for name in expected})

    @unittest.skipIf(os.name == 'nt', 'POSIX retained-owner protocol')
    def test_retained_owner_recovers_each_abrupt_finalization_exit(self):
        from export_staging_retirement import DIRECTORY, LOCK
        child = r'''
import os, sys, json
from pathlib import Path
from export_staging import create_export_staging, _create_public_export_staging, MARKER, OWNER_LOCK
from export_staging_retirement import RETAINED_OWNER
from export_staging_directory import PosixStageDirectory
factory = _create_public_export_staging if sys.argv[3] == 'legacy' else create_export_staging
stage = factory(sys.argv[1], 'mp3')
(stage.path / 'payload').write_bytes(b'owned')
boundary = sys.argv[2]
if sys.argv[3] == 'legacy':
    marker = json.loads((stage.path / MARKER).read_text())
    marker.pop('owner_identity')
    (stage.path / MARKER).write_text(json.dumps(marker))
    (stage.path / (OWNER_LOCK + '.lock')).chmod(0o644)
rename, unlink = os.rename, os.unlink
remove = PosixStageDirectory.remove_empty_directory
def interrupted_rename(src, dst, *args, **kwargs):
    if dst == RETAINED_OWNER and boundary == 'before_owner_move':
        os._exit(77)
    value = rename(src, dst, *args, **kwargs)
    if dst == RETAINED_OWNER and boundary == 'after_owner_move':
        os._exit(77)
    return value
def interrupted_unlink(name, *args, **kwargs):
    value = unlink(name, *args, **kwargs)
    if ((name == MARKER and boundary == 'after_marker_unlink') or
            (name == RETAINED_OWNER and boundary == 'after_owner_unlink')):
        os._exit(77)
    return value
def interrupted_remove(self):
    if boundary == 'before_stage_rmdir':
        os._exit(77)
    value = remove(self)
    if boundary == 'after_stage_rmdir':
        os._exit(77)
    return value
os.supports_dir_fd = set(os.supports_dir_fd) | {interrupted_rename, interrupted_unlink}
os.rename, os.unlink = interrupted_rename, interrupted_unlink
PosixStageDirectory.remove_empty_directory = interrupted_remove
stage.finish()
raise RuntimeError('Crash boundary was not reached')
'''
        boundaries = ('before_owner_move', 'after_owner_move', 'after_marker_unlink',
                      'before_stage_rmdir', 'after_stage_rmdir', 'after_owner_unlink')
        for marker_kind, boundary in ((kind, point) for kind in ('current', 'legacy')
                                      for point in boundaries):
            with self.subTest(marker=marker_kind, boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                completed = subprocess.run([sys.executable, '-c', child, tmp, boundary, marker_kind],
                                           cwd=APP, capture_output=True, text=True, timeout=10)
                self.assertEqual(77, completed.returncode, completed.stderr)
                # A fresh reaper must reopen and lock the original inode. No
                # inherited Python object or graceful finally block survives.
                result = reclaim_export_staging([tmp])
                self.assertEqual([], result['errors'])
                self.assertEqual(1, len(result['removed']))
                self.assertEqual([LOCK], sorted(p.name for p in (Path(tmp) / DIRECTORY).iterdir()))
                self.assertEqual([DIRECTORY], sorted(p.name for p in Path(tmp).iterdir()))

    @unittest.skipIf(os.name == 'nt', 'POSIX retained-owner protocol')
    def test_retained_owner_recovery_preserves_ambiguous_states(self):
        from export_staging_directory import PosixStageDirectory
        from export_staging_retirement import DIRECTORY, LOCK, RETAINED_OWNER
        from utils import file_lock
        from contextlib import ExitStack
        for case in ('missing', 'replaced', 'both', 'symlink', 'hardlink',
                     'payload', 'unknown_slot', 'live_owner', 'receipt_conflict',
                     'legacy_unbound', 'invalid_owner_identity', 'foreign_receipt_parent',
                     'foreign_receipt_prefix'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                stage = export_staging._create_public_export_staging(tmp, 'mp3')
                with patch.object(PosixStageDirectory, 'remove_empty_directory',
                                  side_effect=OSError('injected final removal failure')):
                    with self.assertRaisesRegex(OSError, 'injected'):
                        stage.finish()
                slots = [p for p in (Path(tmp) / DIRECTORY).iterdir() if p.name != LOCK]
                self.assertEqual(1, len(slots))
                slot = slots[0]
                retained = slot / RETAINED_OWNER
                saved = slot / 'stage'
                self.assertEqual(stage.marker['owner_identity'],
                                 [retained.stat().st_dev, retained.stat().st_ino])
                if case == 'missing':
                    retained.unlink()
                elif case in ('replaced', 'symlink'):
                    original = Path(tmp) / 'original-owner'
                    retained.rename(original)
                    if case == 'replaced':
                        retained.write_bytes(b'foreign')
                        retained.chmod(0o600)
                    else:
                        retained.symlink_to(original)
                elif case == 'both':
                    (saved / (OWNER_LOCK + '.lock')).write_bytes(b'foreign owner')
                elif case == 'hardlink':
                    os.link(retained, Path(tmp) / 'other-owner-link')
                elif case == 'payload':
                    (saved / 'foreign.wav').write_bytes(b'foreign audio')
                elif case == 'unknown_slot':
                    (slot / 'foreign.wav').write_bytes(b'foreign audio')
                elif case in ('receipt_conflict', 'legacy_unbound', 'invalid_owner_identity',
                              'foreign_receipt_parent', 'foreign_receipt_prefix'):
                    for name in ('reserved.json', 'verified.json'):
                        receipt = json.loads((slot / name).read_text())
                        if case == 'receipt_conflict':
                            receipt['owner_identity'][1] += 1
                        elif case == 'invalid_owner_identity':
                            receipt['owner_identity'] = [True, 1]
                        elif case == 'foreign_receipt_prefix':
                            receipt['original_name'] = 'foreign-directory'
                        elif case == 'foreign_receipt_parent':
                            marker = json.loads(receipt['marker'])
                            marker['parent'] = str(Path(tmp) / 'foreign')
                            receipt['marker'] = json.dumps(marker)
                        else:
                            receipt.pop('owner_identity')
                            marker = json.loads(receipt['marker'])
                            marker.pop('owner_identity')
                            receipt['marker'] = json.dumps(marker)
                        (slot / name).write_text(json.dumps(receipt))
                elif case == 'live_owner':
                    stream = stack.enter_context(retained.open('r+b'))
                    stack.enter_context(file_lock(retained, timeout=0, lock_handle=stream))
                def snapshot():
                    result = {}
                    for item in slot.rglob('*'):
                        key = str(item.relative_to(slot))
                        if item.is_symlink():
                            result[key] = ('symlink', os.readlink(item))
                        elif item.is_dir():
                            result[key] = ('directory',)
                        else:
                            result[key] = ('file', item.read_bytes(), item.stat().st_ino)
                    return result
                before = snapshot()
                result = reclaim_export_staging([tmp])
                self.assertEqual([], result['removed'])
                self.assertTrue(result['preserved'] or result['errors'])
                self.assertEqual(before, snapshot())

    @unittest.skipIf(os.name == 'nt', 'POSIX retained-owner protocol')
    def test_retained_original_lock_excludes_another_process_until_disposal(self):
        from export_staging_retirement import PosixRetirement, RETAINED_OWNER, DIRECTORY
        child = r'''
import sys
from pathlib import Path
from utils import file_lock
with Path(sys.argv[1]).open('r+b') as stream:
    try:
        with file_lock(sys.argv[1], timeout=0, lock_handle=stream):
            pass
    except TimeoutError:
        raise SystemExit(79)
raise SystemExit('Original lock was not held')
'''
        original = PosixRetirement.check_retained_owner
        checked = []
        def observed(transaction, receipt, stream, **kwargs):
            original(transaction, receipt, stream, **kwargs)
            path = transaction.parent / DIRECTORY / transaction.slot / RETAINED_OWNER
            result = subprocess.run([sys.executable, '-c', child, str(path)],
                                    cwd=APP, capture_output=True, text=True, timeout=10)
            self.assertEqual(79, result.returncode, result.stderr)
            checked.append(True)
        with tempfile.TemporaryDirectory() as tmp:
            stage = create_export_staging(tmp, 'mp3')
            with patch.object(PosixRetirement, 'check_retained_owner', new=observed):
                stage.finish()
            self.assertGreaterEqual(len(checked), 3)

    @unittest.skipIf(os.name == 'nt', 'POSIX retirement protocol')
    def test_unknown_slot_preserves_payload_before_owner_retention(self):
        from export_staging_directory import stage_directory
        from export_staging_retirement import PosixRetirement, DIRECTORY
        injected = ('unknown', 'allocation.json', 'creation.json', 'owner.json',
                    'allocation.json.pending', 'creation.json.pending', 'owner.json.pending')
        for verified, member in ((ready, name) for ready in (False, True) for name in injected):
            with self.subTest(verified=verified, member=member), tempfile.TemporaryDirectory() as tmp:
                stage = export_staging._create_public_export_staging(tmp, 'mp3')
                (stage.path / 'payload.wav').write_bytes(b'preserve audio')
                try:
                    with stage_directory(stage.path, stage._directory_identity) as authority:
                        with PosixRetirement(authority) as transaction:
                            transaction.move(stage._owner_stream)
                            slot = Path(tmp) / DIRECTORY / transaction.slot
                            (slot / member).write_bytes(b'preserve unknown')
                            if not verified:
                                (slot / 'verified.json').unlink()
                finally:
                    stage._lock.__exit__(None, None, None)
                    stage._closed = True
                result = reclaim_export_staging([tmp])
                self.assertEqual([], result['removed'])
                self.assertTrue(result['errors'] or result['preserved'])
                self.assertEqual(b'preserve audio', (slot / 'stage' / 'payload.wav').read_bytes())
                self.assertEqual(b'preserve unknown', (slot / member).read_bytes())

    def test_zero_or_invalid_recovery_budget_never_removes_abandoned_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = create_export_staging(tmp, 'mp3')
            payload = stage.path / 'payload.wav'
            payload.write_bytes(b'preserve audio')
            stage._lock.__exit__(None, None, None)
            stage._closed = True
            for parameter in ('max_remove', 'max_scan', 'max_total_scan'):
                with self.subTest(parameter=parameter):
                    self.assertEqual({'removed': [], 'preserved': [], 'errors': []},
                                     reclaim_export_staging([tmp], **{parameter: 0}))
                    for invalid in (-1, True, 1.5, '1'):
                        with self.assertRaises(ValueError):
                            reclaim_export_staging([tmp], **{parameter: invalid})
                    self.assertEqual(b'preserve audio', payload.read_bytes())
            self.assertEqual([str(stage.path)], reclaim_export_staging([tmp])['removed'])

    def _count_child_stats(self, parent, call):
        """Run `call`, returning (result, distinct names os.stat examined in `parent`).

        Distinct, because admitting a candidate stats its own path again; that
        is admission work on one examined entry, not a second entry.
        """
        real_stat = os.stat
        examined = []
        def counting_stat(path, *args, **kwargs):
            # POSIX discovery stats each name relative to its pinned parent.
            # The Windows branch still supplies the full pathname.
            if (kwargs.get('dir_fd') is not None and isinstance(path, str)
                    and path not in ('.', '..') and '/' not in path):
                examined.append(path)
            elif (isinstance(path, (str, os.PathLike))
                    and os.path.realpath(os.path.dirname(os.fspath(path))) == os.path.realpath(parent)):
                examined.append(os.path.basename(os.fspath(path)))
            return real_stat(path, *args, **kwargs)
        # Only export_staging sees the counting proxy. Patching os.stat itself
        # also replaces it for stage_directory, whose os.supports_dir_fd checks
        # then refuse admission, so the instrument would change the answer.
        proxy = type('CountingOs', (), {
            '__getattr__': lambda _self, name: getattr(os, name),
            'stat': staticmethod(counting_stat)})()
        with patch.object(export_staging, 'os', proxy):
            result = call()
        return result, sorted(set(examined))

    def test_scan_budget_counts_every_examined_entry(self):
        # #1484: max_scan counted only staging-named candidates, so unrelated
        # files and directories in a large book parent were each stat'ed
        # without limit. Every examined entry must count, whatever it is.
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(17):
                if index % 2:
                    (Path(tmp) / f'unrelated-{index:02d}').mkdir()
                else:
                    (Path(tmp) / f'unrelated-{index:02d}.txt').write_bytes(b'keep')
            export_staging._close_scan_iterators()
            consumed = []
            real_scandir = os.scandir
            class CountingEntries:
                def __init__(self, parent):
                    self.entries = real_scandir(parent)
                def __next__(self):
                    entry = next(self.entries)
                    consumed.append(entry.name)
                    return entry
                def close(self):
                    self.entries.close()
            proxy = type('CountingOs', (), {
                '__getattr__': lambda _self, name: getattr(os, name),
                'scandir': staticmethod(CountingEntries)})()
            with patch.object(export_staging, 'os', proxy):
                reclaim_export_staging([tmp], max_scan=1)
            self.assertEqual(1, len(consumed), consumed)
            export_staging._close_scan_iterators()
            result, examined = self._count_child_stats(
                tmp, lambda: reclaim_export_staging([tmp], max_scan=1))
            self.assertEqual(1, len(examined), examined)
            self.assertIn(f'{tmp}: scan limit reached', result['errors'])
            self.assertEqual([], result['removed'])
            # The next call examines a different entry rather than repeating.
            _, following = self._count_child_stats(
                tmp, lambda: reclaim_export_staging([tmp], max_scan=1))
            self.assertEqual(1, len(following), following)
            self.assertNotEqual(examined, following)

        # Repeated spellings identify one parent and share one call budget.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "inner").mkdir()
            for index in range(5):
                (Path(tmp) / str(index)).write_bytes(b"keep")
            alias = Path(tmp, "inner", "..")
            result, examined = self._count_child_stats(
                tmp, lambda: reclaim_export_staging([tmp, alias, tmp], max_scan=1))
            self.assertEqual(1, len(examined))
            self.assertIn(f"{tmp}: scan limit reached", result["errors"])

        # Caller-facing paths retain their spelling even though continuation
        # and duplicate admission use the canonical parent (Windows 8.3 too).
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "inner").mkdir()
            alias = Path(tmp, "inner", "..")
            stage = self._abandoned_public_stage(alias)
            result = reclaim_export_staging([alias, tmp])
            self.assertEqual([str(alias / stage.path.name)], result["removed"])
            self.assertEqual([], result["errors"])
            self.assertFalse(stage.path.exists())

        if os.name != 'nt':
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp, "actual"); target.mkdir()
                alias = Path(tmp, "alias")
                alias.symlink_to(target, target_is_directory=True)
                stage = self._abandoned_public_stage(alias)
                result = reclaim_export_staging([alias, target])
                self.assertEqual([str(alias / stage.path.name)], result["removed"])
                self.assertEqual([], result["errors"])
                self.assertFalse(stage.path.exists())

                private = create_export_staging(alias, 'encode')
                (private.path / 'payload.bin').write_bytes(b'owned encoding')
                private._lock.__exit__(None, None, None)
                private._closed = True
                private_name = private.path.relative_to(target)
                result = reclaim_export_staging([alias, target])
                self.assertEqual([str(alias / private_name)], result["removed"])
                self.assertEqual([], result["errors"])
                self.assertFalse(private.path.exists())

                # Symlink/.. must follow filesystem traversal, not lexical
                # abspath collapsing, when selecting the cleanup authority.
                nested = target / "inner"; nested.mkdir()
                deep_alias = Path(tmp, "deep-alias")
                deep_alias.symlink_to(nested, target_is_directory=True)
                stage = self._abandoned_public_stage(target)
                caller = deep_alias / ".."
                result = reclaim_export_staging([caller, target])
                self.assertEqual([str(caller / stage.path.name)], result["removed"])
                self.assertEqual([], result["errors"])
                self.assertFalse(stage.path.exists())

                other = Path(tmp, "other"); other.mkdir()
                for wrong_parent in (False, True):
                    alias.unlink()
                    alias.symlink_to(target, target_is_directory=True)
                    stage = self._abandoned_public_stage(target)
                    if wrong_parent:
                        nested_stage = create_export_staging(stage.path, 'encode')
                        nested_payload = nested_stage.path / 'nested.bin'
                        nested_payload.write_bytes(b'preserve nested payload')
                        nested_stage._lock.__exit__(None, None, None)
                        nested_stage._closed = True
                        marker_path = stage.path / MARKER
                        marker = json.loads(marker_path.read_text())
                        marker["parent"] = str(other.resolve())
                        marker_path.write_text(json.dumps(marker))
                    matches_parent = export_staging._matches_parent
                    def retarget_caller(marker, admitted_parent):
                        alias.unlink()
                        alias.symlink_to(other, target_is_directory=True)
                        return matches_parent(marker, admitted_parent)
                    with patch.object(export_staging, '_matches_parent',
                                      side_effect=retarget_caller):
                        result = reclaim_export_staging([alias])
                    reported = str(alias / stage.path.name)
                    if wrong_parent:
                        self.assertEqual([], result["removed"])
                        self.assertIn(reported, result["preserved"])
                        self.assertEqual([], result["errors"])
                        self.assertEqual(b'preserve nested payload', nested_payload.read_bytes())
                        self.assertEqual(b'abandoned owned output',
                                         (stage.path / 'payload.bin').read_bytes())
                    else:
                        self.assertEqual([reported], result["removed"])
                        self.assertFalse(stage.path.exists())

    def _abandoned_public_stage(self, parent, payload=b'abandoned owned output'):
        stage = export_staging._create_public_export_staging(parent, 'mp3')
        (stage.path / 'payload.bin').write_bytes(payload)
        stage._lock.__exit__(None, None, None)
        stage._closed = True
        return stage

    def _reclaim_until_removed(self, parent, max_scan, calls):
        """Call recovery up to `calls` times; -> (call number that removed, results)."""
        results = []
        for call in range(1, calls + 1):
            results.append(reclaim_export_staging([parent], max_scan=max_scan))
            if results[-1]['removed']:
                return call, results
        return None, results

    def test_repeated_small_budgets_reach_a_late_abandoned_stage(self):
        # #1484: a per-call cap alone re-examines the same first entries on a
        # stable parent forever. Each call must resume where the last stopped,
        # so every entry is examined within ceil(N / max_scan) calls.
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(17):
                (Path(tmp) / f'-filler-{index:02d}').write_bytes(b'keep')
            stage = self._abandoned_public_stage(tmp)
            # Force a deterministic unrelated-first native iterator so the
            # restart-from-beginning counterfactual genuinely starves.
            real_scandir = os.scandir
            class OrderedEntries:
                def __init__(self, parent):
                    with real_scandir(parent) as entries:
                        self.entries = iter(sorted(entries, key=lambda entry: entry.name))
                def __iter__(self):
                    return self
                def __next__(self):
                    return next(self.entries)
                def close(self):
                    pass
            proxy = type('OrderedOs', (), {
                '__getattr__': lambda _self, name: getattr(os, name),
                'scandir': staticmethod(OrderedEntries)})()
            # One conservative extra call allows the final EOF probe.
            bound = len(os.listdir(tmp)) + 1
            with patch.object(export_staging, "os", proxy):
                call, results = self._reclaim_until_removed(tmp, 1, bound)
            self.assertIsNotNone(call, results[-1])
            self.assertLessEqual(call, bound)
            self.assertEqual([str(stage.path)], results[-1]['removed'])
            self.assertFalse(stage.path.exists())
            for index in range(17):
                self.assertEqual(b'keep', (Path(tmp) / f'-filler-{index:02d}').read_bytes())

            # Different export callers may interleave disjoint parent sets.
            # Each parent must retain its own progress, rather than resetting
            # the other caller at every invocation.
            export_staging._close_scan_iterators()
            later = self._abandoned_public_stage(tmp)
            with tempfile.TemporaryDirectory() as other:
                (Path(other) / "keep").write_bytes(b"keep")
                with patch.object(export_staging, "os", proxy):
                    for _ in range(bound):
                        result = reclaim_export_staging([tmp], max_scan=1)
                        reclaim_export_staging([other], max_scan=1)
                        if result["removed"]:
                            break
                self.assertEqual([str(later.path)], result["removed"])

    def test_preservation_holds_wherever_traversal_meets_the_stage(self):
        from export_staging_directory import stage_directory
        for before, after in ((0, 6), (3, 3), (6, 0)):
            with self.subTest(before=before, after=after), \
                    tempfile.TemporaryDirectory() as tmp:
                for index in range(before):
                    (Path(tmp) / f'-early-{index}').write_bytes(b'keep')
                for index in range(after):
                    (Path(tmp) / f'~late-{index}').write_bytes(b'keep')
                live = export_staging._create_public_export_staging(tmp, 'mp3')
                (live.path / 'payload.bin').write_bytes(b'live')
                mismatched = self._abandoned_public_stage(tmp, b'mismatched')
                marker = json.loads((mismatched.path / MARKER).read_bytes())
                marker['parent'] = str(Path(tmp, 'elsewhere').resolve())
                (mismatched.path / MARKER).write_bytes(json.dumps(marker).encode())
                guarded = self._abandoned_public_stage(tmp, b'guarded')
                with stage_directory(guarded.path, guarded._directory_identity) as authority:
                    authority.create_recovery_guard(export_staging.RECOVERY)
                preserved = [live, mismatched, guarded]
                if os.name != 'nt':
                    linked = self._abandoned_public_stage(tmp, b'linked')
                    moved = Path(tmp, '~marker-target.json')
                    (linked.path / MARKER).rename(moved)
                    (linked.path / MARKER).symlink_to(moved)
                    preserved.append(linked)
                valid = self._abandoned_public_stage(tmp, b'valid')
                try:
                    full_pass = len(os.listdir(tmp)) + 1
                    for _ in range(full_pass):
                        reclaim_export_staging([tmp], max_scan=1)
                    self.assertFalse(valid.path.exists())
                    for stage in preserved:
                        self.assertTrue(stage.path.exists(), stage.path.name)
                        self.assertTrue((stage.path / 'payload.bin').read_bytes())
                finally:
                    live._lock.__exit__(None, None, None)
                    live._closed = True

    def test_invalid_scan_cursor_restarts_within_budget_and_still_progresses(self):
        cases = {
            'missing': None,
            'garbage': b'{not json',
            'wrong schema': {'schema': 2},
            'non-string after': {'schema': 1, 'after_hex': 7},
            'bad hex': {'schema': 1, 'after_hex': 'zz'},
            'foreign parent': {'schema': 1, 'parent_identity': [0, 0],
                               'after_hex': b'-f-2'.hex()},
            'deleted entry': 'deleted',
            'oversized': b' ' * 5000,
        }
        for label, content in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                for index in range(5):
                    (Path(tmp) / f'-f-{index}').write_bytes(b'keep')
                stage = self._abandoned_public_stage(tmp)
                cursor = Path(tmp, export_staging.SCAN_CURSOR)
                if content == 'deleted':
                    info = os.stat(tmp)
                    cursor.write_text(json.dumps({'schema': 1, 'after_hex': b'-f-9-gone'.hex(),
                        'parent_identity': [info.st_dev, info.st_ino]}))
                elif isinstance(content, dict):
                    info = os.stat(tmp)
                    content.setdefault('parent_identity', [info.st_dev, info.st_ino])
                    cursor.write_text(json.dumps(content))
                elif content is not None:
                    cursor.write_bytes(content)
                bound = len(os.listdir(tmp)) + 1
                for call in range(1, bound + 1):
                    result, examined = self._count_child_stats(
                        tmp, lambda: reclaim_export_staging([tmp], max_scan=1))
                    self.assertLessEqual(len(examined), 1, examined)
                    if result['removed']:
                        break
                self.assertEqual([str(stage.path)], result['removed'])
                for index in range(5):
                    self.assertEqual(b'keep', (Path(tmp) / f'-f-{index}').read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX FIFO and directory fixtures')
    def test_cursor_name_held_by_another_node_is_preserved_and_reported(self):
        for kind in ('fifo', 'directory'):
            with self.subTest(kind), tempfile.TemporaryDirectory() as tmp:
                for index in range(3):
                    (Path(tmp) / f'-f-{index}').write_bytes(b'keep')
                cursor = Path(tmp, export_staging.SCAN_CURSOR)
                os.mkfifo(cursor) if kind == 'fifo' else cursor.mkdir()
                stage = self._abandoned_public_stage(tmp)
                _, results = self._reclaim_until_removed(tmp, 1, len(os.listdir(tmp)) + 1)
                self.assertEqual([str(stage.path)], results[-1]['removed'])
                self.assertEqual(kind == 'fifo', stat.S_ISFIFO(cursor.lstat().st_mode))
                self.assertEqual(kind == 'directory', cursor.is_dir())

    @unittest.skipIf(os.name == 'nt' or sys.platform == 'darwin',
                     'needs a filesystem that accepts non-UTF-8 names')
    def test_non_utf8_entry_name_round_trips_through_the_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = os.fsencode(tmp) + b'/-caf\xe9'
            with open(raw, 'wb') as handle:
                handle.write(b'keep')
            (Path(tmp) / '-z').write_bytes(b'keep')
            stage = self._abandoned_public_stage(tmp)
            first = reclaim_export_staging([tmp], max_scan=1)
            self.assertFalse(any('scan cursor' in e for e in first['errors']), first['errors'])
            if not first["removed"]:
                call, _ = self._reclaim_until_removed(tmp, 1, len(os.listdir(tmp)) + 1)
                self.assertIsNotNone(call)
            self.assertFalse(stage.path.exists())
            with open(raw, 'rb') as handle:
                self.assertEqual(b'keep', handle.read())

    def test_parent_changes_between_calls_never_double_handle_and_still_reach_new_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(10):
                (Path(tmp) / f'-f-{index:02d}').write_bytes(b'keep')
            for _ in range(3):
                reclaim_export_staging([tmp], max_scan=2)
            Path(tmp, '-f-00').unlink()
            Path(tmp, '-f-01').unlink()
            for index in range(5):
                (Path(tmp) / f'-g-{index}').write_bytes(b'new')
            stage = self._abandoned_public_stage(tmp)
            # Mutation may require a fresh pass after the current iterator ends.
            bound = len(os.listdir(tmp)) + 3
            removed_at = None
            real = export_staging.stage_directory
            for call in range(1, bound + 1):
                admitted = []
                def counting(*args, **kwargs):
                    admitted.append(str(args[0]))
                    return real(*args, **kwargs)
                with patch.object(export_staging, 'stage_directory', side_effect=counting):
                    result, examined = self._count_child_stats(
                        tmp, lambda: reclaim_export_staging([tmp], max_scan=2))
                # No stage is admitted twice in one call, and no more than the
                # budget's worth of entries is examined.
                self.assertEqual(len(admitted), len(set(admitted)), admitted)
                self.assertLessEqual(len(examined), 2, examined)
                if result['removed']:
                    removed_at = call
                    break
            self.assertIsNotNone(removed_at)
            self.assertFalse(stage.path.exists())

    def test_admission_attempts_are_bounded_by_the_scan_budget(self):
        stages = []
        with tempfile.TemporaryDirectory() as tmp:
            try:
                for _ in range(3):
                    stages.append(export_staging._create_public_export_staging(tmp, 'mp3'))
                attempts = []
                real = export_staging.stage_directory
                def counting(*args, **kwargs):
                    attempts.append(args[0])
                    return real(*args, **kwargs)
                with patch.object(export_staging, 'stage_directory', side_effect=counting):
                    result = reclaim_export_staging([tmp], max_scan=2)
                # Live owners are preserved after admission is attempted, so
                # attempts are visible and must stop at the examined budget.
                self.assertEqual(2, len(attempts))
                self.assertEqual([], result['removed'])
                self.assertTrue(all(stage.path.exists() for stage in stages))
            finally:
                for stage in stages:
                    stage._lock.__exit__(None, None, None)
                    stage._closed = True

    @unittest.skipIf(os.name == 'nt', 'POSIX private retirement registry')
    def test_retirement_slots_keep_their_own_bound_independent_of_max_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            stages = [create_export_staging(tmp, 'mp3') for _ in range(3)]
            for stage in stages:
                stage._lock.__exit__(None, None, None)
                stage._closed = True
            result = reclaim_export_staging([tmp], max_scan=1)
            self.assertEqual(sorted(str(stage.path) for stage in stages),
                             sorted(result['removed']))

    @unittest.skipIf(os.name == 'nt', 'POSIX descriptor-backed discovery')
    def test_stable_65_roots_reach_later_entries_without_iterator_eviction(self):
        with tempfile.TemporaryDirectory() as temporary:
            roots, stages = [], []
            try:
                export_staging._close_scan_iterators()
                for index in range(65):
                    root = Path(temporary, f'root-{index:03d}')
                    root.mkdir()
                    (root / 'foreign').write_bytes(b'preserve')
                    stage = self._abandoned_public_stage(root)
                    roots.append(root)
                    stages.append(stage.path)
                real_scandir = os.scandir

                class OrderedFixtureIterator:
                    def __init__(self, parent):
                        with real_scandir(parent) as entries:
                            self.entries = iter(sorted(
                                entries, key=lambda entry: (entry.name != 'foreign', entry.name)))

                    def __next__(self):
                        return next(self.entries)

                    def close(self):
                        pass

                proxy = type('FixtureOs', (), {
                    '__getattr__': lambda self, name: getattr(os, name),
                    'scandir': staticmethod(OrderedFixtureIterator),
                })()
                with patch.object(export_staging, 'os', proxy):
                    for _ in range(4):
                        reclaim_export_staging(roots, max_scan=1, max_remove=128)
                        self.assertLessEqual(len(export_staging._scan_iterators), 128)
                        if all(not stage.exists() for stage in stages):
                            break
                self.assertTrue(all(not stage.exists() for stage in stages))
                self.assertTrue(all((root / 'foreign').read_bytes() == b'preserve'
                                    for root in roots))
            finally:
                export_staging._close_scan_iterators()

    @unittest.skipIf(os.name == 'nt', 'POSIX private retirement registry')
    def test_recurring_first_root_removals_do_not_starve_later_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, later = Path(temporary, 'first'), Path(temporary, 'later')
            first.mkdir()
            later.mkdir()
            try:
                export_staging._close_scan_iterators()
                pending = self._abandoned_public_stage(later)
                for _ in range(4):
                    self._abandoned_public_stage(first)
                    result = reclaim_export_staging([first, later], max_scan=1, max_remove=1)
                    self.assertLessEqual(len(result['removed']), 1)
                    if not pending.path.exists():
                        break
                self.assertFalse(pending.path.exists())
            finally:
                export_staging._close_scan_iterators()

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink alias fixture')
    def test_rotated_removal_keeps_first_requested_alias_in_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, 'actual')
            target.mkdir()
            alias = Path(temporary, 'alias')
            alias.symlink_to(target, target_is_directory=True)
            try:
                export_staging._close_scan_iterators()
                first = reclaim_export_staging([alias, target], max_total_scan=1)
                self.assertIn('parent scan budget reached; input rows deferred', first['errors'])
                stage = self._abandoned_public_stage(alias)
                removed = reclaim_export_staging([alias, target], max_remove=1)
                self.assertEqual([str(alias / stage.path.name)], removed['removed'])
            finally:
                export_staging._close_scan_iterators()

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink alias fixture')
    def test_rotated_scan_error_keeps_first_requested_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, 'actual')
            target.mkdir()
            alias = Path(temporary, 'alias')
            alias.symlink_to(target, target_is_directory=True)
            for index in range(3):
                (target / f'foreign-{index}').write_bytes(b'preserve')
            try:
                export_staging._close_scan_iterators()
                reclaim_export_staging([alias, target], max_total_scan=1)
                result = reclaim_export_staging([alias, target], max_scan=1)
                self.assertIn(f'{alias}: scan limit reached', result['errors'])
                self.assertNotIn(f'{target}: scan limit reached', result['errors'])
            finally:
                export_staging._close_scan_iterators()

    def test_parent_input_limit_refuses_without_consuming_or_reclaiming(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = self._abandoned_public_stage(temporary)

            class ExplodingIterator:
                def __iter__(self):
                    raise AssertionError('parent iterator must not be consumed')

            untouched = ExplodingIterator()
            self.assertEqual({'removed': [], 'preserved': [], 'errors': []},
                             reclaim_export_staging(untouched, max_scan=0))
            with self.assertRaisesRegex(ValueError, 'bounded list or tuple'):
                reclaim_export_staging(untouched)
            with self.assertRaisesRegex(ValueError, 'row limit'):
                reclaim_export_staging([temporary] * 129)
            with self.assertRaisesRegex(ValueError, 'budget exceeds'):
                reclaim_export_staging([temporary], max_total_scan=export_staging.MAX_TOTAL_LEGACY_SCAN + 1)
            self.assertTrue(stage.path.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX process descriptor limit')
    def test_low_descriptor_headroom_preserves_requested_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, second = Path(temporary, 'first'), Path(temporary, 'second')
            first.mkdir()
            second.mkdir()
            stage = self._abandoned_public_stage(second)
            with patch.object(export_staging, '_fd_headroom', return_value=1):
                result = reclaim_export_staging([first, second])
            self.assertEqual([], result['removed'])
            self.assertIn('Export recovery skipped: insufficient descriptor capacity', result['errors'])
            self.assertTrue(stage.path.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink alias fixture')
    def test_low_descriptor_headroom_counts_unique_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, 'target')
            target.mkdir()
            alias = Path(temporary, 'alias')
            alias.symlink_to(target, target_is_directory=True)
            stage = self._abandoned_public_stage(target)
            with patch.object(export_staging, '_fd_headroom', return_value=1):
                result = reclaim_export_staging([alias, target])
            self.assertEqual([str(alias / stage.path.name)], result['removed'])
            self.assertFalse(stage.path.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX descriptor limit fixture')
    def test_lowered_descriptor_limit_closes_cached_handles(self):
        with tempfile.TemporaryDirectory() as temporary:
            roots = []
            for index in range(3):
                root = Path(temporary, f'root-{index}')
                root.mkdir()
                (root / 'foreign-a').write_bytes(b'keep')
                (root / 'foreign-b').write_bytes(b'keep')
                roots.append(root)
            try:
                export_staging._close_scan_iterators()
                for root in roots:
                    reclaim_export_staging([root], max_scan=1)
                self.assertEqual(3, len(export_staging._scan_iterators))
                with patch.object(export_staging, '_fd_headroom', return_value=1):
                    reclaim_export_staging([roots[-1]], max_scan=1)
                self.assertLessEqual(len(export_staging._scan_iterators), 1)
            finally:
                export_staging._close_scan_iterators()

    @unittest.skipIf(os.name == 'nt', 'POSIX descriptor limit fixture')
    def test_low_descriptor_refusal_still_prunes_cached_handles(self):
        with tempfile.TemporaryDirectory() as temporary:
            roots = []
            for index in range(3):
                root = Path(temporary, f'root-{index}')
                root.mkdir()
                (root / 'foreign-a').write_bytes(b'keep')
                (root / 'foreign-b').write_bytes(b'keep')
                roots.append(root)
            try:
                export_staging._close_scan_iterators()
                for root in roots:
                    reclaim_export_staging([root], max_scan=1)
                self.assertEqual(3, len(export_staging._scan_iterators))
                with patch.object(export_staging, '_fd_headroom', return_value=2):
                    result = reclaim_export_staging(roots, max_scan=1)
                self.assertEqual([], result['removed'])
                self.assertIn('Export recovery skipped: insufficient descriptor capacity',
                              result['errors'])
                self.assertLessEqual(len(export_staging._scan_iterators), 2)
                self.assertTrue(all((root / 'foreign-a').read_bytes() == b'keep'
                                    for root in roots))
            finally:
                export_staging._close_scan_iterators()

    @unittest.skipIf(os.name == 'nt', 'POSIX open-directory replacement fixture')
    def test_unrequested_replaced_root_releases_saved_iterator(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, other = Path(temporary, 'first'), Path(temporary, 'other')
            first.mkdir()
            other.mkdir()
            (first / 'foreign-a').write_bytes(b'keep-a')
            (first / 'foreign-b').write_bytes(b'keep-b')
            moved = Path(temporary, 'moved')
            try:
                export_staging._close_scan_iterators()
                reclaim_export_staging([first], max_scan=1)
                self.assertIn(Path(os.path.realpath(first)), export_staging._scan_iterators)
                first.rename(moved)
                first.mkdir()
                reclaim_export_staging([other])
                self.assertNotIn(first, export_staging._scan_iterators)
                self.assertEqual(b'keep-a', (moved / 'foreign-a').read_bytes())
                self.assertEqual(b'keep-b', (moved / 'foreign-b').read_bytes())
            finally:
                export_staging._close_scan_iterators()

    def test_global_legacy_scan_budget_rotates_to_unvisited_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            roots = []
            for index in range(3):
                root = Path(temporary, f'root-{index}')
                root.mkdir()
                (root / 'foreign').write_bytes(b'preserve')
                roots.append(root)
            stage = self._abandoned_public_stage(roots[2])
            try:
                export_staging._close_scan_iterators()
                examined = []
                real_scandir = os.scandir

                class CountingIterator:
                    def __init__(self, parent):
                        self.entries = real_scandir(parent)

                    def __next__(self):
                        entry = next(self.entries)
                        examined.append(entry.name)
                        return entry

                    def close(self):
                        self.entries.close()

                proxy = type('FixtureOs', (), {
                    '__getattr__': lambda self, name: getattr(os, name),
                    'scandir': staticmethod(CountingIterator),
                })()
                with patch.object(export_staging, 'os', proxy):
                    first = reclaim_export_staging(roots, max_scan=20, max_total_scan=2)
                self.assertEqual(2, len(examined))
                self.assertTrue(stage.path.exists())
                self.assertIn('parent scan budget reached; input rows deferred', first['errors'])
                for _ in range(3):
                    reclaim_export_staging(roots, max_scan=20, max_total_scan=2)
                    if not stage.path.exists():
                        break
                self.assertFalse(stage.path.exists())
            finally:
                export_staging._close_scan_iterators()

    def test_positive_quota_reports_partial_parent_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, second = Path(temporary, 'first'), Path(temporary, 'second')
            first.mkdir()
            second.mkdir()
            for index in range(3):
                (first / f'foreign-{index}').write_bytes(b'preserve')
            try:
                export_staging._close_scan_iterators()
                result = reclaim_export_staging([first, second], max_scan=3,
                                                max_total_scan=2)
                self.assertIn(f'{first}: scan limit reached', result['errors'])
                self.assertTrue(all((first / f'foreign-{index}').read_bytes() == b'preserve'
                                    for index in range(3)))
            finally:
                export_staging._close_scan_iterators()

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink alias fixture')
    def test_duplicate_alias_rows_do_not_permanently_hide_later_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, later = Path(temporary, 'first'), Path(temporary, 'later')
            first.mkdir()
            later.mkdir()
            alias = Path(temporary, 'alias')
            alias.symlink_to(first, target_is_directory=True)
            stage = self._abandoned_public_stage(later)
            try:
                export_staging._close_scan_iterators()
                for _ in range(4):
                    reclaim_export_staging([alias, first, later], max_total_scan=1)
                    if not stage.path.exists():
                        break
                self.assertFalse(stage.path.exists())
            finally:
                export_staging._close_scan_iterators()

    def test_finished_pass_leaves_no_scan_cursor(self):
        export_staging._close_scan_iterators()
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(4):
                (Path(tmp) / f'-f-{index}').write_bytes(b'keep')
            reclaim_export_staging([tmp], max_scan=2)
            self.assertIn(Path(os.path.realpath(tmp)), export_staging._scan_iterators)
            self.assertFalse(Path(tmp, export_staging.SCAN_CURSOR).exists())
            reclaim_export_staging([tmp])
            self.assertNotIn(Path(os.path.realpath(tmp)), export_staging._scan_iterators)
            # Replacing a parent discards its iterator without modifying files
            # in either the old directory or its replacement.
            reclaim_export_staging([tmp], max_scan=1)
            old = Path(tmp).with_name(Path(tmp).name + '-old')
            Path(tmp).rename(old)
            try:
                Path(tmp).mkdir()
                foreign = Path(tmp, export_staging.SCAN_CURSOR)
                foreign.write_bytes(b'foreign state')
                reclaim_export_staging([tmp], max_scan=1)
                self.assertEqual(b'foreign state', foreign.read_bytes())
                self.assertEqual(export_staging.identity(os.stat(tmp)),
                                 export_staging._scan_iterators[Path(os.path.realpath(tmp))][0])
            finally:
                export_staging._close_scan_iterators()
                import shutil
                shutil.rmtree(old)
        # Fixed descriptor cap evicts least-recently-used state rather than
        # letting unrelated idle parents prevent new recovery forever.
        with tempfile.TemporaryDirectory() as tmp:
            roots = []
            for index in range(3):
                parent = Path(tmp, str(index))
                parent.mkdir()
                (parent / 'keep').write_bytes(b'keep')
                roots.append(parent)
            with patch.object(export_staging, 'MAX_OPEN_SCANS', 2):
                result = reclaim_export_staging(roots, max_scan=1)
                self.assertFalse(any("capacity" in e for e in result["errors"]))
                self.assertEqual(2, len(export_staging._scan_iterators))
                self.assertNotIn(Path(os.path.realpath(roots[0])), export_staging._scan_iterators)
                self.assertIn(Path(os.path.realpath(roots[2])), export_staging._scan_iterators)
            export_staging._close_scan_iterators()

        if os.name != 'nt':
            # Swap the discovery parent after its last iterator check. Existing
            # retirement guards already preserve the foreign payload; admission
            # must also avoid creating retirement metadata in that foreign root.
            with tempfile.TemporaryDirectory() as tmp:
                original, foreign = Path(tmp, "original"), Path(tmp, "foreign")
                original.mkdir()
                foreign.mkdir()
                stage = self._abandoned_public_stage(foreign)
                (original / stage.path.name).mkdir()
                before = set(foreign.iterdir())
                moved = Path(tmp, "moved-original")
                real_stat = os.stat
                swapped = []
                def replace_before_stat(path, *args, **kwargs):
                    if (not swapped and (os.fspath(path) == str(original / stage.path.name)
                                         or (kwargs.get('dir_fd') is not None
                                             and os.fspath(path) == stage.path.name))):
                        original.rename(moved)
                        original.symlink_to(foreign, target_is_directory=True)
                        swapped.append(True)
                    return real_stat(path, *args, **kwargs)
                proxy = type('SwappingOs', (), {
                    '__getattr__': lambda _self, name: getattr(os, name),
                    'stat': staticmethod(replace_before_stat)})()
                with patch.object(export_staging, 'os', proxy):
                    result = reclaim_export_staging([original], max_scan=1)
                self.assertEqual([True], swapped)
                self.assertEqual([], result['removed'])
                self.assertEqual(before, set(foreign.iterdir()))
                self.assertEqual(b'abandoned owned output', (stage.path / 'payload.bin').read_bytes())
                self.assertFalse((foreign / '.aa2-export-retirement').exists())
                export_staging._close_scan_iterators()

    def test_owner_creation_refuses_existing_inode(self):
        from export_staging_directory import stage_directory, identity
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'stage'
            directory.mkdir()
            owner = directory / (OWNER_LOCK + '.lock')
            owner.write_bytes(b'preexisting owner')
            before = identity(owner.stat())
            with stage_directory(directory, identity(directory.stat())) as authority:
                with self.assertRaises(FileExistsError):
                    authority.open_owner_stream(create=True)
            self.assertEqual(before, identity(owner.stat()))
            self.assertEqual(b'preexisting owner', owner.read_bytes())

    def test_bad_parent_does_not_abort_recovery_of_other_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = root / 'regular-file'
            bad.write_bytes(b'preserve')
            good = root / 'exports'
            stage = create_export_staging(good, 'mp3')
            stage._lock.__exit__(None, None, None)
            stage._closed = True
            result = reclaim_export_staging(["\0", bad, good])
            self.assertTrue(result['errors'])
            self.assertEqual([str(stage.path)], result['removed'])
            self.assertFalse(stage.path.exists())
            self.assertEqual(b'preserve', bad.read_bytes())
            if os.name != 'nt':
                loop = root / 'loop'
                loop.symlink_to(loop.name)
                stage = self._abandoned_public_stage(good)
                result = reclaim_export_staging([loop, good])
                self.assertTrue(any(str(loop) in error for error in result['errors']))
                self.assertEqual([str(stage.path)], result['removed'])
                self.assertTrue(loop.is_symlink())

        with tempfile.TemporaryDirectory() as tmp:
            bad, good = Path(tmp, "bad"), Path(tmp, "good")
            bad.mkdir()
            legacy = self._abandoned_public_stage(bad)
            ready = create_export_staging(good, 'mp3')
            ready._lock.__exit__(None, None, None)
            ready._closed = True
            # Match the physical stage across canonical paths, Windows 8.3
            # spelling and lexical aliases. The injected error must hit only
            # this stage so recovery can continue to the good parent.
            alias_dir = bad / 'alias'
            alias_dir.mkdir()
            legacy_reference = alias_dir / '..' / legacy.path.name
            real_cleanup = export_staging._cleanup_admitted
            def fail_legacy(path, *args, **kwargs):
                if os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(legacy_reference)):
                    raise RuntimeError('retirement path changed')
                return real_cleanup(path, *args, **kwargs)
            with patch.object(export_staging, '_cleanup_admitted', side_effect=fail_legacy):
                result = reclaim_export_staging([bad, good])
            self.assertIn(str(legacy.path), result['preserved'])
            self.assertTrue(any('retirement path changed' in error for error in result['errors']))
            self.assertTrue((legacy.path / 'payload.bin').exists())
            self.assertEqual([str(ready.path)], result['removed'])

    @unittest.skipIf(os.name == 'nt', 'POSIX private allocation prototype')
    def test_private_creation_recovers_initialization_crashes(self):
        from export_staging_retirement import DIRECTORY, LOCK
        child = r'''
import os,sys
from export_staging_retirement import PosixRetirement
import export_staging
point = sys.argv[2]
write = PosixRetirement._write_receipt
replace = os.replace
marker_write = export_staging._write_marker
def interrupted_marker(*args, **kwargs):
    if point == 'before_marker':os._exit(77)
    result = marker_write(*args, **kwargs)
    if point == 'after_marker':os._exit(77)
    return result
def interrupted_write(self, name, value):
    if point == 'before_' + name:
        os._exit(77)
    result = write(self, name, value)
    if point == 'after_' + name:
        os._exit(77)
    return result
def interrupted_replace(src, dst, *args, **kwargs):
    if point == 'pending_' + dst:
        os._exit(77)
    return replace(src, dst, *args, **kwargs)
PosixRetirement._write_receipt = interrupted_write
os.replace = interrupted_replace
export_staging._write_marker = interrupted_marker
with PosixRetirement(parent=sys.argv[1]) as transaction:
    transaction.initialize_creation('mp3')
raise RuntimeError('Crash point not reached')
'''
        for point in ('before_allocation.json', 'pending_allocation.json',
                      'after_allocation.json', 'before_creation.json',
                      'pending_creation.json', 'after_creation.json',
                      'before_owner.json', 'pending_owner.json', 'after_owner.json',
                      'before_marker', 'after_marker'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run([sys.executable, '-c', child, tmp, point],
                                        cwd=APP, capture_output=True, text=True, timeout=10)
                self.assertEqual(77, result.returncode, result.stderr)
                self.assertEqual([DIRECTORY], sorted(p.name for p in Path(tmp).iterdir()))
                recovered = reclaim_export_staging([tmp])
                self.assertEqual([], recovered['errors'])
                self.assertEqual(1, len(recovered['removed']))
                self.assertEqual([LOCK], sorted(p.name for p in (Path(tmp) / DIRECTORY).iterdir()))

    @unittest.skipIf(os.name == 'nt', 'POSIX private allocation prototype')
    def test_private_creation_preserves_mismatched_or_populated_stage(self):
        from export_staging_retirement import PosixRetirement, DIRECTORY
        for case in ('payload', 'slot_identity', 'stage_identity', 'missing_allocation',
                     'unknown_metadata', 'duplicate_fields'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                with PosixRetirement(parent=tmp) as transaction:
                    stage, expected = transaction.allocate_creation('mp3', 'a' * 32)
                    slot = stage.parent
                if case == 'payload':
                    (stage / 'foreign.wav').write_bytes(b'preserve')
                elif case == 'slot_identity':
                    for name in ('allocation.json', 'creation.json'):
                        receipt = json.loads((slot / name).read_text())
                        receipt['slot_identity'][1] += 1
                        (slot / name).write_text(json.dumps(receipt))
                elif case == 'stage_identity':
                    stage.rename(Path(tmp) / 'original-stage')
                    stage.mkdir(mode=0o700)
                elif case == 'missing_allocation':
                    (slot / 'allocation.json').unlink()
                elif case == 'unknown_metadata':
                    (slot / 'unknown').write_bytes(b'preserve')
                elif case == 'duplicate_fields':
                    path = slot / 'allocation.json'
                    raw = path.read_text()
                    path.write_text('{"schema":1,' + raw[1:])
                before = {str(p.relative_to(slot)): p.read_bytes()
                          for p in slot.rglob('*') if p.is_file()}
                recovered = reclaim_export_staging([tmp])
                self.assertEqual([], recovered['removed'])
                self.assertTrue(recovered['errors'] or recovered['preserved'])
                self.assertTrue(stage.is_dir())
                self.assertEqual(before, {str(p.relative_to(slot)): p.read_bytes()
                                         for p in slot.rglob('*') if p.is_file()})

    @unittest.skipIf(os.name == 'nt', 'POSIX private initialization prototype')
    def test_partial_initialization_reclaims_only_dead_metadata(self):
        from export_staging_directory import stage_directory, stage_owner_lock
        from export_staging_retirement import PosixRetirement, DIRECTORY, LOCK
        for marker_bytes in (None, b'', b'{', b'\xff'):
            with self.subTest(marker=marker_bytes), tempfile.TemporaryDirectory() as tmp:
                with PosixRetirement(parent=tmp) as transaction:
                    path, expected = transaction.allocate_creation('mp3', 'a' * 32)
                    with stage_directory(path, expected) as authority:
                        owner = stage_owner_lock(authority, create=True)
                        owner.__enter__()
                        try:
                            if marker_bytes is not None:
                                descriptor = os.open(path / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                                with os.fdopen(descriptor, 'wb') as stream:
                                    stream.write(marker_bytes)
                        finally:
                            owner.__exit__(None, None, None)
                result = reclaim_export_staging([tmp])
                self.assertEqual([], result['errors'])
                self.assertEqual(1, len(result['removed']))
                self.assertEqual([LOCK], sorted(p.name for p in (Path(tmp) / DIRECTORY).iterdir()))

    @unittest.skipIf(os.name == 'nt', 'POSIX private initialization prototype')
    def test_partial_initialization_preserves_live_or_replaced_owner(self):
        from export_staging_directory import stage_directory, stage_owner_lock, identity
        from export_staging_retirement import PosixRetirement
        for case in ('live', 'replaced', 'missing', 'foreign_valid_marker', 'unknown_payload', 'unbound_replaced'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                with PosixRetirement(parent=tmp) as transaction:
                    path, expected = transaction.allocate_creation('mp3', 'a' * 32)
                    with stage_directory(path, expected) as authority:
                        owner = stage_owner_lock(authority, create=True)
                        stream = owner.__enter__()
                        owned = {**transaction.read_receipt('creation.json'),
                                 'owner_identity': list(identity(os.fstat(stream.fileno())))}
                        if case != 'unbound_replaced':
                            transaction._write_receipt('owner.json', owned)
                        if case == 'foreign_valid_marker':
                            marker = {'schema': 1, 'kind': 'mp3', 'prefix': PREFIXES['mp3'],
                                      'parent': str(Path(tmp).resolve()), 'token': 'b' * 32,
                                      'owner_identity': owned['owner_identity']}
                            authority.write_marker_bytes(json.dumps(marker).encode())
                        else:
                            authority.write_marker_bytes(b'{')
                        if case == 'unknown_payload':
                            (path / 'foreign.wav').write_bytes(b'preserve')
                try:
                    if case != 'live':
                        owner.__exit__(None, None, None)
                    if case in ('replaced', 'missing', 'unbound_replaced'):
                        lock = path / (OWNER_LOCK + '.lock')
                        lock.rename(Path(tmp) / 'original-owner')
                        if case in ('replaced', 'unbound_replaced'):
                            lock.write_bytes(b'foreign owner')
                            lock.chmod(0o600)
                    before = {p.name: p.read_bytes() for p in path.iterdir()}
                    result = reclaim_export_staging([tmp])
                    self.assertEqual([], result['removed'])
                    self.assertTrue(result['errors'] or result['preserved'])
                    self.assertEqual(before, {p.name: p.read_bytes() for p in path.iterdir()})
                finally:
                    if case == 'live':
                        owner.__exit__(None, None, None)

    @unittest.skipIf(os.name == 'nt', 'POSIX private initialization prototype')
    def test_partial_initialization_cleanup_survives_abrupt_exit(self):
        from export_staging_retirement import DIRECTORY, LOCK
        child = r'''
import os,sys
from export_staging import MARKER,reclaim_export_staging
from export_staging_retirement import PosixRetirement,RETAINED_OWNER
from export_staging_directory import stage_directory,stage_owner_lock,PosixStageDirectory
with PosixRetirement(parent=sys.argv[1]) as transaction:
    path,expected = transaction.allocate_creation('mp3','a'*32)
    with stage_directory(path,expected) as authority:
        with stage_owner_lock(authority,create=True):
            authority.write_marker_bytes(b'{')
point=sys.argv[2]
write=PosixRetirement._write_receipt
unlink,rename=os.unlink,os.rename
remove=PosixStageDirectory.remove_empty_directory
def interrupted_write(self,name,value):
    result=write(self,name,value)
    if point=='owner_record' and name=='owner.json':os._exit(77)
    return result
def interrupted_unlink(name,*args,**kwargs):
    result=unlink(name,*args,**kwargs)
    if ((point=='marker_unlink' and name==MARKER) or
        (point=='retained_unlink' and name==RETAINED_OWNER) or
        (point=='creation_receipt_unlink' and name=='creation.json')):os._exit(77)
    return result
def interrupted_rename(src,dst,*args,**kwargs):
    result=rename(src,dst,*args,**kwargs)
    if point=='owner_move' and dst==RETAINED_OWNER:os._exit(77)
    return result
def interrupted_remove(self):
    result=remove(self)
    if point=='stage_remove':os._exit(77)
    return result
PosixRetirement._write_receipt=interrupted_write
PosixStageDirectory.remove_empty_directory=interrupted_remove
os.supports_dir_fd=set(os.supports_dir_fd)|{interrupted_unlink,interrupted_rename}
os.unlink,os.rename=interrupted_unlink,interrupted_rename
reclaim_export_staging([sys.argv[1]])
raise RuntimeError('Cleanup crash boundary not reached')
'''
        for point in ('owner_record', 'marker_unlink', 'owner_move', 'stage_remove',
                      'retained_unlink', 'creation_receipt_unlink'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run([sys.executable, '-c', child, tmp, point],
                                        cwd=APP, capture_output=True, text=True, timeout=10)
                self.assertEqual(77, result.returncode, result.stderr)
                recovered = reclaim_export_staging([tmp])
                self.assertEqual([], recovered['errors'])
                self.assertEqual(1, len(recovered['removed']))
                self.assertEqual([LOCK], sorted(p.name for p in (Path(tmp) / DIRECTORY).iterdir()))

    @unittest.skipIf(os.name == 'nt', 'POSIX private initialization prototype')
    def test_private_initialized_stage_live_normal_and_abandoned_cleanup(self):
        from export_staging_directory import stage_directory
        from export_staging_retirement import PosixRetirement, DIRECTORY, LOCK
        for mode in ('normal', 'abandoned', 'rollback', 'guard'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                with PosixRetirement(parent=tmp) as transaction:
                    path, expected, marker, owner, stream = transaction.initialize_creation('chapter')
                    slot = transaction.slot
                try:
                    (path / 'payload.wav').write_bytes(b'owned audio')
                    live = reclaim_export_staging([tmp])
                    self.assertEqual([], live['removed'])
                    self.assertTrue(live['preserved'])
                    self.assertEqual(b'owned audio', (path / 'payload.wav').read_bytes())
                    if mode == 'rollback':
                        (path / 'old').mkdir()
                        (path / 'old' / 'backup.wav').write_bytes(b'keep backup')
                    elif mode == 'guard':
                        (path / export_staging.RECOVERY).write_bytes(b'keep')
                    if mode == 'normal':
                        with PosixRetirement(parent=tmp) as transaction:
                            transaction.open_slot(slot)
                            with stage_directory(path, expected) as authority:
                                transaction.cleanup_initialized_creation(authority, stream)
                            transaction.discard_empty_slot(slot)
                finally:
                    owner.__exit__(None, None, None)
                if mode == 'abandoned':
                    recovered = reclaim_export_staging([tmp])
                    self.assertEqual([], recovered['errors'])
                    self.assertEqual(1, len(recovered['removed']))
                elif mode in ('rollback', 'guard'):
                    recovered = reclaim_export_staging([tmp])
                    self.assertEqual([], recovered['removed'])
                    self.assertEqual(b'owned audio', (path / 'payload.wav').read_bytes())
                    continue
                self.assertFalse(path.exists())
                self.assertEqual([LOCK], sorted(p.name for p in (Path(tmp) / DIRECTORY).iterdir()))

    @unittest.skipIf(os.name == 'nt', 'POSIX private initialization prototype')
    def test_private_creation_failure_cleans_known_metadata_and_keeps_primary_error(self):
        from export_staging_retirement import PosixRetirement, DIRECTORY, LOCK
        for mode in ('absent_marker', 'partial_marker', 'cleanup_failure'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                original = OSError('marker publication failed')
                cleanup = OSError('cleanup unavailable')
                def fail(path, marker, *, authority):
                    if mode == 'partial_marker':
                        authority.write_marker_bytes(b'{')
                    raise original
                with PosixRetirement(parent=tmp) as transaction:
                    with patch.object(export_staging, '_write_marker', new=fail):
                        if mode == 'cleanup_failure':
                            with patch.object(transaction, 'recover_creation_slot', side_effect=cleanup):
                                with self.assertRaises(OSError) as caught:
                                    transaction.initialize_creation('mp3')
                            self.assertIs(cleanup, caught.exception.__cause__)
                        else:
                            with self.assertRaises(OSError) as caught:
                                transaction.initialize_creation('mp3')
                        self.assertIs(original, caught.exception)
                if mode == 'cleanup_failure':
                    recovered = reclaim_export_staging([tmp])
                    self.assertEqual([], recovered['errors'])
                    self.assertEqual(1, len(recovered['removed']))
                self.assertEqual([LOCK], sorted(p.name for p in (Path(tmp) / DIRECTORY).iterdir()))
                self.assertEqual([DIRECTORY], sorted(p.name for p in Path(tmp).iterdir()))

    @unittest.skipIf(os.name == 'nt', 'POSIX private initialization prototype')
    def test_partial_recovery_rechecks_marker_under_owner_lock(self):
        from contextlib import contextmanager
        import export_staging_retirement as retirement
        from export_staging_directory import stage_directory, stage_owner_lock, identity
        with tempfile.TemporaryDirectory() as tmp:
            with retirement.PosixRetirement(parent=tmp) as transaction:
                path, expected = transaction.allocate_creation('mp3', 'a' * 32)
                with stage_directory(path, expected) as authority:
                    with stage_owner_lock(authority, create=True) as stream:
                        owned = {**transaction.read_receipt('creation.json'),
                                 'owner_identity': list(identity(os.fstat(stream.fileno())))}
                        raw = transaction.creation_cleanup_receipt(owned)['marker'].encode()
            original = retirement.stage_owner_lock
            @contextmanager
            def insert_marker(authority, **kwargs):
                with original(authority, **kwargs) as stream:
                    authority.write_marker_bytes(raw)
                    yield stream
            with patch.object(retirement, 'stage_owner_lock', new=insert_marker):
                result = reclaim_export_staging([tmp])
            self.assertEqual([], result['removed'])
            self.assertTrue(result['errors'])
            self.assertEqual(raw, (path / MARKER).read_bytes())
            self.assertTrue((path / (OWNER_LOCK + '.lock')).exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX private initialization prototype')
    def test_registry_exclusion_and_missing_capability_preserve_slot(self):
        from export_staging_retirement import PosixRetirement
        with tempfile.TemporaryDirectory() as tmp:
            with PosixRetirement(parent=tmp) as first:
                path, _ = first.allocate_creation('mp3', 'a' * 32)
                with self.assertRaises(TimeoutError):
                    with PosixRetirement(parent=tmp):
                        self.fail('A second descriptor acquired the live registry lock')
            with patch.object(os, 'supports_dir_fd', set(os.supports_dir_fd) - {os.stat}):
                result = reclaim_export_staging([tmp])
            self.assertEqual([], result['removed'])
            self.assertTrue(any('Descriptor-relative retirement is unavailable' in error
                                for error in result['errors']))
            self.assertTrue(path.is_dir())
            self.assertEqual(1, len(reclaim_export_staging([tmp])['removed']))

    def run_child(self, parent, kind='mp3', mode='hold'):
        process = subprocess.Popen(
            [sys.executable, '-c', CHILD, str(APP), str(parent), kind, mode],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = process.stdout.readline().strip()
        if not line:
            error = process.stderr.read()
            process.kill()
            process.wait(timeout=10)
            self.fail(error or 'child did not report a staging path')
        return process, Path(line)

    def test_normal_exit_removes_the_marked_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with create_export_staging(tmp, 'mp3') as staging:
                path = staging.path
                (path / 'payload.bin').write_bytes(b'x')
                self.assertTrue(path.is_dir())
                marker_at_release = []
                release = staging._lock.__exit__
                def observed_release(*args):
                    marker_at_release.append((path / MARKER).exists())
                    return release(*args)
                with patch.object(staging._lock, '__exit__', side_effect=observed_release):
                    staging.finish()
                self.assertEqual([False], marker_at_release)
            self.assertFalse(path.exists())

    def test_hard_exit_leaves_a_verifiably_abandoned_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            process, path = self.run_child(tmp, mode='exit')
            process.wait(timeout=10)
            process.communicate()
            self.assertEqual(76, process.returncode)
            self.assertTrue(path.is_dir())
            result = reclaim_export_staging((tmp,))
            self.assertEqual([str(path)], result['removed'])
            self.assertFalse(path.exists())

    def test_live_owner_is_preserved_until_the_lock_is_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            process, path = self.run_child(tmp, mode='hold')
            try:
                result = reclaim_export_staging((tmp,))
                self.assertEqual([], result['removed'])
                self.assertEqual([str(path)], result['preserved'])
                self.assertTrue(path.is_dir())
            finally:
                process.kill()
                process.wait(timeout=10)
                process.communicate()
            result = reclaim_export_staging((tmp,))
            self.assertEqual([str(path)], result['removed'])
            self.assertFalse(path.exists())

    def test_parallel_live_owners_are_both_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, first_path = self.run_child(tmp, mode='hold')
            second, second_path = self.run_child(tmp, mode='hold')
            try:
                result = reclaim_export_staging((tmp,))
                self.assertEqual([], result['removed'])
                self.assertEqual({str(first_path), str(second_path)},
                                 set(result['preserved']))
                self.assertTrue(first_path.is_dir())
                self.assertTrue(second_path.is_dir())
            finally:
                for process in (first, second):
                    process.kill()
                    process.wait(timeout=10)
                    process.communicate()
            result = reclaim_export_staging((tmp,))
            self.assertEqual({str(first_path), str(second_path)},
                             set(result['removed']))

    def test_longer_staging_prefixes_are_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kind in ('cover', 'progress'):
                with self.subTest(kind=kind):
                    process, path = self.run_child(tmp, kind=kind, mode='exit')
                    process.wait(timeout=10)
                    process.communicate()
                    result = reclaim_export_staging((tmp,))
                    self.assertIn(str(path), result['removed'])
                    self.assertFalse(path.exists())

    def test_corrupt_marker_is_preserved_without_aborting_reclaim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = dict(schema=1, kind='mp3', prefix=PREFIXES['mp3'],
                          parent=str(root.resolve()), token='0' * 32)
            malformed = ['[' * 2000 + ']' * 2000,
                         json.dumps({**marker, 'kind': []}),
                         json.dumps({**marker, 'kind': {}}),
                         json.dumps({**marker, 'schema': True}),
                         ' ' * 4097]
            preserved = []
            for index, raw in enumerate(malformed):
                broken = root / (PREFIXES['mp3'] + 'broken-' + str(index))
                broken.mkdir()
                (broken / MARKER).write_text(raw)
                preserved.append(broken)
            directory = root / (PREFIXES['mp3'] + 'directory-marker')
            (directory / MARKER).mkdir(parents=True)
            preserved.append(directory)
            if hasattr(os, 'mkfifo'):
                pipe = root / (PREFIXES['mp3'] + 'fifo-marker')
                pipe.mkdir()
                os.mkfifo(pipe / MARKER)
                preserved.append(pipe)
            process, valid = self.run_child(tmp, mode='exit')
            process.wait(timeout=10)
            process.communicate()
            # Bound the whole reclaim in a child so a FIFO regression cannot
            # hang the test runner. subprocess.run kills/reaps its own child.
            child = ('import sys,json; sys.path.insert(0,sys.argv[1]); '
                     'from export_staging import reclaim_export_staging; '
                     'print(json.dumps(reclaim_export_staging((sys.argv[2],))))')
            completed = subprocess.run([sys.executable, '-c', child, str(APP), str(root)],
                                       capture_output=True, text=True, timeout=10, check=True)
            result = json.loads(completed.stdout)
            self.assertEqual([str(valid)], result['removed'])
            self.assertFalse(valid.exists())
            for broken in preserved:
                with self.subTest(path=broken.name):
                    self.assertIn(str(broken), result['preserved'])
                    self.assertTrue(broken.is_dir())
            if hasattr(os, 'mkfifo'):
                racing = root / (PREFIXES['mp3'] + 'replaced-marker')
                racing.mkdir()
                (racing / MARKER).write_text(json.dumps(marker))
                # Replace a regular marker after lstat, just before os.open.
                # O_NONBLOCK must also protect this race, not just static FIFOs.
                child = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from export_staging import reclaim_export_staging, MARKER
original = os.open
target = os.path.join(sys.argv[3], MARKER)
def replace_then_open(path, flags, *args, **kwargs):
    if os.fspath(path) == target:
        os.unlink(target)
        os.mkfifo(target)
        os.open = original
    return original(path, flags, *args, **kwargs)
os.open = replace_then_open
print(json.dumps(reclaim_export_staging((sys.argv[2],))))
'''
                completed = subprocess.run(
                    [sys.executable, '-c', child, str(APP), str(root), str(racing.resolve())],
                    capture_output=True, text=True, timeout=10, check=True)
                result = json.loads(completed.stdout)
                self.assertIn(str(racing), result['preserved'])
                self.assertTrue((racing / MARKER).is_fifo())

    def test_chapter_rollback_old_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            process, path = self.run_child(tmp, kind='chapter', mode='recovery')
            process.wait(timeout=10)
            process.communicate()
            result = reclaim_export_staging((tmp,))
            self.assertEqual([], result['removed'])
            self.assertIn(str(path), result['preserved'])
            self.assertTrue((path / 'old' / 'backup.wav').is_file())

    def test_recovery_does_not_recreate_a_missing_live_owner_lock(self):
        for replaced in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                stage = create_export_staging(tmp, 'mp3')
                payload = stage.path / 'payload.bin'
                payload.write_bytes(b'live owner payload')
                try:
                    try:
                        (stage.path / (OWNER_LOCK + '.lock')).unlink()
                    except PermissionError:
                        self.skipTest('Platform prevents unlinking the open owner file')
                    if replaced:
                        (stage.path / (OWNER_LOCK + '.lock')).write_bytes(b'')
                    result = reclaim_export_staging((tmp,))
                    self.assertEqual([], result['removed'])
                    self.assertTrue(result['errors'] or result['preserved'])
                    self.assertEqual(b'live owner payload', payload.read_bytes())
                    self.assertEqual(replaced, (stage.path / (OWNER_LOCK + '.lock')).exists())
                finally:
                    stage._lock.__exit__(None, None, None)
                    stage._closed = True


    def test_creation_failure_keeps_original_error_when_cleanup_also_fails(self):
        from export_staging_retirement import PosixRetirement
        if os.name == 'nt':
            from windows_export_staging_retirement import WindowsRetirement
            cases = ((export_staging._create_public_export_staging,
                      export_staging, '_remove_tree'),
                     (create_export_staging, WindowsRetirement, 'recover_slot'))
        else:
            cases = ((create_export_staging, PosixRetirement,
                      'recover_creation_slot'),)
        for factory, target, method in cases:
            with self.subTest(method=method), tempfile.TemporaryDirectory() as tmp:
                original = OSError('marker publication failed')
                cleanup = OSError('staging directory changed during cleanup')
                with patch.object(export_staging, '_write_marker', side_effect=original), \
                        patch.object(target, method, side_effect=cleanup):
                    with self.assertRaises(OSError) as caught:
                        factory(tmp, 'mp3')
                self.assertIs(original, caught.exception)
                self.assertIs(cleanup, caught.exception.__cause__)

    def test_rollback_alias_is_preserved_without_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            foreign = root / 'foreign'
            foreign.mkdir()
            stage = create_export_staging(root, 'chapter')
            try:
                (stage.path / 'old').symlink_to(foreign, target_is_directory=True)
            except (OSError, NotImplementedError):
                stage.finish()
                self.skipTest('Directory symlink creation unavailable')
            stage._lock.__exit__(None, None, None)
            stage._closed = True
            result = reclaim_export_staging((root,))
            self.assertEqual([], result['removed'])
            self.assertIn(str(stage.path), result['preserved'])
            self.assertTrue((stage.path / 'old').is_symlink())
            self.assertTrue((stage.path / MARKER).is_file())
            self.assertTrue(foreign.is_dir())

    def test_unknown_sibling_missing_marker_and_foreign_parent_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sibling = root / '.mp3-merge-not-a-staging-dir'
            sibling.mkdir()
            (sibling / 'unknown.bin').write_bytes(b'keep')
            missing = root / (PREFIXES['mp3'] + 'missing-marker')
            missing.mkdir()
            (missing / 'payload.bin').write_bytes(b'keep')
            foreign = root / (PREFIXES['mp3'] + 'foreign-parent')
            foreign.mkdir()
            (foreign / MARKER).write_text(
                '{"schema":1,"kind":"mp3","prefix":".mp3-merge-",'
                '"parent":"/definitely/not/this/root","token":"' + '0' * 32 + '"}')
            symlink = root / (PREFIXES['mp3'] + 'symlink')
            symlink.symlink_to(sibling, target_is_directory=True)
            process, aliased_lock = self.run_child(tmp, mode='exit')
            process.wait(timeout=10)
            process.communicate()
            target = root / 'foreign-lock'
            target.write_bytes(b'keep')
            actual_lock = aliased_lock / (OWNER_LOCK + '.lock')
            actual_lock.unlink()
            actual_lock.symlink_to(target)
            result = reclaim_export_staging((root,))
            self.assertEqual([], result['removed'])
            self.assertTrue((sibling / 'unknown.bin').is_file())
            self.assertTrue((missing / 'payload.bin').is_file())
            self.assertTrue((foreign / MARKER).is_file())
            self.assertTrue(symlink.is_symlink())
            self.assertIn(str(aliased_lock), result['preserved'])
            self.assertTrue(actual_lock.is_symlink())
            self.assertEqual(b'keep', target.read_bytes())
            self.assertEqual(b'x' * 4096, (aliased_lock / 'payload.bin').read_bytes())

    def test_preserved_recovery_directory_is_not_reaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = create_export_staging(tmp, 'chapter')
            path = staging.path
            (path / 'recovery.bin').write_bytes(b'keep')
            staging.finish(remove=False)
            result = reclaim_export_staging((tmp,))
            self.assertEqual([], result['removed'])
            self.assertTrue((path / 'recovery.bin').is_file())
            guarded = create_export_staging(tmp, 'chapter')
            (guarded.path / 'recovery.bin').write_bytes(b'keep guarded')
            (guarded.path / export_staging.RECOVERY).write_bytes(b'1')
            with self.assertRaises(OSError):
                guarded.finish(remove=True)
            self.assertEqual(b'keep guarded', (guarded.path / 'recovery.bin').read_bytes())
            self.assertTrue((guarded.path / MARKER).exists())


if __name__ == '__main__':
    unittest.main()
