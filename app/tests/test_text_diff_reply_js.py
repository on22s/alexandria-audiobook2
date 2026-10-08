from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
function setup(){const panel={style:{display:'none'},innerHTML:'',textContent:''},summary={textContent:''},errors=[],reads=[];
const context={currentBookFilename:'A',chunkEditsRevision:0,pendingChunkEdits:new Map(),failedChunkEdits:new Map(),escapeHtml:String,
document:{getElementById:id=>id==='text-diff-panel'?panel:id==='text-diff-summary'?summary:{}},API:{get:()=>new Promise((resolve,reject)=>reads.push({resolve,reject}))},showActionError:(...args)=>errors.push(args)};
vm.createContext(context);const start=source.indexOf('let editorIntegrityView =');vm.runInContext(source.slice(start,source.indexOf('function scrollToChunkRow(',start)),context);return{context,panel,summary,errors,reads};}
const old={status:'verified',totals:{script_words:2,source_words:2,deleted:0,inserted:0,replaced:0},hunks:[]};
const fresh={status:'differences',totals:{script_words:2,source_words:2,deleted:0,inserted:0,replaced:1},hunks:[{kind:'replace',chunk:0,entry_index:0,source_words:'OLD',script_words:'NEW',source_before:'',source_after:''}]};
'''


class TextDiffReplyTests(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(['node', '-e', SETUP + code, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_older_reply_or_error_cannot_replace_newer_comparison(self):
        self.run_js(r'''
(async()=>{for(const failure of [false,true]){const s=setup(),a=s.context.openTextDiff(),b=s.context.openTextDiff();assert.equal(s.reads.length,2);s.reads[1].resolve(fresh);await b;const html=s.panel.innerHTML,summary=s.summary.textContent;assert(html.includes('NEW'));if(failure){s.reads[0].reject(Error('older failure'));}else{s.reads[0].resolve(old);}await a;assert.equal(s.panel.innerHTML,html);assert.equal(s.summary.textContent,summary);assert.equal(s.errors.length,0);}})().catch(error=>{console.error(error);process.exitCode=1;});
''')

    def test_book_edits_close_and_direct_merge_comparison_invalidate_pending_reply(self):
        self.run_js(r'''
(async()=>{for(const change of ['book','edit','direct','close']){const s=setup(),a=s.context.openTextDiff();if(change==='book'){s.context.currentBookFilename='B';}else if(change==='edit'){s.context.chunkEditsRevision++;}else{s.context.renderTextDiff(fresh);if(change==='close'){await s.context.openTextDiff();assert.equal(s.panel.style.display,'none');}}
const html=s.panel.innerHTML;s.reads[0].resolve(old);await a;assert.equal(s.panel.innerHTML,html);assert.equal(s.errors.length,0);if(change==='book'||change==='edit'||change==='close'){assert.equal(s.panel.style.display,'none');}}})().catch(error=>{console.error(error);process.exitCode=1;});
''')

    def test_current_failure_and_malformed_reply_remain_visible_errors(self):
        self.run_js(r'''
(async()=>{for(const malformed of [false,true]){const s=setup(),a=s.context.openTextDiff();if(malformed){s.reads[0].resolve(null);}else{s.reads[0].reject(Error('current failure'));}await a;assert.equal(s.errors.length,1);assert.equal(s.summary.textContent,'');assert.equal(s.panel.style.display,'none');}})().catch(error=>{console.error(error);process.exitCode=1;});
''')
