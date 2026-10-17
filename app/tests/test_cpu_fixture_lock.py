"""CPU mocks use real private leases without taking another job's GPU lease."""
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from alexandria_file_lock import acquire_exclusive_file_lock
from experiments.gpu_guard import acquire_gpu_lock, release_gpu_lock
from tests.test_support import use_private_cpu_fixture_lock


class CpuFixtureLockTests(unittest.TestCase):
    def test_private_lease_is_native_and_external_lease_stays_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            external = Path(tmp) / 'external.lock'
            with external.open('a+b') as held, patch.dict(
                    os.environ, {'GPU_LOCK': str(external), 'ALEXANDRIA_GPU_LOCK_HELD': '0'}):
                acquire_exclusive_file_lock(held.fileno())
                with self.assertRaisesRegex(RuntimeError, 'held by another job'):
                    acquire_gpu_lock()
                with use_private_cpu_fixture_lock() as private:
                    self.assertNotEqual(external, private)
                    lease = acquire_gpu_lock()
                    self.assertIsNotNone(lease)
                    try:
                        with private.open('a+b') as probe:
                            with self.assertRaises(OSError) as blocked:
                                acquire_exclusive_file_lock(probe.fileno())
                            self.assertIn(blocked.exception.errno, (errno.EACCES, errno.EAGAIN))
                        with external.open('a+b') as probe:
                            with self.assertRaises(OSError) as blocked:
                                acquire_exclusive_file_lock(probe.fileno())
                            self.assertIn(blocked.exception.errno, (errno.EACCES, errno.EAGAIN))
                    finally:
                        release_gpu_lock(lease)
                    with private.open('a+b') as probe:
                        acquire_exclusive_file_lock(probe.fileno())
                self.assertFalse(private.exists())
                self.assertEqual(str(external), os.environ['GPU_LOCK'])
                with self.assertRaisesRegex(RuntimeError, 'held by another job'):
                    acquire_gpu_lock()

    def test_exception_restores_the_callers_inherited_claim_without_borrowing_it(self):
        original = {'GPU_LOCK': '/caller/lock', 'ALEXANDRIA_GPU_LOCK_HELD': '1',
                    'ALEXANDRIA_GPU_LOCK_PID': '999999', 'ALEXANDRIA_GPU_LOCK_FD': '9'}
        with patch.dict(os.environ, original):
            with self.assertRaisesRegex(ValueError, 'fixture failed'):
                with use_private_cpu_fixture_lock() as private:
                    self.assertEqual('0', os.environ['ALEXANDRIA_GPU_LOCK_HELD'])
                    lease = acquire_gpu_lock()
                    release_gpu_lock(lease)
                    raise ValueError('fixture failed')
            self.assertFalse(private.exists())
            self.assertEqual(original, {key: os.environ[key] for key in original})
