import concurrent.futures
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import weakref
import zipfile
from unittest.mock import patch

from pydub import AudioSegment
import project
from project import ProjectManager


class ExportStagingTests(unittest.TestCase):
    def test_native_audacity_cancel_during_track_zip_and_before_publish_preserves_archive(self):
        for boundary in ('track', 'zip', 'closed'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as root:
                manager = ProjectManager(root)
                audio = AudioSegment(data=b'\x01\x00' * 2400,
                                     sample_width=2, frame_rate=24000, channels=1)
                source = Path(root, 'voice.wav')
                with source.open('wb') as handle:
                    audio.export(handle, format='wav')
                manager.save_chunks([{'id': 0, 'uid': 'u1', 'speaker': 'ALICE',
                                      'text': 'Synthetic sentence.', 'status': 'done',
                                      'audio_path': source.name}])
                previous = Path(root, 'audacity_export.zip')
                previous.write_bytes(b'previous export')
                cancelled = threading.Event()
                original_zip = zipfile.ZipFile
                class CancelZip(original_zip):
                    def writestr(self, name, data, *args, **kwargs):
                        result = super().writestr(name, data, *args, **kwargs)
                        if boundary == 'zip' and str(name).endswith('.wav'):
                            cancelled.set()
                        return result
                    def __exit__(self, *args):
                        result = super().__exit__(*args)
                        if boundary == 'closed':
                            cancelled.set()
                        return result
                def progress(message):
                    if boundary == 'track' and message.startswith('Writing track:'):
                        cancelled.set()
                with patch('project.zipfile.ZipFile', CancelZip):
                    result = manager.export_audacity(progress_callback=progress,
                                                     cancel_check=cancelled.is_set)
                self.assertTrue(cancelled.is_set())
                self.assertEqual((False, 'Export cancelled'), result)
                self.assertEqual(b'previous export', previous.read_bytes())
                self.assertEqual([], list(Path(root).glob('*.pending.*')))
                self.assertTrue(manager.export_audacity()[0])
                with original_zip(previous) as archive:
                    decoded = AudioSegment.from_file(io.BytesIO(archive.read('alice.wav')), format='wav')
                    self.assertEqual(audio.raw_data, decoded.raw_data)

    def make_manager(self, root, speaker, duration):
        manager = ProjectManager(root)
        audio = AudioSegment.silent(duration=duration, frame_rate=24000)
        rows = [({'speaker': speaker, 'text': speaker, 'uid': speaker}, audio)]
        manager._load_chunks_with_audio = lambda **kwargs: (rows, 0)
        return manager

    def test_overlapping_audacity_exports_have_independent_archives(self):
        with tempfile.TemporaryDirectory() as root:
            managers = [self.make_manager(root, 'Alice', 40),
                        self.make_manager(root, 'Bob', 100)]
            barrier = threading.Barrier(2)
            original_zip, original_replace = zipfile.ZipFile, project.os.replace
            publications, staging = [], []

            class OverlappingZip(original_zip):
                def __enter__(self):
                    staging.append(self.filename)
                    return super().__enter__()

                def __exit__(self, *args):
                    result = super().__exit__(*args)
                    barrier.wait(timeout=10)
                    return result

            def publish(source, destination):
                publications.append(Path(source).read_bytes())
                original_replace(source, destination)

            with patch('project.zipfile.ZipFile', OverlappingZip), \
                 patch('project.os.replace', publish), \
                 concurrent.futures.ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda manager: manager.export_audacity(), managers))
            self.assertTrue(all(ok for ok, _ in results))
            self.assertEqual(2, len(set(staging)))
            found = {}
            for payload in publications:
                with original_zip(io.BytesIO(payload)) as archive:
                    name = archive.read('project.lof').decode().split('"')[1]
                    segment = AudioSegment.from_file(io.BytesIO(archive.read(name)), format='wav')
                    found[name] = len(segment)
            self.assertEqual({'alice.wav': 40, 'bob.wav': 100}, found)
            self.assertEqual(['audacity_export.zip'], sorted(p.name for p in Path(root).iterdir()
                                                           if p.is_file()))

    def test_overlapping_m4b_exports_keep_each_calls_wav_and_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            managers = [self.make_manager(root, 'Alice', 40),
                        self.make_manager(root, 'Bob', 100)]
            barrier = threading.Barrier(2)
            from m4b_encode import encode_m4b
            original_run = subprocess.run
            inputs, outputs = [], []
            lock = threading.Lock()

            def encode(cmd, *args, **kwargs):
                barrier.wait(timeout=10)
                wav, meta = cmd[cmd.index('-i') + 1], cmd[cmd.index('-map_metadata') - 1]
                with open(wav, 'rb') as source:
                    audio = AudioSegment.from_file(source, format='wav')
                with lock:
                    inputs.append((len(audio), Path(meta).read_text(), wav, meta))
                result = encode_m4b(cmd, *args, **kwargs)
                if result[0] == 0:
                    probe = original_run(['ffprobe', '-v', 'error', '-show_entries',
                                          'format_tags=title', '-of', 'json', cmd[-1]],
                                         capture_output=True, text=True, check=True)
                    with lock:
                        outputs.append(json.loads(probe.stdout)['format']['tags']['title'])
                return result

            with patch('m4b_encode.encode_m4b', side_effect=encode), \
                 concurrent.futures.ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(manager.merge_m4b, True, {'title': speaker})
                           for manager, speaker in zip(managers, ['Alice', 'Bob'])]
                results = [future.result(timeout=20) for future in futures]
            self.assertTrue(all(ok for ok, _ in results), results)
            self.assertEqual(['Alice', 'Bob'], sorted(outputs))
            self.assertEqual(2, len({row[2] for row in inputs}))
            self.assertEqual(2, len({row[3] for row in inputs}))
            self.assertTrue(any(duration == 40 and 'title=Alice' in meta
                                for duration, meta, _, _ in inputs))
            self.assertTrue(any(duration == 100 and 'title=Bob' in meta
                                for duration, meta, _, _ in inputs))
            self.assertEqual(['audiobook.m4b'], sorted(p.name for p in Path(root).iterdir()
                                                    if p.is_file()))

    def test_audacity_releases_previous_track_and_groups_timeline_once(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ProjectManager(root)
            rows = [({'speaker': f'Speaker{i}', 'text': str(i)},
                     AudioSegment(data=(i + 1).to_bytes(2, "little") * 240,
                                  sample_width=2, frame_rate=24000, channels=1))
                    for i in range(20)]
            manager._load_chunks_with_audio = lambda **kwargs: (rows, 0)
            manager._load_pause_defaults = lambda: (0, 0)
            original_timeline, original_export = project.compute_timeline, AudioSegment.export
            iterations, tracks = [], []

            class CountedTimeline(list):
                def __iter__(self):
                    iterations.append(True)
                    return super().__iter__()

            def export(track, *args, **kwargs):
                self.assertTrue(all(ref() is None for ref in tracks),
                                'previous full-book tracks remain retained')
                tracks.append(weakref.ref(track))
                return original_export(track, *args, **kwargs)

            with patch('project.compute_timeline', side_effect=lambda *args, **kwargs: CountedTimeline(original_timeline(*args, **kwargs))), \
                 patch.object(AudioSegment, 'export', export):
                ok, path = manager.export_audacity()
            self.assertTrue(ok)
            self.assertLessEqual(len(iterations), 3)
            self.assertEqual(20, len(tracks))
            self.assertTrue(all(ref() is None for ref in tracks))
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(22, len(archive.namelist()))
                self.assertEqual(20, len(archive.read('labels.txt').splitlines()))
                for i in range(20):
                    segment = AudioSegment.from_file(io.BytesIO(archive.read(f'speaker{i}.wav')), format='wav')
                    self.assertEqual(200, len(segment))
                    # Existing 11025 Hz gap resampling rounds submillisecond edges.
                    self.assertEqual((i + 1).to_bytes(2, 'little') * 192,
                                     segment[i * 10 + 1:(i + 1) * 10 - 1].raw_data)
                    if i:
                        self.assertFalse(any(segment[:i * 10 - 1].raw_data))
                    if i < 19:
                        self.assertFalse(any(segment[(i + 1) * 10 + 1:].raw_data))

    def test_failed_exports_preserve_prior_outputs_and_clean_only_owned_staging(self):
        with tempfile.TemporaryDirectory() as root:
            manager = self.make_manager(root, 'Alice', 40)
            oldzip, oldm4b = Path(root, 'audacity_export.zip'), Path(root, 'audiobook.m4b')
            oldzip.write_bytes(b'previous zip'); oldm4b.write_bytes(b'previous m4b')
            unrelated = Path(root, 'temp_m4b_combined.wav'); unrelated.write_bytes(b'foreign scratch')
            with patch.object(AudioSegment, 'export', side_effect=RuntimeError('encode failed')):
                with self.assertRaisesRegex(RuntimeError, 'encode failed'):
                    manager.export_audacity()
                with self.assertRaisesRegex(RuntimeError, 'encode failed'):
                    manager.merge_m4b()
            self.assertEqual(b'previous zip', oldzip.read_bytes())
            self.assertEqual(b'previous m4b', oldm4b.read_bytes())
            self.assertEqual(b'foreign scratch', unrelated.read_bytes())
            self.assertEqual({oldzip.name, oldm4b.name, unrelated.name},
                             {p.name for p in Path(root).iterdir() if p.is_file()})
