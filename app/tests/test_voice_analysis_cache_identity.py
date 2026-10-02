"""Real phase checkpoints must invalidate stale inputs without discarding recovery."""
import ast
from contextlib import redirect_stdout
import io
import inspect
import os
from pathlib import Path
import pickle
import tempfile
import unittest
from unittest.mock import Mock
import zipfile

import numpy as np
import soundfile as sf
from tests.test_voice_analysis_cache import load_analysis_functions, SOURCE, StopAfterSimilarity
from tests import test_voice_dedup_source_cache as dedup_fixture


class VoiceCacheIdentityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.zips = self.root / 'zips'
        self.zips.mkdir()
        self.output = self.root / 'analyze'
        self.output.mkdir()
        self.cache = self.output / 'embeddings_cache.pkl'
        self.zip = self.zips / 'voice.zip'
        self.ns = load_analysis_functions()
        self.model = self.ns['fixture_model']
        self.model_file = self.root / 'embedding.ckpt'
        self.model_file.write_bytes(b'old model artifact')
        self.model._alexandria_model_files = {'embedding': self.model_file}
        self.stage_file = self.root / 'stage.py'
        self.stage_file.write_bytes(SOURCE.read_bytes())
        self.ns['__file__'] = str(self.stage_file)
        nodes = [node for node in ast.parse(SOURCE.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name == 'load_wav_from_zip']
        self.ns['sf'] = sf
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), self.ns)
        self.ns['extract_embedding'] = Mock(side_effect=lambda wav, sr, model, device: np.array([wav[0], 1.0]))
        self.write_zip(self.zip, 0.25)

    @staticmethod
    def write_zip(path, value, name='train/one.wav', count=1):
        stream = io.BytesIO()
        sf.write(stream, np.full(160, value), 16000, format='WAV', subtype='PCM_16')
        with zipfile.ZipFile(path, 'w') as archive:
            for index in range(count):
                archive.writestr(name if count == 1 else f'train/{index}.wav', stream.getvalue())

    def run_analysis(self, seed=42):
        with redirect_stdout(io.StringIO()) as logs:
            try:
                kwargs = {'seed': seed} if 'seed' in inspect.signature(self.ns['run_analyze']).parameters else {}
                self.ns['run_analyze'](self.model, 'cpu', self.zips, self.output, **kwargs)
            except StopAfterSimilarity:
                pass
        with self.cache.open('rb') as handle:
            return pickle.load(handle), logs.getvalue()

    def test_unchanged_then_same_name_size_timestamp_audio_replacement(self):
        first, _ = self.run_analysis()
        old_bytes = self.cache.read_bytes()
        self.run_analysis()
        self.assertEqual(1, self.ns['extract_embedding'].call_count)
        self.assertEqual(old_bytes, self.cache.read_bytes())
        stamp = self.zip.stat().st_mtime_ns
        old_size = self.zip.stat().st_size
        self.write_zip(self.zip, -0.25)
        os.utime(self.zip, ns=(stamp, stamp))
        self.assertEqual(old_size, self.zip.stat().st_size)
        second, _ = self.run_analysis()
        self.assertEqual(2, self.ns['extract_embedding'].call_count)
        self.assertAlmostEqual(-0.25, second['embeddings']['voice'][0, 0])
        self.assertNotEqual(first['identities']['voice'], second['identities']['voice'])

    def test_model_state_artifact_code_dependencies_samples_and_seed_invalidate(self):
        self.run_analysis()
        changes = [lambda: setattr(self.model, '_alexandria_state_sha256', '2' * 64),
                   lambda: self.model_file.write_bytes(b'new model artifact'),
                   lambda: self.stage_file.write_bytes(b'new feature definition'),
                   lambda: self.model._alexandria_dependency_versions.update(fixture='2'),
                   lambda: self.ns.update(ANALYZE_SAMPLES=200)]
        for index, change in enumerate(changes, 2):
            with self.subTest(change=index):
                change()
                self.run_analysis()
                self.assertEqual(index, self.ns['extract_embedding'].call_count)
        self.run_analysis(seed=43)
        self.assertEqual(7, self.ns['extract_embedding'].call_count)

    def test_normalized_group_membership_add_remove_invalidates_entire_group(self):
        self.run_analysis()
        other = self.zips / 'voice-converted.zip'
        self.write_zip(other, -0.25, 'train/two.wav')
        added, _ = self.run_analysis()
        self.assertEqual(3, self.ns['extract_embedding'].call_count)
        self.assertEqual(2, len(added['embeddings']['voice']))
        self.assertEqual({str(self.zip), str(other)}, {p for p, n in added['wav_names']['voice']})
        other.unlink()
        removed, _ = self.run_analysis()
        self.assertEqual(4, self.ns['extract_embedding'].call_count)
        self.assertEqual(1, len(removed['embeddings']['voice']))
        self.assertEqual([str(self.zip.resolve())], [r['path'] for r in removed['identities']['voice']['document']['sources']])

    def test_removed_group_is_retained_for_recovery_but_excluded_from_reports(self):
        self.run_analysis()
        self.zip.rename(self.zips / 'current.zip')
        current, logs = self.run_analysis()
        self.assertEqual({'voice', 'current'}, set(current['embeddings']))
        self.assertIn('Analyzing 1 groups', logs)
        self.assertNotIn('Analyzing 2 groups', logs)
        self.assertEqual(2, self.ns['extract_embedding'].call_count)
        _, logs = self.run_analysis()
        self.assertIn('Analyzing 1 groups', logs)
        self.assertEqual(2, self.ns['extract_embedding'].call_count)

    def test_legacy_entry_without_identity_rebuilds(self):
        legacy = {'embeddings': {'voice': np.array([[99., 99.]])},
                  'prosody': {'voice': [{'duration': 99.}]},
                  'wav_names': {'voice': [('old.zip', 'old.wav')]}}
        self.cache.write_bytes(pickle.dumps(legacy))
        current, _ = self.run_analysis()
        self.assertEqual(1, self.ns['extract_embedding'].call_count)
        self.assertAlmostEqual(0.25, current['embeddings']['voice'][0, 0])
        self.assertIn('voice', current['identities'])

    def test_source_or_model_change_during_extraction_leaves_checkpoint_intact(self):
        self.run_analysis()
        before = self.cache.read_bytes()
        for target in ('source', 'model'):
            with self.subTest(target=target):
                self.write_zip(self.zip, 0.5)
                def changed(wav, sr, model, device):
                    if target == 'source':
                        self.write_zip(self.zip, -0.5)
                    else:
                        self.model_file.write_bytes(b'changed during extraction')
                    return np.array([wav[0], 1.0])
                self.ns['extract_embedding'].side_effect = changed
                with self.assertRaisesRegex(RuntimeError, 'identity changed during extraction'):
                    self.run_analysis()
                self.assertEqual(before, self.cache.read_bytes())

    def test_missing_model_artifact_refuses_before_extracting_or_overwriting(self):
        self.run_analysis()
        before = self.cache.read_bytes()
        self.model_file.unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_analysis()
        self.assertEqual(1, self.ns['extract_embedding'].call_count)
        self.assertEqual(before, self.cache.read_bytes())


class VoiceDedupFullIdentityTests(unittest.TestCase):
    setUp = dedup_fixture.VoiceDedupSourceCacheTests.setUp
    write_zip = dedup_fixture.VoiceDedupSourceCacheTests.write_zip

    def run_dedup(self, seed=42):
        with redirect_stdout(io.StringIO()):
            kwargs = {'seed': seed} if 'seed' in inspect.signature(self.ns['run_dedup']).parameters else {}
            self.ns['run_dedup'](self.ns['fixture_model'], 'cpu', self.zips, self.output, **kwargs)
        with self.cache.open('rb') as handle:
            return pickle.load(handle)

    def test_model_stage_and_sampling_changes_preserve_old_entries_and_reextract(self):
        model = self.ns['fixture_model']
        stage = self.root / 'stage.py'
        stage.write_bytes(SOURCE.read_bytes())
        self.ns['__file__'] = str(stage)
        artifact = self.root / 'weights.ckpt'
        artifact.write_bytes(b'old weights')
        model._alexandria_model_files = {'embedding': artifact}
        first = self.run_dedup()
        for index, change in enumerate([
            lambda: setattr(model, '_alexandria_state_sha256', '2' * 64),
            lambda: artifact.write_bytes(b'new weights'),
            lambda: stage.write_bytes(b'new stage'),
            lambda: model._alexandria_dependency_versions.update(fixture='2'),
            lambda: self.ns.update(DEDUP_SAMPLES=100)], 2):
            with self.subTest(change=index):
                change()
                current = self.run_dedup()
                self.assertEqual(index, self.ns['extract_embedding'].call_count)
                self.assertEqual(index, len(current))
                self.assertIn(next(iter(first)), current)
        self.run_dedup(seed=43)
        self.assertEqual(7, self.ns['extract_embedding'].call_count)
        self.run_dedup(seed=43)
        self.assertEqual(7, self.ns['extract_embedding'].call_count)

    def test_model_artifact_change_mid_extraction_cannot_publish_a_cache(self):
        artifact = self.root / 'weights.ckpt'
        artifact.write_bytes(b'old weights')
        self.ns['fixture_model']._alexandria_model_files = {'embedding': artifact}
        self.run_dedup()
        before = self.cache.read_bytes()
        self.write_zip(0.5)
        def changed(wav, sr, model, device):
            artifact.write_bytes(b'new weights')
            return np.array([wav[0], 1.0])
        self.ns['extract_embedding'].side_effect = changed
        with self.assertRaisesRegex(RuntimeError, 'identity changed during extraction'):
            self.run_dedup()
        self.assertEqual(before, self.cache.read_bytes())
