"""Execute native editor integrity/merge JS with actual native HTTP response fixtures."""
import json
from pathlib import Path
import subprocess
import unittest
from tests import test_merge_integrity

SOURCE=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
SETUP=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),reports=JSON.parse(process.argv[2]);
const fields={},handlers={},posts=[],events=[];let kind='verified',decision=true;
const el=id=>fields[id]||(fields[id]={style:{},textContent:'',className:'',innerHTML:'',addEventListener:(event,fn)=>{handlers[id]=fn;}});
const ctx={document:{getElementById:el,querySelector:()=>({click:()=>events.push('tab')})},API:{get:async url=>{assert.strictEqual(url,'/api/editor/integrity');events.push('get');return reports[kind];},post:async(url,body)=>posts.push({url,body,kind})},
 pendingChunkEdits:new Map(),failedChunkEdits:new Map(),chunkEditsRevision:0,
 ensureEditorRenderSnapshot:async()=>events.push('flush'),showConfirm:async text=>{events.push(text);return decision;},showToast:text=>events.push(text),
 pollLogs:()=>events.push('poll'),escapeHtml:value=>String(value).replaceAll('<','&lt;').replaceAll('>','&gt;')};
vm.createContext(ctx);const run=code=>vm.runInContext(code,ctx);
run(source.slice(source.indexOf('let editorIntegrityView ='),source.indexOf('async function openTextDiff()')));
run(source.slice(source.indexOf('function renderTextDiff('),source.indexOf('function scrollToChunkRow(')));
const start=source.indexOf("document.getElementById('btn-merge').addEventListener");
run(source.slice(start,source.indexOf("document.getElementById('btn-cancel-merge').addEventListener",start)));
'''

class MergeIntegrityJsTests(unittest.TestCase):
    def run_js(self,body):
        native=test_merge_integrity.MergeIntegrityAdmission('test_clean_chunks_schedule_as_before_without_body')
        native.setUp();self.addCleanup(native.doCleanups)
        reports={'verified':native.client.get('/api/editor/integrity').json()}
        native.change_rows('alpha gamma')
        reports['differences']=native.client.get('/api/editor/integrity').json()
        native.state['input_file_path']=str(native.script);native.write_state()
        reports['unavailable']=native.client.get('/api/editor/integrity').json()
        for status,report in reports.items():self.assertEqual(status,report['status'])
        command=SETUP+'\n(async()=>{\n'+body+'\n})().catch(e=>{console.error(e);process.exitCode=1;});'
        result=subprocess.run(['node','-e',command,str(SOURCE),json.dumps(reports)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_native_route_differences_are_shown_before_exact_confirmation_and_decline_stops(self):
        self.run_js(r'''
kind='differences';decision=false;await handlers['btn-merge']();assert.strictEqual(posts.length,0);
assert.deepStrictEqual(events.slice(0,2),['flush','get']);assert.match(el('text-diff-panel').innerHTML,/beta/);assert.match(el('text-diff-panel').innerHTML,/gamma/);
assert.match(events.at(-1),/1 word differences.*Cancel to fix/);
decision=true;await handlers['btn-merge']();assert.strictEqual(posts.length,1);assert.strictEqual(posts[0].body.integrity_confirmation,reports.differences.snapshot);assert.strictEqual(posts[0].url,'/api/merge');assert.ok(events.includes('poll'));
kind='unavailable';await handlers['btn-merge']();assert.strictEqual(posts.length,2);assert.strictEqual(posts[1].body.integrity_confirmation,reports.unavailable.snapshot);assert.match(el('text-diff-panel').textContent,/script JSON/);assert.ok(events.some(event=>event.includes('Continue without a verified source match')));
kind='verified';await handlers['btn-merge']();assert.strictEqual(posts.length,3);assert.strictEqual(posts[2].body.integrity_confirmation,reports.verified.snapshot);
ctx.ensureEditorRenderSnapshot=async()=>{throw Error('save failed');};await handlers['btn-merge']();assert.strictEqual(posts.length,3);assert.match(events.at(-1),/Merge failed: save failed/);
''')

    def test_badge_uses_native_status_and_discards_inflight_response_after_edit(self):
        self.run_js(r'''
await ctx.refreshEditorIntegrity();assert.strictEqual(el('editor-integrity-badge').textContent,'Saved text matches source');
kind='differences';await ctx.refreshEditorIntegrity();assert.strictEqual(el('editor-integrity-badge').textContent,'1 word differences');
kind='unavailable';await ctx.refreshEditorIntegrity();assert.strictEqual(el('editor-integrity-badge').textContent,'Source comparison unavailable');
let resolve;ctx.API.get=()=>new Promise(done=>{resolve=done;});const pending=ctx.refreshEditorIntegrity();ctx.invalidateEditorIntegrity('Edits awaiting save');resolve(reports.verified);await pending;
assert.strictEqual(el('editor-integrity-badge').textContent,'Edits awaiting save');assert.strictEqual(el('editor-integrity-badge').className,'badge bg-secondary');
let reads=0;ctx.API.get=async()=>{reads++;return reports.verified;};ctx.pendingChunkEdits.set(0,Promise.resolve());await ctx.refreshEditorIntegrity();assert.strictEqual(reads,0);assert.match(el('editor-integrity-badge').textContent,/Save edits/);
ctx.pendingChunkEdits.clear();ctx.failedChunkEdits.set(0,Error('save failed'));await ctx.refreshEditorIntegrity();assert.strictEqual(reads,0);
''')
