"""The encoder streams staged PCM from a mapping instead of copying it."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydub import AudioSegment

import audio_export


PCM = b'\x00\x10' * 24000
PCM8 = bytes(range(256)) * 8
COVER = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')


def make_settings(fmt='mp3', width=2, **options):
    return {'sample_width': width, 'frame_rate': 24000, 'channels': 1,
            'format': fmt, 'options': options}


class BoundedRead:
    """Reject unbounded read(), which would copy the entire PCM into memory."""

    def __init__(self, stream):
        self._stream = stream

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def read(self, size=-1):
        if size is None or size < 0:
            raise AssertionError('encoder read the whole PCM at once')
        return self._stream.read(size)

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *exc):
        return self._stream.__exit__(*exc)


class AudioEncoderStreamingTests(unittest.TestCase):
    def test_encoder_never_reads_the_whole_pcm(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, encoded = Path(tmp) / 'input.pcm', Path(tmp) / 'output.mp3'
            raw.write_bytes(PCM)
            original = audio_export._open_encoder_file
            def bounded(path, mode, settings):
                return BoundedRead(original(path, mode, settings))
            with patch.object(audio_export, '_open_encoder_file', side_effect=bounded):
                audio_export.run_encoder(str(raw), str(encoded), make_settings(bitrate='192k'))
            self.assertGreater(encoded.stat().st_size, 1024)
            self.assertAlmostEqual(1000, len(AudioSegment.from_mp3(encoded)), delta=1)

    def test_streamed_output_matches_pydub_for_used_options_and_widths(self):
        cases = (
            (2, PCM, {'bitrate': '192k'}),
            (2, PCM, {'bitrate': '128k', 'tags': {'title': 'T', 'artist': 'A'},
                      'id3v2_version': '3'}),
            (2, PCM, {'bitrate': '192k', 'parameters': ['-ar', '16000', '-ac', '1']}),
            (1, PCM8, {'bitrate': '96k'}),
        )
        for width, data, chosen in cases:
            with self.subTest(width=width, options=chosen), \
                    tempfile.TemporaryDirectory() as tmp:
                raw = Path(tmp) / 'input.pcm'
                raw.write_bytes(data)
                streamed = Path(tmp) / 'streamed.mp3'
                expected = Path(tmp) / 'expected.mp3'
                segment = AudioSegment(data=data, sample_width=width,
                                       frame_rate=24000, channels=1)
                with expected.open('wb') as handle:
                    segment.export(handle, format='mp3', **chosen)
                audio_export.run_encoder(str(raw), str(streamed),
                                         make_settings(width=width, **chosen))
                self.assertEqual(expected.read_bytes(), streamed.read_bytes())

    def test_streamed_cover_art_matches_pydub(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw, cover = root / 'input.pcm', root / 'cover.png'
            streamed, expected = root / 'streamed.mp3', root / 'expected.mp3'
            raw.write_bytes(PCM)
            cover.write_bytes(COVER)
            segment = AudioSegment(data=PCM, sample_width=2, frame_rate=24000, channels=1)
            with expected.open('wb') as handle:
                segment.export(handle, format='mp3', bitrate='128k', cover=str(cover))
            audio_export.run_encoder(str(raw), str(streamed),
                                     make_settings(bitrate='128k', cover=str(cover)))
            self.assertEqual(expected.read_bytes(), streamed.read_bytes())

    def test_raw_export_copies_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, encoded = Path(tmp) / 'input.pcm', Path(tmp) / 'output.raw'
            raw.write_bytes(PCM)
            audio_export.run_encoder(str(raw), str(encoded), make_settings('raw'))
            self.assertEqual(PCM, encoded.read_bytes())

    def test_registered_encoder_files_stream_and_record_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw, output = root / 'input.pcm', root / 'output.mp3'
            report = root / 'encoder-error.json'
            raw.write_bytes(PCM)
            output.touch()
            report.touch()
            identities = {member.name: (member.stat().st_dev, member.stat().st_ino)
                          for member in (raw, output, report)}
            settings = make_settings(bitrate='192k')
            settings['registered_files'] = identities
            settings['exception_path'] = str(report)
            audio_export.run_encoder(str(raw), str(output), settings)
            self.assertGreater(output.stat().st_size, 1024)
            self.assertEqual((identities['output.mp3']),
                             (output.stat().st_dev, output.stat().st_ino))
            completed = json.loads(report.read_text())
            self.assertEqual('success', completed['status'])
            self.assertEqual('output.mp3', completed['output'])


if __name__ == '__main__':
    unittest.main()
