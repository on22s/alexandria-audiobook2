"""Oversized references retain late evidence in bounded, valid JSON requests."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import generate_personas as personas


class PersonaReferenceBudgetTests(unittest.TestCase):
    def test_large_reference_is_valid_and_retains_tail(self):
        ref = {'name': 'ALICE', 'observations': [
            {'voice_clues': ['warm ' * 2800]},
            {'voice_clues': ['late distinctive accent']}], 'sample_lines': ['Hello.']}
        prompt = personas._compile_character_prompt(ref, '{character_ref}')
        self.assertGreater(len(prompt), 12000)
        self.assertEqual(ref['observations'], json.loads(prompt)['observations'])
        self.assertNotIn('...TRUNCATED...', prompt)

    def test_compile_processes_all_evidence_in_bounded_valid_requests(self):
        ref = {'name': 'ALICE', 'observations': [
            {'voice_clues': ['clue-%d ' % i * 1200]} for i in range(5)],
            'sample_lines': ['Hello.'], 'voice_clues': ['late distinctive accent']}
        seen = []

        def reply(client, model, system, prompt, params, **kwargs):
            if 'Supported partial persona drafts' not in prompt:
                # Full payload must remain parseable, even for split source requests.
                payload = json.loads(prompt)
                self.assertLessEqual(len(prompt), 12000 + len(
                    json.dumps({'name': 'ALICE'})))
                seen.extend((field, item) for field, items in payload.items()
                            if field not in ('name', 'partial_evidence', 'shared_voice_context', 'reference_sample') for item in items)
            return {'description': 'Warm natural voice.', 'ref_text': 'Hello.'}

        with tempfile.TemporaryDirectory() as tmp:
            refs = Path(tmp) / 'refs';refs.mkdir()
            Path(personas._character_ref_path(str(refs), 'ALICE')).write_text(json.dumps(ref))
            with patch.object(personas, 'call_llm_for_object', side_effect=reply):
                self.assertTrue(personas._compile_persona(
                    object(), 'fixture', object(), {}, tmp, str(refs), 'ALICE', {},
                    None, '{character_ref}', preview_saver=lambda *args: True))
        self.assertGreater(len(seen), 1)
        observed = [item for field, item in seen if field == 'observations']
        self.assertCountEqual(ref['observations'], observed)
        self.assertIn('late distinctive accent', [item for field, item in seen if field == 'voice_clues'])

    def test_fixed_prompt_over_budget_fails_without_a_request(self):
        with patch.object(personas, 'call_llm_for_object') as request:
            with self.assertRaises(personas.PersonaContextRecoveryError):
                personas.request_persona_with_evidence(
                    object(), 'fixture', 'system', lambda parts: 'x' * 20,
                    [], object(), 'fixture', max_prompt_chars=10)
            request.assert_not_called()


class PersonaBalancedBatchTests(unittest.TestCase):
    def test_balances_by_size_and_combines_once(self):
        from generate_script import LLMGenParams
        evidence = [('features', 'x' * 100) for _ in range(100)] + [('observations', 'y' * 5000)]
        calls = []
        def build(parts):
            return json.dumps({'name': 'ALICE', 'items': parts})
        def reply(*args, **kwargs):
            calls.append(args[3])
            return {'description': 'Dry and weary voice.', 'ref_text': 'Hello.'}
        with patch.object(personas, 'call_llm_for_object', side_effect=reply):
            result = personas.request_persona_with_evidence(
                object(), 'fixture', 'system', build, evidence, LLMGenParams(),
                'balanced', max_prompt_chars=len(build([])) + 12000)
        source = [p for p in calls if 'Supported partial persona drafts' not in p]
        self.assertEqual(len(source), 2)
        self.assertEqual(len(calls), 3)
        self.assertLess(abs(len(source[0]) - len(source[1])), 1000)
        self.assertCountEqual([tuple(item) for p in source for item in json.loads(p)['items']], evidence)
        self.assertEqual(result['description'], 'Dry and weary voice.')

    def test_context_budget_can_split_below_character_allowance(self):
        from generate_script import LLMGenParams
        parts = [('features', 'x' * 1500) for _ in range(10)]
        calls = []
        def build(items): return json.dumps({'items': items})
        def reply(*args, **kwargs):
            calls.append(args[3])
            return {'description': 'Dry voice.', 'ref_text': 'Hello.'}
        with patch.object(personas, 'call_llm_for_object', side_effect=reply):
            personas.request_persona_with_evidence(object(), 'fixture', 'system', build,
                parts, LLMGenParams(context_length=4096, max_tokens=600),
                'bounded', max_prompt_chars=1000000)
        self.assertGreater(len(calls), 1)
        for prompt in calls:
            self.assertGreaterEqual(personas.get_effective_max_tokens(600, 4096,
                [{'content': 'system'}, {'content': prompt}], scale_to_context=False), 600)

    def test_reference_allowance_validated_by_config(self):
        from config_settings import PromptConfig
        from pydantic import ValidationError
        self.assertEqual(PromptConfig().persona_reference_chars, 12000)
        self.assertEqual(PromptConfig(persona_reference_chars=1000000).persona_reference_chars, 1000000)
        for value in (0, 11999, 1000001):
            with self.assertRaises(ValidationError): PromptConfig(persona_reference_chars=value)

    def test_unknown_context_keeps_default_ceiling_for_large_setting(self):
        ref = {'name': 'ALICE', 'features': ['x' * 900 for _ in range(15)], 'sample_lines': ['Hello.']}
        for context, expected_calls in ((None, 3), (12288, 1)):
            calls = []
            def reply(*args, **kwargs):
                calls.append(args[3])
                return {'description': 'Dry voice.', 'ref_text': 'Hello.'}
            with tempfile.TemporaryDirectory() as tmp:
                refs = Path(tmp) / 'refs'; refs.mkdir()
                Path(personas._character_ref_path(str(refs), 'ALICE')).write_text(json.dumps(ref))
                with patch.object(personas, 'call_llm_for_object', side_effect=reply):
                    personas._compile_persona(object(), 'fixture', object(), {}, tmp,
                        str(refs), 'ALICE', {}, None, '{character_ref}',
                        context_length=context, reference_chars=1000000,
                        preview_saver=lambda *args: True)
            self.assertEqual(len(calls), expected_calls)

    def test_shared_context_cannot_expand_request_allowance(self):
        ref = {'name': 'ALICE', 'voice_clues': ['Dry. ' * 5000], 'personality': ['Weary.'], 'sample_lines': ['Hello.']}
        calls = []
        def reply(*args, **kwargs):
            calls.append(args[3])
            return {'description': 'Dry voice.', 'ref_text': 'Hello.'}
        with tempfile.TemporaryDirectory() as tmp:
            refs = Path(tmp) / 'refs'; refs.mkdir()
            Path(personas._character_ref_path(str(refs), 'ALICE')).write_text(json.dumps(ref))
            with patch.object(personas, 'call_llm_for_object', side_effect=reply):
                personas._compile_persona(object(), 'fixture', object(), {}, tmp,
                    str(refs), 'ALICE', {}, None, '{character_ref}', preview_saver=lambda *args: True)
        self.assertGreater(len(calls), 1)
        for prompt in calls:
            self.assertLessEqual(len(prompt), 12000 + len(json.dumps({'name': 'ALICE'})))
            if 'Supported partial persona drafts' not in prompt:
                payload = json.loads(prompt)
                self.assertLessEqual(len(json.dumps(payload.get('shared_voice_context', {}), ensure_ascii=False)), 1000)
