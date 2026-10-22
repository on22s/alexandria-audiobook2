from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import wave

from audio_generation_owner import (AudioGenerationCancelled, AudioGenerationOwner,
                                     ensure_audio_generation_owner)
from audio_validation import GeneratedAudioError
from experiments.plan_dialogue_continuity import get_continuity_experiment_packet
from experiments.render_dialogue_continuity import save_continuity_render_pair
from tests import test_dialogue_continuity as fixtures


class ContinuityRenderTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.dumps({'version': 1, 'narrator_speakers': ['NARRATOR'],
                              'entries': fixtures.DialogueContinuityTests().rows()}).encode()
        self.packet = get_continuity_experiment_packet(self.raw)
        self.voices = {'Anna': {'type': 'custom', 'seed': 42}}
        self.calls = []
        self.cancelled = False

    @contextmanager
    def admit(self, **kwargs):
        if self.cancelled:
            raise AudioGenerationCancelled()
        yield

    def engine(self, failure=None):
        def render(text, instruct, speaker, voices, output, *, capture_input):
            self.calls.append((text, instruct, speaker, voices, capture_input))
            with wave.open(output, 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
                wav.writeframes(b'\x00\x10' * 2400)
            data = Path(output).read_bytes()
            if failure == 'cancel':
                self.cancelled = True
            return SimpleNamespace(success=failure != 'false', synthesis_receipt={'captured': True},
                speech_input={'text': text, 'audio': {'sha256': hashlib.sha256(data).hexdigest(),
                              'size': len(data) if failure != 'stale' else 1}})
        return SimpleNamespace(generate_voice_result=render, get_model_settings=lambda: {'mode': 'fixture'})

    def save(self, destination, engine=None):
        return save_continuity_render_pair(engine or self.engine(), self.raw, self.packet,
                                           0, self.voices, destination)

    def test_actual_pair_files_bind_requests_captured_text_seed_and_audio(self):
        with tempfile.TemporaryDirectory() as directory, ensure_audio_generation_owner(
                AudioGenerationOwner('a' * 32, self.admit)):
            destination = Path(directory) / 'pair'
            receipt = self.save(destination)
            self.assertEqual(receipt, json.loads((destination / 'receipt.json').read_text()))
            self.assertEqual(2, len(self.calls))
            self.assertEqual(self.calls[0][0], self.calls[1][0])
            self.assertNotEqual(self.calls[0][1], self.calls[1][1])
            self.assertEqual(self.calls[0][3], self.calls[1][3])
            for arm in ('baseline', 'deterministic'):
                data = (destination / (arm + '.wav')).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), receipt['arms'][arm]['audio']['sha256'])
            before = (destination / 'receipt.json').read_bytes()
            with self.assertRaises(FileExistsError):
                self.save(destination)
            self.assertEqual(before, (destination / 'receipt.json').read_bytes())

    def test_failed_stale_and_cancelled_audio_never_publish_a_pair(self):
        for failure in ('false', 'stale', 'cancel'):
            self.cancelled = False
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / 'pair'
                with ensure_audio_generation_owner(AudioGenerationOwner('a' * 32, self.admit)):
                    with self.assertRaises((GeneratedAudioError, AudioGenerationCancelled)):
                        self.save(destination, self.engine(failure))
                self.assertFalse(destination.exists())
                self.assertFalse(list(Path(directory).glob('.continuity-*')))

    def test_unowned_stale_plan_and_random_seed_refuse_before_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'pair'
            with self.assertRaisesRegex(ValueError, 'owned'):
                self.save(destination)
            with ensure_audio_generation_owner(AudioGenerationOwner('a' * 32, self.admit)):
                self.packet['plan']['rows'][0]['baseline']['text'] = 'Changed'
                with self.assertRaisesRegex(ValueError, 'changed'):
                    self.save(destination)
                self.packet = get_continuity_experiment_packet(self.raw)
                self.voices['Anna']['seed'] = -1
                with self.assertRaisesRegex(ValueError, 'seed'):
                    self.save(destination)
            self.assertEqual([], self.calls)

    def test_effective_alias_and_ensemble_seeds_are_validated_and_receipted(self):
        cases = [({'Anna': {'alias_of': 'Real', 'seed': 42},
                   'Real': {'type': 'custom', 'seed': -1}}, None),
                 ({'Anna': {'alias_of': 'Real', 'seed': -1},
                   'Real': {'type': 'custom', 'seed': 7}}, {'Real': 7}),
                 ({'Anna': {'type': 'ensemble', 'seed': 42, 'members': ['One', 'Two']},
                   'One': {'type': 'custom', 'seed': 8},
                   'Two': {'type': 'custom', 'seed': -1}}, None),
                 ({'Anna': {'type': 'ensemble', 'members': ['One', 'Two']},
                   'One': {'type': 'custom', 'seed': 8},
                   'Two': {'type': 'custom', 'seed': 9}}, {'One': 8, 'Two': 9})]
        for voices, seeds in cases:
            with self.subTest(seeds=seeds), tempfile.TemporaryDirectory() as directory:
                self.calls = []; self.voices = voices
                with ensure_audio_generation_owner(AudioGenerationOwner('a' * 32, self.admit)):
                    if seeds is None:
                        with self.assertRaisesRegex(ValueError, 'seed'):
                            self.save(Path(directory) / 'pair')
                        self.assertEqual([], self.calls)
                    else:
                        result = self.save(Path(directory) / 'pair')
                        self.assertEqual(seeds, result['seeds'])
                        self.assertEqual(self.calls[0][3], self.calls[1][3])

    def test_materialized_timeline_seed_is_the_mapping_passed_to_both_arms(self):
        self.voices = {'Anna': {'type': 'custom', 'seed': -1,
            'versions': {'fixed': {'seed': 10}},
            'version_timeline': [{'from_index': 2, 'version_id': 'fixed'}]}}
        with tempfile.TemporaryDirectory() as directory, ensure_audio_generation_owner(
                AudioGenerationOwner('a' * 32, self.admit)):
            with self.assertRaisesRegex(ValueError, 'seed'):
                self.save(Path(directory) / 'too-early')
            self.assertEqual([], self.calls)
            receipt = save_continuity_render_pair(self.engine(), self.raw, self.packet,
                2, self.voices, Path(directory) / 'pair')
            self.assertEqual({'Anna': 10}, receipt['seeds'])
            self.assertEqual([10, 10], [call[3]['Anna']['seed'] for call in self.calls])
            self.assertEqual(-1, self.voices['Anna']['seed'])

    def test_alias_target_timeline_is_selected_before_seed_validation(self):
        self.voices = {'Anna': {'alias_of': 'Old',
            'versions': {'new': {'alias_of': 'New'}},
            'version_timeline': [{'from_index': 2, 'version_id': 'new'}]},
            'Old': {'type': 'custom', 'seed': -1}, 'New': {'type': 'custom', 'seed': 7}}
        with tempfile.TemporaryDirectory() as directory, ensure_audio_generation_owner(
                AudioGenerationOwner('a' * 32, self.admit)):
            with self.assertRaisesRegex(ValueError, 'seed'):
                self.save(Path(directory) / 'before')
            self.assertEqual([], self.calls)
            receipt = save_continuity_render_pair(self.engine(), self.raw, self.packet,
                2, self.voices, Path(directory) / 'after')
            self.assertEqual({'New': 7}, receipt['seeds'])
            for call in self.calls:
                from tts import get_resolved_speaker
                self.assertEqual('New', get_resolved_speaker(call[2], call[3]))
                self.assertEqual(7, call[3]['New']['seed'])
            self.assertEqual('Old', self.voices['Anna']['alias_of'])
