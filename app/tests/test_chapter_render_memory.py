import json
from pathlib import Path
import tempfile
import unittest
import weakref
from unittest.mock import patch

from pydub import AudioSegment
import project
from project import ProjectManager, CHAPTER_EXPORT_DIR


class ChapterRenderMemoryTests(unittest.TestCase):
    def make_project(self, root):
        manager = ProjectManager(root)
        manager._load_pause_defaults = lambda: (400, 200)
        rows = []
        for index in range(6):
            audio = AudioSegment(data=(2000 + index).to_bytes(2, 'little') * 2400,
                                 sample_width=2, frame_rate=24000, channels=1)
            path = Path(root, f'line{index}.wav')
            with path.open('wb') as target:
                audio.export(target, format='wav')
            rows.append({'id': index, 'uid': f'row{index}',
                         'speaker': 'A' if index % 2 == 0 else 'B',
                         'text': f'Chapter {index // 3 + 1}' if index % 3 == 0 else
                                 'This is a long narrative sentence. ' * 4,
                         'audio_path': path.name, 'pause_after': 100 if index % 2 == 0 else None})
        manager.save_chunks(rows)
        return manager

    def test_full_export_combines_only_one_chapter_and_releases_previous_render(self):
        with tempfile.TemporaryDirectory() as root:
            manager = self.make_project(root)
            original = project.combine_audio_with_pauses
            sizes, renders = [], []

            def combine(segments, *args, **kwargs):
                self.assertTrue(all(ref() is None for ref in renders),
                                'the prior chapter render is still retained')
                sizes.append(len(segments))
                self.assertLessEqual(len(segments), 3, 'whole-book combined render')
                audio = original(segments, *args, **kwargs)
                renders.append(weakref.ref(audio))
                return audio

            with patch('project.combine_audio_with_pauses', side_effect=combine):
                ok, message = manager.export_chapters(fmt='wav')
            self.assertTrue(ok, message)
            self.assertEqual([3, 3], sizes)
            self.assertTrue(all(ref() is None for ref in renders))
            manifest = json.loads(Path(root, CHAPTER_EXPORT_DIR, 'manifest.json').read_text())
            self.assertEqual([[0, 2], [3, 5]], [row['chunks'] for row in manifest['chapters']])
            for row in manifest['chapters']:
                with Path(root, CHAPTER_EXPORT_DIR, row['file']).open('rb') as source:
                    audio = AudioSegment.from_file(source, format='wav')
                self.assertAlmostEqual(row['end_ms'] - row['start_ms'], len(audio), delta=1)

    def test_subset_does_not_render_unrequested_chapters(self):
        with tempfile.TemporaryDirectory() as root:
            manager = self.make_project(root)
            sizes, original = [], project.combine_audio_with_pauses

            def combine(segments, *args, **kwargs):
                sizes.append(len(segments))
                return original(segments, *args, **kwargs)

            with patch('project.combine_audio_with_pauses', side_effect=combine):
                ok, message = manager.export_chapters(fmt='wav', chapters=[1])
            self.assertTrue(ok, message)
            self.assertEqual([3], sizes)
            manifest = json.loads(Path(root, CHAPTER_EXPORT_DIR, 'manifest.json').read_text())
            self.assertEqual([1], [row['index'] for row in manifest['chapters']])

    def test_full_and_changed_only_render_the_same_changed_chapter_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            manager = self.make_project(root)
            self.assertTrue(manager.export_chapters(fmt='wav')[0])
            out = Path(root, CHAPTER_EXPORT_DIR)
            original_first = (out / '01 - Chapter 1.wav').read_bytes()
            replacement = AudioSegment(data=(10000).to_bytes(2, 'little') * 4800,
                                       sample_width=2, frame_rate=24000, channels=1)
            with Path(root, 'line4.wav').open('wb') as target:
                replacement.export(target, format='wav')
            self.assertTrue(manager.export_chapters(fmt='wav', changed_only=True)[0])
            changed = (out / '02 - Chapter 2.wav').read_bytes()
            self.assertEqual(original_first, (out / '01 - Chapter 1.wav').read_bytes())
            self.assertTrue(manager.export_chapters(fmt='wav')[0])
            self.assertEqual(changed, (out / '02 - Chapter 2.wav').read_bytes())


if __name__ == '__main__':
    unittest.main()
