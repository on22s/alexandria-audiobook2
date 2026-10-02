"""Run the actual dialog helpers with delayed modal transitions and shared buttons."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class ConfirmationOwnershipJsTest(unittest.TestCase):
    def run_js(self, scenario):
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
class Element {
  constructor(){this.listeners=new Map();this.textContent='';this.removed=false;}
  addEventListener(event,fn){if(!this.listeners.has(event)){this.listeners.set(event,new Set());}this.listeners.get(event).add(fn);}
  removeEventListener(event,fn){this.listeners.get(event)?.delete(fn);}
  emit(event){for(const fn of [...(this.listeners.get(event)||[])]){if(this.listeners.get(event).has(fn)){fn();}}}
  remove(){this.removed=true;}
}
const elements=Object.fromEntries(['confirmModal','confirmModalBody','confirmModalOk','confirmModalCancel'].map(id=>[id,new Element()]));
const modals=[],toasts=[],allToasts=[];
elements['toast-container']={insertAdjacentHTML(position,html){const id=html.match(/id="([^"]+)"/)[1];const element=new Element();allToasts.push({id,element});if(!elements[id]){elements[id]=element;}}};
let failShow=false;
let delayShow=false;
const context={document:{getElementById(id){return elements[id];}},Date:{now:()=>123},escapeHtml:value=>value,
 bootstrap:{Modal:class {constructor(el){this.el=el;this.hidden=false;this.disposed=false;modals.push(this);}
 show(){if(failShow){failShow=false;throw new Error('show failed');}this.shown=!delayShow;if(this.shown){this.el.emit('shown.bs.modal');}}
 hide(){if(this.shown){this.hidden=true;}}
 dispose(){this.disposed=true;}},Toast:class {constructor(el){toasts.push(el);}show(){}}}};
vm.runInNewContext(source.slice(0,source.indexOf('// Big/long-running jobs')),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const click=button=>elements[button].emit('click');
const hidden=()=>elements.confirmModal.emit('hidden.bs.modal');
const results=[];
const ask=message=>context.showConfirm(message).then(value=>results.push([message,value]));
(async()=>{
 const mode=process.argv[2];
 if(mode==='queue'){
   ask('delete book A');ask('delete book B');ask('replace voice C');await tick();
   assert.equal(elements.confirmModalBody.textContent,'delete book A');assert.equal(modals.length,1);
   click('confirmModalOk');click('confirmModalOk');click('confirmModalCancel');await tick();
   assert.deepEqual(results,[]);assert.equal(modals.length,1,'second prompt waits for hidden transition');
   hidden();await tick();assert.deepEqual(results,[['delete book A',true]]);
   assert.equal(elements.confirmModalBody.textContent,'delete book B');assert.equal(modals.length,2);
   click('confirmModalCancel');hidden();await tick();
   assert.deepEqual(results,[['delete book A',true],['delete book B',false]]);
   assert.equal(elements.confirmModalBody.textContent,'replace voice C');
   hidden();await tick();assert.deepEqual(results,[['delete book A',true],['delete book B',false],['replace voice C',false]]);
   assert.ok(modals.every(modal=>modal.disposed));
   for(const element of [elements.confirmModal,elements.confirmModalOk,elements.confirmModalCancel]){
     assert.ok([...element.listeners.values()].every(listeners=>listeners.size===0),'all listeners cleaned');
   }
 }else if(mode==='dismiss'){
   ask('first');ask('second');await tick();hidden();await tick();
   assert.deepEqual(results,[['first',false]]);assert.equal(elements.confirmModalBody.textContent,'second');
   click('confirmModalOk');hidden();await tick();assert.deepEqual(results,[['first',false],['second',true]]);
 }else if(mode==='failure'){
   failShow=true;const failed=context.showConfirm('broken').then(()=>{throw new Error('expected rejection');},error=>assert.equal(error.message,'show failed'));
   ask('next');await failed;await tick();assert.equal(elements.confirmModalBody.textContent,'next');
   click('confirmModalOk');hidden();await tick();assert.deepEqual(results,[['next',true]]);
   assert.ok(modals.every(modal=>modal.disposed));
 }else if(mode==='early'){
   delayShow=true;ask('early');ask('later');await tick();click('confirmModalOk');await tick();
   assert.deepEqual(results,[]);assert.equal(modals.length,1);assert.ok(!modals[0].hidden);
   modals[0].shown=true;elements.confirmModal.emit('shown.bs.modal');
   assert.ok(modals[0].hidden,'early decision hides once opening transition completes');
   delayShow=false;hidden();await tick();assert.deepEqual(results,[['early',true]]);
   assert.equal(elements.confirmModalBody.textContent,'later');hidden();await tick();
   assert.deepEqual(results,[['early',true],['later',false]]);
 }else if(mode==='toast'){
   context.showToast('one');context.showToast('two');
   assert.equal(new Set(allToasts.map(item=>item.id)).size,2);
   assert.equal(toasts[0],allToasts[0].element);assert.equal(toasts[1],allToasts[1].element);
   toasts[0].emit('hidden.bs.toast');assert.ok(toasts[0].removed);assert.ok(!toasts[1].removed);
   toasts[1].emit('hidden.bs.toast');assert.ok(toasts[1].removed);
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(SOURCE), scenario],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_overlapping_confirmations_wait_for_individual_decisions_and_hide(self):
        self.run_js('queue')

    def test_escape_or_backdrop_dismissal_cancels_only_the_visible_prompt(self):
        self.run_js('dismiss')

    def test_show_failure_cleans_listeners_and_allows_the_next_prompt(self):
        self.run_js('failure')

    def test_same_millisecond_toasts_keep_distinct_elements_and_cleanup(self):
        self.run_js('toast')

    def test_click_during_opening_transition_waits_for_shown_before_hiding(self):
        self.run_js('early')
