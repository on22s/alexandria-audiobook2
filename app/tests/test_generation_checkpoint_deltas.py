import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from generation_checkpoint_deltas import (
    GenerationCheckpointDeltas, load_generation_delta_checkpoint, _get_digest)
from utils import atomic_json_write


class CheckpointDeltaTests(unittest.TestCase):
    def test_indexed_edits_rewind_and_detached_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'checkpoint.json'
            state = {'stage': 'segment', 'segmented': [{'text': 'a'}, {'text': 'b'}],
                     'named': [None, None], 'obsolete': True}
            writer = GenerationCheckpointDeltas(path)
            writer.save_checkpoint(state)
            header = path.read_bytes()
            state['named'][1] = {'speaker': 'B', 'text': 'b'}
            state['segmented'].append({'text': 'c'})
            writer.save_checkpoint(state)
            state['segmented'] = [{'text': 'corrected'}]
            state['named'] = [None]
            state['stage'] = 'segment_failed'
            del state['obsolete']
            writer.save_checkpoint(state)
            expected = copy.deepcopy(state)
            state['segmented'][0]['text'] = 'unsaved'
            self.assertEqual(load_generation_delta_checkpoint(path), expected)
            self.assertEqual(path.read_bytes(), header)
            loaded = load_generation_delta_checkpoint(path)
            loaded['named'].append('unsaved')
            self.assertEqual(load_generation_delta_checkpoint(path), expected)

    def test_native_process_exit_recovers_last_committed_change(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'checkpoint.json'
            script = ('import os; from generation_checkpoint_deltas import GenerationCheckpointDeltas; '
                      'w=GenerationCheckpointDeltas(%r); '
                      'w.save_checkpoint({"named":[None,None],"stage":"attribute"}); '
                      'w.save_checkpoint({"named":[None,{"speaker":"A"}],"stage":"attribute"}); '
                      'os._exit(93)') % str(path)
            result = subprocess.run([sys.executable, '-c', script], env=os.environ.copy())
            self.assertEqual(result.returncode, 93)
            self.assertEqual(load_generation_delta_checkpoint(path), {
                'named': [None, {'speaker': 'A'}], 'stage': 'attribute'})

    def test_failed_append_and_failed_legacy_migration_preserve_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'checkpoint.json'
            state = {'named': [None]}
            path.write_text(json.dumps(state))
            original = path.read_bytes()
            writer = GenerationCheckpointDeltas(path)
            with patch('generation_checkpoint_deltas.atomic_json_write', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    writer.save_checkpoint(state)
            self.assertEqual(path.read_bytes(), original)
            writer.save_checkpoint(state)
            with patch('generation_checkpoint_deltas.atomic_json_write', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    writer.save_checkpoint({'named': [{'speaker': 'A'}]})
            self.assertEqual(load_generation_delta_checkpoint(path), state)
            writer.save_checkpoint({'named': [{'speaker': 'A'}]})
            writer.save_checkpoint({'named': [{'speaker': 'A'}]}, compact=True)
            self.assertEqual(json.loads(path.read_text()), {'named': [{'speaker': 'A'}]})
            self.assertEqual(load_generation_delta_checkpoint(path), json.loads(path.read_text()))

    def test_corruption_gap_and_unsafe_directory_fail_without_editing_evidence(self):
        for mutation in ('digest', 'gap', 'directory'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'checkpoint.json'
                writer = GenerationCheckpointDeltas(path)
                writer.save_checkpoint({'named': [None]})
                writer.save_checkpoint({'named': [{'speaker': 'A'}]})
                header = json.loads(path.read_text())
                shard = path.parent / header['directory'] / '00000000.json'
                if mutation == 'digest':
                    record = json.loads(shard.read_text())
                    record['changes']['fields']['named']['entries']['0']['speaker'] = 'B'
                    shard.write_text(json.dumps(record))
                elif mutation == 'gap':
                    shard.rename(shard.with_name('00000001.json'))
                else:
                    header['directory'] = '../outside'
                    path.write_text(json.dumps(header))
                before = {str(p): p.read_bytes() for p in Path(root).rglob('*') if p.is_file()}
                with self.assertRaises(ValueError):
                    load_generation_delta_checkpoint(path)
                with self.assertRaises(ValueError):
                    writer.save_checkpoint({'named': [{'speaker': 'C'}]})
                self.assertEqual(before, {str(p): p.read_bytes() for p in Path(root).rglob('*') if p.is_file()})

    def test_growing_list_serializes_only_new_entries(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'checkpoint.json'
            writer = GenerationCheckpointDeltas(path)
            state = {'segmented': [], 'named': [], 'stage': 'segment'}
            writer.save_checkpoint(state)
            header = path.read_bytes()
            for index in range(64):
                state['segmented'].append({'text': str(index) + 'x' * 256})
                writer.save_checkpoint(state)
            shards = list(writer.directory.glob('*.json'))
            self.assertEqual(len(shards), 64)
            self.assertLess(sum(p.stat().st_size for p in shards), 64 * 900)
            self.assertEqual(path.read_bytes(), header)
            self.assertEqual(load_generation_delta_checkpoint(path), state)
            for index, shard in enumerate(sorted(shards)):
                entries = json.loads(shard.read_text())['changes']['fields']['segmented']['entries']
                self.assertEqual(set(entries), {str(index)})

    def test_publication_then_error_reloads_committed_state_before_retry(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'checkpoint.json'
            writer = GenerationCheckpointDeltas(path)
            writer.save_checkpoint({'named': [None]})
            def publish_then_fail(value, target):
                atomic_json_write(value, target)
                raise OSError('directory sync failed after publication')
            with patch('generation_checkpoint_deltas.atomic_json_write', side_effect=publish_then_fail):
                with self.assertRaises(OSError):
                    writer.save_checkpoint({'named': [{'speaker': 'A'}]})
            self.assertEqual(load_generation_delta_checkpoint(path), {'named': [{'speaker': 'A'}]})
            writer.save_checkpoint({'named': [{'speaker': 'B'}]})
            self.assertEqual(load_generation_delta_checkpoint(path), {'named': [{'speaker': 'B'}]})
            self.assertEqual(len(list(writer.directory.glob('*.json'))), 2)
            # Manual recovery can replace the base with a complete legacy snapshot.
            atomic_json_write({'named': [{'speaker': 'manual'}]}, str(path))
            writer.save_checkpoint({'named': [{'speaker': 'manual'}, None]})
            self.assertEqual(load_generation_delta_checkpoint(path), {
                'named': [{'speaker': 'manual'}, None]})

    def test_valid_digest_does_not_admit_invalid_indexed_changes(self):
        for change in ({'length': 2, 'entries': {}},
                       {'length': 1, 'entries': {'01': None}},
                       {'length': True, 'entries': {}},
                       {'length': 1, 'entries': {'-1': None}}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'checkpoint.json'
                writer = GenerationCheckpointDeltas(path)
                writer.save_checkpoint({'named': [None]})
                record = {'previous': writer.digest,
                          'changes': {'fields': {'named': change}, 'removed': []}}
                shard = writer.directory / '00000000.json'
                atomic_json_write(dict(record, sha256=_get_digest(record)), str(shard))
                original = shard.read_bytes()
                with self.assertRaises(ValueError):
                    load_generation_delta_checkpoint(path)
                self.assertEqual(shard.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
