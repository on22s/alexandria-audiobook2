"""Exercise real chapter handlers before expansion, reads and serialization."""
from pathlib import Path
import os
import subprocess
import unittest

SOURCE = Path(os.environ.get('CHAPTER_PRESET_SOURCE', Path(__file__).resolve().parent.parent / 'static/js/app-core.js'))
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const elements={},toasts=[],storage=new Map(),readers=[],requests=[],handlers={};let prompt='';
const el=id=>elements[id]||(elements[id]={value:'',checked:false,style:{},innerHTML:'',appendChild:()=>{},addEventListener:(_event,callback)=>handlers[id]=callback});
class Reader{constructor(){readers.push(this);}readAsText(file){this.pending=file.text().then(text=>{this.result=text;this.onload();});}}
const ctx={window:null,currentBookFilename:"book.txt",FileReader:Reader,URLSearchParams,document:{getElementById:el,createElement:()=>({}),querySelectorAll:()=>[]},localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},showToast:(...args)=>toasts.push(args),showConfirm:async()=>true,showPresetEditor:async()=>prompt?({name:prompt,description:''}):null,prompt:()=>prompt,API:{get:async url=>{requests.push(url);return {chapters:[]};},post:async(url,data)=>{requests.push({url,data});return {count:Object.keys(data).length};}},pollExport:()=>{}};ctx.window=ctx;
vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('function showActionError('),source.indexOf('function showConfirm(')),ctx);const run=code=>vm.runInContext(code,ctx);
function load(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a);run(source.slice(a,b));}
load('function escapeHtml(', '// Parse a numeric input');
load('const taskStartButtons =','// --- API Helpers ---');
load('const CHAPTER_PRESETS_KEY =', 'function renderChapterList(');
ctx.renderChapterList=()=>{};
load("async function getChapterExportPreview(", "document.getElementById('chapter-cancel-btn').addEventListener");
for(const [id,value] of Object.entries({'chapter-format':'wav','chapter-template':'{chapter_number}','chapter-padding':'2'})){el(id).value=value;}
const key='alexandria.chapter-template-presets';
'''


class ChapterInputSafetyJsTests(unittest.TestCase):
    def test_import_latest_selection_owns_storage_and_error_feedback(self):
        self.run_js(r'''
ctx.FileReader=class {constructor(){readers.push(this);}readAsText(file){this.file=file;}};
const input=()=>({files:[{size:100}],value:'selected'});
storage.set(key,JSON.stringify({retained:{template:'keep'}}));
ctx.importChapterTemplatePresets(input());ctx.importChapterTemplatePresets(input());
readers[1].result=JSON.stringify({shared:{template:'NEW',padding:3}});readers[1].onload();const saved=storage.get(key),count=toasts.length;
readers[0].result=JSON.stringify({shared:{template:'OLD',padding:2}});readers[0].onload();assert.strictEqual(storage.get(key),saved);assert.strictEqual(toasts.length,count);assert.strictEqual(JSON.parse(saved).retained.template,'keep');
ctx.importChapterTemplatePresets(input());ctx.importChapterTemplatePresets(input());readers[2].onerror();readers[2].result='invalid';readers[2].onload();assert.strictEqual(toasts.length,count);assert.strictEqual(storage.get(key),saved);
readers[3].onerror();assert.strictEqual(toasts.length,count+1);assert(toasts.at(-1)[0].includes('could not be read'));
ctx.importChapterTemplatePresets(input());readers[4].result=JSON.stringify({next:{template:'next'}});readers[4].onload();assert.strictEqual(JSON.parse(storage.get(key)).next.template,'next');
''')

    def test_preview_count_and_export_use_current_selection_and_reject_stale_responses(self):
        self.run_js(r"""
let done=false;process.on('beforeExit',()=>assert(done,'chapter count assertions must finish'));
el('chapter-changed-only').checked=true;el('chapter-selection').value='2';
let resolve;ctx.API.get=url=>{requests.push(url);return new Promise(r=>resolve=r);};
const preview=handlers['chapter-preview-btn']();el('chapter-selection').value='3';resolve({chapters:[{file:'old.wav'}]});await preview;assert.strictEqual(el('chapter-status').textContent,undefined);
const newer=handlers['chapter-preview-btn']();resolve({chapters:[{file:'three.wav'}]});await newer;assert.match(el('chapter-status').textContent,/1 chapter.*Changed only enabled/);
let polls=0;ctx.pollExport=()=>polls++;
const exporting=handlers['chapter-export-btn']();el('chapter-changed-only').checked=false;resolve({chapters:[{file:'three.wav'}]});await exporting;
assert.strictEqual(requests.filter(r=>typeof r==='object').length,0);assert.strictEqual(polls,0);assert.strictEqual(el('chapter-export-btn').disabled,false);assert.match(el('chapter-status').innerHTML,/settings changed/);
ctx.API.get=async url=>{requests.push(url);return {chapters:[{file:'three.wav'},{file:'four.wav'}]};};
let posted;ctx.API.post=async(url,data)=>{posted={url,data};assert.match(el('chapter-status').textContent,/Exporting 2 chapter/);};
await handlers['chapter-export-btn']();assert.strictEqual(posted.data.changed_only,false);assert.deepStrictEqual(JSON.parse(JSON.stringify(posted.data.chapters)),[2]);assert.strictEqual(polls,1);
ctx.releaseTaskStart('chapter_export');
ctx.API.get=async()=>{throw Error('preview refused');};posted=null;await handlers['chapter-export-btn']();assert.strictEqual(posted,null);assert.strictEqual(el('chapter-export-btn').disabled,false);assert.match(el('chapter-status').innerHTML,/preview refused/);
done=true;
""")

    def test_save_dialog_cancel_and_changed_settings_preserve_stored_presets(self):
        self.run_js(r'''
const original=JSON.stringify({existing:{template:'old',padding:2}});storage.set(key,original);
for(const change of ['cancel','template','cache']){let answer;ctx.showPresetEditor=options=>{assert.strictEqual(options.includeDescription,false);assert(options.title.includes('chapter filename'));return new Promise(resolve=>answer=resolve);};const saving=ctx.saveChapterTemplatePreset();if(change==='template'){el('chapter-template').value='later edit';}else if(change==='cache'){storage.set(key,JSON.stringify({newer:{template:'new',padding:3}}));}const before=storage.get(key);answer(change==='cancel'?null:{name:'new'});await saving;assert.strictEqual(storage.get(key),before);if(change==='template'){assert.strictEqual(el('chapter-template').value,'later edit');}}
''')

    def test_overwrite_requires_decision_and_rechecks_settings_and_storage(self):
        self.run_js(r'''
const original=JSON.stringify({existing:{template:'original',padding:2,selection:'1'}});storage.set(key,original);prompt='existing';let decision;const messages=[];ctx.showConfirm=message=>{messages.push(message);return new Promise(resolve=>decision=resolve);};
for(const outcome of ['cancel','settings','storage','approve']){storage.set(key,original);el('chapter-template').value='replacement';const saving=ctx.saveChapterTemplatePreset();await new Promise(setImmediate);assert(messages.at(-1).includes('"existing"'));assert.strictEqual(storage.get(key),original);if(outcome==='settings'){el('chapter-template').value='later edit';}if(outcome==='storage'){storage.set(key,JSON.stringify({existing:{template:'newer',padding:4}}));}const before=storage.get(key);decision(outcome!=='cancel');await saving;if(outcome==='approve'){assert.strictEqual(JSON.parse(storage.get(key)).existing.template,'replacement');}else{assert.strictEqual(storage.get(key),before);}}
prompt='new';const count=messages.length;await ctx.saveChapterTemplatePreset();assert.strictEqual(messages.length,count);assert(Object.hasOwn(JSON.parse(storage.get(key)),'new'));
''')

    def run_js(self, code):
        script = SETUP + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_ranges_are_bounded_before_expansion_and_valid_selections_preserved(self):
        self.run_js(r'''
assert.deepStrictEqual(JSON.parse(JSON.stringify(ctx.parseChapterSelection('3,1-2,2'))),[0,1,2]);assert.strictEqual(ctx.parseChapterSelection(''),null);assert.strictEqual(ctx.parseChapterSelection('1-10000').length,10000);
let adds=0;ctx.Set=class extends Set{add(value){if(++adds>10000){throw Error('unsafe expansion instrumentation');}return super.add(value);}};
for(const value of ['1-10001','1-999999999','1-5001,1-5000']){adds=0;assert.throws(()=>ctx.parseChapterSelection(value),/expansion limit/);assert(adds<=10000);}
assert.throws(()=>ctx.parseChapterSelection('1,'.repeat(32769)),/input limit/);assert.throws(()=>ctx.parseChapterSelection('9007199254740992'),/safe whole numbers/);
assert.deepStrictEqual(JSON.parse(JSON.stringify(ctx.parseChapterSelection('999999999'))),[999999998]);
''')

    def test_invalid_range_is_visible_and_neither_preview_nor_export_sends_request(self):
        self.run_js(r'''
el('chapter-selection').value='1-10001';await handlers['chapter-preview-btn']();assert.strictEqual(requests.length,0);assert.match(toasts.at(-1)[0],/Preview failed.*expansion limit/);
await handlers['chapter-export-btn']();assert.strictEqual(requests.length,0);assert.strictEqual(el('chapter-cancel-btn').style.display,'none');assert.match(el('chapter-status').innerHTML,/expansion limit/);
el('chapter-selection').value='1,3-4';await handlers['chapter-preview-btn']();assert.strictEqual(requests.length,1);const query=new URL(requests[0],'http://fixture').searchParams;assert.deepStrictEqual(query.getAll('chapters'),['0','2','3']);
''')

    def test_import_size_is_checked_before_reader_and_boundary_valid_file_round_trips(self):
        self.run_js(r'''
for(const size of [1048577,4*1024**3]){const input={files:[{size,text:async()=>'{"large":{"template":"fixture"}}'}],value:'selected'};ctx.importChapterTemplatePresets(input);assert.strictEqual(input.value,'');assert.strictEqual(readers.length,0);assert.match(toasts.at(-1)[0],/1 MiB limit/);assert.strictEqual(storage.size,0);}
const json=JSON.stringify({normal:{template:'{chapter_name}',padding:3,selection:'1-3'}});const file=new File([json+' '.repeat(1048576-json.length)],'presets.json');const input={files:[file],value:'selected'};ctx.importChapterTemplatePresets(input);assert.strictEqual(readers.length,1);await readers[0].pending;assert.strictEqual(JSON.parse(storage.get(key)).normal.selection,'1-3');
const saved=storage.get(key);ctx.importChapterTemplatePresets({files:[new File(['invalid'],'bad.json')],value:'selected'});await readers[1].pending;assert.strictEqual(storage.get(key),saved);assert.strictEqual(toasts.at(-1)[1],'error');
''')

    def test_prototype_sensitive_alias_and_preset_names_survive_json_save_reload_delete(self):
        self.run_js(r'''
for(const name of ['__proto__','constructor','toString']){prompt=name;el('chapter-template').value='template '+name;await ctx.saveChapterTemplatePreset();const saved=JSON.parse(storage.get(key));assert(Object.hasOwn(saved,name));assert.strictEqual(saved[name].template,'template '+name);ctx.loadChapterTemplatePreset(name);assert.strictEqual(el('chapter-template').value,'template '+name);}
el('chapter-template-preset').value='__proto__';await ctx.deleteChapterTemplatePreset();assert(!Object.hasOwn(JSON.parse(storage.get(key)),'__proto__'));
load('let characterAliasesLoaded', '// One-line "N changes:');run('characterAliasesLoaded=true');ctx.document.querySelectorAll=()=>['__proto__','constructor','toString'].map(name=>({querySelector:selector=>({value:selector==='.nick-alias'?name:'Alice'})}));
await ctx.saveCharacterAliases();const saved=JSON.parse(JSON.stringify(requests.at(-1).data));assert.deepStrictEqual(Object.keys(saved),['__proto__','constructor','toString']);assert.strictEqual(saved.__proto__,'Alice');assert.strictEqual({}.Alice,undefined);
''')

    def test_delete_requires_current_confirmation_and_preserves_storage_on_cancel_or_change(self):
        self.run_js(r'''
const original=JSON.stringify({A:{template:'A',padding:2},B:{template:'B',padding:3}});storage.set(key,original);el('chapter-template-preset').value='A';let decision;const messages=[];ctx.showConfirm=message=>{messages.push(message);return new Promise(resolve=>decision=resolve);};
let pending=ctx.deleteChapterTemplatePreset();assert(messages[0].includes('"A"'));await ctx.deleteChapterTemplatePreset();assert.strictEqual(messages.length,1);assert.strictEqual(storage.get(key),original);decision(false);await pending;assert.strictEqual(storage.get(key),original);
pending=ctx.deleteChapterTemplatePreset();el('chapter-template-preset').value='B';decision(true);await pending;assert.strictEqual(storage.get(key),original);assert(toasts.at(-1)[0].includes('changed'));
el('chapter-template-preset').value='A';pending=ctx.deleteChapterTemplatePreset();const newer=JSON.stringify({A:{template:'new',padding:4},B:{template:'B',padding:3}});storage.set(key,newer);decision(true);await pending;assert.strictEqual(storage.get(key),newer);
pending=ctx.deleteChapterTemplatePreset();decision(true);await pending;assert(!Object.hasOwn(JSON.parse(storage.get(key)),'A'));assert.strictEqual(JSON.parse(storage.get(key)).B.padding,3);
ctx.localStorage.setItem=()=>{throw Error('blocked storage');};el('chapter-template-preset').value='B';pending=ctx.deleteChapterTemplatePreset();decision(true);await pending;assert(Object.hasOwn(JSON.parse(storage.get(key)),'B'));assert.strictEqual(toasts.at(-1)[1],'error');pending=ctx.deleteChapterTemplatePreset();decision(false);await pending;assert.strictEqual(messages.length,6);
''')
