"""Real file/lease boundaries, restart reclamation and retained provider lifetime."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cpu_asr_scratch import CPUASRScratch, CPUASRUnavailable, DIRECTORY, OUTPUTS, is_close_owned_cpu_asr_supported
from subprocess_ownership import start_owned_subprocess, stop_owned_subprocess


class CPUASRScratchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_single_slot_is_nonblocking_and_capture_is_exact_and_private(self):
        if not is_close_owned_cpu_asr_supported():
            return
        source = self.root / 'source'
        source.write_bytes(b'own synthetic audio')
        with CPUASRScratch(self.root) as scratch:
            path, binding = scratch.capture(source, lambda: False)
            self.assertEqual(source.read_bytes(), path.read_bytes())
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), binding['sha256'])
            self.assertEqual(len(source.read_bytes()), binding['size'])
            self.assertEqual(0, path.stat().st_mode & 0o077)
            with self.assertRaisesRegex(CPUASRUnavailable, 'busy'):
                with CPUASRScratch(self.root):
                    self.fail('second ASR admitted')
        self.assertEqual({'authority', 'lease'}, set(os.listdir(self.root / DIRECTORY)))
        self.assertEqual(b'own synthetic audio', source.read_bytes())

    def test_interrupted_known_outputs_are_reclaimed_on_next_admission(self):
        if not is_close_owned_cpu_asr_supported():
            return
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            with CPUASRScratch(self.root) as scratch:
                for name in OUTPUTS:
                    fd = os.open(scratch.path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.write(fd, b'incomplete JSON or audio'); os.close(fd)
                raise RuntimeError('interruption')
        self.assertTrue((self.root / DIRECTORY / 'asr.json').exists())
        with CPUASRScratch(self.root) as scratch:
            self.assertEqual({'authority', 'lease'}, set(os.listdir(scratch.path)))

    def test_unknown_file_preserves_all_known_outputs_before_any_cleanup(self):
        if not is_close_owned_cpu_asr_supported():
            return
        with CPUASRScratch(self.root) as scratch:
            directory = scratch.path
        for name in ('input.audio', 'unknown.wav'):
            path = directory / name
            path.write_bytes(b'preserve'); path.chmod(0o600)
        with self.assertRaisesRegex(CPUASRUnavailable, 'unknown'):
            with CPUASRScratch(self.root):
                self.fail('unknown authority admitted')
        self.assertEqual(b'preserve', (directory / 'input.audio').read_bytes())
        self.assertEqual(b'preserve', (directory / 'unknown.wav').read_bytes())

    def test_corrupt_authority_is_preserved(self):
        if not is_close_owned_cpu_asr_supported():
            return
        with CPUASRScratch(self.root) as scratch:
            authority = scratch.path / 'authority'
        authority.write_bytes(b'corrupt')
        with self.assertRaisesRegex(CPUASRUnavailable, 'authority'):
            with CPUASRScratch(self.root):
                self.fail('corrupt authority admitted')
        self.assertEqual(b'corrupt', authority.read_bytes())

    def test_symlink_hardlink_and_fifo_outputs_preserve_targets(self):
        if not is_close_owned_cpu_asr_supported():
            return
        outside = self.root / 'outside'; outside.write_bytes(b'protected'); outside.chmod(0o600)
        with CPUASRScratch(self.root) as scratch:
            path = scratch.path / 'audio.wav'
        for create in (lambda: path.symlink_to(outside), lambda: os.link(outside, path), lambda: os.mkfifo(path, 0o600)):
            create()
            try:
                with self.assertRaises(CPUASRUnavailable):
                    with CPUASRScratch(self.root):
                        self.fail('unsafe file admitted')
                self.assertEqual(b'protected', outside.read_bytes())
                self.assertTrue(path.is_symlink() or path.exists())
            finally:
                path.unlink()

    def test_root_alias_private_mode_and_replaced_lease_are_refused(self):
        if not is_close_owned_cpu_asr_supported():
            return
        with CPUASRScratch(self.root) as scratch:
            directory = scratch.path
            (directory / 'lease').rename(directory / 'saved-lease')
            (directory / 'lease').write_bytes(b''); (directory / 'lease').chmod(0o600)
            with self.assertRaisesRegex(CPUASRUnavailable, 'lease identity'):
                scratch.capture(self.root / 'unused', lambda: False)
            (directory / 'lease').unlink()
            (directory / 'saved-lease').rename(directory / 'lease')
        directory.chmod(0o755)
        with self.assertRaisesRegex(CPUASRUnavailable, 'not private'):
            with CPUASRScratch(self.root):
                self.fail('public directory admitted')
        directory.chmod(0o700)
        moved = self.root / 'moved'; directory.rename(moved); directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(OSError):
            with CPUASRScratch(self.root):
                self.fail('aliased directory admitted')

    def test_child_outputs_are_private_and_removed_after_reaping(self):
        if not is_close_owned_cpu_asr_supported():
            return
        with CPUASRScratch(self.root) as scratch:
            target = scratch.path / 'asr.json'
            scratch.run([sys.executable, '-c', 'from pathlib import Path;Path(' + repr(str(target)) + ').write_text("{}")'], lambda: False)
            self.assertEqual(0, target.stat().st_mode & 0o077)
        self.assertFalse(target.exists())

    def test_live_outside_owner_retains_lease_after_callers_handle_closes(self):
        if not is_close_owned_cpu_asr_supported():
            return
        process = None
        try:
            with self.assertRaisesRegex(RuntimeError, 'caller exit'):
                with CPUASRScratch(self.root) as scratch:
                    process = start_owned_subprocess([sys.executable, '-c', 'import time;time.sleep(30)'],
                        task_lease_fd=scratch.lease_fd, start_new_session=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    raise RuntimeError('caller exit')
            with self.assertRaisesRegex(CPUASRUnavailable, 'busy'):
                with CPUASRScratch(self.root):
                    self.fail('live provider overlapped another provider')
        finally:
            if process is not None:
                stop_owned_subprocess(process, timeout=1)
                process._alexandria_control.close()
        with CPUASRScratch(self.root):
            pass

    def test_empty_oversize_and_cancelled_captures_are_unavailable(self):
        if not is_close_owned_cpu_asr_supported():
            return
        source = self.root / 'source'
        for length, cancel, message in [(0, False, 'empty'), (64 * 1024**2 + 1, False, '64 MiB'),
                                        (10, True, 'cancelled')]:
            with source.open('wb') as stream:
                stream.truncate(length)
            with self.assertRaisesRegex(CPUASRUnavailable, message):
                with CPUASRScratch(self.root) as scratch:
                    scratch.capture(source, lambda: cancel)
        with CPUASRScratch(self.root):
            pass

    def test_unsupported_platform_is_explicit_without_filesystem_mutation(self):
        with patch('cpu_asr_scratch.is_close_owned_cpu_asr_supported', return_value=False):
            with self.assertRaisesRegex(CPUASRUnavailable, 'platform'):
                with CPUASRScratch(self.root):
                    self.fail('unsupported platform admitted')
        self.assertEqual([], list(self.root.iterdir()))

    def test_fifo_and_directory_inputs_are_rejected_without_blocking_or_output_creation(self):
        if not is_close_owned_cpu_asr_supported():
            return
        fifo = self.root / 'source.fifo'; os.mkfifo(fifo, 0o600)
        for path in (fifo, self.root):
            with self.subTest(path=path), self.assertRaises(CPUASRUnavailable):
                with CPUASRScratch(self.root) as scratch:
                    scratch.capture(path, lambda: False)
            self.assertFalse((self.root / DIRECTORY / 'input.audio').exists())


if __name__ == '__main__':
    unittest.main()
