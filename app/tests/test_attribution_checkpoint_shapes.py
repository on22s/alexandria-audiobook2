"""Scoring and comparison accept the same persisted checkpoint/result forms."""
import json
from pathlib import Path
import tempfile
import unittest
from build_scoring_sheet import load_named, find_model_runs
from compare_attribution_arms import load_attribution_entries
from generation_checkpoint_deltas import GenerationCheckpointDeltas


class AttributionCheckpointShapeTests(unittest.TestCase):
    def test_scoring_readers_replay_latest_indexed_attribution(self):
        row = {'speaker': 'ALICE', 'text': 'Alice waited.'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'model' / 'book' / 'result.json.threepass_checkpoint.json'
            path.parent.mkdir(parents=True)
            writer = GenerationCheckpointDeltas(path)
            writer.save_checkpoint({'named': [None]})
            writer.save_checkpoint({'named': [row]})
            original = path.read_bytes()
            self.assertEqual(load_attribution_entries(path), [row])
            self.assertEqual(load_named(path), [row])
            self.assertEqual(find_model_runs(tmp, 'book'), {'model': [row]})
            self.assertEqual(path.read_bytes(), original)

    def test_native_named_entries_and_result_lists_share_one_shape_contract(self):
        row = {'speaker': 'ALICE', 'text': 'Alice waited.'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'checkpoint.json'
            for shape in ({'named': [None, row]}, {'entries': [None, row]}, [None, row],
                          {'named': [], 'entries': [None, row]}, {'named': None, 'entries': [None, row]}):
                with self.subTest(shape=shape):
                    path.write_text(json.dumps(shape))
                    before = path.read_bytes()
                    self.assertEqual([None, row], load_attribution_entries(path))
                    self.assertEqual([row], load_named(path))
                    self.assertEqual(before, path.read_bytes())

    def test_invalid_selected_shapes_fail_in_both_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'checkpoint.json'
            for shape in (3, None, {'entries': 'bad'}, {'named': 0, 'entries': []},
                          {'entries': [False]}, {'named': [{'text': 'valid'}, ['bad']]}):
                for reader in (load_attribution_entries, load_named):
                    with self.subTest(shape=shape, reader=reader.__name__):
                        path.write_text(json.dumps(shape))
                        with self.assertRaises(ValueError):
                            reader(path)

    def test_native_matrix_entries_checkpoint_is_discovered_as_completed_run(self):
        row = {'speaker': 'ALICE', 'text': 'Alice waited.'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'model' / 'book' / 'result.json.threepass_checkpoint.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'entries': [None, row]}))
            self.assertEqual({'model': [row]}, find_model_runs(tmp, 'book'))
