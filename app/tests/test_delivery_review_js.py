import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import script


class DeliveryReviewJsTests(unittest.TestCase):
    def run_js(self, code):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{"speaker": "NARRATOR", "text": "The <room> was cold.",
                     "instruct": "Neutral.", "instruct_unchecked": True}]
            (Path(tmp) / "annotated_script.json").write_text(json.dumps(rows))
            app = FastAPI()
            app.include_router(script.router)
            with patch.object(script, "DATA_DIR", tmp), TestClient(app) as http:
                response = http.get('/api/annotated_script/delivery_review')
                self.assertEqual(200, response.status_code)
                native = response.json()
        source = Path(__file__).parent.parent / 'static/js/app-core.js'
        harness = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8'),native=JSON.parse(process.argv[2]);
let finished=false;process.on('beforeExit',()=>assert(finished,'All delivery UI assertions must finish'));
const panel={style:{},innerHTML:'',textContent:''},posts=[],toasts=[];let poll,loads=0;
const ctx={window:null,document:{getElementById:()=>panel},API:{get:async url=>url.includes('/status/')?{running:false}:native,post:async(url,data)=>{posts.push({url,data});return{claim_id:'fixture-owner'};}},
escapeHtml:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),ensureEditorRenderSnapshot:async()=>[],confirmIfRemote:async()=>true,showToast:message=>toasts.push(message),loadChunks:async()=>loads++,_startPolling:(key,fetch,options)=>{assert.strictEqual(key,'delivery_retry');poll={fetch,options};}};
ctx.window=ctx;vm.createContext(ctx);const run=code=>vm.runInContext(code,ctx);
run(source.slice(source.indexOf('let deliveryReviewView ='),source.indexOf('async function loadChunks(')));
'''
        result = subprocess.run(['node', '-e', harness + code, str(source), json.dumps(native)],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_native_list_retry_snapshot_and_owned_cancel_then_refresh(self):
        self.run_js(r'''
(async()=>{
await ctx.refreshDeliveryReview();assert.match(panel.innerHTML,/Needs delivery review: 1/);assert.match(panel.innerHTML,/Entry 1/);assert.match(panel.innerHTML,/&lt;room&gt;/);assert(!panel.innerHTML.includes('The <room>'));
await ctx.retryDeliveryInstructions();assert.strictEqual(posts.length,1);assert.strictEqual(posts[0].data.snapshot,native.snapshot);assert.match(panel.innerHTML,/Cancel delivery retry/);
await ctx.cancelDeliveryInstructions();assert.strictEqual(posts[1].data.claim_id,'fixture-owner');assert.match(toasts.at(-1),/Cancellation queued/);
assert.strictEqual((await poll.fetch()).running,false);await poll.options.onDone();assert.strictEqual(loads,1);assert.match(panel.innerHTML,/Needs delivery review: 1/);assert.strictEqual(run('deliveryRetryClaim'),null);
finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_decline_failed_flush_and_stale_response_do_not_start_or_paint_old_book(self):
        self.run_js(r'''
(async()=>{
ctx.confirmIfRemote=async()=>false;await ctx.retryDeliveryInstructions();assert.strictEqual(posts.length,0);
ctx.ensureEditorRenderSnapshot=async()=>{throw Error('save failed');};ctx.confirmIfRemote=async()=>true;await ctx.retryDeliveryInstructions();assert.strictEqual(posts.length,0);assert.match(toasts.at(-1),/save failed/);
let release;ctx.API.get=()=>new Promise(resolve=>release=resolve);const old=ctx.refreshDeliveryReview();ctx.API.get=async()=>({...native,count:0,entries:[],rows:[]});await ctx.refreshDeliveryReview();assert.strictEqual(panel.style.display,'none');release(native);assert.strictEqual(await old,null);assert.strictEqual(panel.style.display,'none');
ctx.API.get=async()=>{throw Error('unavailable');};await ctx.refreshDeliveryReview();assert.match(panel.textContent,/unavailable/);
finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')
