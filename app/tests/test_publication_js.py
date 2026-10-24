from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE
VALIDATION_SOURCE = Path(__file__).resolve().parents[1]/'static/js/app-publication-validation.js'
VALIDATION_LOADER = "vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);\n"

SOURCE = Path(__file__).resolve().parents[1] / 'static/js/app-publication.js'
SETUP = r'''
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(v){if(v){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
Element.prototype.replaceChildren=function(...nodes){for(const c of [...this.children]){c.remove();}for(const n of nodes){this.appendChild(n);}};
for(const id of ['review','origin','title-choice','author-choice','cover-choice','reviewed','review-required','apply','clear','clear-cover','cover-status','info','image','feedback']){const e=new Element(id==='image'?'img':'div',{id:'m4b-publication-'+id});e.value='';e.checked=false;body.appendChild(e);}
for(const name of ['title','author','narrator','year','description','per-chunk','require-ready']){const e=new Element('input',{id:'m4b-'+name});e.value='';e.checked=false;body.appendChild(e);}
const el=id=>document.getElementById('m4b-publication-'+id),field=name=>document.getElementById('m4b-'+name);
const text=node=>node.textContent+node.children.map(text).join('');
const plan={book_token:'a'.repeat(64),revision:'b'.repeat(64),source_name:'source.txt',binding_status:'current',uploaded_cover:{present:false},candidates:[{id:'c'.repeat(64),origin:{filename:'<img onerror=evil>',sha256:'d'.repeat(64)},fields:{title:['Source title'],author:['Source author']},other_creators:[],warnings:[],cover_status:'available',cover:{preview_data_url:'data:image/png;base64,YWJj'}}]};
let pendingGet=null,pendingPost=null,workspaceBook=plan.book_token,badResolve=false,failPreview=false;const calls=[];
context.window=context;context._existingUploadSelectionRequest={};
context.API={get:async path=>{calls.push(['get',path]);if(pendingGet){return await pendingGet.promise;}if(failPreview&&path==='/api/m4b_publication'){throw Error('fixture offline');}return path==='/api/workspace'?{book_token:workspaceBook}:plan;},post:async(path,req)=>{calls.push(['post',path,req]);const fields={};for(const name of ['title','author']){if(req[name+'_index']!==null){fields[name]=plan.candidates.find(r=>r.id===req.origin_id).fields[name][req[name+'_index']];}}const result={book_token:req.expected_book_token,revision:req.expected_revision,origin_id:req.origin_id,fields,cover_selection:req.select_cover?Object.fromEntries(['expected_book_token','expected_revision','origin_id','reviewed'].map(k=>[k,req[k]])):null};if(badResolve){result.fields.author='Unrequested author';}if(pendingPost){pendingPost.result=result;await pendingPost.promise;}return result;}};
const defer=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const choose=(id,value)=>{el(id).value=value;el(id).fire('change');};
const tick=()=>new Promise(setImmediate);
'''


class PublicationJSTests(unittest.TestCase):
    def run_js(self, script):
        wrapped = "let finished=false;process.on('beforeExit',()=>assert(finished,'Unfinished async test'));\n"
        wrapped += script.replace('})().catch', '})().then(()=>{finished=true;}).catch')
        result = subprocess.run(['node', '-e', DOM_FIXTURE + VALIDATION_LOADER + SETUP + wrapped, str(SOURCE),str(VALIDATION_SOURCE)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_explicit_safe_preview_apply_cover_clear_and_typed_export_payload(self):
        self.run_js(r'''
(async()=>{
 assert(el('apply').disabled);await context.previewM4bPublication();assert.equal(calls.filter(c=>c[0]==='post').length,0);
 assert(text(el('info')).includes('<img onerror=evil>'));assert.equal(el('info').querySelectorAll('img').length,0);
 choose('title-choice','0');choose('author-choice','0');el('cover-choice').checked=true;el('cover-choice').fire('change');
 assert(!el('apply').disabled);await context.applyM4bPublication();assert.equal(field('title').value,'Source title');assert.equal(field('author').value,'Source author');assert(el('apply').disabled);
 let payload=await context.getM4bPublicationPayload();assert.equal(payload.expected_book_token,'a'.repeat(64));assert.equal(payload.source_cover.origin_id,'c'.repeat(64));assert.equal(payload.narrator,null);
 el('cover-choice').checked=false;el('cover-choice').fire('change');payload=await context.getM4bPublicationPayload();assert(!payload.source_cover);assert.equal(field('title').value,'Source title');
 context.clearM4bAppliedSuggestions();payload=await context.getM4bPublicationPayload();assert.equal(payload.title,null);assert(!payload.expected_book_token);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_manual_entered_and_intentionally_empty_fields_win_and_book_reset_keeps_manual_edits(self):
        self.run_js(r'''
(async()=>{
 field('title').value='Manual title';field('title').fire('input');field('author').value='';field('author').fire('input');
 await context.previewM4bPublication();assert(el('title-choice').disabled);assert(el('author-choice').disabled);
 el('cover-choice').checked=true;el('cover-choice').fire('change');await context.applyM4bPublication();
 const req=calls.find(c=>c[0]==='post')[2];assert.equal(req.title_index,null);assert.equal(req.author_index,null);
 let payload=await context.getM4bPublicationPayload();assert.equal(payload.title,'Manual title');assert.equal(payload.author,'');
 context.resetM4bPublicationBook();payload=await context.getM4bPublicationPayload();assert.equal(payload.title,'Manual title');assert.equal(payload.author,'');assert(!payload.source_cover);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_late_book_preview_and_resolve_after_user_edit_or_cover_change_cannot_apply(self):
        self.run_js(r'''
(async()=>{
 pendingGet=defer();const old=context.previewM4bPublication();await tick();context._existingUploadSelectionRequest={};context.resetM4bPublicationBook();pendingGet.resolve(plan);await old;pendingGet=null;assert(el('apply').disabled);
 await context.previewM4bPublication();choose('title-choice','0');pendingPost=defer();const apply=context.applyM4bPublication();await tick();field('title').value='New manual title';field('title').fire('input');pendingPost.resolve();await apply;pendingPost=null;
 assert.equal(field('title').value,'New manual title');assert(el('feedback').textContent.includes('changed'));
 el('cover-choice').checked=true;el('cover-choice').fire('change');pendingPost=defer();const cover=context.applyM4bPublication();await tick();context.invalidateM4bPublicationCover();pendingPost.resolve();await cover;pendingPost=null;
 const payload=await context.getM4bPublicationPayload();assert(!payload.source_cover);assert.equal(payload.title,'New manual title');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_stale_review_uploaded_precedence_and_untrusted_preview_image(self):
        self.run_js(r'''
(async()=>{
 plan.binding_status='stale';await context.previewM4bPublication();choose('title-choice','0');assert(el('apply').disabled);el('reviewed').checked=true;el('reviewed').fire('change');assert(!el('apply').disabled);
 await context.applyM4bPublication();workspaceBook='e'.repeat(64);await assert.rejects(context.getM4bPublicationPayload(),/book changed/);
 context.resetM4bPublicationBook();plan.uploaded_cover.present=true;plan.candidates[0].cover.preview_data_url='https://example.invalid/evil.svg';await context.previewM4bPublication();assert(el('cover-choice').disabled);assert(el('image').hidden);assert.equal(el('image').getAttribute('src'),null);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_incomplete_resolve_and_export_options_changed_during_book_read_refuse(self):
        self.run_js(r'''
(async()=>{
 await context.previewM4bPublication();choose('title-choice','0');badResolve=true;await context.applyM4bPublication();assert.equal(field('title').value,'');assert(el('feedback').textContent.includes('incomplete'));
 badResolve=false;await context.applyM4bPublication();pendingGet=defer();const exporting=context.getM4bPublicationPayload();await tick();field('per-chunk').checked=true;pendingGet.resolve({book_token:workspaceBook});await assert.rejects(exporting,/options changed/);pendingGet=null;
 field('author').value='Manual author';field('author').fire('input');context.resetM4bPublicationBook();assert.equal(field('title').value,'');assert.equal(field('author').value,'Manual author');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_readonly_refresh_preserves_accepted_cover_until_its_revision_changes(self):
        self.run_js(r'''
(async()=>{
 await context.previewM4bPublication();el('cover-choice').checked=true;el('cover-choice').fire('change');await context.applyM4bPublication();
 await context.previewM4bPublication();assert(el('cover-choice').checked);let payload=await context.getM4bPublicationPayload();assert(payload.source_cover);
 failPreview=true;await context.previewM4bPublication();payload=await context.getM4bPublicationPayload();assert(payload.source_cover);assert(el('feedback').textContent.includes('earlier reading'));
 failPreview=false;plan.revision='f'.repeat(64);await context.previewM4bPublication();payload=await context.getM4bPublicationPayload();assert(!payload.source_cover);assert(!el('cover-choice').checked);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_actual_export_handler_sends_absent_empty_and_source_choices_with_book_fencing(self):
        self.run_js(r'''
(async()=>{
 const status=new Element('div',{id:'m4b-status'});body.appendChild(status);
 let claimed=false,polls=0;const exported=[];
 context.claimTaskStart=()=>{if(claimed){return false;}claimed=true;return true;};context.releaseTaskStart=()=>{claimed=false;};
 context.pollExport=()=>{polls++;claimed=false;};context.escapeHtml=String;context.getActionErrorMessage=(prefix,error)=>prefix+': '+error.message;
 const post=context.API.post;context.API.post=async(path,payload)=>{if(path==='/api/merge_m4b'){exported.push(payload);return {};}return post(path,payload);};
 const core=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');
 vm.runInContext(core.match(/window\.exportM4B = async \(\) => \{[\s\S]*?\n        \};/)[0],context);
 await context.exportM4B();assert.equal(exported[0].title,null);context.clearM4bTitle();await context.exportM4B();assert.equal(exported[1].title,'');
 await context.previewM4bPublication();assert(el('title-choice').disabled);el('cover-choice').checked=true;el('cover-choice').fire('change');await context.applyM4bPublication();
 pendingGet=defer();const first=context.exportM4B();await tick();await context.exportM4B();assert(claimed);assert.equal(exported.length,2);
 pendingGet.resolve({book_token:'e'.repeat(64)});await first;pendingGet=null;assert(!claimed);assert(status.innerHTML.includes('book changed'));assert.equal(exported.length,2);
 await context.exportM4B();assert.equal(exported.length,3);assert.equal(exported[2].title,'');assert.equal(exported[2].source_cover.origin_id,'c'.repeat(64));assert.equal(exported[2].expected_book_token,'a'.repeat(64));assert.equal(polls,3);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
