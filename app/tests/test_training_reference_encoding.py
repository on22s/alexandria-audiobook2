"""Reference diagnostics must preserve training text under strict stdout encodings."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from train_lora import get_training_reference_text


class TrainingReferenceEncodingTests(unittest.TestCase):
    def test_reference_sources_preserve_unicode_with_strict_captured_stdout(self):
        text = 'Synthetic reference 日本語'
        for encoding in ('cp1252', 'utf-8'):
            for source in ('file', 'matching_sample', 'legacy_sample'):
                with self.subTest(encoding=encoding, source=source), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    audio = root / 'ref.wav'
                    audio.write_bytes(b'synthetic reference identity')
                    if source == 'file':
                        (root / 'ref_text.txt').write_text(text, encoding='utf-8')
                    samples = [{'text': text, 'audio_path': str(audio) if source == 'matching_sample' else ''}]
                    before = dict(samples[0])
                    stream = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors='strict')
                    try:
                        with contextlib.redirect_stdout(stream):
                            reference = get_training_reference_text(tmp, str(audio), samples)
                        self.assertEqual(text, reference)
                        self.assertEqual(before, samples[0])
                        stream.flush()
                        diagnostic = stream.buffer.getvalue().decode(encoding)
                        self.assertIn('[DATA] Using', diagnostic)
                        self.assertIn(text if encoding == 'utf-8' else r'\u65e5\u672c\u8a9e', diagnostic)
                    finally:
                        stream.close()
