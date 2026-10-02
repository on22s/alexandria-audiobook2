"""Exercise duplicate-colored attribution prompts and bounded surround evidence."""
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest

import three_pass_generate as tp
import attribution_prompt_variants as variants
from generate_script import LLMGenParams
from tests.test_three_pass_generate import _load_orchestration_fixture_cast

if os.environ.get('THREE_PASS_CONTEXT_SOURCE'):
    spec = importlib.util.spec_from_file_location('tp_context_saved', os.environ['THREE_PASS_CONTEXT_SOURCE'])
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)
if os.environ.get('VARIANT_CONTEXT_SOURCE'):
    spec = importlib.util.spec_from_file_location('variant_context_saved', os.environ['VARIANT_CONTEXT_SOURCE'])
    variants = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(variants)


class ThreePassMissingContextTests(unittest.TestCase):
    def test_oversized_adjacent_entries_do_not_hide_shorter_surrounding_evidence(self):
        entries = [{'type':'NARRATOR','text':'Earlier.'}, {'type':'SPOKEN','text':'x' * 100},
                   {'type':'SPOKEN','text':'Target.'}, {'type':'NARRATOR','text':'x' * 100},
                   {'type':'SPOKEN','text':'After.'}]
        before = json.dumps(entries)
        surround = tp.build_window_surround(entries, [2], 12)
        self.assertEqual({'before':'Earlier.','after':'“After.”'}, surround)
        self.assertTrue(all(len(text) <= 12 for text in surround.values()))
        self.assertEqual({'before':'','after':''}, tp.build_window_surround(entries, [2], 0))
        self.assertEqual(before, json.dumps(entries))

    def test_actual_run_prompts_omitted_neighbors_and_writes_exact_source_order(self):
        source = 'Alice stood. "Yes." "Yes." She waited. "No."'
        segmented = [{'type':'NARRATOR','text':'Alice stood.'}, {'type':'SPOKEN','text':'Yes.'},
                     {'type':'SPOKEN','text':'Yes.'}, {'type':'NARRATOR','text':'She waited.'},
                     {'type':'SPOKEN','text':'No.'}]
        seen = []

        def create(**kwargs):
            if not seen:
                seen.append(('segment', []))
                response = segmented
            else:
                prompt = kwargs['messages'][-1]['content']
                match = re.search(r'\[\{"n":', prompt)
                self.assertIsNotNone(match, prompt)
                entries = json.JSONDecoder().raw_decode(prompt[match.start():])[0]
                stage = 'attribute' if 'type' in entries[0] else 'instruct'
                seen.append((stage, entries))
                response = [{'n':e['n'], 'head':' '.join(e['text'].split()[:3]),
                             **({'speaker':'NARRATOR' if e['type'] == 'NARRATOR' else 'ALICE'}
                                if stage == 'attribute' else {'instruct':'Plain.'})}
                            for e in entries]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response)), finish_reason='stop')], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'script.json'
            result = tp.run_three_pass(client, 'fixture', source, LLMGenParams(max_tokens=500, structured_output='off'),
                                       chunk_size=6000, output_path=str(output),
                                       cast=_load_orchestration_fixture_cast(('ALICE',)))
            attributed = [entries for stage, entries in seen if stage == 'attribute']
            self.assertEqual(2, len(attributed))
            self.assertEqual(['Alice stood.','Yes.','She waited.','No.'], [e['text'] for e in attributed[0]])
            self.assertIn('next_context', attributed[0][1])
            self.assertEqual(segmented[2], attributed[0][1]['next_context'])
            self.assertEqual(segmented[2], attributed[0][2]['previous_context'])
            self.assertEqual(segmented[1], attributed[1][0]['previous_context'])
            self.assertEqual(segmented[3], attributed[1][0]['next_context'])
            self.assertNotIn('previous_context', attributed[0][0], 'included narration must not be repeated as neighbor context')
            self.assertEqual([e['text'] for e in segmented], [e['text'] for e in result])
            self.assertEqual(['NARRATOR','ALICE','ALICE','NARRATOR','ALICE'], [e['speaker'] for e in result])
            checkpoint = json.loads(Path(tp.three_pass_checkpoint_path(str(output))).read_text())
            self.assertEqual(result, checkpoint['annotated'])

    def test_passage_variants_keep_spoken_evidence_unmarked_and_do_not_repeat_included_entries(self):
        entries = [{'type':'SPOKEN','text':'Yes.'}, {'type':'SPOKEN','text':'Yes.'}, {'type':'NARRATOR','text':'She waited.'}]
        # Build the actual orchestration context, not a handcrafted provider-only fixture.
        contexts = tp.get_missing_attribute_contexts(entries, [1], 25)
        text = variants.passage_text([entries[1]], contexts)
        self.assertEqual('"Yes."\n\n|0|"Yes."|0|\n\nShe waited.', text)
        for variant in ('passage','michel','michel2','michel2_full'):
            _, user = variants.build_variant_request(variant, [entries[1]], LLMGenParams(max_tokens=500), ['ALICE'], neighbor_contexts=contexts, surround={'before':'','after':''})
            self.assertIn('"Yes."\n\n|0|"Yes."|0|', user)
            self.assertEqual(1, user.count('|0|"Yes."|0|'))
        self.assertEqual([{}, {}, {}], tp.get_missing_attribute_contexts(entries, [0,1,2], 25))
        self.assertEqual([{}], tp.get_missing_attribute_contexts(entries, [2], 2), 'evidence outside fixed source window stays controlled by surround knob')
