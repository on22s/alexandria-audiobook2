"""Native archive/checkpoint/CSV/phase artifacts bind summary to actual outputs."""
import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import numpy as np
import pandas as pd
from scipy.stats import wasserstein_distance
from tests import test_voice_dedup_source_cache as fixture


class VoiceAnalysisPhaseCompletionTests(unittest.TestCase):
    setUp = fixture.VoiceDedupSourceCacheTests.setUp
    write_zip = fixture.VoiceDedupSourceCacheTests.write_zip
    run_dedup = fixture.VoiceDedupSourceCacheTests.run_dedup

    def configure(self):
        if os.environ.get('VOICE_PHASE_SOURCE'):
            source = Path(os.environ['VOICE_PHASE_SOURCE'])
            tree = ast.parse(source.read_text())
            names = {'run_dedup', 'run_analyze', 'write_pipeline_summary', 'normalize_group_key'}
            nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
            exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), self.ns)
        self.analyze = self.root / 'analyze'
        self.analyze.mkdir()
        self.ns.update(pd=pd, PROSODY_METRICS=['duration'], wasserstein_distance=wasserstein_distance)
        self.ns['plt'] = SimpleNamespace(subplots=Mock(return_value=(Mock(), Mock())),
            tight_layout=Mock(), savefig=Mock(), close=Mock())
        self.ns['sns'] = SimpleNamespace(heatmap=Mock(), boxplot=Mock(), color_palette=lambda *_args: ['red'] * 10)
        self.ns['tqdm'].write = Mock()
        self.umap = SimpleNamespace(UMAP=Mock(return_value=SimpleNamespace(
            fit_transform=lambda values: np.zeros((len(values), 2)))))

    def analyze_phase(self):
        with redirect_stdout(io.StringIO()), patch.dict('sys.modules', {'umap': self.umap}):
            self.ns['run_analyze'](self.ns['fixture_model'], 'cpu', self.zips / '_deduped', self.analyze)

    def summary(self):
        with redirect_stdout(io.StringIO()):
            self.ns['write_pipeline_summary'](self.zips, self.output, self.analyze)
        return (self.output / 'pipeline_summary.log').read_text()

    def test_unique_narrator_without_plot_is_done_using_actual_output_group_key(self):
        self.configure()
        self.run_dedup()
        self.assertFalse(list(self.output.glob('dedup_*.png')))
        self.analyze_phase()
        self.assertIn('[DONE]    narrator', self.summary())
        state = json.loads((self.analyze / 'phase_state.json').read_text())
        self.assertEqual('complete', state['status'])
        self.assertNotIn('narrator', state['groups'])
        self.assertEqual(1, len(state['groups']))
        self.assertTrue((self.analyze / 'speaker_similarity_table.csv').is_file())

    def test_old_plots_cannot_mask_changed_source_or_missing_replacement(self):
        self.configure()
        self.run_dedup()
        self.analyze_phase()
        (self.output / 'dedup_narrator.png').write_bytes(b'old plot')
        self.assertIn('[DONE]    narrator', self.summary())
        self.write_zip(-0.25)
        self.assertNotIn('[DONE]', self.summary())
        self.run_dedup()
        self.analyze_phase()
        output = next((self.zips / '_deduped').glob('*.zip'))
        output.unlink()
        summary = self.summary()
        self.assertNotIn('[DONE]', summary)
        self.assertIn('[DEDUP]   narrator', summary)
        self.assertEqual(b'old plot', (self.output / 'dedup_narrator.png').read_bytes())

    def test_interrupted_analysis_cannot_reuse_old_completion_record(self):
        self.configure()
        self.run_dedup()
        self.analyze_phase()
        self.assertIn('[DONE]', self.summary())
        self.ns['plt'].subplots.side_effect = RuntimeError('interrupted plot')
        with self.assertRaisesRegex(RuntimeError, 'interrupted plot'):
            self.analyze_phase()
        state = json.loads((self.analyze / 'phase_state.json').read_text())
        self.assertEqual('running', state['status'])
        self.assertIn('[ANALYZE] narrator', self.summary())
        self.assertNotIn('[DONE]', self.summary())

    def test_partial_extraction_retries_missing_coverage_before_completion(self):
        self.configure()
        # Two real WAV rows; force only the second embedding to fail once.
        with zipfile.ZipFile(self.zip) as archive:
            wav = archive.read('train/one.wav')
        with zipfile.ZipFile(self.zip, 'w') as archive:
            archive.writestr('train/one.wav', wav)
            archive.writestr('train/two.wav', wav)
        self.run_dedup()
        original = self.ns['extract_embedding'].side_effect
        self.ns['extract_embedding'].side_effect = [np.array([1., 2.]), RuntimeError('clip failure')]
        self.analyze_phase()
        state = json.loads((self.analyze / 'phase_state.json').read_text())
        self.assertEqual('partial', state['status'])
        self.assertNotIn('[DONE]', self.summary())
        self.ns['extract_embedding'].side_effect = original
        self.ns['extract_embedding'].reset_mock()
        self.analyze_phase()
        self.assertEqual(2, self.ns['extract_embedding'].call_count)
        self.assertIn('[DONE]', self.summary())
        self.assertEqual('complete', json.loads((self.analyze / 'phase_state.json').read_text())['status'])

    def test_partial_dedup_preserves_stale_outputs_and_retries_incomplete_cache(self):
        self.configure()
        with zipfile.ZipFile(self.zip) as archive:
            wav = archive.read('train/one.wav')
        with zipfile.ZipFile(self.zip, 'w') as archive:
            archive.writestr('train/one.wav', wav)
            archive.writestr('train/two.wav', wav)
        stale = self.zips / '_deduped' / 'previous.zip'
        stale.parent.mkdir()
        stale.write_bytes(b'previous dataset bytes')
        original = self.ns['extract_embedding'].side_effect
        self.ns['extract_embedding'].side_effect = [np.array([1., 2.]), RuntimeError('clip failure')]
        self.run_dedup()
        self.assertEqual('partial', json.loads((self.output / 'phase_state.json').read_text())['status'])
        self.assertEqual(b'previous dataset bytes', stale.read_bytes())
        self.assertIn('[DEDUP]   narrator', self.summary())
        self.ns['extract_embedding'].side_effect = original
        self.ns['extract_embedding'].reset_mock()
        self.run_dedup()
        self.assertEqual(2, self.ns['extract_embedding'].call_count)
        self.assertFalse(stale.exists())
        self.assertEqual('complete', json.loads((self.output / 'phase_state.json').read_text())['status'])
