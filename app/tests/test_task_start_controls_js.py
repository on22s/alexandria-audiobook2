"""Actual start handlers own admission before awaits and through task completion."""
from pathlib import Path
from html.parser import HTMLParser
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
WORKBENCH = SOURCE.with_name('app-workbench.js')
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),core=fs.readFileSync(process.argv[1],'utf8'),workbench=fs.readFileSync(process.argv[2],'utf8');
const elements={},handlers={},polls={},toasts=[];let posts=0,gets=0;
const el=id=>elements[id]||(elements[id]={disabled:false,value:'',checked:false,style:{},addEventListener:(_event,callback)=>handlers[id]=callback});
const ctx={window:null,currentBookFilename:'book.txt',currentIsRemote:false,failoverIsRemote:false,console,Date,document:{getElementById:el,querySelectorAll:()=>[],createTextNode:text=>({text}),createElement:()=>({click:()=>{}}),body:{appendChild:()=>{},removeChild:()=>{}}},API:{},_startPolling:(key,fetch,options)=>polls[key]={fetch,...options},
 showToast:(...args)=>toasts.push(args),notifyJobDone:()=>{},renderManualRequest:()=>{},getPersonaContextLines:()=>8,voicesScopeIsNew:()=>true,keepCurrentVoicesIfAsked:async()=>true,
 loadVoices:async()=>({refreshedResources:['/api/voice_design/list','/api/clone_voices/list'],failedResources:[]}),refreshVoiceMetadata:async()=>{},renderVoiceSuggestions:()=>{},loadChapterExports:()=>{},setTimeout:()=>1};ctx.window=ctx;
vm.createContext(ctx);vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),ctx);const run=code=>vm.runInContext(code,ctx);
function load(start,end){const a=core.indexOf(start),b=core.indexOf(end,a);assert(a>=0&&b>a);run(core.slice(a,b));}
load('function escapeHtml(', '// Parse a numeric input');load('async function confirmIfRemote(', '// navigator.clipboard');
if(core.includes('const taskStartButtons =')){load('const taskStartButtons =','// --- API Helpers ---');}
load('function getTaskLogUpdate(', '// --- Setup Tab ---');
if(core.includes('function isTaskFailed(')){load('function isTaskFailed(', '// --- Desktop notifications ---');}
load('async function generatePersonas()', 'async function cancelPersonas()');load('let personaVoiceRefreshRequest =', 'async function pollPersonaStatus()');
load('async function pollPersonaStatus()', '// --- Voices Tab ---');
if(core.includes('function getLoraModelsById(')){load('function getLoraModelsById(', 'async function suggestVoices(');}
load('async function suggestVoices(', 'function renderVoiceSuggestions()');
load('function isExportComplete(', '// --- Chapter-by-chapter export ---');load('window.exportM4B =','// --- Polling Logic ---');
ctx.URLSearchParams=URLSearchParams;ctx.chapterExportParams=()=>({chapters:null});load('async function getChapterExportPreview(',"document.getElementById('chapter-preview-btn').addEventListener");load("document.getElementById('chapter-export-btn').addEventListener", "document.getElementById('chapter-cancel-btn').addEventListener");
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const turn=()=>new Promise(setImmediate);
'''


class TaskStartControlJsTests(unittest.TestCase):
    def test_unsupported_pause_has_persistent_help_without_enabling_controls(self):
        code = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const core=fs.readFileSync(process.argv[1],'utf8');
const html=fs.readFileSync(process.argv[2],'utf8');
assert.strictEqual((html.match(/id="script-pause-unavailable-help"/g)||[]).length,1);
assert(/id="script-pause-unavailable-help" hidden/.test(html));
const ids=['btn-pause-script','btn-pause-batch-script','btn-pause-review','btn-pause-batch-review','btn-pause-nick','btn-vl-pause'];
const buttons=Object.fromEntries(ids.map(id=>[id,{disabled:false,title:'',attrs:{},classes:new Set(['btn-outline-warning']),setAttribute(k,v){this.attrs[k]=v;},classList:{remove(k){buttons[id].classes.delete(k);},add(k){buttons[id].classes.add(k);}}}]));
const hint={hidden:true};
const ctx={document:{getElementById:id=>id==='script-pause-unavailable-help'?hint:buttons[id],querySelectorAll:()=>Object.values(buttons)}};
vm.createContext(ctx);vm.runInContext(core.slice(core.indexOf('function applyPauseSupport('),core.indexOf('// Restores the Pause appearance')),ctx);
for(const capabilities of [null,{}, {pause_resume:true}]){ctx.applyPauseSupport(capabilities);assert(hint.hidden);assert(ids.every(id=>!buttons[id].disabled));}
ctx.applyPauseSupport({pause_resume:false});ctx.applyPauseSupport({pause_resume:false});
assert.strictEqual(hint.hidden,false);
for(const id of ids){assert(buttons[id].disabled);assert(buttons[id].title.includes('Use Cancel'));assert(buttons[id].classes.has('btn-outline-secondary'));assert(!buttons[id].classes.has('btn-outline-warning'));}
for(const id of ids.slice(0,2)){assert.strictEqual(buttons[id].attrs['aria-describedby'],'script-pause-unavailable-help');}
"""
        result = subprocess.run(['node', '-e', code, str(SOURCE), str(SOURCE.parent.parent / 'index.html')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_script_cost_admission_is_owned_before_await_and_recovers(self):
        self.run_js(r"""
run('let _scriptStartOver=false;let scriptBatchPoller=null;');
load("document.getElementById('btn-gen-script').addEventListener('click', async () => {", '// Pause is SIGSTOP');
el('file-upload').files=[];el('upload-status').innerHTML='<span class="text-success">Loaded</span>';
el('btn-pause-script').classList={remove(){},add(){}};ctx._resetPauseBtn=()=>{};ctx._isStripFrontMatterChecked=()=>false;ctx.refreshScriptRecovery=()=>{};
let completed=false;process.on('beforeExit',()=>assert(completed,'script assertions must finish'));
for(const outcome of ['decline','error','approve']){
 const gate=deferred();let admissions=0,posted=0,poll;
 ctx.confirmIfRemote=()=>{admissions++;return gate.promise;};ctx.API.post=async()=>{posted++;};ctx.pollScriptLogs=(_task,done)=>poll=done;
 const first=handlers['btn-gen-script']();assert.strictEqual(el('btn-gen-script').disabled,true);
 await handlers['btn-gen-script']();assert.strictEqual(admissions,1);assert.strictEqual(posted,0);
 if(outcome==='error'){gate.reject(Error('offline'));}else{gate.resolve(outcome==='approve');}
 await first;
 assert.strictEqual(posted,outcome==='approve'?1:0);
 if(outcome==='approve'){assert.strictEqual(el('btn-gen-script').disabled,true);assert.strictEqual(el('btn-cancel-script').style.display,'inline-block');await handlers['btn-gen-script']();assert.strictEqual(admissions,1);poll();}
 assert.strictEqual(el('btn-gen-script').disabled,false);
}
completed=true;
""")

    def test_export_control_ids_are_wired_to_actual_buttons(self):
        class Buttons(HTMLParser):
            def __init__(self):
                super().__init__()
                self.buttons = []

            def handle_starttag(self, tag, attrs):
                if tag == 'button':
                    self.buttons.append(dict(attrs))

        parser = Buttons()
        parser.feed(SOURCE.parent.parent.joinpath('index.html').read_text())
        for id, handler in [('btn-export-audacity', 'exportAudacity()'), ('btn-export-m4b', 'exportM4B()')]:
            matches = [button for button in parser.buttons if button.get('id') == id]
            self.assertEqual(1, len(matches))
            self.assertEqual(handler, matches[0]['onclick'])

    def run_js(self, code):
        result = subprocess.run(['node', '-e', SETUP + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});', str(SOURCE), str(WORKBENCH)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_exports_disable_immediately_block_duplicate_and_release_only_on_terminal_or_start_error(self):
        self.run_js(r'''
for(const [task,id,start] of [['audacity_export','btn-export-audacity',()=>ctx.exportAudacity()],['m4b_export','btn-export-m4b',()=>ctx.exportM4B()],['chapter_export','chapter-export-btn',()=>handlers['chapter-export-btn']()]]){
 const gate=deferred();posts=0;ctx.API.get=async()=>({chapters:[]});ctx.API.post=async()=>{posts++;return gate.promise;};const pending=start();assert.strictEqual(el(id).disabled,true);await start();assert.strictEqual(posts,1);gate.resolve({});await pending;assert.strictEqual(el(id).disabled,true);await start();assert.strictEqual(posts,1);
 assert(!polls[task].doneCheck({running:true}));polls[task].onTick({running:true,logs:[]});assert.strictEqual(el(id).disabled,true);polls[task].onDone({running:false,logs:[],result:{status:'cancelled',message:'fixture'}});assert.strictEqual(el(id).disabled,false);
 ctx.API.post=async()=>{posts++;throw Error('start rejected');};await start();assert.strictEqual(el(id).disabled,false);assert.strictEqual(posts,2);
}
''')

    def test_persona_preflight_is_owned_cancellation_releases_and_accepted_job_stays_owned(self):
        self.run_js(r'''
ctx.voicesScopeIsNew=()=>false;const choice=deferred();let prompts=0;ctx.keepCurrentVoicesIfAsked=()=>{prompts++;return choice.promise;};ctx.API.post=async()=>{posts++;return {};};
const pending=ctx.generatePersonas();assert.strictEqual(el('btn-gen-personas').disabled,true);await ctx.generatePersonas();assert.strictEqual(prompts,1);choice.resolve(false);await pending;assert.strictEqual(el('btn-gen-personas').disabled,false);assert.strictEqual(posts,0);
ctx.voicesScopeIsNew=()=>true;const gate=deferred();ctx.API.post=async()=>{posts++;return gate.promise;};const starting=ctx.generatePersonas();await ctx.generatePersonas();assert.strictEqual(posts,1);gate.resolve({});await starting;assert.strictEqual(el('btn-gen-personas').disabled,true);await ctx.generatePersonas();assert.strictEqual(posts,1);
await polls.persona.onDone({running:false,logs:['Task persona completed successfully.']});assert.strictEqual(el('btn-gen-personas').disabled,false);assert.strictEqual(el('persona-refresh-status').textContent,'');assert.strictEqual(el('persona-refresh-retry').hidden,true);ctx.API.post=async()=>{throw Error('rejected');};await ctx.generatePersonas();assert.strictEqual(el('btn-gen-personas').disabled,false);
''')

    def test_per_character_suggestions_share_global_ownership_through_metadata_refresh(self):
        self.run_js(r'''
const button={disabled:false,closest:()=>({dataset:{voice:'Alice'}})},cache=deferred(),metadata=deferred();ctx.API.get=async()=>{gets++;return cache.promise;};ctx.API.post=async()=>{posts++;return {suggestions:{}};};ctx.refreshVoiceMetadata=()=>metadata.promise;
const pending=ctx.suggestMoreVoices(button);assert.strictEqual(button.disabled,true);assert.strictEqual(el('btn-suggest-voices').disabled,true);await ctx.suggestMoreVoices(button);await ctx.suggestVoices();assert.strictEqual(gets,1);assert.strictEqual(posts,0);cache.resolve([]);await turn();assert.strictEqual(posts,1);assert.strictEqual(button.disabled,true);await ctx.suggestVoices();assert.strictEqual(posts,1);metadata.resolve();await pending;assert.strictEqual(button.disabled,false);assert.strictEqual(el('btn-suggest-voices').disabled,false);
ctx.API.get=async()=>[];ctx.API.post=async()=>{throw Error('rejected');};await ctx.suggestMoreVoices(button);assert.strictEqual(button.disabled,false);assert.strictEqual(el('btn-suggest-voices').disabled,false);
ctx.voicesScopeIsNew=()=>false;ctx.keepCurrentVoicesIfAsked=async()=>false;await ctx.suggestVoices();assert.strictEqual(el('btn-suggest-voices').disabled,false);
''')

    def test_reload_reattachment_claims_suggestion_start_until_status_finishes(self):
        self.run_js(r'''
const a=workbench.indexOf('function reattachTaskActivity('),b=workbench.indexOf('let _reattachGeneration =',a);run(workbench.slice(a,b));ctx.reattachTaskActivity('voices',['btn-suggest-voices'],'suggest-status');ctx.API.get=async()=>{gets++;return [];};ctx.API.post=async()=>{posts++;return {suggestions:{}};};await ctx.suggestVoices();assert.strictEqual(gets,0);assert.strictEqual(posts,0);await polls['reattach:voices'].onDone({running:false,logs:[]});assert.strictEqual(el('btn-suggest-voices').disabled,false);await ctx.suggestVoices();assert.strictEqual(posts,1);
''')
