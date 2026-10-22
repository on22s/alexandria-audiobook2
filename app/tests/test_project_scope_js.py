"""Scope controls use reviewed revisions, safe text and bounded/stale UI rendering."""
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

SOURCE=Path(__file__).resolve().parents[1]/'static/js/app-project-scope.js'
SETUP=r'''
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
Element.prototype.replaceChildren=function(...nodes){for(const child of [...this.children]){child.remove();}for(const node of nodes){this.appendChild(node);}};
const text=node=>node.textContent+node.children.map(text).join('');
for(const id of ['panel','find','next','entry','preview','apply','undo','discard','search','feedback','undo-status','review']){
 body.appendChild(new Element('div',{id:'source-scope-'+id}));}
const el=id=>document.getElementById('source-scope-'+id);el('search').value='';el('entry').value='';el('panel').open=true;
const calls=[];let undoAvailable=false,failApply=false,releasePreview=null;
const plan={revision:'a'.repeat(64),start_entry:1,source_name:'<img onerror=evil>',source_range:{removed_start_word:0,retained_start_word:3,end_word:6},
 removed_uids:['old'],removed_script:[{speaker:'<script>',text:'<img src=x>'}],retained:[{uid:'kept',speaker:'A',text:'Exact retained text.',audio_verified:true}],
 voices:[{uid:'kept',effective_voices:{A:{voice:'Ryan',character_style:'Adult'}}}],history_versions:1,preserved_files:[{path:'old.wav',exists:true}],notice:'Older exports stay on disk.'};
context.window=context;context._existingUploadSelectionRequest={};
context.flushVoiceSaves=async()=>{};context.ensureCastListEditsDiscardable=async()=>true;
context.refreshVoiceMetadata=async()=>{};context.loadChunks=async()=>{};
context.API={get:async path=>{calls.push(['GET',path]);return path.includes('/entries?')?{entries:[{start_entry:1,label:'2. A: Exact retained text.'}],next_offset:null,total_entries:2}
 :{available:undoAvailable,can_undo:undoAvailable,revision:undoAvailable?'b'.repeat(64):null};},
 post:async(path,payload)=>{calls.push(['POST',path,payload]);if(path.endsWith('/preview')){return releasePreview?await new Promise(resolve=>releasePreview.resolve=resolve):plan;}
 if(path.endsWith('/apply')){if(failApply){throw Error('Scope preview changed');}undoAvailable=true;}else{undoAvailable=false;}return {};}};
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
'''


class ScopeJSTests(unittest.TestCase):
    def run_js(self,script):
        script="let finished=false;process.on('beforeExit',()=>assert(finished,'Test did not finish'));\n"+script.replace('})().catch','})().then(()=>{finished=true;}).catch')
        result=subprocess.run(['node','-e',DOM_FIXTURE+SETUP+script,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_preview_is_read_only_safe_and_mutations_use_exact_reviewed_revisions(self):
        self.run_js(r'''
(async()=>{
 await context.refreshSourceScope();assert(calls.every(c=>c[0]==='GET'));assert(el('apply').disabled);
 await context.applySourceScope();assert.equal(calls.filter(c=>c[0]==='POST').length,0);
 el('entry').value='1';await context.previewSourceScope();assert(!el('apply').disabled);
 assert(text(el('review')).includes('<img onerror=evil>'));assert.equal(el('review').querySelectorAll('img').length,0);
 await context.applySourceScope();const applied=calls.find(c=>c[1].endsWith('/apply'));
 assert.equal(applied[2].expected_revision,'a'.repeat(64));assert.equal(applied[2].start_entry,1);assert(el('apply').disabled);assert(!el('undo').disabled);
 await context.undoSourceScope();const undone=calls.find(c=>c[0]==='POST'&&c[1].endsWith('/undo'));
 assert.equal(undone[2].expected_revision,'b'.repeat(64));assert(el('undo').disabled);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_stale_reply_cannot_enable_apply_and_conflict_stays_visible(self):
        self.run_js(r'''
(async()=>{
 await context.refreshSourceScope();el('entry').value='1';releasePreview={};
 const pending=context.previewSourceScope();await new Promise(setImmediate);
 context.invalidateSourceScopePreview();releasePreview.resolve(plan);await pending;assert(el('apply').disabled);
 releasePreview={};const oldBook=context.previewSourceScope();await new Promise(setImmediate);
 context._existingUploadSelectionRequest={};context.resetSourceScopeBook();releasePreview.resolve(plan);await oldBook;
 assert(el('apply').disabled);assert.equal(el('entry').value,'');
 el('entry').value='1';
 releasePreview=null;await context.previewSourceScope();failApply=true;await context.applySourceScope();
 assert(el('apply').disabled);assert(el('feedback').textContent.includes('Scope preview changed'));
 const large=Array.from({length:121},(_,i)=>({text:'row '+i}));const details=context.renderSourceScopeRows('Entries',large,row=>row.text);
 assert.equal(details.children[1].children.length,50);details.children[2].children[2].onclick();assert.equal(details.children[1].children.length,50);
 details.children[2].children[2].onclick();assert.equal(details.children[1].children.length,21);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
