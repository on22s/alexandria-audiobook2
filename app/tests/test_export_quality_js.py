"""Result checks stay book scoped and text safe across exports and reconnects."""
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static'
SETUP = r'''
Element.prototype.replaceChildren=function(...nodes){for(const node of [...this.children]){node.remove();}for(const node of nodes){this.appendChild(node);}};
for(const id of ['export-quality-noise','export-quality-gap','export-quality-reject','export-quality-reports','export-quality-status','m4b-status','m4b-per-chunk','m4b-title','m4b-author','m4b-narrator','m4b-year','m4b-description','m4b-require-ready','m4b-cancel-btn','chapter-status','chapter-cancel-btn','audacity-status','chapter-format','chapter-per-chunk','chapter-template','chapter-padding','chapter-book-name','chapter-series-name','chapter-volume','chapter-selection','chapter-changed-only','chapter-require-ready']){body.appendChild(new Element('div',{id}));}
document.getElementById('export-quality-noise').value='-50';document.getElementById('export-quality-gap').value='5';
context.window=context;context.chunkSnapshotBookToken='b'.repeat(64);context.chunkSnapshotBook='fixture';context.currentBookFilename='fixture';const errors=[],posts=[];
context.showActionError=(...args)=>errors.push(args);context.escapeHtml=v=>v;context.getActionErrorMessage=(...args)=>args.join(' ');context.claimTaskStart=()=>true;context.releaseTaskStart=()=>{};context.pollExport=()=>{};
const report={file:'<img src=x onerror=alert(1)>.mp3',status:'review',expected_format:'mp3',expected_frames:24000,decoded_frames:24000,sample_rate:24000,channels:1,unexpected_silence_count:1,unexpected_silence_ranges:[{start_seconds:.1,end_seconds:.5}],options:{silence_dbfs:-50,max_unexpected_silence_seconds:.2,reject_unexpected_silence:false},sha256:'synthetic-sha'};
const rejection={task:'audio',status:'failed',stage:'decode',reason:'<script>alert(2)</script>',diagnostics:[{kind:'chapter',chapter:1,expected_start_seconds:1,expected_end_seconds:2,actual_start_seconds:3,actual_end_seconds:4}]};
const result={book_token:'b'.repeat(64),reports:[report],rejected_attempts:[rejection],attempts:[{task:'audio',status:'failed',completed_files:0}],unavailable:2,reports_per_task_limit:32,rejections_per_task_limit:16,identity_check:'metadata_only'};
context.API={get:async()=>result,post:async(url,data)=>{posts.push({url,data});return{};}};
load();const core=fs.readFileSync(process.argv[2],'utf8');let finished=false;process.on('beforeExit',()=>assert(finished,'scenario must finish'));(async()=>{
'''


class ExportQualityJsTests(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(['node', '-e', DOM_FIXTURE + SETUP + code +
            '\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});',
            str(STATIC / 'js/app-export-quality.js'), str(STATIC / 'js/app-core.js')],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_options_reach_actual_export_request_and_bad_values_never_post(self):
        self.run_js(r'''
const start=core.indexOf('window.exportM4B =');vm.runInContext(core.slice(start,core.indexOf('\n        };',start)+12),context);
document.getElementById('export-quality-noise').value='-42';document.getElementById('export-quality-gap').value='.4';document.getElementById('export-quality-reject').checked=true;
await context.exportM4B();assert.equal(posts.length,1);assert.deepEqual(JSON.parse(JSON.stringify(posts[0].data.export_quality)),{silence_dbfs:-42,max_unexpected_silence_seconds:.4,reject_unexpected_silence:true});
for(const [id,value] of [['export-quality-gap','Infinity'],['export-quality-noise','-100'],['export-quality-gap','0'],['export-quality-noise','NaN']]){const input=document.getElementById(id),before=input.value;input.value=value;await context.exportM4B();assert.equal(posts.length,1);input.value=before;}
context.parseChapterSelection=()=>null;document.getElementById('chapter-format').value='wav';document.getElementById('chapter-template').value='{chapter_name}';document.getElementById('chapter-padding').value='2';for(const id of ['chapter-book-name','chapter-series-name','chapter-volume','chapter-selection']){document.getElementById(id).value='';}
vm.runInContext(core.slice(core.indexOf('function chapterExportParams('),core.indexOf('function renderChapterList(')),context);assert.equal(context.chapterExportParams().export_quality.silence_dbfs,-42);
''')

    def test_reports_are_safe_text_with_separate_failure_and_explicit_scope(self):
        self.run_js(r'''
await context.loadExportQualityReports();const container=document.getElementById('export-quality-reports');assert.equal(container.children.length,2);
assert(container.children[0].children[0].textContent.includes('<img src=x onerror=alert(1)>'));
assert(container.children[1].children.some(node=>node.textContent.includes('<script>alert(2)</script>')));
assert(container.children[1].children.some(node=>node.textContent.includes('Chapter 2: expected 1–2 seconds; found 3–4 seconds')));
assert(container.children[0].children.some(node=>node.textContent.includes('Silence: 0.100–0.500 seconds')));
assert(container.children.every(card=>card.children.every(line=>line.tagName==='P' && !line.children.length)));
const status=document.getElementById('export-quality-status').textContent;for(const phrase of ['does not rehash or decode','2 earlier receipt','32 deliveries','16 rejected attempts','app session']){assert(status.includes(phrase),phrase);}
const card=context.renderExportQualityReport({file:'tracks.zip',status:'verified',verification_scope:'audacity_structure_and_frames',tracks_checked:20,members_checked:22,track_summaries_abbreviated:true,track_summaries:[{file:'voice.wav',frames:24000,sample_rate:24000,channels:1}]},false);assert(card.children.some(node=>node.textContent.includes('Inactive-speaker silence is intentional')));assert(card.children.some(node=>node.textContent.includes('Track details are abbreviated')));
''')

    def test_missing_or_mismatched_editor_snapshot_shows_a_refresh_instruction(self):
        self.run_js(r'''
let reads=0;context.API.get=async()=>{reads++;return result;};
for(const [token,book] of [[null,null],['b'.repeat(64),'older-book']]){
 context.chunkSnapshotBookToken=token;context.chunkSnapshotBook=book;
 await context.loadExportQualityReports();
 assert(document.getElementById('export-quality-status').textContent.includes('Refresh Editor for the current book'));
 assert.equal(document.getElementById('export-quality-reports').children.length,0);
}
assert.equal(reads,0);
''')

    def test_actual_editor_snapshot_identity_refresh_invalidates_rendered_cards(self):
        self.run_js(r'''
await context.loadExportQualityReports();const container=document.getElementById('export-quality-reports');assert.equal(container.children.length,2);
body.appendChild(new Element('tbody',{id:'chunks-table-body'}));context.loadChunksTimer=null;context.chunkSnapshotRevision=null;context.cachedChunks=[];
context.API.get=async()=>({book_token:'c'.repeat(64),revision:'r',full:true,chunks:[],changed_ids:[],total:0});
const start=core.indexOf('async function refreshChunkSnapshot('),end=core.indexOf('\n        window.toggleChunkExpand',start);vm.runInContext(core.slice(start,end),context);await context.refreshChunkSnapshot(true);
assert.equal(container.children.length,0);assert.equal(context.chunkSnapshotBookToken,'c'.repeat(64));
''')

    def test_stale_async_book_settings_and_terminal_reconnect_cannot_restore_old_cards(self):
        self.run_js(r'''
let resolve;context.API.get=()=>new Promise(r=>resolve=r);const pending=context.loadExportQualityReports();context.resetExportQualityView();resolve(result);await pending;assert.equal(document.getElementById('export-quality-reports').children.length,0);
const switched=context.loadExportQualityReports();context.currentBookFilename='different';resolve(result);await switched;assert.equal(document.getElementById('export-quality-reports').children.length,0);
context.currentBookFilename='fixture';context.API.get=async()=>result;await context.loadExportQualityReports();assert.equal(document.getElementById('export-quality-reports').children.length,2);
context.exportStatusGenerations=new Map();let poll;context._startPolling=(name,fetch,options)=>poll=options;vm.runInContext(core.slice(core.indexOf('function isExportComplete('),core.indexOf('window.exportAudacity =')),context);
for(const task of ['audacity_export','m4b_export','chapter_export']){await context.loadExportQualityReports();context.pollExport(task);poll.onDone({running:false,result:{status:'failed',message:'failure'}});assert.equal(document.getElementById('export-quality-reports').children.length,0);}
context.chunkSnapshotBookToken='c'.repeat(64);await context.loadExportQualityReports();assert.equal(document.getElementById('export-quality-reports').children.length,0);
''')


if __name__ == '__main__':
    unittest.main()
