"""Explicit native Windows dialogue cleanup checks; Linux discovery never skips them."""
import sys

if sys.platform != 'win32':
    print('Native Windows dialogue cleanup checks require Windows; nothing was verified.',
          file=sys.stderr)
    raise SystemExit(2)

from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from tests import test_dialogue_continuity_eval as fixture


ev = fixture.ev


class NativeWindowsDialogueCleanupTests(fixture.DialogueContinuityFixture):
    def _assert_swapped_package_child_survives(self, directory):
        self.run_eval(rows=(0,))
        swapped = []
        def replace_child(filename, label):
            payload = Path(filename)
            payload.rename(self.root / 'original.wav')
            foreign = self.root / 'foreign'
            if directory:
                foreign.mkdir()
                (foreign / 'keep.txt').write_bytes(b'foreign directory')
            else:
                foreign.write_bytes(b'foreign file')
            foreign.rename(payload)
            swapped.append(payload)
            raise RuntimeError('fixture packaging failure')
        with patch.object(ev, 'validate_generated_audio', side_effect=replace_child):
            with self.assertRaises(Exception) as caught:
                ev.build_blind_package(self.root / 'run', self.root / 'package',
                                       self.root / 'public.json', self.root / 'key.json')
        self.assertEqual(1, len(swapped))
        foreign = swapped[0] / 'keep.txt' if directory else swapped[0]
        self.assertTrue(foreign.exists(), 'cleanup deleted the swapped-in foreign child')
        self.assertEqual(b'foreign directory' if directory else b'foreign file',
                         foreign.read_bytes())
        self.assertIsInstance(caught.exception, ev.ContinuityEvaluationError)
        self.assertIn('cleanup refused', str(caught.exception))
        swapped[0].parent.rename(self.root / 'preserved-stage')

    def test_windows_cleanup_preserves_swapped_in_file(self):
        self._assert_swapped_package_child_survives(directory=False)

    def test_windows_cleanup_preserves_swapped_in_nonempty_directory(self):
        self._assert_swapped_package_child_survives(directory=True)

    def test_windows_cleanup_preserves_replaced_owned_directory(self):
        path = self.root / 'staging'
        path.mkdir()
        child = path / 'nested'
        child.mkdir()
        admitted = {('nested',): ev._path_identity(child)}
        (child / 'owned.wav').write_bytes(b'owned audio')
        admitted[('nested', 'owned.wav')] = ev._path_identity(child / 'owned.wav')
        child.rename(self.root / 'original-directory')
        foreign = self.root / 'foreign-directory'
        foreign.mkdir()
        (foreign / 'keep.txt').write_bytes(b'foreign directory')
        foreign.rename(child)
        with self.assertRaisesRegex(ev.ContinuityEvaluationError, 'cleanup refused'):
            ev._remove_owned_dir(path, ev._path_identity(path), admitted)
        self.assertEqual(b'foreign directory', (child / 'keep.txt').read_bytes())
        child.rename(self.root / 'preserved-foreign-directory')
        path.rename(self.root / 'released-stage')

    def test_windows_cleanup_refuses_unadmitted_child(self):
        path = self.root / 'staging'
        path.mkdir()
        identity = ev._path_identity(path)
        foreign = path / 'unadmitted.wav'
        foreign.write_bytes(b'foreign audio')
        with self.assertRaisesRegex(ev.ContinuityEvaluationError, 'cleanup refused') as caught:
            ev._remove_owned_dir(path, identity, {})
        self.assertIn('not admitted', str(caught.exception.__cause__))
        self.assertEqual(b'foreign audio', foreign.read_bytes())

    def test_windows_directory_refusal_still_removes_both_manifests(self):
        self.run_eval(rows=(0,))
        package = self.root / 'package'
        locked = []
        def refuse_publication(source, destination):
            self.assertEqual(package, Path(destination))
            payload = next(Path(source).glob('*.wav'))
            locked.append((payload, payload.open('rb'), payload.read_bytes()))
            raise OSError('fixture publication failure')
        try:
            with patch.object(ev, 'move_source_upload_exclusively',
                              side_effect=refuse_publication):
                with self.assertRaisesRegex(ev.ContinuityEvaluationError, 'cleanup refused'):
                    ev.build_blind_package(self.root / 'run', package,
                                           self.root / 'public.json', self.root / 'key.json')
            self.assertEqual(1, len(locked))
            payload, stream, original = locked[0]
            self.assertFalse(stream.closed)
            self.assertEqual(original, payload.read_bytes())
            self.assertEqual([], list(self.root.glob('key.json.building-*')))
            self.assertEqual([], list(self.root.glob('public.json.building-*')))
        finally:
            for payload, stream, original in locked:
                stream.close()

    def _assert_native_cleanup_obstruction_is_retryable(self, readonly):
        path = self.root / 'staging'
        path.mkdir()
        owned = path / 'owned.wav'
        owned.write_bytes(b'owned audio')
        identity = ev._path_identity(path)
        admitted = {('owned.wav',): ev._path_identity(owned)}
        stream = None
        try:
            if readonly:
                owned.chmod(stat.S_IREAD)
            else:
                stream = owned.open('rb')
            with self.assertRaisesRegex(ev.ContinuityEvaluationError, 'cleanup refused') as caught:
                ev._remove_owned_dir(path, identity, admitted)
            self.assertIsInstance(caught.exception.__cause__, OSError)
            self.assertEqual(5 if readonly else 32, caught.exception.__cause__.winerror)
            self.assertEqual(b'owned audio', owned.read_bytes())
        finally:
            if stream is not None:
                stream.close()
            owned.chmod(stat.S_IREAD | stat.S_IWRITE)
        moved = self.root / 'moved-stage'
        path.rename(moved)
        moved.rename(path)
        renamed = path / 'renamed.wav'
        owned.rename(renamed)
        renamed.rename(owned)
        ev._remove_owned_dir(path, identity, admitted)
        self.assertEqual([], list(path.iterdir()))

    def test_windows_cleanup_readonly_file_refuses_releases_handles_and_retries(self):
        self._assert_native_cleanup_obstruction_is_retryable(readonly=True)

    def test_windows_cleanup_locked_file_refuses_releases_handles_and_retries(self):
        self._assert_native_cleanup_obstruction_is_retryable(readonly=False)


NATIVE_TEST_COUNT = 7


def main():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeWindowsDialogueCleanupTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if (result.wasSuccessful() and result.testsRun == NATIVE_TEST_COUNT
                 and not result.skipped) else 1


if __name__ == '__main__':
    raise SystemExit(main())
