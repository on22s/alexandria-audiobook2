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
const ctx={window:null,currentIsRemote:false,failoverIsRemote:false,console,Date,document:{getElementById:el,querySelectorAll:()=>[],createTextNode:text=>({text}),createElement:()=>({click:()=>{}}),body:{appendChild:()=>{},removeChild:()=>{}}},API:{},_startPolling:(key,fetch,options)=>polls[key]={fetch,...options},
 showToast:(...args)=>toasts.push(args),notifyJobDone:()=>{},renderManualRequest:()=>{},getPersonaContextLines:()=>8,voicesScopeIsNew:()=>true,keepCurrentVoicesIfAsked:async()=>true,
 loadVoices:async()=>{},refreshVoiceMetadata:async()=>{},renderVoiceSuggestions:()=>{},loadChapterExports:()=>{},setTimeout:()=>1};ctx.window=ctx;
vm.createContext(ctx);const run=code=>vm.runInContext(code,ctx);
function load(start,end){const a=core.indexOf(start),b=core.indexOf(end,a);assert(a>=0&&b>a);run(core.slice(a,b));}
load('function escapeHtml(', '// Parse a numeric input');load('async function confirmIfRemote(', '// navigator.clipboard');
if(core.includes('const taskStartButtons =')){load('const taskStartButtons =','// --- API Helpers ---');}
load('function getTaskLogUpdate(', '// --- Setup Tab ---');
if(core.includes('function isTaskFailed(')){load('function isTaskFailed(', '// --- Desktop notifications ---');}
load('async function generatePersonas()', 'async function cancelPersonas()');load('async function pollPersonaStatus()', '// --- Voices Tab ---');
if(core.includes('function getLoraModelsById(')){load('function getLoraModelsById(', 'async function suggestVoices(');}
load('async function suggestVoices(', 'function renderVoiceSuggestions()');
load('function isExportComplete(', '// --- Chapter-by-chapter export ---');load('window.exportM4B =','// --- Polling Logic ---');
ctx.chapterExportParams=()=>({});load("document.getElementById('chapter-export-btn').addEventListener", "document.getElementById('chapter-cancel-btn').addEventListener");
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const turn=()=>new Promise(setImmediate);
'''


class TaskStartControlJsTests(unittest.TestCase):
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
 const gate=deferred();posts=0;ctx.API.post=async()=>{posts++;return gate.promise;};const pending=start();assert.strictEqual(el(id).disabled,true);await start();assert.strictEqual(posts,1);gate.resolve({});await pending;assert.strictEqual(el(id).disabled,true);await start();assert.strictEqual(posts,1);
 assert(!polls[task].doneCheck({running:true}));polls[task].onTick({running:true,logs:[]});assert.strictEqual(el(id).disabled,true);polls[task].onDone({running:false,logs:[],result:{status:'cancelled',message:'fixture'}});assert.strictEqual(el(id).disabled,false);
 ctx.API.post=async()=>{posts++;throw Error('start rejected');};await start();assert.strictEqual(el(id).disabled,false);assert.strictEqual(posts,2);
}
''')

    def test_persona_preflight_is_owned_cancellation_releases_and_accepted_job_stays_owned(self):
        self.run_js(r'''
ctx.voicesScopeIsNew=()=>false;const choice=deferred();let prompts=0;ctx.keepCurrentVoicesIfAsked=()=>{prompts++;return choice.promise;};ctx.API.post=async()=>{posts++;return {};};
const pending=ctx.generatePersonas();assert.strictEqual(el('btn-gen-personas').disabled,true);await ctx.generatePersonas();assert.strictEqual(prompts,1);choice.resolve(false);await pending;assert.strictEqual(el('btn-gen-personas').disabled,false);assert.strictEqual(posts,0);
ctx.voicesScopeIsNew=()=>true;const gate=deferred();ctx.API.post=async()=>{posts++;return gate.promise;};const starting=ctx.generatePersonas();await ctx.generatePersonas();assert.strictEqual(posts,1);gate.resolve({});await starting;assert.strictEqual(el('btn-gen-personas').disabled,true);await ctx.generatePersonas();assert.strictEqual(posts,1);
await polls.persona.onDone({running:false,logs:['Task persona completed successfully.']});assert.strictEqual(el('btn-gen-personas').disabled,false);ctx.API.post=async()=>{throw Error('rejected');};await ctx.generatePersonas();assert.strictEqual(el('btn-gen-personas').disabled,false);
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
