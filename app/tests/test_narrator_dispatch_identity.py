"""Effective narrator identity must agree across admission, dispatch and restore."""
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from audio_take_history import AudioTakeHistory
from book_state_transaction import get_book_operation_token
from synthesis_recovery_identity import get_recovery_digest
from synthesis_recovery_serving import NativeRecoveryServingIdentity
from take_publication import KEY
from take_restoration import capture_restore_target, restore_editor_take
from tests import test_take_restoration as restoration_cases


def serving_identity(engine, request):
    digest = get_recovery_digest(asdict(request))
    return NativeRecoveryServingIdentity(digest, 'c' * 64, 'd' * 64)


class NarratorDispatchIdentityTests(unittest.TestCase):
    def run_case(self, mode, dispatch='single'):

        def generate(manager):
            if dispatch == 'batch':
                result = manager.generate_chunks_batch([0], batch_size=2, batch_seed=42)
                return (result['completed'] == [0] and not result['failed']), None
            return manager.generate_chunk_audio(0)

        """Generate, archive, mutate, restore, then render the same line fresh."""
        calls = []
        case = restoration_cases.TakeRestorationTests()

        class Model:
            def generate_custom_voice(self, **kwargs):
                speaker = kwargs['speaker']
                calls.append(speaker)
                amplitude = .4 if speaker == 'Serena' else .2
                return [np.sin(np.arange(2400) * .1) * amplitude], 24000

        with case.fixture() as (root, manager, _, fresh), case.owner():
            engine = case.engine()
            engine._local_custom_model = Model()
            Path(manager.config_path).write_text(json.dumps({'tts': {
                **engine.get_model_settings(),
                'synthesis_recovery': engine._synthesis_recovery}}))
            manager.invalidate_config_cache()
            voice = {'type': 'custom', 'voice': 'Ryan', 'seed': 42}
            voices = {'NARRATOR': voice}
            row = manager.load_chunks()[0]
            if mode in ('narrator', 'alias'):
                row['narrator_gender'] = 'male'
                if mode == 'alias':
                    row['speaker'] = 'READER'
                    voices['READER'] = {'alias_of': 'NARRATOR'}
                manager.save_chunks([row])
                voice.update(narrator_strategy='gender',
                             versions={'V': {**voice, 'gender': 'female'}})

            def save():
                Path(manager.voice_config_path).write_text(json.dumps(voices))
                manager.invalidate_config_cache()

            save()
            patches = (patch.object(manager, 'get_engine', return_value=engine),
                       patch('synthesis_recovery.capture_native_serving_identity',
                             side_effect=serving_identity),
                       patch('synthesis_recovery_serving.capture_native_custom_serving_identity',
                             side_effect=serving_identity))
            with patches[0], patches[1], patches[2]:
                ok, message = generate(manager)
                self.assertTrue(ok, message)
                origin = manager.load_chunks()[0]
                audio = (root / origin['audio_path']).read_bytes()
                first_calls = calls[:]
                self.assertEqual(first_calls, ['Ryan'] * 3)
                manager.update_chunk(0, {'take': 3})
                manager._publish_generated_chunk_audio(
                    manager.load_chunks()[0], str(fresh), 'NARRATOR')
                current = manager.load_chunks()[0]
                displaced = (root / current['audio_path']).read_bytes()
                self.assertNotEqual(audio, displaced)
                history = AudioTakeHistory(root)
                with history._locked() as document:
                    record = next(item for item in document['records']
                                  if item['audio_revision'] == origin['audio_revision'])
                request = SimpleNamespace(
                    expected_book_token=get_book_operation_token(root),
                    source_book_token=get_book_operation_token(root),
                    expected_uid=current['uid'],
                    expected_audio_revision=current['audio_revision'],
                    expected_audio_path=current['audio_path'],
                    expected_take=current['take'], version=record['version'],
                    sha256=record['sha256'])
                if mode in ('narrator', 'alias'):
                    voice['versions']['V']['voice'] = 'Serena'
                elif mode == 'metadata':
                    voice['gender'] = 'female'
                elif mode == 'missing':
                    voices.clear()
                elif mode == 'effective':
                    voice['voice'] = 'Serena'
                save()
                try:
                    restored = restore_editor_take(
                        manager, 0, request, capture_restore_target(manager, 0, request))
                    outcome = 'restored'
                    self.assertEqual((root / restored['audio_path']).read_bytes(), audio)
                    self.assertEqual(restored['audio_revision'], origin['audio_revision'])
                except ValueError as error:
                    outcome = str(error)
                    self.assertEqual((root / current['audio_path']).read_bytes(), displaced)
                calls.clear()
                if mode == 'missing':
                    return outcome, first_calls, [], None
                with case.fixture() as (other, next_manager, _, _), \
                        patch.object(next_manager, 'get_engine', return_value=engine):
                    next_manager.save_chunks([row])
                    Path(next_manager.config_path).write_bytes(
                        Path(manager.config_path).read_bytes())
                    Path(next_manager.voice_config_path).write_bytes(
                        Path(manager.voice_config_path).read_bytes())
                    next_manager.invalidate_config_cache()
                    ok, message = generate(next_manager)
                    self.assertTrue(ok, message)
                    second = next_manager.load_chunks()[0]
                    fresh_bytes = (other / second['audio_path']).read_bytes()
                    if outcome == 'restored':
                        self.assertEqual(fresh_bytes, audio)
                    return outcome, first_calls, calls, fresh_bytes

    def test_metadata_only_control_restores_and_keeps_provider_and_bytes(self):
        outcome, first, second, fresh_bytes = self.run_case('metadata')
        self.assertEqual(outcome, 'restored')
        self.assertEqual(second, ['Ryan'] * 3)
        self.assertEqual(first, second)

    def test_inoperative_narrator_version_change_is_not_an_effective_change(self):
        outcome, first, second, fresh_bytes = self.run_case('narrator')
        self.assertEqual(outcome, 'restored')
        self.assertEqual(second, ['Ryan'] * 3)
        self.assertEqual(first, second)

    def test_effective_voice_change_is_refused_with_changed_provider_and_bytes(self):
        outcome, first, second, fresh_bytes = self.run_case('effective')
        self.assertIn('voice or seed selection', outcome)
        self.assertEqual(second, ['Serena'] * 3)
        self.assertNotEqual(first, second)

    def test_removed_speaker_reports_normal_mismatch_and_preserves_audio(self):
        outcome, first, second, fresh_bytes = self.run_case('missing')
        self.assertIn('voice or seed selection', outcome)
        self.assertNotIn('NoneType', outcome)

    def test_batch_no_match_restores_and_fresh_batch_keeps_every_window(self):
        outcome, first, second, fresh_bytes = self.run_case('narrator', dispatch='batch')
        self.assertEqual(outcome, 'restored')
        self.assertEqual(first, ['Ryan'] * 3)
        self.assertEqual(second, ['Ryan'] * 3)

    def test_batch_narrator_alias_restores_and_fresh_batch_keeps_every_window(self):
        outcome, first, second, fresh_bytes = self.run_case('alias', dispatch='batch')
        self.assertEqual(outcome, 'restored')
        self.assertEqual(first, ['Ryan'] * 3)
        self.assertEqual(second, ['Ryan'] * 3)

    def test_batch_effective_voice_change_is_refused_with_changed_windows(self):
        outcome, first, second, fresh_bytes = self.run_case('effective', dispatch='batch')
        self.assertIn('voice or seed selection', outcome)
        self.assertEqual(first, ['Ryan'] * 3)
        self.assertEqual(second, ['Serena'] * 3)


if __name__ == '__main__':
    unittest.main()
