"""Finished-byte binding, distinct confirmation and explicit unavailable/cancel paths."""
from contextlib import ExitStack
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

from cpu_asr_scratch import CPUASRScratch, is_close_owned_cpu_asr_supported
from speech_verification import verify_finished_speech, SpeechVerificationCancelled

TEXT = 'The calm guide closed the quiet garden gate before leaving.'


class FinishedSpeechVerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.audio = self.root / 'finished.wav'
        t = np.arange(32000) / 16000
        self.write_audio(.1 * np.sin(t * 2 * np.pi * 300))
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('cpu_asr_scratch.get_cpu_asr_scratch_parent', return_value=self.root))
        self.identity = self.stack.enter_context(patch('speech_verification.get_verifier_identity', side_effect=
            lambda runtime, role, cancel: {'role': role, 'model': {'sha256': role},
                'decoder': {'resolved_path': shutil.which('ffmpeg')}}))
        self.transcribe = self.stack.enter_context(patch('speech_verification.transcribe_asr_audio'))

    def write_audio(self, values):
        with wave.open(str(self.audio), 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes((values * 32767).astype('<i2').tobytes())

    def run_case(self, primary=TEXT, confirmation=None, text=TEXT):
        self.transcribe.side_effect = [{'transcription': [{'text': value}]} for value in
                                       [primary, confirmation if confirmation is not None else primary]]
        return verify_finished_speech(self.audio, text, 'English', {}, {})

    def test_good_long_take_needs_no_confirmation_and_binds_finished_bytes(self):
        if not is_close_owned_cpu_asr_supported(): return
        import hashlib
        original = self.audio.read_bytes()
        result = self.run_case()
        self.assertEqual('no_content_candidate', result['status'])
        self.assertEqual(hashlib.sha256(original).hexdigest(), result['audio']['sha256'])
        self.assertEqual(1, self.transcribe.call_count)
        self.assertEqual(2, self.identity.call_count)
        self.assertEqual(original, self.audio.read_bytes())
        self.assertNotIn('confirmation', result)

    def test_omission_requires_distinct_confirmation_of_the_same_region(self):
        if not is_close_owned_cpu_asr_supported(): return
        result = self.run_case('The calm guide closed.', 'The calm guide closed.')
        self.assertEqual('corroborated_content_difference', result['status'])
        self.assertEqual(2, self.transcribe.call_count)
        self.assertEqual(4, self.identity.call_count)
        self.assertEqual(['the','quiet','garden','gate','before','leaving'],
                         result['corroborated_regions'][0]['expected_tokens'])
        self.assertNotEqual(result['primary']['identity']['model'], result['confirmation']['identity']['model'])

    def test_disagreeing_confirmation_does_not_promote_a_primary_suspicion(self):
        if not is_close_owned_cpu_asr_supported(): return
        result = self.run_case('The calm guide closed.', TEXT)
        self.assertEqual('review', result['status'])
        self.assertEqual([], result['corroborated_regions'])

    def test_duplicate_phrase_corroborates_but_authored_repeat_does_not(self):
        if not is_close_owned_cpu_asr_supported(): return
        result = self.run_case(TEXT + ' ' + TEXT)
        self.assertEqual('corroborated_content_difference', result['status'])
        self.transcribe.reset_mock()
        control = self.run_case(TEXT + ' ' + TEXT, text=TEXT + ' ' + TEXT)
        self.assertEqual('no_content_candidate', control['status'])
        self.assertEqual(1, self.transcribe.call_count)

    def test_unavailable_confirmation_retains_primary_evidence_and_never_claims_pass(self):
        if not is_close_owned_cpu_asr_supported(): return
        original = self.identity.side_effect
        def identity(runtime, role, cancel):
            if role == 'confirmation': raise ValueError('confirmation missing')
            return original(runtime, role, cancel)
        self.identity.side_effect = identity
        result = self.run_case('The calm guide closed.')
        self.assertEqual('review_confirmation_unavailable', result['status'])
        self.assertEqual('confirmation missing', result['reason'])
        self.assertIn('primary', result)
        self.assertNotIn('corroborated_regions', result)

    def test_changed_audio_is_unavailable_even_after_both_transcripts_agree(self):
        if not is_close_owned_cpu_asr_supported(): return
        def transcript(*args, **kwargs):
            self.write_audio(np.zeros(32000))
            return {'transcription': [{'text': 'The calm guide closed.'}]}
        self.transcribe.side_effect = transcript
        result = verify_finished_speech(self.audio, TEXT, 'en', {}, {})
        self.assertEqual('unavailable', result['status'])
        self.assertEqual('audio_readmission', result['stage'])
        self.assertIn('audio changed', result['reason'])

    def test_short_acoustic_suspicion_triggers_stronger_check_even_if_asr_collapses_repeat(self):
        if not is_close_owned_cpu_asr_supported(): return
        t = np.arange(16000) / 16000
        frequency = np.repeat([200,600,1500,350,1000],3200)
        clip = np.sin(2 * np.pi * np.cumsum(frequency) / 16000) * (.25 * np.sin(np.pi*t)**2 + .05)
        self.write_audio(np.concatenate([clip, np.zeros(4000), clip]))
        result = self.run_case('Go now.', 'Go now.', text='Go now.')
        self.assertEqual(2, self.transcribe.call_count)
        self.assertEqual('review', result['status'])
        self.assertTrue(result['short_acoustic']['repetition_candidates'])
        self.assertEqual([], result['corroborated_regions'])

    def test_cancel_is_not_downgraded_to_unavailable(self):
        with self.assertRaises(SpeechVerificationCancelled):
            verify_finished_speech(self.audio, TEXT, 'en', {}, {}, lambda: True)
        if not is_close_owned_cpu_asr_supported(): return
        self.transcribe.side_effect = SpeechVerificationCancelled('cancelled during provider')
        with self.assertRaises(SpeechVerificationCancelled):
            verify_finished_speech(self.audio, TEXT, 'en', {}, {})

    def test_unavailable_short_acoustic_check_never_becomes_no_candidate(self):
        if not is_close_owned_cpu_asr_supported(): return
        for reason in ['unavailable_island_limit', 'unavailable_duration_limit']:
            with self.subTest(reason=reason):
                self.transcribe.reset_mock()
                with patch('speech_verification.get_short_acoustic_evidence', return_value={'status': reason}):
                    result = self.run_case('Go now.', text='Go now.')
                self.assertEqual('review_short_acoustic_unavailable', result['status'])
                self.assertEqual(reason, result['short_acoustic']['status'])

    def test_long_audio_for_a_short_input_is_explicitly_incomplete_acoustic_evidence(self):
        if not is_close_owned_cpu_asr_supported(): return
        self.write_audio(np.zeros(20 * 16000))
        result = self.run_case('Go now.', text='Go now.')
        self.assertEqual('unavailable_duration_limit', result['short_acoustic']['status'])
        self.assertEqual('review_short_acoustic_unavailable', result['status'])

    def test_busy_and_unsupported_language_are_explicit_without_provider_work(self):
        result = verify_finished_speech(self.audio, TEXT, 'fr', {}, {})
        self.assertEqual('unavailable', result['status'])
        self.transcribe.assert_not_called()
        if not is_close_owned_cpu_asr_supported(): return
        with CPUASRScratch(self.root):
            result = verify_finished_speech(self.audio, TEXT, 'en', {}, {})
        self.assertEqual('unavailable', result['status'])
        self.assertIn('busy', result['reason'])
        self.transcribe.assert_not_called()

    def test_malformed_audio_metadata_is_unavailable_without_asr(self):
        if not is_close_owned_cpu_asr_supported(): return
        self.audio.write_bytes(b'not generated audio')
        result = verify_finished_speech(self.audio, TEXT, 'en', {}, {})
        self.assertEqual('unavailable', result['status'])
        self.assertIn('metadata is unavailable', result['reason'])
        self.transcribe.assert_not_called()

    def test_oversized_diagnostics_are_discarded_with_explicit_unavailable_status(self):
        if not is_close_owned_cpu_asr_supported(): return
        with patch('speech_verification.MAX_RESULT_BYTES',1024):
            result=self.run_case()
        self.assertEqual('unavailable',result['status'])
        self.assertEqual('evidence_limit',result['stage'])
        self.assertNotIn('primary',result)
        self.assertIn('audio',result)
        import json
        self.assertLessEqual(len(json.dumps(result).encode('utf-8')),1024)


if __name__ == '__main__':
    unittest.main()
