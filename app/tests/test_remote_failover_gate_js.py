"""Execute actual LLM starters and shared cost gate before any inference POST."""
from pathlib import Path
import os
import subprocess
import unittest

SOURCE = Path(os.environ.get('FAILOVER_GATE_SOURCE', Path(__file__).resolve().parent.parent / 'static/js/app-core.js'))
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const turn=()=>new Promise(setImmediate);
function client(activeRemote=false,failoverRemote=true){
 const elements={},handlers={},posts=[],gets=[],prompts=[],toasts=[],polls=[];let decision=false,keepCalls=0;
 const el=id=>elements[id]||(elements[id]={value:id==='review-context-window'?'5':'',files:[],style:{},checked:false,disabled:false,innerHTML:'',classList:{add(){},remove(){}},addEventListener:(_event,callback)=>handlers[id]=callback});
 const ctx={window:null,document:{getElementById:el,querySelectorAll:()=>[]},currentIsRemote:activeRemote,failoverIsRemote:failoverRemote,currentBookFilename:'book.txt',showPresetEditor:async()=>({name:'teen'}),console,
 showConfirm:async message=>{prompts.push(message);return typeof decision==='function'?await decision():decision;},showToast:(...args)=>toasts.push(args),prompt:()=> 'teen',
 API:{post:async(url,payload)=>{posts.push({url,payload});return {suggestions:{}};},get:async url=>{gets.push(url);return[];}},
 _resetPauseBtn(){},_isStripFrontMatterChecked:()=>true,_isReviewDedupeChecked:()=>true,_disableReviewButtons(){},_showReviewControls(){},_onReviewDone(){},
 pollScriptLogs:task=>polls.push(task),pollPersonaStatus:()=>polls.push('persona'),getPersonaContextLines:()=>8,voicesScopeIsNew:()=>false,keepCurrentVoicesIfAsked:async()=>{keepCalls++;return true;},refreshVoiceMetadata:async()=>[],renderVoiceSuggestions(){}};ctx.window=ctx;
 el('upload-status').innerHTML='<span class="text-success">Loaded</span>';vm.createContext(ctx);
 function load(a,b){const start=source.indexOf(a),end=source.indexOf(b,start);assert(start>=0&&end>start);vm.runInContext(source.slice(start,end),ctx);}
 load('async function confirmIfRemote(', '// navigator.clipboard');load('const taskStartButtons =','// --- API Helpers ---');
 vm.runInContext('let _scriptStartOver=false;let scriptBatchPoller=null;',ctx);
 load('function getLoadedScriptSourceFilename()', 'window.startBookPreflight =');
 load("document.getElementById('btn-gen-script').addEventListener", '// Pause is SIGSTOP');
 load('function _isReviewForceChecked()', 'function _isStripFrontMatterChecked()');
 load("document.getElementById('btn-review-script').addEventListener", 'const _reviewPauseResume =');
 load('async function findNicknames()', 'let characterAliasesLoaded =');load('async function generatePersonas()', 'async function cancelPersonas()');
 load('window.regeneratePersona =', 'window.selectVoiceCandidate =');load('function getLoraModelsById(', 'function renderVoiceSuggestions()');
 const button={disabled:false,closest:()=>({dataset:{voice:'Alice'}})};
 const actions=[['contextual',()=>handlers['btn-review-script-contextual'](),'/api/review_script_contextual'],['nicknames',()=>ctx.findNicknames(),'/api/find_nicknames'],['persona',()=>ctx.generatePersonas(),'/api/generate_personas'],['regenerate',()=>ctx.regeneratePersona(button),'/api/generate_personas'],['age',()=>ctx.generateAgeVersion(button),'/api/generate_personas'],['suggest',()=>ctx.suggestVoices(),'/api/suggest_voices'],['suggestMore',()=>ctx.suggestMoreVoices(button),'/api/suggest_voices'],['review',()=>handlers['btn-review-script'](),'/api/review_script'],['script',()=>handlers['btn-gen-script'](),'/api/generate_script']];
 return {ctx,el,posts,gets,prompts,toasts,polls,actions,setDecision:value=>decision=value,getKeepCalls:()=>keepCalls};
}
'''


class RemoteFailoverGateJsTests(unittest.TestCase):
    def run_js(self, code):
        script = SETUP + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_cost_warning_does_not_assume_provider_or_billing_model(self):
        self.run_js(r'''
for(const [remote,failover] of [[true,false],[false,true]]){
 const c=client(remote,failover);assert.strictEqual(await c.ctx.confirmIfRemote('the selected task'),false);
 assert.strictEqual(c.prompts.length,1);assert.doesNotMatch(c.prompts[0],/Thunder|by the hour/);assert.match(c.prompts[0],/may incur.*charges/);assert.match(c.prompts[0],/Cancel/);
 c.setDecision(true);assert.strictEqual(await c.ctx.confirmIfRemote('the selected task'),true);assert.strictEqual(c.prompts.length,2);
}
const local=client(false,false);assert.strictEqual(await local.ctx.confirmIfRemote('local task'),true);assert.strictEqual(local.prompts.length,0);
''')

    def test_every_real_starter_decline_prevents_requests_and_releases_claims(self):
        self.run_js(r'''
for(let index=0;index<9;index++){
 const c=client(),[name,start]=c.actions[index];await start();assert.strictEqual(c.prompts.length,1,name);assert.match(c.prompts[0],/failover is on/);assert.strictEqual(c.posts.length,0,name);assert.strictEqual(c.gets.length,0,name);assert.strictEqual(c.polls.length,0,name);assert.strictEqual(c.getKeepCalls(),0,name);assert.strictEqual(c.toasts.length,0,name);
 assert.strictEqual(c.el('btn-gen-personas').disabled,false);assert.strictEqual(c.el('btn-suggest-voices').disabled,false);
 c.setDecision(true);await start();assert.strictEqual(c.posts.length,1,name);assert.strictEqual(c.posts[0].url,c.actions[index][2],name);
 if(name==='contextual'){assert.strictEqual(c.posts[0].payload.window_size,5);}if(name==='age'){assert.strictEqual(c.posts[0].payload.age_group,'teen');}if(name==='suggestMore'){assert.deepStrictEqual(Array.from(c.posts[0].payload.characters),['Alice']);}
}
''')

    def test_held_confirmation_blocks_owned_persona_and_voice_starts_before_side_effects(self):
        self.run_js(r'''
for(const index of [2,5]){
 const c=client();let resolve;c.setDecision(()=>new Promise(r=>resolve=r));const [name,start]=c.actions[index];const waiting=start();await turn();assert.strictEqual(c.prompts.length,1);assert.strictEqual(c.posts.length,0);assert.strictEqual(c.gets.length,0);assert.strictEqual(c.getKeepCalls(),0);
 assert.strictEqual(c.el(index===2?'btn-gen-personas':'btn-suggest-voices').disabled,true);await start();assert.strictEqual(c.prompts.length,1,'duplicate cannot add warning or start');resolve(false);await waiting;assert.strictEqual(c.el(index===2?'btn-gen-personas':'btn-suggest-voices').disabled,false);
}
''')

    def test_single_jobs_preserve_explicit_remote_choice_while_batch_remote_gate_remains(self):
        self.run_js(r'''
for(const remote of [false,true]){for(let index=0;index<9;index++){
 const c=client(remote,false),[name,start,endpoint]=c.actions[index];await start();assert.strictEqual(c.prompts.length,0,name);assert.strictEqual(c.posts.length,1,name);assert.strictEqual(c.posts[0].url,endpoint,name);
}}
const batch=client(true,false);assert.strictEqual(await batch.ctx.confirmIfRemote('this batch review'),false);assert.strictEqual(batch.prompts.length,1);assert.match(batch.prompts[0],/REMOTE LLM/);assert.match(batch.prompts[0],/charges/);
const hidden=client(false,true);assert.strictEqual(await hidden.ctx.confirmIfRemote('this batch script generation'),false);assert.match(hidden.prompts[0],/failover is on/);
const local=client(false,false);assert.strictEqual(await local.ctx.confirmIfRemote('local batch'),true);assert.strictEqual(local.prompts.length,0);
''')

    def test_loaded_book_can_retry_generation_after_failure_markup_changes(self):
        self.run_js(r"""
const s=client(false,false);s.ctx.escapeHtml=String;
const a=source.indexOf('function showActionError(');vm.runInContext(source.slice(a,source.indexOf('function showConfirm(',a)),s.ctx);
let calls=0;s.ctx.API.post=async(path)=>{assert.strictEqual(path,'/api/generate_script');calls++;if(calls===1){throw Error('temporarily busy');}return{};};
const script=s.actions.find(row=>row[0]==='script')[1];await script();assert.strictEqual(calls,1);assert(s.el('upload-status').innerHTML.includes('text-danger'));assert.strictEqual(s.ctx.currentBookFilename,'book.txt');
await script();assert.strictEqual(calls,2);assert.strictEqual(s.ctx.getLoadedScriptSourceFilename(),'book.txt');
s.ctx.currentBookFilename='';s.el('btn-gen-script').disabled=false;await script();assert.strictEqual(calls,2);assert.strictEqual(s.ctx.getLoadedScriptSourceFilename(),'');
""")
