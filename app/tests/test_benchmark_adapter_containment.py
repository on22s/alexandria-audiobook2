import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import benchmark_runner as runner
from benchmark_fixtures import _hash_entries


class BenchmarkAdapterContainmentTests(unittest.TestCase):
    def make_fixture(self, name):
        fixture = dict(voice_type='lora', text='Hello', instruct='Neutral',
                       speaker='N', seed=42, adapter_path='adapter',
                       adapter_artifact_sha256={name: hashlib.sha256(b'weights').hexdigest()})
        fixture['sha256'] = _hash_entries(fixture)
        fixture['id'] = 'lora'
        return fixture

    def test_outside_artifacts_rejected_before_hashing_or_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = root / 'adapter'
            adapter.mkdir()
            outside = root / 'outside'
            outside.write_bytes(b'weights')
            (adapter / 'linked').symlink_to(outside)
            for name in ('../outside', str(outside), 'linked', '../adapter/linked',
                         '..\\outside'):
                with self.subTest(name=name):
                    fixture = self.make_fixture(name)
                    original = copy.deepcopy(fixture)
                    with patch.object(runner, 'get_file_sha256') as read:
                        with self.assertRaises(ValueError):
                            runner._validate_tts_fixture(fixture, tmp)
                        read.assert_not_called()
                    with patch.object(runner, 'run_benchmark_subprocess') as transport:
                        with self.assertRaises(ValueError):
                            runner._stage_remote_tts_assets({'fixtures': [fixture]}, tmp, 'fixture-host')
                        transport.assert_not_called()
                    self.assertEqual(original, fixture)
            self.assertEqual(b'weights', outside.read_bytes())

    def test_contained_files_and_internal_links_keep_hash_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp, 'adapter')
            adapter.mkdir()
            (adapter / 'weights').write_bytes(b'weights')
            (adapter / 'linked').symlink_to(adapter / 'weights')
            (adapter / 'nested').mkdir()
            (adapter / 'nested' / 'weights').write_bytes(b'weights')
            for name in ('weights', 'linked', 'nested/weights'):
                with self.subTest(name=name):
                    fixture = self.make_fixture(name)
                    original = copy.deepcopy(fixture)
                    runner._validate_tts_fixture(fixture, tmp)
                    self.assertEqual(original, fixture)
            (adapter / 'weights').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'hash changed'):
                runner._validate_tts_fixture(self.make_fixture('weights'), tmp)
