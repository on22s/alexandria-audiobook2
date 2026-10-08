"""Actual upload queue rebuilding retains narrator drafts by file identity."""
from pathlib import Path
import subprocess
import unittest
SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class ScriptBatchNarratorDraftJsTests(unittest.TestCase):
    def test_add_sort_repeat_and_remove_preserve_only_remaining_file_drafts(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const fields={};let selected=[],files=[],rows=[];
const el=id=>fields[id]||(fields[id]={value:'',style:{},textContent:''});
const body=el('script-batch-queue-body');Object.defineProperty(body,'innerHTML',{set(){rows=[];for(const id of Object.keys(fields)){if(id.startsWith('script-batch-narrator-')){delete fields[id];}}}});body.appendChild=row=>rows.push(row);
const c={window:null,scriptBatchQueue:[],document:{getElementById:id=>id==='script-batch-files'?{files}:el(id),querySelectorAll:()=>selected,createElement(){const row={};Object.defineProperty(row,'innerHTML',{set:html=>{const id=html.match(/id="(script-batch-narrator-\d+)"/)[1];el(id);}});return row;}},escapeHtml:String};c.window=c;vm.createContext(c);
const a=source.indexOf('window.onScriptBatchFilesChange =');vm.runInContext(source.slice(a,source.indexOf('window.scriptBatchSelectAll =',a)),c);
const A={dataset:{name:'book-a.txt'}},B={dataset:{name:'book-b.txt'}},local={name:'book-a.txt'};
selected=[A];c.onScriptBatchFilesChange();el('script-batch-narrator-0').value='Alice "quoted"';selected=[A,B];c.onScriptBatchFilesChange();assert.strictEqual(el('script-batch-narrator-0').value,'Alice "quoted"');el('script-batch-narrator-1').value='Bob';
selected=[B,A];c.onScriptBatchFilesChange();assert.strictEqual(el('script-batch-narrator-0').value,'Bob');assert.strictEqual(el('script-batch-narrator-1').value,'Alice "quoted"');c.onScriptBatchFilesChange();assert.strictEqual(el('script-batch-narrator-0').value,'Bob');
files=[local];c.onScriptBatchFilesChange();assert.strictEqual(el('script-batch-narrator-0').value,'');assert.strictEqual(el('script-batch-narrator-2').value,'Alice "quoted"');el('script-batch-narrator-0').value='Local narrator';
selected=[A];c.onScriptBatchFilesChange();assert.strictEqual(el('script-batch-narrator-0').value,'Local narrator');assert.strictEqual(el('script-batch-narrator-1').value,'Alice "quoted"');
files=[{name:'book-a.txt'}];selected=[A,B];c.onScriptBatchFilesChange();assert.strictEqual(el('script-batch-narrator-0').value,'');assert.strictEqual(el('script-batch-narrator-1').value,'Alice "quoted"');assert.strictEqual(el('script-batch-narrator-2').value,'');
files=[];selected=[];c.onScriptBatchFilesChange();assert.strictEqual(c.scriptBatchQueue.length,0);assert.strictEqual(el('script-batch-queue-container').style.display,'none');
'''
        result = subprocess.run(['node', '-e', code, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
