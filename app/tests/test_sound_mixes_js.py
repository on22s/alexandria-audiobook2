"""Mixed-audio UI binds exact candidate identities and explicit user decisions."""
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE
VALIDATION_SOURCE = Path(__file__).resolve().parents[1]/'static/js/app-publication-validation.js'
VALIDATION_LOADER = "vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);vm.runInContext(fs.readFileSync(process.argv[3],'utf8'),context);\n"
SOURCE = Path(__file__).resolve().parent.parent/'static/js/app-sound-mixes.js'
SETUP = r'''
context.window=context;context.confirm=()=>true;
Object.defineProperty(Element.prototype,'type',{get(){return this.attrs.type||'text';},set(value){this.attrs.type=value;}});
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
Element.prototype.replaceChildren=function(...nodes){for(const child of [...this.children])child.remove();this.textContent='';for(const node of nodes)this.appendChild(node);};
Element.prototype.append=function(...nodes){for(const node of nodes)this.appendChild(node);};
Element.prototype.pause=function(){};Element.prototype.load=function(){};Element.prototype.play=function(){return Promise.resolve();};
for(const id of ['sound-mixes-content','sound-mixes-status','sound-mixes-refresh'])body.appendChild(new Element('div',{id}));
const profiles=()=>Object.fromEntries(['mp3','wav','m4b'].map(name=>[name,{available:true,revision:'d'.repeat(64),summary:name+' approved profile'}]));
const requests=[];context.fetch=(url,options={})=>new Promise((resolve,reject)=>requests.push({url,options,resolve:(value,status=200)=>resolve({status,json:async()=>value}),reject}));
context.API={_handleError:async()=>{}};
const row=()=>({stage:'render-fixture',bytes:96000,recovery_revision:'a'.repeat(64),status:'metadata_ready',approval_status:'absent',media_verified:false,candidate_revision:'b'.repeat(64),context_revision:'c'.repeat(64),book_id:'<img onerror=bad()>',frames:4800,frame_rate:24000});
const tick=()=>new Promise(resolve=>setImmediate(resolve));load();
const api=vm.runInContext('SoundMixes',context),box=document.getElementById('sound-mixes-content');
const click=label=>{const node=box.querySelectorAll('button').find(n=>n.textContent===label);assert(node,label);return node.onclick();};
async function ready(rows=[row()]){const p=api.refresh();requests.at(-1).resolve({context_revision:'c'.repeat(64)});await tick();requests.at(-1).resolve({delivery_profiles:profiles(),candidates:rows});await p;}
'''
class SoundMixesJsTests(unittest.TestCase):
    def run_js(self, body):
        script=DOM_FIXTURE+VALIDATION_LOADER+SETUP+'\nlet done=false;process.on("beforeExit",()=>assert(done));(async()=>{'+body+'\ndone=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result=subprocess.run(['node','-e',script,str(SOURCE),str(VALIDATION_SOURCE),str(SOURCE.parent/'app-sound-mix-cover.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_profile_is_visible_and_format_change_requires_new_confirmation(self):
        self.run_js(r'''
await ready();assert(box.querySelectorAll('p').some(n=>n.textContent==='mp3 approved profile'));
const checkbox=box.querySelectorAll('input').find(n=>n.type==='checkbox');checkbox.checked=true;
const format=box.querySelectorAll('select')[0];format.value='wav';format.onchange();
assert.strictEqual(checkbox.checked,false);assert(box.querySelectorAll('p').some(n=>n.textContent==='wav approved profile'));
checkbox.checked=true;const pending=click('Record full-mix approval');
const body=JSON.parse(requests[2].options.body);assert.equal(body.expected_encoding,'d'.repeat(64));assert.equal(body.delivery.format,'wav');
requests[2].resolve({});await tick();requests[3].resolve({context_revision:'c'.repeat(64)});await tick();requests[4].resolve({delivery_profiles:profiles(),candidates:[row()]});await pending;
''')

    def test_m4b_metadata_edits_clear_confirmation_and_bind_actual_entered_values(self):
        self.run_js(r'''
await ready();const format=box.querySelectorAll('select')[0];format.value='m4b';format.onchange();
const check=box.querySelectorAll('input').find(n=>n.type==='checkbox');check.checked=true;
const fields=box.querySelectorAll('input').filter(n=>n.hasAttribute('data-m4b-field'));
const title=fields.find(n=>n.getAttribute('data-m4b-field')==='title');title.value='Synthetic <title> 雪';title.oninput();assert.strictEqual(check.checked,false);
const chapters=fields.find(n=>n.getAttribute('data-m4b-field')==='per_chunk_chapters');chapters.checked=true;check.checked=true;
const pending=click('Record full-mix approval');const body=JSON.parse(requests[2].options.body);
assert.equal(body.delivery.format,'m4b');assert.equal(body.delivery.m4b.title,title.value);assert.strictEqual(body.delivery.m4b.per_chunk_chapters,true);
assert.equal(body.delivery.m4b.author,'');assert.equal(body.expected_encoding,'d'.repeat(64));assert.equal(box.querySelectorAll('title').length,0);
requests[2].resolve({});await tick();requests[3].resolve({context_revision:'c'.repeat(64)});await tick();requests[4].resolve({delivery_profiles:profiles(),candidates:[row()]});await pending;
''')

    def test_source_cover_choice_binds_preview_and_requires_separate_review(self):
        self.run_js(r'''
await ready();const format=box.querySelectorAll('select')[0];format.value='m4b';format.onchange();
context.API.get=async()=>({book_token:'a'.repeat(64),revision:'b'.repeat(64),binding_status:'current',source_name:'Source',uploaded_cover:{present:false},candidates:[{id:'e'.repeat(64),origin:{filename:'<img onerror=bad()>',sha256:'f'.repeat(64)},fields:{title:[],author:[]},other_creators:[],warnings:[],cover_status:'available',cover:{sha256:'f'.repeat(64),preview_data_url:'data:image/png;base64,AA=='}}]});
await click('Review cover options');const select=box.querySelectorAll('select')[1];select.value='e'.repeat(64);select.onchange();
const checks=box.querySelectorAll('input').filter(n=>n.type==='checkbox');checks[0].checked=true;
click('Record full-mix approval');assert.equal(requests.length,2);assert(document.getElementById('sound-mixes-status').textContent.includes('confirm its review'));
box.querySelectorAll('img')[0].onload();checks.at(-1).checked=true;checks.at(-1).onchange();assert.strictEqual(checks[0].checked,false);checks[0].checked=true;
const pending=click('Record full-mix approval');const body=JSON.parse(requests[2].options.body);
assert.equal(body.delivery.m4b.cover_sha256,'f'.repeat(64));assert.equal(body.delivery.m4b.cover_source.expected_revision,'b'.repeat(64));assert.equal(body.delivery.m4b.cover_source.expected_book_token,'a'.repeat(64));assert.strictEqual(body.delivery.m4b.cover_source.reviewed,true);
assert.equal(box.querySelectorAll('img').length,1);assert.equal(box.querySelectorAll('img')[0].src,'data:image/png;base64,AA==');
requests[2].resolve({});await tick();requests[3].resolve({context_revision:'c'.repeat(64)});await tick();requests[4].resolve({delivery_profiles:profiles(),candidates:[row()]});await pending;
''')

    def test_uploaded_preview_requires_image_load_and_explicit_review_and_handles_errors(self):
        self.run_js(r'''
let changed=0;const controls=vm.runInContext('createSoundMixCoverControls',context)(()=>changed++,()=>true);box.append(controls.element);
context.API.get=async()=>({book_token:'a'.repeat(64),revision:'b'.repeat(64),binding_status:'current',source_name:'Source',uploaded_cover:{present:true,sha256:'f'.repeat(64)},candidates:[]});
await click('Review cover options');const select=box.querySelectorAll('select')[0],check=box.querySelectorAll('input')[0];
select.value='uploaded';select.onchange();let image=box.querySelectorAll('img')[0];
assert.equal(image.src,'/api/m4b_cover/preview?expected_book_token='+ 'a'.repeat(64)+'&sha256='+'f'.repeat(64));
assert.strictEqual(check.disabled,true);check.checked=true;assert.throws(()=>controls.getSelection(),/confirm its review/);
image.onload();assert.strictEqual(check.disabled,false);check.checked=false;assert.throws(()=>controls.getSelection(),/confirm its review/);
check.checked=true;assert.equal(controls.getSelection().cover_sha256,'f'.repeat(64));assert.strictEqual(controls.getSelection().cover_source,null);
const before=changed,lateLoad=image.onload;image.onerror();assert(changed>before);lateLoad();assert.strictEqual(check.checked,false);assert.strictEqual(check.disabled,true);assert.throws(()=>controls.getSelection(),/confirm its review/);
select.value='';select.onchange();assert.deepStrictEqual(JSON.parse(JSON.stringify(controls.getSelection())),{});
''')

    def test_programmatic_choice_change_cannot_reuse_loaded_image_review(self):
        self.run_js(r'''
const controls=vm.runInContext('createSoundMixCoverControls',context)(()=>{},()=>true);box.append(controls.element);
context.API.get=async()=>({book_token:'a'.repeat(64),revision:'b'.repeat(64),binding_status:'current',source_name:'Source',uploaded_cover:{present:false},candidates:['d','e'].map(id=>({id:id.repeat(64),origin:{filename:id+'.epub',sha256:'f'.repeat(64)},fields:{title:[],author:[]},other_creators:[],warnings:[],cover_status:'available',cover:{sha256:'f'.repeat(64),preview_data_url:'data:image/png;base64,AA=='}}))});
await click('Review cover options');const select=box.querySelectorAll('select')[0],check=box.querySelectorAll('input')[0];
select.value='d'.repeat(64);select.onchange();box.querySelectorAll('img')[0].onload();check.checked=true;
assert.equal(controls.getSelection().cover_source.origin_id,'d'.repeat(64));select.value='e'.repeat(64);
assert.throws(()=>controls.getSelection(),/confirm its review/);
''')

    def test_old_image_events_cannot_approve_or_clear_a_new_choice(self):
        self.run_js(r'''
let changed=0;const controls=vm.runInContext('createSoundMixCoverControls',context)(()=>changed++,()=>true);box.append(controls.element);
context.API.get=async()=>({book_token:'a'.repeat(64),revision:'b'.repeat(64),binding_status:'current',source_name:'Source',uploaded_cover:{present:true,sha256:'f'.repeat(64)},candidates:[]});
await click('Review cover options');const select=box.querySelectorAll('select')[0],check=box.querySelectorAll('input')[0];
select.value='uploaded';select.onchange();const first=box.querySelectorAll('img')[0],oldLoad=first.onload,oldError=first.onerror;
select.value='';select.onchange();select.value='uploaded';select.onchange();const second=box.querySelectorAll('img')[0];assert.notStrictEqual(first,second);
oldLoad();assert.strictEqual(check.disabled,true);assert.throws(()=>controls.getSelection(),/confirm its review/);
second.onload();check.checked=true;select.value='';assert.deepStrictEqual(JSON.parse(JSON.stringify(controls.getSelection())),{});select.value='uploaded';const before=changed;oldError();assert.equal(changed,before);assert.strictEqual(check.checked,true);assert.equal(controls.getSelection().cover_sha256,'f'.repeat(64));
const staleLoad=second.onload,staleError=second.onerror;await click('Review cover options');const afterRefresh=changed;
staleLoad();staleError();assert.equal(changed,afterRefresh);assert.strictEqual(check.disabled,true);assert.strictEqual(check.checked,false);assert.equal(box.querySelectorAll('img').length,0);
''')

    def test_playback_and_approval_require_exact_candidate_and_explicit_confirmation(self):
        self.run_js(r'''
await ready();assert(box.querySelectorAll('img').every(n=>n.hidden));click('Listen');
const audio=box.querySelectorAll('audio')[0];assert(audio.src.includes('candidate_revision='+row().candidate_revision));assert(audio.src.includes('end_frame=4800'));
click('Record full-mix approval');assert.equal(requests.length,2);
box.querySelectorAll('input').find(n=>n.type==='checkbox').checked=true;
const pending=click('Record full-mix approval');click('Record full-mix approval');assert.equal(requests.length,3);
const body=JSON.parse(requests[2].options.body);assert.equal(body.candidate_revision,row().candidate_revision);assert.strictEqual(body.full_mix_reviewed,true);assert.equal(body.delivery.format,'mp3');
requests[2].resolve({});await tick();requests[3].resolve({context_revision:'c'.repeat(64)});await tick();requests[4].resolve({delivery_profiles:profiles(),candidates:[row()]});await pending;
''')

    def test_discard_needs_confirmation_and_binds_inventory_revision(self):
        self.run_js(r'''
await ready();context.confirm=()=>false;click('Discard saved mix');assert.equal(requests.length,2);
context.confirm=()=>true;const pending=click('Discard saved mix');assert(requests[2].url.includes('recovery_revision='+row().recovery_revision));
requests[2].resolve({});await tick();requests[3].resolve({context_revision:'c'.repeat(64)});await tick();requests[4].resolve({delivery_profiles:profiles(),candidates:[]});await pending;
''')

    def test_invalid_inventory_and_missing_book_do_not_enable_render(self):
        self.run_js(r'''
await ready([{...row(),frames:Infinity}]);assert.equal(box.querySelectorAll('button').length,0);
const p=api.refresh();requests.at(-1).reject(new Error('No active book'));await tick();requests.at(-1).resolve({delivery_profiles:profiles(),candidates:[row()]});await p;
assert(!box.querySelectorAll('button').some(n=>n.textContent==='Render full mix'));assert(box.querySelectorAll('button').some(n=>n.textContent==='Discard saved mix'));
''')

    def test_unavailable_profile_preserves_recovery_and_blocks_approval(self):
        self.run_js(r'''
const unavailable=profiles();unavailable.m4b={available:false,revision:null,summary:'M4B unavailable'};
const p=api.refresh();requests.at(-1).resolve({context_revision:'c'.repeat(64)});await tick();requests.at(-1).resolve({delivery_profiles:unavailable,candidates:[row()]});await p;
assert(box.querySelectorAll('button').some(n=>n.textContent==='Discard saved mix'));
const select=box.querySelectorAll('select')[0];assert.equal(select.value,'mp3');assert.strictEqual(select.children.find(n=>n.value==='m4b').disabled,true);
select.value='m4b';select.onchange();box.querySelectorAll('input').find(n=>n.type==='checkbox').checked=true;
click('Record full-mix approval');assert.equal(requests.length,2);assert(document.getElementById('sound-mixes-status').textContent.includes('available delivery format'));
const malformed={...unavailable,m4b:{...unavailable.m4b,available:'false'}};
const bad=api.refresh();requests.at(-1).resolve({context_revision:'c'.repeat(64)});await tick();requests.at(-1).resolve({delivery_profiles:malformed,candidates:[row()]});await bad;
assert.equal(box.querySelectorAll('button').length,0);
''')

    def test_deliver_saved_approval_and_verified_download_are_identity_bound(self):
        self.run_js(r"""
const approved={...row(),approval_status:'recorded',approval_revision:'e'.repeat(64),delivery:{format:'wav'}};
await ready([approved]);const p=click('Deliver saved WAV approval');
const request=requests.at(-1);assert(request.url.endsWith('/deliver'));
assert.deepStrictEqual(JSON.parse(request.options.body),{candidate_revision:'b'.repeat(64),approval_revision:'e'.repeat(64),integrity_confirmation:null});
request.resolve({status:'published',file:'mixed_audiobook.wav',candidate_revision:'b'.repeat(64),approval_revision:'e'.repeat(64),warnings:[],quality:{status:'verified',sha256:'f'.repeat(64),output_bytes:96044}});
await tick();requests.at(-1).resolve({context_revision:'c'.repeat(64)});await tick();requests.at(-1).resolve({delivery_profiles:profiles(),candidates:[approved]});await p;
const link=box.querySelectorAll('a').find(n=>n.textContent.includes('Download WAV'));assert(link);
assert.strictEqual(link.href,'/api/sound_design/delivery/wav/download?sha256='+'f'.repeat(64));
assert.strictEqual(link.download,'mixed_audiobook.wav');
""")

    def test_source_review_requires_new_explicit_confirmation_and_exact_snapshot(self):
        self.run_js(r"""
const approved={...row(),approval_status:'recorded',approval_revision:'e'.repeat(64),delivery:{format:'mp3'}};
await ready([approved]);const p=click('Deliver saved MP3 approval');
requests.at(-1).resolve({detail:{status:'differences',snapshot:'f'.repeat(64),hunks:[{source_before:'Before',source_words:'<script>bad</script>',source_after:'After',script_words:'Own dialogue'}]}},409);
await tick();requests.at(-1).resolve({context_revision:'c'.repeat(64)});await tick();requests.at(-1).resolve({delivery_profiles:profiles(),candidates:[approved]});await p;
assert(box.querySelectorAll('pre').some(n=>n.textContent.includes('<script>bad</script>')));assert(!box.querySelectorAll('script').length);
const count=requests.length;click('Confirm source text and deliver');assert.strictEqual(count,requests.length);
const consent=box.querySelectorAll('input').at(-1);assert.strictEqual(consent.checked,false);consent.checked=true;
const confirmed=click('Confirm source text and deliver');assert.strictEqual(JSON.parse(requests.at(-1).options.body).integrity_confirmation,'f'.repeat(64));
requests.at(-1).resolve({status:'published',file:'mixed_audiobook.mp3',candidate_revision:'b'.repeat(64),approval_revision:'e'.repeat(64),warnings:['Identity unavailable'],quality:null});
await tick();requests.at(-1).resolve({context_revision:'c'.repeat(64)});await tick();requests.at(-1).resolve({delivery_profiles:profiles(),candidates:[approved]});await confirmed;
assert(!box.querySelectorAll('a').length);assert(document.getElementById('sound-mixes-status').textContent.includes('Published mixed_audiobook.mp3'));
""")
