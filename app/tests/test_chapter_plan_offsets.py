import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from project import CHAPTER_EXPORT_DIR
from pydub import AudioSegment
from tests import test_chapter_render_memory as fixtures


class ChapterPlanOffsetTests(unittest.TestCase):
    def get_manifest(self, root):
        return json.loads(Path(root, CHAPTER_EXPORT_DIR, 'manifest.json').read_text())

    def test_changed_boundary_pause_repositions_reused_chapters_like_full_export(self):
        for per_chunk in (True, False):
            with self.subTest(per_chunk=per_chunk), tempfile.TemporaryDirectory() as root:
                manager = fixtures.ChapterRenderMemoryTests().make_project(root)
                self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=per_chunk)[0])
                before = self.get_manifest(root)
                paths = [Path(root, CHAPTER_EXPORT_DIR, row['file']) for row in before['chapters']]
                old_bytes = [path.read_bytes() for path in paths]
                rows = manager.load_chunks()
                boundary = 0 if per_chunk else 2
                rows[boundary]['pause_after'] += 550
                manager.save_chunks(rows)
                loads, load = [], manager._load_chunks_with_audio

                def record_load(*args, **kwargs):
                    loads.append(len(kwargs['chunks']))
                    return load(*args, **kwargs)

                with patch.object(manager, '_load_chunks_with_audio', side_effect=record_load):
                    ok, message = manager.export_chapters(fmt='wav', per_chunk_chapters=per_chunk,
                                                         changed_only=True)
                self.assertTrue(ok, message)
                incremental = self.get_manifest(root)
                self.assertEqual([1 if per_chunk else 3], loads)
                self.assertEqual(before['chapters'][1]['start_ms'] + 550,
                                 incremental['chapters'][1]['start_ms'])
                self.assertEqual(old_bytes, [path.read_bytes() for path in paths],
                                 'interchapter pauses must not enter chapter audio')
                self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=per_chunk)[0])
                self.assertEqual(incremental, self.get_manifest(root))
                self.assertEqual(old_bytes, [path.read_bytes() for path in paths])

    def test_defaults_change_repositions_reused_single_chunk_chapters(self):
        with tempfile.TemporaryDirectory() as root:
            manager = fixtures.ChapterRenderMemoryTests().make_project(root)
            self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True)[0])
            before = self.get_manifest(root)
            manager._load_pause_defaults = lambda: (900, 200)
            ok, message = manager.export_chapters(fmt='wav', per_chunk_chapters=True, changed_only=True)
            self.assertTrue(ok, message)
            incremental = self.get_manifest(root)
            self.assertEqual(before['chapters'][2]['start_ms'] + 500,
                             incremental['chapters'][2]['start_ms'])
            self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True)[0])
            self.assertEqual(incremental, self.get_manifest(root))

    def test_first_subset_uses_the_same_nominal_timeline_as_full_export(self):
        with tempfile.TemporaryDirectory() as root:
            manager = fixtures.ChapterRenderMemoryTests().make_project(root)
            rows = manager.load_chunks()
            repeated = []
            for index in range(120):
                row = dict(rows[index % len(rows)])
                row.update(id=index, uid=f"long{index}", pause_after=100,
                           text=f"Chapter {index // 60 + 1}" if index % 60 == 0 else
                                "This is long enough narrative prose. " * 4)
                repeated.append(row)
            manager.save_chunks(repeated)
            self.assertTrue(manager.export_chapters(fmt='wav')[0])
            full = self.get_manifest(root)
            shutil.rmtree(Path(root, CHAPTER_EXPORT_DIR))
            self.assertTrue(manager.export_chapters(fmt='wav', chapters=[1])[0])
            subset = self.get_manifest(root)
            self.assertEqual(full['chapters'][1], subset['chapters'][0])

    def test_legacy_manifest_rebuilds_before_changed_only_reuse(self):
        with tempfile.TemporaryDirectory() as root:
            manager = fixtures.ChapterRenderMemoryTests().make_project(root)
            self.assertTrue(manager.export_chapters(fmt='wav')[0])
            manifest = self.get_manifest(root)
            path = Path(root, CHAPTER_EXPORT_DIR, 'manifest.json')
            manifest.pop('chapter_plan_version', None)
            path.write_text(json.dumps(manifest))
            original, loads = manager._load_chunks_with_audio, []

            def record_load(*args, **kwargs):
                loads.append(kwargs.get('chunks'))
                return original(*args, **kwargs)

            with patch.object(manager, '_load_chunks_with_audio', side_effect=record_load):
                ok, message = manager.export_chapters(fmt='wav', changed_only=True)
            self.assertTrue(ok, message)
            self.assertIn('2 chapter file(s) written', message)
            self.assertEqual([None], loads)
            self.assertEqual(1, self.get_manifest(root)['chapter_plan_version'])
            with patch.object(manager, '_load_chunks_with_audio', side_effect=AssertionError('unexpected decode')):
                ok, message = manager.export_chapters(fmt='wav', changed_only=True)
            self.assertTrue(ok, message)
            self.assertIn('0 chapter file(s) written, 2 unchanged and kept', message)
