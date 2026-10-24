"""Exercise real cue UI requests and DOM controls with deterministic races."""
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-sound-cues.js'
SETUP = r'''
context.window=context;
Element.prototype.replaceChildren=function(...nodes){for(const child of [...this.children])child.remove();this.textContent='';for(const node of nodes)this.appendChild(node);};
Element.prototype.append=function(...nodes){for(const node of nodes)this.appendChild(node);};
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
for(const id of ['sound-cues-content','sound-cues-status','sound-cues-refresh'])body.appendChild(new Element('div',{id}));
const requests=[];context.fetch=(url,options={})=>new Promise((resolve,reject)=>requests.push({url,options,resolve:value=>resolve({json:async()=>value}),reject}));
context.API={_handleError:async()=>{}};
vm.runInContext(fs.readFileSync(process.argv[1].replace('app-sound-cues.js','app-sound-assets.js'),'utf8'),context);
const sample=()=>({asset_id:'a'.repeat(32),state:'ready',display_name:'Rain <img onerror=bad()>.wav',canonical:{sha256:'b'.repeat(64),frames:48000,frame_rate:24000,channels:1,sample_width:4}});
const selection=()=>({chunk_uid:'line-1',generation_sha256:'c'.repeat(64),audio_sha256:'d'.repeat(64),audio_revision:null});
const book=()=>({book_id:'synthetic book',source_sha256:'e'.repeat(64),script_sha256:'f'.repeat(64)});
const version=(id='rain')=>({schema:1,cue_id:id,parent_version:null,version:'1'.repeat(64),recipe:{book:book(),selection:selection(),asset:{asset_id:sample().asset_id,...sample().canonical},frame_rate:24000,boundary:'start',offset_frames:0,kind:'effect',duration:{mode:'native',frames:null,loop_crossfade_frames:null},mix:{gain_db:0,duck_db:0,fade_in_frames:0,fade_out_frames:0}}});
const data=()=>({context_revision:'2'.repeat(64),registry_revision:null,book_token:'3'.repeat(64),book:book(),registry:{versions:[],selected:{}},chunks:[{chunk_uid:'line-1',speaker:'NARRATOR',text:'Synthetic <img onerror=bad()> dialogue.',has_recorded_take:true}],total_chunks:1,next_offset:null});
const anchor=()=>({context_revision:'2'.repeat(64),anchor_revision:'4'.repeat(64),selection:selection()});
const tick=()=>new Promise(resolve=>setImmediate(resolve));load();
const api=vm.runInContext('SoundCues',context),box=document.getElementById('sound-cues-content'),message=document.getElementById('sound-cues-status');
const click=label=>{const node=box.querySelectorAll('button').find(n=>n.textContent===label);assert(node,'missing '+label);return node.onclick();};
const field=name=>box.querySelectorAll('[data-cue-field]').find(n=>n.getAttribute('data-cue-field')===name);
async function ready(value=data()){const n=requests.length,p=api.refresh();requests[n].resolve(value);requests[n+1].resolve({assets:[sample()]});await p;}
'''


class SoundCuesJsTests(unittest.TestCase):
    def run_js(self, body):
        script = DOM_FIXTURE + SETUP + '\nlet done=false;process.on("beforeExit",()=>assert(done));(async()=>{' + body + '\ndone=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_latest_refresh_wins_and_untrusted_labels_remain_literal(self):
        self.run_js(r'''
const old=api.refresh(),fresh=api.refresh();requests[2].resolve(data());requests[3].resolve({assets:[sample()]});await fresh;
requests[0].resolve({...data(),book:{...book(),book_id:'old book'}});requests[1].resolve({assets:[]});await old;
assert(box.querySelectorAll('p').some(n=>n.textContent==='Book: synthetic book'));click('New cue');
assert.equal(field('dialogue').children[0].textContent,'NARRATOR: Synthetic <img onerror=bad()> dialogue.');
assert.equal(box.querySelectorAll('img').length,0);
''')

    def test_save_uses_server_anchor_and_frozen_context_and_suppresses_duplicate_click(self):
        self.run_js(r'''
await ready();click('New cue');field('name').value='Rain';field('offset').value='0.29';field('duck').value='-6';
const pending=click('Save and select revision');click('Save and select revision');assert.equal(requests.length,3);
assert(requests[2].url.includes('chunk_uid=line-1'));assert(field('name').disabled);
requests[2].resolve(anchor());await tick();assert.equal(requests.length,4);
const payload=JSON.parse(requests[3].options.body);assert.equal(requests[3].url,'/api/sound_design/cues/revise');
assert.equal(payload.context_revision,data().context_revision);assert.equal(payload.registry_revision,null);
assert.equal(payload.anchor_revision,anchor().anchor_revision);assert.equal(payload.asset_sha256,sample().canonical.sha256);
assert.equal(payload.parameters.offset_frames,13920);assert.equal(payload.parameters.mix.duck_db,-6);
assert(!('book' in payload));assert(!('selection' in payload));
requests[3].resolve({status:'saved',cue_id:'Rain',book_token:data().book_token,registry_revision:'5'.repeat(64),version:'6'.repeat(64)});
await tick();requests[4].resolve(data());requests[5].resolve({assets:[sample()]});await pending;
assert(message.textContent.includes('Audio audition'));assert(!document.getElementById('sound-cues-refresh').disabled);
''')

    def test_changed_take_is_visible_and_never_silently_rewrites_history(self):
        self.run_js(r'''
const current=data();current.registry.versions=[version()];await ready(current);
const pending=click('Review dialogue binding');requests[2].resolve({...anchor(),selection:{...selection(),audio_sha256:'9'.repeat(64)}});await pending;
assert(message.textContent.includes('stale'));assert.equal(requests.length,3);
click('Revise');assert.equal(field('name').value,'rain');
assert.equal(field('offset').value,'0');assert.equal(current.registry.versions[0].recipe.selection.audio_sha256,selection().audio_sha256);
''')

    def test_stale_source_is_labeled_and_exclusion_keeps_exact_cue_identity(self):
        self.run_js(r'''
const current=data(),row=version('__proto__');row.recipe.book.source_sha256='9'.repeat(64);
current.registry.versions=[row];current.registry.selected=JSON.parse('{"__proto__":"'+'1'.repeat(64)+'"}');await ready(current);
assert(box.querySelectorAll('p').some(n=>n.textContent.includes('Source changed')));
const pending=click('Exclude cue');const payload=JSON.parse(requests[2].options.body);
assert.equal(payload.cue_id,'__proto__');assert.equal(payload.version,null);assert.equal(requests[2].options.method,'POST');
requests[2].reject(new Error('changed context'));await pending;
assert.equal(box.children.length,0);assert(message.textContent.includes('Refresh'));await api.refresh();
'''.replace('await api.refresh();', ''))

    def test_invalid_values_are_refused_before_network_and_failed_anchor_cannot_publish(self):
        self.run_js(r'''
await ready();click('New cue');field('name').value='rain';field('gain').value='Infinity';click('Save and select revision');assert.equal(requests.length,2);
field('gain').value='0';field('duration').value='fixed';field('loop').value='0.1';click('Save and select revision');assert.equal(requests.length,2);
field('loop').value='0';const pending=click('Save and select revision');requests[2].resolve({...anchor(),context_revision:'8'.repeat(64)});await pending;
assert.equal(requests.length,3);assert.equal(box.children.length,0);
''')

    def test_invalid_history_and_asset_responses_do_not_enable_controls(self):
        self.run_js(r'''
for(const change of [d=>{d.registry.versions=[version(),version()];},d=>{d.registry.selected={missing:'0'.repeat(64)};},d=>{d.chunks[0].has_recorded_take='yes';},d=>{d.registry.versions=[version()];d.registry.versions[0].recipe.mix.gain_db=Infinity;}]){
const value=data();change(value);await ready(value);assert.equal(box.querySelectorAll('button').length,0);assert(message.textContent.includes('Invalid')||message.textContent.includes('Check'));
}
for(const [key,value] of [['frames',Infinity],['frames',0],['frames',192000*86400+1],['frame_rate',0],['frame_rate',true],['channels',3],['sample_width',3]]){
const current=data();current.registry.versions=[version()];current.registry.versions[0].recipe.asset[key]=value;
await ready(current);assert.equal(box.querySelectorAll('button').length,0);assert(message.textContent.includes('Invalid'));
}
''')

    def test_unicode_history_and_negative_subsample_fades_are_handled_explicitly(self):
        self.run_js(r'''
const current=data(),row=version(' cue ');current.book.book_id='😀'.repeat(1024);row.recipe.book=current.book;
current.chunks[0].text='😀'.repeat(500);current.registry.versions=[row];await ready(current);click('Revise');
assert.equal(field('name').value,' cue ');field('fadein').value='-0.000000001';click('Save and select revision');
assert.equal(requests.length,2);assert(message.textContent.includes('negative'));
field('fadein').value='0';const pending=click('Save and select revision');requests[2].reject(new Error('fixture stop'));await pending;
assert.equal(requests.length,3);
''')

    def test_loop_and_fade_geometry_is_checked_against_converted_asset_length(self):
        self.run_js(r'''
await ready();click('New cue');field('name').value='loop';field('kind').value='ambience';field('duration').value='fixed';
for(const [length,loop,fadein,fadeout] of [['1','0.1','0','0'],['3','2','0','0'],['3','0','0','0'],['1','0','0.6','0.6']]){
field('length').value=length;field('loop').value=loop;field('fadein').value=fadein;field('fadeout').value=fadeout;
click('Save and select revision');assert.equal(requests.length,2);
}
field('length').value='3';field('loop').value='0.1';field('fadein').value='0';field('fadeout').value='0';
const pending=click('Save and select revision');assert.equal(requests.length,3);requests[2].reject(new Error('fixture stop'));await pending;
''')

    def test_page_navigation_uses_server_offset_and_resets_form(self):
        self.run_js(r'''
const current=data();current.total_chunks=201;current.next_offset=100;await ready(current);click('New cue');field('name').value='unsaved';
const pending=click('Next dialogue');assert(requests[2].url.includes('offset=100'));assert.equal(box.children.length,0);
requests[2].resolve({...data(),total_chunks:201,next_offset:200});requests[3].resolve({assets:[sample()]});await pending;
assert(box.querySelectorAll('button').some(n=>n.textContent==='Previous dialogue'));assert(!field('name'));
''')
