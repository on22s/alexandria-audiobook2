"""Actual preview readiness and filename/reuse parity with native chapter export."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from routers import editor
from tests import test_export_task_results as export_native
from tests.test_chapter_export import _project
from tests import test_training_ui_contract as training_ui


class ChapterPreviewParityTests(unittest.TestCase):
    fixture=export_native.ExportTaskResultTests.fixture

    def test_native_readiness_errors_match_export_and_selected_changed_only_preview_matches_files(self):
        with self.fixture() as (root,states,client):
            pm,chunks=_project(root);source=Path(root,'script.json');voices=Path(root,'voice_config.json');source.write_text(json.dumps([{'speaker':'NARRATOR'},{'speaker':'MIA'}]));voices.write_text(json.dumps({'NARRATOR':{'ready':False},'MIA':{'ready':True}}))
            with patch.object(editor,'SCRIPT_PATH',str(source)),patch.object(editor,'project_manager',pm),patch.object(pm,'load_chunks',return_value=chunks):
                before={p.name:p.read_bytes() for p in (source,voices)}
                preview=client.get('/api/export_chapters/preview',params={'format':'wav','require_ready':True});export=client.post('/api/export_chapters',json={'format':'wav','require_ready':True})
                self.assertEqual(409,preview.status_code);self.assertEqual(preview.json(),export.json());self.assertEqual(['NARRATOR'],preview.json()['detail']['speakers']);self.assertFalse(states['chapter_export']['running']);self.assertEqual(before,{p.name:p.read_bytes() for p in (source,voices)})
                self.assertEqual(200,client.get('/api/export_chapters/preview',params={'format':'wav','require_ready':False}).status_code)
                source.write_text('{}');preview=client.get('/api/export_chapters/preview',params={'require_ready':True});export=client.post('/api/export_chapters',json={'require_ready':True});self.assertEqual(503,preview.status_code);self.assertEqual(preview.json(),export.json())
                source.write_text(json.dumps([{'speaker':'NARRATOR'},{'speaker':'MIA'}]));voices.write_text(json.dumps({'NARRATOR':{'ready':True},'MIA':{'ready':True}}))
                preview=client.get('/api/export_chapters/preview',params=[('format','wav'),('chapters',1),('changed_only',False),('require_ready',True)]);self.assertEqual(200,preview.status_code,preview.text);files=[r['file'] for r in preview.json()['chapters']];self.assertEqual(1,len(files))
                response=client.post('/api/export_chapters',json={'format':'wav','chapters':[1],'require_ready':True});self.assertEqual(200,response.status_code,response.text);self.assertEqual('done',states['chapter_export']['result']['status'])
                out=Path(root,'chapter_exports');saved={p.name:p.read_bytes() for p in out.glob('*.wav')};self.assertEqual(files,list(saved))
                reused=client.get('/api/export_chapters/preview',params=[('format','wav'),('chapters',1),('changed_only',True),('require_ready',True)]);self.assertEqual(200,reused.status_code,reused.text);self.assertEqual([],reused.json()['chapters'])
                response=client.post('/api/export_chapters',json={'format':'wav','chapters':[1],'changed_only':True,'require_ready':True});self.assertEqual(200,response.status_code,response.text);self.assertEqual(saved,{p.name:p.read_bytes() for p in out.glob('*.wav')})

    def test_actual_frontend_preview_serializes_all_export_filters(self):
        training_ui.TrainingUiContractTests().run_js(r'''
const start=core.indexOf('function parseChapterSelection('),end=core.indexOf('function renderChapterList(',start);vm.runInContext(core.slice(start,end),ctx);ctx.URLSearchParams=URLSearchParams;
for(const [id,value] of Object.entries({'chapter-format':'wav','chapter-template':'{chapter_number} - {chapter_name}','chapter-padding':'2','chapter-book-name':'book','chapter-series-name':'series','chapter-volume':'3','chapter-selection':'1,3-4'})){element(id).value=value;}
element('chapter-per-chunk').checked=false;element('chapter-changed-only').checked=true;element('chapter-require-ready').checked=true;
let handler;const renders=[],requests=[];element('chapter-preview-btn').addEventListener=(_name,fn)=>handler=fn;ctx.renderChapterList=(rows,exported)=>renders.push([rows,exported]);ctx.API=vm.runInContext('API',ctx);ctx.API.get=async url=>{requests.push(url);return {chapters:[{file:'one.wav'}]};};
ctx.currentBookFilename='fixture.txt';const a=core.indexOf('async function getChapterExportPreview('),b=core.indexOf("document.getElementById('chapter-export-btn').addEventListener",a);vm.runInContext(core.slice(a,b),ctx);await handler();
const query=new URL(requests[0],'http://fixture').searchParams;assert.deepStrictEqual(query.getAll('chapters'),['0','2','3']);assert.strictEqual(query.get('changed_only'),'true');assert.strictEqual(query.get('require_ready'),'true');assert.strictEqual(query.get('format'),'wav');assert.strictEqual(query.get('book_name'),'book');assert.strictEqual(renders.length,1);assert.strictEqual(renders[0][1],false);assert.match(element('chapter-status').textContent,/1 chapter/);
element('chapter-selection').value='';element('chapter-changed-only').checked=false;element('chapter-require-ready').checked=false;await handler();const all=new URL(requests[1],'http://fixture').searchParams;assert.deepStrictEqual(all.getAll('chapters'),[]);assert.strictEqual(all.get('changed_only'),'false');assert.strictEqual(all.get('require_ready'),'false');
ctx.API.get=async()=>{throw Error('not ready');};await handler();assert(toasts.at(-1)[0].includes('not ready'));assert.strictEqual(renders.length,2,'failed preview must not render an invented successful file set');
''')
