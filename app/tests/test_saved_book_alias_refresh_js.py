"""Real saved-book and alias handlers must not reuse an old editor's mapping."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
SETUP = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const core=fs.readFileSync(process.argv[1],'utf8'),scripts=fs.readFileSync(process.argv[2],'utf8');
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const panel={innerHTML:'',style:{display:'block'}},gets=[],posts=[],toasts=[];
const context={window:{},console:{error(){}},Date,escapeHtml:String,
 document:{getElementById:()=>panel,querySelectorAll:()=>{
  const values=[...panel.innerHTML.matchAll(/value="([^"]*)"/g)].map(m=>m[1]);
  return values.length?[{querySelector:s=>({value:values[s==='.nick-alias'?0:1]})}]:[];
 }},API:{get:url=>{const d=deferred();gets.push({url,...d});return d.promise;},post:async(url,data)=>{if(url==='/api/scripts/load'){return {status:'loaded',name:'Book B'};}posts.push({url,data});return {count:1};}},
 showToast:(...args)=>toasts.push(args),showConfirm:async()=>true,
 fetch:async()=>({ok:true,json:async()=>({status:'loaded',name:'Book B'})}),ensureCastListEditsDiscardable:async()=>true,clearCastListEditor(){},loadCastList:async()=>{},flushVoiceSaves:async()=>{},resetDesignerForm(){},clearVoiceSuggestions(){},
 loadChunks:async()=>{},refreshVoiceMetadata:async()=>{},loadVoices:async()=>{},loadSavedScripts(){},loadDesignedVoices(){}};
vm.createContext(context);vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),context);
const bookStart=core.indexOf('let currentBookFilename =');vm.runInContext(core.slice(bookStart,core.indexOf('async function loadConfig()',bookStart)),context);
const start=core.indexOf('let characterAliasesLoaded'),end=core.indexOf('// One-line "N changes:',start);
assert(start>=0&&end>start);vm.runInContext(core.slice(start,end),context);
const begin=scripts.indexOf('async function loadScript(name)'),stop=scripts.indexOf('async function deleteScript(name)',begin);
vm.runInContext(scripts.slice(begin,stop),context);
async function initial(){const read=context.loadCharacterAliases(true);gets.at(-1).resolve({OLD:'ALICE'});await read;}
const tick=()=>new Promise(setImmediate);
'''

class SavedBookAliasRefreshJsTests(unittest.TestCase):
    def run_js(self, scenario):
        result = subprocess.run(['node', '-e', SETUP + "let finished=false;process.on('beforeExit',()=>assert(finished,'scenario must finish'));(async()=>{" + scenario +
            "})().then(()=>{finished=true;}).catch(e=>{console.error(e);process.exitCode=1;});",
            str(ROOT / 'static/js/app-core.js'), str(ROOT / 'static/js/app-scripts.js')],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_success_refreshes_and_discards_old_pending_response(self):
        self.run_js(r'''
await initial();const old=context.loadCharacterAliases(true);const stale=gets.at(-1);
const loading=context.loadScript('Book B');await tick();
assert.strictEqual(gets.length,3,'successful switch must request current aliases');
assert(!panel.innerHTML.includes('ALICE'));await context.saveCharacterAliases();assert.strictEqual(posts.length,0);
gets.at(-1).resolve({NEW:'BOB'});await loading;assert(panel.innerHTML.includes('BOB'));
stale.resolve({STALE:'ALICE'});await old;assert(!panel.innerHTML.includes('ALICE'));assert(panel.innerHTML.includes('BOB'));
await context.saveCharacterAliases();assert.deepStrictEqual(JSON.parse(JSON.stringify(posts)),[{url:'/api/character_aliases',data:{NEW:'BOB'}}]);
''')

    def test_failed_refresh_after_switch_stays_disarmed_even_after_stale_success(self):
        self.run_js(r'''
await initial();const old=context.loadCharacterAliases(true);const stale=gets.at(-1);
const loading=context.loadScript('Book B');await tick();assert.strictEqual(gets.length,3);
gets.at(-1).reject(new Error('offline'));await loading;stale.resolve({OLD:'ALICE'});await old;
assert(!panel.innerHTML.includes('ALICE'));await context.saveCharacterAliases();assert.strictEqual(posts.length,0);
assert(toasts.some(args=>String(args[0]).includes('Reload character aliases')));
''')

    def test_cancel_refusal_and_transport_failure_keep_existing_editor(self):
        self.run_js(r'''
await initial();const before=panel.innerHTML;const post=context.API.post;
for(const outcome of ['cancel','refuse','network']){
 context.showConfirm=async()=>outcome!=='cancel';context.API.post=async(url,...args)=>{if(url==='/api/scripts/load'){throw new Error(outcome==='network'?'offline':'refused');}return post(url,...args);};
 await context.loadScript('Book B');assert.strictEqual(panel.innerHTML,before);assert.strictEqual(gets.length,1);
}
await context.saveCharacterAliases();assert.strictEqual(posts.length,1);
''')
