import builtins
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor


class EditorZipServiceTests(unittest.TestCase):
    def test_both_http_families_use_shared_disk_archive_with_bounded_source_reads(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            audio, m4b = root / 'book.mp3', root / 'book.m4b'
            chapter_dir = root / 'chapter_exports'; chapter_dir.mkdir()
            chapter = chapter_dir / 'one.wav'
            for path, value in ((audio, b'A'), (m4b, b'B'), (chapter, b'C')):
                path.write_bytes(value * (2 * 1024 * 1024 + 17))
            (chapter_dir / 'manifest.json').write_text(json.dumps({'chapters': [{'index': 0, 'file': chapter.name}]}))
            sources = {str(path): path.read_bytes() for path in (audio, m4b, chapter)}
            calls, reads = [], []
            original_open, builder = builtins.open, editor.build_zip_download
            class BoundedReader:
                def __init__(self, stream): self.stream = stream
                def __getattr__(self, name): return getattr(self.stream, name)
                def __enter__(self): return self
                def __exit__(self, *args): return self.stream.__exit__(*args)
                def read(self, size=-1):
                    if not 0 < size <= 65536:
                        raise AssertionError(f'unbounded archive source read: {size}')
                    reads.append(size)
                    return self.stream.read(size)
            def open_source(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                return BoundedReader(stream) if str(path) in sources and args and args[0] == 'rb' else stream
            def build(members, filename):
                response = builder(members, filename)
                self.assertTrue(Path(response.path).is_file())
                calls.append((filename, response.path))
                return response
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'DATA_DIR', str(root)), \
                 patch.object(editor, 'AUDIOBOOK_PATH', str(audio)), \
                 patch.object(editor, 'M4B_PATH', str(m4b)), \
                 patch.object(editor, 'build_zip_download', side_effect=build), \
                 patch.object(builtins, 'open', side_effect=open_source), TestClient(app) as client:
                for route, expected in (('/api/export_zip', {'audiobook.mp3': sources[str(audio)], 'audiobook.m4b': sources[str(m4b)]}),
                                        ('/api/chapter_exports/zip', {'one.wav': sources[str(chapter)]})):
                    response = client.get(route)
                    self.assertEqual(200, response.status_code, response.text[:100])
                    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                        self.assertEqual(set(expected), set(archive.namelist()))
                        for name, data in expected.items():
                            self.assertEqual(hashlib.sha256(data).digest(), hashlib.sha256(archive.read(name)).digest())
                    self.assertFalse(Path(calls[-1][1]).exists(), 'completed FileResponse did not remove its archive')
            self.assertEqual(['alexandria_export.zip', 'chapters.zip'], [name for name, _ in calls])
            self.assertGreater(len(reads), 90)
            self.assertFalse(list(root.glob('.alexandria-export-*.zip')))

    def test_archive_failure_removes_own_temp_and_preserves_foreign_archive(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            foreign = root / '.alexandria-export-foreign.zip'; foreign.write_bytes(b'keep')
            source = root / 'source.mp3'; source.write_bytes(b'audio')
            with patch.object(editor, 'DATA_DIR', str(root)), \
                 patch.object(zipfile.ZipFile, 'write', side_effect=OSError('fixture failed source read')):
                with self.assertRaisesRegex(OSError, 'fixture failed'):
                    editor.build_zip_download([(str(source), source.name)], 'export.zip')
            self.assertEqual(b'keep', foreign.read_bytes())
            self.assertEqual([foreign], list(root.glob('.alexandria-export-*.zip')))
