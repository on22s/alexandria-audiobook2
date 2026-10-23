"""Real independent decoding and immutable archive compatibility boundaries."""
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

import audio_pcm_decode as decoder
from audio_take_history import AudioTakeHistory
from audio_generation_owner import AudioGenerationCancelled


class AudioPCMDecodeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source.wav'
        sf.write(self.source, np.linspace(-.5, .5, 70001), 24000, subtype='FLOAT')

    def decode(self, **options):
        info = sf.info(self.source)
        arguments = dict(frames=info.frames, sample_rate=info.samplerate,
            channels=info.channels, source_limit=1024**2, check_active=lambda: None)
        arguments.update(options)
        with self.source.open('rb') as stream:
            return decoder.decode_audio_pcm(stream, self.source, **arguments)

    def test_invalid_geometry_cannot_start_expensive_work(self):
        for field, value in [('frames', True), ('sample_rate', True), ('channels', True),
                             ('source_limit', True), ('timeout', float('nan'))]:
            with self.subTest(field=field):
                with patch.object(decoder, 'run_owned_media_check') as run:
                    with self.assertRaises(ValueError):
                        self.decode(**{field: value})
                    run.assert_not_called()

    def test_duplicate_external_geometry_is_refused(self):
        metadata = b'{"streams":[{"sample_rate":"24000","channels":2,"channels":1}]}'
        with patch.object(decoder, 'run_owned_media_check', return_value=metadata) as run:
            with self.assertRaises(ValueError):
                self.decode()
            self.assertEqual(1, run.call_count)

    def test_cancellation_in_actual_mpeg_decode_preserves_pending_archive(self):
        source = self.root / 'cancelled.mp3'
        sf.write(source, .1 * np.sin(np.arange(70001) * .1), 24000,
                 format='MP3', subtype='MPEG_LAYER_III')
        original = source.read_bytes()
        history = AudioTakeHistory(self.root)
        cancelled = threading.Event()
        run = decoder.run_owned_media_check
        def cancel_on_output(command, *args, **kwargs):
            if command[0] == 'ffmpeg':
                consume = kwargs['consume_output']
                def consume_then_cancel(block):
                    consume(block)
                    cancelled.set()
                kwargs['consume_output'] = consume_then_cancel
            return run(command, *args, **kwargs)
        with patch.object(decoder, 'run_owned_media_check', side_effect=cancel_on_output):
            with self.assertRaises(AudioGenerationCancelled):
                history.capture(source, 'a' * 64, 'cancelled-uid', {},
                                cancel_check=cancelled.is_set)
        self.assertTrue(cancelled.is_set(), 'Actual decoder output was never reached')
        self.assertEqual('pending', history.list()['records'][0]['state'])
        self.assertEqual(original, source.read_bytes())
        self.assertEqual(original, next(history.directory.glob('*.mp3')).read_bytes())

    def test_borrowed_buffered_input_stays_open_and_keeps_its_position(self):
        original = self.source.read_bytes()
        with self.source.open('rb') as stream:
            self.assertEqual(original[:17], stream.read(17))
            facts = decoder.decode_audio_pcm(stream, self.source, frames=70001,
                sample_rate=24000, channels=1, source_limit=1024**2,
                check_active=lambda: None)
            self.assertEqual(70001, facts['frames'])
            self.assertFalse(stream.closed)
            self.assertEqual(17, stream.tell())
            self.assertEqual(original[17:33], stream.read(16))

    def test_path_swap_cannot_feed_unadmitted_pcm_to_the_sink(self):
        original = self.source.read_bytes()
        expected, _ = sf.read(self.source, dtype='float64', always_2d=True)
        replacement, backup = self.root / 'replacement.wav', self.root / 'admitted.wav'
        sf.write(replacement, np.full(70001, .3), 24000, subtype='FLOAT')
        pieces, events = [], []
        run = decoder.run_owned_media_check
        def swap_during_decode(command, *args, **kwargs):
            if command[0] != 'ffmpeg':
                return run(command, *args, **kwargs)
            try:
                self.source.replace(backup)
            except PermissionError:
                # Some native platforms deny renaming an already-open source.
                events.append('OS protected admitted source')
                return run(command, *args, **kwargs)
            events.append('path swapped')
            try:
                replacement.replace(self.source)
                return run(command, *args, **kwargs)
            finally:
                self.source.unlink(missing_ok=True)
                backup.replace(self.source)
        with patch.object(decoder, 'run_owned_media_check', side_effect=swap_during_decode):
            try:
                self.decode(consume_pcm=pieces.append)
            except ValueError as error:
                self.assertIn('source changed', str(error))
        self.assertTrue(events)
        self.assertTrue(pieces, 'Actual decoder output was never reached')
        np.testing.assert_array_equal(expected, np.concatenate(pieces))
        self.assertEqual(original, self.source.read_bytes())

    def test_mpeg_matrix_preserves_full_geometry_and_reference_pcm(self):
        for rate in (16000, 24000, 48000):
            for channels in (1, 2):
                for frames in (24001, 70001):
                    with self.subTest(rate=rate, channels=channels, frames=frames):
                        samples = np.arange(frames)
                        values = .1 * np.sin(samples * 2 * np.pi * 440 / rate)
                        if channels == 2:
                            values = np.column_stack((values,
                                .06 * np.sin(samples * 2 * np.pi * 730 / rate)))
                        self.source = self.root / 'matrix.mp3'
                        sf.write(self.source, values, rate, format='MP3', subtype='MPEG_LAYER_III')
                        original = self.source.read_bytes()
                        expected, _ = sf.read(self.source, dtype='float64', always_2d=True)
                        pieces = []
                        facts = self.decode(consume_pcm=pieces.append)
                        self.assertEqual((frames, rate, channels),
                            tuple(facts[key] for key in ('frames', 'sample_rate', 'channels')))
                        np.testing.assert_allclose(np.concatenate(pieces), expected, rtol=0, atol=1e-6)
                        self.assertEqual(original, self.source.read_bytes())

    def test_truncated_mpeg_cannot_satisfy_the_admitted_frame_count(self):
        self.source = self.root / 'truncated.mp3'
        sf.write(self.source, .1 * np.sin(np.arange(70001) * .1), 24000,
                 format='MP3', subtype='MPEG_LAYER_III')
        payload = self.source.read_bytes()
        self.source.write_bytes(payload[:len(payload) // 2])
        with self.assertRaises(ValueError):
            self.decode(frames=70001)
        self.assertEqual(payload[:len(payload) // 2], self.source.read_bytes())

    def test_independent_geometry_disagreement_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'geometry disagrees'):
            self.decode(sample_rate=48000)

    def test_changed_source_with_unchanged_stat_version_refuses_after_real_decode(self):
        original = self.source.read_bytes()
        before = self.source.stat()
        run = decoder.run_owned_media_check
        def mutate_after_decode(command, *args, **kwargs):
            result = run(command, *args, **kwargs)
            if command[0] == 'ffmpeg':
                data = bytearray(original); data[-1] ^= 1
                self.source.write_bytes(data)
                os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
            return result
        real_fstat, real_lstat = os.fstat, Path.lstat
        def stable_version(info):
            if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
                return info
            # Model a platform/filesystem whose complete observed timestamp
            # version remains unchanged despite a same-size payload rewrite.
            fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size',
                      'st_mtime_ns', 'st_ctime_ns')
            return SimpleNamespace(**{name: getattr(before, name) for name in fields})
        def source_lstat(path, *args, **kwargs):
            return stable_version(real_lstat(path, *args, **kwargs))
        with patch.object(decoder, 'run_owned_media_check', side_effect=mutate_after_decode), \
                patch.object(os, 'fstat', side_effect=lambda fd: stable_version(real_fstat(fd))), \
                patch.object(Path, 'lstat', new=source_lstat):
            with self.assertRaisesRegex(ValueError, 'source changed'):
                self.decode()

    def test_nonfinite_is_refused_and_finite_out_of_range_is_preserved(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            with self.subTest(value=value):
                sf.write(self.source, np.array([0., value, 0.]), 24000, subtype='FLOAT')
                with self.assertRaises(ValueError):
                    self.decode()
        values = np.array([0., 1.25, -1.5])
        sf.write(self.source, values, 24000, subtype='FLOAT')
        pieces = []
        self.decode(consume_pcm=pieces.append)
        np.testing.assert_array_equal(values, np.concatenate(pieces)[:, 0])

    def test_partial_frame_output_is_refused(self):
        def partial(command, *args, **kwargs):
            if command[0] == 'ffprobe':
                return b'{"streams":[{"sample_rate":"24000","channels":1}]}'
            kwargs['consume_output'](b'bad')
            return b''
        with patch.object(decoder, 'run_owned_media_check', side_effect=partial):
            with self.assertRaisesRegex(ValueError, 'incomplete frames'):
                self.decode()

    def test_mpeg_archives_preserve_original_bytes_and_use_actual_format(self):
        read = sf.SoundFile.read
        def refuse_mpeg_block_reads(audio, *args, **kwargs):
            if audio.format == 'MP3':
                raise AssertionError('MPEG block decoding bypassed the independent validator')
            return read(audio, *args, **kwargs)
        for suffix in ('.mp3', '.wav'):
            with self.subTest(suffix=suffix):
                source = self.root / ('mpeg' + suffix)
                sf.write(source, .1 * np.sin(np.arange(70001) * .1), 24000,
                         format='MP3', subtype='MPEG_LAYER_III')
                original = source.read_bytes()
                history = AudioTakeHistory(self.root)
                with patch.object(sf.SoundFile, 'read', new=refuse_mpeg_block_reads):
                    row = history.capture(source, 'a' * 64, suffix, {})
                self.assertEqual('ready', row['state'])
                self.assertEqual(dict(frames=70001, sample_rate=24000, channels=1), row['shape'])
                stream, _, _ = history.open(row['version'], row['sha256'])
                with stream:
                    self.assertEqual(original, stream.read())
                self.assertEqual(original, source.read_bytes())

    def test_unavailable_decoder_keeps_pending_copy_and_explains_requirement(self):
        source = self.root / 'missing-decoder.mp3'
        sf.write(source, np.full(24001, .1), 24000, format='MP3', subtype='MPEG_LAYER_III')
        original = source.read_bytes()
        history = AudioTakeHistory(self.root)
        with patch.object(decoder, 'run_owned_media_check', side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(ValueError, 'ffprobe and ffmpeg'):
                history.capture(source, 'a' * 64, 'synthetic-uid', {})
        row = history.list()['records'][0]
        self.assertEqual('pending', row['state'])
        self.assertEqual(original, source.read_bytes())
        retained = next(history.directory.glob('*.mp3'))
        self.assertEqual(original, retained.read_bytes())
