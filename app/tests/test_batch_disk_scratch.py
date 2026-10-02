"""Scratch capacity must be admitted alongside ZIP output, with an explicit opt-in bypass."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import alexandria_batch_processor as batch

class BatchDiskScratchTests(unittest.TestCase):
    def test_zip_capacity_alone_does_not_admit_long_book_scratch(self):
        processor = batch.BatchProcessor('fixture.gguf')
        with patch.object(processor, 'validate_files', return_value=['book.wav']), \
                patch.object(batch, 'get_audio_duration_seconds', return_value=10 * 3600), \
                patch.object(batch.shutil, 'disk_usage', return_value=SimpleNamespace(free=3 * 1024 ** 3)), \
                patch.object(batch, 'log_gpu_stats'), patch.object(batch.time, 'sleep'), \
                patch.object(processor, 'print_summary'), patch.object(processor, 'process_file') as dispatch:
            self.assertFalse(processor.run(['book.wav']))
        dispatch.assert_not_called()
        self.assertTrue(processor.disk_refused)

    def test_explicit_override_bypasses_probe_and_logs_choice(self):
        processor = batch.BatchProcessor('fixture.gguf', skip_disk_check=True)
        with patch.object(batch, 'check_disk_space', side_effect=OSError('unreadable disk')) as probe, \
                self.assertLogs(batch.logger, level='WARNING') as logs:
            self.assertTrue(processor.ensure_disk_space('book.wav', 100))
        probe.assert_not_called()
        self.assertIn('--skip-disk-check', logs.output[0])
        self.assertFalse(processor.disk_refused)

    def test_override_is_not_the_default(self):
        processor = batch.BatchProcessor('fixture.gguf')
        with patch.object(batch, 'check_disk_space', return_value=False):
            self.assertFalse(processor.ensure_disk_space('book.wav', 1))
        self.assertTrue(processor.disk_refused)

    def test_sequential_scratch_reserves_maximum_not_sum(self):
        processor = batch.BatchProcessor('fixture.gguf')
        processor.audio_durations = {'first.wav': 3600, 'last.wav': 7200}
        dataset, temporary = processor.get_scratch_estimates_gb(['first.wav', 'last.wav'])
        self.assertEqual(0.5 + 7200 * 208000 / 1024 ** 3, dataset)
        self.assertEqual(0.25 + 7200 * 32000 / 1024 ** 3, temporary)

    def test_separate_scratch_filesystem_refuses_despite_output_capacity(self):
        processor = batch.BatchProcessor('fixture.gguf')
        processor.audio_durations = {'book.wav': 3600}
        def probe(path):
            if str(path).endswith('dataset_temp'):
                return '/scratch'
            if str(path) == '/tmp-system':
                return '/tmp-system'
            return '/output'
        with patch.object(batch, 'get_disk_probe_path', side_effect=probe), \
                patch.object(batch.tempfile, 'gettempdir', return_value='/tmp-system'), \
                patch.object(batch.os, 'stat', side_effect=lambda path: SimpleNamespace(
                    st_dev={'/output': 1, '/scratch': 2, '/tmp-system': 3}[path])), \
                patch.object(batch, 'check_disk_space', side_effect=[True, False]) as check:
            self.assertFalse(processor.ensure_disk_space('book.wav', 0.5))
        self.assertEqual(['/output', '/scratch'], [call.args[0] for call in check.call_args_list])
        self.assertEqual(0.5 + 3600 * 208000 / 1024 ** 3,
                         check.call_args_list[1].kwargs['required_gb_per_file'])
        self.assertIn('/scratch', processor.results['failed'][0]['reason'])

    def test_separate_system_temp_is_also_checked(self):
        processor = batch.BatchProcessor('fixture.gguf')
        processor.audio_durations = {'book.wav': 3600}
        with patch.object(batch, 'get_disk_probe_path', side_effect=['/output', '/scratch', '/system-temp']), \
                patch.object(batch.os, 'stat', side_effect=lambda path: SimpleNamespace(
                    st_dev={'/output': 1, '/scratch': 2, '/system-temp': 3}[path])), \
                patch.object(batch, 'check_disk_space', side_effect=[True, True, False]) as check:
            self.assertFalse(processor.ensure_disk_space('book.wav', 0.5))
        self.assertEqual(3, check.call_count)
        self.assertEqual(0.25 + 3600 * 32000 / 1024 ** 3,
                         check.call_args_list[2].kwargs['required_gb_per_file'])
        self.assertIn('/system-temp', processor.results['failed'][0]['reason'])

    def test_direct_call_probes_duration_before_computing_output_budget(self):
        processor = batch.BatchProcessor('fixture.gguf')
        processor.output_bytes_per_second = 1024 ** 3 / 3600
        with patch.object(batch, 'get_audio_duration_seconds', return_value=7200) as duration, \
                patch.object(processor, 'ensure_disk_space', return_value=False) as admission, \
                patch.object(batch.subprocess, 'Popen') as child:
            processor.process_file('book.wav', 1, 1)
        duration.assert_called_once_with('book.wav')
        admission.assert_called_once_with('book.wav', 2)
        child.assert_not_called()

    def test_cli_override_is_explicitly_forwarded_and_default_is_false(self):
        from unittest.mock import MagicMock
        for enabled in (False, True):
            processor = MagicMock()
            processor.run.return_value = True
            argv = ['batch', 'book.wav', '--model', 'fixture.gguf']
            if enabled:
                argv.append('--skip-disk-check')
            with self.subTest(enabled=enabled), patch.object(sys, 'argv', argv), \
                    patch.object(batch, 'BatchProcessor', return_value=processor) as constructor:
                with self.assertRaises(SystemExit) as exited:
                    batch.main()
            self.assertEqual(0, exited.exception.code)
            self.assertIs(enabled, constructor.call_args.kwargs['skip_disk_check'])

    def test_override_choice_is_retained_in_batch_receipt(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            processor = batch.BatchProcessor('fixture.gguf', skip_disk_check=True)
            native_save = batch.save_batch_receipt
            with patch.object(batch, 'log_gpu_stats'), patch.object(batch, 'save_batch_receipt',
                    side_effect=lambda filename, data: native_save(Path(tmp) / filename, data)):
                processor.print_summary()
            receipt = json.loads(next(Path(tmp).glob('batch_results_*.json')).read_text())
            self.assertIs(True, receipt['skip_disk_check'])

    def test_override_runs_worker_but_still_requires_verified_output_receipt(self):
        import tempfile
        import zipfile
        for valid in (True, False):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); audio = root / 'book.wav'; audio.write_bytes(b'input')
                output = root / 'dataset.zip'
                marker = Path(str(output) + '.complete.json'); marker.write_text('stale receipt')
                processor = batch.BatchProcessor('fixture.gguf', skip_disk_check=True)
                def launch(*args, **kwargs):
                    with zipfile.ZipFile(output, 'w') as archive:
                        archive.writestr('metadata.jsonl', '{"audio_filepath":"train/clip.wav"}\n')
                        if valid:
                            archive.writestr('train/clip.wav', b'fixture clip')
                    return SimpleNamespace(stdout=[], returncode=0, wait=lambda: 0, poll=lambda: 0)
                with patch.object(batch, 'get_output_name', return_value=str(output)), \
                        patch.object(batch.subprocess, 'Popen', side_effect=launch) as child, \
                        patch.object(batch, 'check_disk_space', side_effect=OSError('unreadable')) as probe:
                    processor.process_file(str(audio), 1, 1)
                child.assert_called_once()
                probe.assert_not_called()
                self.assertEqual(int(valid), len(processor.results['succeeded']))
                self.assertEqual(int(not valid), len(processor.results['failed']))
                self.assertEqual(valid, marker.exists())
