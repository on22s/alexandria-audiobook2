"""Native test-audio handler submits once and releases fields on terminal results."""
from pathlib import Path
import subprocess
import unittest

class LoraTestPendingJsTests(unittest.TestCase):
    def test_pending_failure_retry_and_success(self):
        source=Path(__file__).resolve().parent.parent/'static/js/app-training.js'
        script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');const fields={};const el=id=>fields[id]||(fields[id]={value:'',disabled:false,innerHTML:'',textContent:'',style:{},focus(){}});let finish,fail,calls=0;const c={window:null,document:{getElementById:el},API:{post:(path,body)=>{assert.strictEqual(path,'/api/lora/test');assert.strictEqual(body.adapter_id,'A');calls++;return new Promise((r,j)=>{finish=r;fail=j;});}},showToast(){},escapeHtml:x=>String(x).replaceAll('<','&lt;'),Date};c.window=c;vm.createContext(c);{const guidanceCore=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');vm.runInContext(guidanceCore.slice(guidanceCore.indexOf('function showActionError('),guidanceCore.indexOf('function showConfirm(')),c);}const a=s.indexOf('let loraTestPending =');vm.runInContext(s.slice(a,s.indexOf('window.deleteLoraModel',a)),c);
el('lora-test-adapter').value='A';el('lora-test-text').value='Words';el('lora-test-instruct').value='Calm';el('btn-lora-test-generate').innerHTML='<i></i>Generate';el('lora-test-instruct').disabled=true;el('lora-test-audio').innerHTML='old audio';let done=false;process.on('beforeExit',()=>assert(done));(async()=>{
let pending=c.runLoraTest();assert(el('btn-lora-test-generate').disabled);assert(el('lora-test-adapter').disabled);assert(el('lora-test-text').disabled);assert.strictEqual(el('lora-test-audio').innerHTML,'');assert(el('lora-test-status').textContent.includes('A'));await c.runLoraTest();c.testLoraModel('B');assert.strictEqual(calls,1);assert.strictEqual(el('lora-test-adapter').value,'A');fail(Error('<offline>'));await pending;assert(!el('btn-lora-test-generate').disabled);assert(!el('lora-test-text').disabled);assert(el('lora-test-instruct').disabled);assert(el('lora-test-status').innerHTML.includes('test task status before generating again'));assert(!el('lora-test-status').innerHTML.includes('<offline>'));
pending=c.runLoraTest();finish({audio_url:'/audio/test.wav'});await pending;assert.strictEqual(calls,2);assert(el('lora-test-audio').innerHTML.includes('/audio/test.wav'));assert.strictEqual(el('btn-lora-test-generate').innerHTML,'<i></i>Generate');assert(!el('btn-lora-test-generate').disabled);done=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)
