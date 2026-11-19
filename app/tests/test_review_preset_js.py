"""Issue #2079: Review prompt presets use the shared pass-preset controls.

Covers render, switch, edit-then-save, delete, Default restoring the checked-in
prompts, and unsaved edits being treated as an edited Default. All text is
synthetic; the harness fakes the DOM and the config API.
"""
import unittest

from tests import test_prompt_preset_transactions_js as actions


SEED = r'''
const DEFAULT={system_prompt:'synthetic checked-in system',user_prompt:'Context {context}\nEntries {batch}'};
const CUSTOM={name:'Synthetic review',description:'why',system_prompt:'synthetic custom system',user_prompt:'Custom context {context}\nCustom entries {batch}'};
// Do not seed passPromptDefaults.review: the app must supply the table entry
// itself. A build without a review pass throws here instead of silently
// creating the keys on its dynamic objects.
run('renderPassPromptPresets("review",'+JSON.stringify([CUSTOM])+',"Synthetic review");');
function setDefaults(){run('passPromptDefaults.review='+JSON.stringify(DEFAULT)+';');}
'''


class ReviewPresetJsTests(unittest.TestCase):
    def run_case(self, code):
        return actions.PromptPresetTransactionTests().run_case(SEED + code)

    def test_render_switch_to_default_and_edit_detection(self):
        self.run_case(r'''
const select=element('review-prompt-preset-select');
assert.strictEqual(run('activePassPromptPreset.review'),'Synthetic review');
assert.strictEqual(select.value,'0','the saved preset is selected');
assert.strictEqual(element('review-system-prompt').value,CUSTOM.system_prompt);
assert.strictEqual(element('review-user-prompt').value,CUSTOM.user_prompt);

setDefaults();
select.value='-1';await select.onchange();
assert.strictEqual(run('activePassPromptPreset.review'),'default');
assert.strictEqual(element('review-system-prompt').value,DEFAULT.system_prompt);
assert.strictEqual(element('review-user-prompt').value,DEFAULT.user_prompt);
assert.strictEqual(context.buildConfigPayload(2).prompts.review_user_prompt,DEFAULT.user_prompt,'Default is what Setup saves for review_script');

element('review-user-prompt').value='Edited {context}\nEdited {batch}';
let payload=context.getPassPromptPresetPayload('review');
assert.strictEqual(payload.active,'default (edited)');
assert.strictEqual(payload.own.find(p=>p.name==='default (edited)').user_prompt,'Edited {context}\nEdited {batch}');
element('review-user-prompt').value=DEFAULT.user_prompt;
assert.strictEqual(context.getPassPromptPresetPayload('review').active,'default');
const clean=context.getPassPromptPresetPayload('review');
assert.strictEqual(clean.active,'default');
assert.strictEqual(clean.own.filter(p=>p.name==='default (edited)').length,0,'an untouched Default adds no edited preset');

let decisions=0;context.showConfirm=()=>{decisions++;return true;};
element('review-system-prompt').value='Unsaved synthetic edit';
select.value='0';await select.onchange();
assert.strictEqual(decisions,1,'switching away from edits asks once');
assert.strictEqual(element('review-system-prompt').value,CUSTOM.system_prompt);
assert.strictEqual(run('activePassPromptPreset.review'),'Synthetic review');
''')

    def test_save_and_delete_write_the_config_fields_review_script_reads(self):
        self.run_case(r'''
mode='success';
setDefaults();
const system='Saved synthetic system',user='Saved context {context}\nSaved entries {batch}';
element('review-system-prompt').value=system;
element('review-user-prompt').value=user;
context.showPresetEditor=async()=>({name:'Another review',description:'another reason'});
await context.window.savePassPromptPreset('review');
assert.strictEqual(run('activePassPromptPreset.review'),'Another review');
assert.strictEqual(persisted.prompts.review_preset,'Another review');
const saved=persisted.prompts.review_prompt_presets.find(p=>p.name==='Another review');
assert.strictEqual(saved.system_prompt,system);
assert.strictEqual(saved.user_prompt,user);
assert.strictEqual(persisted.prompts.review_system_prompt,system,'review_script reads this exact field');
assert.strictEqual(persisted.prompts.review_user_prompt,user,'review_script reads this exact field');

context.showConfirm=async()=>true;
await context.window.deletePassPromptPreset('review');
assert.strictEqual(run('activePassPromptPreset.review'),'default');
assert.strictEqual(persisted.prompts.review_preset,'default');
assert(!persisted.prompts.review_prompt_presets.some(p=>p.name==='Another review'));
assert.strictEqual(element('review-system-prompt').value,DEFAULT.system_prompt,'delete restores the checked-in system prompt');
assert.strictEqual(element('review-user-prompt').value,DEFAULT.user_prompt,'delete restores the checked-in user prompt');
''')

    def test_failed_write_keeps_review_cache_and_editor_text(self):
        self.run_case(r'''
mode='fail';
element('review-system-prompt').value='Unsaved synthetic text';
const before=snapshot(),errors=toasts.length;
await context.window.savePassPromptPreset('review');
assert.strictEqual(snapshot(),before);
assert.strictEqual(element('review-system-prompt').value,'Unsaved synthetic text');
assert.strictEqual(toasts.length,errors+1);
assert.strictEqual(toasts.at(-1)[1],'error');
assert.strictEqual(persisted,null);
''')


if __name__ == "__main__":
    unittest.main()
