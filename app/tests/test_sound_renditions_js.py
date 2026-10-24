"""Exercise rendition controls against real cue and asset JavaScript modules."""
from pathlib import Path
import subprocess
import unittest
from tests.test_sound_cues_js import SETUP as CUE_SETUP
from tests.test_ui_help_js import DOM_FIXTURE

SOURCE = Path(__file__).resolve().parents[1] / 'static/js/app-sound-cues.js'
SETUP = CUE_SETUP + r'''
context.TextEncoder=TextEncoder;
Element.prototype.prepend=function(child){child.remove();child.parentElement=this;this.children.unshift(child);};
for(const id of ['sound-renditions-content','sound-renditions-status','sound-renditions-refresh'])body.appendChild(new Element('div',{id}));
vm.runInContext(fs.readFileSync(process.argv[1].replace('app-sound-cues.js','app-sound-renditions.js'),'utf8'),context);
let invalidated=0;context.SoundMixes={invalidate:()=>invalidated++};
const renditions=vm.runInContext('SoundRenditions',context),rbox=document.getElementById('sound-renditions-content');
const rmessage=document.getElementById('sound-renditions-status');
const asset=()=>({...sample(),source:{sha256:'7'.repeat(64),bytes:100},canonical:{...sample().canonical,bytes:192000,conversion:'pcm-v1',codec:'pcm_f32le',format:'wav'}});
const record=()=>({schema:1,rendition_id:'8'.repeat(64),source:{cue_id:'rain',version:version().version,book:book()},composition:{scaffold_sha256:null,arrangement_sha256:null,family_id:null},origin:{kind:'imported_audio',renderer:null},derivation:{kind:'import',parent:null},master:{asset_id:asset().asset_id,source:asset().source,canonical:asset().canonical,offset_frames:0},stems:[]});
const history=()=>({context_revision:data().context_revision,registry_revision:null,ledger_revision:'9'.repeat(64),book_token:data().book_token,book_id:book().book_id,records:[record()],keep:[],draft_selected:[],total_records:1,next_offset:null,approval_checked:false});
const rclick=label=>{const node=rbox.querySelectorAll('button').find(n=>n.textContent===label);assert(node,'missing '+label);return node.onclick();};
const rfield=name=>rbox.querySelectorAll('[data-rendition-field]').find(n=>n.getAttribute('data-rendition-field')===name);
async function rready(value=history(),failAssets=false){const n=requests.length,p=renditions.refresh();requests[n].resolve(value);if(failAssets){requests[n+1].reject(new Error('unavailable'));}else{requests[n+1].resolve({assets:[asset()]});}await p;}
async function form(source=version(),cueView=data()){const n=requests.length,p=renditions.recordFromCue(source,cueView);requests[n].resolve(history());requests[n+1].resolve({assets:[asset()]});await p;}
const ack=(status,extra={})=>({status,context_revision:'a'.repeat(64),ledger_revision:'b'.repeat(64),book_token:data().book_token,refresh_required:true,...extra});
'''


class SoundRenditionsJsTests(unittest.TestCase):
    def run_js(self, body):
        script = DOM_FIXTURE + SETUP + '\nlet done=false;process.on("beforeExit",()=>assert(done));(async()=>{' + body + '\ndone=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_history_survives_unavailable_assets_and_rejects_approval_claims(self):
        self.run_js(r'''
const value=history();value.records[0].composition.family_id='<img onerror=bad()>';
await rready(value,true);assert(rbox.querySelectorAll('p').some(n=>n.textContent.includes('<img onerror=bad()>')));
assert.equal(rbox.querySelectorAll('img').length,0);assert(rmessage.textContent.includes('repair audio'));
for(const bad of [{...history(),approval_checked:true},{...history(),ledger_revision:null},{...history(),total_records:0}]){
await rready(bad);assert.equal(rbox.children.length,0);assert(rmessage.textContent.includes('Invalid'));
}
''')

    def test_record_binds_exact_source_and_media_and_requires_renderer_declaration(self):
        self.run_js(r'''
await form();rfield('origin').value='declared_external_render';rfield('origin').onchange();
rclick('Record immutable rendition');assert.equal(requests.length,2);assert(rmessage.textContent.includes('Complete'));
for(const [name,value] of [['component','Tool'],['revision','v1'],['request','c'.repeat(64)],['result','d'.repeat(64)]]){rfield(name).value=value;}
rclick('Add stem');rfield('stem-1-label').value='wind';rfield('stem-1-offset').value='-120';
const p=rclick('Record immutable rendition');rclick('Record immutable rendition');assert.equal(requests.length,3);
const payload=JSON.parse(requests[2].options.body);assert(requests[2].url.endsWith('/renditions/record'));
assert.equal(payload.source.version,version().version);assert.equal(payload.source.cue_id,'rain');assert(!('book' in payload.source));
assert.equal(payload.master.source_sha256,asset().source.sha256);assert.equal(payload.master.canonical_sha256,asset().canonical.sha256);
assert.equal(payload.stems[0].offset_frames,-120);assert.equal(payload.derivation.kind,'render');assert.equal(payload.derivation.parent,null);
assert.equal(payload.origin.renderer.component_revision,'v1');assert.equal(payload.context_revision,data().context_revision);
assert.equal(payload.ledger_revision,history().ledger_revision);assert.equal(payload.registry_revision,null);
requests[2].resolve(ack('recorded',{rendition_id:record().rendition_id}));await tick();
requests[3].resolve(history());requests[4].resolve({assets:[asset()]});await p;assert.equal(invalidated,1);
assert(rmessage.textContent.includes('review the full mix'));
''')

    def test_cleanup_protection_stays_locked_after_busy_state_and_selection_change(self):
        self.run_js(r'''
await rready();const check=rbox.querySelectorAll('[data-rendition-cleanup]')[0];check.checked=true;check.onchange();
const p=rclick('Preview selected cleanup'),impact={...history(),impact_revision:'c'.repeat(64),rows:[{rendition_id:record().rendition_id,protected:true,reasons:['kept']}],removable_ids:[]};
requests[2].resolve(impact);await p;const remove=rbox.querySelectorAll('button').find(n=>n.textContent==='Delete reviewed record metadata');
assert(remove.disabled);assert.equal(remove.getAttribute('data-locked'),'true');
check.checked=false;check.onchange();assert.equal(document.getElementById('sound-renditions-cleanup').children.length,0);
assert.equal(requests.length,3);
''')

    def test_cleanup_commits_only_exact_preview_and_does_not_delete_assets(self):
        self.run_js(r'''
await rready();const check=rbox.querySelectorAll('[data-rendition-cleanup]')[0];check.checked=true;check.onchange();
const p=rclick('Preview selected cleanup');requests[2].resolve({...history(),impact_revision:'c'.repeat(64),rows:[{rendition_id:record().rendition_id,protected:false,reasons:[]}],removable_ids:[record().rendition_id]});await p;
const commit=rclick('Delete reviewed record metadata'),payload=JSON.parse(requests[3].options.body);
assert.equal(payload.impact_revision,'c'.repeat(64));assert.deepEqual(payload.rendition_ids,[record().rendition_id]);
requests[3].resolve(ack('removed',{removed_ids:[record().rendition_id]}));await tick();
requests[4].resolve({...history(),records:[],total_records:0});requests[5].resolve({assets:[asset()]});await commit;
assert(!requests.some(r=>r.options.method==='DELETE'||r.url.includes('purge')));assert.equal(invalidated,1);
''')

    def test_late_source_bridge_cannot_replace_newer_form_and_stale_book_refuses_form(self):
        self.run_js(r'''
const first=renditions.recordFromCue(version('old'),data()),second=renditions.recordFromCue(version('new'),data());
requests[2].resolve(history());requests[3].resolve({assets:[asset()]});await second;
requests[0].resolve(history());requests[1].resolve({assets:[asset()]});await first;
assert(rbox.querySelectorAll('*').filter(n=>n.tagName==='H5').some(n=>n.textContent.includes('new')));assert(!rbox.querySelectorAll('*').filter(n=>n.tagName==='H5').some(n=>n.textContent.includes('old')));
await form(version(),{...data(),book_token:'f'.repeat(64)});assert(!rfield('master'));assert(rmessage.textContent.includes('changed'));
// Model native queued details toggles and the HTML ontoggle refresh. Opening
// through the bridge must survive the extra refresh started by that handler.
const panel=new Element('details');body.appendChild(panel);panel.appendChild(rbox);let opened=false;
Object.defineProperty(panel,'open',{get(){return opened;},set(value){opened=value;setImmediate(()=>{if(opened){renditions.refresh();}panel.fire('toggle');});}});
const n=requests.length,openedForm=renditions.recordFromCue(version('opened'),data());await tick();
assert.equal(requests.length,n+4);requests[n+2].resolve(history());requests[n+3].resolve({assets:[asset()]});await openedForm;
requests[n].resolve(history());requests[n+1].resolve({assets:[asset()]});await tick();
assert(rbox.querySelectorAll('*').some(n=>n.tagName==='H5'&&n.textContent.includes('opened')));
panel.open=false;await tick();const count=requests.length;
const closing=renditions.recordFromCue(version('closed'),data());panel.open=false;await tick();await closing;
assert.equal(requests.length,count);
await tick();const before=requests.length,show=renditions.showHistory('rain');await tick();assert(panel.open);
requests[before+2].resolve(history());requests[before+3].resolve({assets:[asset()]});await show;
requests[before].resolve(history());requests[before+1].resolve({assets:[asset()]});await tick();
assert(requests[before+2].url.includes('cue_id=rain'));assert(!rfield('master'));
''')

    def test_use_master_binds_current_selected_parent_and_server_anchor(self):
        self.run_js(r'''
const current=data(),old=version(),parent={...version(),version:'6'.repeat(64),parent_version:old.version};
current.registry.versions=[old,parent];current.registry.selected={rain:parent.version};
const obsolete=api.useRendition({...record(),rendition_id:'f'.repeat(64)},history());
const p=api.useRendition(record(),history());requests[2].resolve(current);requests[3].resolve({assets:[asset()]});await p;
requests[0].resolve(current);requests[1].resolve({assets:[asset()]});await obsolete;
requests.splice(0,2);
assert(field('name').readOnly);assert.equal(field('asset').children.length,1);
const save=click('Save and select revision');requests[2].resolve(anchor());await tick();
const payload=JSON.parse(requests[3].options.body);assert(requests[3].url.endsWith('/renditions/consume'));
assert.equal(payload.parent_version,parent.version);assert.equal(payload.rendition_id,record().rendition_id);
assert.equal(payload.anchor_revision,anchor().anchor_revision);assert.equal(payload.ledger_revision,history().ledger_revision);
assert(!('asset_id' in payload));assert(!('selection' in payload));assert(!('book' in payload));
requests[3].resolve({...ack('saved'),ledger_revision:history().ledger_revision,registry_revision:'7'.repeat(64),cue_id:'rain',rendition_id:record().rendition_id,version:'5'.repeat(64)});await tick();
requests[4].resolve(current);requests[5].resolve({assets:[asset()]});await tick();
requests[6].resolve(history());requests[7].resolve({assets:[asset()]});await save;assert.equal(invalidated,1);
assert(message.textContent.includes('Review the full mix'));
''')
