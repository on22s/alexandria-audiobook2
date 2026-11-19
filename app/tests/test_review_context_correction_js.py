"""Visible review context agrees with the native request, including rejects.

Also exercises the Entries/Characters toggle: it swaps the field value to that
mode's remembered setting, keeps both values independently, and sends the
selected mode with the request.
"""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class ReviewContextCorrectionJsTests(unittest.TestCase):
    def test_native_requests_use_visible_correction_and_toggle(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');
const elements={},posts=[],toasts=[],handlers={},stored=new Map();let handler,allowed=true,blocked=false;
function el(id){return elements[id]??={value:'',style:{},checked:false,max:'',textContent:'',attrs:{},
 setAttribute(name,value){this.attrs[name]=value;},getAttribute(name){return this.attrs[name]??null;},
 addEventListener:(event,fn)=>{if(event==='click'){handlers[id]=fn;if(id==='btn-review-script-contextual'){handler=fn;}}}};}
const storage={getItem:key=>{if(blocked){throw Error('storage blocked');}return stored.has(key)?stored.get(key):null;},
 setItem:(key,value)=>{if(blocked){throw Error('storage blocked');}stored.set(key,value);},
 removeItem:key=>stored.delete(key),clear:()=>stored.clear()};
const c={document:{getElementById:el,querySelectorAll:()=>[{dataset:{name:'book.json'}}]},window:{},
 localStorage:storage,reviewBatchSelected:[],confirmIfRemote:async()=>allowed,showToast:text=>toasts.push(text),
 API:{post:async(url,data)=>{posts.push({url,data});return {estimated_calls:1,total_entries:1,batch_size:25};}},
 _disableReviewButtons(){},_showReviewControls(){},_isReviewDedupeChecked:()=>true,
 _isReviewForceChecked:()=>false,_onReviewDone(){},pollScriptLogs(){},_resetPauseBtn(){},
 pollReviewBatch(){},escapeHtml:String};
vm.createContext(c),c.t=c.t||((key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split(String.fromCharCode(123)+name+String.fromCharCode(125)).join(String(values[name])),String(english))),c.formatUiSafetyText=c.formatUiSafetyText||((key,english,values=Object())=>c.t(key,english,values)),c.tSafety=c.tSafety||((key,english,values=Object())=>Object.fromEntries([['primary',c.t(key,english,values)],['secondary',null]]));
c.syncReviewRestartButtons=()=>{};c.refreshReviewRestart=()=>{};
let a=s.indexOf('const REVIEW_CONTEXT_STORAGE_KEY =');vm.runInContext(s.slice(a,s.indexOf('const _reviewPauseResume',a)),c);
a=s.indexOf('async function startBatchReview(');vm.runInContext(s.slice(a,s.indexOf('// --- Nickname discovery',a)),c);
let finished=false;process.on('beforeExit',()=>assert(finished));
const run=async()=>{await handler();};
(async()=>{
 // Entries mode: clamping, correction message, and the sent mode.
 storage.clear();
 for(const [raw,want] of [['101',100],['-3',1],['2.5',2],['4',4]]){
  el('review-context-window').value=raw;const before=toasts.length;await run();
  assert.strictEqual(el('review-context-window').value,String(want));
  assert.strictEqual(posts.at(-1).data.window_size,want);
  assert.strictEqual(posts.at(-1).data.context_mode,'entries');
  assert.strictEqual(toasts.length-before,raw==='4'?0:1);
  if(raw!=='4'){assert.strictEqual(toasts.at(-1),`Context Window must be 1–100; using ${want}`);}
 }
 assert.strictEqual(el('btn-review-context-mode').getAttribute('aria-pressed'),'false');
 assert.strictEqual(el('btn-review-context-mode').textContent,'Entries');
 assert.strictEqual(el('review-contextual-label').textContent,'Contextual Review (+/- N entries)');
 assert.strictEqual(el('review-context-window').max,'100','the field max comes from the entries table');

 // An empty field falls back to this mode's remembered value, not a hard-coded 4.
 el('review-context-window').value='7';await run();
 el('review-context-window').value='';await run();
 assert.strictEqual(posts.at(-1).data.window_size,7,'empty field uses the remembered entry count');

 // Toggle to characters: button state, field value/limit, and sent mode.
 handlers['btn-review-context-mode']();
 assert.strictEqual(el('btn-review-context-mode').textContent,'Characters');
 assert.strictEqual(el('review-contextual-label').textContent,'Contextual Review (+/- N characters)');
 assert.strictEqual(el('btn-review-context-mode').getAttribute('aria-pressed'),'true');
 assert.strictEqual(el('review-context-window').value,'2000','characters mode starts at its own default');
 assert.strictEqual(el('review-context-window').max,'1000000');
 assert(el('btn-review-context-mode').getAttribute('aria-label').includes('characters'));

 // Characters packing limit and per-mode preservation.
 el('review-context-window').value='5000';await run();
 assert.strictEqual(posts.at(-1).data.window_size,5000);
 assert.strictEqual(posts.at(-1).data.context_mode,'chars');
 el('review-context-window').value='1000001';await run();
 assert.strictEqual(el('review-context-window').value,'1000000');
 assert.strictEqual(toasts.at(-1),'Context Window must be 1–1000000; using 1000000');
 el('review-context-window').value='5000';await run();
 handlers['btn-review-context-mode']();
 assert.strictEqual(el('review-context-window').value,'7','entries value survived the round trip');
 handlers['btn-review-context-mode']();
 assert.strictEqual(el('review-context-window').value,'5000','characters value survived the round trip');

 // A storage-blocked browser must not break the toggle or the request.
 blocked=true;handlers['btn-review-context-mode']();await run();blocked=false;
 assert.strictEqual(posts.at(-1).data.context_mode,'entries');

 // Batch stays in entries mode: 0 disables, 100 is the cap, and it does not
 // overwrite the single-book preference.
 const count=posts.length;
 el('review-batch-context-window').value='101';await c.startBatchReview();
 assert.strictEqual(el('review-batch-context-window').value,'100');
 assert.strictEqual(posts.at(-1).data.context_window,100);
 assert.strictEqual(toasts.at(-1),'Context Window must be 0–100; using 100');
 el('review-batch-context-window').value='0';await c.startBatchReview();
 assert.strictEqual(posts.at(-1).data.context_window,0);
 assert.strictEqual(JSON.parse(stored.get('alexandria.review-context')).chars,5000,'batch did not change the single-book preference');
 allowed=false;const before=posts.length;
 el('review-context-window').value='101';await run();
 el('review-batch-context-window').value='101';await c.startBatchReview();
 assert.strictEqual(posts.length,before);
 finished=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
