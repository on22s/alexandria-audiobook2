"""Draft preservation and stale audition responses, with model-free browser API doubles."""
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent/'static'
SETUP = r'''
Element.prototype.querySelector=function(s){return this.querySelectorAll(s)[0]||null;};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}for(const child of children){this.appendChild(child);}};
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(v){if(v){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
Element.prototype.insertCell=function(){return this.appendChild(new Element('td'));};Element.prototype.add=function(child){this.appendChild(child);};
for(const id of ['pronunciation-add','pronunciation-preview','pronunciation-save','pronunciation-player','pronunciation-status','pronunciation-rows','pronunciation-line','pronunciation-audition-original','pronunciation-audition-spoken']){body.appendChild(new Element('div',{id}));}
let pauses=0,loads=0;const player=document.getElementById('pronunciation-player');player.pause=()=>pauses++;player.load=()=>loads++;
context.Option=function(text,value){const node=new Element('option');node.textContent=text;node.value=value;return node;};
context.window.addEventListener=()=>{};context.chunkSnapshotBookToken='b'.repeat(64);context.chunkSnapshotBook='A';context.currentBookFilename='A';
const errors=[],posts=[],revoked=[],toasts=[];context.showToast=(...v)=>toasts.push(v);context.showActionError=(...e)=>errors.push(e);context.showConfirm=async()=>true;context.ensureEditorRenderSnapshot=async()=>[];
context.URL={createObjectURL:()=> 'blob:synthetic',revokeObjectURL:value=>revoked.push(value)};
const row={id:0,uid:'u0',speaker:'Hero'};let saved={names:JSON.parse('{"Rom":"Rohm","__proto__":"Literal"}'),revision:'a'.repeat(64)};
context.API={get:async()=>saved,post:async(url,data)=>{posts.push({url,data});if(url.endsWith('/save')){saved={names:data.names,revision:'c'.repeat(64)};return saved;}return {book_token:context.chunkSnapshotBookToken,counts:JSON.parse('{"Rom":2,"__proto__":1}'),lines:[row]};}};
context.fetch=async()=>({ok:true,blob:async()=>({})});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);const run=s=>vm.runInContext(s,context),tick=()=>new Promise(resolve=>setImmediate(resolve));
let finished=false;process.on('beforeExit',()=>assert(finished,'scenario must finish'));
(async()=>{
'''


class PronunciationPanelJsTests(unittest.TestCase):
    def run_js(self, code):
        result=subprocess.run(['node','-e',DOM_FIXTURE+SETUP+code+'\nfinished=true;})().catch(error=>{console.error(error);process.exitCode=1;});',str(STATIC/'js/app-pronunciation.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_literal_keys_counts_no_auto_generation_and_explicit_shared_save(self):
        self.run_js(r'''
context.addPronunciationRow('before load','must not be lost');assert.equal(toasts.length,1);assert.equal(document.getElementById('pronunciation-rows').children.length,0);await context.loadPronunciationPanel();assert.equal(document.getElementById('pronunciation-add').disabled,false);assert.equal(posts.length,0);assert.equal(Object.getPrototypeOf(context.getPronunciationNames()),run('Object.prototype'));assert.equal(context.getPronunciationNames().__proto__,'Literal');
await context.previewPronunciationPanel();const rows=document.getElementById('pronunciation-rows').children;assert.equal(rows[0].querySelector('.pronunciation-count').textContent,'2');assert.equal(rows[1].querySelector('.pronunciation-count').textContent,'1');
context.addPronunciationRow('<img onerror=x>','Say');assert.equal(body.querySelectorAll('img').length,0);assert.equal(posts.length,1);await context.savePronunciationPanel();assert.equal(posts[1].url,'/api/pronunciation/save');assert.equal(posts[1].data.names['<img onerror=x>'],'Say');assert.equal(run('pronunciationDirty'),false);
''')

    def test_duplicate_names_confirmation_edits_and_load_keep_unsaved_draft(self):
        self.run_js(r'''
await context.loadPronunciationPanel();context.addPronunciationRow('Rom','duplicate');assert.throws(()=>context.getPronunciationNames(),/only once/);document.getElementById('pronunciation-rows').children.at(-1).remove();context.onPronunciationEdit();
context.showConfirm=async()=>false;await context.loadPronunciationPanel();assert.equal(run('pronunciationDirty'),true);
context.showConfirm=async()=>{context.onPronunciationEdit();return true;};await context.savePronunciationPanel();assert.equal(posts.length,0);assert.equal(errors.length,1);
''')

    def test_late_count_reply_and_audition_edits_book_switch_cannot_publish(self):
        self.run_js(r'''
await context.loadPronunciationPanel();let release;context.API.post=()=>new Promise(resolve=>release=resolve);const pending=context.previewPronunciationPanel();await tick();context.onPronunciationEdit();release({book_token:'b'.repeat(64),counts:{Rom:99},lines:[row]});await pending;assert.equal(run('pronunciationLines.length'),0);
context.API.post=async()=>({book_token:'b'.repeat(64),counts:{Rom:2},lines:[row]});await context.previewPronunciationPanel();document.getElementById('pronunciation-line').value='0';document.getElementById('pronunciation-audition-original').value='Rom';
let requests=0;context.fetch=()=>{requests++;return new Promise(resolve=>release=resolve);};const audition=context.auditionPronunciation('original');await tick();await context.auditionPronunciation('original');assert.equal(requests,1);context.onPronunciationAuditionEdit();release({ok:true,blob:async()=>({})});await audition;assert.equal(run('pronunciationAudioUrl'),null);
context.fetch=async()=>({ok:true,blob:async()=>({})});await context.auditionPronunciation('original');assert.equal(run('pronunciationAudioUrl'),'blob:synthetic');context.currentBookFilename='B';context.resetPronunciationBook();assert.equal(run('pronunciationAudioUrl'),null);assert.deepEqual(revoked,['blob:synthetic']);assert.equal(run('pronunciationLines.length'),0);
''')

    def test_failed_audition_has_explicit_error_and_no_player(self):
        self.run_js(r'''
await context.loadPronunciationPanel();await context.previewPronunciationPanel();document.getElementById('pronunciation-line').value='0';document.getElementById('pronunciation-audition-original').value='Rom';document.getElementById('pronunciation-audition-spoken').value='Rohm';context.fetch=async()=>({ok:false,status:409,json:async()=>({detail:'Book changed'})});await context.auditionPronunciation('spoken');assert.equal(run('pronunciationAudioUrl'),null);assert.equal(errors.length,1);assert(document.getElementById('pronunciation-status').textContent.includes('refused'));
''')

    def test_multiline_native_value_normalization_preserves_unedited_raw_rules(self):
        self.run_js(r'''
context.API.get=async()=>({names:{'A\r\nB':'Ay\r\nBee'},revision:'a'.repeat(64)});
await context.loadPronunciationPanel();const row=document.getElementById('pronunciation-rows').children[0];
for(const field of ['original','spoken']){const input=row.querySelector('.pronunciation-'+field);assert.equal(input.tagName,'TEXTAREA');input.value=input.value.replace(/\r\n/g,'\n');input.pronunciationLoadedValue=input.value;}
await context.savePronunciationPanel();assert.equal(posts[0].data.names['A\r\nB'],'Ay\r\nBee');
row.querySelector('.pronunciation-spoken').value='Changed\nName';context.onPronunciationEdit();await context.savePronunciationPanel();assert.equal(posts[1].data.names['A\r\nB'],'Changed\nName');
''')

    def test_replacement_audition_sends_original_and_draft_rule_together(self):
        self.run_js(r'''
await context.loadPronunciationPanel();await context.previewPronunciationPanel();document.getElementById('pronunciation-line').value='0';document.getElementById('pronunciation-audition-original').value='Rom';document.getElementById('pronunciation-audition-spoken').value='Rohm~';let payload;
context.fetch=async(url,options)=>{payload=JSON.parse(options.body);return{ok:true,blob:async()=>({})};};await context.auditionPronunciation('spoken');assert.equal(payload.text,'Rom');assert.equal(payload.replacement,'Rohm~');
''')
