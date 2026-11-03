"""Exact server anchors and drafts survive asynchronous occurrence editing."""
import unittest
from tests import test_pronunciation_panel_js as fixtures


SETUP = r'''
for(const action of ['load','find','homographs','preview','save','candidates','rules','text','status']){body.appendChild(new Element('div',{id:'pronunciation-occurrence-'+action}));}
context.crypto={randomUUID:()=> 'rule-'+(posts.length+1)};
const anchor={uid:'u0',coordinate_space:'pre_lexicon_v1',offset_unit:'unicode_codepoint',text_sha256:'d'.repeat(64),prepared_sha256:'e'.repeat(64),start:10,end:13,original:'Ann'};
let lineSaved={book_token:'b'.repeat(64),uid:'u0',index:0,revision:'1'.repeat(64),rules:[],context:{speaker:'Hero',voice_sha256:'f'.repeat(64),language:'English',engine_mode:'local'},context_source:'configured',preview:{text:'𠀀𠀁𠀂𠀃 Shared\r\nShared.',decisions:[]}};
const sharedPost=context.API.post;
context.API.post=async(url,data)=>{
 if(!url.includes('/occurrences')){return sharedPost(url,data);}
 posts.push({url,data:JSON.parse(JSON.stringify(data))});
 if(url.endsWith('/save')){lineSaved={...lineSaved,rules:data.rules,revision:'2'.repeat(64),audio_detached:false};}
 if(url.endsWith('/candidates')){return {...lineSaved,candidates:[{anchor,before:'𠀀𠀁𠀂𠀃 Ann\r\n<img onerror=x>',after:'<script>x</script>'}],truncated:false};}
 return JSON.parse(JSON.stringify(lineSaved));
};
await context.loadPronunciationPanel();await context.previewPronunciationPanel();document.getElementById('pronunciation-line').value='0';
document.getElementById('pronunciation-audition-original').value='Ann';document.getElementById('pronunciation-audition-spoken').value='Ah-n';
const panel=()=>document.getElementById('pronunciation-occurrence-rules'),candidates=()=>document.getElementById('pronunciation-occurrence-candidates');
const add=async()=>{await context.findPronunciationOccurrences();candidates().querySelectorAll('button')[0].fire('click');};
'''


class OccurrencePronunciationJsTests(unittest.TestCase):
    def run_js(self, code):
        fixtures.PronunciationPanelJsTests.run_js(self, SETUP + code)

    def test_homograph_meaning_creates_only_explicit_draft_and_escapes_context(self):
        self.run_js(r'''
await context.loadOccurrencePronunciation();
const actual=context.API.post;const meaning='metal — The pipe contains lead.';let options;
context.API.post=async(url,data,config)=>{options=config;posts.push({url,data});return {...lineSaved,analysis_status:'completed',word_list:['lead','read','wind'],candidate_limit:64,truncated:false,candidates:[{anchor:{...anchor,original:'lead',end:14},before:'<img onerror=x>',after:'<script>x</script>',meanings:[meaning,'guide — I lead the team.']}]};};
await context.findPronunciationHomographs();
assert.equal(posts.at(-1).url,'/api/pronunciation/occurrences/homographs');assert.equal(posts.at(-1).data.index,0);assert.equal(posts.at(-1).data.revision,'1'.repeat(64));assert.equal(options.timeoutMs,30000);assert(options.timeoutMessage.includes('Homograph discovery'));
assert.equal(candidates().querySelectorAll('img').length,0);assert.equal(candidates().querySelectorAll('script').length,0);
assert.equal(document.getElementById('pronunciation-audition-spoken').value,'Ah-n');candidates().querySelector('button').fire('click');assert.equal(run('occurrencePronunciationRules.length'),0);
const input=candidates().querySelector('input');assert.equal(input.value,'');input.value='led';const count=posts.length;candidates().querySelector('button').fire('click');
assert.equal(posts.length,count);assert.equal(run('occurrencePronunciationRules[0].state'),'draft');assert.equal(run('occurrencePronunciationRules[0].note'),'User-selected meaning: '+meaning);assert.equal(run('occurrencePronunciationRules[0].replacement'),'led');
input.value='leed';candidates().querySelectorAll('button')[1].fire('click');assert.equal(run('occurrencePronunciationRules.length'),1);assert.equal(run('occurrencePronunciationRules[0].replacement'),'led');assert(toasts.at(-1)[0].includes('already has'));
context.API.post=actual;await context.saveOccurrencePronunciation();assert.equal(posts.at(-1).data.rules[0].state,'draft');assert.equal(errors.length,0);
''')

    def test_homograph_states_busy_and_late_results(self):
        self.run_js(r'''
await context.loadOccurrencePronunciation();let release,calls=0;
context.API.post=()=>{calls++;return new Promise(resolve=>release=resolve);};const pending=context.findPronunciationHomographs();await tick();
assert.equal(document.getElementById('pronunciation-occurrence-homographs').disabled,true);await context.findPronunciationHomographs();assert.equal(calls,1);
context.onOccurrenceQueryEdit();release({...lineSaved,analysis_status:'completed',word_list:['lead'],candidate_limit:64,candidates:[{anchor,before:'late',after:''}],truncated:false});await pending;assert.equal(candidates().children.length,0);
for(const state of ['unsupported_language','unavailable','completed','bogus']){context.API.post=async()=>({...lineSaved,analysis_status:state,word_list:['lead'],candidate_limit:64,candidates:[],truncated:false});await context.findPronunciationHomographs();const text=document.getElementById('pronunciation-occurrence-status').textContent;assert(text.includes(state==='unsupported_language'?'English only':state==='unavailable'?'unavailable':state==='bogus'?'unknown analysis':'0 possible'));}
context.API.post=async()=>({...lineSaved,context_source:'unknown',analysis_status:'unavailable',word_list:['lead'],candidate_limit:64,candidates:[]});await context.findPronunciationHomographs();assert(document.getElementById('pronunciation-occurrence-status').textContent.includes('active engine'));
context.API.post=async()=>({...lineSaved,analysis_status:'completed',word_list:['lead'],candidate_limit:2,truncated:true,candidates:[{anchor,before:'',after:'',meanings:[]}]});await context.findPronunciationHomographs();assert.equal(candidates().querySelectorAll('button').length,1);assert(document.getElementById('pronunciation-occurrence-status').textContent.includes('First 2 matches'));
context.API.post=async()=>{throw new Error('409 stale rules');};await context.findPronunciationHomographs();assert.equal(candidates().children.length,0);assert.equal(errors.length,1);assert(document.getElementById('pronunciation-occurrence-status').textContent.includes('failed'));assert.equal(document.getElementById('pronunciation-occurrence-homographs').disabled,false);
''')

    def test_server_anchor_roundtrips_unicode_crlf_and_literal_snippets_without_approval(self):
        self.run_js(r'''
await context.loadOccurrencePronunciation();await add();
assert.equal(body.querySelectorAll('img,script').length,0);assert.equal(candidates().querySelectorAll('mark')[0].textContent,'Ann');
assert.equal(run('occurrencePronunciationRules[0].state'),'draft');await context.saveOccurrencePronunciation();
const payload=posts.at(-1).data,rule=payload.rules[0];for(const [key,value] of Object.entries(anchor)){assert.equal(rule[key],value,key);}
assert.equal(rule.start,10);assert.equal(rule.replacement,'Ah-n');assert.equal(rule.state,'draft');assert.equal(run('occurrencePronunciationDirty'),false);assert.equal(errors.length,0);
''')

    def test_review_fallback_restrictions_and_note_preserve_unedited_raw_text(self):
        self.run_js(r'''
lineSaved.rules=[{...anchor,id:'existing',state:'draft',replacement:'Ah\r\nN'}];await context.loadOccurrencePronunciation();
const card=panel().children[0],state=card.querySelector('select'),fields=card.querySelectorAll('textarea');
// Native textarea newline normalization must not silently edit loaded values.
fields[0].value='Ah\nN';state.value='approved';state.fire('change');fields[2].value='Reviewed';fields[2].fire('input');
await context.saveOccurrencePronunciation();assert.equal(posts.at(-1).data.rules[0].replacement,'Ah\r\nN');
const next=panel().children[0],texts=next.querySelectorAll('textarea');texts[0].value='Fallback';texts[0].fire('input');texts[1].value='/æn/';texts[1].fire('input');
const checks=next.querySelectorAll('input');for(const check of checks){check.checked=true;check.fire('change');}
await context.saveOccurrencePronunciation();const rule=posts.at(-1).data.rules[0];assert.equal(rule.phonetic_hint,'/æn/');assert.equal(rule.fallback,'Fallback');assert(!Object.hasOwn(rule,'replacement'));assert.equal(rule.note,'Reviewed');assert.equal(rule.restrictions.voice_sha256,'f'.repeat(64));assert.equal(rule.restrictions.speaker,'Hero');
assert.equal(errors.length,0);
''')

    def test_late_query_and_preview_cannot_publish_after_edits(self):
        self.run_js(r'''
await context.loadOccurrencePronunciation();const actual=context.API.post;let release;
context.API.post=()=>new Promise(resolve=>release=resolve);const pending=context.findPronunciationOccurrences();await tick();context.onOccurrenceQueryEdit();release({...lineSaved,candidates:[{anchor,before:'late',after:''}]});await pending;assert.equal(candidates().children.length,0);
context.API.post=actual;await add();context.API.post=()=>new Promise(resolve=>release=resolve);const preview=context.previewOccurrencePronunciation();await tick();panel().querySelector('textarea').value='Newer';panel().querySelector('textarea').fire('input');release({...lineSaved,preview:{text:'stale',decisions:[]}});await preview;assert.equal(document.getElementById('pronunciation-occurrence-text').hidden,true);assert.equal(run('occurrencePronunciationRules[0].replacement'),'Newer');
''')

    def test_reselected_restriction_displays_the_value_that_will_be_saved(self):
        self.run_js(r'''
lineSaved.rules=[{...anchor,id:'existing',state:'approved',replacement:'Ah-n',restrictions:{language:'German'}}];await context.loadOccurrencePronunciation();
const check=panel().querySelectorAll('input')[2],label=check.parentElement.querySelector('span');assert(label.textContent.includes('German'));
check.checked=false;check.fire('change');check.checked=true;check.fire('change');assert(label.textContent.includes('English'));assert(!label.textContent.includes('German'));
await context.saveOccurrencePronunciation();assert.equal(posts.at(-1).data.rules[0].restrictions.language,'English');
delete lineSaved.context.language;lineSaved.rules[0].restrictions.language='German';await context.loadOccurrencePronunciation();
const unavailable=panel().querySelectorAll('input')[2];assert.equal(unavailable.disabled,false);unavailable.checked=false;unavailable.fire('change');assert.equal(unavailable.checked,false);assert.equal(unavailable.disabled,true);
await context.saveOccurrencePronunciation();assert(!Object.hasOwn(posts.at(-1).data.rules[0].restrictions,'language'));
''')

    def test_late_save_advances_revision_without_overwriting_newer_draft_and_failures_keep_it(self):
        self.run_js(r'''
await context.loadOccurrencePronunciation();await add();const actual=context.API.post;let release,payload;
context.API.post=(url,data)=>{payload=JSON.parse(JSON.stringify(data));return new Promise(resolve=>release=resolve);};const saving=context.saveOccurrencePronunciation();await tick();
panel().querySelector('textarea').value='Newer';panel().querySelector('textarea').fire('input');release({...lineSaved,rules:payload.rules,revision:'2'.repeat(64)});await saving;
assert.equal(run('occurrencePronunciationRules[0].replacement'),'Newer');assert.equal(run('occurrencePronunciationDirty'),true);assert.equal(run('occurrencePronunciationState.revision'),'2'.repeat(64));
context.API.post=async()=>{throw new Error('409: conflicting editor');};await context.saveOccurrencePronunciation();assert.equal(run('occurrencePronunciationRules[0].replacement'),'Newer');assert.equal(run('occurrencePronunciationDirty'),true);assert.equal(errors.length,1);
context.API.post=actual;await context.saveOccurrencePronunciation();assert.equal(posts.at(-1).data.revision,'2'.repeat(64));assert.equal(posts.at(-1).data.rules[0].replacement,'Newer');
''')

    def test_book_and_line_switch_keep_draft_and_do_not_attach_old_save_preview(self):
        self.run_js(r'''
await context.loadOccurrencePronunciation();await add();let release,payload;
context.API.post=(url,data)=>{payload=JSON.parse(JSON.stringify(data));return new Promise(resolve=>release=resolve);};const saving=context.saveOccurrencePronunciation();await tick();
context.currentBookFilename='B';context.resetPronunciationBook();release({...lineSaved,rules:payload.rules,revision:'2'.repeat(64),preview:{text:'old book',decisions:[]}});await saving;
assert.equal(run('occurrencePronunciationRules.length'),1);assert.equal(run('occurrencePronunciationDirty'),true);assert.equal(document.getElementById('pronunciation-occurrence-text').hidden,true);assert.equal(document.getElementById('pronunciation-occurrence-save').disabled,true);
// Returning to the original selection retains the new revision and the draft.
context.currentBookFilename='A';run("pronunciationBook=getPronunciationBook(); pronunciationLines=[{id:0,uid:'u0',speaker:'Hero'}]");context.onPronunciationLineEdit();assert.equal(document.getElementById('pronunciation-occurrence-save').disabled,false);assert.equal(run('occurrencePronunciationState.revision'),'2'.repeat(64));
context.showConfirm=async()=>false;await context.loadOccurrencePronunciation();assert.equal(run('occurrencePronunciationRules.length'),1);
''')
