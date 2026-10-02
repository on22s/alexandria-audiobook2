"""Real multipart import callbacks remain scoped to their modal opening."""
from pathlib import Path
import os
import subprocess
import unittest

SOURCE = Path(os.environ.get('CLONE_IMPORT_SOURCE', Path(__file__).resolve().parent.parent / 'static/js/app-scripts.js'))
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const fields={},uploads=[],toasts=[],listeners=[];let hides=0,shows=0,refreshes=0;
const el=id=>fields[id]||(fields[id]={value:'',checked:false,disabled:false,textContent:'',onclick:null,addEventListener:(event,callback,options)=>listeners.push({event,callback,options})});
const hidden=()=>{const active=listeners.splice(0);for(const listener of active){assert.strictEqual(listener.event,'hidden.bs.modal');assert.strictEqual(listener.options.once,true);listener.callback();}};
const modal={show:()=>shows++,hide:()=>{hides++;hidden();}};
const ctx={window:null,document:{getElementById:el},bootstrap:{Modal:{getOrCreateInstance:()=>modal}},FormData,showToast:(...args)=>toasts.push(args),loadVoices:async()=>refreshes++,API:{get:async()=>[{id:'imported'}]},fetch:(url,options)=>{assert.strictEqual(url,'/api/clone_voices/upload');assert.strictEqual(options.method,'POST');let resolve;const promise=new Promise(r=>resolve=r);uploads.push({form:options.body,resolve});return promise;}};ctx.window=ctx;vm.createContext(ctx);
const start=source.indexOf('function _cloneImportFields()');vm.runInContext(source.slice(start,source.indexOf('window.playCloneVoice =',start)),ctx);
const file=name=>new File(['native multipart fixture'],name,{type:'audio/wav'});
const open=name=>{ctx._openCloneImportModal(file(name));el('clone-import-ref-text').value='words for '+name;el('clone-import-rights-confirmed').checked=true;ctx._refreshCloneImportSubmit();};
const complete=(index,ok=true)=>uploads[index].resolve({ok,json:async()=>ok?{measures:{duration_s:1}}:{detail:'fixture rejected'}});
'''


class CloneImportLifecycleJsTests(unittest.TestCase):
    def run_js(self, code):
        script = SETUP + '\nlet finished=false;process.on("beforeExit",()=>assert(finished,"Import assertions must finish"));\n(async()=>{\n' + code + '\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_old_success_does_not_close_new_modal_and_success_releases_submit_handler(self):
        self.run_js(r'''
open('A.wav');const pendingA=el('clone-import-submit').onclick();assert.strictEqual(uploads[0].form.get('file').name,'A.wav');assert.strictEqual(uploads[0].form.get('ref_text'),'words for A.wav');assert.strictEqual(uploads[0].form.get('rights_confirmed'),'true');hidden();
open('B.wav');const handlerB=el('clone-import-submit').onclick;const pendingB=handlerB();assert.strictEqual(uploads[1].form.get('file').name,'B.wav');complete(0);await pendingA;assert.strictEqual(hides,0,'old completion must not hide B');assert.strictEqual(el('clone-import-submit').onclick,handlerB);assert.strictEqual(el('clone-import-submit').disabled,true);assert.strictEqual(el('clone-import-ref-text').value,'words for B.wav');
complete(1);await pendingB;assert.strictEqual(hides,1);assert.strictEqual(el('clone-import-submit').onclick,null,'persistent submit must not retain successful File');assert.strictEqual(refreshes,2);assert.strictEqual(listeners.length,0);
''')

    def test_old_failure_cannot_enable_new_inflight_submit_or_restore_closed_handler(self):
        self.run_js(r'''
open('A.wav');const pendingA=el('clone-import-submit').onclick();hidden();open('B.wav');const pendingB=el('clone-import-submit').onclick();complete(0,false);await pendingA;assert.strictEqual(el('clone-import-submit').disabled,true,'old failure cannot enable B');assert.strictEqual(hides,0);
hidden();assert.strictEqual(el('clone-import-submit').onclick,null);complete(1,false);await pendingB;assert.strictEqual(el('clone-import-submit').onclick,null);assert.strictEqual(hides,0);assert.strictEqual(listeners.length,0);
''')

    def test_current_failure_keeps_same_file_for_retry_then_success_clears_callback(self):
        self.run_js(r'''
open('retry.wav');const handler=el('clone-import-submit').onclick;let pending=handler();complete(0,false);await pending;assert.strictEqual(el('clone-import-submit').onclick,handler);assert.strictEqual(el('clone-import-submit').disabled,false);assert.strictEqual(hides,0);
el('clone-import-ref-text').value='corrected transcript';pending=el('clone-import-submit').onclick();assert.strictEqual(uploads[1].form.get('file').name,'retry.wav');assert.strictEqual(uploads[1].form.get('ref_text'),'corrected transcript');complete(1);await pending;assert.strictEqual(el('clone-import-submit').onclick,null);assert.strictEqual(hides,1);assert.strictEqual(listeners.length,0);
open('closed.wav');hidden();assert.strictEqual(el('clone-import-submit').onclick,null,'dismissed unused File must also be released');
''')
