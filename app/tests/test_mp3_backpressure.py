"""Compare real PCM/MP3 artifacts; exercise slow sinks and owned failures."""
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pydub import AudioSegment
from pydub.generators import Sine
import audio_pcm_producer as pcm
from audio_streaming import write_combined_pcm
from audio_export import export_pcm_mp3
from tts import combine_audio_with_pauses, compute_timeline, ExportCancelled
from project import ProjectManager


class Mp3BackpressureTests(unittest.TestCase):
    def test_pcm_artifact_matches_native_promotion_trimming_and_pause_rules(self):
        layouts = [(1, 16000, 1), (2, 24000, 2), (1, 44100, 4), (2, 22050, 2)]
        for overrides in (None, [0, 13, -1, None]):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as tmp:
                segments = [Sine(317 + i * 100, sample_rate=rate).to_audio_segment(duration=380)
                            .set_channels(channels).set_sample_width(width)
                            for i, (channels, rate, width) in enumerate(layouts)]
                before = [s.raw_data for s in segments]
                expected = combine_audio_with_pauses(segments, ['A', 'A', 'B', 'A'], 57, 23, overrides)
                path = Path(tmp) / 'combined.pcm'
                settings = write_combined_pcm(segments, ['A', 'A', 'B', 'A'], path, 57, 23, overrides)
                self.assertIsNotNone(settings)
                self.assertEqual(expected.raw_data, path.read_bytes())
                self.assertEqual((expected.sample_width, expected.frame_rate, expected.channels),
                    (settings['sample_width'], settings['frame_rate'], settings['channels']))
                self.assertEqual(before, [s.raw_data for s in segments])
                self.assertEqual(['combined.pcm'], sorted(p.name for p in Path(tmp).iterdir()))

    def test_real_encoded_output_matches_native_export_audio(self):
        for width in (1, 2, 4):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                segments = [Sine(440, sample_rate=24000).to_audio_segment(duration=800).set_sample_width(width)]
                combined = combine_audio_with_pauses(segments, ['A'])
                expected = root / 'native.mp3'
                combined.export(expected, format='mp3', bitrate='192k').close()
                settings = write_combined_pcm(segments, ['A'], root / 'input.pcm', 80, 20)
                export_pcm_mp3(root / 'input.pcm', root / 'actual.mp3', settings, lambda: False)
                native = AudioSegment.from_mp3(expected); actual = AudioSegment.from_mp3(root / 'actual.mp3')
                self.assertEqual((native.frame_rate, native.channels), (actual.frame_rate, actual.channels))
                self.assertEqual(native.raw_data, actual.raw_data)
                self.assertFalse(any(p.is_dir() for p in root.iterdir()))

    def test_producer_does_not_pull_next_block_while_consumer_is_blocked(self):
        arrived, release = threading.Event(), threading.Event()
        pulled, writes, failures = [], [], []
        real = tempfile.TemporaryFile
        class SlowWriter:
            def __init__(self, stream): self.stream = stream
            def __getattr__(self, name): return getattr(self.stream, name)
            def write(self, data):
                writes.append(len(data)); arrived.set()
                if not release.wait(3): raise AssertionError('consumer was never released')
                return self.stream.write(data)
        def pieces():
            for i in range(8):
                pulled.append(i)
                yield b'\x01\x00' * 32768
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(pcm.tempfile, 'TemporaryFile', side_effect=lambda **kw: SlowWriter(real(**kw))):
                with pcm.PCMProducer(tmp, lambda: None) as producer:
                    def run():
                        try: producer.append(pieces(), (1, 24000, 2))
                        except BaseException as error: failures.append(error)
                    thread = threading.Thread(target=run)
                    thread.start()
                    try:
                        self.assertTrue(arrived.wait(3))
                        self.assertEqual([0], pulled)
                    finally:
                        release.set(); thread.join(3)
                    self.assertFalse(thread.is_alive()); self.assertEqual([], failures)
                    self.assertEqual(8, len(pulled)); self.assertLessEqual(max(writes), pcm.PCM_BLOCK_BYTES)
                    self.assertEqual(8 * 65536, sum(len(data) for data in producer.read()))

    def test_short_producer_write_is_refused(self):
        real = tempfile.TemporaryFile
        class ShortWriter:
            def __init__(self, stream): self.stream = stream
            def __getattr__(self, name): return getattr(self.stream, name)
            def write(self, data): return self.stream.write(data[:-1])
        with tempfile.TemporaryDirectory() as tmp, patch.object(pcm.tempfile, 'TemporaryFile',
                side_effect=lambda **kw: ShortWriter(real(**kw))):
            with pcm.PCMProducer(tmp, lambda: None) as producer, self.assertRaisesRegex(OSError, 'Incomplete'):
                producer.append([b'\1\0' * 100], (1, 24000, 2))

    def test_mastering_uses_final_encoded_file_and_preserves_old_file_on_failure(self):
        import audio_mastering
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); raw = root / 'source.pcm'; output = root / 'out.mp3'
            segment = Sine(317, sample_rate=24000).to_audio_segment(duration=5000).apply_gain(-20)
            settings = write_combined_pcm([segment], ['A'], raw, 0, 0)
            report = export_pcm_mp3(raw, output, settings, lambda: False, mastering={})
            self.assertEqual(audio_mastering.get_audio_sha256(output), report['sha256'])
            prior = output.read_bytes()
            with patch.object(audio_mastering, 'export_mastered_pcm', side_effect=RuntimeError('encoder failed')):
                with self.assertRaisesRegex(RuntimeError, 'encoder failed'):
                    export_pcm_mp3(raw, output, settings, lambda: False, mastering={})
            self.assertEqual(prior, output.read_bytes())
            self.assertEqual([], list(root.glob('.pcm-encode-*')))

    def test_project_ordinary_merge_never_calls_full_book_builder(self):
        import project
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pm = ProjectManager(tmp)
            source = root / 'voice.wav'
            Sine(317, sample_rate=24000).to_audio_segment(duration=300).export(source, format='wav').close()
            pm.save_chunks([{'id': i, 'uid': str(i), 'text': 'Synthetic line.', 'speaker': 'A',
                             'audio_path': 'voice.wav', 'status': 'done'} for i in range(5)])
            original = source.read_bytes()
            with patch.object(project, 'combine_audio_with_pauses', side_effect=AssertionError('whole-book allocation')), \
                 patch.object(project, '_export_audio_segment', side_effect=AssertionError('whole-book encoder input')):
                ok, message = pm.merge_audio()
            self.assertTrue(ok, message)
            self.assertEqual(original, source.read_bytes())
            decoded = AudioSegment.from_mp3(root / 'cloned_audiobook.mp3')
            self.assertGreater(len(decoded), 1000)
            self.assertEqual([], [p for p in root.iterdir() if p.name.startswith(('.mp3-merge-', '.pcm-encode-', '.stream-pcm-'))])

    def test_custom_sources_keep_legacy_compatibility_path(self):
        class Custom(AudioSegment): pass
        with tempfile.TemporaryDirectory() as tmp:
            segment = Custom(data=b'\1\0' * 200, sample_width=2, frame_rate=24000, channels=1)
            path = Path(tmp) / 'untouched.pcm'
            self.assertIsNone(write_combined_pcm([segment], ['A'], path, 0, 0))
            self.assertFalse(path.exists())

    def test_primary_stage_write_fault_survives_close_failure_and_old_file_is_kept(self):
        segment = Sine(317, sample_rate=24000).to_audio_segment(duration=300)
        real_open = Path.open
        class Broken:
            def __init__(self, stream): self.stream = stream
            def write(self, _data): raise ValueError('primary write failure')
            def close(self): self.stream.close(); raise OSError('secondary close failure')
        def open_file(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            return Broken(stream) if path.name == 'output.pcm' and args and args[0] == 'wb' else stream
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'existing.pcm'; target.write_bytes(b'previous complete stage')
            with patch.object(Path, 'open', open_file), self.assertRaisesRegex(ValueError, 'primary write failure'):
                write_combined_pcm([segment], ['A'], target, 0, 0)
            self.assertEqual(b'previous complete stage', target.read_bytes())
            self.assertEqual(['existing.pcm'], [p.name for p in Path(tmp).iterdir()])

    def test_mp3_cancel_reaps_real_encoder_tree_and_preserves_previous_output(self):
        import json, os, sys, time
        import audio_ffmpeg
        real_start = audio_ffmpeg.start_owned_subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); marker = root / 'started.json'
            segment = Sine(317, sample_rate=24000).to_audio_segment(duration=300)
            settings = write_combined_pcm([segment], ['A'], root / 'input.pcm', 0, 0)
            output = root / 'out.mp3'; output.write_bytes(b'previous verified MP3')
            children = []
            script = "import os,sys,json,time,subprocess;child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);open(sys.argv[1],'wb').write(b'partial');open(sys.argv[2],'w').write(json.dumps([os.getpid(),child.pid]));time.sleep(60)"
            def start(command, **kwargs):
                child = real_start([sys.executable, '-c', script, command[-1], str(marker)], **kwargs)
                children.append(child); return child
            deadline = time.monotonic() + 10
            def cancel():
                if time.monotonic() > deadline: self.fail('encoder did not reach the cancellation boundary')
                return marker.exists()
            with patch.object(audio_ffmpeg, 'start_owned_subprocess', side_effect=start):
                with self.assertRaises(ExportCancelled):
                    export_pcm_mp3(root / 'input.pcm', output, settings, cancel)
            self.assertEqual(b'previous verified MP3', output.read_bytes())
            self.assertIsNotNone(children[0].poll())
            if sys.platform == 'linux':
                self.assertTrue(all(not Path('/proc', str(pid)).exists() for pid in json.loads(marker.read_text())))
            self.assertEqual([], list(root.glob('.pcm-encode-*')))
