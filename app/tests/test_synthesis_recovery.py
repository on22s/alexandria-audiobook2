"""Recovery orchestration uses real PCM/storage with declared serving fixtures."""
from contextlib import contextmanager, ExitStack
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from audio_generation_owner import AudioGenerationOwner, AudioGenerationCancelled, ensure_audio_generation_owner
import synthesis_recovery as recovery
from synthesis_recovery_cache import is_recovery_storage_platform_supported
from synthesis_recovery_context import (RecoveryParent, RecoveryAdmissionError,
    ensure_synthesis_recovery_parent, get_recovery_claim_digest)
from synthesis_recovery_serving import NativeRecoveryServingIdentity, SynthesisRecoveryDependencyChanged
from synthesis_render import get_synthesis_window_plan, render_synthesis_windows
from synthesis_settings import get_synthesis_window_settings


@unittest.skipUnless(is_recovery_storage_platform_supported(), 'Linux durability required')
class RecoveryOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = get_synthesis_window_settings({'enabled': True,
            'budgets': {'local.custom': {'max_chars': 5}}})
        self.plan = get_synthesis_window_plan('One. Two.', self.policy, ['local.custom'])
        self.engine = SimpleNamespace(_synthesis_recovery={'enabled': True, 'max_audio_mb': 16},
            get_synthesis_recovery_policy=lambda: {'enabled': True, 'max_audio_mb': 16})
        self.identity = NativeRecoveryServingIdentity('a' * 64, 'b' * 64, 'c' * 64)
        self.request = object()
        self.admit_error = None

    @contextmanager
    def admit(self, **kwargs):
        if self.admit_error is not None:
            raise self.admit_error
        yield

    @contextmanager
    def scope(self, claim='a' * 32, identity=None, parent_inputs='d' * 64):
        with ExitStack() as stack:
            stack.enter_context(ensure_audio_generation_owner(AudioGenerationOwner(claim, self.admit)))
            parent = RecoveryParent(str(self.root), 'b' * 64, 'c' * 64, parent_inputs,
                                    get_recovery_claim_digest(), self.admit)
            stack.enter_context(ensure_synthesis_recovery_parent(parent))
            stack.enter_context(patch.object(recovery, '_capture_engine_methods'))
            stack.enter_context(patch.object(recovery, 'capture_native_custom_serving_identity',
                                             return_value=identity or self.identity))
            stack.enter_context(patch('synthesis_recovery_serving.capture_native_custom_serving_identity',
                                     return_value=identity or self.identity))
            session = stack.enter_context(recovery.ensure_synthesis_recovery(self.engine, 'custom',
                                         self.plan, self.policy, self.request))
            yield session, parent

    def write(self, path):
        sf.write(path, np.sin(np.arange(2400) * .1) * .2, 24000)
        return True

    def render(self, session, callback=None):
        return render_synthesis_windows(self.plan, callback or (lambda text, path: self.write(path)),
                                       self.root / 'parent.wav', self.policy, recovery=session)

    def test_interruption_restart_reuses_only_completed_window_and_retires_explicitly(self):
        output = self.root / 'parent.wav'
        output.write_bytes(b'prior take')
        calls = []
        def interrupted(text, path):
            calls.append(text)
            if len(calls) == 2:
                raise RuntimeError('provider interruption')
            return self.write(path)
        with self.scope() as (first, parent):
            key = first.key
            with self.assertRaisesRegex(RuntimeError, 'provider interruption'):
                self.render(first, interrupted)
        self.assertEqual(b'prior take', output.read_bytes())
        calls.clear()
        def resumed(text, path):
            calls.append(text)
            return self.write(path)
        with self.scope('f' * 32) as (second, parent):
            self.assertEqual(key, second.key)
            receipt = self.render(second, resumed)
            self.assertEqual([self.plan.windows[1].text], calls)
            self.assertEqual([0], receipt['recovery']['reused_windows'])
            self.assertEqual([1], receipt['recovery']['saved_windows'])
            self.assertEqual(4800, sf.info(output).frames)
            recovery.retire_committed_synthesis_recovery(parent, receipt)
            with second.storage.read_transaction() as transaction:
                self.assertEqual({}, transaction.data['requests'])

    def test_dependency_drift_after_copy_aborts_before_provider_and_preserves_prior(self):
        with self.scope() as (session, parent):
            self.render(session)
        output = self.root / 'parent.wav'
        before = output.read_bytes()
        changed = replace(self.identity, sha256='e' * 64)
        with self.scope('f' * 32) as (session, parent):
            with patch('synthesis_recovery_serving.capture_native_custom_serving_identity', return_value=changed):
                with self.assertRaises(SynthesisRecoveryDependencyChanged):
                    self.render(session, lambda *args: self.fail('drift must abort before provider'))
        self.assertEqual(before, output.read_bytes())

    def test_dependency_failure_after_private_assembly_keeps_prior_caller_output(self):
        import synthesis_render
        original = synthesis_render.assemble_synthesis_windows
        output = self.root / 'parent.wav'
        output.write_bytes(b'prior caller output')
        def assemble(*args, **kwargs):
            result = original(*args, **kwargs)
            self.assertNotEqual(output, Path(args[2]))
            self.admit_error = RecoveryAdmissionError('authority changed after assembly')
            return result
        try:
            with self.scope() as (session, parent):
                with patch.object(synthesis_render, 'assemble_synthesis_windows', side_effect=assemble):
                    with self.assertRaises(RecoveryAdmissionError):
                        self.render(session)
        finally:
            self.admit_error = None
        self.assertEqual(b'prior caller output', output.read_bytes())

    def test_owner_or_book_failure_is_never_an_optional_cache_miss(self):
        for error in (AudioGenerationCancelled(), RecoveryAdmissionError('book unavailable')):
            with self.subTest(error=type(error).__name__), self.scope() as (session, parent):
                self.admit_error = error
                try:
                    with self.assertRaises(type(error)):
                        self.render(session, lambda *args: self.fail('invalid owner must not render'))
                finally:
                    self.admit_error = None

    def test_corrupt_payload_falls_back_under_same_identity_without_touching_prior_cache(self):
        with self.scope() as (session, parent):
            self.render(session)
        payload = next((self.root / '.synthesis-recovery-v1').glob('*_0_*.wav'))
        payload.write_bytes(b'corrupt')
        calls = []
        with self.scope('f' * 32) as (session, parent):
            receipt = self.render(session, lambda text, path: (calls.append(text), self.write(path))[1])
        self.assertEqual(2, len(calls))
        self.assertEqual('cache_unavailable', receipt['recovery']['status'])
        self.assertEqual(b'corrupt', payload.read_bytes())

    def test_changed_parent_inputs_or_serving_identity_do_not_reuse_previous_audio(self):
        with self.scope() as (session, parent):
            self.render(session)
            key = session.key
        for kwargs in ({'parent_inputs': 'e' * 64}, {'identity': replace(self.identity, sha256='f' * 64)}):
            with self.subTest(change=kwargs), self.scope('f' * 32, **kwargs) as (session, parent):
                self.assertNotEqual(key, session.key)
                receipt = self.render(session)
                self.assertEqual([], receipt['recovery']['reused_windows'])

    def test_cache_failure_keeps_serving_guard_and_postcommit_cleanup_cannot_raise(self):
        with self.scope() as (session, parent):
            session.disable_cache()
            changed = replace(self.identity, sha256='e' * 64)
            with patch('synthesis_recovery_serving.capture_native_custom_serving_identity', return_value=changed):
                with self.assertRaises(SynthesisRecoveryDependencyChanged):
                    self.render(session)
            receipt = {'recovery': session.get_receipt()}
            self.admit_error = AudioGenerationCancelled()
            try:
                recovery.retire_committed_synthesis_recovery(parent, receipt)
            finally:
                self.admit_error = None

    def test_disabled_or_unsupported_platform_does_no_model_or_cache_inspection(self):
        with patch.object(recovery, '_capture_engine_methods', side_effect=AssertionError('inspection')):
            self.engine._synthesis_recovery = None
            with recovery.ensure_synthesis_recovery(self.engine, 'custom', self.plan, self.policy) as session:
                self.assertIsNone(session)
            self.engine._synthesis_recovery = {'enabled': True}
            with patch.object(recovery, 'is_recovery_storage_platform_supported', return_value=False):
                with recovery.ensure_synthesis_recovery(self.engine, 'custom', self.plan, self.policy) as session:
                    self.assertEqual('unsupported_durability', session.reason)
        self.assertEqual([], list(self.root.iterdir()))
