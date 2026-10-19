"""Optional workflow controls retain their original request semantics."""
from html.parser import HTMLParser
from pathlib import Path
import unittest

from tests import test_training_numbers_js as training


class DisclosureInventory(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.stack, self.ids, self.summaries = [], {}, set()
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        identifier = attrs.get('id')
        if identifier:
            self.ids.setdefault(identifier, []).append((tag, attrs, tuple(self.stack)))
        if tag == 'summary' and self.stack:
            self.summaries.add(self.stack[-1][1])
        if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:
            self.stack.append((tag, identifier))

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class WorkflowDisclosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = DisclosureInventory((Path(__file__).parent.parent/'static/index.html').read_text())

    def get_control(self, identifier):
        matches=self.page.ids[identifier]
        self.assertEqual(1,len(matches),identifier)
        return matches[0]

    def test_optional_tuning_has_one_original_control_and_run_actions_stay_outside(self):
        for detail, controls, actions in [
            ('training-tuning', ['lora-lr','lora-batch-size','lora-rank','lora-alpha','lora-grad-accum'], ['btn-lora-train','btn-lora-cancel']),
            ('voicelab-tuning', ['vl-device','vl-target-loss','vl-max-epochs','vl-lora-r','vl-candidate-checkpoints'], ['btn-vl-start','btn-vl-pause','btn-vl-cancel'])]:
            tag,attrs,_=self.get_control(detail)
            self.assertEqual('details',tag)
            self.assertNotIn('open',attrs)
            self.assertIn(detail,self.page.summaries)
            for control in controls:
                self.assertIn(('details',detail),self.get_control(control)[2])
            for action in actions:
                self.assertNotIn(('details',detail),self.get_control(action)[2])
        for control in ['lora-adapter-name','lora-dataset-select','lora-epochs','lora-language']:
            self.assertNotIn('details',[tag for tag,_ in self.get_control(control)[2]])

    def test_cast_and_recovery_keep_native_disclosures_and_live_targets(self):
        for identifier in ['series-cast-card','persona-recovery-panel','editor-line-details']:
            self.assertEqual('details',self.get_control(identifier)[0])
        for identifier in ['cast-select','cast-panel','btn-cast-apply']:
            self.assertIn(('details','series-cast-card'),self.get_control(identifier)[2])
        self.assertEqual('openCastApply()',self.get_control('btn-cast-apply')[1]['onclick'])
        for identifier in ['vl-name-apply','vl-name-overwrite']:
            self.assertNotIn('details',[tag for tag,_ in self.get_control(identifier)[2]])

    def test_real_training_builder_reads_nondefault_tuning_without_visibility_checks(self):
        # Actual shipped JS is intercepted at API.post; visibility does not
        # change the original fields read by the request builder.
        result=training.TrainingNumericRequestJsTests().run_inputs({
            'lora-rank':'16','lora-alpha':'48','lora-grad-accum':'4',
            'lora-lr':'5e-6','lora-batch-size':'2'})
        self.assertEqual((16,48,4,5e-6,2),tuple(result[k] for k in [
            'lora_r','lora_alpha','gradient_accumulation_steps','lr','batch_size']))

    def test_tab_boundaries_keep_voice_logs_and_training_controls_in_their_screen(self):
        for control,tab in [('voices-logs','voices-tab'),('lora-adapter-name','training-tab'),('vl-zips_dir','voicelab-tab')]:
            ancestors=self.get_control(control)[2]
            self.assertIn(('div',tab),ancestors)
            self.assertEqual([tab],[identifier for _,identifier in ancestors if identifier and identifier.endswith('-tab')])
