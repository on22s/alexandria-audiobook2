"""Known-answer controls for advanced persona sample fidelity and grounding."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import generate_personas as personas
from persona_validation import get_reference_samples, validate_compiled_persona_payload

class PersonaGroundingTests(unittest.TestCase):
    def test_exact_copy_accepted_and_paraphrases_stitching_rejected(self):
        samples = ['Leave the lever alone!', 'Go back to work.']
        self.assertEqual(validate_compiled_persona_payload({'description': 'Dry voice.', 'ref_text': samples[0]}, samples)['ref_text'], samples[0])
        for text in ['Do not touch that lever!', ' '.join(samples), 'Invented words.']:
            with self.assertRaisesRegex(ValueError, 'exactly copy'):
                validate_compiled_persona_payload({'description': 'Dry voice.', 'ref_text': text}, samples)

    def test_long_sample_is_source_excerpt_and_input_unchanged(self):
        ref = {'sample_lines': ['Leave the lever alone! ' + 'Long dialogue. ' * 400]}
        before = copy.deepcopy(ref)
        self.assertEqual(get_reference_samples(ref), ['Leave the lever alone!'])
        self.assertEqual(ref, before)

    def test_observation_dialogue_allowed_narration_not_allowed(self):
        ref = {'observations': [{'sample_lines': ['Go back to work.'], 'evidence': [{'quote': 'A room.'}]}]}
        self.assertEqual(get_reference_samples(ref), ['Go back to work.'])
        self.assertEqual(get_reference_samples({'sample_lines': ['', 'bad\x00sample']}), [])

    def compile(self, ref, reply, preview, advanced_prompt='{character_ref}', **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            refs = Path(tmp) / 'refs'; refs.mkdir()
            Path(personas._character_ref_path(str(refs), 'ALICE')).write_text(json.dumps(ref))
            with patch.object(personas, 'call_llm_for_object', side_effect=reply):
                return personas._compile_persona(object(), 'fixture', object(), {}, tmp, str(refs),
                    'ALICE', {}, None, advanced_prompt, preview_saver=preview, **kwargs)

    def test_source_and_merge_receive_same_rules_and_literal_sample(self):
        calls = []
        def reply(*args, **kwargs):
            calls.append((args[2], args[3]))
            payload = {'description': 'Dry voice.', 'ref_text': 'Leave the lever alone!'}
            kwargs['validate_object'](payload)
            return payload
        self.assertTrue(self.compile({'name': 'ALICE', 'features': ['x' * 900 for _ in range(15)],
            'sample_lines': ['Leave the lever alone!']}, reply, lambda *args: True))
        self.assertEqual(len(calls), 3)
        self.assertTrue(any('Supported partial persona drafts' in p for _, p in calls))
        for system, prompt in calls:
            self.assertIn(personas.PERSONA_GROUNDING_RULES, system)
            self.assertIn('Leave the lever alone!', prompt)
            self.assertIn(personas.PERSONA_CUE_INSTRUCTIONS, prompt)
            self.assertLess(system.index('Unknown, unspecified'), system.index('Use only supported speaker traits'))

    def test_exhausted_invalid_result_does_not_publish_preview(self):
        from unittest.mock import Mock
        preview = Mock()
        with self.assertRaises(personas.PersonaContextRecoveryError):
            self.compile({'name': 'ALICE', 'sample_lines': ['Hello.']}, lambda *a, **k: None, preview)
        preview.assert_not_called()

    def test_invented_compiled_sample_does_not_publish_preview(self):
        from unittest.mock import Mock
        preview = Mock()
        with self.assertRaises(personas.PersonaContextRecoveryError):
            self.compile({'name': 'ALICE', 'sample_lines': ['Hello.']},
                lambda *a, **k: {'description': 'Dry voice.', 'ref_text': 'Invented words.'}, preview)
        preview.assert_not_called()

    def test_missing_source_sample_fails_without_request_or_preview(self):
        from unittest.mock import Mock
        request, preview = Mock(), Mock()
        with self.assertRaisesRegex(personas.PersonaContextRecoveryError, 'No safe source'):
            self.compile({'name': 'ALICE', 'voice_clues': ['Dry voice.']}, request, preview)
        request.assert_not_called()
        preview.assert_not_called()

    def test_default_templates_request_supported_traits_without_required_slots(self):
        for prompt in (personas._compile_character_prompt({'name': 'ALICE'}),
                       personas.PERSONA_ADVANCED_PROMPT):
            self.assertIn('only traits explicitly supported in the reference', prompt)
            self.assertIn('Omit unknown traits rather than supplying defaults', prompt)
            self.assertNotIn('covering apparent age/gender if inferable', prompt)

    def test_source_cues_keep_literal_independent_ids_and_leave_input_unchanged(self):
        ref = {'name': 'ALICE', 'voice_clues': ['Accent unspecified.', 'Shouts in danger.', 'Shouts in danger.'],
               'features': ['One observation calls her young.', 'Another calls her elderly.']}
        before = copy.deepcopy(ref)
        cues = personas.get_source_persona_cues(personas.get_selected_character_reference(ref))
        self.assertEqual([item['id'] for item in cues['items']],
                         ['voice_clues:0', 'voice_clues:1', 'features:0', 'features:1'])
        self.assertEqual('Accent unspecified.', cues['items'][0]['text'])
        self.assertEqual('Another calls her elderly.', cues['items'][-1]['text'])
        self.assertEqual(ref, before)

    def test_oversized_cue_is_reported_without_clipping_or_losing_later_cue(self):
        ref = {'name': 'ALICE', 'voice_clues': ['x' * 5000, 'Shouts in danger.'], 'features': []}
        cues = personas.get_source_persona_cues(ref)
        self.assertEqual(['voice_clues:0'], cues['omitted_ids'])
        self.assertEqual([{'id': 'voice_clues:1', 'text': 'Shouts in danger.'}], cues['items'])
        self.assertLessEqual(len(json.dumps(cues['items'], ensure_ascii=False)), 4000)

    def test_late_cue_reaches_every_source_and_merge_within_original_allowance(self):
        calls = []
        def reply(*args, **kwargs):
            calls.append(args[3])
            payload = json.JSONDecoder().raw_decode(args[3])[0]
            self.assertIn({'id': 'voice_clues:11', 'text': 'Shouts in danger.'},
                          payload['source_cue_ledger']['items'])
            self.assertLessEqual(len(args[3]), 12000 + len(json.dumps({'name': 'ALICE'})))
            self.assertIn('Do not pair facts from separate source IDs', args[2])
            self.assertIn('never means absent, neutral', args[2])
            return {'description': 'Dry voice; shouts in danger.', 'ref_text': 'Hello.'}
        ref = {'name': 'ALICE', 'voice_clues': ['Soft delivery.'] * 11 + ['Shouts in danger.'],
               'features': ['x' * 900 for _ in range(15)], 'sample_lines': ['Hello.']}
        self.assertTrue(self.compile(ref, reply, lambda *a: True))
        self.assertTrue(any('Supported partial persona drafts' in p for p in calls))

    def test_repeated_source_cues_still_respect_small_context_completion_budget(self):
        calls = []
        def reply(*args, **kwargs):
            calls.append(args[3])
            self.assertGreaterEqual(personas.get_effective_max_tokens(600, 4096,
                [{'content': args[2]}, {'content': args[3]}], scale_to_context=False), 600)
            return {'description': 'Dry voice.', 'ref_text': 'Hello.'}
        ref = {'name': 'ALICE', 'voice_clues': ['cue %d ' % i + 'x' * 120 for i in range(20)],
               'features': ['feature %d ' % i + 'y' * 140 for i in range(20)],
               'sample_lines': ['Hello.']}
        self.assertTrue(self.compile(ref, reply, lambda *a: True, context_length=4096))
        self.assertGreater(len(calls), 1)

    def test_long_sample_and_full_ledger_recover_without_losing_source_cues(self):
        ref = {'name': 'ALICE', 'voice_clues': ['cue %d ' % i + 'x' * 160 for i in range(25)],
               'sample_lines': ['Long source dialogue ' * 140]}
        delivered = []
        calls = []
        def reply(*args, **kwargs):
            prompt = args[3]
            calls.append(prompt)
            self.assertGreaterEqual(personas.get_effective_max_tokens(600, 4096,
                [{'content': args[2]}, {'content': prompt}], scale_to_context=False), 600)
            payload = json.JSONDecoder().raw_decode(prompt.split('Character reference:\n')[-1])[0]
            self.assertTrue(ref['sample_lines'][0].strip().startswith(payload['reference_sample']))
            kwargs['validate_object']({'description': 'Dry voice.', 'ref_text': payload['reference_sample']})
            if 'Supported partial persona drafts' not in prompt:
                delivered.extend(payload.get('voice_clues', []))
            return {'description': 'Dry voice.', 'ref_text': payload['reference_sample']}
        self.assertTrue(self.compile(ref, reply, lambda *a: True, context_length=4096,
                                     advanced_prompt=None))
        self.assertCountEqual(delivered, ref['voice_clues'])
        self.assertTrue(any('Supported partial persona drafts' in p for p in calls))

    def test_ordinary_discovery_rejects_invented_and_other_speaker_samples(self):
        batch = [{'speaker': 'Alice', 'text': 'Leave the lever alone!'},
                 {'speaker': 'BOB', 'text': 'Go back to work.'},
                 {'speaker': 'Narrator', 'text': 'The room was quiet.'}]
        response = {'ALICE': {'features': ['Dry voice.'], 'sample_lines': [
            'Leave the lever alone!', 'Invented dialogue.', 'Go back to work.', 'The room was quiet.']}}
        with patch.object(personas, 'call_llm_for_object', return_value=response):
            characters = personas._discover_batch_characters(
                object(), 'fixture', 'prompt', batch, 1, allowed_speakers=['ALICE', 'BOB'])
        self.assertEqual(characters[0]['sample_lines'], ['Leave the lever alone!'])
        self.assertEqual(characters[0]['features'], ['Dry voice.'])

    def test_ordinary_invented_discovery_sample_cannot_reach_preview(self):
        from types import SimpleNamespace
        previews = []
        def request(*args, **kwargs):
            if kwargs['label'].startswith('PERSONA DISCOVERY'):
                return {'ALICE': {'sample_lines': ['Invented dialogue.']}}
            payload = json.JSONDecoder().raw_decode(args[3])[0]
            self.assertEqual(payload['reference_sample'], 'Leave the lever alone!')
            return {'description': 'Dry voice.', 'ref_text': payload['reference_sample']}
        with tempfile.TemporaryDirectory() as root, \
             patch.object(personas, 'call_llm_for_object', side_effect=request), \
             patch.object(personas, '_save_generated_preview', side_effect=lambda *a, **k: previews.append(a[5]) or True):
            failures = personas._run_advanced_speaker_generation(
                [{'speaker': 'ALICE', 'text': 'Leave the lever alone!'}], ['ALICE'],
                {'ALICE': ['Leave the lever alone!']}, {}, object(), 'fixture', object(), root,
                SimpleNamespace(batch_size=40), advanced_prompt='{character_ref}')
        self.assertEqual(failures, [])
        self.assertEqual(previews, ['Leave the lever alone!'])

    def test_all_supported_discovery_identity_keys_preserve_saved_observations(self):
        batch = [{'speaker': 'ALICE', 'text': 'Leave the lever alone!'}]
        for key in ('name', 'speaker', 'speaker_label'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as refs:
                response = {'characters': [{key: 'ALICE', 'voice_clues': ['A gravelly baritone.'],
                    'sample_lines': ['Leave the lever alone!', 'Invented dialogue.']}]}
                before = copy.deepcopy(response)
                with patch.object(personas, 'call_llm_for_object', return_value=response):
                    characters = personas._discover_batch_characters(
                        object(), 'fixture', 'prompt', batch, 1, allowed_speakers=['ALICE'])
                personas._write_batch_character_refs(refs, characters, ['ALICE'], 1)
                saved = json.loads(Path(personas._character_ref_path(refs, 'ALICE')).read_text())
                self.assertEqual(saved['voice_clues'], ['A gravelly baritone.'])
                self.assertEqual(saved['sample_lines'], ['Leave the lever alone!'])
                self.assertEqual(response, before)

    def test_actual_source_and_merge_prompts_fit_both_limits_without_clipping_drafts(self):
        cases = [
            (2048, {'name': 'ALICE', 'voice_clues': ['cue %d ' % i + 'x' * 10 for i in range(20)],
                    'features': ['feature %d ' % i + 'y' * 10 for i in range(20)],
                    'sample_lines': ['Hello there.', 'Hello.']}, False, 'Calm voice. ' * 12 + 'Deep.'),
            (8192, {'name': 'ALICE', 'voice_clues': ['cue %d ' % i + 'x' * 110 for i in range(20)],
                    'features': ['feature %d ' % i + 'y' * 110 for i in range(20)],
                    'sample_lines': [('world ' * 330).strip(), 'Hello.']}, False, 'Calm voice. ' * 12 + 'Deep.'),
            (8192, {'name': 'ALICE', 'voice_clues': ['cue %d ' % i + 'x' * 110 for i in range(20)],
                    'features': ['feature %d ' % i + 'y' * 400 for i in range(40)],
                    'sample_lines': [('world ' * 330).strip(), 'Hello.']}, False, 'Calm voice. ' * 12 + 'Deep.'),
            (4096, {'name': 'ALICE', 'voice_clues': ['cue %d ' % i + 'x' * 160 for i in range(25)],
                    'sample_lines': [('word ' * 399).strip(), ('other ' * 332).strip()]}, True,
                    ('A warm and natural voice with soft and balanced expression. ' * 10).strip()),
        ]
        for context, ref, prefer_original, description in cases:
            with self.subTest(context=context):
                calls, originals, merges, outputs = [], [], [], []
                limit = 12000 + len(personas._compile_character_prompt(
                    ref, reference_text=json.dumps({'name': 'ALICE'}, ensure_ascii=False)))
                def reply(*args, **kwargs):
                    prompt = args[3]
                    calls.append(prompt)
                    self.assertLessEqual(len(prompt), limit)
                    self.assertGreaterEqual(personas.get_effective_max_tokens(600, context,
                        [{'content': args[2]}, {'content': prompt}], scale_to_context=False), 600)
                    payload = json.JSONDecoder().raw_decode(prompt.split('Character reference:\n')[-1])[0]
                    marker = 'Supported partial persona drafts (not new source text):\n'
                    if marker in prompt:
                        drafts = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]
                        merges.append(drafts)
                        for draft in drafts:
                            self.assertIn(draft, outputs)
                            self.assertEqual(draft['description'], description)
                        sample = drafts[0]['ref_text']
                    else:
                        for field in ('voice_clues', 'features', 'sample_lines'):
                            originals.extend((field, text) for text in payload.get(field, []))
                        sample = ((payload.get('sample_lines') or [payload['reference_sample']])[0]
                                  if prefer_original else payload['reference_sample'])
                    result = {'description': description, 'ref_text': sample}
                    kwargs['validate_object'](result)
                    outputs.append(result)
                    return result
                self.assertTrue(self.compile(ref, reply, lambda *a: True,
                    advanced_prompt=None, context_length=context))
                self.assertTrue(merges)
                self.assertCountEqual(originals, [(field, text) for field in
                    ('voice_clues', 'features', 'sample_lines') for text in ref.get(field, [])])
                if len(ref.get('features', [])) > 20:
                    self.assertGreater(sum('Supported partial persona drafts' not in p for p in calls), 2)
                if prefer_original:
                    self.assertTrue(any(all(draft['ref_text'] in ref['sample_lines']
                                            and len(draft['ref_text']) >= 1991 for draft in group)
                                        for group in merges))

    def test_irreducible_fixed_context_still_fails_before_request_or_preview(self):
        from unittest.mock import Mock
        request, preview = Mock(), Mock()
        with self.assertRaises(personas.PersonaContextRecoveryError):
            self.compile({'name': 'ALICE', 'voice_clues': ['Soft voice.'], 'sample_lines': ['Hello.']},
                request, preview, advanced_prompt='x' * 7000 + '{character_ref}', context_length=2048)
        request.assert_not_called()
        preview.assert_not_called()
