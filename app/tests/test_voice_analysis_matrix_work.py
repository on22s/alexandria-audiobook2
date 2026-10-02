"""Compare actual phase matrices and CSVs with direct SciPy reference values."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import zipfile

import numpy as np
import pandas as pd
from scipy.spatial.distance import cdist, pdist, squareform
from scipy.stats import wasserstein_distance
from tests import test_voice_analysis_cache as analysis_fixture
from tests import test_voice_dedup_source_cache as dedup_fixture
from voice_clustering import cluster_voices, load_cluster_overrides


class StopAfterMatrices(Exception):
    pass


class NumpyCounter:
    def __init__(self):
        self.array = Mock(wraps=np.array)

    def __getattr__(self, name):
        return getattr(np, name)


class VoiceAnalysisMatrixWorkTests(unittest.TestCase):
    def analyze(self, group_count, nonfinite=False, empty_prosody=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zips, output = root / 'zips', root / 'analyze'
            zips.mkdir()
            ns = analysis_fixture.load_analysis_functions()
            rng = np.random.RandomState(73)
            embeddings, prosody = {}, {}
            metrics = ['pitch', 'rms', 'duration']
            for index in range(group_count):
                group = f'group_{index}'
                size = index % 3 + 1
                embeddings[group] = rng.normal(size=(size, 4))
                prosody[group] = [{metric: float(index * 10 + row + m * 0.5)
                                   for m, metric in enumerate(metrics)} for row in range(size)]
                if nonfinite and index == 0:
                    prosody[group][0]['pitch'] = float('nan')
                with zipfile.ZipFile(zips / (group + '.zip'), 'w') as archive:
                    for row in range(size):
                        archive.writestr(f'train/{row}.wav', b'controlled decode')
            ns['load_wav_from_zip'] = lambda path, name, **_kwargs: ((Path(path).stem, int(Path(name).stem)), 16000)
            ns['extract_embedding'] = lambda wav, sr, model, device: embeddings[wav[0]][wav[1]]
            ns['extract_prosody'] = lambda wav, sr: prosody[wav[0]][wav[1]].copy()
            if empty_prosody:
                ns['extract_prosody'] = Mock(side_effect=ValueError('controlled feature extraction failure'))
                ns['tqdm'].write = Mock()
            ns['PROSODY_METRICS'] = metrics
            ns['cdist'] = Mock(wraps=cdist)
            ns['wasserstein_distance'] = Mock(wraps=wasserstein_distance)
            ns['np'] = NumpyCounter()
            ns['pd'] = pd
            matrices = []
            def subplot(**kwargs):
                if len(matrices) == 2:
                    raise StopAfterMatrices()
                return Mock(), Mock()
            ns['plt'] = SimpleNamespace(subplots=subplot, tight_layout=Mock(), savefig=Mock(), close=Mock())
            ns['sns'] = SimpleNamespace(heatmap=lambda matrix, **kwargs: matrices.append(matrix.copy()))
            with redirect_stdout(io.StringIO()):
                try:
                    ns['run_analyze'](ns['fixture_model'], 'cpu', zips, output)
                except StopAfterMatrices:
                    pass
            labels = sorted(embeddings)
            expected_similarity = np.zeros((group_count, group_count))
            expected_metrics = []
            for i, first in enumerate(labels):
                for j, second in enumerate(labels):
                    if i == j:
                        values = embeddings[first]
                        expected_similarity[i, j] = np.mean(1 - squareform(pdist(values, 'cosine'))) if len(values) > 1 else 1.0
                    else:
                        expected_similarity[i, j] = np.mean(1 - cdist(embeddings[first], embeddings[second], 'cosine'))
            for metric in metrics:
                matrix = np.zeros((group_count, group_count))
                for i, first in enumerate(labels):
                    for j, second in enumerate(labels):
                        matrix[i, j] = wasserstein_distance([p[metric] for p in prosody[first]],
                                                           [p[metric] for p in prosody[second]])
                expected_metrics.append(matrix)
            if group_count:
                self.assertEqual(2, len(matrices))
                np.testing.assert_allclose(expected_similarity, matrices[0], rtol=0, atol=1e-14)
                np.testing.assert_allclose(np.mean(expected_metrics, axis=0), matrices[1], rtol=0, atol=1e-14)
                csv = pd.read_csv(output / 'speaker_similarity_table.csv', index_col=0)
                self.assertEqual([name.replace('_', ' ') for name in labels], csv.columns.tolist())
                np.testing.assert_allclose(expected_similarity, csv.to_numpy(), rtol=0, atol=1e-14)
                self.assertTrue(np.array_equal(matrices[0], matrices[0].T))
                self.assertTrue(np.array_equal(matrices[1], matrices[1].T, equal_nan=True))
            else:
                self.assertEqual([], matrices)
                self.assertFalse((output / 'speaker_similarity_table.csv').exists())
            vector_calls = [call for call in ns['np'].array.call_args_list
                            if isinstance(call.args[0], list) and call.args[0]
                            and all(isinstance(item, float) for item in call.args[0])]
            return ns['cdist'].call_count, ns['wasserstein_distance'].call_count, len(vector_calls)

    def test_empty_single_and_unequal_sample_group_matrices_and_real_csv(self):
        for count in (0, 1, 2, 4):
            with self.subTest(groups=count):
                self.analyze(count)

    def test_each_unordered_group_pair_is_computed_once(self):
        for count in (1, 2, 4):
            with self.subTest(groups=count):
                cosine_calls, emd_calls, _ = self.analyze(count)
                pairs = count * (count - 1) // 2
                self.assertEqual(pairs, cosine_calls)
                self.assertEqual(3 * pairs, emd_calls)

    def test_each_group_metric_vector_is_constructed_once(self):
        for count in (1, 2, 4):
            with self.subTest(groups=count):
                _, _, vector_calls = self.analyze(count)
                self.assertEqual(3 * count, vector_calls)

    def test_nonfinite_prosody_retains_nan_instead_of_reporting_zero_divergence(self):
        for count in (1, 2):
            with self.subTest(groups=count):
                self.analyze(count, nonfinite=True)

    def test_empty_feature_distribution_still_refuses_analysis(self):
        with self.assertRaisesRegex(ValueError, 'empty'):
            self.analyze(1, empty_prosody=True)


class DedupDiagonalEvidenceTests(unittest.TestCase):
    setUp = dedup_fixture.VoiceDedupSourceCacheTests.setUp
    write_zip = dedup_fixture.VoiceDedupSourceCacheTests.write_zip

    def test_dedup_diagonal_is_measured_cohesion_and_survives_in_json_report(self):
        import soundfile as sf
        stream = io.BytesIO()
        sf.write(stream, np.full(160, -0.25), 16000, format='WAV', subtype='PCM_16')
        with zipfile.ZipFile(self.zip, 'a') as archive:
            archive.writestr('train/two.wav', stream.getvalue())
        (self.narrator / 'second.zip').write_bytes(self.zip.read_bytes())
        self.ns['extract_embedding'] = lambda wav, sr, model, device: np.array([wav[0], 1.0])
        self.ns.update(DEDUP_THRESHOLD=0.99, cluster_voices=cluster_voices,
            load_cluster_overrides=load_cluster_overrides,
            plt=SimpleNamespace(subplots=lambda **kwargs: (Mock(), Mock()), tight_layout=Mock(), savefig=Mock(), close=Mock()),
            sns=SimpleNamespace(heatmap=Mock()))
        with redirect_stdout(io.StringIO()):
            self.ns['run_dedup'](self.ns['fixture_model'], 'cpu', self.zips, self.output)
        report = json.loads((self.output / 'dedup_clusters.json').read_text())
        matrix = np.array(report['narrators']['narrator']['similarity_matrix'])
        vectors = np.array([[0.25, 1.0], [-0.25, 1.0]])
        expected = np.mean(1 - cdist(vectors, vectors, 'cosine'))
        self.assertLess(expected, 0.99)
        np.testing.assert_allclose(matrix, np.full((2, 2), expected), rtol=0, atol=1e-14)
        self.assertEqual(2, len(list((self.zips / '_deduped').glob('*.zip'))))
