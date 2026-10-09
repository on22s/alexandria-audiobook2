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
                            if field != 'name' for item in items)
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
        self.assertEqual(ref['observations'], observed)
        self.assertIn('late distinctive accent', [item for field, item in seen if field == 'voice_clues'])

    def test_fixed_prompt_over_budget_fails_without_a_request(self):
        with patch.object(personas, 'call_llm_for_object') as request:
            with self.assertRaises(personas.PersonaContextRecoveryError):
                personas.request_persona_with_evidence(
                    object(), 'fixture', 'system', lambda parts: 'x' * 20,
                    [], object(), 'fixture', max_prompt_chars=10)
            request.assert_not_called()
