"""Actual ranked candidate writes stay bounded and indexed without dropping rows."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import voices

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const elements={},toasts=[],writes=[];let refreshed=0;
const el=id=>elements[id]||(elements[id]={disabled:false,value:'',style:{},innerHTML:''});
const ctx={window:null,currentIsRemote:false,failoverIsRemote:false,document:{getElementById:el,querySelectorAll:()=>[]},console:{debug:()=>{}},API:{},showToast:(...args)=>toasts.push(args),voicesScopeIsNew:()=>true,keepCurrentVoicesIfAsked:async()=>true,refreshVoiceMetadata:async()=>{refreshed++;},renderVoiceSuggestions:()=>{}};ctx.window=ctx;vm.createContext(ctx);
const run=code=>vm.runInContext(code,ctx);
function load(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a);run(source.slice(a,b));}
load('function escapeHtml(', '// Parse a numeric input');load('async function confirmIfRemote(', '// navigator.clipboard');load('const taskStartButtons =','// --- API Helpers ---');
if(source.includes('function getLoraModelsById(')){load('function getLoraModelsById(', 'async function suggestVoices(');}
load('async function suggestVoices(', 'window.suggestMoreVoices =');
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const turn=()=>new Promise(setImmediate);
'''


class SuggestionCandidateQueueTests(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(['node', '-e', SETUP + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});', str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_large_ranked_pool_bounds_active_writes_and_reports_failure_without_dropping_rest(self):
        self.run_js(r'''
const suggestions=Object.fromEntries(Array.from({length:150},(_,i)=>['Speaker '+i,{ranked_adapter_ids:Array.from({length:8},(_,j)=>'model-'+j)}]));const expected=1200;
let active=0,maxActive=0,settled=0,idReads=0;const waiting=[];
const models=Array.from({length:1000},(_,i)=>({get id(){idReads++;return 'model-'+i;},path:'/adapter/'+i,gender:'female'}));
ctx.API.get=async()=>models;ctx.API.post=async(url,payload)=>{if(url==='/api/suggest_voices'){return {suggestions};}const gate=deferred();waiting.push(gate);writes.push({url,payload});maxActive=Math.max(maxActive,++active);try{return await gate.promise;}finally{active--;settled++;}};
ctx.refreshVoiceMetadata=async()=>{assert.strictEqual(settled,expected);refreshed++;};const pending=ctx.suggestVoices();await turn();assert.strictEqual(active,4);assert.strictEqual(writes.length,4);assert.strictEqual(el('btn-suggest-voices').disabled,true);
let rejected=false;while(settled<expected){const batch=waiting.splice(0);assert(batch.length>0);for(const gate of batch){if(!rejected){rejected=true;gate.reject(Error('fixture persistence failure'));}else{gate.resolve({});}}await turn();}await pending;
assert.strictEqual(maxActive,4);assert.strictEqual(writes.length,expected);assert.strictEqual(refreshed,1);assert(idReads<=2000,'adapter library should be indexed once');assert.strictEqual(el('btn-suggest-voices').disabled,false);assert.match(el('suggest-status').innerHTML,/1 candidate\(s\) could not be saved/);assert.strictEqual(toasts.at(-1)[1],'warning');
for(let i=0;i<150;i++){const rows=writes.filter(row=>row.url===`/api/voices/Speaker%20${i}/candidates`);assert.strictEqual(rows.length,8);for(let j=0;j<8;j++){assert.strictEqual(rows[j].payload.candidate_id,'model-'+j);assert.strictEqual(rows[j].payload.config.rank,j+1);assert.strictEqual(rows[j].payload.config.adapter_path,'/adapter/'+j);}}
''')

    def test_collect_indexes_once_preserves_first_duplicate_and_refreshes_changed_library(self):
        self.run_js(r'''
load('function collectVoiceConfig()', 'function onVoiceReadyChange(');let reads=0;
ctx._loraModelsCache=Array.from({length:1000},(_,i)=>({get id(){reads++;return 'model-'+i;},adapter_path:'/first/'+i}));ctx._loraModelsCache.push({id:'model-999',path:'/duplicate'});
const cards=Array.from({length:100},(_,i)=>({dataset:{voice:'Speaker '+i},querySelector:selector=>({'.voice-type:checked':{value:i%2?'lora':'builtin_lora'},'.lora-adapter-select':{value:'model-999'},'.builtin-lora-select':{value:'model-999'},'.builtin-lora-style':{value:'style'},'.lora-character-style':{value:'style'}}[selector]||null)}));ctx.document.querySelectorAll=()=>cards;ctx._voicesByName={};
const first=ctx.collectVoiceConfig();assert(reads<=2000,'one index per collection');assert.strictEqual(Object.keys(first).length,100);for(const entry of Object.values(first)){assert.strictEqual(entry.adapter_path,'/first/999');}
ctx._loraModelsCache=[{id:'model-999',adapter_path:'/updated'}];const second=ctx.collectVoiceConfig();assert.strictEqual(second['Speaker 0'].adapter_path,'/updated');assert.strictEqual(first['Speaker 0'].adapter_path,'/first/999');
''')

    def test_real_ranked_packets_persist_every_candidate_through_native_routes(self):
        output = self.run_js(r'''
const suggestions=Object.fromEntries(Array.from({length:6},(_,i)=>['Speaker '+i,{ranked_adapter_ids:Array.from({length:5},(_,j)=>'model-'+j)}]));ctx.API.get=async()=>[{id:'model-0',builtin:true,adapter_path:'/builtin'}];
ctx.API.post=async(url,payload)=>{if(url==='/api/suggest_voices'){return {suggestions};}writes.push({url,payload});return {};};await ctx.suggestVoices();assert.strictEqual(writes.length,30);console.log(JSON.stringify(writes));
''')
        packets = json.loads(output)
        app = FastAPI()
        app.include_router(voices.router)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'voice_config.json')
            script = Path(root, 'annotated_script.json')
            script.write_text(json.dumps([{'speaker': 'Speaker ' + str(i), 'text': 'line'} for i in range(6)]))
            path.write_text('{}')
            with patch.object(voices, 'VOICE_CONFIG_PATH', str(path)), patch.object(voices, 'SCRIPT_PATH', str(script)), TestClient(app) as client:
                def save(row):
                    response = client.post(row['url'], json=row['payload'])
                    self.assertEqual(200, response.status_code, response.text)

                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(save, packets))
            saved = json.loads(path.read_text())
            self.assertEqual(6, len(saved))
            for entry in saved.values():
                rows = sorted(entry['candidates'], key=lambda row: row['rank'])
                self.assertEqual(['model-' + str(i) for i in range(5)], [row['candidate_id'] for row in rows])
                self.assertEqual([1, 2, 3, 4, 5], [row['rank'] for row in rows])
                self.assertEqual('builtin_lora', rows[0]['type'])
                self.assertEqual('/builtin', rows[0]['adapter_path'])
