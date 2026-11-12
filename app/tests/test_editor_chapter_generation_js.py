"""Generate by chapter (#1921): real panel code and the real shared batch renderer.

What these verify:
- the progress map classifies chapters from real line statuses (done, partial,
  pending, failed), counting only non-empty lines;
- the panel sends the parsed chapter list to the repair preview, refuses chapter
  numbers the book does not have before any request, starts nothing when every
  line already has audio, and asks the renderer to replace audio only when
  Regenerate is ticked;
- the shared renderer, given a chapter preview without Regenerate, renders only
  lines without audio and never shows the replace-audio confirmation.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_editor_render_barrier import HARNESS
from tests.test_editor_repair_js import UI_SETUP
from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static'
CORE = (STATIC / 'js/app-core.js').read_text(encoding='utf-8')
PARSER = CORE[CORE.index('function parseChapterSelection('):CORE.index('function chapterExportParams(')]
PANEL_SETUP = r'''
vm.runInContext(%s,context);
for(const id of ['editor-chapter-selection','editor-chapter-regenerate','editor-chapter-status','editor-chapter-summary','editor-chapter-map']){body.appendChild(new Element('div',{id}));}
const chapterEl=id=>document.getElementById('editor-chapter-'+id);chapterEl('regenerate').checked=false;
const options={book_token:context.chunkSnapshotBookToken,options_revision:'f'.repeat(64),speakers:['Hero','NARRATOR'],
  chapters:[{index:0,title:'One',first:0,last:1},{index:1,title:'Two',first:2,last:3},{index:2,title:'Three',first:4,last:4}]};
const previews=[];let previewRows=[];
context.API={get:async()=>options,post:async(url,data)=>{previews.push({url,data:JSON.parse(JSON.stringify(data))});return {book_token:context.chunkSnapshotBookToken,repair_revision:'c'.repeat(64),rows:previewRows,rendered_count:0};}};
''' % json.dumps(PARSER)


class EditorChapterGenerationJsTests(unittest.TestCase):
    def run_ui(self, code):
        script = DOM_FIXTURE + UI_SETUP + PANEL_SETUP + code + '\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(STATIC / 'js/app-editor-repair.js')],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_progress_map_states_count_only_non_empty_lines(self):
        self.run_ui(r'''
const chunks=[{id:0,text:'a',status:'done'},{id:1,text:'b',status:'done'},{id:2,text:'c',status:'done'},{id:3,text:'d',status:'pending'},{id:4,text:'   ',status:'pending'}];
const states=context.getEditorChapterProgress(options.chapters,chunks).map(c=>[c.number,c.state,c.done,c.total]);
assert.deepEqual(JSON.parse(JSON.stringify(states)),[[1,'done',2,2],[2,'partial',1,2],[3,'pending',0,0]]);
chunks[3].status='error';assert.equal(context.getEditorChapterProgress(options.chapters,chunks)[1].state,'failed');
context.renderEditorChapterMap(context.getEditorChapterProgress(options.chapters,chunks));
assert.equal(chapterEl('map').children.length,3);assert.equal(chapterEl('map').children[1].className,'bg-danger');
assert.match(chapterEl('summary').textContent,/^1 of 3 chapters fully generated, 0 partly generated, 1 with failed lines\.$/);
''')

    def test_panel_sends_chapters_skips_done_and_regenerates_only_when_ticked(self):
        self.run_ui(r'''
chapterEl('selection').value='3, 1-2';previewRows=[{id:0,uid:'u0',text:'a',status:'done'},{id:3,uid:'u3',text:'d',status:'pending'}];
await context.generateEditorChapters();
assert.equal(previews.length,1);assert.deepEqual(previews[0].data.chapters,[0,1,2]);assert.deepEqual(previews[0].data.speakers,['Hero','NARRATOR']);
assert.equal(starts.length,1);assert.equal(starts[0][0],false);assert.equal(starts[0][1].rows.length,2);
assert.deepEqual(JSON.parse(JSON.stringify(starts[0][1].getFilters())),{chapters:previews[0].data.chapters,speakers:previews[0].data.speakers});
chapterEl('selection').value='9';assert.deepEqual(JSON.parse(JSON.stringify(starts[0][1].getFilters().chapters)),[8]);
previewRows=[{id:0,uid:'u0',text:'a',status:'done'}];chapterEl('selection').value='1';
await context.generateEditorChapters();assert.equal(starts.length,1,'all lines done: nothing starts');assert.match(chapterEl('status').textContent,/already has audio/);
chapterEl('regenerate').checked=true;await context.generateEditorChapters();assert.equal(starts.length,2);assert.equal(starts[1][0],true);
''')

    def test_out_of_range_and_empty_input_never_request_a_preview(self):
        self.run_ui(r'''
for(const value of ['4','2, 7-8','','abc']){chapterEl('selection').value=value;await context.generateEditorChapters();}
assert.equal(previews.length,0);assert.equal(starts.length,0);assert.equal(errors.length,4);
assert.match(errors[0][1].message,/has 3 chapters, so chapter 4 does not exist/);assert.match(errors[1][1].message,/chapter 7 does not exist/);
''')

    def run_render(self, code):
        with tempfile.TemporaryDirectory() as root:
            source = (STATIC / 'js/app-editor-repair.js').read_text(encoding='utf-8')
            helper = source[source.index('function getEditorRepairSelection('):source.index('async function generateEditorRepair(')]
            extra = '\nconst helper=' + json.dumps(helper) + ';\n' + code
            result = subprocess.run(['node', '-e', HARNESS + extra, str(STATIC / 'js/app-core.js'), str(Path(root) / 'rows.json')],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_renderer_without_regenerate_skips_done_lines_and_never_confirms(self):
        self.run_render(r'''
(async()=>{for(const button of ['renderAll','renderBatchFast']){for(const status of ['pending','done']){
const a=client();a.setHold(false);const rows=await a.context.ensureEditorRenderSnapshot();const filters={chapters:[0],speakers:['NEW SPEAKER']};vm.runInContext(helper,a.context);
const snapshot=a.context.ensureEditorRenderSnapshot;a.context.ensureEditorRenderSnapshot=async()=>(await snapshot()).map(row=>({...row,status}));
let confirms=0;a.context.showConfirm=async()=>{confirms+=1;return true;};
const preview={book_token:'book-token',repair_revision:'e'.repeat(64),filters,getFilters:()=>filters,rows};
await a.context[button](false,preview);assert.equal(confirms,0);
assert.equal(a.batches.length,status==='pending'?1:0,button+' '+status);if(status==='pending'){assert.deepEqual(a.batches[0].data.indices,[7]);}
}}finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')


if __name__ == '__main__':
    unittest.main()
