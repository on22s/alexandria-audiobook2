"""Upload quota scan skips vanished entries but surfaces I/O errors (#1794).

The competing writer is ``publish_preparer_uploads`` (app/routers/preparer.py),
which renames final->backup, stage->final and removes temporaries/backups
without holding the script upload quota lease.  A directory entry can
therefore disappear between enumeration and the per-entry type/stat check in
``get_upload_storage_bytes``.  The expected behaviour is a per-entry
``FileNotFoundError`` skip at ``stat`` and a native ``False`` skip at the
``is_file`` type check (fresh DT_UNKNOWN/stat-fallback may return False
with no error; Windows cached-type metadata may also return True, so no
claim is made that every Windows vanish returns False), while unrelated
I/O/permission errors remain visible -- never zero/free storage.

Fault injection here wraps real scratch ``os.DirEntry`` objects and forces
a fresh native ``os.stat`` after deleting the target, because Windows
``DirEntry`` metadata can be cached (docs.python.org/3.10/library/os:
DirEntry.stat errors propagate; metadata may be cached).  The factory below
materialises proxies after closing its native iterator; ``_ScanContext`` is
a controlled iterator, not a held-open native directory probe.  This is
deterministic helper fault injection,
not a timed live HTTP race.
"""
import asyncio
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from routers import script
from tests import test_sound_design_upload_selection as selection_tests
from source_upload_stage import OwnedSourceUploadStage


class _ProxyEntry:
    """Wrap a real DirEntry; inject at most one fault mode per entry."""

    def __init__(self, real, mode):
        self._real = real
        self.name = real.name
        self.path = real.path
        self._mode = mode

    def is_file(self, *, follow_symlinks=True):
        if self._mode == 'vanish_is_file_false':
            # Documented native fallback: a DT_UNKNOWN/stat-fallback vanish
            # at the type check yields False, not FileNotFoundError
            # (docs.python.org/3.10/library/os; CPython DirEntry_test_mode
            # catches ENOENT and returns False).  Remove the owned target
            # then a fresh lstat maps ENOENT to False portably.
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            try:
                facts = os.lstat(self.path)
            except FileNotFoundError:
                return False
            # Path reappeared (ABA): classify its current mode honestly
            # instead of unconditionally False.
            if follow_symlinks and stat.S_ISLNK(facts.st_mode):
                try:
                    facts = os.stat(self.path)
                except FileNotFoundError:
                    return False
            return stat.S_ISREG(facts.st_mode)
        if self._mode == 'permission_is_file':
            raise PermissionError('controlled inaccessible entry')
        if self._mode == 'cancel_is_file':
            raise asyncio.CancelledError('controlled cancellation')
        if self._mode == 'symlink_like':
            # A symlink to a file: False without following, True with.
            return bool(follow_symlinks)
        if self._mode == 'explode':
            raise AssertionError('active entry metadata touched')
        return self._real.is_file(follow_symlinks=follow_symlinks)

    def is_dir(self, *, follow_symlinks=True):
        return self._real.is_dir(follow_symlinks=follow_symlinks)

    def is_symlink(self):
        return bool(self._mode == 'symlink_like') or self._real.is_symlink()

    def stat(self, *, follow_symlinks=True):
        if self._mode == 'root_rename_stat':
            # Post-open root loss: the owned scratch root is physically
            # renamed while the controlled scan is active, then a fresh
            # native stat reports the entry missing (avoids Windows DirEntry
            # cached metadata).  Payload is preserved under the new name.
            root = Path(self.path).parent
            root.rename(root.with_name(root.name + '-moved'))
            return os.stat(self.path, follow_symlinks=follow_symlinks)
        if self._mode == 'vanish_stat':
            # Disappearance after the is_file check: the file was confirmed
            # present, then removed (preparer publication), then a fresh
            # native stat reports it missing (avoids Windows DirEntry cache
            # masking that makes DirEntry.stat return stale success).
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            return os.stat(self.path, follow_symlinks=follow_symlinks)
        if self._mode == 'permission_stat':
            raise PermissionError('controlled inaccessible entry')
        if self._mode == 'oserror_stat':
            raise OSError('controlled I/O failure')
        if self._mode == 'cancel_stat':
            raise asyncio.CancelledError('controlled cancellation')
        if self._mode == 'keyboard_stat':
            raise KeyboardInterrupt('controlled keyboard interrupt')
        if self._mode == 'symlink_like':
            # Large target size that must never be counted when not following.
            class _Facts:
                st_size = 99999
            return _Facts()
        if self._mode == 'explode':
            raise AssertionError('active entry metadata touched')
        return self._real.stat(follow_symlinks=follow_symlinks)

    def inode(self):
        return self._real.inode()


class _ScanContext:
    """Context-manager + iterator yielding proxies in a fixed order."""

    def __init__(self, proxies):
        self._proxies = proxies
        self._index = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        if self._index >= len(self._proxies):
            raise StopIteration
        entry = self._proxies[self._index]
        self._index += 1
        return entry

    def close(self):
        pass


def _make_fake_scandir(real_scandir, ordered_names, modes, uploads_dir):
    def fake_scandir(path):
        if os.path.normcase(str(path)) != os.path.normcase(str(uploads_dir)):
            return real_scandir(path)
        with real_scandir(path) as it:
            real_by_name = {e.name: e for e in list(it)}
        proxies = []
        for name in ordered_names:
            real = real_by_name.get(name)
            if real is None:
                # A synthetic entry (e.g. symlink-like) with no real file.
                class _Synthetic:
                    pass
                fake_real = _Synthetic()
                fake_real.name = name
                fake_real.path = os.path.join(str(path), name)
                fake_real.is_file = lambda *, follow_symlinks=True: bool(follow_symlinks)
                fake_real.is_dir = lambda *, follow_symlinks=True: False
                fake_real.is_symlink = lambda: True
                fake_real.inode = lambda: 0
                class _Facts:
                    st_size = 99999
                fake_real.stat = lambda *, follow_symlinks=True: _Facts()
                proxies.append(_ProxyEntry(fake_real, modes.get(name)))
            else:
                proxies.append(_ProxyEntry(real, modes.get(name)))
        return _ScanContext(proxies)
    return fake_scandir


class UploadStorageQuotaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.uploads = Path(self._tmp.name) / 'uploads'
        self.uploads.mkdir(parents=True)
        patcher = patch.object(script, 'UPLOADS_DIR', str(self.uploads))
        self.addCleanup(patcher.stop)
        patcher.start()
        self._real_scandir = os.scandir

    def _write(self, name, data):
        path = self.uploads / name
        path.write_bytes(data)
        return path

    def test_normal_named_files_counted(self):
        self._write('a.txt', b'1234')
        self._write('b.txt', b'123456')
        self.assertEqual(10, script.get_upload_storage_bytes())

    def test_vanished_at_stat_is_skipped_and_survivors_counted(self):
        before = b'before-123'
        after = b'after-12345'
        self._write('before.txt', before)
        self._write('vanished.txt', b'vanished payload that must be skipped')
        self._write('after.txt', after)
        fake = _make_fake_scandir(self._real_scandir,
                                  ['before.txt', 'vanished.txt', 'after.txt'],
                                  {'vanished.txt': 'vanish_stat'},
                                  self.uploads)
        with patch.object(os, 'scandir', fake):
            result = script.get_upload_storage_bytes()
        self.assertEqual(len(before) + len(after), result)
        self.assertTrue((self.uploads / 'before.txt').is_file())
        self.assertTrue((self.uploads / 'after.txt').is_file())
        self.assertFalse((self.uploads / 'vanished.txt').exists())

    def test_vanished_at_is_file_returns_false_and_survivors_counted(self):
        # Documented native fallback: a DT_UNKNOWN/stat-fallback vanish at
        # is_file yields False (docs.python.org/3.10/library/os,
        # CPython DirEntry_test_mode), not FileNotFoundError.  Windows
        # cached-type metadata may also return True, so this controls only
        # the fresh-fallback False outcome and never claims every Windows
        # vanish returns False.  Native False must skip the entry without
        # entering the FileNotFoundError root re-open probe.
        before = b'BEFORE!'
        after = b'after-98765'
        self._write('before.txt', before)
        self._write('vanished.txt', b'vanished payload that must be skipped')
        self._write('after.txt', after)
        fake = _make_fake_scandir(self._real_scandir,
                                  ['before.txt', 'vanished.txt', 'after.txt'],
                                  {'vanished.txt': 'vanish_is_file_false'},
                                  self.uploads)
        opens = []

        def observing_scandir(path, *args, **kwargs):
            opens.append(str(path))
            return fake(path, *args, **kwargs)

        with patch.object(os, 'scandir', observing_scandir):
            result = script.get_upload_storage_bytes()
        self.assertEqual(len(before) + len(after), result)
        self.assertEqual(1, len(opens))
        self.assertTrue((self.uploads / 'before.txt').is_file())
        self.assertTrue((self.uploads / 'after.txt').is_file())
        self.assertFalse((self.uploads / 'vanished.txt').exists())

    def test_active_name_excluded_without_touching_metadata(self):
        survivor = b'survivor-bytes'
        self._write('owned.txt', b'X' * 999)
        self._write('survivor.txt', survivor)
        fake = _make_fake_scandir(self._real_scandir,
                                  ['owned.txt', 'survivor.txt'],
                                  {'owned.txt': 'explode'},
                                  self.uploads)
        with patch.object(script, 'get_upload_stage_accounting',
                          return_value=({'owned.txt'}, 7)), \
                patch.object(os, 'scandir', fake):
            result = script.get_upload_storage_bytes()
        self.assertEqual(len(survivor) + 7, result)

    def test_real_stage_authoritative_bytes_counted_once(self):
        survivor = b'12345678'
        self._write('survivor.txt', survivor)
        stage = OwnedSourceUploadStage(str(self.uploads), byte_limit=64)
        self.addCleanup(stage.close)
        payload = b'stage-payload'
        stage.write(payload)
        # Named stage extent is excluded from the named total; the size
        # recorded by the stage (get_upload_stage_accounting) is added once.
        self.assertEqual(len(survivor) + len(payload),
                         script.get_upload_storage_bytes())

    def test_directory_and_symlink_like_excluded(self):
        payload = b'file-bytes'
        self._write('file.txt', payload)
        subdir = self.uploads / 'subdir'
        subdir.mkdir()
        # A directory entry on the real filesystem is excluded.
        self.assertEqual(len(payload), script.get_upload_storage_bytes())
        # A symlink-like entry (is_file False unless following) with a large
        # target must stay excluded, proving follow_symlinks=False is used.
        # No real symlink is created, so no Windows privilege is involved.
        fake = _make_fake_scandir(self._real_scandir,
                                  ['file.txt', 'link.txt'],
                                  {'link.txt': 'symlink_like'},
                                  self.uploads)
        with patch.object(os, 'scandir', fake):
            result = script.get_upload_storage_bytes()
        self.assertEqual(len(payload), result)

    def test_scandir_root_failure_propagates(self):
        self._write('a.txt', b'1234')
        for error in (FileNotFoundError('controlled missing root'),
                      PermissionError('controlled inaccessible root'),
                      OSError('controlled I/O failure')):
            with self.subTest(error=type(error).__name__):
                def fake(path, _e=error):
                    raise _e
                with patch.object(os, 'scandir', fake):
                    with self.assertRaises(type(error)):
                        script.get_upload_storage_bytes()

    def test_missing_root_after_scan_open_raises_not_partial_sum(self):
        before = b'before-123'
        payload = b'owned synthetic bytes'
        after = b'after-12345'
        self._write('before.txt', before)
        self._write('payload.txt', payload)
        self._write('after.txt', after)
        fake = _make_fake_scandir(self._real_scandir,
                                  ['before.txt', 'payload.txt', 'after.txt'],
                                  {'payload.txt': 'root_rename_stat'},
                                  self.uploads)
        with patch.object(os, 'scandir', fake):
            with self.assertRaises(FileNotFoundError):
                script.get_upload_storage_bytes()
        moved = self.uploads.with_name(self.uploads.name + '-moved')
        self.assertFalse(self.uploads.exists())
        self.assertEqual(before, (moved / 'before.txt').read_bytes())
        self.assertEqual(payload, (moved / 'payload.txt').read_bytes())
        self.assertEqual(after, (moved / 'after.txt').read_bytes())

    def test_root_rescan_permission_and_non_directory_errors_propagate(self):
        for error in (PermissionError('controlled inaccessible root on rescan'),
                      NotADirectoryError('controlled non-directory root on rescan')):
            with self.subTest(error=type(error).__name__):
                self._write('before.txt', b'1234')
                self._write('vanished.txt', b'vanished payload that must be skipped')
                base_fake = _make_fake_scandir(
                    self._real_scandir,
                    ['before.txt', 'vanished.txt'],
                    {'vanished.txt': 'vanish_stat'},
                    self.uploads)
                calls = {'count': 0}

                def fake(path, _base=base_fake, _error=error):
                    calls['count'] += 1
                    if calls['count'] > 1:
                        raise _error
                    return _base(path)

                with patch.object(os, 'scandir', fake):
                    with self.assertRaises(type(error)):
                        script.get_upload_storage_bytes()

    def test_iteration_failure_propagates(self):
        survivor = b'1234'
        self._write('good.txt', survivor)
        self._write('good2.txt', b'5678')
        with self._real_scandir(self.uploads) as it:
            real = list(it)
        real_by_name = {e.name: e for e in real}

        class _Failing:
            def __init__(self):
                self.step = 0
                self.items = [_ProxyEntry(real_by_name['good.txt'], None)]
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                return self
            def __next__(self):
                if self.step < len(self.items):
                    entry = self.items[self.step]
                    self.step += 1
                    return entry
                raise OSError('controlled iteration failure')
            def close(self):
                pass
        with patch.object(os, 'scandir', lambda path: _Failing()):
            with self.assertRaisesRegex(OSError, 'controlled iteration failure'):
                script.get_upload_storage_bytes()

    def test_permission_and_other_oserror_propagate_after_partial_sums(self):
        before = b'1234'
        after = b'5678'
        for mode, error_type in (('permission_stat', PermissionError),
                                 ('permission_is_file', PermissionError),
                                 ('oserror_stat', OSError)):
            with self.subTest(mode=mode):
                self._write('before.txt', before)
                self._write('bad.txt', b'bad-bytes')
                self._write('after.txt', after)
                # Re-create vanished/bad each subTest (permission modes do not
                # delete, so files persist; rewrite to force fresh state).
                (self.uploads / 'bad.txt').write_bytes(b'bad-bytes')
                fake = _make_fake_scandir(self._real_scandir,
                                          ['before.txt', 'bad.txt', 'after.txt'],
                                          {'bad.txt': mode},
                                          self.uploads)
                with patch.object(os, 'scandir', fake):
                    with self.assertRaises(error_type) as caught:
                        script.get_upload_storage_bytes()
                if error_type is PermissionError:
                    self.assertIn('controlled inaccessible entry', str(caught.exception))
                else:
                    self.assertIn('controlled I/O failure', str(caught.exception))
                # A propagated error is not a partial sum and not zero: the
                # function raised instead of returning anything.

    def test_cancellation_and_keyboard_interrupt_propagate(self):
        self._write('good.txt', b'1234')
        self._write('bad.txt', b'bad-bytes')
        for mode, error_type in (('cancel_stat', asyncio.CancelledError),
                                 ('cancel_is_file', asyncio.CancelledError),
                                 ('keyboard_stat', KeyboardInterrupt)):
            with self.subTest(mode=mode):
                fake = _make_fake_scandir(self._real_scandir,
                                          ['good.txt', 'bad.txt'],
                                          {'bad.txt': mode},
                                          self.uploads)
                with patch.object(os, 'scandir', fake):
                    with self.assertRaises(error_type):
                        script.get_upload_storage_bytes()


class UploadStorageQuotaHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_succeeds_when_quota_entry_vanishes(self):
        async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (root, uploads, client):
            survivor = b'Survivor synthetic source.'
            (uploads / 'survivor.txt').write_bytes(survivor)
            (uploads / 'vanished.txt').write_bytes(b'Preparer-published bytes disappearing concurrently.')
            real_scandir = os.scandir
            fake = _make_fake_scandir(
                real_scandir,
                ['survivor.txt', 'vanished.txt'],
                {'vanished.txt': 'vanish_stat'},
                uploads)
            payload = b'New synthetic source.'
            with patch.object(os, 'scandir', fake):
                response = await client.post('/api/upload',
                                             params={'select_active': 'false'},
                                             files={'file': ('new.txt', payload)})
            self.assertEqual(200, response.status_code, response.text)
            stored = uploads / response.json()['stored_filename']
            self.assertTrue(stored.is_file())
            self.assertEqual(payload, stored.read_bytes())
            self.assertEqual(survivor, (uploads / 'survivor.txt').read_bytes())
            self.assertFalse((uploads / 'vanished.txt').exists())
            # Quota afterwards counts survivors + the persisted upload only.
            self.assertEqual(len(survivor) + len(payload),
                             script.get_upload_storage_bytes())

    async def test_over_quota_survivors_still_refuse_despite_vanished_entry(self):
        async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (root, uploads, client):
            survivor = b'Survivor synthetic source.'
            (uploads / 'survivor.txt').write_bytes(survivor)
            (uploads / 'vanished.txt').write_bytes(b'Preparer-published bytes disappearing concurrently.')
            real_scandir = os.scandir
            fake = _make_fake_scandir(
                real_scandir,
                ['survivor.txt', 'vanished.txt'],
                {'vanished.txt': 'vanish_stat'},
                uploads)
            payload = b'New synthetic source.'
            # Survivors alone fill the quota (available <= 0). A return-0
            # implementation would wrongly admit this upload with 200.
            with patch.object(os, 'scandir', fake), \
                    patch.object(script, 'MAX_UPLOAD_STORAGE_BYTES', len(survivor)):
                response = await client.post('/api/upload',
                                             params={'select_active': 'false'},
                                             files={'file': ('new.txt', payload)})
            self.assertEqual(413, response.status_code, response.text)
            self.assertEqual([], list(uploads.glob('new*.txt')))
            self.assertEqual(survivor, (uploads / 'survivor.txt').read_bytes())

    async def test_permission_error_during_quota_remains_visible(self):
        async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (root, uploads, client):
            (uploads / 'blocked.txt').write_bytes(b'Controlled inaccessible entry payload.')
            real_scandir = os.scandir
            fake = _make_fake_scandir(
                real_scandir,
                ['blocked.txt'],
                {'blocked.txt': 'permission_stat'},
                uploads)
            with patch.object(os, 'scandir', fake):
                response = await client.post('/api/upload',
                                             params={'select_active': 'false'},
                                             files={'file': ('new.txt', b'New synthetic source.')})
            # A catch-all quota handler would admit this with 200; the error
            # must stay visible as a server error instead.
            self.assertEqual(500, response.status_code, response.text)
            self.assertEqual([], list(uploads.glob('new*.txt')))


if __name__ == '__main__':
    unittest.main()
