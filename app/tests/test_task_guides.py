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
for(const id of ['task-guide-cards','task-guide-detail','task-guide-return','task-guide-title','task-guide-image','task-guide-step-title','task-guide-instruction','task-guide-expected','task-guide-cost','task-guide-prev','task-guide-next','task-guide-feedback','home-tab','setup-tab','settings-tts','script-tab','script-single-area','voices-tab','editor-tab','audio-tab','preparer-tab','dataset-builder-tab','training-tab','chapter-options','chapter-template','btn-merge']){body.appendChild(new Element('div',{id}));}
const navigation=[],opened=[],warnings=[];let pending=false;
context.currentTabName='home';context.activateTab=tab=>{navigation.push(tab);context.currentTabName=tab;};context.openConfigField=target=>opened.push(target.id);context.isEditorEditPending=()=>pending;context.showToast=(...args)=>warnings.push(args);context.API={post(){throw new Error('guide must not POST');}};
load();
'''

class TaskGuideTests(unittest.TestCase):
    def run_js(self, code):
        result=subprocess.run(['node','-e',DOM_FIXTURE+SETUP+code,str(STATIC/'js/app-guides.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        return result.stdout

    def test_every_guide_target_and_illustration_is_bundled(self):
        output=self.run_js("console.log(JSON.stringify(vm.runInContext('TASK_GUIDES',context)));")
        html=(STATIC/'index.html').read_text();guides=json.loads(output)
        self.assertEqual(4,len(guides))
        for guide in guides:
            image=STATIC/'guides'/f"{guide['image']}.svg"
            self.assertTrue(image.is_file());self.assertNotIn('http',image.read_text().replace('http://www.w3.org/2000/svg',''))
            for _,tab,target,*_rest in guide['steps']:
                self.assertRegex(html,r'id="'+re.escape(tab+'-tab')+'"')
                self.assertRegex(html,r'id="'+re.escape(target)+'"')

    def test_navigation_return_steps_and_unknown_target_are_read_only(self):
        self.run_js(r'''
assert.equal(document.getElementById('task-guide-cards').children.length,4);
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
