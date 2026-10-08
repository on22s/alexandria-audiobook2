"""Actual shared picker reads cannot overwrite a newer list and checked rows."""
from pathlib import Path
import subprocess
import unittest
SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class ScriptPickerRequestOrderJsTests(unittest.TestCase):
    def test_newer_review_picker_and_independent_containers_own_their_replies(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');const containers={},reads=[];let checkboxes=[];
function container(id){if(containers[id]){return containers[id];}const node={};let html='';Object.defineProperty(node,'innerHTML',{get:()=>html,set:value=>{html=value;if(id==='review-batch-list'){checkboxes=[...value.matchAll(/data-name="([^"]*)"[^>]*>/g)].map(m=>({dataset:{name:m[1]},checked:m[0].includes('checked')}));}}});containers[id]=node;return node;}
const c={window:null,reviewBatchScripts:[],document:{getElementById:container,querySelectorAll:()=>checkboxes.filter(row=>row.checked)},API:{get:()=>new Promise((resolve,reject)=>reads.push({resolve,reject}))},escapeHtml:String,getActionErrorMessage:(_label,error)=>error.message};c.window=c;vm.createContext(c);
const a=source.indexOf('async function _loadScriptList(');vm.runInContext(source.slice(a,source.indexOf('async function startBatchReview()',a)),c);
(async()=>{
const older=c.loadReviewBatchScripts(),newer=c.loadReviewBatchScripts();reads[1].resolve([{name:'New book'}]);await newer;checkboxes[0].checked=true;const markup=container('review-batch-list').innerHTML;reads[0].resolve([{name:'Old book'}]);await older;assert.strictEqual(c.reviewBatchScripts[0].name,'New book');assert.strictEqual(container('review-batch-list').innerHTML,markup);assert.strictEqual(checkboxes[0].checked,true);
const start=reads.length,oldError=c.loadReviewBatchScripts(),latest=c.loadReviewBatchScripts();reads[start+1].resolve([{name:'New book'}]);await latest;const current=container('review-batch-list').innerHTML;reads[start].reject(Error('old error'));await oldError;assert.strictEqual(container('review-batch-list').innerHTML,current);assert(checkboxes[0].checked);
let other;const next=reads.length,one=c.loadReviewBatchScripts(),two=c._loadScriptList('cast-picker',rows=>other=rows);reads[next+1].resolve([{name:'Cast target'}]);await two;reads[next].resolve([{name:'Review target'}]);await one;assert.strictEqual(other[0].name,'Cast target');assert.strictEqual(c.reviewBatchScripts[0].name,'Review target');
const failureIndex=reads.length,failure=c.loadReviewBatchScripts();reads[failureIndex].reject(Error('current failure'));await failure;assert(container('review-batch-list').innerHTML.includes('current failure'));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
