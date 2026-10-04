import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import project
from routers import editor
from tests import test_chapter_export as fixtures


SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class ChapterPreviewSelectionTests(unittest.TestCase):
    def test_http_preview_matches_actual_writes_for_subset_changes_and_legacy_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            pm, chunks = fixtures._project(root)
            pm.save_chunks(chunks)
            app = FastAPI(); app.include_router(editor.router)
            def verify(client, indices=None, changed=False):
                params = [('format', 'wav'), ('changed_only', str(changed).lower())]
                if indices is not None:
                    params.extend(('chapters', index) for index in indices)
                response = client.get('/api/export_chapters/preview', params=params)
                self.assertEqual(200, response.status_code, response.text)
                names = [row['file'] for row in response.json()['chapters']]
                writes = []
                original = project._export_audio_segment
                def capture(segment, path, *args, **kwargs):
                    writes.append(Path(path).name)
                    return original(segment, path, *args, **kwargs)
                with patch.object(project, '_export_audio_segment', side_effect=capture):
                    ok, message = pm.export_chapters(fmt='wav', chapters=indices, changed_only=changed)
                self.assertTrue(ok, message)
                self.assertEqual(names, writes)
                return names
            with patch.object(editor, 'project_manager', pm), TestClient(app) as client:
                self.assertEqual(['02 - Chapter 2.wav'], verify(client, [1, 1]))
                self.assertEqual(['01 - Chapter 1.wav'], verify(client, [0], True))
                self.assertEqual([], verify(client, None, True))
                fixtures._tone(str(Path(root, chunks[4]['audio_path'])), 0.9, hz=570)
                self.assertEqual([], verify(client, [0], True))
                self.assertEqual(['02 - Chapter 2.wav'], verify(client, [1], True))
                Path(root, project.CHAPTER_EXPORT_DIR, '01 - Chapter 1.wav').unlink()
                self.assertEqual(['01 - Chapter 1.wav'], verify(client, None, True))
                manifest_path = Path(root, project.CHAPTER_EXPORT_DIR, 'manifest.json')
                manifest = json.loads(manifest_path.read_text()); manifest.pop('chapter_plan_version')
                manifest_path.write_text(json.dumps(manifest))
                self.assertEqual(['01 - Chapter 1.wav', '02 - Chapter 2.wav'], verify(client, None, True))
                for indices in ([], [-1], [2]):
                    with self.subTest(indices=indices):
                        # Empty explicit selection is tested directly because an
                        # empty repeated query is indistinguishable from omission.
                        if not indices:
                            with self.assertRaises(ValueError):
                                pm.preview_chapter_filenames(fmt='wav', chapters=[])
                        else:
                            response = client.get('/api/export_chapters/preview',
                                params=[('format', 'wav')] + [('chapters', x) for x in indices])
                            self.assertEqual(400, response.status_code, response.text)

    def test_preview_is_read_only_and_normalizes_selected_numbering(self):
        with tempfile.TemporaryDirectory() as root:
            pm, chunks = fixtures._project(root); pm.save_chunks(chunks)
            pm.load_chunks()  # Existing legacy UID migration, outside the measurement.
            before = {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob('*') if p.is_file()}
            rows = pm.preview_chapter_filenames(fmt='wav', chapters=[1, 0, 1])
            self.assertEqual([1, 2], [row['number'] for row in rows])
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob('*') if p.is_file()})

    def test_actual_ui_handler_sends_selection_and_changed_only(self):
        source = SOURCE
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const marker="document.getElementById('chapter-preview-btn').addEventListener";
const start=source.indexOf('async function getChapterExportPreview('),end=source.indexOf("document.getElementById('chapter-export-btn')",start);
assert(start>=0&&end>start);
let handler, seen, finished=false;
process.on('beforeExit',()=>assert(finished));
const nodes=new Map(), node=id=>{if(!nodes.has(id)){nodes.set(id,{textContent:'',innerHTML:'',style:{},addEventListener:(event,fn)=>handler=fn});}return nodes.get(id);};
let options={format:'wav',per_chunk_chapters:false,template:'{chapter_number} - {chapter_name}',padding:2,book_name:'Book',series_name:'Series',volume_number:'1',chapters:[0,2],changed_only:true};
const context={document:{getElementById:node},chapterExportParams:()=>options,currentBookFilename:'fixture.txt',URLSearchParams,
 API:{get:async url=>{seen=new URL(url,'http://fixture');return{chapters:[]};}},escapeHtml:value=>value,showToast:message=>{throw Error(message);}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function renderChapterList('),source.indexOf('async function loadChapterExports(')),context);
vm.runInContext(source.slice(start,end),context);
(async()=>{await handler();assert.deepStrictEqual(seen.searchParams.getAll('chapters'),['0','2']);assert.strictEqual(seen.searchParams.get('changed_only'),'true');assert(node('chapter-list').innerHTML.includes('No chapter files would be written.'));
options={...options,chapters:null,changed_only:false};await handler();assert.deepStrictEqual(seen.searchParams.getAll('chapters'),[]);assert.strictEqual(seen.searchParams.get('changed_only'),'false');finished=true;})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
