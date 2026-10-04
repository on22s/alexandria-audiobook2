"""Actual acknowledgements and voice metadata determine defaults and options."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
SCRIPTS = SOURCE.with_name('app-scripts.js')
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),core=fs.readFileSync(process.argv[1],'utf8'),scripts=fs.readFileSync(process.argv[2],'utf8');
const elements={},handlers={},toasts=[],suggestions=[];let selectedFilename='Book.One.txt',refuse=false;
const decode=text=>text.replace(/&quot;/g,'"').replace(/&#039;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
function el(id){if(elements[id]){return elements[id];}let html='';const element={value:'',files:[],style:{},options:[],dataset:{},appendChild:()=>{},addEventListener:(event,callback)=>handlers[id+':'+event]=callback};Object.defineProperty(element,'innerHTML',{get:()=>html,set:value=>{html=value;element.options=[...value.matchAll(/<option value="([^"]*)"/g)].map(match=>({value:decode(match[1])}));}});Object.defineProperty(element,'textContent',{get:()=>decode(html.replace(/<[^>]*>/g,''))});elements[id]=element;return element;}
const ctx={window:null,document:{getElementById:el},API:{post:async()=>{if(refuse){throw Error('refused');}return {stored_filename:selectedFilename};},upload:async()=>({stored_filename:selectedFilename,reused:true})},showPresetEditor:async options=>{suggestions.push(options.name);return null;},showToast:(...args)=>toasts.push(args),showConfirm:async()=>true,console,
 loadExistingScriptUploads:async()=>{},ensureCastListEditsDiscardable:async()=>true,clearCastListEditor(){},loadCastList:async()=>{},flushVoiceSaves:async()=>{},clearCharacterAliases:()=>{},resetDesignerForm:()=>{},clearVoiceSuggestions:()=>{},loadCharacterAliases:async()=>{},loadChunks:async()=>{},loadVoices:async()=>{},loadSavedScripts:()=>{},loadDesignedVoices:()=>{}};ctx.window=ctx;
vm.createContext(ctx);const run=code=>vm.runInContext(code,ctx);
function load(start,end){const a=core.indexOf(start),b=core.indexOf(end,a);assert(a>=0&&b>a);run(core.slice(a,b));}
load('function escapeHtml(', '// Parse a numeric input');
if(core.includes('let currentBookFilename =')){load('let currentBookFilename =','async function loadConfig()');}
load('window.selectExistingScriptUpload =', '// Generate resumes saved progress');
load('window.snapshotScript =','window.cancelScript =');
load('function _keepCastName()', 'function onVoicesScopeChange(');
load('window.updateNarratorPreviewFields =','window.previewNarratorSelection =');
'''


class CurrentBookNarratorJsTests(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(['node', '-e', SETUP + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});', str(SOURCE), str(SCRIPTS)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_upload_reuse_saved_book_acknowledgements_drive_names_not_display_text(self):
        self.run_js(r'''
el('existing-upload-select').value='Book.One.txt';await ctx.selectExistingScriptUpload();assert.strictEqual(ctx._keepCastName(),'Book.One');await ctx.snapshotScript();assert.match(suggestions.at(-1),/^Book\.One snapshot /);
el('upload-status').innerHTML='<span>Unrelated restyled status</span>';assert.strictEqual(ctx._keepCastName(),'Book.One');
selectedFilename='Book.Two.txt';el('file-upload').files=[{}];await handlers['file-upload:change']();assert.strictEqual(ctx._keepCastName(),'Book.Two');
refuse=true;await ctx.selectExistingScriptUpload();assert.strictEqual(ctx._keepCastName(),'Book.Two');refuse=false;
const a=scripts.indexOf('async function loadScript(name)'),b=scripts.indexOf('async function deleteScript(name)',a);run(scripts.slice(a,b));ctx.API.post=async()=>({status:'loaded',name:'Saga.vol.2'});await ctx.loadScript('Saga.vol.2');assert.strictEqual(ctx._keepCastName(),'Saga.vol.2');
ctx.window._selectedCast='Explicit cast';assert.strictEqual(ctx._keepCastName(),'Explicit cast');
''')

    def test_book_and_version_changes_refresh_options_and_keep_only_valid_choices(self):
        self.run_js(r'''
const focus=el('narrator-focus'),version=el('narrator-version');el('narrator-strategy').value='chapter';ctx._voicesNames=['NARRATOR','Alice','Bob'];ctx._voicesByName={NARRATOR:{config:{versions:{adult:{},young:{}}}}};ctx.updateNarratorPreviewFields();assert.deepStrictEqual(focus.options.map(x=>x.value),['','Alice','Bob']);assert.deepStrictEqual(version.options.map(x=>x.value),['','adult','young']);
focus.value='Bob';version.value='young';ctx.updateNarratorPreviewFields();assert.strictEqual(focus.value,'Bob');assert.strictEqual(version.value,'young');
ctx._voicesNames=['Narrator','Clara','Bob'];ctx._voicesByName={Narrator:{config:{versions:{older:{},young:{}}}}};ctx.updateNarratorPreviewFields();assert.deepStrictEqual(focus.options.map(x=>x.value),['','Clara','Bob']);assert.deepStrictEqual(version.options.map(x=>x.value),['','older','young']);assert.strictEqual(focus.value,'Bob');assert.strictEqual(version.value,'young');
ctx._voicesNames=['Narrator','New "name"'];ctx._voicesByName={Narrator:{config:{versions:{new:{}}}}};ctx.updateNarratorPreviewFields();assert.strictEqual(focus.value,'');assert.strictEqual(version.value,'');assert.deepStrictEqual(focus.options.map(x=>x.value),['','New "name"']);
ctx._voicesNames=[];ctx._voicesByName={};ctx.updateNarratorPreviewFields();assert.deepStrictEqual(focus.options.map(x=>x.value),['']);assert.deepStrictEqual(version.options.map(x=>x.value),['']);el('narrator-strategy').value='focus';ctx.updateNarratorPreviewFields();assert.strictEqual(el('narrator-focus-group').style.display,'');assert.strictEqual(el('narrator-version-group').style.display,'none');
''')

    def test_actual_preview_packet_retains_valid_choices_and_clears_removed_values(self):
        self.run_js(r'''
load('window.previewNarratorSelection =','window.setVoiceApproval =');const packets=[];ctx.API.post=async(url,data)=>{assert.strictEqual(url,'/api/narrator/preview');packets.push(data);return {selected:{voice:'fixture'}};};
ctx._voicesNames=['NARRATOR','Alice'];ctx._voicesByName={NARRATOR:{config:{versions:{adult:{}}}}};el('narrator-strategy').value='chapter';ctx.updateNarratorPreviewFields();el('narrator-focus').value='Alice';el('narrator-version').value='adult';await ctx.previewNarratorSelection();assert.strictEqual(packets[0].focus_speaker,'Alice');assert.strictEqual(packets[0].narrator_version,'adult');
ctx._voicesNames=['NARRATOR','Bob'];ctx._voicesByName={NARRATOR:{config:{versions:{older:{}}}}};await ctx.previewNarratorSelection();assert.strictEqual(packets[1].focus_speaker,null);assert.strictEqual(packets[1].narrator_version,null);assert.strictEqual(packets[1].strategy,'chapter');
''')

    def test_snapshot_modal_cancel_and_book_switch_send_no_requests(self):
        self.run_js(r'''
ctx.applyCurrentBookFilename('source.txt');let posts=0;ctx.API.post=async(path,body)=>{posts++;assert.strictEqual(path,'/api/generate_script/snapshot');assert.strictEqual(body.name,'my snapshot');return{name:body.name,entries:2};};
ctx.showPresetEditor=async options=>{assert.strictEqual(options.nameLabel,'Snapshot name');assert.strictEqual(options.actionLabel,'Save snapshot');assert.strictEqual(options.includeDescription,false);return null;};await ctx.snapshotScript();assert.strictEqual(posts,0);
ctx.showPresetEditor=async()=>{ctx.applyCurrentBookFilename('other.txt');return{name:'my snapshot'};};await ctx.snapshotScript();assert.strictEqual(posts,0);assert(toasts.at(-1)[0].includes('book changed'));
ctx.showPresetEditor=async()=>({name:'my snapshot'});await ctx.snapshotScript();assert.strictEqual(posts,1);
''')
