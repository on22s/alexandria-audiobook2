"""Visible review context agrees with the native request, including rejects."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'

class ReviewContextCorrectionJsTests(unittest.TestCase):
    def test_native_requests_use_visible_correction(self):
        code=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');const elements={},posts=[],toasts=[];let handler,allowed=true;
function el(id){return elements[id]??={value:'',style:{},checked:false,addEventListener:(event,fn)=>handler=fn};}
const c={document:{getElementById:el,querySelectorAll:()=>[{dataset:{name:'book.json'}}]},window:{},reviewBatchSelected:[],confirmIfRemote:async()=>allowed,showToast:text=>toasts.push(text),API:{post:async(url,data)=>{posts.push({url,data});return{};}},_disableReviewButtons(){},_showReviewControls(){},_isReviewDedupeChecked:()=>true,_isReviewForceChecked:()=>false,_onReviewDone(){},pollScriptLogs(){},_resetPauseBtn(){},pollReviewBatch(){},escapeHtml:String};vm.createContext(c);
let a=s.indexOf('function applyReviewContextWindow(');vm.runInContext(s.slice(a,s.indexOf('const _reviewPauseResume',a)),c);a=s.indexOf('async function startBatchReview(');vm.runInContext(s.slice(a,s.indexOf('// --- Nickname discovery',a)),c);
let finished=false;process.on('beforeExit',()=>assert(finished));
(async()=>{for(const [raw,want] of [['99',12],['-3',1],['',4],['2.5',2],['4',4]]){el('review-context-window').value=raw;const before=toasts.length;await handler();assert.strictEqual(el('review-context-window').value,String(want));assert.strictEqual(posts.at(-1).data.window_size,want);assert.strictEqual(toasts.length-before,raw==='4'?0:1);if(raw!=='4')assert.strictEqual(toasts.at(-1),`Context Window must be 1–12; using ${want}`);}
for(const [raw,want] of [['99',12],['-3',0],['',0],['0',0],['12',12]]){el('review-batch-context-window').value=raw;const before=toasts.length;await c.startBatchReview();assert.strictEqual(el('review-batch-context-window').value,String(want));assert.strictEqual(posts.at(-1).data.context_window,want);assert.strictEqual(toasts.length-before,['0','12'].includes(raw)?0:1);}
allowed=false;const count=posts.length;el('review-context-window').value='99';await handler();el('review-batch-context-window').value='99';await c.startBatchReview();assert.strictEqual(posts.length,count);assert.strictEqual(el('review-context-window').value,'99');assert.strictEqual(el('review-batch-context-window').value,'99');finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',code,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
