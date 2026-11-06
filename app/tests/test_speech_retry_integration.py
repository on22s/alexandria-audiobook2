"""Real speech-retry orchestration retains prior takes across retry outcomes."""
import copy
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from book_state_transaction import get_book_operation_token
from tests import test_speech_verification_publication as publication_fixture
from tests.test_speech_verification_receipt import get_report


class SpeechRetryIntegrationTests(unittest.TestCase):
    @contextmanager
    def retry_fixture(self):
        publication = publication_fixture.SpeechVerificationPublicationTests()
        with publication.fixture() as (root, manager, before, prior, flags):
            config = json.loads((root / 'config.json').read_text())
            config['tts']['speech_retry'] = {'enabled': True}
            (root / 'config.json').write_text(json.dumps(config))
            manager.invalidate_config_cache()
            manager.engine._speech_retry = {'enabled': True}
            yield root, manager, before, prior, flags

    def generate(self, manager, mode):
        if mode == 'single':
            return manager.generate_chunk_audio(0)[0]
        if mode == 'parallel':
            return manager.generate_chunks_parallel([0], max_workers=1)['completed'] == [0]
        return manager.generate_chunks_batch([0], batch_size=1)['completed'] == [0]

    def assert_prior(self, root, manager, before, prior):
        row = manager.load_chunks()[0]
        self.assertEqual(before['audio_path'], row['audio_path'])
        self.assertEqual(before['audio_revision'], row['audio_revision'])
        self.assertEqual(prior, (root / row['audio_path']).read_bytes())
        self.assertEqual([], list((root / 'voicelines').glob('.render-*')))
        self.assertEqual([], list(root.glob('chunk_*.wav')))
        return row

    def history(self, row):
        return row.get('speech_retry_attempts') or []

    def provider_seeds(self, spy):
        return [call[0][3]['NARRATOR']['seed'] for call in spy.call_args_list]

    def provider_speakers(self, spy):
        return [call[0][2] for call in spy.call_args_list]

    def sequence_verifier(self, root, before, prior, reports, actions=None):
        state = {'position': 0, 'prior_intact': []}
        def verify(path, text, *args, **kwargs):
            position = state['position']
            state['position'] += 1
            state['prior_intact'].append((root / before['audio_path']).read_bytes() == prior)
            if actions is not None and position in actions:
                actions[position]()
            outcome = reports[position]
            if outcome == 'mismatch':
                return get_report(path, text, 'The calm guide closed.', status='corroborated_content_difference')
            if outcome == 'review':
                return get_report(path, text, 'Exact.', status='review')
            if outcome == 'unavailable':
                return {'version': 1, 'status': 'unavailable', 'stage': 'primary_identity', 'reason': 'cached model missing'}
            return get_report(path, text)
        return verify, state

    def test_mismatch_then_verified_retries_once_with_distinct_seed_in_all_modes(self):
        for mode in ('single', 'parallel', 'native'):
            with self.subTest(mode=mode), self.retry_fixture() as (root, manager, before, prior, flags):
                verify, state = self.sequence_verifier(root, before, prior, ['mismatch', 'good'])
                original_provider = manager.engine.generate_voice_result
                with patch('speech_verification.verify_finished_speech', side_effect=verify) as checker, patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy, patch.object(manager.engine, 'generate_batch', side_effect=AssertionError('opted-in native path used engine batch')) as batch_spy:
                    self.assertTrue(self.generate(manager, mode))
                self.assertEqual(2, provider_spy.call_count)
                self.assertEqual(2, checker.call_count)
                self.assertEqual([True, True], state['prior_intact'])
                self.assertEqual(['NARRATOR', 'NARRATOR'], self.provider_speakers(provider_spy))
                seeds = self.provider_seeds(provider_spy)
                self.assertEqual(2, len(set(seeds)))
                batch_spy.assert_not_called()
                row = manager.load_chunks()[0]
                self.assertNotEqual(before['audio_revision'], row['audio_revision'])
                self.assertEqual('done', row['status'])
                self.assertIsNone(row['speech_verification_failure'])
                self.assertEqual('no_content_candidate', row['speech_verification']['status'])
                self.assertEqual(row['audio_revision'], row['speech_verification']['audio_revision'])
                attempts = self.history(row)
                self.assertEqual(2, len(attempts))
                self.assertEqual(2, len({entry['attempt_id'] for entry in attempts}))
                self.assertEqual([1, 2], [entry['attempt'] for entry in attempts])
                self.assertEqual(seeds, [entry['requested_seed'] for entry in attempts])
                self.assertEqual(['line-0', 'line-0'], [entry['uid'] for entry in attempts])
                self.assertEqual([get_book_operation_token(root)] * 2, [entry['book_token'] for entry in attempts])
                self.assertTrue(all(entry['input_digest'] for entry in attempts))
                self.assertEqual('corroborated_content_difference', attempts[0]['receipt']['status'])
                self.assertEqual('no_content_candidate', attempts[1]['receipt']['status'])
                self.assertEqual([], list((root / 'voicelines').glob('.render-*')))

    def test_repeated_mismatch_and_non_retryable_verifier_outcomes_keep_prior(self):
        scenarios = [
            ('repeated_mismatch', ['mismatch', 'mismatch'], 2, 'corroborated_content_difference'),
            ('unavailable_first', ['unavailable'], 1, 'verifier_unavailable'),
            ('mismatch_then_unavailable', ['mismatch', 'unavailable'], 2, 'verifier_unavailable'),
            ('review_first', ['review'], 1, 'acoustic_suspicion'),
            ('mismatch_then_review', ['mismatch', 'review'], 2, 'acoustic_suspicion'),
        ]
        for name, reports, expected_calls, terminal in scenarios:
            with self.subTest(scenario=name), self.retry_fixture() as (root, manager, before, prior, flags):
                verify, state = self.sequence_verifier(root, before, prior, reports)
                original_provider = manager.engine.generate_voice_result
                with patch('speech_verification.verify_finished_speech', side_effect=verify) as checker, patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy:
                    self.assertFalse(self.generate(manager, 'single'))
                self.assertEqual(expected_calls, provider_spy.call_count)
                self.assertEqual(expected_calls, checker.call_count)
                self.assertEqual([True] * expected_calls, state['prior_intact'])
                seeds = self.provider_seeds(provider_spy)
                self.assertEqual(expected_calls, len(set(seeds)))
                row = self.assert_prior(root, manager, before, prior)
                self.assertEqual('error', row['status'])
                attempts = self.history(row)
                self.assertEqual(expected_calls, len(attempts))
                self.assertEqual(terminal, attempts[-1]['outcome'])
                if name == 'repeated_mismatch':
                    self.assertEqual('corroborated_content_difference', row['speech_verification_failure']['status'])
                    self.assertEqual(['corroborated_content_difference'] * 2, [entry['receipt']['status'] for entry in attempts])
                if name == 'mismatch_then_unavailable':
                    self.assertEqual('corroborated_content_difference', attempts[0]['receipt']['status'])

    def test_provider_failure_and_exception_do_not_trigger_content_retry(self):
        from tts import TTSFailure, TTSGenerationResult
        for kind in ('result', 'exception', 'invalid_audio'):
            with self.subTest(kind=kind), self.retry_fixture() as (root, manager, before, prior, flags):
                verify, state = self.sequence_verifier(root, before, prior, ['good'])
                if kind == 'result':
                    provider = lambda *args, **kwargs: TTSGenerationResult(False, TTSFailure('provider_error', 'provider unavailable', 'Check provider.'))
                    context = patch.object(manager.engine, 'generate_voice_result', side_effect=provider)
                elif kind == 'invalid_audio':
                    context = patch.object(manager.engine, 'generate_voice_result', return_value=TTSGenerationResult(True))
                else:
                    context = patch.object(manager.engine, 'generate_voice_result', side_effect=RuntimeError('synthetic provider exception'))
                with patch('speech_verification.verify_finished_speech', side_effect=verify) as checker, context as provider_spy:
                    self.assertFalse(self.generate(manager, 'single'))
                self.assertEqual(1, provider_spy.call_count)
                checker.assert_not_called()
                row = self.assert_prior(root, manager, before, prior)
                self.assertEqual('error', row['status'])
                attempts = self.history(row)
                self.assertEqual(1, len(attempts))
                self.assertEqual('provider_failure', attempts[0]['outcome'])

    def test_oom_parallel_stepdown_shares_budget_and_exhausts_after_two_calls(self):
        from tts import TTSFailure, TTSGenerationResult
        scenarios = [
            ('oom_then_verified', ['good'], True),
            ('oom_then_mismatch', ['mismatch'], False),
            ('all_oom', [], False),
        ]
        for name, reports, succeeds in scenarios:
            with self.subTest(scenario=name), self.retry_fixture() as (root, manager, before, prior, flags):
                verify, state = self.sequence_verifier(root, before, prior, reports)
                original_provider = manager.engine.generate_voice_result
                calls = []
                def provider(*args, **kwargs):
                    calls.append(args)
                    if len(calls) == 1 or name == 'all_oom':
                        return TTSGenerationResult(False, TTSFailure('out_of_memory', 'CUDA out of memory', 'Free memory.'))
                    return original_provider(*args, **kwargs)
                with patch('speech_verification.verify_finished_speech', side_effect=verify) as checker, patch.object(manager.engine, 'generate_voice_result', side_effect=provider) as provider_spy:
                    result = manager.generate_chunks_parallel([0], max_workers=3)
                self.assertEqual(2, provider_spy.call_count)
                self.assertEqual([0] if succeeds else [], result['completed'])
                if succeeds:
                    row = manager.load_chunks()[0]
                    self.assertNotEqual(before['audio_revision'], row['audio_revision'])
                    self.assertEqual(2, len(self.history(row)))
                else:
                    row = self.assert_prior(root, manager, before, prior)
                    self.assertEqual(2, len(self.history(row)))

    def test_text_edit_during_first_verification_stops_retry_and_preserves_edit(self):
        with self.retry_fixture() as (root, manager, before, prior, flags):
            snapshots = []
            actions = {0: lambda: (manager.update_chunk(0, {'text': 'New edited words.'}), snapshots.append(copy.deepcopy(manager.load_chunks())))}
            verify, state = self.sequence_verifier(root, before, prior, ['mismatch', 'good'], actions=actions)
            original_provider = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=verify), patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy:
                self.assertFalse(self.generate(manager, 'single'))
            self.assertEqual(1, provider_spy.call_count)
            row = manager.load_chunks()[0]
            self.assertEqual(snapshots[0], manager.load_chunks())
            self.assertEqual('New edited words.', row['text'])
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())
            self.assertEqual([], list((root / 'voicelines').glob('.render-*')))

    def test_book_switch_during_first_verification_stops_retry_without_row_rewrite(self):
        with self.retry_fixture() as (root, manager, before, prior, flags):
            snapshots = []
            token_holder = {'value': get_book_operation_token(root)}
            def switch_book():
                token_holder['value'] = '9' * 64
                snapshots.append(copy.deepcopy(manager.load_chunks()))
            verify, state = self.sequence_verifier(root, before, prior, ['mismatch', 'good'], actions={0: switch_book})
            original_provider = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=verify), patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy, patch('project.get_book_operation_token', side_effect=lambda root_dir: token_holder['value']):
                self.assertFalse(self.generate(manager, 'single'))
            self.assertEqual(1, provider_spy.call_count)
            self.assertEqual(snapshots[0], manager.load_chunks())
            row = manager.load_chunks()[0]
            self.assertEqual(before['audio_path'], row['audio_path'])
            self.assertEqual(before['audio_revision'], row['audio_revision'])
            self.assertEqual(prior, (root / row['audio_path']).read_bytes())

    def test_cancellation_during_first_or_second_verification_never_publishes(self):
        for position in (0, 1):
            with self.subTest(position=position), self.retry_fixture() as (root, manager, before, prior, flags):
                def cancel():
                    flags['cancelled'] = True
                reports = ['mismatch', 'good'] if position == 1 else ['mismatch', 'good']
                verify, state = self.sequence_verifier(root, before, prior, reports, actions={position: cancel})
                original_provider = manager.engine.generate_voice_result
                with patch('speech_verification.verify_finished_speech', side_effect=verify), patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy:
                    self.assertFalse(self.generate(manager, 'single'))
                self.assertEqual(position + 1, provider_spy.call_count)
                row = self.assert_prior(root, manager, before, prior)
                self.assertEqual(before['status'], row['status'])
                self.assertEqual(before['error'], row['error'])
                attempts = self.history(row)
                self.assertEqual(position + 1, len(attempts))
                self.assertEqual('cancellation', attempts[-1]['outcome'])

    def test_released_owner_and_cancelled_verifier_exception_do_not_publish(self):
        with self.retry_fixture() as (root, manager, before, prior, flags):
            snapshots = []
            def release():
                flags['released'] = True
                snapshots.append(copy.deepcopy(manager.load_chunks()))
            verify, state = self.sequence_verifier(root, before, prior, ['good'], actions={0: release})
            original_provider = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=verify), patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy:
                self.assertFalse(self.generate(manager, 'single'))
            self.assertEqual(1, provider_spy.call_count)
            self.assertEqual(snapshots[0], manager.load_chunks())
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())
        with self.retry_fixture() as (root, manager, before, prior, flags):
            from speech_verification import SpeechVerificationCancelled
            original_provider = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=SpeechVerificationCancelled('synthetic verifier cancellation')), patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy:
                self.assertFalse(self.generate(manager, 'single'))
            self.assertEqual(1, provider_spy.call_count)
            row = self.assert_prior(root, manager, before, prior)
            self.assertEqual(before['status'], row['status'])
            self.assertEqual('cancellation', self.history(row)[-1]['outcome'])

    def test_deadline_before_render_after_synthesis_and_inside_verifier_preserves_prior(self):
        with self.retry_fixture() as (root, manager, before, prior, flags):
            budget = manager._create_speech_retry_budget(manager.load_chunks()[0])
            budget.deadline = 0
            original_provider = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=AssertionError('deadline verifier ran')) as checker, patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy:
                result = manager._generate_captured_chunk_audio(0, manager.load_chunks()[0], budget)
            self.assertFalse(result[0])
            provider_spy.assert_not_called()
            checker.assert_not_called()
            self.assertEqual([], self.history(self.assert_prior(root, manager, before, prior)))
        with self.retry_fixture() as (root, manager, before, prior, flags):
            budget = manager._create_speech_retry_budget(manager.load_chunks()[0])
            def expire_inside_verifier(path, text, *args, **kwargs):
                budget.deadline = 0
                return get_report(path, text)
            original_provider = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=expire_inside_verifier), patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy:
                result = manager._generate_captured_chunk_audio(0, manager.load_chunks()[0], budget)
            self.assertFalse(result[0])
            self.assertEqual(1, provider_spy.call_count)
            row = self.assert_prior(root, manager, before, prior)
            self.assertEqual('deadline', self.history(row)[-1]['outcome'])
        with self.retry_fixture() as (root, manager, before, prior, flags):
            from speech_retry import get_speech_retry_budget
            budget = manager._create_speech_retry_budget(manager.load_chunks()[0])
            original_provider = manager.engine.generate_voice_result
            def expire_after_provider(*args, **kwargs):
                result = original_provider(*args, **kwargs)
                active_budget = get_speech_retry_budget()
                self.assertIsNotNone(active_budget)
                active_budget.deadline = 0
                return result
            with patch('speech_verification.verify_finished_speech', side_effect=AssertionError('expired verifier ran')) as checker, patch.object(manager.engine, 'generate_voice_result', side_effect=expire_after_provider) as provider_spy:
                result = manager._generate_captured_chunk_audio(0, manager.load_chunks()[0], budget)
            self.assertFalse(result[0])
            self.assertEqual(1, provider_spy.call_count)
            checker.assert_not_called()
            self.assertEqual([], list((root / 'voicelines').glob('.render-*')))
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())

    def test_stale_engine_or_disabled_verification_refuses_before_provider(self):
        scenarios = [
            ('saved_on_active_none', {'enabled': True}, None, {'enabled': True}),
            ('saved_off_active_on', {'enabled': False}, {'enabled': True}, {'enabled': True}),
            ('verification_off_retry_on', {'enabled': True}, {'enabled': True}, {'enabled': False}),
        ]
        for name, retry_saved, retry_active, verification_saved in scenarios:
            with self.subTest(scenario=name), self.retry_fixture() as (root, manager, before, prior, flags):
                config = json.loads((root / 'config.json').read_text())
                config['tts']['speech_retry'] = retry_saved
                config['tts']['speech_verification'] = verification_saved
                (root / 'config.json').write_text(json.dumps(config))
                manager.invalidate_config_cache()
                manager.engine._speech_retry = retry_active
                if name == 'verification_off_retry_on':
                    manager.engine._speech_verification = None
                original_provider = manager.engine.generate_voice_result
                with patch('speech_verification.verify_finished_speech', side_effect=AssertionError('refused verifier ran')) as checker, patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy:
                    self.assertFalse(self.generate(manager, 'single'))
                provider_spy.assert_not_called()
                checker.assert_not_called()
                row = self.assert_prior(root, manager, before, prior)
                self.assertEqual('error', row['status'])
                attempts = self.history(row)
                self.assertEqual(1, len(attempts))
                self.assertEqual('admission_refusal', attempts[0]['outcome'])

    def test_batch_seed_overrides_selected_voice_seed_and_default_seed_is_deterministic(self):
        with self.retry_fixture() as (root, manager, before, prior, flags):
            verify, state = self.sequence_verifier(root, before, prior, ['good'])
            original_provider = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=verify), patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy:
                self.assertEqual([0], manager.generate_chunks_batch([0], batch_size=1, batch_seed=123)['completed'])
            self.assertEqual([123], self.provider_seeds(provider_spy))
            self.assertEqual(['NARRATOR'], self.provider_speakers(provider_spy))
        default_seeds = []
        for repeat in range(2):
            with self.subTest(repeat=repeat), self.retry_fixture() as (root, manager, before, prior, flags):
                verify, state = self.sequence_verifier(root, before, prior, ['good'])
                original_provider = manager.engine.generate_voice_result
                with patch('speech_verification.verify_finished_speech', side_effect=verify), patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy:
                    self.assertEqual([0], manager.generate_chunks_batch([0], batch_size=1)['completed'])
                default_seeds.append(self.provider_seeds(provider_spy)[0])
        self.assertEqual(default_seeds[0], default_seeds[1])
        self.assertNotEqual(123, default_seeds[0])

    def test_retry_disabled_baseline_publishes_without_retry_history(self):
        with self.retry_fixture() as (root, manager, before, prior, flags):
            config = json.loads((root / 'config.json').read_text())
            config['tts']['speech_retry'] = {'enabled': False}
            (root / 'config.json').write_text(json.dumps(config))
            manager.invalidate_config_cache()
            manager.engine._speech_retry = None
            verify, state = self.sequence_verifier(root, before, prior, ['good'])
            original_provider = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=verify) as checker, patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider_spy:
                self.assertTrue(self.generate(manager, 'single'))
            self.assertEqual(1, provider_spy.call_count)
            checker.assert_called_once()
            row = manager.load_chunks()[0]
            self.assertNotEqual(before['audio_revision'], row['audio_revision'])
            self.assertIsNone(row.get('speech_retry_attempts'))

    def test_deadline_during_audio_install_keeps_the_selected_pointer_and_prior_bytes(self):
        import project
        with self.retry_fixture() as (root, manager, before, prior, flags):
            budget = manager._create_speech_retry_budget(manager.load_chunks()[0])
            original_sync = project.sync_adapter_directory
            expired = []
            def sync_then_expire(path):
                original_sync(path)
                if str(path) == str(manager.voicelines_dir):
                    expired.append(path)
                    budget.deadline = 0
            verify, state = self.sequence_verifier(root, before, prior, ['good'])
            with patch('speech_verification.verify_finished_speech', side_effect=verify), \
                    patch('project.sync_adapter_directory', side_effect=sync_then_expire):
                result = manager._generate_captured_chunk_audio(0, manager.load_chunks()[0], budget)
            self.assertFalse(result[0])
            self.assertTrue(expired)
            row = self.assert_prior(root, manager, before, prior)
            self.assertEqual('deadline', self.history(row)[-1]['outcome'])
            self.assertEqual('no_content_candidate', self.history(row)[-1]['receipt']['status'])

    def test_requested_seed_is_local_and_retained_capture_preserves_attempt_evidence(self):
        from take_retention import get_captured_take
        with self.retry_fixture() as (root, manager, before, prior, flags):
            voices_path = root / manager.voice_config_path
            voices = json.loads(voices_path.read_text())
            voices['NARRATOR']['seed'] = 123
            voices_path.write_text(json.dumps(voices))
            original_voices = voices_path.read_bytes()
            verify, state = self.sequence_verifier(root, before, prior, ['mismatch', 'good'])
            with patch('speech_verification.verify_finished_speech', side_effect=verify):
                self.assertTrue(self.generate(manager, 'single'))
            row = manager.load_chunks()[0]
            self.assertEqual(123, self.history(row)[0]['requested_seed'])
            self.assertNotEqual(123, self.history(row)[1]['requested_seed'])
            self.assertEqual(original_voices, voices_path.read_bytes())
            self.assertEqual(before.get('take'), row.get('take'))
            captured = get_captured_take(row)
            self.assertEqual(self.history(row), captured['speech_retry_attempts'])
            captured['speech_retry_attempts'][0]['requested_seed'] = 999
            self.assertEqual(123, self.history(row)[0]['requested_seed'])

    def test_native_batch_callback_cancellation_stops_queued_lines(self):
        with self.retry_fixture() as (root, manager, before, prior, flags):
            rows = manager.load_chunks()
            second = {**copy.deepcopy(rows[0]), 'id': 1, 'uid': 'line-1',
                      'audio_revision': 'prior-second', 'speech_verification': None}
            manager.save_chunks(rows + [second])
            cancelled = {'value': False}
            def verify_then_cancel(path, text, *args, **kwargs):
                cancelled['value'] = True
                return get_report(path, text)
            original_provider = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=verify_then_cancel), \
                    patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider, \
                    patch.object(manager.engine, 'generate_batch', side_effect=AssertionError('native batch bypass')):
                result = manager.generate_chunks_batch([0, 1], batch_seed=999,
                    batch_group_by_type=True, cancel_check=lambda: cancelled['value'])
            self.assertEqual([], result['completed'])
            self.assertEqual(2, result['cancelled'])
            self.assertEqual(1, provider.call_count)
            self.assertEqual([999], self.provider_seeds(provider))
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())
            self.assertEqual(before['audio_revision'], manager.load_chunks()[0]['audio_revision'])

    def test_external_parent_wait_uses_remaining_shared_deadline(self):
        import time
        from concurrent.futures import TimeoutError
        from unittest.mock import Mock
        from speech_retry import ensure_speech_retry_budget
        with self.retry_fixture() as (root, manager, before, prior, flags):
            budget = manager._create_speech_retry_budget(manager.load_chunks()[0])
            budget.deadline = time.monotonic() + 1.0
            manager.engine._external_timeout = 300
            future = Mock()
            future.result.side_effect = TimeoutError()
            output = str(root / before['audio_path'])
            provider = Mock(side_effect=AssertionError('provider was not submitted'))
            with ensure_speech_retry_budget(budget), \
                    patch.object(manager.engine, '_submit_external', return_value=future):
                self.assertFalse(manager.engine._external_single(provider, output))
            timeout = future.result.call_args.kwargs['timeout']
            self.assertTrue(0 <= timeout <= 1.0)
            self.assertLess(timeout, manager.engine._external_timeout)
            provider.assert_not_called()
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())

    def test_shared_deadline_stops_later_local_windows_and_preserves_parent_output(self):
        import tempfile
        from pathlib import Path
        import numpy as np
        import soundfile as sf
        from speech_retry import SpeechRetryBudget, SpeechRetryDeadlineError, ensure_speech_retry_budget
        from synthesis_render import render_synthesis_windows
        from synthesis_windows import plan_synthesis_windows
        from synthesis_settings import get_synthesis_window_settings
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'parent.wav'
            output.write_bytes(b'prior parent bytes')
            plan = plan_synthesis_windows(publication_fixture.TEXT, max_chars=28)
            self.assertGreater(len(plan.windows), 1)
            policy = get_synthesis_window_settings({'enabled': True,
                'budgets': {'local.custom': {'max_chars': 28}}})
            budget = SpeechRetryBudget()
            calls = []
            def render(span, path):
                calls.append(span)
                sf.write(path, np.sin(np.arange(2400) / 30) * .1, 24000)
                budget.deadline = 0
                return True
            with ensure_speech_retry_budget(budget), self.assertRaises(SpeechRetryDeadlineError):
                render_synthesis_windows(plan, render, output, policy)
            self.assertEqual(1, len(calls))
            self.assertEqual(b'prior parent bytes', output.read_bytes())


if __name__ == '__main__':
    unittest.main()
