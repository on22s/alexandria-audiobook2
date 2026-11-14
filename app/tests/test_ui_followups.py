"""#1938 UI behaviour, using production handlers and synthetic data only."""
import subprocess
from pathlib import Path
import unittest

STATIC = Path(__file__).resolve().parents[1] / "static"


class UiFollowupTests(unittest.TestCase):
    def test_ingest_refresh_coalesces_and_preserves_failure_details_without_stale_facts(self):
        self.run_js(r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm'),elements={},pending=[];
const el=id=>elements[id]??={textContent:'',setAttribute(){},removeAttribute(){}};
const c={window:{},currentBookFilename:'A',document:{getElementById:el},describeApiError:e=>e.message,
 API:{get:()=>new Promise((resolve,reject)=>pending.push({resolve,reject}))},t:(k,s,v={})=>s.replace(/\{(\w+)\}/g,(_,n)=>v[n])};
vm.createContext(c);vm.runInContext(fs.readFileSync(process.argv[1]+'/js/app-ui-followups.js','utf8'),c);
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{
const a=c.refreshScriptIngestFacts(),again=c.refreshScriptIngestFacts();assert.equal(pending.length,1);assert.strictEqual(a,again);
c.currentBookFilename='B';c.resetScriptIngestFacts();const b=c.refreshScriptIngestFacts();assert.equal(pending.length,2);
pending[0].resolve({source:{source_name:'OLD'},cast:{status:'available',count:1},script:{status:'verified'}});await a;
assert(!el('script-ingest-source-state').textContent.includes('verified'));assert(!el('script-ingest-metadata').textContent.includes('OLD'));
pending[1].reject(Error('Book state is busy'));await b;assert(el('script-ingest-feedback').textContent.includes('Book state is busy'));
assert.equal(el('script-ingest-metadata').textContent,'');finished=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_ingest_comparison_is_bound_during_source_aba(self):
        from contextlib import ExitStack
        import json
        import tempfile
        from unittest.mock import patch
        from routers import script
        from tests.test_single_narrator_source_preview import SingleNarratorSourcePreviewTests
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            client, source, text = SingleNarratorSourcePreviewTests().fixture(root, stack, 'Original synthetic source A.')
            annotation = root / 'annotated_script.json'
            annotation.write_text(json.dumps([{'speaker':'NARRATOR','text':'Replacement synthetic source B.'}]))
            stack.enter_context(patch.object(script, 'SCRIPT_PATH', str(annotation)))
            compare = script.get_source_integrity
            def swapped(*args, **kwargs):
                source.write_text('Replacement synthetic source B.', encoding='utf-8')
                try:
                    return compare(*args, **kwargs)
                finally:
                    source.write_text(text, encoding='utf-8')
            stack.enter_context(patch.object(script, 'get_source_integrity', swapped))
            result = client.get('/api/script/ingest-status')
            self.assertEqual(200, result.status_code, result.text)
            self.assertNotEqual('verified', result.json()['script']['status'])
            self.assertEqual(text, source.read_text())

    def test_waveform_peaks_cover_the_last_sample_for_short_and_uneven_buffers(self):
        self.run_js(r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm'),root=process.argv[1];
const source=fs.readFileSync(root+'/js/app-editor-review.js','utf8');let length=321;
const c={window:{AudioContext:class{async decodeAudioData(){const data=new Float32Array(length);data[length-1]=1;return{getChannelData:()=>data};}}},
 fetch:async()=>({ok:true,arrayBuffer:async()=>new ArrayBuffer(0)})};
vm.createContext(c);vm.runInContext('let editorWaveformContext=null;'+source.slice(source.indexOf('async function decodeEditorWaveformPeaks('),source.indexOf('function drawEditorWaveform(')),c);
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{
for(length of [1,2,159,160,161,319,320,321,1001]){
 const peaks=await c.decodeEditorWaveformPeaks('synthetic');assert(peaks.some(pair=>pair[1]===1),'last sample of '+length+' is represented');
 assert(peaks.every(([min,max])=>min<=max&&min>=-1&&max<=1));}
finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_real_connection_route_round_trip_with_local_protocol_server(self):
        from contextlib import ExitStack
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import json
        import threading
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import system
        requests = []
        post_bodies = []
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def respond(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()
            def do_GET(self):
                requests.append(('GET', self.path))
                self.respond({'object':'list', 'data':[{'id':'synthetic-model', 'object':'model', 'created':0, 'owned_by':'test'}]})
            def do_POST(self):
                requests.append(('POST', self.path))
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                post_bodies.append(body)
                if body['messages'][0]['content'] != 'Reply with the single word: pong':
                    self.send_error(400)
                    return
                self.respond({'id':'synthetic', 'object':'chat.completion', 'created':0, 'model':'synthetic-model',
                              'choices':[{'index':0, 'finish_reason':'stop', 'message':{'role':'assistant', 'content':'pong'}}]})
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            app = FastAPI()
            app.include_router(system.router)
            with ExitStack() as stack:
                claim = stack.enter_context(patch.object(system, 'claim_gpu_task', return_value='synthetic-claim'))
                release = stack.enter_context(patch.object(system, 'release_gpu_task_claim'))
                client = stack.enter_context(TestClient(app))
                result = client.post('/api/llm/test', json={'base_url':f'http://127.0.0.1:{server.server_port}/v1',
                    'api_key':'local', 'model_name':'synthetic-model', 'transport':'http'})
                self.assertEqual(200, result.status_code, result.text)
                self.assertTrue(result.json()['ok'], result.text)
                self.assertTrue(result.json()['model_present'])
                self.assertEqual('pong', result.json()['reply'])
                self.assertFalse(result.json()['is_remote'])
                claim.assert_called_once_with('llm_test')
                release.assert_called_once_with('llm_test', 'synthetic-claim')
            # The shared client may probe provider/context metadata before the
            # explicit list call; only one tiny completion is permitted here.
            self.assertGreaterEqual(len(requests), 2)
            self.assertIn(('GET', '/v1/models'), requests)
            self.assertEqual(('POST', '/v1/chat/completions'), requests[-1])
            self.assertEqual(1, sum(method == 'POST' for method, _ in requests), (requests, post_bodies))
            self.assertTrue(all(method == 'GET' for method, _ in requests[:-1]), requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def run_js(self, script):
        result = subprocess.run(["node", "-e", script, str(STATIC)],
                                capture_output=True, text=True, encoding="utf-8", timeout=25)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_waveform_seek_is_bounded_and_does_not_cross_take_identity(self):
        self.run_js(r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm'),root=process.argv[1];
const source=fs.readFileSync(root+'/js/app-editor-review.js','utf8');
const attrs={}, canvas={getBoundingClientRect:()=>({left:13,width:321}),setAttribute:(k,v)=>attrs[k]=v};
const audio={duration:10,currentTime:0,getAttribute:()=>'/take.wav?r=one'};
let uid='one',token='book',peaks=[[0,1]],draws=0;
const c={document:{getElementById:()=>canvas},window:{},getEditorReviewTarget:()=>({chunk:{uid,audio_revision:'r1'},row:{querySelector:()=>audio}}),
 chunkSnapshotBookToken:token,editorWaveformKey:JSON.stringify([token,uid,'r1','/take.wav?r=one']),editorWaveformPeaks:new Map(),redrawEditorWaveform:()=>draws++};
vm.createContext(c);
vm.runInContext(source.slice(source.indexOf('function getEditorWaveformKey('),source.indexOf('function redrawEditorWaveform(')),c);
vm.runInContext(source.slice(source.indexOf('function getEditorWaveformPosition('),source.indexOf('function observeEditorWaveform(')),c);
for(let i=-100;i<=500;i++){c.seekEditorWaveform({clientX:i});assert(audio.currentTime>=0&&audio.currentTime<=10);}
c.seekEditorWaveform({clientX:13});assert.equal(audio.currentTime,0);
c.seekEditorWaveform({clientX:334});assert.equal(audio.currentTime,10);
c.seekEditorWaveform({clientX:173.5});assert.equal(audio.currentTime,5);
uid='other';c.seekEditorWaveform({clientX:334});assert.equal(audio.currentTime,5);
uid='one';audio.duration=Infinity;c.seekEditorWaveform({clientX:334});assert.equal(audio.currentTime,5);
audio.duration=10;c.seekEditorWaveform({clientX:NaN});assert.equal(audio.currentTime,5);
""")

    def test_pending_count_matches_render_admission_and_row_states(self):
        self.run_js(r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm'),root=process.argv[1];
const label={textContent:'',setAttribute(){},removeAttribute(){}},c={window:{},document:{getElementById:()=>label},t:(k,s,v={})=>s.replace(/\{(\w+)\}/g,(_,n)=>v[n])};
vm.createContext(c);const core=fs.readFileSync(root+'/js/app-core.js','utf8');c.escapeHtml=String;
vm.runInContext(core.slice(core.indexOf('function _driftBadge('),core.indexOf('function applyDriftFilter(')),c);
vm.runInContext(core.slice(core.indexOf('function isChunkPendingRender('),core.indexOf('window.renderAll =')),c);
vm.runInContext(fs.readFileSync(root+'/js/app-ui-followups.js','utf8'),c);
const rows=[{status:'done',text:'Done'},{status:'pending',text:'Next'},{status:'error',text:'Retry'},
 {status:'pending',text:'  '},{status:'skipped',text:'Not spoken'},{status:'generating',text:'Working'}];
c.renderPendingCount(rows);assert.equal(label.textContent,'Render Pending (4)');
c.renderPendingCount([]);assert.equal(label.textContent,'Render Pending (0)');
assert.equal(c.getChunkRowState({status:'done',audio_path:'synthetic.wav',drift:{flagged:true}}),'flagged');
assert.equal(c.getChunkRowState({status:'done',drift:{flagged:true}}),'rendered');
assert.equal(c.getChunkRowState({status:'error'}),'failed');
assert.equal(c.getChunkRowState({status:'done'}),'rendered');
assert.equal(c.getChunkRowState({status:'generating'}),'generating');
assert.equal(c.getChunkRowState({status:'pending'}),'pending');
""")

    def test_connection_details_reject_stale_success_and_include_failure_recovery(self):
        self.run_js(r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm'),root=process.argv[1],elements={};
const el=id=>elements[id]??={value:'',style:{},textContent:'',innerHTML:'',setAttribute(){},removeAttribute(){},scrollIntoView(){}};
el('llm-url').value='http://localhost:1234/v1';let profile={base_url:el('llm-url').value},resolve;
const c={window:{},document:{getElementById:el},currentLlmMode:'local',getEditedLlmProfile:()=>({...profile}),
 API:{post:()=>new Promise(r=>resolve=r)},performance:{now:()=>100},escapeHtml:String,
 t:(k,s,v={})=>s.replace(/\{(\w+)\}/g,(_,n)=>v[n])};
vm.createContext(c);vm.runInContext(fs.readFileSync(root+'/js/app-ui-followups.js','utf8'),c);
const core=fs.readFileSync(root+'/js/app-core.js','utf8');
vm.runInContext(core.slice(core.indexOf('async function testLlmConnection('),core.indexOf('const generationControlFields')),c);
let finished=false;process.on('beforeExit',()=>assert(finished));
(async()=>{const pending=c.testLlmConnection();assert(el('llm-test-result').innerHTML.includes('spinner-border'));
 resolve({ok:false,step:'models',error:'offline'});await pending;
 assert(el('llm-test-result').innerHTML.includes('troubleshootLlmConnection'));
 assert(el('llm-test-result').innerHTML.includes('ms'));assert.equal(el('llm-test-btn').disabled,false);
 const next=c.testLlmConnection();profile.base_url='changed';resolve({ok:true,reply:'stale',model:'stale'});await next;
 assert(!el('llm-test-result').innerHTML.includes('stale'));finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_live_ingest_snapshot_does_not_treat_filenames_as_completion(self):
        self.run_js(r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm'),root=process.argv[1],elements={};
const el=id=>elements[id]??={textContent:'',dataset:{},setAttribute(){},removeAttribute(){}};
const c={window:{},document:{getElementById:el},currentBookFilename:'chosen.txt',t:(k,s,v={})=>s.replace(/\{(\w+)\}/g,(_,n)=>v[n])};
vm.createContext(c);vm.runInContext(fs.readFileSync(root+'/js/app-ui-followups.js','utf8'),c);
c.renderScriptIngestFacts({source:{source_name:'chosen.txt',total_chars:20,word_count:4,chapter_count:1,estimated_minutes:1},
 script:{status:'differences'},cast:{status:'not_built'}});
assert.equal(el('script-ingest-source-state').textContent,'Selected source verified');
assert.equal(el('script-ingest-script-state').textContent,'Source differences: review required');
assert.equal(el('script-ingest-cast-state').textContent,'Not built');
c.resetScriptIngestFacts();assert(!el('script-ingest-source-state').textContent.includes('verified'));
""")

    def test_ingest_route_reports_actual_source_metrics_and_missing_script(self):
        from contextlib import ExitStack
        import tempfile
        import json
        from unittest.mock import patch
        from routers import script
        from tests.test_single_narrator_source_preview import SingleNarratorSourcePreviewTests
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            client, source, _ = SingleNarratorSourcePreviewTests().fixture(
                root, stack, 'Chapter 1\n\nSynthetic source text.\n')
            stack.enter_context(patch.object(script, 'SCRIPT_PATH', str(root / 'annotated_script.json')))
            result = client.get('/api/script/ingest-status')
            self.assertEqual(200, result.status_code, result.text)
            body = result.json()
            self.assertEqual(5, body['source']['word_count'])
            self.assertEqual(1, body['source']['chapter_count'])
            self.assertNotIn('text_start', body['source'])
            self.assertEqual('not_built', body['cast']['status'])
            self.assertEqual('not_built', body['script']['status'])
            self.assertEqual('Chapter 1\n\nSynthetic source text.\n', source.read_text())
            annotation = root / 'annotated_script.json'
            annotation.write_text(json.dumps([{'speaker':'NARRATOR', 'text': source.read_text()}]), encoding='utf-8')
            matched = client.get('/api/script/ingest-status')
            self.assertEqual(200, matched.status_code, matched.text)
            self.assertEqual('verified', matched.json()['script']['status'])
            annotation.write_text(json.dumps([{'speaker':'NARRATOR', 'text':'Different words.'}]), encoding='utf-8')
            self.assertEqual('differences', client.get('/api/script/ingest-status').json()['script']['status'])
            self.assertEqual(409, client.get('/api/script/ingest-status?expected_book_token=' + '0' * 64).status_code)
            state = json.loads((root / 'state.json').read_text())
            state['script_selection_pending'] = True
            (root / 'state.json').write_text(json.dumps(state))
            awaiting = client.get('/api/script/ingest-status')
            self.assertEqual(200, awaiting.status_code, awaiting.text)
            self.assertEqual('not_built', awaiting.json()['script']['status'])
            self.assertEqual(5, awaiting.json()['source']['word_count'])
            self.assertEqual(409, client.get('/api/annotated_script/diff').status_code,
                             'Original published-script guard must remain effective')

    def test_ingest_route_refuses_source_replacement_and_keeps_files(self):
        from contextlib import ExitStack
        import tempfile
        from unittest.mock import patch
        from routers import script
        from tests.test_single_narrator_source_preview import SingleNarratorSourcePreviewTests
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            client, source, _ = SingleNarratorSourcePreviewTests().fixture(root, stack)
            stack.enter_context(patch.object(script, 'SCRIPT_PATH', str(root / 'annotated_script.json')))
            preview = script._get_single_narrator_source_preview
            calls = []
            def replaced(**kwargs):
                calls.append(kwargs)
                if len(calls) == 2:
                    source.write_text('Replacement synthetic source.', encoding='utf-8')
                return preview(**kwargs)
            stack.enter_context(patch.object(script, '_get_single_narrator_source_preview', replaced))
            result = client.get('/api/script/ingest-status')
            self.assertEqual(409, result.status_code, result.text)
            self.assertEqual('Replacement synthetic source.', source.read_text())

    def test_ingest_roster_is_validated_and_bound_to_source_bytes(self):
        from contextlib import ExitStack
        import json
        import tempfile
        from unittest.mock import patch
        from routers import script
        from cast_list import get_cast_list_path
        from tests.test_single_narrator_source_preview import SingleNarratorSourcePreviewTests
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            client, source, _ = SingleNarratorSourcePreviewTests().fixture(root, stack)
            stack.enter_context(patch.object(script, 'SCRIPT_PATH', str(root / 'annotated_script.json')))
            cast = Path(get_cast_list_path(str(source), str(root)))
            cast.parent.mkdir()
            cast.write_text(json.dumps({'cast':[{'name':'ALICE','aliases':['A']}]}))
            result = client.get('/api/script/ingest-status')
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual({'status':'available', 'count':1}, result.json()['cast'])
            cast.write_text('invalid json')
            self.assertEqual('unavailable', client.get('/api/script/ingest-status').json()['cast']['status'])
            source.write_text('A different synthetic book.', encoding='utf-8')
            self.assertEqual('not_built', client.get('/api/script/ingest-status').json()['cast']['status'])


if __name__ == "__main__":
    unittest.main()
