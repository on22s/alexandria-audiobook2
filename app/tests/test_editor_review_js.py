"""Behavioral review UI: real identity races, original controls, and live drafts."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent/'static'
SETUP = r'''
Object.defineProperty(Element.prototype,'classList',{get(){const node=this;return {contains:value=>node.className.split(/\s+/).includes(value),add(value){if(!this.contains(value)){node.className+=' '+value;}},remove(value){node.className=node.className.split(/\s+/).filter(item=>item!==value).join(' ');}};}});
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
Element.prototype.querySelector=function(s){return this.querySelectorAll(s)[0]||null;};
Element.prototype.append=function(...children){for(const child of children){this.appendChild(child);}};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}this.textContent='';this.append(...children);};
Element.prototype.scrollIntoView=function(){this.scrolled=true;};
Element.prototype.focus=function(){document.activeElement=this;this.fire('focusin');};
function el(id,tag='div',parent=body){return parent.appendChild(new Element(tag,{id}));}
const html=fs.readFileSync(process.argv[2],'utf8');
for(const match of html.matchAll(/id="(editor-(?:quality|line)[^"]*)"/g)){el(match[1]);}
const inspector=body.appendChild(new Element('aside',{class:'editor-line-inspector'}));context.getComputedStyle=()=>({position:'sticky'});
const tbody=el('chunks-table-body','tbody'),rows=new Map(),fields=new Map(),plays=[],generates=[],scrolls=[],errors=[],toasts=[];let stops=0,expands=0;
document.querySelector=s=>s.startsWith('#chunks-table-body ')?tbody.querySelector(s.slice(19)):body.querySelector(s);
document.querySelectorAll=s=>s.startsWith('#chunks-table-body ')?tbody.querySelectorAll(s.slice(19)):body.querySelectorAll(s);
context.currentBookFilename='Book A';context.chunkSnapshotBook='Book A';context.chunkSnapshotBookToken='b'.repeat(64);context.chunkSnapshotRevision='a'.repeat(64);
function makeRow(chunk){
 const row=tbody.appendChild(new Element('tr',{'data-id':String(chunk.id),'data-uid':chunk.uid,'data-book-token':context.chunkSnapshotBookToken,class:'chunk-row'}));rows.set(chunk.uid,row);
 const controls={};for(const [key,cls,tag] of [['text','chunk-text','textarea'],['instruct','chunk-instruct','textarea'],['pause_after','chunk-pause-after','input'],['speaker','chunk-speaker','select']]){const field=row.appendChild(new Element(tag,{class:cls}));field.value=String(chunk[key]??'');controls[key]=field;}
 fields.set(chunk.uid,controls);row.appendChild(new Element('button',{class:'chunk-toggle-btn'}));
 if(chunk.audio_path){const audio=row.appendChild(new Element('audio',{class:'chunk-audio',src:'/synthetic.wav','data-id':String(chunk.id)}));audio.play=async()=>{plays.push(audio);audio.fire('play');};audio.pause=()=>{};}
 return row;
}
context.cachedChunks=[{id:0,uid:'u0',speaker:'UNKNOWN',text:'<img onerror=bad()>',instruct:'softly',pause_after:200,status:'error',error:'synthetic refusal'},{id:1,uid:'u1',speaker:'Hero',text:'Saved words',instruct:'',status:'done',audio_path:'one.wav',source_entry_indices:[5,7]}];context.cachedChunks.forEach(makeRow);
context.scrollToChunkRow=id=>{scrolls.push(id);const c=context.cachedChunks.find(c=>c.id===id);if(c){rows.get(c.uid).style.display='';}};
context.toggleChunkExpand=button=>{expands++;button.closest('tr').classList.add('expanded');};
context.stopSequence=()=>{stops++;};context.generateChunk=async id=>{generates.push(id);};
context.showToast=(...args)=>toasts.push(args);context.showActionError=(...args)=>errors.push(args);
context.API={get:async()=>facts()};
context.isEditorEditPending=()=>false;
const core=fs.readFileSync(process.argv[1].replace('app-editor-review.js','app-core.js'),'utf8');
vm.runInContext(core.slice(core.indexOf('        function getEditorFieldValue('),core.indexOf('        window.suggestChunkGap =')),context);
function facts(){return {book_token:context.chunkSnapshotBookToken,revision:context.chunkSnapshotRevision,rows:context.cachedChunks.map((c,i)=>({id:c.id,uid:c.uid,issues:i===0?['unknown','failed']:['drift'],audio:c.status==='done'?'present':'not_rendered',drift:c.status==='done'?'flagged':'not_rendered'}))};}
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);document.getElementById('editor-quality-filter').value='all';
const run=s=>vm.runInContext(s,context),tick=()=>new Promise(resolve=>setImmediate(resolve));
let completed=false;process.on('beforeExit',()=>assert(completed,'scenario must finish its awaited assertions'));
(async()=>{
'''


class EditorReviewJsTests(unittest.TestCase):
    def test_outlier_input_error_keeps_unrelated_findings_visible(self):
        self.run_js(r'''
const savedFacts=facts;context.API.get=async()=>({...savedFacts(),outlier_error:'Cannot read voice_config.json; restore or repair it before checking.'});
await context.refreshEditorReview();assert(document.getElementById('editor-quality-status').textContent.includes('restore or repair'));
assert(document.getElementById('editor-quality-list').children.length>0);assert(document.getElementById('editor-quality-status').textContent.includes('voice drift'));
''')

    def test_outlier_reload_restores_cancel_and_refreshes_once_on_matching_book(self):
        self.run_js(r'''
el('btn-outlier-check','button');el('btn-outlier-cancel','button');el('editor-outlier-status');
let poll,loads=0;const posts=[];context._startPolling=(key,fetch,options)=>{poll=options;};context.loadChunks=async()=>{loads++;};context.API.post=async(url,payload)=>{posts.push({url,payload});};
const book=context.chunkSnapshotBookToken;context.attachEditorOutlierCheck({book_token:book,running:true});
assert(document.getElementById('btn-outlier-check').disabled);assert(!document.getElementById('btn-outlier-cancel').disabled);
await context.cancelOutlierCheck();assert.equal(posts[0].payload.expected_book_token,book);
poll.onTick({book_token:book,progress:{done:3,total:8}});assert(document.getElementById('editor-outlier-status').textContent.includes('3 / 8'));
await poll.onDone({book_token:book,running:false,logs:['Done, no certification.']});assert.equal(loads,1);assert(document.getElementById('btn-outlier-cancel').disabled);
context.attachEditorOutlierCheck({book_token:book,running:true});context.chunkSnapshotBookToken='c'.repeat(64);await poll.onDone({book_token:book,running:false,logs:['old book']});assert.equal(loads,1);
''')

    def test_outlier_start_explains_pending_edit_and_missing_book_refusals(self):
        self.run_js(r'''
el('btn-outlier-check','button');el('btn-outlier-cancel','button');el('editor-outlier-status');let posts=0;context.API.post=async()=>{posts++;};context.isEditorEditPending=()=>true;
await context.runOutlierCheck();assert.equal(posts,0);assert(document.getElementById('editor-outlier-status').textContent.includes('pending edits'));
context.isEditorEditPending=()=>false;context.chunkSnapshotBookToken=null;await context.runOutlierCheck();assert.equal(posts,0);assert(document.getElementById('editor-outlier-status').textContent.includes('Load the selected book'));
''')

    def test_outlier_findings_details_and_book_bound_start_cancel(self):
        self.run_js(r'''
el('btn-outlier-check','button');el('btn-outlier-cancel','button');el('editor-outlier-status');
const savedFacts=facts;context.API.get=async()=>{const data=savedFacts();data.rows[1].issues=['outliers'];data.rows[1].outlier={status:'flagged',metrics:{median_pitch_hz:{value:220,z:4,flagged:true},voiced_ratio:null},labels:{median_pitch_hz:'Approximate pitch'},error:'One metric unavailable'};return data;};
await context.refreshEditorReview();document.getElementById('editor-quality-filter').value='outliers';context.renderEditorQualityInbox();assert.equal(document.getElementById('editor-quality-list').children.length,1);
context.selectEditorReviewLineById(1);assert(document.getElementById('editor-line-outlier').textContent.includes('220.00'));assert(document.getElementById('editor-line-outlier').textContent.includes('unavailable'));assert.equal(generates.length,0);
const posts=[];let poll;context.API.post=async(url,payload)=>{posts.push({url,payload});return {status:'started'};};context._startPolling=(key,fetch,options)=>{poll=options;};context.loadChunks=async()=>{};
await context.runOutlierCheck();assert.equal(posts[0].url,'/api/chunks/outlier_check');assert.equal(posts[0].payload.expected_book_token,context.chunkSnapshotBookToken);assert(document.getElementById('btn-outlier-check').disabled);
await context.runOutlierCheck();assert.equal(posts.length,1);await context.cancelOutlierCheck();assert.equal(posts[1].url,'/api/chunks/outlier_check/cancel');assert.equal(posts[1].payload.expected_book_token,context.chunkSnapshotBookToken);
await poll.onDone({running:false,status:'cancelled',logs:['Cancelled; no results saved.']});assert(!document.getElementById('btn-outlier-check').disabled);assert(document.getElementById('btn-outlier-cancel').disabled);assert.equal(generates.length,0);
''')

    def test_outlier_start_refusal_releases_controls_and_reports_uncertainty(self):
        self.run_js(r'''
el('btn-outlier-check','button');el('btn-outlier-cancel','button');el('editor-outlier-status');context.API.post=async()=>{throw new Error('refused');};await context.runOutlierCheck();assert(!document.getElementById('btn-outlier-check').disabled);assert(document.getElementById('btn-outlier-cancel').disabled);assert.equal(errors.length,1);assert(document.getElementById('editor-outlier-status').textContent.includes('unconfirmed'));
''')

    def test_queue_marks_advanced_snapshot_stale_and_explains_refused_click(self):
        self.run_js(r'''
await context.refreshEditorReview();const list=document.getElementById('editor-quality-list');
const action=list.children[0].querySelectorAll('button')[1];
context.chunkSnapshotRevision='d'.repeat(64);context.renderEditorQualityInbox();
assert(document.getElementById('editor-quality-binding').textContent.includes('Stale'));
assert(list.querySelectorAll('button').every(button=>button.disabled));action.fire('click');
assert(toasts.some(args=>args[0].includes('snapshot changed')));assert.equal(run('editorReviewSelection'),null);
await context.refreshEditorReview();assert(!list.children[0].querySelectorAll('button')[1].disabled);
context.isEditorEditPending=()=>true;context.renderEditorQualityInbox();assert(list.querySelectorAll('button').every(button=>button.disabled));
''')

    def test_all_lines_view_exposes_unrendered_and_no_candidate_without_fabricated_findings(self):
        self.run_js(r'''
context.API.get=async()=>{const data=facts();data.rows.forEach(row=>{row.issues=[];row.speech='no_content_candidate';});return data;};
await context.refreshEditorReview();document.getElementById('editor-quality-filter').value='all_lines';context.renderEditorQualityInbox(true);
assert.equal(document.getElementById('editor-quality-list').children.length,2);
assert(document.getElementById('editor-quality-status').textContent.includes('0 with saved findings'));
assert.equal(generates.length,0);
''')

    def test_review_queue_routes_to_read_only_candidates_and_rejects_old_revision(self):
        self.run_js(r'''
el('editor-take-details');const snapshots=[];context.loadEditorTakeSnapshots=(...args)=>snapshots.push(args);
await context.refreshEditorReview();const list=document.getElementById('editor-quality-list');
const first=list.children[0],action=first.querySelectorAll('button')[1];
assert(first.children[2].textContent.includes('Line UID u0'));assert(first.children[2].textContent.includes('No current speech check'));
action.fire('click');assert.equal(run('editorReviewSelection.uid'),'u0');assert.deepEqual(snapshots,[[false,0]]);
assert.equal(document.getElementById('editor-take-details').open,true);assert.equal(generates.length,0);
fields.get('u0').text.value='Unsaved draft';action.fire('click');assert.equal(snapshots.length,1);
fields.get('u0').text.value=context.cachedChunks[0].text;context.isEditorEditPending=()=>true;
action.fire('click');assert.equal(snapshots.length,1);context.isEditorEditPending=()=>false;
context.chunkSnapshotRevision='c'.repeat(64);await context.refreshEditorReview();
action.fire('click');assert.equal(snapshots.length,1);
list.children[0].querySelectorAll('button')[1].fire('click');assert.equal(snapshots.length,2);
context.currentBookFilename='another';list.children[0].querySelectorAll('button')[1].fire('click');assert.equal(snapshots.length,2);
''')

    def test_queue_missing_evidence_is_not_a_clean_take(self):
        self.run_js(r'''
context.API.get=async()=>{const data=facts();data.rows[1].issues=[];data.rows[1].speech='not_checked';return data;};
await context.refreshEditorReview();document.getElementById('editor-quality-filter').value='speech_not_checked';context.renderEditorQualityInbox(true);
const list=document.getElementById('editor-quality-list');assert.equal(list.children.length,1);
assert(list.children[0].children[1].textContent.includes('No current speech evidence'));
assert.equal(generates.length,0);assert.equal(plays.length,0);
context.markEditorReviewStale();assert(list.querySelectorAll('button').every(button=>button.disabled));
''')

    def test_recorded_input_is_read_only_and_rejects_stale_identity(self):
        self.run_js(r'''
const chunk=context.cachedChunks[1];chunk.audio_revision='take';chunk.recorded_input_state='current';chunk.recorded_pronunciation_state='not_recorded';
chunk.speech_verification={book_token:context.chunkSnapshotBookToken,uid:chunk.uid,audio_revision:'take',input_digest:'inputs',expected_sha256:'exact',audio:{sha256:'audio'},language:'English',input_provenance:'leaf'};
context.API.post=()=>{throw Error('No post allowed');};
await context.refreshEditorReview();context.selectEditorReviewLineById(1);
const status=document.getElementById('editor-line-recorded-status'),binding=document.getElementById('editor-line-recorded-binding');
assert(status.textContent.includes('Exact input recorded'));assert(binding.textContent.includes('inputs'));assert(binding.textContent.includes('engine settings are unavailable'));
context.isEditorEditPending=()=>true;context.renderEditorLineInspector();assert(status.textContent.includes('stale'));assert.equal(binding.textContent,'');
context.isEditorEditPending=()=>false;context.renderEditorLineInspector();assert(binding.textContent.includes('inputs'));
fields.get('u1').text.value='Draft text';context.renderEditorLineInspector();assert(!binding.textContent.includes('Draft text'));assert.equal(chunk.text,'Saved words');
chunk.speech_verification.uid='other';context.renderEditorLineInspector();assert(status.textContent.includes('does not match'));assert.equal(binding.textContent,'');
chunk.speech_verification.uid=chunk.uid;context.chunkSnapshotRevision='new';context.renderEditorLineInspector();assert(status.textContent.includes('stale'));assert.equal(binding.textContent,'');
assert.equal(generates.length,0);assert.equal(plays.length,0);
''')

    def test_recorded_input_missing_invalid_and_changed_settings_remain_explicit(self):
        self.run_js(r'''
await context.refreshEditorReview();context.selectEditorReviewLineById(1);const chunk=context.cachedChunks[1];
const status=document.getElementById('editor-line-recorded-status');
for(const [state,reason,text] of [['not_recorded',null,'No exact input'],['invalid','invalid','malformed'],['stale','inputs_changed','pronunciation rules'],['stale','other_take','another take'],['historical','changed','historical']]){
chunk.recorded_input_state=state;chunk.recorded_input_reason=reason;
if(state==='historical'){chunk.audio_revision='take';chunk.speech_verification_historical={uid:chunk.uid,book_token:context.chunkSnapshotBookToken,audio_revision:'take'};}
context.renderEditorLineInspector();assert(status.textContent.includes(text),status.textContent);}
delete chunk.recorded_input_state;context.renderEditorLineInspector();assert(status.textContent.includes('Reload'));
delete chunk.audio_path;context.renderEditorLineInspector();assert(status.textContent.includes('No selected audio'));
''')

    def test_recorded_occurrence_decisions_explain_once_without_editing_source(self):
        self.run_js(r'''
const chunk=context.cachedChunks[1];chunk.audio_revision='take';chunk.recorded_input_state='not_recorded';chunk.recorded_pronunciation_state='recorded';
chunk.pronunciation_receipt={audio_revision:'take',context:{speaker:'Hero'},text_sha256:'source',prepared_sha256:'prepared',speech_sha256:'speech',decisions:[{id:'<img onerror=bad()>',applied:true,reason:'applied_respelling'},{id:'duplicate',applied:false,reason:'duplicate_rule_id'}]};
const before=JSON.stringify(chunk);await context.refreshEditorReview();context.selectEditorReviewLineById(1);
const details=document.getElementById('editor-line-recorded-pronunciation');assert(details.textContent.includes('applied once'));assert(details.textContent.includes('not applied'));assert(details.textContent.includes('duplicate_rule_id'));assert.equal(details.querySelectorAll('img').length,0);assert.equal(JSON.stringify(chunk),before);
for(const state of ['other_take','changed_text','changed_context','invalid']){chunk.recorded_pronunciation_state=state;context.renderEditorLineInspector();assert(!details.textContent.includes('applied once'));assert(details.textContent.includes('unavailable'));}
assert.equal(document.getElementById('editor-line-text').textContent,'Saved words');
''')

    def test_recorded_input_retains_diagnostics_without_audio_and_labels_view_revision(self):
        self.run_js(r'''
const chunk=context.cachedChunks[1];chunk.audio_revision='take';chunk.recorded_input_state='stale';chunk.recorded_input_reason='inputs_changed';delete chunk.audio_path;
await context.refreshEditorReview();context.selectEditorReviewLineById(1);
const status=document.getElementById('editor-line-recorded-status');assert(status.textContent.includes('No selected audio file'));assert(status.textContent.includes('pronunciation rules'));
chunk.recorded_input_state='historical';chunk.recorded_input_reason='changed';
chunk.speech_verification_historical={uid:chunk.uid,book_token:context.chunkSnapshotBookToken,audio_revision:'take',input_digest:'saved-digest',expected_sha256:'saved-text'};
context.renderEditorLineInspector();const binding=document.getElementById('editor-line-recorded-binding');assert(binding.textContent.includes('saved-digest'));assert(binding.textContent.includes('not a recorded capture revision'));assert(binding.textContent.includes(context.chunkSnapshotRevision));
chunk.recorded_input_state='toString';chunk.recorded_input_reason='constructor';context.renderEditorLineInspector();assert(status.textContent.includes('Reload'));assert(!status.textContent.includes('function'));
document.getElementById('editor-line-recorded-binding').remove();context.renderEditorLineInspector();assert(document.getElementById('editor-line-quality').textContent.length>0);
''')

    def run_js(self, script):
        result = subprocess.run(['node','-e',DOM_FIXTURE+SETUP+script+"\ncompleted=true;})().catch(error=>{console.error(error);process.exitCode=1;});",str(STATIC/'js/app-editor-review.js'),str(STATIC/'index.html')], capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_input_preview_is_read_only_and_never_replaces_historical_evidence(self):
        self.run_js(r'''
context.isEditorEditPending=()=>false;
context.selectEditorReviewLineById(1);
const posts=[];context.API.post=async(url,payload)=>{posts.push({url,payload});return {
 book_token:payload.expected_book_token,uid:payload.expected_uid,revision:'rules',context:{engine_mode:'local'},context_source:'configured',
 preview:{kind:'current_settings_preview',authored_text:'Saved words',text:'<img onerror=bad()>',transformations:[{type:'example'}],decisions:[]}};};
document.getElementById('editor-line-speech-expected').textContent='Historical exact input';
await context.previewEditorSpeechInput();
assert.equal(posts.length,1);assert.equal(posts[0].url,'/api/pronunciation/occurrences');
assert.deepEqual(posts[0].payload,{expected_book_token:context.chunkSnapshotBookToken,expected_uid:'u1',index:1});assert.equal(generates.length,0);
assert.equal(document.getElementById('editor-line-speech-expected').textContent,'Historical exact input');
assert(document.getElementById('editor-line-input-status').textContent.includes('not the input recorded'));
assert(document.getElementById('editor-line-input-details').textContent.includes('<img onerror=bad()>'));
const details=JSON.parse(document.getElementById('editor-line-input-details').textContent);
assert.deepEqual(details.transformations,[{type:'example'}]);assert.deepEqual(details.context,{engine_mode:'local'});
assert.equal(details.settings_source,'configured');assert.deepEqual(details.pronunciation_decisions,[]);
context.renderEditorLineInspector();assert.equal(document.getElementById('editor-line-input-preview').disabled,false);
assert(document.getElementById('editor-line-input-details').textContent.includes('<img onerror=bad()>'));
assert.equal(body.querySelectorAll('img').length,0);
context.isEditorEditPending=()=>true;await context.previewEditorSpeechInput();assert.equal(posts.length,1);
''')

    def test_input_preview_discards_late_selection_response_and_changed_saved_text(self):
        self.run_js(r'''
context.isEditorEditPending=()=>false;context.selectEditorReviewLineById(1);
let resolve;context.API.post=()=>new Promise(done=>{resolve=done;});
const pending=context.previewEditorSpeechInput();
context.selectEditorReviewLineById(0);
resolve({book_token:context.chunkSnapshotBookToken,uid:'u1',preview:{kind:'current_settings_preview',authored_text:'Saved words',text:'late'}});
await pending;assert.equal(document.getElementById('editor-line-input-details').textContent,'');
context.selectEditorReviewLineById(1);
context.API.post=async()=>({book_token:context.chunkSnapshotBookToken,uid:'u1',preview:{kind:'current_settings_preview',authored_text:'Changed elsewhere',text:'new'}});
await context.previewEditorSpeechInput();assert(document.getElementById('editor-line-input-status').textContent.includes('unavailable'));
assert.equal(document.getElementById('editor-line-input-details').textContent,'');
''')

    def test_input_preview_discards_book_revision_changes_and_reports_failure(self):
        self.run_js(r'''
context.isEditorEditPending=()=>false;
for(const change of [()=>{context.chunkSnapshotRevision='changed';},()=>{context.currentBookFilename='another';}]){
 context.currentBookFilename=context.chunkSnapshotBook;context.chunkSnapshotRevision='before';context.selectEditorReviewLineById(1);
 let resolve;context.API.post=()=>new Promise(done=>{resolve=done;});
 const pending=context.previewEditorSpeechInput();change();
 resolve({book_token:context.chunkSnapshotBookToken,uid:'u1',preview:{kind:'current_settings_preview',authored_text:'Saved words',text:'late'}});
 await pending;assert.equal(document.getElementById('editor-line-input-details').textContent,'');
 assert(!document.getElementById('editor-line-input-status').textContent.includes('Reading saved'));
}
context.currentBookFilename=context.chunkSnapshotBook;context.selectEditorReviewLineById(1);
context.API.post=async()=>{throw new Error('Saved rules need repair');};
await context.previewEditorSpeechInput();assert(document.getElementById('editor-line-input-status').textContent.includes('Saved rules need repair'));
assert.equal(generates.length,0);
''')

    def test_input_preview_normalizes_dirty_checks_and_bounds_inflight_reads(self):
        self.run_js(r'''
const chunk=context.cachedChunks[1];chunk.text='Saved\r\nwords';fields.get('u1').text.value='Saved\nwords';
context.selectEditorReviewLineById(1);
const button=document.getElementById('editor-line-input-preview');assert.equal(button.disabled,false);
let resolve,posts=0;context.API.post=()=>{posts++;return new Promise(done=>{resolve=done;});};
const pending=context.previewEditorSpeechInput();assert.equal(button.disabled,true);
await context.previewEditorSpeechInput();assert.equal(posts,1);
context.isEditorEditPending=()=>true;
resolve({book_token:context.chunkSnapshotBookToken,uid:'u1',preview:{kind:'current_settings_preview',authored_text:chunk.text,text:'prepared'}});
await pending;assert.equal(button.disabled,true);
assert(!document.getElementById('editor-line-input-status').textContent.includes('Reading saved'));
context.isEditorEditPending=()=>false;context.renderEditorLineInspector();assert.equal(button.disabled,false);
fields.get('u1').text.value='An unsaved edit';context.renderEditorLineInspector();assert.equal(button.disabled,true);
await context.previewEditorSpeechInput();assert.equal(posts,1);
chunk.text='An unsaved edit';context.chunkSnapshotRevision=null;context.renderEditorLineInspector();assert.equal(button.disabled,false);
''')

    def test_filters_counts_text_injection_and_inspector_use_saved_facts(self):
        self.run_js(r'''
await context.refreshEditorReview();const list=document.getElementById('editor-quality-list');assert.equal(list.children.length,2);
assert(document.getElementById('editor-quality-status').textContent.includes('1 unknown speaker'));
assert.equal(body.querySelectorAll('img').length,0);
list.children[0].children[0].fire('click');assert.equal(document.getElementById('editor-line-text').textContent,'<img onerror=bad()>');assert.equal(document.getElementById('editor-line-error').textContent,'synthetic refusal');assert.equal(generates.length,0);
document.getElementById('editor-quality-filter').value='missing';context.renderEditorQualityInbox(true);assert.equal(list.children.length,0);assert(list.textContent.includes('does not establish'));
context.selectEditorReviewLineById(1);assert.equal(document.getElementById('editor-line-source').textContent,'Saved mapping: 6, 8');assert(document.getElementById('editor-line-quality').textContent.includes('Voice drift flagged'));
''')

    def test_speech_evidence_separates_selected_take_and_rejected_attempt_without_html(self):
        self.run_js(r'''
const chunk=context.cachedChunks[1];chunk.audio_revision='selected';
chunk.speech_verification={book_token:context.chunkSnapshotBookToken,uid:chunk.uid,audio_revision:'selected',status:'no_content_candidate',expected_text:'<img onerror=bad()>',seconds:2.5,primary:{expected_words:10,observed_words:10,unmatched_expected:0,unmatched_observed:0,differences:[]}};
chunk.speech_verification_failure={book_token:context.chunkSnapshotBookToken,uid:chunk.uid,audio_revision:'rejected',status:'corroborated_content_difference',expected_text:'Actual rejected input',reason:'missing tail',primary:{expected_words:10,observed_words:3,unmatched_expected:7,unmatched_observed:0,differences:[{kind:'delete',expected:'<script>bad()</script>',observed:'',ambiguity:[]}],diagnostics_abbreviated:true,difference_count:12}};
context.API.get=async()=>{const data=facts();data.rows[1]={...data.rows[1],speech:'no_content_candidate',speech_attempt:'rejected',issues:['speech_rejected']};return data;};
await context.refreshEditorReview();context.selectEditorReviewLineById(1);
assert(document.getElementById('editor-line-speech').textContent.includes('listening review still needed'));
assert(document.getElementById('editor-line-speech-attempt').textContent.includes('prior take was kept'));
assert(document.getElementById('editor-line-speech-expected').textContent.includes('<img onerror=bad()>'));
const evidence=document.getElementById('editor-line-speech-evidence').textContent;
assert(evidence.includes('Selected take'));assert(evidence.includes('Rejected attempt; this is not the selected take'));assert(evidence.includes('Showing 1 of 12 differences'));
assert.equal(body.querySelectorAll('img').length,0);assert.equal(body.querySelectorAll('script').length,0);
document.getElementById('editor-quality-filter').value='speech_rejected';context.renderEditorQualityInbox(true);assert.equal(document.getElementById('editor-quality-list').children.length,1);
context.markEditorReviewStale();assert.equal(document.getElementById('editor-line-speech-evidence').textContent,'');assert(document.getElementById('editor-line-speech').textContent.includes('stale'));
''')

    def test_historical_receipt_is_labelled_and_never_presented_as_current(self):
        self.run_js(r'''
const chunk=context.cachedChunks[1];chunk.audio_revision='selected';
chunk.speech_verification_historical={book_token:context.chunkSnapshotBookToken,uid:chunk.uid,audio_revision:'selected',status:'no_content_candidate',expected_text:'Historical exact input',audio:{sha256:'e'.repeat(64)}};chunk.speech_verification_historical_reason='legacy';
context.API.get=async()=>{const data=facts();data.rows[1]={...data.rows[1],speech:'not_checked'};return data;};
await context.refreshEditorReview();context.selectEditorReviewLineById(1);
assert(document.getElementById('editor-line-speech').textContent.includes('Historical speech evidence'));
assert(document.getElementById('editor-line-speech-expected').textContent.includes('Historical exact input'));
assert(document.getElementById('editor-line-speech-evidence').textContent.includes('Historical saved take (legacy)'));
assert(!document.getElementById('editor-line-speech-evidence').textContent.includes('Selected take'));
''')

    def test_historical_reason_copy_and_recheck_availability_are_distinct(self):
        self.run_js(r'''
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-speech-verification.js'),'utf8'),context);
context.getChunkAudioIdentity=chunk=>chunk.audio_revision;context.isEditorEditPending=()=>false;
const chunk=context.cachedChunks[1];chunk.audio_revision='selected';chunk.audio_path='one.wav';
chunk.speech_verification_historical={book_token:context.chunkSnapshotBookToken,uid:chunk.uid,audio_revision:'selected',status:'no_content_candidate',expected_text:'Historical input',audio:{sha256:'e'.repeat(64)}};
context.API.get=async()=>{const data=facts();data.rows[1].speech='not_checked';return data;};
await context.refreshEditorReview();context.selectEditorReviewLineById(1);
for(const [reason,text,disabled] of [['changed','no longer matches its recorded filesystem identity',false],['platform','cannot verify audio byte freshness',true],['unavailable','currently unavailable or unreadable',true],['unknown','freshness could not be classified',true]]){
 chunk.speech_verification_historical_reason=reason;context.renderEditorLineInspector();
 assert(document.getElementById('editor-line-speech').textContent.includes(text));
 context.window.applySpeechVerificationInspector();
 assert.equal(document.getElementById('editor-line-speech-recheck').disabled,disabled);
}
chunk.speech_verification_historical_reason='legacy';context.renderEditorLineInspector();context.window.applySpeechVerificationInspector();
assert.equal(document.getElementById('editor-line-speech-recheck').disabled,false);
''')

    def test_mismatched_selected_take_receipt_cannot_show_details(self):
        self.run_js(r'''
const chunk=context.cachedChunks[1];chunk.audio_revision='new';chunk.speech_verification={book_token:context.chunkSnapshotBookToken,uid:chunk.uid,audio_revision:'old',status:'review',expected_text:'Stale API text'};
context.API.get=async()=>{const data=facts();data.rows[1].speech='review';return data;};await context.refreshEditorReview();context.selectEditorReviewLineById(1);
assert.equal(document.getElementById('editor-line-speech-evidence').textContent,'');assert(!document.getElementById('editor-line-speech-expected').textContent.includes('Stale API text'));
''')

    def test_speech_recheck_saves_original_identity_and_ignores_late_selection_reply(self):
        self.run_js(r'''
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-speech-verification.js'),'utf8'),context);
context.getChunkAudioIdentity=chunk=>chunk.audio_revision;context.isEditorEditPending=()=>false;context.ensureEditorRenderSnapshot=async()=>{};
const chunk=context.cachedChunks[1];chunk.audio_revision='selected';chunk.speech_verification={book_token:context.chunkSnapshotBookToken,uid:chunk.uid,audio_revision:'selected',status:'no_content_candidate',expected_text:'Actual input',input_digest:'d'.repeat(64),audio:{sha256:'e'.repeat(64)}};
context.API.get=async()=>{const data=facts();data.rows[1].speech='no_content_candidate';return data;};await context.refreshEditorReview();context.selectEditorReviewLineById(1);
let finish,bodySent,calls=0;context.API.post=(url,body)=>{calls++;bodySent=body;return new Promise(resolve=>finish=resolve);};
const pending=context.recheckEditorSpeech();await tick();assert.equal(calls,1);assert.equal(bodySent.expected_uid,'u1');assert.equal(bodySent.expected_audio_revision,'selected');assert.equal(bodySent.expected_book_token,context.chunkSnapshotBookToken);assert.equal(document.getElementById('editor-line-speech-cancel').disabled,false);
await context.recheckEditorSpeech();assert.equal(calls,1);context.selectEditorReviewLineById(0);finish({book_token:context.chunkSnapshotBookToken,uid:'u1',receipt:{audio_revision:'selected'}});await pending;
assert.equal(run('speechVerificationBusy'),false);assert.equal(document.getElementById('editor-line-speech-cancel').disabled,true);assert(document.getElementById('editor-line-speech-status').textContent.includes('different selection'));assert(!document.getElementById('editor-line-speech-evidence').textContent.includes('Actual input'));
''')

    def test_historical_recheck_success_is_not_discarded_as_another_selection(self):
        self.run_js(r'''
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-speech-verification.js'),'utf8'),context);
context.getChunkAudioIdentity=chunk=>chunk.audio_revision;context.isEditorEditPending=()=>false;
const chunk=context.cachedChunks[1];chunk.audio_revision='selected';chunk.audio_path='one.wav';
chunk.speech_verification_historical={book_token:context.chunkSnapshotBookToken,uid:chunk.uid,audio_revision:'selected',status:'no_content_candidate',expected_text:'Historical input',input_digest:'d'.repeat(64),audio:{sha256:'e'.repeat(64)}};
chunk.speech_verification_historical_reason='legacy';delete chunk.speech_verification;
let serverRevision=context.chunkSnapshotRevision,refreshes=0;
context.ensureEditorRenderSnapshot=async()=>[context.cachedChunks[0],context.cachedChunks[1]];
context.ensureChunkRefresh=async()=>{refreshes++;context.cachedChunks[1]={...chunk,speech_verification:{...chunk.speech_verification_historical,status:'review'}};delete context.cachedChunks[1].speech_verification_historical;serverRevision='c'.repeat(64);context.chunkSnapshotRevision=serverRevision;return context.cachedChunks;};
context.API.get=async()=>{const data=facts();data.revision=serverRevision;data.rows[1].speech=context.cachedChunks[1].speech_verification?.status||'not_checked';return data;};
context.API.post=async()=>({book_token:context.chunkSnapshotBookToken,uid:chunk.uid,receipt:{audio_revision:'selected'}});
await context.refreshEditorReview();context.selectEditorReviewLineById(1);
assert.equal(document.getElementById('editor-line-speech-recheck').disabled,false);
await context.recheckEditorSpeech();
assert.equal(refreshes,1);assert.equal(run('editorReviewFresh'),true);
assert(document.getElementById('editor-line-speech-status').textContent.includes('recorded'));
assert(!document.getElementById('editor-line-speech-status').textContent.includes('different selection'));
''')

    def test_speech_recheck_requires_proven_input_and_cancel_uses_owned_endpoint(self):
        self.run_js(r'''
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-speech-verification.js'),'utf8'),context);
context.getChunkAudioIdentity=chunk=>chunk.audio_revision;context.isEditorEditPending=()=>false;context.ensureEditorRenderSnapshot=async()=>{};
await context.refreshEditorReview();context.selectEditorReviewLineById(1);let calls=[];context.API.post=async(url,body)=>{calls.push([url,body]);};
await context.recheckEditorSpeech();assert.equal(calls.length,0);assert(toasts.at(-1)[0].includes('recorded synthesis input'));
context.applySpeechVerificationTaskActivity(true);assert.equal(document.getElementById('editor-line-speech-cancel').disabled,false);await context.cancelEditorSpeechVerification();assert.equal(calls[0][0],'/api/speech_verification/cancel');context.applySpeechVerificationTaskActivity(false);assert.equal(document.getElementById('editor-line-speech-cancel').disabled,true);
''')

    def test_successful_same_selection_recheck_installs_snapshot_before_refreshing_facts(self):
        self.run_js(r'''
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-speech-verification.js'),'utf8'),context);
context.getChunkAudioIdentity=chunk=>chunk.audio_revision;context.isEditorEditPending=()=>false;
const chunk=context.cachedChunks[1];chunk.audio_revision='selected';chunk.speech_verification={book_token:context.chunkSnapshotBookToken,uid:chunk.uid,audio_revision:'selected',status:'no_content_candidate',expected_text:'Actual input',input_digest:'d'.repeat(64),audio:{sha256:'e'.repeat(64)}};
const changed={...chunk,speech_verification:{...chunk.speech_verification,status:'review'}};
let serverRevision=context.chunkSnapshotRevision,serverChunk=chunk,refreshes=0;
context.ensureEditorRenderSnapshot=async()=>[context.cachedChunks[0],serverChunk]; // The actual barrier returns rows, without installing cached rows/revision.
context.ensureChunkRefresh=async()=>{refreshes++;context.cachedChunks[1]=serverChunk;context.chunkSnapshotRevision=serverRevision;return context.cachedChunks;};
context.API.get=async()=>{const data=facts();data.revision=serverRevision;data.rows[1].speech=serverChunk.speech_verification.status;return data;};
await context.refreshEditorReview();context.selectEditorReviewLineById(1);
context.API.post=async()=>{serverChunk=changed;serverRevision='c'.repeat(64);return {book_token:context.chunkSnapshotBookToken,uid:'u1',receipt:{audio_revision:'selected'}};};
await context.recheckEditorSpeech();assert.equal(refreshes,1);assert.equal(run('editorReviewFresh'),true);assert.equal(context.cachedChunks[1].speech_verification.status,'review');assert.equal(context.chunkSnapshotRevision,serverRevision);assert(document.getElementById('editor-line-speech').textContent.includes('listening review'));assert(document.getElementById('editor-line-speech-status').textContent.includes('recorded'));
''')

    def test_incomplete_recheck_does_not_hide_completed_content_difference(self):
        self.run_js(r'''
const chunk=context.cachedChunks[1];chunk.audio_revision='selected';chunk.speech_verification={book_token:context.chunkSnapshotBookToken,uid:chunk.uid,audio_revision:'selected',status:'corroborated_content_difference',expected_text:'Original API text'};
chunk.speech_verification_recheck={...chunk.speech_verification,status:'unavailable',reason:'cached model missing'};
context.API.get=async()=>{const data=facts();data.rows[1]={...data.rows[1],speech:'corroborated_content_difference',speech_recheck:'unavailable',issues:['speech_difference','speech_unavailable']};return data;};
await context.refreshEditorReview();context.selectEditorReviewLineById(1);assert(document.getElementById('editor-line-speech').textContent.includes('confirmed material'));assert(document.getElementById('editor-line-speech-attempt').textContent.includes('evidence is retained'));assert(document.getElementById('editor-line-speech-evidence').textContent.includes('cached model missing'));assert(document.getElementById('editor-line-speech-evidence').textContent.includes('Latest incomplete recheck'));
''')

    def test_serialized_latest_request_wins_and_late_book_reply_cannot_paint(self):
        self.run_js(r'''
const gates=[];let calls=0;context.API.get=()=>{calls++;return new Promise(resolve=>gates.push(resolve));};
const first=context.refreshEditorReview();await tick();const old=facts();const second=context.refreshEditorReview();const third=context.refreshEditorReview();assert.equal(calls,1);
context.chunkSnapshotBookToken='c'.repeat(64);context.currentBookFilename='Book B';context.chunkSnapshotBook='Book B';context.resetEditorReview();gates.shift()(old);await Promise.all([first,second,third]);assert.equal(calls,1);assert.equal(run('editorReviewFresh'),false);assert.equal(document.getElementById('editor-quality-list').children.length,0);
context.chunkSnapshotBookToken='b'.repeat(64);context.currentBookFilename='Book A';context.chunkSnapshotBook='Book A';const a=context.refreshEditorReview();await tick();const b=context.refreshEditorReview();const c=context.refreshEditorReview();gates.shift()(facts());await tick();assert.equal(calls,3);assert.equal(gates.length,1);gates.shift()(facts());await Promise.all([a,b,c]);assert.equal(run('editorReviewFresh'),true);
''')

    def test_replaced_uid_clears_selection_and_old_finding_cannot_target_replacement(self):
        self.run_js(r'''
await context.refreshEditorReview();const old=document.getElementById('editor-quality-list').children[0].children[0];old.fire('click');assert.equal(run('editorReviewSelection.uid'),'u0');
context.cachedChunks[0]={...context.cachedChunks[0],uid:'replacement'};rows.get('u0').setAttribute('data-uid','replacement');context.renderEditorLineInspector();assert.equal(run('editorReviewSelection'),null);
old.fire('click');assert.equal(run('editorReviewSelection'),null);assert.equal(generates.length,0);assert(toasts.at(-1)[0].includes('line changed'));
''')

    def test_reorder_resolves_current_uid_and_drafts_focus_original_controls(self):
        self.run_js(r'''
await context.refreshEditorReview();context.selectEditorReviewLineById(0);const controls=fields.get('u0');controls.text.value='unsaved exact words';controls.text.fire('input');assert.equal(document.getElementById('editor-line-text').textContent,'unsaved exact words');assert(document.getElementById('editor-line-state').textContent.includes('Unsaved'));
context.cachedChunks[0]={...context.cachedChunks[0],id:7};rows.get('u0').setAttribute('data-id','7');rows.get('u0').style.display='none';context.focusEditorReviewField('pause_after');assert.equal(scrolls.at(-1),7);assert.equal(document.activeElement,controls.pause_after);assert.equal(expands,1);assert.equal(controls.text.value,'unsaved exact words');assert.equal(generates.length,0);
context.focusEditorReviewField('__proto__');assert.equal(expands,1);
''')

    def test_play_and_generate_reuse_existing_audio_and_original_handlers(self):
        self.run_js(r'''
await context.refreshEditorReview();context.selectEditorReviewLineById(1);const audio=rows.get('u1').querySelector('.chunk-audio');await context.playEditorReviewLine();assert.equal(stops,1);assert.equal(plays[0],audio);assert.equal(body.querySelectorAll('audio').length,1);assert.equal(run('editorReviewSelection.uid'),'u1');await context.generateEditorReviewLine();assert.deepEqual(generates,[1]);
audio.play=async()=>{throw Error('decode failed');};await context.playEditorReviewLine();assert.equal(errors.length,1);
context.chunkSnapshotBookToken='c'.repeat(64);await context.generateEditorReviewLine();assert.equal(generates.length,1);
''')

    def test_pending_audition_stops_if_the_book_or_audio_element_is_replaced(self):
        self.run_js(r'''
await context.refreshEditorReview();context.selectEditorReviewLineById(1);const audio=rows.get('u1').querySelector('.chunk-audio');let release,paused=0;audio.play=()=>new Promise(resolve=>release=resolve);audio.pause=()=>paused++;const playing=context.playEditorReviewLine();await tick();context.chunkSnapshotBookToken='c'.repeat(64);release();await playing;assert.equal(paused,1);
''')

    def test_malformed_or_changed_facts_and_failures_disable_old_links(self):
        self.run_js(r'''
await context.refreshEditorReview();const old=document.getElementById('editor-quality-list').children[0].children[0];
for(const mutate of [d=>d.rows.pop(),d=>d.rows[0].issues.push('__proto__'),d=>d.rows[1].uid='u0',d=>d.revision='old',d=>d.rows[1].audio='certified']){
 context.API.get=async()=>{const d=facts();mutate(d);return d;};await context.refreshEditorReview();assert.equal(run('editorReviewFresh'),false);assert(old.disabled);old.fire('click');assert.equal(run('editorReviewSelection'),null);
}
context.API.get=async()=>{throw Error('network unavailable');};await context.refreshEditorReview();assert(document.getElementById('editor-quality-status').textContent.includes('earlier reading'));
''')

    def test_unchanged_findings_retain_focus_and_disclosure_and_publish_no_drafts(self):
        self.run_js(r'''
await context.refreshEditorReview();const button=document.getElementById('editor-quality-list').children[0].children[0];button.focus();document.getElementById('editor-line-details').open=false;
await context.refreshEditorReview();assert.equal(document.getElementById('editor-quality-list').children[0].children[0],button);assert.equal(document.activeElement,button);assert.equal(document.getElementById('editor-line-details').open,false);
fields.get('u1').text.focus();assert.equal(run('editorReviewSelection.uid'),'u1');context.markEditorReviewStale();assert.equal(button.disabled,true);assert.equal(generates.length,0);assert(document.getElementById('editor-line-source').textContent.includes('Last loaded mapping'));
''')

    def test_stacked_mobile_selection_reveals_inspector_but_field_focus_stays_in_table(self):
        self.run_js(r'''
context.getComputedStyle=()=>({position:'static'});await context.refreshEditorReview();context.selectEditorReviewLineById(0);assert.equal(inspector.scrolled,true);inspector.scrolled=false;context.focusEditorReviewField('text');assert.equal(document.activeElement,fields.get('u0').text);assert.equal(inspector.scrolled,false);assert.equal(fields.get('u0').text.scrolled,true);
''')

    def test_pagination_counts_every_finding_and_preserves_all_rows(self):
        self.run_js(r'''
context.cachedChunks=Array.from({length:123},(_,i)=>({id:i,uid:'page'+i,speaker:'UNKNOWN',status:'pending',text:'line'}));context.API.get=async()=>({book_token:context.chunkSnapshotBookToken,revision:context.chunkSnapshotRevision,rows:context.cachedChunks.map(c=>({id:c.id,uid:c.uid,issues:['unknown'],audio:'not_rendered',drift:'not_rendered'}))});
await context.refreshEditorReview();assert.equal(document.getElementById('editor-quality-list').children.length,50);assert(document.getElementById('editor-quality-status').textContent.includes('123 lines'));assert.equal(document.getElementById('editor-quality-more').hidden,false);context.showMoreEditorFindings();assert.equal(document.getElementById('editor-quality-list').children.length,100);context.showMoreEditorFindings();assert.equal(document.getElementById('editor-quality-list').children.length,123);assert.equal(document.getElementById('editor-quality-more').hidden,true);
''')

    def test_markup_preserves_handlers_separates_whole_book_actions_and_loads_before_restore(self):
        class Inventory(HTMLParser):
            def __init__(self): super().__init__(); self.ids={};self.in_details=False
            def handle_starttag(self,tag,attrs):
                a=dict(attrs)
                if tag=='details' and 'editor-whole-book' in a.get('class',''): self.in_details=True
                if a.get('id'): self.ids.setdefault(a['id'],[]).append((a,self.in_details))
            def handle_endtag(self,tag):
                if tag=='details': self.in_details=False
        html=(STATIC/'index.html').read_text();p=Inventory();p.feed(html)
        for key,handler in [('btn-play-seq','playSequence()'),('btn-batch-fast','startRender(false)'),('btn-regen-all','startRender(true)'),('btn-cancel-render','cancelRender()'),('btn-drift-check','runDriftCheck()')]:
            self.assertEqual(1,len(p.ids[key]));self.assertEqual(handler,p.ids[key][0][0]['onclick'])
        self.assertTrue(p.ids['btn-regen-all'][0][1]);self.assertTrue(p.ids['btn-merge'][0][1]);self.assertFalse(p.ids['btn-play-seq'][0][1])
        self.assertLess(html.index('app-editor-review.js'),html.index('app-reports.js'))
        core=(STATIC/'js/app-core.js').read_text()
        self.assertIn('window.refreshEditorReview?.();',core);self.assertIn('window.markEditorReviewStale?.();',core);self.assertIn('window.resetEditorReview?.();',core)
        self.assertIn('selectEditorReviewLineById(${chunk.id})',core)
