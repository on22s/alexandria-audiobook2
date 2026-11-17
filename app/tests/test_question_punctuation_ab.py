"""Owned experimental inputs and real capture plumbing, without model inference."""
from contextlib import contextmanager
import copy
import json
import os
import sys
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile
from fastapi import HTTPException

from experiments import question_punctuation_ab as ab
from experiments import final_word_tune as tune
from experiments.run_dialogue_continuity import run_owned_audio_experiment
from tests.test_dialogue_continuity_controller import isolated_core_ownership


class FakeEngine:
    def __init__(self, events, changed_text=False, change_settings=False):
        self.events = events
        self.changed_text = changed_text
        self.change_settings = change_settings
        self.settings = {'mode': 'local', 'max_new_tokens': 40, 'language': 'English'}
        self.calls = []

    def get_model_settings(self):
        return copy.deepcopy(self.settings)

    def _generate_lora_voice_prepared(self, text, instruct, voice, path):
        from audio_generation_owner import ensure_audio_generation_active, get_audio_generation_claim_id
        from synthesis_input import capture_speech_input
        assert get_audio_generation_claim_id() is not None
        ensure_audio_generation_active()
        self.calls.append((text, copy.deepcopy(voice), path))
        self.events.append('render')
        soundfile.write(path, np.sin(np.arange(2400) * 0.05) * 0.1, 24000)
        capture_speech_input(path, 'different' if self.changed_text else text, 'English', 'local_lora_api')
        if self.change_settings:
            self.settings['max_new_tokens'] += 1
        return True


class QuestionExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'run'
        self.out.mkdir(); (self.out / 'wav').mkdir()
        self.speaker = 'a/../../../victim'
        self.entries = {self.speaker: {'type': 'lora', 'adapter_path': str(self.root / 'adapter'),
                                     'adapter_generation_sha256': 'a' * 64}}

    def run_pairs(self, engine, core):
        with patch.object(core.project_manager, 'get_engine', return_value=engine), patch.object(
                ab, 'freeze_question_voices', side_effect=copy.deepcopy):
            return run_owned_audio_experiment(core, lambda admitted: ab.render_question_pairs(
                admitted, self.entries, [1234], ab.LINES[:1], self.out, {'provenance': {'fixture': True}}), self.out / 'run')

    def test_owned_pair_captures_actual_text_and_confines_speaker_paths(self):
        events = []
        outside = self.root / 'victim_1234_question.wav'; outside.write_bytes(b'keep')
        with isolated_core_ownership(events) as (core, state):
            engine = FakeEngine(events)
            result = self.run_pairs(engine, core)
            self.assertFalse(state['audio']['running'])
        self.assertEqual(['render', 'render', 'cleanup', 'release'], events)
        self.assertEqual(outside.read_bytes(), b'keep')
        self.assertEqual(result['status'], 'captured')
        self.assertEqual(result['provenance'], {'fixture': True})
        self.assertEqual(json.loads((self.out / 'rows.json').read_text())['provenance'], result['provenance'])
        self.assertEqual([row['submitted_text'] for row in result['rows']],
                         [ab.LINES[0][1] + '?', ab.LINES[0][1] + '?.'])
        for row in result['rows']:
            self.assertEqual(Path(row['wav']).parent, self.out / 'wav')
            self.assertNotIn('victim', Path(row['wav']).name)
            self.assertEqual(row['wav_sha256'], ab.file_sha256(row['wav']))
            self.assertEqual(row['speech_input']['text'], row['requested_text'])
        manifest = [json.loads(line) for line in (self.out / 'manifest.jsonl').read_text().splitlines()]
        self.assertEqual(len(manifest), 2)
        self.assertEqual([row['wav_sha256'] for row in manifest], [row['wav_sha256'] for row in result['rows']])

    def test_unowned_render_refuses_before_provider_or_removal(self):
        path = self.root / 'outside.wav'; path.write_bytes(b'keep')
        engine = FakeEngine([])
        with self.assertRaisesRegex(ValueError, 'owned'):
            ab.render_exact(engine, 'Why?', {}, path)
        self.assertEqual(path.read_bytes(), b'keep')
        self.assertEqual(engine.calls, [])

    def test_occupied_host_lease_refuses_before_engine_initialization(self):
        events = []
        with (isolated_core_ownership(events) as (core, _state), patch.object(
                core, 'acquire_task_lease', side_effect=RuntimeError('occupied host')),
                patch.object(core.project_manager, 'get_engine') as get_engine):
            with self.assertRaisesRegex(HTTPException, 'occupied host'):
                run_owned_audio_experiment(core, lambda engine: None, self.out / 'run')
            get_engine.assert_not_called()

    def test_mismatched_submitted_text_never_publishes_a_manifest(self):
        events = []
        with isolated_core_ownership(events) as (core, _state):
            with self.assertRaisesRegex(ValueError, 'submitted-input'):
                self.run_pairs(FakeEngine(events, changed_text=True), core)
        self.assertFalse((self.out / 'manifest.jsonl').exists())
        self.assertEqual(events[-2:], ['cleanup', 'release'])

    def test_engine_settings_change_stops_the_pair(self):
        events = []
        with isolated_core_ownership(events) as (core, _state):
            engine = FakeEngine(events, change_settings=True)
            with self.assertRaisesRegex(ValueError, 'settings changed'):
                self.run_pairs(engine, core)
        self.assertEqual(len(engine.calls), 1)
        self.assertFalse((self.out / 'manifest.jsonl').exists())

    def test_cancellation_during_final_validation_keeps_the_run_incomplete(self):
        from audio_generation_owner import AudioGenerationCancelled
        import audio_validation
        validate = audio_validation.validate_generated_audio
        events = []; calls = []
        with isolated_core_ownership(events) as (core, state):
            def cancel_on_second(*args, **kwargs):
                result = validate(*args, **kwargs); calls.append(True)
                if len(calls) == 2:
                    with core._gpu_lock:
                        state['audio']['cancel'] = True
                return result
            with patch.object(audio_validation, 'validate_generated_audio', cancel_on_second):
                with self.assertRaises(AudioGenerationCancelled):
                    self.run_pairs(FakeEngine(events), core)
        self.assertFalse((self.out / 'manifest.jsonl').exists())
        self.assertEqual(json.loads((self.out / 'rows.json').read_text())['status'], 'incomplete')
        self.assertEqual(json.loads((self.out / 'rows.json').read_text())['provenance'], {'fixture': True})
        self.assertEqual(events[-2:], ['cleanup', 'release'])

    def test_cancellation_after_last_partial_record_cannot_publish_complete_metadata(self):
        from audio_generation_owner import AudioGenerationCancelled
        save = ab.atomic_json_write; events = []
        with isolated_core_ownership(events) as (core, state):
            def cancel_after_last(document, path):
                result = save(document, path)
                if document['status'] == 'incomplete' and len(document['rows']) == 2:
                    with core._gpu_lock:
                        state['audio']['cancel'] = True
                return result
            with patch.object(ab, 'atomic_json_write', cancel_after_last):
                with self.assertRaises(AudioGenerationCancelled):
                    self.run_pairs(FakeEngine(events), core)
        self.assertFalse((self.out / 'manifest.jsonl').exists())
        self.assertEqual(json.loads((self.out / 'rows.json').read_text())['status'], 'incomplete')
        self.assertEqual(events[-2:], ['cleanup', 'release'])

    def test_existing_output_is_not_reused_or_deleted(self):
        events = []
        path = self.root / 'old.wav'; path.write_bytes(b'keep')
        with isolated_core_ownership(events) as (core, _state):
            engine = FakeEngine(events)
            with patch.object(core.project_manager, 'get_engine', return_value=engine):
                with self.assertRaises(FileExistsError):
                    run_owned_audio_experiment(core, lambda engine: ab.render_exact(engine, 'Why?', {}, path), self.out / 'run')
        self.assertEqual(path.read_bytes(), b'keep')
        self.assertEqual(engine.calls, [])

    def test_adapter_generation_is_frozen_and_changed_declared_generation_refuses(self):
        @contextmanager
        def snapshot(path):
            yield str(self.root), 'b' * 64
        events = []
        entries = copy.deepcopy(self.entries)
        entries[self.speaker].pop('adapter_generation_sha256')
        with isolated_core_ownership(events) as (core, _state), patch(
                'adapter_checkpoint_transaction.ensure_adapter_generation_snapshot', snapshot), patch(
                'voice_manifest.get_resolved_adapter_path', side_effect=lambda path: path):
            frozen = run_owned_audio_experiment(core, lambda engine: ab.freeze_question_voices(entries), self.out / 'run')
            self.assertEqual(frozen[self.speaker]['adapter_generation_sha256'], 'b' * 64)
            with self.assertRaisesRegex(ValueError, 'Adapter changed'):
                run_owned_audio_experiment(core, lambda engine: ab.freeze_question_voices(self.entries), self.out / 'run')


class QuestionInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'; self.config.write_text(json.dumps({'tts': {'mode': 'local'}}))
        self.voices = self.root / 'voices.json'; self.voices.write_text(json.dumps(
            {'NARRATOR': {'type': 'builtin_lora', 'adapter_path': str(self.root / 'adapter')}}))
        self.args = SimpleNamespace(config=str(self.config), voice_config=str(self.voices),
                                    speakers='NARRATOR', seeds='1234', limit=1)

    def test_explicit_local_builtin_lora_inputs_are_supported(self):
        _config, _voices, entries, seeds, lines = ab.get_question_inputs(self.args)
        self.assertEqual(entries['NARRATOR']['type'], 'builtin_lora')
        self.assertEqual(seeds, [1234]); self.assertEqual(lines, ab.LINES[:1])

    def test_random_duplicate_or_out_of_range_seeds_refuse(self):
        for value in ('-1', '1234,-1', '1234,1234', str(2**31)):
            with self.subTest(value=value):
                self.args.seeds = value
                with self.assertRaisesRegex(ValueError, 'seeds'):
                    ab.get_question_inputs(self.args)


    def test_windowed_comparison_refuses_before_runtime(self):
        self.config.write_text(json.dumps({'tts': {'mode': 'local', 'synthesis_windows': {
            'enabled': True, 'budgets': {'local.lora': {'max_chars': 43}}}}}))
        with self.assertRaisesRegex(ValueError, 'Disable synthesis windows'):
            ab.get_question_inputs(self.args)

    def test_empty_or_duplicate_speaker_selections_refuse(self):
        for value in ('', 'NARRATOR,NARRATOR'):
            with self.subTest(value=value):
                self.args.speakers = value
                with self.assertRaisesRegex(ValueError, 'unique speakers'):
                    ab.get_question_inputs(self.args)


    def test_existing_run_refuses_without_importing_core_or_reusing_rows(self):
        old = self.root / 'rows.json'; old.write_text('{"status":"old"}')
        script = '''
import builtins, sys
from experiments import question_punctuation_ab as ab
ab.get_experiment_source_identity=lambda root: {'head':'fixture','tree':'fixture'}
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name=='core': raise AssertionError('engine bootstrap must not run')
    return original(name,*args,**kwargs)
builtins.__import__=guarded
ab.main(sys.argv[1:])
'''
        env = dict(os.environ); env.pop('ALEXANDRIA_DATA_DIR', None)
        result = subprocess.run([sys.executable, '-c', script, '--config', str(self.config),
            '--voice-config', str(self.voices), '--speakers', 'NARRATOR', '--out-dir', str(self.root)],
            cwd=Path(ab.REPO) / 'app', env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('[WinError 183]' if os.name == 'nt' else '[Errno 17]', result.stderr)
        self.assertNotIn('engine bootstrap', result.stderr)
        self.assertEqual(old.read_text(), '{"status":"old"}')


class QuestionScoringIdentityTests(unittest.TestCase):
    def test_scoring_binds_actual_bytes_and_refuses_stale_or_changed_clips(self):
        for mode in ('current', 'stale', 'changed'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); wav = root / 'clip.wav'
                soundfile.write(wav, np.ones(2400) * 0.1, 24000)
                expected = ab.file_sha256(wav)
                manifest = root / 'manifest.jsonl'; out = root / 'tune.json'
                manifest.write_text(json.dumps({'id': 'fixture', 'wav': str(wav), 'text': 'Why?',
                    'wav_sha256': '0' * 64 if mode == 'stale' else expected}) + '\n')
                def measure(*args):
                    if mode == 'changed':
                        wav.write_bytes(b'changed')
                    return {'measured': True, 'rising': True}
                with patch.object(sys, 'argv', ['scorer', '--manifest', str(manifest), '--out', str(out)]), patch(
                        'experiments.provenance.provenance', return_value={}), patch.object(
                        tune, 'measure_clip', side_effect=measure) as scorer:
                    tune.main()
                row = json.loads(out.read_text())['rows'][0]
                if mode == 'current':
                    self.assertTrue(row['measured'])
                    self.assertEqual(row['measured_wav_sha256'], expected)
                else:
                    self.assertFalse(row['measured'])
                    self.assertNotIn('rising', row)
                if mode == 'stale':
                    scorer.assert_not_called()

    def test_ctc_cache_reuses_keys_and_evicts_at_two_models(self):
        from experiments import ctc_text_boundary as ctc
        calls = []
        class Model:
            def to(self, device):
                return self
            def eval(self):
                return self
        def load(name):
            calls.append(name)
            return Model()
        fake = SimpleNamespace(AutoProcessor=SimpleNamespace(from_pretrained=lambda name: object()),
                               AutoModelForCTC=SimpleNamespace(from_pretrained=load))
        ctc._load_ctc.cache_clear(); self.addCleanup(ctc._load_ctc.cache_clear)
        with patch.dict(sys.modules, {'transformers': fake}):
            first = ctc._load_ctc('a', 'cpu')
            self.assertIs(first, ctc._load_ctc('a', 'cpu'))
            ctc._load_ctc('b', 'cpu'); ctc._load_ctc('c', 'cpu'); ctc._load_ctc('a', 'cpu')
        self.assertEqual(calls, ['a', 'b', 'c', 'a'])
        self.assertEqual(ctc._load_ctc.cache_info().currsize, 2)


if __name__ == '__main__':
    unittest.main()
