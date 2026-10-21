"""Versioned request proof restores only PCM previously saved with observed input."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from synthesis_input import (ensure_speech_input_capture, capture_speech_input, get_captured_speech_input)
from synthesis_recovery_cache import RecoveryCacheStorage, is_recovery_storage_platform_supported
from synthesis_recovery_serving import SynthesisRecoveryDependencyChanged
from tests import test_synthesis_recovery as recovery_fixture


class SynthesisInputRecoveryTests(unittest.TestCase):
    def fixture(self):
        fixture = recovery_fixture.RecoveryOrchestrationTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.engine._language = 'English'
        return fixture

    def render_with_proof(self, fixture, calls=None):
        def render(text, path):
            if calls is not None: calls.append(text)
            fixture.write(path)
            capture_speech_input(path, text, 'English', 'known_leaf_fixture')
            return True
        return render

    def test_proof_version_has_a_separate_key_and_never_relabels_legacy_cached_pcm(self):
        if not is_recovery_storage_platform_supported(): return
        fixture = self.fixture()
        with fixture.scope() as (legacy, parent):
            legacy_key = legacy.key
            receipt = fixture.render(legacy)
            self.assertNotIn('input_proof_version', receipt['recovery'])
        calls = []
        with ensure_speech_input_capture(), fixture.scope('f'*32) as (new, parent):
            self.assertNotEqual(legacy_key, new.key)
            receipt = fixture.render(new, self.render_with_proof(fixture, calls))
            self.assertEqual([], receipt['recovery']['reused_windows'])
            self.assertEqual(2, len(calls))
            self.assertEqual(1, receipt['recovery']['input_proof_version'])
            proof = get_captured_speech_input(fixture.root/'parent.wav')
            self.assertEqual('One. Two.', proof['text'])

    def test_interruption_then_fresh_scope_restores_input_without_calling_cached_leaf(self):
        if not is_recovery_storage_platform_supported(): return
        fixture = self.fixture(); calls = []
        write = self.render_with_proof(fixture, calls)
        def interrupted(text, path):
            if calls: raise RuntimeError('provider interruption')
            return write(text, path)
        with ensure_speech_input_capture(), fixture.scope() as (first, parent):
            key = first.key
            with self.assertRaisesRegex(RuntimeError,'interruption'):
                fixture.render(first,interrupted)
            self.assertIsNone(get_captured_speech_input(fixture.root/'parent.wav'))
        calls.clear()
        with ensure_speech_input_capture(), fixture.scope('f'*32) as (second,parent):
            self.assertEqual(key,second.key)
            receipt=fixture.render(second,self.render_with_proof(fixture,calls))
            self.assertEqual([fixture.plan.windows[1].text],calls)
            self.assertEqual([0],receipt['recovery']['reused_windows'])
            self.assertEqual([1],receipt['recovery']['saved_windows'])
            self.assertEqual('One. Two.',get_captured_speech_input(fixture.root/'parent.wav')['text'])

    def test_unobserved_or_mismatched_leaf_input_cannot_enter_proof_contract_cache(self):
        if not is_recovery_storage_platform_supported(): return
        for mismatch in (False,True):
            fixture=self.fixture()
            def render(text,path):
                fixture.write(path)
                if mismatch: capture_speech_input(path,'Wrong submitted text.','English','unknown_fixture')
                return True
            with self.subTest(mismatch=mismatch),ensure_speech_input_capture(),fixture.scope() as (session,parent):
                receipt=fixture.render(session,render)
                self.assertEqual('unproven_window_input',receipt['recovery']['status'])
                self.assertEqual([],receipt['recovery']['saved_windows'])
                self.assertIsNone(get_captured_speech_input(fixture.root/'parent.wav'))
                with RecoveryCacheStorage(fixture.root,parent.admit) as storage:
                    with storage.read_transaction() as transaction:
                        self.assertTrue(all(not request['windows'] for request in transaction.data['requests'].values()))

    def test_changed_serving_after_capture_aborts_before_recovered_input_or_new_provider(self):
        if not is_recovery_storage_platform_supported(): return
        fixture=self.fixture()
        with ensure_speech_input_capture(),fixture.scope() as (session,parent):
            fixture.render(session,self.render_with_proof(fixture))
        before=(fixture.root/'parent.wav').read_bytes()
        changed=replace(fixture.identity,sha256='e'*64)
        with ensure_speech_input_capture(),fixture.scope('f'*32) as (session,parent):
            with patch('synthesis_recovery_serving.capture_native_custom_serving_identity',return_value=changed):
                with self.assertRaises(SynthesisRecoveryDependencyChanged):
                    fixture.render(session,lambda *args:self.fail('changed serving reached provider'))
            self.assertIsNone(get_captured_speech_input(fixture.root/'parent.wav'))
        self.assertEqual(before,(fixture.root/'parent.wav').read_bytes())


if __name__ == '__main__':
    unittest.main()
