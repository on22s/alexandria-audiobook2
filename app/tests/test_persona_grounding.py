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

    def compile(self, ref, reply, preview):
        with tempfile.TemporaryDirectory() as tmp:
            refs = Path(tmp) / 'refs'; refs.mkdir()
            Path(personas._character_ref_path(str(refs), 'ALICE')).write_text(json.dumps(ref))
            with patch.object(personas, 'call_llm_for_object', side_effect=reply):
                return personas._compile_persona(object(), 'fixture', object(), {}, tmp, str(refs),
                    'ALICE', {}, None, '{character_ref}', preview_saver=preview)

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
