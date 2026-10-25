"""assume_ample_free_disk hides the host's free space, but not a test's own disk fixture."""
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_support import assume_ample_free_disk
from training_preflight import get_training_disk_error


class AssumeAmpleFreeDiskTests(unittest.TestCase):
    def test_low_host_disk_no_longer_reaches_the_preflight_floor(self):
        with patch.object(shutil, 'disk_usage', return_value=SimpleNamespace(total=16, used=15, free=1024 ** 3)):
            self.assertIsNotNone(get_training_disk_error(shutil.disk_usage('/').free))
            case = unittest.TestCase()
            assume_ample_free_disk(case)
            self.assertIsNone(get_training_disk_error(shutil.disk_usage('/').free))
            # A test of the floor itself patches locally, and that still wins.
            with patch.object(shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
                self.assertIsNotNone(get_training_disk_error(shutil.disk_usage('/').free))
            case.doCleanups()
            self.assertEqual(1024 ** 3, shutil.disk_usage('/').free)


if __name__ == '__main__':
    unittest.main()
