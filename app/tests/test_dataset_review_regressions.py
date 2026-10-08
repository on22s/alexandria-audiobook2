"""Reviewed state transitions use actual archives, flat JSON and checkpoint bundles."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import soundfile as sf
from benchmark_fixtures import build_lora_training_manifest
from benchmark_runner import _validate_lora_training_fixture
from dataset_metadata import get_training_dataset_preflight
from lora_training_benchmark import execute_fixture
from routers import dataset_builder as builder, lora
from voice_clustering import cluster_voices
from voice_dataset_merge import merge_voice_datasets
from voice_manifest import is_adapter_named
from tests.test_lora_candidate_promotion import _write_real_checkpoint


class DatasetReviewRegressions(unittest.TestCase):
    def test_transitive_manual_merge_refuses_a_split_conflict(self):
        with self.assertRaisesRegex(ValueError, 'conflict'):
            cluster_voices(['a', 'b', 'c'], np.eye(3), .9,
                           {'merge': [['a', 'b'], ['b', 'c']], 'split': [['a', 'c']]})

    def test_merged_archive_rewrites_self_and_separate_references(self):
        for separate in (False, True):
            with self.subTest(separate=separate), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                wav = root / 'source.wav'
                sf.write(wav, np.sin(np.arange(2400) / 30) * .1, 24000)
                reference = 'reference.wav' if separate else 'train/original.wav'
                source = root / 'source.zip'
                with zipfile.ZipFile(source, 'w') as archive:
                    archive.writestr('train/original.wav', wav.read_bytes())
                    if separate:
                        archive.writestr(reference, wav.read_bytes())
                    archive.writestr('metadata.jsonl', json.dumps({
                        'audio_filepath': 'train/original.wav', 'text': 'Known words.', 'ref_audio': reference}) + '\n')
                merged = root / 'merged.zip'
                merge_voice_datasets([source], merged)
                with zipfile.ZipFile(merged) as archive:
                    archive.extractall(root / 'dataset')
                preflight = get_training_dataset_preflight(str(root / 'dataset'))
                selected = root / 'dataset' / preflight['reference']
                self.assertEqual(wav.read_bytes(), selected.read_bytes())
                self.assertNotEqual(reference, preflight['reference'])

    def test_metadata_edits_invalidate_inherited_rows_and_preserve_explicit_seed(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(builder, 'DATASET_BUILDER_DIR', folder), \
             patch.object(builder, 'process_state', {'dataset_builder': {'running': False}}):
            samples = [{'text': 'words', 'emotion': '', 'seed': seed,
                        'status': 'done', 'audio_url': '/old.wav'} for seed in ('', '7')]
            builder._save_builder_state('voice', {'description': 'warm', 'global_seed': '1', 'samples': samples})
            builder._dataset_builder_update_meta_sync(builder.DatasetBuilderUpdateMetaRequest(
                name='voice', description='warm', global_seed='2'))
            state = builder._load_builder_state('voice')
            self.assertEqual(['pending', 'done'], [row['status'] for row in state['samples']])
            self.assertIsNone(state['samples'][0]['audio_url'])
            self.assertEqual('/old.wav', state['samples'][1]['audio_url'])
            builder._dataset_builder_update_meta_sync(builder.DatasetBuilderUpdateMetaRequest(
                name='voice', description='bright', global_seed='2'))
            self.assertEqual(['pending', 'pending'], [row['status'] for row in builder._load_builder_state('voice')['samples']])

    def test_checkpoint_promotion_and_rollback_discard_old_audition(self):
        with tempfile.TemporaryDirectory() as folder:
            models = Path(folder)
            adapter = models / 'voice'
            _write_real_checkpoint(adapter, 'production')
            _write_real_checkpoint(adapter / 'candidates/epoch_002', 'candidate')
            manifest = models / 'manifest.json'
            manifest.write_text(json.dumps([{'id': 'voice',
                'evaluation': {'recommended_candidate': 'epoch_002'},
                'evaluation_candidates': [{'id': 'epoch_002'}]}]))
            preview = adapter / 'preview_sample.wav'
            preview.write_bytes(b'old audition')
            original = (adapter / 'adapter_model.safetensors').read_bytes()
            lora._promote_lora_candidate('voice', folder, str(manifest))
            self.assertFalse(preview.exists())
            self.assertNotEqual(original, (adapter / 'adapter_model.safetensors').read_bytes())
            preview.write_bytes(b'candidate audition')
            lora._rollback_lora_promotion('voice', folder, str(manifest))
            self.assertFalse(preview.exists())
            self.assertEqual(original, (adapter / 'adapter_model.safetensors').read_bytes())

    def test_timestamp_training_ids_are_unnamed_and_descriptive_ids_stay_named(self):
        self.assertFalse(is_adapter_named({'id': 'book_1791412345', 'dataset_id': 'book'}))
        self.assertFalse(is_adapter_named({'id': 'book', 'dataset_id': 'book'}))
        self.assertTrue(is_adapter_named({'id': 'warm_baritone_30s_m', 'dataset_id': 'book'}))
        self.assertTrue(is_adapter_named({'id': 'legacy'}))

    def test_naming_cli_publishes_a_descriptive_id_for_fresh_batch_adapter(self):
        from tests.test_support import write_test_adapter
        with tempfile.TemporaryDirectory() as folder:
            models = Path(folder)
            row = {'id': 'book_1791480000', 'dataset_id': 'book', 'name': 'book',
                   'voice_profile': 'Warm baritone in his 30s; best for fantasy.'}
            write_test_adapter(models / row['id'])
            manifest = models / 'manifest.json'
            manifest.write_text(json.dumps([row]))
            script = Path(__file__).resolve().parents[2] / 'tools/voice_lab/name_voices.py'
            result = subprocess.run([sys.executable, str(script), '--models-dir', folder,
                                     '--manifest', str(manifest), '--apply'], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            renamed = json.loads(manifest.read_text())[0]
            self.assertNotEqual(row['id'], renamed['id'])
            self.assertTrue((models / renamed['id'] / 'adapter_model.safetensors').is_file())
            self.assertFalse((models / row['id']).exists())

    def test_real_training_fixture_copies_and_hashes_external_reference_and_transcript(self):
        from lora_evidence import get_file_sha256
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            dataset = root / 'dataset'
            dataset.mkdir()
            for name in ('sample.wav', 'ref.wav'):
                sf.write(dataset / name, np.sin(np.arange(2400) / (30 if name == 'ref.wav' else 40)) * .1, 24000)
            (dataset / 'metadata.jsonl').write_text(json.dumps({'audio_filepath': 'sample.wav', 'ref_audio': 'ref.wav', 'text': 'Sample words.'}) + '\n')
            (dataset / 'ref_text.txt').write_text('Reference words.')
            fixture = build_lora_training_manifest([{'dataset_path': 'dataset', 'sample_count': 1}], folder)['fixtures'][0]
            self.assertIn('ref.wav', fixture['audio_sha256'])
            _validate_lora_training_fixture(fixture, folder)
            def train(command, **kwargs):
                staged = Path(command[command.index('--data_dir') + 1])
                self.assertEqual('ref.wav', get_training_dataset_preflight(str(staged))['reference'])
                self.assertEqual('Reference words.', (staged / 'ref_text.txt').read_text())
                self.assertEqual((dataset / 'ref.wav').read_bytes(), (staged / 'ref.wav').read_bytes())
                output = Path(command[command.index('--output_dir') + 1])
                output.mkdir()
                weights = output / 'adapter_model.safetensors'
                weights.write_bytes(b'fixture weights')
                (output / 'training_meta.json').write_text(json.dumps({'training_time_seconds': 2,
                    'num_samples': 1, 'epochs': 1, 'final_loss': 1, 'best_loss': 1,
                    'oom_skips': 0, 'checkpoint_sha256': get_file_sha256(weights)}))
                return subprocess.CompletedProcess(command, 0, '', '')
            with patch('lora_training_benchmark.subprocess.run', side_effect=train):
                execute_fixture({**fixture, 'root_dir': folder}, 'python', 'train.py', str(root / 'output'))
            (dataset / 'ref.wav').write_bytes(b'changed reference')
            with self.assertRaisesRegex(ValueError, 'hash changed'):
                _validate_lora_training_fixture(fixture, folder)
