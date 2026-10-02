"""Measured resource floors must not be guessed away."""
import unittest
from training_preflight import get_training_disk_error, get_training_vram_error

class TrainingResourcePreflightTests(unittest.TestCase):
    def test_runtime_rejects_unavailable_index_and_low_free_vram(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from training_preflight import get_training_runtime_preflight
        cuda = Mock()
        torch = SimpleNamespace(cuda=cuda, __version__='fixture')
        cases = [(False, 1, 9, 'cuda', 'unavailable'),
                 (True, 1, 9, 'cuda:1', 'index'),
                 (True, 1, 7, 'cuda:0', '8 GB')]
        for available, count, free, device, message in cases:
            with self.subTest(device=device, free=free):
                cuda.reset_mock()
                cuda.is_available.return_value = available
                cuda.device_count.return_value = count
                cuda.mem_get_info.return_value = (free * 1024 ** 3, 16 * 1024 ** 3)
                with patch.dict('sys.modules', {'torch': torch}), patch('training_preflight.importlib.util.find_spec', return_value=object()), patch('training_preflight.resolve_device', return_value=device):
                    with self.assertRaisesRegex(ValueError, message):
                        get_training_runtime_preflight(device)
                if not available or count == 1 and device == 'cuda:1':
                    cuda.mem_get_info.assert_not_called()

    def test_vram_floor_cpu_and_unknown(self):
        self.assertIsNotNone(get_training_vram_error('cuda', 7.999))
        self.assertIsNone(get_training_vram_error('cuda', 8))
        self.assertIsNone(get_training_vram_error('cpu', 0))
        self.assertIsNone(get_training_vram_error('mps', None))
        for value in (float('nan'), float('inf'), -1, True):
            self.assertIsNotNone(get_training_vram_error('cuda', value))

    def test_disk_floor_and_larger_scratch_need(self):
        gb = 1024 ** 3
        self.assertIsNotNone(get_training_disk_error(2 * gb - 1))
        self.assertIsNone(get_training_disk_error(2 * gb))
        self.assertIsNotNone(get_training_disk_error(3 * gb, 4 * gb))
        self.assertIsNone(get_training_disk_error(6 * gb, 4 * gb))

class SelectedInterpreterPreflightTests(unittest.TestCase):
    def test_interpreter_errors_are_failures_not_ready_defaults(self):
        import subprocess
        from unittest.mock import patch
        from training_preflight import get_selected_interpreter_preflight
        cases = [OSError('missing executable'), subprocess.TimeoutExpired('probe', 300),
                 subprocess.CompletedProcess(['probe'], 0, 'not JSON', ''),
                 subprocess.CompletedProcess(['probe'], 0, '{"status":"ready"}', ''),
                 subprocess.CompletedProcess(['probe'], 1, '{"status":"ready","datasets":[],"errors":[]}', '')]
        for case in cases:
            with self.subTest(case=case):
                options = {'side_effect': case} if isinstance(case, Exception) else {'return_value': case}
                with patch('subprocess.run', **options):
                    report = get_selected_interpreter_preflight('/chosen/python', ['book.zip'], 'cpu')
                self.assertEqual('failed', report['status'])
                self.assertTrue(report['errors'])

    def test_selected_interpreter_receives_all_archive_paths_and_device(self):
        import json
        import subprocess
        from unittest.mock import patch
        from training_preflight import get_selected_interpreter_preflight
        receipt = {'status': 'failed', 'datasets': [], 'errors': [{'archive': 'last.zip', 'error': 'bad ZIP'}]}
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1, 'banner\n' + json.dumps(receipt), '')) as run:
            report = get_selected_interpreter_preflight('/chosen/python', ['first.zip', 'last.zip'], 'cuda:1')
        self.assertEqual(receipt, report)
        self.assertEqual('/chosen/python', run.call_args.args[0][0])
        self.assertEqual({'zip_paths': ['first.zip', 'last.zip'], 'device': 'cuda:1'}, json.loads(run.call_args.kwargs['input']))
        self.assertEqual(300, run.call_args.kwargs['timeout'])
