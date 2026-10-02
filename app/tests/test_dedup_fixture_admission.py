"""Incomplete benchmark fixtures cannot reach dedup; native CPU worker proof."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import dedup_benchmark as worker
import benchmark_runner as runner
import benchmark_fixtures as fixtures


class DedupFixtureAdmissionTests(unittest.TestCase):
    def fixture(self, root):
        dataset = root / 'dataset'
        dataset.mkdir()
        rows = [{'audio_filepath': 'one.wav', 'text': 'One'}, {'audio': 'two.wav', 'text': 'Two'}]
        metadata = ''.join(json.dumps(row) + '\n' for row in rows).encode()
        (dataset / 'metadata.jsonl').write_bytes(metadata)
        hashes = {}
        for name, data in (('one.wav', b'fixture one'), ('two.wav', b'fixture two')):
            (dataset / name).write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        fixture = {'root_dir': str(root), 'dataset_path': 'dataset', 'metadata_sha256': hashlib.sha256(metadata).hexdigest(),
                   'audio_sha256': hashes, 'samples_per_volume': 1, 'seed': 42, 'model_id': 'fixture'}
        script = root / 'analysis.py'
        script.write_text('''import argparse,json,pathlib,zipfile,shutil
p=argparse.ArgumentParser();p.add_argument('--phase');p.add_argument('--device');p.add_argument('--zips2');p.add_argument('--dedup-out');p.add_argument('--seed');a=p.parse_args()
zips=sorted((pathlib.Path(a.zips2)/'benchmark_narrator').glob('*.zip'))
assert len(zips)==2
proof=[]
for path in zips:
 with zipfile.ZipFile(path) as z:
  rows=[json.loads(line) for line in z.read('metadata.jsonl').decode().splitlines() if line]
  proof.append({'name':path.name,'rows':rows,'audio':[z.read(row.get('audio_filepath') or row.get('audio')).decode() for row in rows]})
(pathlib.Path(__file__).parent/'child_proof.json').write_text(json.dumps(proof))
out=pathlib.Path(a.dedup_out);out.mkdir();(out/'dedup_clusters.json').write_text(json.dumps({'narrators':{'benchmark_narrator':{'similarity_matrix':[[1,.8],[.8,1]],'clusters':[[0,1]]}}}))
d=pathlib.Path(a.zips2)/'_deduped';d.mkdir();shutil.copyfile(zips[0],d/'first.zip')
''')
        return fixture, script

    def test_invalid_counts_and_insufficient_volume_fail_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture, script = self.fixture(root)
            for count in (0, -1, True, False, '1', 1.0, 2):
                with self.subTest(count=count), patch.object(worker.subprocess, 'run') as run:
                    changed = {**fixture, 'samples_per_volume': count}
                    with self.assertRaises(ValueError):
                        worker.execute_fixture(changed, sys.executable, str(script))
                    run.assert_not_called()
            self.assertFalse((root / 'child_proof.json').exists())

    def test_missing_selected_hash_is_rejected_by_worker_and_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture, script = self.fixture(root)
            fixture['audio_sha256'].pop('two.wav')
            selected = {key: fixture[key] for key in ('dataset_path', 'metadata_sha256', 'samples_per_volume', 'audio_sha256', 'model_id', 'seed')}
            fixture['sha256'] = runner._hash_entries(selected)
            for execute in (lambda: worker.execute_fixture(fixture, sys.executable, str(script)),
                            lambda: runner._validate_dedup_fixture(fixture, tmp)):
                with self.assertRaisesRegex(ValueError, 'selected audio hash is missing'):
                    execute()
            self.assertFalse((root / 'child_proof.json').exists())

    def test_native_cpu_child_receives_exact_two_full_hash_verified_volumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture, script = self.fixture(root)
            original = copy.deepcopy(fixture)
            result = worker.execute_fixture(fixture, sys.executable, str(script))
            proof = json.loads((root / 'child_proof.json').read_text())
            self.assertEqual(['volume_01.zip', 'volume_02.zip'], [item['name'] for item in proof])
            self.assertEqual([1, 1], [len(item['rows']) for item in proof])
            self.assertEqual([['fixture one'], ['fixture two']], [item['audio'] for item in proof])
            self.assertEqual(.8, result['similarity'])
            self.assertEqual(1, result['output_zips'][0]['sample_count'])
            self.assertEqual(original, fixture)

    def test_manifest_builder_uses_the_same_strict_sample_count_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            for count in (True, 0, -1, 2):
                with self.subTest(count=count), self.assertRaises(ValueError):
                    fixtures.build_voicelab_dedup_manifest([{'dataset_path': 'dataset', 'samples_per_volume': count}], tmp)
