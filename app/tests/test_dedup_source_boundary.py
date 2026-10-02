import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
import dedup_benchmark as worker


class DedupSourceBoundaryTests(unittest.TestCase):
    def fixture(self, root):
        dataset = root / 'dataset'; dataset.mkdir()
        (dataset / 'train').mkdir()
        for name in ('one.wav', 'two.wav'):
            (dataset / 'train' / name).write_bytes(b'known fixture audio ' + name.encode())
        entries = [{'audio_filepath': 'train/one.wav', 'text': 'One'},
                   {'audio': 'train/two.wav', 'text': 'Two'}]
        fixture = {'root_dir': str(root), 'dataset_path': 'dataset', 'samples_per_volume': 1, 'seed': 42}
        self.metadata(dataset, entries, fixture)
        return dataset, entries, fixture

    def metadata(self, dataset, entries, fixture):
        metadata = dataset / 'metadata.jsonl'
        metadata.write_text(''.join(json.dumps(row) + '\n' for row in entries))
        fixture['metadata_sha256'] = hashlib.sha256(metadata.read_bytes()).hexdigest()
        fixture['audio_sha256'] = {row.get('audio_filepath') or row.get('audio'):
            hashlib.sha256((dataset / (row.get('audio_filepath') or row.get('audio'))).read_bytes()).hexdigest()
            for row in entries}

    def analysis(self, command, **kwargs):
        zips = Path(command[command.index('--zips2') + 1]); output = Path(command[command.index('--dedup-out') + 1])
        output.mkdir(); deduped = zips / '_deduped'; deduped.mkdir()
        self.observed = []
        for path in sorted((zips / 'benchmark_narrator').glob('*.zip')):
            with zipfile.ZipFile(path) as archive:
                self.observed.append({name: archive.read(name) for name in archive.namelist()})
            shutil.copy2(path, deduped / path.name)
        (output / 'dedup_clusters.json').write_text(json.dumps({'narrators': {'benchmark_narrator':
            {'similarity_matrix': [[1, .8], [.8, 1]], 'clusters': [[0, 1]]}}}))
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def test_outside_datasets_metadata_and_audio_refuse_before_dispatch(self):
        for mode in ('dataset-absolute', 'dataset-parent', 'metadata-link', 'audio-link', 'audio-absolute', 'audio-parent'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); dataset, entries, fixture = self.fixture(root)
                original = (dataset / 'train/one.wav').read_bytes()
                if mode.startswith('dataset'):
                    empty = root / 'empty'; empty.mkdir(); fixture['root_dir'] = str(empty)
                    fixture['dataset_path'] = str(dataset) if mode.endswith('absolute') else '../dataset'
                elif mode == 'metadata-link':
                    outside = root / 'outside.jsonl'; outside.write_bytes((dataset / 'metadata.jsonl').read_bytes())
                    (dataset / 'metadata.jsonl').unlink(); (dataset / 'metadata.jsonl').symlink_to(outside)
                else:
                    outside = root / 'outside.wav'; outside.write_bytes(original)
                    (dataset / 'link.wav').symlink_to(outside)
                    entries[0]['audio_filepath'] = {'audio-link': 'link.wav', 'audio-absolute': str(outside),
                                                    'audio-parent': '../outside.wav'}[mode]
                    self.metadata(dataset, entries, fixture)
                before = copy.deepcopy(fixture)
                with patch.object(worker.subprocess, 'run', side_effect=self.analysis) as dispatch:
                    with self.assertRaises(ValueError): worker.execute_fixture(fixture, 'fixture-python', 'fixture-analysis')
                    dispatch.assert_not_called()
                self.assertEqual(before, fixture)
                self.assertEqual(original, (dataset / 'train/one.wav').read_bytes())

    def test_contained_but_unsafe_zip_member_names_are_rejected(self):
        for name in ('../dataset/train/one.wav', './train/one.wav', 'train//one.wav', 'C:one.wav', 'bad\\one.wav'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); dataset, entries, fixture = self.fixture(root)
                if name in ('C:one.wav', 'bad\\one.wav'):
                    (dataset / name).write_bytes((dataset / 'train/one.wav').read_bytes())
                entries[0]['audio_filepath'] = name; self.metadata(dataset, entries, fixture)
                with patch.object(worker.subprocess, 'run', side_effect=self.analysis) as dispatch:
                    with self.assertRaisesRegex(ValueError, 'unsafe dedup audio member'):
                        worker.execute_fixture(fixture, 'fixture-python', 'fixture-analysis')
                    dispatch.assert_not_called()

    def test_safe_nested_and_internal_symlink_audio_keeps_exact_archive_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset, entries, fixture = self.fixture(Path(tmp))
            (dataset / 'inside.wav').symlink_to(dataset / 'train/one.wav')
            entries[0]['audio_filepath'] = 'inside.wav'; self.metadata(dataset, entries, fixture)
            before = copy.deepcopy(fixture)
            with patch.object(worker.subprocess, 'run', side_effect=self.analysis):
                result = worker.execute_fixture(fixture, 'fixture-python', 'fixture-analysis')
            self.assertEqual(2, result['output_zip_count']); self.assertEqual(.8, result['similarity'])
            for archive, entry in zip(self.observed, entries):
                name = entry.get('audio_filepath') or entry.get('audio')
                self.assertEqual({name, 'metadata.jsonl'}, set(archive))
                self.assertEqual(entry, json.loads(archive['metadata.jsonl']))
                self.assertEqual((dataset / name).read_bytes(), archive[name])
            self.assertEqual(before, fixture)
