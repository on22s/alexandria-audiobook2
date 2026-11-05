"""Guides use real declared destinations and never dispatch a job."""
import json
from pathlib import Path
import re
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent/'static'
SETUP = r'''
Element.prototype.append=function(...nodes){for(const node of nodes){this.appendChild(node);}};
Element.prototype.replaceChildren=function(...nodes){for(const node of [...this.children]){node.remove();}this.append(...nodes);};
Element.prototype.querySelector=function(s){return this.querySelectorAll(s)[0]||null;};
Element.prototype.focus=function(){context.focused=this.id;};Element.prototype.scrollIntoView=function(){};
for(const id of ['task-guide-cards','task-guide-detail','task-guide-return','task-guide-title','task-guide-image','task-guide-step-title','task-guide-instruction','task-guide-expected','task-guide-cost','task-guide-prev','task-guide-next','task-guide-feedback','home-tab','setup-tab','settings-tts','script-tab','script-single-area','voices-tab','editor-tab','audio-tab','preparer-tab','dataset-builder-tab','training-tab','chapter-options','chapter-template','btn-merge','btn-batch-fast']){body.appendChild(new Element('div',{id}));}
const navigation=[],opened=[],warnings=[];let pending=false;
context.currentTabName='home';context.activateTab=tab=>{navigation.push(tab);context.currentTabName=tab;};context.openConfigField=target=>opened.push(target.id);context.isEditorEditPending=()=>pending;context.showToast=(...args)=>warnings.push(args);context.API={post(){throw new Error('guide must not POST');}};
load();
'''

class TaskGuideTests(unittest.TestCase):
    def run_js(self, code):
        result=subprocess.run(['node','-e',DOM_FIXTURE+SETUP+code,str(STATIC/'js/app-guides.js')],capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        return result.stdout

    def test_every_guide_target_and_illustration_is_bundled(self):
        output=self.run_js("console.log(JSON.stringify(vm.runInContext('TASK_GUIDES',context)));")
        html=(STATIC/'index.html').read_text(encoding='utf-8');guides=json.loads(output)
        self.assertEqual(5,len(guides))
        self.assertEqual(['first-book','single-narrator','repair','dataset','export'],[guide['id'] for guide in guides])
        single=next(guide for guide in guides if guide['id']=='single-narrator')
        # Preview copy must state truthfully that already-authorized pending
        # card edits may be saved by preview (#1724 C126).
        self.assertIn('may save your already-authorized pending card edits', single['steps'][2][5])
        self.assertNotIn('Preview writes nothing', single['steps'][2][5])
        self.assertEqual(['script','script','voices','editor','editor','editor'],[step[1] for step in single['steps']])
        self.assertEqual(['script-single-area','script-tab','voices-tab','editor-tab','btn-batch-fast','btn-merge'],[step[2] for step in single['steps']])
        # The flow must be complete and explicit: the sample step says the
        # rest comes next, the batch step names the real Render Pending
        # button and its cancel/failure handling, and Merge All is only
        # reached after line audio exists.
        self.assertIn('remaining lines are generated in the next step', single['steps'][3][3])
        self.assertIn('Render Pending', single['steps'][4][3])
        self.assertIn('Cancel', single['steps'][4][3])
        self.assertIn('only combines chunks that already have audio', single['steps'][4][3])
        self.assertIn('Merge All', single['steps'][5][3])
        for guide in guides:
            image=STATIC/'guides'/f"{guide['image']}.svg"
            self.assertTrue(image.is_file());self.assertNotIn('http',image.read_text(encoding='utf-8').replace('http://www.w3.org/2000/svg',''))
            for _,tab,target,*_rest in guide['steps']:
                self.assertRegex(html,r'id="'+re.escape(tab+'-tab')+'"')
                self.assertRegex(html,r'id="'+re.escape(target)+'"')

    def test_navigation_return_steps_and_unknown_target_are_read_only(self):
        self.run_js(r'''
assert.equal(document.getElementById('task-guide-cards').children.length,5);
context.openTaskGuide('first-book');context.onTaskGuideStep(1);context.openTaskGuideTarget();assert.deepEqual(navigation,['script']);assert.deepEqual(opened,['script-tab']);
context.returnToTaskGuide();assert.equal(context.currentTabName,'home');assert(document.getElementById('task-guide-step-title').textContent.startsWith('2 of 4'));
context.openTaskGuide('missing');assert.equal(warnings.length,1);context.onTaskGuideStep(99);assert.equal(warnings.length,2);
document.getElementById('script-tab').remove();const count=navigation.length;context.openTaskGuideTarget();assert.equal(navigation.length,count);assert(document.getElementById('task-guide-feedback').textContent.includes('unavailable'));
context.closeTaskGuide();assert(document.getElementById('task-guide-return').hidden);assert(document.getElementById('task-guide-detail').hidden);
''')

    def test_pending_editor_edits_block_navigation_without_saving_or_discarding(self):
        self.run_js(r'''
context.openTaskGuide('repair',1);pending=true;context.openTaskGuideTarget();assert.equal(navigation.length,0);assert.equal(opened.length,0);assert(document.getElementById('task-guide-feedback').textContent.includes('pending'));
pending=false;context.openTaskGuideTarget();assert.deepEqual(navigation,['voices']);assert.deepEqual(opened,['voices-tab']);
''')

    def test_merge_navigation_preserves_keyboard_action_and_pending_return(self):
        self.run_js(r'''
const merge=document.getElementById('btn-merge');merge.tagName='BUTTON';context.openTaskGuide('export',1);context.openTaskGuideTarget();assert.equal(merge.hasAttribute('tabindex'),false);assert.deepEqual(opened,['btn-merge']);pending=true;const count=navigation.length;context.returnToTaskGuide();assert.equal(navigation.length,count);assert.equal(warnings.length,1);
''')

    def test_single_narrator_uses_generic_guide_navigation_without_interception(self):
        # #1724 simplification: the single-narrator checklist uses the generic
        # openTaskGuideTarget path only. No panel opener, no tab interception,
        # no POST/inference, and navigation never writes the literal checkbox.
        # Final step targets Editor btn-merge (then instructs Audio export).
        self.run_js(r'''
context.openTaskGuide('single-narrator',0);context.openTaskGuideTarget();
assert.deepEqual(navigation,['script']);assert.deepEqual(opened,['script-single-area']);
context.openTaskGuide('single-narrator',2);context.openTaskGuideTarget();
assert.deepEqual(navigation,['script','home','voices']);assert.deepEqual(opened,['script-single-area','voices-tab']);
context.openTaskGuide('single-narrator',4);context.openTaskGuideTarget();
assert.deepEqual(navigation,['script','home','voices','home','editor']);assert.equal(opened[2],'btn-batch-fast');
context.openTaskGuide('single-narrator',5);context.openTaskGuideTarget();
assert.deepEqual(navigation,['script','home','voices','home','editor','home','editor']);assert.equal(opened[3],'btn-merge');
''')

    def test_single_narrator_hidden_target_gives_visible_feedback(self):
        # DS040: in batch mode the single-book area is display:none. The
        # guide must not silently aim at a hidden pane: it gives visible
        # feedback with an instruction and does not navigate/changes book.
        self.run_js(r'''
document.getElementById('script-single-area').style.display='none';
context.openTaskGuide('single-narrator',0);context.openTaskGuideTarget();
assert.deepEqual(navigation,[],'hidden target must not activate a tab as success');
assert.deepEqual(opened,[],'hidden target must not be opened as success');
assert(document.getElementById('task-guide-feedback').textContent.includes('hidden'),'feedback must name the hidden pane');
assert(document.getElementById('task-guide-feedback').textContent.includes('current mode'),'feedback must be a useful instruction');
''')
