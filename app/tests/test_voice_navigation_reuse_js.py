"""Native Node executes real loader/navigation, with controlled API responses."""
import subprocess
import unittest
from tests import test_voice_load_requests_js as loader_tests


class VoiceNavigationReuseJsTests(unittest.TestCase):
    def test_pending_navigation_shares_startup_load_but_explicit_refresh_stays_fresh(self):
        self.run_js(r'''
const c=client();const startup=c.ctx.loadVoices();const navigation=c.ctx.loadVoices(false);await turn();assert.strictEqual(c.reads.length,0);c.saveGate.resolve();await turn();assert.strictEqual(c.reads.length,5,'pending navigation must share the startup fetches');c.resolveAll();await Promise.all([startup,navigation]);assert.strictEqual(c.draws.length,1);
c.reads.length=0;const forced=c.ctx.loadVoices();await turn();assert.strictEqual(c.reads.length,5);c.resolveAll();await forced;
c.reads.length=0;c.ctx.voiceSaveQueue.flush=()=>Promise.reject(Error('save failed'));await assert.rejects(c.ctx.loadVoices(false),/save failed/);c.ctx.voiceSaveQueue.flush=()=>Promise.resolve();await finish(c,true);assert.strictEqual(c.reads.length,5,'rejected pending promise must clear for recovery');
''')

    def run_js(self, body):
        code = loader_tests.SETUP + r"""
async function finish(c, force=false){const promise=c.ctx.loadVoices(force);await turn();c.resolveAll();await promise;}
let finished=false;process.on('beforeExit',()=>assert(finished,'all assertions must finish'));
""" + '\n(async()=>{\n' + body + '\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', code, str(loader_tests.SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_navigation_reuses_resources_and_cards_but_checks_metadata_and_expires(self):
        self.run_js(r"""
const c=client();let clock=0;c.ctx.performance.now=()=>clock;c.saveGate.resolve();
await finish(c);assert.strictEqual(c.reads.length,5);assert.strictEqual(c.draws.length,1);
const cards=c.el('voices-list').innerHTML;
for(let i=0;i<3;i++){c.reads.length=0;clock+=1000;await finish(c);assert.deepStrictEqual(c.reads,['/api/voice_config/snapshot']);assert.strictEqual(c.draws.length,1);assert.strictEqual(c.el('voices-list').innerHTML,cards);}
c.reads.length=0;clock=10000;await finish(c);assert.strictEqual(c.reads.length,5);assert.strictEqual(c.draws.length,2);
c.reads.length=0;await finish(c,true);assert.strictEqual(c.reads.length,5);assert.strictEqual(c.draws.length,3,'mutation callers force fresh dropdowns/cards within TTL');
""")

    def test_changed_revision_or_book_renders_even_with_fresh_resources(self):
        self.run_js(r"""
const c=client();c.saveGate.resolve();await finish(c);
c.snapshot.revision='1'.repeat(64);c.snapshot.voices=[{name:'Bob',config:{type:'custom',voice:'Ryan'}}];c.reads.length=0;await finish(c);assert.strictEqual(c.reads.length,1);assert.strictEqual(c.draws.length,2);assert(c.el('voices-list').innerHTML.includes('Bob:'));
c.snapshot.book_token='c'.repeat(64);c.snapshot.voices=[{name:'Carol',config:{type:'custom',voice:'Ryan'}}];c.reads.length=0;await finish(c);assert.strictEqual(c.reads.length,1);assert.strictEqual(c.draws.length,3);assert(c.el('voices-list').innerHTML.includes('Carol:'));
""")

    def test_partial_resource_failure_retries_and_save_guards_survive_reuse(self):
        self.run_js(r"""
const c=client();c.saveGate.resolve();let pending=c.ctx.loadVoices(false);await turn();c.gates.get('/api/voice_design/list').reject(Error('optional failed'));c.resolveAll();await pending;assert.strictEqual(c.draws.length,1);
c.reads.length=0;await finish(c);assert.strictEqual(c.reads.length,5,'optional failure must not cache degraded resources');
const cards=c.el('voices-list').innerHTML;c.reads.length=0;c.ctx.voiceSaveQueue.flush=()=>Promise.reject(Error('unsaved edits'));await assert.rejects(c.ctx.loadVoices(false),/unsaved edits/);assert.strictEqual(c.reads.length,0);assert.strictEqual(c.el('voices-list').innerHTML,cards);
c.ctx.voiceSaveQueue.flush=()=>Promise.resolve();pending=c.ctx.loadVoices(false);await turn();c.setDirty();c.resolveAll();await assert.rejects(pending,/Voice edits changed/);assert.strictEqual(c.el('voices-list').innerHTML,cards);
""")

    def test_real_navigation_requests_reuse_and_regular_call_still_forces(self):
        self.run_js(r"""
const c=client(),loads=[];let handler;
const link={dataset:{tab:'voices'},classList:{remove(){},add(){}},removeAttribute(){},setAttribute(){},addEventListener(name,fn){assert.strictEqual(name,'click');handler=fn;}};
c.ctx.document.querySelectorAll=selector=>selector==='.nav-link'?[link]:[{style:{}}];c.ctx.document.getElementById=id=>({style:{},classList:{contains:()=>false}});c.ctx.rememberTab=()=>{};c.ctx.loadVoices=(...args)=>loads.push(args);
c.ctx.window=c.ctx;c.ctx.location={hash:'#setup'};c.ctx.history={pushState:(_s,_t,hash)=>c.ctx.location.hash=hash};c.ctx.addEventListener=()=>{};
const a=source.indexOf('const TAB_STORAGE_KEY =');const b=source.indexOf('// --- LLM model picker:',a);assert(a>=0&&b>a);vm.runInContext(source.slice(a,b),c.ctx);
handler({preventDefault(){},currentTarget:link});assert.deepStrictEqual(loads,[[false]]);
""")
