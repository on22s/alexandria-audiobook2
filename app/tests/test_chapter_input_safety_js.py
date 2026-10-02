"""Exercise real chapter handlers before expansion, reads and serialization."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const elements={},toasts=[],storage=new Map(),readers=[],requests=[],handlers={};let prompt='';
const el=id=>elements[id]||(elements[id]={value:'',checked:false,style:{},innerHTML:'',appendChild:()=>{},addEventListener:(_event,callback)=>handlers[id]=callback});
class Reader{constructor(){readers.push(this);}readAsText(file){this.pending=file.text().then(text=>{this.result=text;this.onload();});}}
const ctx={window:null,FileReader:Reader,URLSearchParams,document:{getElementById:el,createElement:()=>({}),querySelectorAll:()=>[]},localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},showToast:(...args)=>toasts.push(args),prompt:()=>prompt,API:{get:async url=>{requests.push(url);return {chapters:[]};},post:async(url,data)=>{requests.push({url,data});return {count:Object.keys(data).length};}},pollExport:()=>{}};ctx.window=ctx;
vm.createContext(ctx);const run=code=>vm.runInContext(code,ctx);
function load(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a);run(source.slice(a,b));}
load('function escapeHtml(', '// Parse a numeric input');
load('const taskStartButtons =','// --- API Helpers ---');
load('const CHAPTER_PRESETS_KEY =', 'function renderChapterList(');
ctx.renderChapterList=()=>{};
load("document.getElementById('chapter-preview-btn').addEventListener", "document.getElementById('chapter-cancel-btn').addEventListener");
for(const [id,value] of Object.entries({'chapter-format':'wav','chapter-template':'{chapter_number}','chapter-padding':'2'})){el(id).value=value;}
const key='alexandria.chapter-template-presets';
'''


class ChapterInputSafetyJsTests(unittest.TestCase):
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
for(const name of ['__proto__','constructor','toString']){prompt=name;el('chapter-template').value='template '+name;ctx.saveChapterTemplatePreset();const saved=JSON.parse(storage.get(key));assert(Object.hasOwn(saved,name));assert.strictEqual(saved[name].template,'template '+name);ctx.loadChapterTemplatePreset(name);assert.strictEqual(el('chapter-template').value,'template '+name);}
el('chapter-template-preset').value='__proto__';ctx.deleteChapterTemplatePreset();assert(!Object.hasOwn(JSON.parse(storage.get(key)),'__proto__'));
load('let characterAliasesLoaded', '// One-line "N changes:');run('characterAliasesLoaded=true');ctx.document.querySelectorAll=()=>['__proto__','constructor','toString'].map(name=>({querySelector:selector=>({value:selector==='.nick-alias'?name:'Alice'})}));
await ctx.saveCharacterAliases();const saved=JSON.parse(JSON.stringify(requests.at(-1).data));assert.deepStrictEqual(Object.keys(saved),['__proto__','constructor','toString']);assert.strictEqual(saved.__proto__,'Alice');assert.strictEqual({}.Alice,undefined);
''')
