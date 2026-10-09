"""Native removal handlers: cancellation, book ownership and pending restoration."""
from pathlib import Path
import subprocess
import unittest

class VoiceRemovalSafeguardsJsTests(unittest.TestCase):
    def test_native_removal_guards(self):
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');let choice=false,resolve,fail=false,deletes=[],refreshes=0;
const c={currentBookFilename:'A',_voiceSaveSnapshot:{book_token:'a'.repeat(64)},flushVoiceSaves:async()=>{},showConfirm:async()=>choice,showToast(){},setCastStatus(){},escapeHtml:String,API:{del:async url=>{deletes.push(url);if(fail){throw Error('refused');}}},loadVoices:async()=>refreshes++,loadCastLibrary:async()=>refreshes++};c.window=c;vm.createContext(c);vm.runInContext(s.slice(s.indexOf('function getVoiceCardMetadata('),s.indexOf('function createVoiceCard(')),c);vm.runInContext(s.slice(s.indexOf('function showActionError('),s.indexOf('function showConfirm(')),c);
for(const [start,end] of [['async function applyConfirmedVoiceRemoval(','window.favoriteVoiceCandidate ='],['async function deleteCastMember(','// Save current-book characters'],['const pendingVoiceStateSaves =','// Editor: from this line'],['async function removeStylePoint(','// Voices tab:']]){const a=s.indexOf(start),b=s.indexOf(end,a);assert(a>=0&&b>a);vm.runInContext(s.slice(a,b),c);}
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{for(const action of [b=>c.deleteVoiceCandidate(b,'candidate / #'),b=>c.deleteCastMember('cast / #','VOICE # 日本語',b),b=>c.clearVoiceStates(b),b=>c.removeStylePoint('VOICE # 日本語',3,b)]){
const b={disabled:false,closest:()=>({dataset:{voice:'VOICE # 日本語'}})};c.currentBookFilename='A';deletes=[];refreshes=0;c.showConfirm=async()=>false;await action(b);assert.strictEqual(deletes.length,0);assert(!b.disabled);
c.showConfirm=()=>new Promise(done=>resolve=done);const pending=action(b);assert(b.disabled);await action(b);assert.strictEqual(deletes.length,0);await new Promise(setImmediate);c.currentBookFilename='B';resolve(true);await pending;assert.strictEqual(deletes.length,0);assert(!b.disabled);
c.currentBookFilename='A';c.showConfirm=async()=>true;await action(b);assert.strictEqual(deletes.length,1);assert(deletes[0].includes(encodeURIComponent('VOICE # 日本語')));assert.strictEqual(refreshes,1);assert(!b.disabled);
fail=true;await action(b);assert(!b.disabled);assert.strictEqual(refreshes,1);fail=false;}finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        source=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
        result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_cast_selection_changes_do_not_delete_or_clear_the_new_selection(self):
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');let resolve,ack,fail=false,requests=[],messages=[],confirmations=[];const button={disabled:false};
const c={currentBookFilename:'A',document:{getElementById:()=>button},showConfirm:text=>{confirmations.push(text);return new Promise(done=>resolve=done);},showToast(){},setCastStatus:text=>messages.push(text),escapeHtml:String,loadCastLibrary:async()=>{},API:{del:async url=>{requests.push(url);if(fail){throw Error('refused');}await new Promise(done=>ack=done);}}};c.window=c;c._selectedCast='cast A';vm.createContext(c);vm.runInContext(s.slice(s.indexOf('function getVoiceCardMetadata('),s.indexOf('function createVoiceCard(')),c);vm.runInContext(s.slice(s.indexOf('function showActionError('),s.indexOf('function showConfirm(')),c);
for(const [start,end] of [['async function applyConfirmedVoiceRemoval(','window.deleteVoiceCandidate ='],['async function deleteCast()','async function deleteCastMember(']]){const a=s.indexOf(start),b=s.indexOf(end,a);assert(a>=0&&b>a);vm.runInContext(s.slice(a,b),c);}
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{let pending=c.deleteCast();assert.strictEqual(confirmations[0],'Delete cast "cast A"? Cast-only members will be removed; shared characters will be kept.');assert(button.disabled);await c.deleteCast();c._selectedCast='cast B';resolve(true);await pending;assert.strictEqual(requests.length,0);assert.strictEqual(c._selectedCast,'cast B');assert(!button.disabled);
c._selectedCast='cast A';pending=c.deleteCast();resolve(false);await pending;assert.strictEqual(requests.length,0);
pending=c.deleteCast();resolve(true);for(let i=0;i<5;i++){await Promise.resolve();}assert.deepStrictEqual(requests,['/api/voice_library/casts/cast%20A']);c._selectedCast='cast B';ack();await pending;assert.strictEqual(c._selectedCast,'cast B','delete acknowledgement must not clear a later selection');assert(!button.disabled);
fail=true;c._selectedCast='cast A';pending=c.deleteCast();resolve(true);await pending;assert.strictEqual(c._selectedCast,'cast A');assert(!button.disabled);finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_candidate_accessible_names_escape_ids_and_favourite_state(self):
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');const c={escapeHtml:t=>String(t).replaceAll('"','&quot;').replaceAll('<','&lt;'),getInlineStringArgument:()=>"'safe'"};vm.createContext(c);vm.runInContext(s.slice(s.indexOf('function getVoiceCardMetadata('),s.indexOf('function createVoiceCard(')),c);vm.runInContext(s.slice(s.indexOf('function showActionError('),s.indexOf('function showConfirm(')),c);const a=s.indexOf('function getVoiceCandidateMarkup(');vm.runInContext(s.slice(a,s.indexOf('function getLibraryVoiceReference(',a)),c);
for(const favorite of [true,false]){const html=c.getVoiceCandidateMarkup([{candidate_id:'A"<B',favorite}]);for(const verb of [favorite?'Unfavourite':'Favourite','Use','Delete']){assert(html.includes('aria-label="'+verb+' candidate A&quot;&lt;B"'));}assert(!html.includes('candidate A"<B'));}
''')

    def test_designed_delete_refreshes_cards_only_after_successful_acknowledgment(self):
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path'),s=fs.readFileSync(process.env.DESIGNED_DELETE_SOURCE||path.join(path.dirname(process.argv[1]),'app-scripts.js'),'utf8');const guidanceCore=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');let decision,ack,refused=false,requests=[],events=[],toasts=[];const button={disabled:false},id='voice / 日本語';const c={window:{_designedVoicesCache:[{id,name:'Saved voice'}]},showConfirm:message=>{assert(message.includes('Saved voice'));return new Promise(resolve=>decision=resolve);},showToast:(...args)=>toasts.push(args),fetch:async(url)=>{requests.push(url);return new Promise(resolve=>ack=()=>resolve({ok:!refused,json:async()=>({detail:'refused'})}));},loadDesignedVoices:async()=>events.push('library'),loadVoices:async()=>events.push('cards')};vm.createContext(c);vm.runInContext(s.slice(s.indexOf('function getVoiceCardMetadata('),s.indexOf('function createVoiceCard(')),c);vm.runInContext(guidanceCore.slice(guidanceCore.indexOf('function showActionError('),guidanceCore.indexOf('function showConfirm(')),c);const a=s.indexOf('const pendingLibraryVoiceRemovals');vm.runInContext(s.slice(a,s.indexOf('window.openDesignedVoiceForEdit',a)),c);
let finished=false;process.on('beforeExit',()=>assert(finished,'delete verification unfinished'));(async()=>{let pending=c.window.deleteDesignedVoice(id,button);assert(button.disabled);await c.window.deleteDesignedVoice(id,button);decision(false);await pending;assert.strictEqual(requests.length,0);assert.deepStrictEqual(events,[]);assert(!button.disabled);
for(const failure of [true,false]){refused=failure;pending=c.window.deleteDesignedVoice(id,button);decision(true);await new Promise(setImmediate);assert.deepStrictEqual(events,[],'refresh must wait for delete acknowledgment');assert.strictEqual(requests.at(-1),'/api/voice_design/'+encodeURIComponent(id));ack();await pending;if(failure){assert.deepStrictEqual(events,[]);}else{assert.deepStrictEqual(events,['library','cards']);assert(toasts.some(([text])=>text.includes('Characters')&&text.includes('new reference')));}assert(!button.disabled);}finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def run_js(self,script):
        source=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
        result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
