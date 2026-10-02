"""Persona boundaries reject controls while preserving legitimate Unicode text."""
import copy
import unittest
from unittest.mock import patch
from persona_validation import validate_persona_payload
import script_preflight as preflight


class PersonaTextControlTests(unittest.TestCase):
    def test_both_text_fields_reject_controls_before_stripping_without_mutating_payload(self):
        for field in ('description','ref_text'):
            for control in ('\x00','\x1b','\x7f','\x85','\x0b','\ud800'):
                with self.subTest(field=field,control=repr(control)):
                    payload={'description':'Warm natural voice.','ref_text':'Hello, my friend.','extra':'kept'}
                    payload[field]=control+payload[field];before=copy.deepcopy(payload)
                    with self.assertRaisesRegex(ValueError,field):validate_persona_payload(payload)
                    self.assertEqual(before,payload)

    def test_multiline_unicode_combining_and_joined_emoji_remain_unchanged(self):
        payload={'description':'  Café\n柔らかな声\tCalm.  ','ref_text':'  سلام\r\nCafe\u0301 👩\u200d🚀  '}
        before=copy.deepcopy(payload);result=validate_persona_payload(payload)
        self.assertEqual({name:value.strip() for name,value in payload.items()},result)
        self.assertEqual(before,payload)

    def test_existing_script_control_report_stays_exact_and_nonmutating(self):
        text='Café\n\t\r\x00\x1b\x7f\ud800';before=text
        self.assertEqual(['U+0000','U+001B','U+007F','U+D800'],preflight.audit_unicode_text(text)['unsafe_controls'])
        self.assertEqual(before,text)

    def test_near_duplicate_detector_uses_shared_normalization_for_both_entries(self):
        first='ONE, two three four five six seven eight nine ten.'
        second='one two three four five six seven eight nine eleven.'
        with patch.object(preflight,'_normalize_words',wraps=preflight._normalize_words) as normalize:
            findings=preflight.find_adjacent_near_duplicate_entries([first,second],'unrelated source')
        self.assertIn(first,[call.args[0] for call in normalize.call_args_list])
        self.assertIn(second,[call.args[0] for call in normalize.call_args_list])
        self.assertEqual('adjacent_near_duplicate',findings[0]['code'])
        self.assertEqual('manual_review',findings[0]['severity'])

    def test_shared_object_request_rejects_control_payload_with_validation_evidence(self):
        import contextlib,io,json,tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        import generate_script as generation
        payload={'description':'Warm\x1b[31m voice.','ref_text':'Hello there.'}
        create=Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)),finish_reason='stop')],usage=None))
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        attempts=[]
        with tempfile.TemporaryDirectory() as tmp,patch.object(generation,'get_response_log_path',return_value=str(Path(tmp)/'response.log')),contextlib.redirect_stdout(io.StringIO()) as output:
            result=generation.call_llm_for_object(client,'fixture','Return persona JSON.','ALICE',
                generation.LLMGenParams(),label="PERSONA ALICE",validate_object=validate_persona_payload,max_retries=0,attempt_observer=attempts.append)
            self.assertIn('U+001B',output.getvalue())
            self.assertIn('invalid_object',attempts[0]['failure_codes'])
        self.assertFalse(result);create.assert_called_once()
        self.assertNotEqual('accepted',attempts[0]['outcome'])
