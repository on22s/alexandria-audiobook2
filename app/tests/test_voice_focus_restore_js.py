"""Native focus restoration respects speaker identity, moved focus and book changes."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'

class VoiceFocusRestoreJsTests(unittest.TestCase):
    def test_native_control_identity_and_focus_ownership(self):
        code=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');const body={},outside={},speaker='A " 日本語';let focused=[];
function field(attrs={}){return{tagName:'BUTTON',disabled:false,getAttribute:key=>attrs[key]??null,getClientRects:()=>[{}],focus:options=>focused.push({field:this,attrs,options})};}
const old=field({'onclick':"setVoiceApproval(this, 'voice_status', 'approved')"});const equivalent=field({'onclick':"setVoiceApproval(this, 'voice_status', 'approved')"});const other=field({'onclick':"setVoiceApproval(this, 'persona_status', 'approved')"});const heading={getClientRects:()=>[{}],focus:options=>focused.push({heading:true,options})};let controls=[other,equivalent];const card={dataset:{voice:speaker},querySelectorAll:()=>controls,querySelector:()=>heading};old.closest=()=>card;
const container={contains:node=>node===old,querySelectorAll:()=>[card]};const c={document:{activeElement:old,body}};vm.createContext(c);let a=s.indexOf('function getVoiceListFocusSnapshot(');vm.runInContext(s.slice(a,s.indexOf('async function loadVoices(',a)),c);
const snapshot=c.getVoiceListFocusSnapshot(container,'book-A');assert.strictEqual(snapshot.speaker,speaker);c.document.activeElement=body;c.restoreVoiceListFocus(container,snapshot,'book-A');assert.strictEqual(focused.length,1);assert.strictEqual(focused[0].attrs.onclick,equivalent.getAttribute('onclick'));assert.strictEqual(focused[0].options.preventScroll,true);
focused=[];c.document.activeElement=outside;c.restoreVoiceListFocus(container,snapshot,'book-A');assert.strictEqual(focused.length,0);c.document.activeElement=body;c.restoreVoiceListFocus(container,snapshot,'book-B');assert.strictEqual(focused.length,0);
controls=[other];c.restoreVoiceListFocus(container,snapshot,'book-A');assert.strictEqual(focused.length,1);assert(focused[0].heading);assert.strictEqual(heading.tabIndex,-1);
focused=[];const favorite=field({'data-voice-focus-key':'candidate-favorite:x','aria-label':'Favourite candidate x'});favorite.closest=()=>card;container.contains=node=>node===favorite;c.document.activeElement=favorite;const fav=c.getVoiceListFocusSnapshot(container,'book-A');controls=[field({'data-voice-focus-key':'candidate-favorite:x','aria-label':'Unfavourite candidate x'})];c.document.activeElement=body;c.restoreVoiceListFocus(container,fav,'book-A');assert.strictEqual(focused.length,1);assert.strictEqual(focused[0].attrs['aria-label'],'Unfavourite candidate x');c.document.activeElement=outside;assert.strictEqual(c.getVoiceListFocusSnapshot(container,'book-A'),null);
'''
        result = subprocess.run(['node', '-e', code, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
