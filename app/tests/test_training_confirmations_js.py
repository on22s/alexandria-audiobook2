"""Actual shared modal and destructive Training callbacks, delayed user decisions."""
import os
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(os.environ.get('TRAINING_CONFIRM_SOURCE', Path(__file__).resolve().parent.parent / 'static/js/app-training.js'))
CORE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
SETUP = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),core=fs.readFileSync(process.argv[2],'utf8');
class Element {constructor(){this.listeners=new Map();this.textContent='';this.classList={toggle(){}};}addEventListener(event,fn){if(!this.listeners.has(event)){this.listeners.set(event,new Set());}this.listeners.get(event).add(fn);}removeEventListener(event,fn){this.listeners.get(event)?.delete(fn);}emit(event){for(const fn of [...(this.listeners.get(event)||[])]){if(this.listeners.get(event).has(fn)){fn();}}}}
const elements=Object.fromEntries(['confirmModalTitle','confirmModal','confirmModalBody','confirmModalOk','confirmModalCancel','lora-comparison-panel'].map(id=>[id,new Element()])),modals=[],calls=[],toasts=[];let loads=0,history=0;
const ctx={window:null,document:{getElementById:id=>elements[id]},bootstrap:{Modal:class{constructor(el){this.el=el;modals.push(this);}show(){this.el.emit('shown.bs.modal');}hide(){}dispose(){this.disposed=true;}}},
confirm(){throw Error('native confirm must never be used');},API:{post:async(url,data)=>{calls.push({url,data});return {freed_bytes:2048,removed_count:2};},_handleError:async response=>assert(response.ok)},fetch:async(url,request)=>{calls.push({url,method:request.method});return {ok:true};}};ctx.window=ctx;vm.createContext(ctx);
vm.runInContext(core.slice(0,core.indexOf('async function confirmIfRemote(')),ctx);vm.runInContext(source,ctx);
ctx.showToast=(...args)=>toasts.push(args);ctx.loadLoraModels=async()=>loads++;ctx.openLoraReviewHistory=()=>history++;
const tick=()=>new Promise(setImmediate),click=id=>elements[id].emit('click'),hidden=()=>elements.confirmModal.emit('hidden.bs.modal');
let finished=false;process.on('beforeExit',()=>assert(finished,'all dialog assertions must finish'));
"""


class TrainingConfirmationsJsTests(unittest.TestCase):
    def run_js(self, body):
        code=SETUP+'\n(async()=>{\n'+body+'\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result=subprocess.run(['node','-e',code,str(SOURCE),str(CORE)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_every_action_waits_for_shared_modal_and_cancel_prevents_requests(self):
        self.run_js(r"""
const id='voice # ? 日本語',base='/api/lora/models/'+encodeURIComponent(id);
const actions=[['clearLoraReviewHistory','/reviews/cleanup','Delete all human review history'],['promoteLoraCandidate','/promote','Promote candidate-1'],['rollbackLoraPromotion','/rollback-promotion','Restore the production checkpoint'],['recoverLoraCheckpointSwap','/recover-checkpoint-swap','Recover production'],['deleteLoraRollbackBackup','/rollback-backup','Permanently delete this rollback backup']];
for(const [name,suffix,message] of actions){
 const before=calls.length;let pending=ctx[name](id,'candidate-1');await tick();assert.strictEqual(calls.length,before);assert(elements.confirmModalBody.textContent.includes(message));click('confirmModalCancel');hidden();await pending;assert.strictEqual(calls.length,before,'cancel prevents mutation');
 pending=ctx[name](id,'candidate-1');await tick();assert.strictEqual(calls.length,before);click('confirmModalOk');await tick();assert.strictEqual(calls.length,before,'request waits for hidden transition');hidden();await pending;
 assert.strictEqual(calls.length,before+1);const call=calls.at(-1);assert.strictEqual(call.url,base+suffix);if(name==='promoteLoraCandidate'){assert.strictEqual(call.data.expected_candidate_id,'candidate-1');}else if(name==='deleteLoraRollbackBackup'){assert.strictEqual(call.method,'DELETE');}else{assert.strictEqual(JSON.stringify(call.data),'{}');}
}
assert.strictEqual(loads,4);assert.strictEqual(history,1);assert.strictEqual(toasts.length,5);assert(modals.every(m=>m.disposed));
""")

    def test_overlapping_training_actions_keep_separate_confirmations(self):
        self.run_js(r"""
const first=ctx.promoteLoraCandidate('one','candidate-1'),second=ctx.deleteLoraRollbackBackup('two');await tick();assert.strictEqual(modals.length,1);assert(elements.confirmModalBody.textContent.includes('candidate-1'));assert.strictEqual(calls.length,0);
click('confirmModalOk');hidden();await first;await tick();assert.strictEqual(calls.length,1);assert.strictEqual(calls[0].url,'/api/lora/models/one/promote');assert.strictEqual(modals.length,2);assert(elements.confirmModalBody.textContent.includes('Permanently delete'));hidden();await second;assert.strictEqual(calls.length,1,'backdrop dismissal cancels second action');assert.strictEqual(loads,1);
""")

    def test_missing_promotion_identity_does_not_prompt_or_mutate(self):
        self.run_js(r"""
for(const id of [undefined,null,'','  ']){await ctx.promoteLoraCandidate('one',id);}
assert.strictEqual(modals.length,0);assert.strictEqual(calls.length,0);assert.strictEqual(toasts.length,4);assert(toasts.every(t=>t[1]==='warning'));
""")
