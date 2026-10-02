"""Disk refusal must stop real batch dispatch, not just print a warning."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import alexandria_batch_processor as batch


class BatchDiskAdmissionTests(unittest.TestCase):
    def test_initial_disk_refusal_stops_batch_before_dispatch(self):
        processor = batch.BatchProcessor('fixture.gguf')
        with patch.object(processor, 'validate_files', return_value=['first.wav', 'last.wav']), \
             patch.object(batch, 'check_disk_space', return_value=False), \
             patch.object(batch, 'log_gpu_stats'), patch.object(batch.time, 'sleep'), \
             patch.object(processor, 'print_summary'), patch.object(processor, 'process_file') as dispatch:
            self.assertFalse(processor.run(['first.wav', 'last.wav']))
        dispatch.assert_not_called()
        self.assertIn('disk', processor.results['failed'][0]['reason'].lower())

    def test_between_books_refusal_stops_the_next_book(self):
        processor = batch.BatchProcessor('fixture.gguf')
        with patch.object(processor, 'validate_files', return_value=['first.wav', 'last.wav']), \
             patch.object(batch, 'get_audio_duration_seconds', return_value=3600), \
             patch.object(batch, 'check_disk_space', side_effect=[True, False]), \
             patch.object(batch, 'log_gpu_stats'), patch.object(batch.time, 'sleep'), \
             patch.object(processor, 'print_summary'), patch.object(processor, 'process_file') as dispatch:
            self.assertFalse(processor.run(['first.wav', 'last.wav']))
        dispatch.assert_called_once_with('first.wav', 1, 2)
        self.assertEqual('last.wav', processor.results['failed'][0]['file'])

    def test_healthy_batch_dispatches_every_book_and_checks_remaining_budget(self):
        processor = batch.BatchProcessor('fixture.gguf')
        with patch.object(processor, 'validate_files', return_value=['first.wav', 'last.wav']), \
             patch.object(batch, 'get_audio_duration_seconds', return_value=3600), \
             patch.object(batch, 'check_disk_space', return_value=True) as check, \
             patch.object(batch, 'log_gpu_stats'), patch.object(batch.time, 'sleep'), \
             patch.object(processor, 'print_summary'), patch.object(processor, 'process_file') as dispatch:
            self.assertTrue(processor.run(['first.wav', 'last.wav']))
        self.assertEqual(2, dispatch.call_count)
        self.assertEqual([1.0, 0.5], [call.kwargs['required_gb_per_file'] for call in check.call_args_list])

    def test_disk_probe_failure_refuses_and_shortfall_names_output_filesystem(self):
        from types import SimpleNamespace
        with patch.object(batch.shutil, 'disk_usage', side_effect=OSError('fixture disk probe failure')):
            self.assertFalse(batch.check_disk_space('/output', 0.5, 1))
        with patch.object(batch.shutil, 'disk_usage', return_value=SimpleNamespace(free=1024 ** 3)), \
             self.assertLogs(batch.logger, level='ERROR') as log:
            self.assertFalse(batch.check_disk_space('/output', 2, 1))
        self.assertIn('/output', log.output[0])
        self.assertIn('1073741824 more bytes', log.output[0])

    def test_metadata_duration_is_bounded_and_invalid_values_keep_floor(self):
        import subprocess
        for value in ('nan', 'inf', '0', '-1', 'N/A'):
            with self.subTest(value=value), patch.object(batch.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, value, '')) as probe:
                self.assertIsNone(batch.get_audio_duration_seconds('book.mp3'))
            self.assertEqual(10, probe.call_args.kwargs['timeout'])
        with patch.object(batch.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '3600', '')):
            self.assertEqual(3600, batch.get_audio_duration_seconds('book.mp3'))

    def test_published_volume_refusal_kills_and_reaps_without_success_receipt(self):
        import json
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); audio = root / 'book.wav'; audio.write_bytes(b'fixture input')
            output = root / 'dataset.zip'
            processor = batch.BatchProcessor('fixture.gguf')
            class Child:
                returncode = None
                killed = False
                waited = False
                def __init__(self):
                    self.stdout = self.lines()
                def lines(self):
                    with zipfile.ZipFile(output, 'w') as archive:
                        archive.writestr('metadata.jsonl', json.dumps({'audio_filepath':'train/clip.wav'})+'\n')
                        archive.writestr('train/clip.wav', b'fixture audio')
                    yield f'Volume 1 saved: {output}\n'
                def poll(self): return self.returncode
                def kill(self): self.killed = True; self.returncode = -9
                def wait(self): self.waited = True; self.returncode = self.returncode or 0
            child = Child()
            with patch.object(batch, 'get_output_name', return_value=str(output)), \
                 patch.object(batch, 'check_disk_space', side_effect=[True, False]), \
                 patch.object(batch.subprocess, 'Popen', return_value=child), \
                 patch.object(batch.threading.Thread, 'start'):
                processor.process_file(str(audio), 1, 1)
            self.assertTrue(child.killed)
            self.assertTrue(child.waited)
            self.assertEqual([], processor.results['succeeded'])
            self.assertTrue(batch.is_complete_dataset_zip(output))
            self.assertFalse(Path(str(output)+'.complete.json').exists())
            self.assertTrue(processor.disk_refused)

    def test_only_verified_completed_output_refines_duration_estimate(self):
        import io
        import json
        import zipfile
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); audio = root/'book.wav';audio.write_bytes(b'fixture')
            output=root/'dataset.zip';processor=batch.BatchProcessor('fixture.gguf')
            processor.audio_durations={str(audio):3600,'next.wav':7200}
            def launch(*args, **kwargs):
                with zipfile.ZipFile(output,'w') as archive:
                    archive.writestr('metadata.jsonl',json.dumps({'audio_filepath':'train/clip.wav'})+'\n')
                    archive.writestr('train/clip.wav',b'fixture')
                return SimpleNamespace(stdout=io.StringIO(''),returncode=0,wait=lambda:None,poll=lambda:0)
            actual_size=0.75*1024**3
            native_getsize=batch.os.path.getsize
            def size(path): return actual_size if str(path)==str(output) else native_getsize(path)
            with patch.object(batch,'get_output_name',return_value=str(output)), \
                 patch.object(batch,'check_disk_space',return_value=True), \
                 patch.object(batch.subprocess,'Popen',side_effect=launch), \
                 patch.object(batch.os.path,'getsize',side_effect=size), \
                 patch.object(batch.threading.Thread,'start'):
                processor.process_file(str(audio),1,2)
            self.assertEqual(1,len(processor.results['succeeded']))
            self.assertEqual(1.5,processor.get_disk_estimate_gb('next.wav'))
            self.assertEqual(0.5,processor.get_disk_estimate_gb('unknown.wav'))

    def test_real_cpu_child_is_reaped_on_published_volume_disk_refusal(self):
        import json
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); audio=root/'book.wav';audio.write_bytes(b'fixture')
            output=root/'dataset.zip';processor=batch.BatchProcessor('fixture.gguf')
            code=('import zipfile,time\n'
                  'with zipfile.ZipFile('+repr(str(output))+',"w") as z:\n'
                  ' z.writestr("metadata.jsonl", "{}\\n")\n'
                  ' z.writestr("train/clip.wav", b"fixture")\n'
                  'print("Volume 1 saved: fixture",flush=True)\n'
                  'time.sleep(30)\n')
            children=[];native_popen=subprocess.Popen
            def launch(*args,**kwargs):
                child=native_popen([sys.executable,'-u','-c',code],**kwargs);children.append(child);return child
            try:
                start=time.monotonic()
                with patch.object(batch,'get_output_name',return_value=str(output)), \
                     patch.object(batch,'check_disk_space',side_effect=[True,False]), \
                     patch.object(batch.subprocess,'Popen',side_effect=launch):
                    processor.process_file(str(audio),1,1)
                self.assertLess(time.monotonic()-start,5)
                self.assertIsNotNone(children[0].poll())
                self.assertNotEqual(0,children[0].returncode)
                self.assertFalse(Path(str(output)+'.complete.json').exists())
                self.assertTrue(output.exists())
            finally:
                for child in children:
                    if child.poll() is None: child.kill()
                    child.wait()

    def test_direct_book_call_refuses_before_child_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            audio=Path(directory)/'book.wav';audio.write_bytes(b'fixture')
            processor=batch.BatchProcessor('fixture.gguf')
            with patch.object(batch,'check_disk_space',return_value=False), \
                 patch.object(batch.subprocess,'Popen') as launch:
                processor.process_file(str(audio),1,1)
            launch.assert_not_called()
            self.assertTrue(processor.disk_refused)
