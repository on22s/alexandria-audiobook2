"""Known unchanged pre-scope outputs stay preserved and cannot be downloaded."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from project_scope_exports import get_scope_export_inventory, is_scope_export_historical
from routers import editor


class ScopeExportTests(unittest.TestCase):
    def test_preserved_mp3_and_chapters_are_fenced_until_their_file_identity_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();mp3=root/'cloned_audiobook.mp3';mp3.write_bytes(b'old output')
            chapters=root/'chapter_exports';chapters.mkdir();chapter=chapters/'one.mp3';chapter.write_bytes(b'old chapter')
            baseline=get_scope_export_inventory(root)
            (root/'state.json').write_text(json.dumps({'_source_scope_exports':baseline}))
            with patch.object(editor,'DATA_DIR',str(root)),patch.object(editor,'AUDIOBOOK_PATH',str(mp3)),\
                    patch.object(editor,'_chapter_export_dir',return_value=str(chapters)):
                with self.assertRaises(HTTPException) as blocked:asyncio.run(editor.get_audiobook())
                self.assertEqual(409,blocked.exception.status_code)
                with self.assertRaises(HTTPException) as blocked:editor._exported_chapter_path('one.mp3')
                self.assertEqual(409,blocked.exception.status_code)
                self.assertEqual(b'old output',mp3.read_bytes());self.assertEqual(b'old chapter',chapter.read_bytes())
                mp3.write_bytes(b'new unverified output')
                self.assertFalse(is_scope_export_historical(root,mp3))
                self.assertEqual(str(mp3),asyncio.run(editor.get_audiobook()).path)
                # Replacing one delivery must not release an unchanged chapter.
                self.assertTrue(is_scope_export_historical(root,chapter))
