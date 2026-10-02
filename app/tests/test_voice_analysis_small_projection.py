"""Known small-graph failures and finite geometry; no model inference."""
import ast
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from tests.test_voice_analysis_cache import load_analysis_functions


class SmallProjectionTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_analysis_functions()
        if os.environ.get('PROJECTION_SOURCE'):
            source = Path(os.environ['PROJECTION_SOURCE'])
            tree = ast.parse(source.read_text())
            # Execute the old actual projection expression under the same input.
            run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'run_analyze')
            assignment = next(node for node in ast.walk(run) if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'umap_coords' for target in node.targets))
            expression = compile(ast.Expression(assignment.value), str(source), 'eval')
            def old_projection(embeddings):
                import umap
                return eval(expression, {'umap_lib': umap, 'combined': embeddings}), 'UMAP'
            self.ns['get_embedding_projection'] = old_projection

    def test_one_and_two_rows_need_no_umap_and_preserve_pair_distance(self):
        for count in (1, 2):
            with self.subTest(count=count):
                values = np.array([[1., 2., 3.], [4., -1., 2.]])[:count]
                original = values.copy()
                constructor = Mock(side_effect=AssertionError('tiny graph must not run'))
                with patch.dict('sys.modules', {'umap': SimpleNamespace(UMAP=constructor)}):
                    coordinates, name = self.ns['get_embedding_projection'](values)
                self.assertEqual((count, 2), coordinates.shape)
                self.assertTrue(np.isfinite(coordinates).all())
                np.testing.assert_array_equal(original, values)
                if count == 1:
                    self.assertEqual('Single sample', name)
                    np.testing.assert_array_equal([[0., 0.]], coordinates)
                else:
                    self.assertEqual('PCA', name)
                    np.testing.assert_array_equal([0., 0.], coordinates[:, 1])
                    self.assertAlmostEqual(np.linalg.norm(values[0]-values[1]),
                                           np.linalg.norm(coordinates[0]-coordinates[1]))

    def test_three_rows_avoid_spectral_failure_and_larger_rows_keep_default_init(self):
        for count in (3, 4, 8, 15, 16, 30):
            with self.subTest(count=count):
                values = np.random.RandomState(73).normal(size=(count, 4))
                def construct(**kwargs):
                    self.assertEqual(min(15, count-1), kwargs['n_neighbors'])
                    self.assertEqual(.1, kwargs['min_dist'])
                    self.assertEqual(42, kwargs['random_state'])
                    if count == 3:
                        self.assertEqual('random', kwargs.get('init'))
                    else:
                        self.assertNotIn('init', kwargs)
                    return SimpleNamespace(fit_transform=lambda array: np.zeros((len(array), 2)))
                with patch.dict('sys.modules', {'umap': SimpleNamespace(UMAP=construct)}):
                    coordinates, name = self.ns['get_embedding_projection'](values)
                self.assertEqual('UMAP', name)
                self.assertEqual((count, 2), coordinates.shape)
