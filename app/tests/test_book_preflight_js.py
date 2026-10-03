"""Exercise actual preflight controls against a native structured receipt."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from routers import script

class BookPreflightJsTests(unittest.TestCase):
    def run_js(self, extra):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, 'sample.json'))
            plan = {'1': 3, '2': 4, '3': 2}
            sample = {'label': 'first', 'chunk_index': 0, 'status': 'failed', 'planned_calls': plan, 'failure_codes': {'coverage': 2}, 'error': '<rejected>'}
            script.atomic_json_write({'status': 'failed'}, script.three_pass_manifest_path(output + '.preflight_first.json'))
            script.atomic_json_write({'status': 'failed', 'planned_calls': plan, 'samples': [sample]}, output + '.preflight_manifest.json')
            summary = script.get_completed_book_preflight_summary(output)
        receipt = {'job_id': 'a' * 32, 'status': 'failed', 'source_filename': '<book>.txt', 'source_is_current': False, 'summary': summary}
        source = Path(__file__).parent.parent / 'static/js/app-core.js'
        harness = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),receipt=JSON.parse(process.argv[2]);
const elements={}; for(const id of ['book-preflight-result','btn-book-preflight','btn-cancel-book-preflight','script-first-person-narrator']){elements[id]={style:{},innerHTML:'',value:' ALICE ',disabled:false};}
const posts=[],toasts=[]; let poll;
const ctx={document:{getElementById:id=>elements[id]},escapeHtml:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),confirmIfRemote:async()=>true,_isStripFrontMatterChecked:()=>false,showToast:s=>toasts.push(s),API:{post:async(url,data)=>{posts.push({url,data});return{...receipt,status:'running'};},get:async()=>receipt},_startPolling:(key,fetch,options)=>{assert.strictEqual(key,'book_preflight');poll={fetch,options};}};
ctx.window=ctx;vm.createContext(ctx);const run=code=>vm.runInContext(code,ctx);
run(source.slice(source.indexOf('let bookPreflightJob ='),source.indexOf('// End book-preflight controls.')));
'''
        result = subprocess.run(['node', '-e', harness + extra, str(source), json.dumps(receipt)],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_native_failure_plan_and_owned_cancel(self):
        self.run_js(r'''
(async()=>{await ctx.startBookPreflight();assert.strictEqual(posts.length,1);assert.strictEqual(posts[0].data.strip_front_matter,false);assert.strictEqual(posts[0].data.first_person_narrator,'ALICE');assert(elements['btn-book-preflight'].disabled);await ctx.cancelBookPreflight();assert.strictEqual(posts[1].url,`/api/generate_script/preflight/${receipt.job_id}/cancel`);assert.match(toasts.at(-1),/worker to exit/);const data=await poll.fetch();await poll.options.onTick(data);assert(poll.options.doneCheck(data));assert.match(elements['book-preflight-result'].innerHTML,/coverage: 2/);assert.match(elements['book-preflight-result'].innerHTML,/Step 1 3/);assert.match(elements['book-preflight-result'].innerHTML,/earlier source/);assert.match(elements['book-preflight-result'].innerHTML,/&lt;rejected&gt;/);assert(!elements['book-preflight-result'].innerHTML.includes('<rejected>'));await poll.options.onDone(data);assert(!elements['btn-book-preflight'].disabled);assert.strictEqual(posts.length,2);})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_decline_and_stale_poll_do_not_start_or_overwrite(self):
        self.run_js(r'''
(async()=>{ctx.confirmIfRemote=async()=>false;await ctx.startBookPreflight();assert.strictEqual(posts.length,0);assert(!elements['btn-book-preflight'].disabled);ctx.confirmIfRemote=async()=>true;await ctx.startBookPreflight();const before=elements['book-preflight-result'].innerHTML;const stale={...receipt,job_id:'b'.repeat(32)};await poll.options.onTick(stale);await poll.options.onDone(stale);assert.strictEqual(elements['book-preflight-result'].innerHTML,before);assert(elements['btn-book-preflight'].disabled);assert(!poll.options.doneCheck(stale));})().catch(e=>{console.error(e);process.exitCode=1;});
''')
