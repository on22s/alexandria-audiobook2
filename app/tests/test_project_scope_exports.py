"""Known unchanged pre-scope outputs stay preserved and cannot be downloaded."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import HTTPException
import project_scope_exports
from project_scope_exports import (get_scope_export_inventory, is_scope_export_historical,
                                   is_scope_export_inventory_compatible,
                                   require_scope_export_current)
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

    def test_malformed_export_proof_refuses_historical_and_current_readers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();mp3=root/'cloned_audiobook.mp3';mp3.write_bytes(b'')
            baseline=get_scope_export_inventory(root)
            cases=[]
            for legacy in (True, False):
                for bad in (False, 0.0):
                    cases.append((f"{'legacy' if legacy else 'current'}:{bad!r}", legacy, bad))
            cases.append(('legacy:malformed-version', True, 'malformed'))
            cases.append(('current:malformed-rows', False, 'malformed-rows'))
            for label, legacy, bad in cases:
                with self.subTest(variant=label):
                    inventory=copy.deepcopy(baseline)
                    if legacy:
                        inventory={name:inventory[name] for name in project_scope_exports.LEGACY_EXPORTS}
                    if bad == 'malformed-rows':
                        inventory['cloned_audiobook.mp3']=[]
                    elif bad == 'malformed':
                        inventory['cloned_audiobook.mp3']['cloned_audiobook.mp3']=[0,0]
                    else:
                        row=inventory['cloned_audiobook.mp3']['cloned_audiobook.mp3']
                        self.assertEqual(0, row[2]); self.assertIs(int, type(row[2]))
                        row[2]=bad
                    state=root/'state.json'; state.write_text(json.dumps({'_source_scope_exports':inventory}))
                    before_state=state.read_bytes(); before_mp3=mp3.read_bytes()
                    with self.assertRaisesRegex(ValueError, 'needs inspection'):
                        is_scope_export_historical(root, mp3)
                    with self.assertRaisesRegex(ValueError, 'needs inspection'):
                        require_scope_export_current(root, mp3)
                    self.assertEqual(before_state, state.read_bytes())
                    self.assertEqual(before_mp3, mp3.read_bytes())

    def test_export_inventory_row_limit_pure_compatibility(self):
        def rows(count):
            return {f'synthetic/file-{i}.txt':[1,2,3,4,5] for i in range(count)}
        for legacy in (True, False):
            for count, expected in ((4096, True), (4097, False)):
                with self.subTest(layout='legacy' if legacy else 'current', rows=count):
                    if legacy:
                        recorded={name:{} for name in project_scope_exports.LEGACY_EXPORTS}
                        recorded['cloned_audiobook.mp3']=rows(count)
                        current={name:{} for name in project_scope_exports.EXPORTS}
                        current['cloned_audiobook.mp3']=copy.deepcopy(recorded['cloned_audiobook.mp3'])
                    else:
                        recorded={name:{} for name in project_scope_exports.EXPORTS}
                        recorded['cloned_audiobook.mp3']=rows(count)
                        current=copy.deepcopy(recorded)
                    self.assertEqual(expected, is_scope_export_inventory_compatible(recorded, current))

    def test_export_inventory_row_limit_historical_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();mp3=root/'cloned_audiobook.mp3';mp3.write_bytes(b'')
            baseline=get_scope_export_inventory(root)
            real_row=baseline['cloned_audiobook.mp3']['cloned_audiobook.mp3']
            for legacy in (True, False):
                for count, accepted in ((4096, True), (4097, False)):
                    with self.subTest(layout='legacy' if legacy else 'current', rows=count):
                        filled={'cloned_audiobook.mp3':real_row}
                        filled.update({f'synthetic/file-{i}.txt':[1,2,3,4,5] for i in range(count-1)})
                        keys=project_scope_exports.LEGACY_EXPORTS if legacy else project_scope_exports.EXPORTS
                        inventory={name:{} for name in keys}
                        inventory['cloned_audiobook.mp3']=filled
                        state=root/'state.json'; state.write_text(json.dumps({'_source_scope_exports':inventory}))
                        before_state=state.read_bytes(); before_mp3=mp3.read_bytes()
                        if accepted:
                            self.assertTrue(is_scope_export_historical(root, mp3))
                            with self.assertRaises(HTTPException) as blocked:
                                require_scope_export_current(root, mp3)
                            self.assertEqual(409, blocked.exception.status_code)
                        else:
                            with self.assertRaisesRegex(ValueError, 'needs inspection'):
                                is_scope_export_historical(root, mp3)
                            with self.assertRaisesRegex(ValueError, 'needs inspection'):
                                require_scope_export_current(root, mp3)
                        self.assertEqual(before_state, state.read_bytes())
                        self.assertEqual(before_mp3, mp3.read_bytes())
