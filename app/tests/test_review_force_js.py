"""Execute actual review handlers with the force checkbox on and off."""
import os
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(os.environ.get('REVIEW_FORCE_JS_SOURCE', Path(__file__).resolve().parent.parent / 'static/js/app-core.js'))

class ReviewForceJsTests(unittest.TestCase):
    def test_all_review_handlers_forward_checkbox(self):
        code = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
function fn(name){const start=source.indexOf('function '+name+'(');assert(start>=0,name);const end=source.indexOf('\n        }',start);assert(end>=0);return source.slice(source.slice(start-6,start)==='async '?start-6:start,end+10);}
const elements={},handlers={},calls=[];
function element(id){if(!elements[id]){elements[id]={checked:false,value:'4',style:{},addEventListener:(event,callback)=>{handlers[id]=callback;}};}return elements[id];}
const ctx={document:{getElementById:element,querySelectorAll:()=>[{dataset:{name:'book'}}]},API:{post:async(url,data)=>{calls.push({url,data});return {estimated_calls:1,total_entries:1,batch_size:25};}},confirmIfRemote:async()=>true,_disableReviewButtons:()=>{},_showReviewControls:()=>{},_onReviewDone:()=>{},pollScriptLogs:()=>{},pollReviewBatch:()=>{},_resetPauseBtn:()=>{},showToast:(message)=>{throw Error(message);},escapeHtml:String};
vm.createContext(ctx);
vm.runInContext(fn('_isReviewDedupeChecked')+'\n'+fn('_isReviewForceChecked')+'\n'+fn('startBatchReview'),ctx);
const a=source.indexOf("document.getElementById('btn-review-script').addEventListener");
const b=source.indexOf("document.getElementById('btn-review-script-contextual').addEventListener",a);
const end=source.indexOf('\n        const _reviewPauseResume',b);
assert(a>=0&&b>a&&end>b);vm.runInContext(source.slice(a,end),ctx);
(async()=>{for(const force of [false,true]){element('review-force-rerun').checked=force;await handlers['btn-review-script']();await handlers['btn-review-script-contextual']();await ctx.startBatchReview();const last=calls.slice(-3);assert.deepStrictEqual(last.map(x=>x.url),['/api/review_script','/api/review_script_contextual','/api/review_script/batch/start']);assert(last.every(x=>x.data.force_review===force));}})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node','-e',code,str(SOURCE)], capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)
