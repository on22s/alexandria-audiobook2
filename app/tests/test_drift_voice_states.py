"""Native scoring pairs follow the voice actually selected for each row."""
import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import wave

from project import ProjectManager
from tts import get_chunk_voice_inputs
import voice_drift


def save_pcm(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as handle:
        handle.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        handle.writeframes(struct.pack('<h', value) * 400)


def get_pcm(path):
    with wave.open(str(path), 'rb') as handle:
        return struct.unpack('<h', handle.readframes(1))[0]


class DriftVoiceStateTests(unittest.TestCase):
    def run_check(self, root, config, rows, indices=None):
        before = copy.deepcopy((config, rows))
        files = {path: path.read_bytes() for path in root.rglob('*.wav')}
        pairs, decoded = [], []
        def score(items, python):
            self.assertLessEqual(len(items), voice_drift.DRIFT_BATCH_SIZE)
            for target, reference in items:
                pairs.append((get_pcm(target), get_pcm(reference)))
                decoded.extend((Path(target), Path(reference)))
            return [.8] * len(items), None
        report = voice_drift.check_voice_drift(rows, config, str(root), sys.executable, .45,
            indices=indices, resolve_asset_path=lambda path: str(root / path), score_pairs=score)
        self.assertIsNone(report['error'])
        self.assertEqual(before, (config, rows))
        self.assertTrue(all(not path.exists() for path in decoded))
        self.assertEqual(files, {path: path.read_bytes() for path in files})
        return report, pairs

    def rows(self, root, count=3, speaker='Hero'):
        rows = []
        for index in range(count):
            path = root / f'line-{index}.wav'
            save_pcm(path, 100 + index)
            rows.append({'id': index, 'uid': f'u{index}', 'speaker': speaker,
                         'text': f'Synthetic line {index}.', 'status': 'done', 'audio_path': path.name})
        return rows

    def test_clone_and_lora_timeline_states_choose_their_own_configured_reference(self):
        for adult in ({'type': 'clone', 'ref_audio': 'adult.wav'},
                      {'type': 'lora', 'adapter_path': 'adult-adapter'},
                      {'type': 'builtin_lora', 'adapter_path': 'adult-adapter'}):
            with self.subTest(adult=adult), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                save_pcm(root / 'young.wav', 11)
                save_pcm(root / 'adult.wav', 22)
                save_pcm(root / 'adult-adapter/ref_sample.wav', 33)
                config = {'Hero': {'type': 'clone', 'ref_audio': 'young.wav', 'versions': {'adult': adult},
                                   'version_timeline': [{'from_index': 1, 'version_id': 'adult'}]}}
                rows = self.rows(root)
                report, pairs = self.run_check(root, config, rows)
                self.assertEqual([(100, 11), (101, 22 if adult['type'] == 'clone' else 33),
                                  (102, 22 if adult['type'] == 'clone' else 33)], pairs)
                self.assertEqual([.8, .8, .8], [row['score'] for row in report['results']])

    def test_timeline_alias_uses_selected_root_reference_before_ensemble_members(self):
        for root_voice in ({'type': 'clone', 'ref_audio': 'adult.wav'},
                           {'type': 'builtin_lora', 'adapter_path': 'adult-adapter'},
                           {'type': 'ensemble', 'members': ['Member']}):
            with self.subTest(root_voice=root_voice), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                save_pcm(root / 'young.wav', 11)
                save_pcm(root / 'adult.wav', 22)
                save_pcm(root / 'adult-adapter/ref_sample.wav', 33)
                config = {'Hero': {'type': 'clone', 'ref_audio': 'young.wav',
                                  'versions': {'adult': {'alias_of': 'Grown'}},
                                  'version_timeline': [{'from_index': 1, 'version_id': 'adult'}]},
                          'Grown': root_voice,
                          'Member': {'type': 'clone', 'ref_audio': 'adult.wav'}}
                rows = self.rows(root)
                inputs = get_chunk_voice_inputs(config, rows[2], 2)
                self.assertEqual('Grown', next(iter(inputs)))
                report, pairs = self.run_check(root, config, rows, [2])
                expected = {'clone': 22, 'builtin_lora': 33, 'ensemble': 101}[root_voice['type']]
                self.assertEqual([(102, expected)], pairs)
                label = {'clone': 'clone:Grown', 'builtin_lora': 'lora:Grown',
                         'ensemble': 'chunk:u1'}[root_voice['type']]
                self.assertEqual(label, report['results'][0]['reference'])

    def test_custom_design_and_ensemble_fallbacks_only_use_compatible_states(self):
        for kind in ('custom', 'design', 'ensemble'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if kind == 'ensemble':
                    config = {'Hero': {'type': 'ensemble', 'members': ['Member']},
                              'Member': {'type': 'custom', 'voice': 'Ryan', 'versions': {'adult': {'voice': 'Vivian'}},
                                         'version_timeline': [{'from_index': 1, 'version_id': 'adult'}]}}
                else:
                    config = {'Hero': {'type': kind, 'voice': 'Ryan', 'description': 'Young voice',
                                      'versions': {'adult': {'voice': 'Vivian', 'description': 'Adult voice'}},
                                      'version_timeline': [{'from_index': 1, 'version_id': 'adult'}]}}
                report, pairs = self.run_check(root, config, self.rows(root), [2])
                self.assertEqual([(102, 101)], pairs)
                self.assertEqual('chunk:u1', report['results'][0]['reference'])

    def test_alias_and_dynamic_narrator_use_same_effective_inputs_as_generation(self):
        for speaker, context in (('Alias', {}), ('NARRATOR', {'focus_speaker': 'Hero'})):
            with self.subTest(speaker=speaker), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                save_pcm(root / 'young.wav', 11)
                save_pcm(root / 'adult.wav', 22)
                config = {'Hero': {'type': 'clone', 'ref_audio': 'young.wav',
                                  'versions': {'adult': {'ref_audio': 'adult.wav'}},
                                  'version_timeline': [{'from_index': 1, 'version_id': 'adult'}]},
                          'Alias': {'alias_of': 'Hero'},
                          'NARRATOR': {'type': 'custom', 'voice': 'Ryan', 'narrator_strategy': 'focus'}}
                rows = [dict(row, **context) for row in self.rows(root, speaker=speaker)]
                manager = ProjectManager(str(root))
                original = copy.deepcopy(config)
                for index, row in enumerate(rows):
                    self.assertEqual(manager.get_chunk_voice_inputs(config, row, index),
                                     get_chunk_voice_inputs(config, row, index))
                report, pairs = self.run_check(root, config, rows)
                self.assertEqual([(100, 11), (101, 22), (102, 22)], pairs)
                self.assertEqual(original, config)

    def test_unselected_compatible_reference_survives_batch_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = self.rows(root, 67)
            config = {'Hero': {'type': 'custom', 'voice': 'Ryan', 'versions': {'adult': {'voice': 'Vivian'}},
                               'version_timeline': [{'from_index': 1, 'version_id': 'adult'}]}}
            report, pairs = self.run_check(root, config, rows, list(range(2, 67)))
            self.assertEqual(65, len(pairs))
            self.assertTrue(all(reference == 101 for _, reference in pairs))
            self.assertTrue(all(row['reference'] == 'chunk:u1' for row in report['results']))

    def test_single_state_self_reference_is_explicitly_unmeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = self.rows(root, 2)
            config = {'Hero': {'type': 'custom', 'voice': 'Ryan', 'versions': {'adult': {'voice': 'Vivian'}},
                               'version_timeline': [{'from_index': 1, 'version_id': 'adult'}]}}
            report, pairs = self.run_check(root, config, rows, [1])
            self.assertEqual([], pairs)
            row = report['results'][0]
            self.assertIsNone(row['score'])
            self.assertFalse(row['flagged'])
            self.assertEqual('self', row['reference'])
            self.assertIn('not measured', row['error'])

    def test_late_compatible_reference_change_still_refuses_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = ProjectManager(str(root))
            rows = self.rows(root)
            config = {'Hero': {'type': 'custom', 'voice': 'Ryan', 'versions': {'adult': {'voice': 'Vivian'}},
                               'version_timeline': [{'from_index': 1, 'version_id': 'adult'}]}}
            manager.save_chunks(rows)
            rows = manager.load_chunks()
            report, pairs = self.run_check(root, config, rows, [2])
            self.assertEqual([(102, 101)], pairs)
            changed = copy.deepcopy(rows)
            changed[1]['audio_revision'] = 'newer-compatible-reference'
            manager.save_chunks(changed)
            before = Path(manager.chunks_path).read_bytes()
            self.assertEqual(0, voice_drift.apply_drift_results(manager, report['results'], .45,
                                                              expected_chunks=rows))
            self.assertEqual(before, Path(manager.chunks_path).read_bytes())
            self.assertNotIn('drift', manager.load_chunks()[2])


if __name__ == '__main__':
    unittest.main()
