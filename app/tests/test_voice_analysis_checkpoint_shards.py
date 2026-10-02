"""Native checkpoint bytes, interrupted extraction, and compaction recovery."""
import ast
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import pickle
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import numpy as np
from tests import test_voice_dedup_source_cache as dedup_fixture
from tests.test_voice_analysis_cache import StopAfterSimilarity, SOURCE
from voice_analysis_cache import (load_voice_analysis_pickle,
    save_voice_analysis_checkpoint, compact_voice_analysis_checkpoints)


class VoiceCheckpointShardTests(unittest.TestCase):
    def fixture(self):
        fixture = dedup_fixture.VoiceDedupSourceCacheTests('test_unchanged_zip_reuses_extraction_and_preserves_cached_artifact')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if os.environ.get('VOICE_CHECKPOINT_SOURCE'):
            source = Path(os.environ['VOICE_CHECKPOINT_SOURCE'])
            names = {'run_dedup', 'run_analyze', '_load_pickle_cache', '_atomic_pickle_dump'}
            nodes = [node for node in ast.parse(source.read_text()).body
                     if isinstance(node, ast.FunctionDef) and node.name in names]
            exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), fixture.ns)
        return fixture

    def test_actual_phases_write_linear_embedding_bytes_and_preserve_full_pickle(self):
        for phase in ('dedup', 'analyze'):
            with self.subTest(phase=phase):
                fixture = self.fixture()
                for index in range(1, 8):
                    folder = fixture.zips / f'narrator{index}'
                    folder.mkdir()
                    shutil.copy2(fixture.zip, folder / 'book.zip')
                if phase == 'analyze':
                    source = fixture.root / 'analysis_zips'
                    source.mkdir()
                    for index in range(8):
                        shutil.copy2(fixture.zip, source / f'voice{index}.zip')
                fixture.ns['extract_embedding'] = Mock(return_value=np.ones(8192))
                written = []
                original = pickle.dump
                def measure(value, handle, *args, **kwargs):
                    start = handle.tell()
                    original(value, handle, *args, **kwargs)
                    written.append(handle.tell() - start)
                with redirect_stdout(io.StringIO()), patch.object(pickle, 'dump', side_effect=measure):
                    if phase == 'dedup':
                        fixture.ns['run_dedup'](fixture.ns['fixture_model'], 'cpu', fixture.zips, fixture.output)
                    else:
                        with self.assertRaises(StopAfterSimilarity):
                            fixture.ns['run_analyze'](fixture.ns['fixture_model'], 'cpu', source, fixture.output)
                with fixture.cache.open('rb') as handle:
                    full = pickle.load(handle)
                entries = full if phase == 'dedup' else full['embeddings']
                self.assertEqual(8, len(entries))
                print(f"{phase}: serialized={sum(written)} full_cache={fixture.cache.stat().st_size} writes={len(written)}")
                self.assertLess(sum(written), fixture.cache.stat().st_size * 2.5,
                                f'{phase} rewrote cumulative data: {written}')
                self.assertFalse(list(fixture.cache.with_suffix('.pkl.parts').glob('*.pkl')))

    def test_interrupted_group_checkpoint_resumes_without_base_or_reextracting_prior_groups(self):
        fixture = self.fixture()
        source = fixture.root / 'analysis_zips'
        source.mkdir()
        for index in range(3):
            shutil.copy2(fixture.zip, source / f'voice{index}.zip')
        fixture.ns['extract_embedding'] = Mock(side_effect=[np.ones(2), np.ones(2), KeyboardInterrupt()])
        with redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            fixture.ns['run_analyze'](fixture.ns['fixture_model'], 'cpu', source, fixture.output)
        progress = load_voice_analysis_pickle(fixture.cache, {})
        self.assertEqual({'voice0', 'voice1'}, set(progress['embeddings']))
        fixture.ns['extract_embedding'] = Mock(return_value=np.ones(2))
        with redirect_stdout(io.StringIO()), self.assertRaises(StopAfterSimilarity):
            fixture.ns['run_analyze'](fixture.ns['fixture_model'], 'cpu', source, fixture.output)
        self.assertEqual(1, fixture.ns['extract_embedding'].call_count)
        with fixture.cache.open('rb') as handle:
            full = pickle.load(handle)
        self.assertEqual(3, len(full['embeddings']))

    def test_compaction_interruption_replays_latest_updates_without_mutating_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cache.pkl'
            updates = {'embeddings': {'voice': [1]}, 'identities': {'voice': 'first'}}
            save_voice_analysis_checkpoint(updates, path, nested=True)
            save_voice_analysis_checkpoint({'embeddings': {'voice': [2]}, 'identities': {'voice': 'second'}}, path, nested=True)
            original = Path.unlink
            calls = []
            def interrupt(target, *args, **kwargs):
                if target.suffix == '.pkl':
                    calls.append(target)
                    if len(calls) == 2:
                        raise OSError('interrupted cleanup')
                return original(target, *args, **kwargs)
            with patch.object(Path, 'unlink', interrupt), self.assertRaises(OSError):
                compact_voice_analysis_checkpoints(path)
            self.assertEqual([2], load_voice_analysis_pickle(path, {})['embeddings']['voice'])
            self.assertEqual([1], updates['embeddings']['voice'])
            compact_voice_analysis_checkpoints(path)
            with path.open('rb') as handle:
                self.assertEqual('second', pickle.load(handle)['identities']['voice'])

    def test_failed_shard_publication_preserves_prior_progress_and_corrupt_shard_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cache.pkl'
            save_voice_analysis_checkpoint({'first': [1]}, path)
            with patch.object(pickle, 'dump', side_effect=OSError('disk failure')), self.assertRaises(OSError):
                save_voice_analysis_checkpoint({'second': [2]}, path)
            directory = path.with_suffix('.pkl.parts')
            (directory / '99999999999999999999-broken.pkl').write_bytes(b'broken pickle')
            with redirect_stdout(io.StringIO()) as output:
                progress = load_voice_analysis_pickle(path, {})
            self.assertEqual({'first': [1]}, progress)
            self.assertIn('unreadable cache', output.getvalue())
            self.assertTrue(list(directory.glob('*.corrupt')))
            self.assertFalse(list(directory.glob('*.tmp')))

    def test_downstream_readers_see_journal_only_data_and_missing_cache_still_fails(self):
        from experiments.build_unseen_holdout import volume_centroids
        from experiments.build_tight_dataset import cached_clips
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cache.pkl'
            for reader in (lambda: volume_centroids(path), lambda: cached_clips(path, {'volume'})):
                with self.assertRaises(FileNotFoundError):
                    reader()
            save_voice_analysis_checkpoint({'folder/volume': (np.array([[1., 0.], [1., 0.]]), ['one.wav', 'two.wav'])}, path)
            np.testing.assert_array_equal([1., 0.], volume_centroids(path)['volume'])
            self.assertEqual(['one.wav', 'two.wav'], cached_clips(path, {'volume'})['volume'][1])

    def test_clock_rollback_cannot_reverse_checkpoint_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cache.pkl'
            with patch('time.time_ns', side_effect=[9999, 1]):
                save_voice_analysis_checkpoint({'voice': 'first'}, path)
                save_voice_analysis_checkpoint({'voice': 'second'}, path)
            self.assertEqual('second', load_voice_analysis_pickle(path, {})['voice'])
