from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from pydub import AudioSegment
from project import ProjectManager


class ChapterPreviewMetadataTests(unittest.TestCase):
    def test_cold_and_warm_previews_match_decoded_names_without_loading_pcm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pm = ProjectManager(tmp)
            Path(pm.voicelines_dir).mkdir(exist_ok=True)
            chunks = []
            for index, fmt in enumerate(('wav', 'flac', 'ogg', 'mp3')):
                path = Path(pm.voicelines_dir, f'c{index}.{fmt}')
                segment = AudioSegment(data=np.full(24000, 1000, dtype=np.int16).tobytes(),
                                       frame_rate=24000, channels=1, sample_width=2)
                with path.open('wb') as handle:
                    segment.export(handle, format=fmt)
                chunks.append({'speaker': 'NARRATOR', 'text': f'Chapter {index + 1}',
                               'audio_path': str(path.relative_to(root))})
            corrupt = Path(pm.voicelines_dir, 'bad.wav'); corrupt.write_bytes(b'invalid audio header')
            chunks.extend({'speaker': 'NARRATOR', 'text': 'Missing', 'audio_path': path}
                          for path in ('voicelines/missing.wav', 'voicelines/bad.wav', '../outside.wav'))
            before = {path: path.read_bytes() for path in Path(pm.voicelines_dir).iterdir()}
            # Compare the public filename plan with the existing decode admission.
            admitted = [chunk for chunk in chunks if pm.get_chunk_audio_segment(chunk) is not None]
            with patch.object(pm, 'load_chunks', return_value=admitted):
                expected = pm.preview_chapter_filenames(fmt='wav', per_chunk_chapters=True)
            with patch.object(pm, 'load_chunks', return_value=chunks), \
                    patch.object(pm, 'get_chunk_audio_segment', side_effect=AssertionError('preview decoded PCM')):
                self.assertEqual(expected, pm.preview_chapter_filenames(fmt='wav', per_chunk_chapters=True))
                self.assertEqual(expected, pm.preview_chapter_filenames(fmt='wav', per_chunk_chapters=True))
                self.assertEqual([expected[2]], pm.preview_chapter_filenames(
                    fmt='wav', per_chunk_chapters=True, chapters=[2]))
            for path, raw in before.items():
                self.assertEqual(raw, path.read_bytes())


if __name__ == '__main__':
    unittest.main()
