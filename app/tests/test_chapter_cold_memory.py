import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import weakref

import numpy as np
import soundfile as sf
from project import ProjectManager, CHAPTER_EXPORT_DIR


class ColdChapterMemoryTests(unittest.TestCase):
    def test_selected_cold_chapter_releases_prior_decoded_pcm_and_preserves_samples_offsets_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pm = ProjectManager(tmp)
            Path(pm.voicelines_dir).mkdir(exist_ok=True)
            chunks, original = [], {}
            for i in range(12):
                path = Path(pm.voicelines_dir, f'c{i}.wav')
                sf.write(path, np.full(24001, 1000 + i, dtype=np.int16), 24000, subtype='PCM_16')
                original[path] = path.read_bytes()
                chunks.append({'speaker': 'NARRATOR', 'text': f'Chapter {i + 1}',
                               'audio_path': str(path.relative_to(root))})
            reader = pm.get_chunk_audio_segment
            references, observed = [], []
            def decode(chunk):
                segment = reader(chunk)
                references.append(weakref.ref(segment))
                observed.append(sum(ref() is not None for ref in references))
                return segment
            with patch.object(pm, 'load_chunks', return_value=chunks), \
                    patch.object(pm, '_load_pause_defaults', return_value=(0, 0)), \
                    patch.object(pm, 'get_chunk_audio_segment', side_effect=decode):
                ok, message = pm.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[11])
                self.assertTrue(ok, message)
                self.assertLessEqual(max(observed), 2, 'cold export retained preceding decoded book PCM')
                out = root / CHAPTER_EXPORT_DIR
                row = json.loads((out / 'manifest.json').read_text())['chapters'][0]
                pcm, rate = sf.read(out / row['file'], dtype='int16')
                self.assertEqual(24000, rate)
                np.testing.assert_array_equal(np.full(24001, 1011, dtype=np.int16), pcm)
                self.assertAlmostEqual(11 * 24001 * 1000 / 24000, row['start_ms'])
                selected = (out / row['file']).read_bytes()
                self.assertTrue(pm.export_chapters(fmt='wav', per_chunk_chapters=True)[0])
                self.assertEqual(selected, (out / row['file']).read_bytes())
            for path, raw in original.items():
                self.assertEqual(raw, path.read_bytes())
            self.assertEqual([], list(root.glob('.source-pcm-*')))


if __name__ == '__main__':
    unittest.main()
