import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from book_state_transaction import get_book_operation_token
from chunk_outlier_check import (OUTLIER_METRICS, OUTLIER_LABELS, OutlierCheckError, get_outlier_fact,
    get_outlier_population_key, get_outlier_snapshot)
from editor_review_facts import get_editor_review_facts
from editor_poll_snapshot import get_snapshot_digest
from project import ProjectManager


class OutlierEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name, value in [('annotated_script.json', []), ('voice_config.json', {}),
                            ('state.json', {'active_book_id': 'one'})]:
            (self.root / name).write_text(json.dumps(value), encoding='utf-8')
        self.manager = ProjectManager(str(self.root))
        self.chunks = [dict(id=index, uid=str(index), text='synthetic', speaker='A',
                            status='done', audio_path=f'{index}.wav', audio_revision='one')
                       for index in range(2)]
        self.manager.save_chunks(self.chunks)
        for chunk in self.chunks:
            (self.root / chunk['audio_path']).write_bytes(b'synthetic metadata fixture')
        self.snapshot = get_outlier_snapshot(self.manager, get_book_operation_token(self.root))
        self.results = [dict(index=index, uid=str(index), metrics={metric:
            dict(value=1., median=1., spread=1., z=0., flagged=False)
            for metric in OUTLIER_METRICS}, error=None, flagged=False) for index in range(2)]

    def test_batch_writes_once_and_does_not_invalidate_itself(self):
        with patch.object(self.manager, '_save_chunks_locked', wraps=self.manager._save_chunks_locked) as save:
            self.assertEqual(2, self.manager.apply_outlier_results(self.snapshot, self.results, lambda: None))
            self.assertEqual(1, save.call_count)
        rows = self.manager.load_chunks()
        self.assertEqual(self.snapshot['population_key'], get_outlier_population_key(rows))
        self.assertEqual('measured', get_outlier_fact(rows[0], self.snapshot['population_key'],
            get_snapshot_digest(self.snapshot['book']['voices']))['status'])
        self.assertEqual('unavailable', get_outlier_fact(rows[0], self.snapshot['population_key'],
            get_snapshot_digest({'A': {'type': 'custom'}}))['status'])

    def test_actual_cancel_callback_prevents_any_evidence_write(self):
        def cancel():
            raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.manager.apply_outlier_results(self.snapshot, self.results, cancel)
        self.assertTrue(all('outlier' not in row for row in self.manager.load_chunks()))

    def test_population_mutations_reject_whole_batch(self):
        for mutation in ('text', 'audio_path', 'audio_revision', 'voice_revision', 'add', 'delete', 'order'):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(self.chunks)
                if mutation == 'add':
                    changed.append(dict(changed[0], uid='new', id=2))
                elif mutation == 'delete':
                    changed.pop()
                elif mutation == 'order':
                    changed.reverse()
                else:
                    changed[1][mutation] = 'changed'
                self.manager.save_chunks(changed)
                with self.assertRaisesRegex(OutlierCheckError, 'Saved takes changed; refresh Editor'):
                    self.manager.apply_outlier_results(self.snapshot, self.results, lambda: None)
                self.assertTrue(all(not row.get('outlier') for row in self.manager.load_chunks()))

    def test_source_switch_and_voice_config_reject_batch(self):
        for name, value in [('annotated_script.json', [{'text': 'changed'}]),
                            ('state.json', {'active_book_id': 'two'}),
                            ('voice_config.json', {'A': {'type': 'custom'}})]:
            with self.subTest(name=name):
                path = self.root / name
                before = path.read_bytes()
                path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaisesRegex(OutlierCheckError, 'Book inputs changed; refresh Editor'):
                    self.manager.apply_outlier_results(self.snapshot, self.results, lambda: None)
                path.write_bytes(before)
        with self.assertRaisesRegex(ValueError, 'active book changed'):
            get_outlier_snapshot(self.manager, '0' * 64)

    def test_partial_positive_and_missing_never_clean(self):
        chunk = copy.deepcopy(self.chunks[0])
        chunk['outlier'] = dict(self.results[0], population_key=self.snapshot['population_key'])
        chunk['outlier']['metrics'].pop('median_pitch_hz')
        fact = get_outlier_fact(chunk, self.snapshot['population_key'])
        self.assertEqual('unavailable', fact['status'])
        self.assertEqual(['median_pitch_hz'], fact['missing'])
        chunk['outlier']['metrics']['loudness_db']['flagged'] = True
        fact = get_outlier_fact(chunk, self.snapshot['population_key'])
        self.assertEqual('flagged', fact['status'])
        self.assertNotIn('labels', fact)
        self.assertIn('proxy', OUTLIER_LABELS['syllable_rate'])
        with patch('editor_review_facts.get_outlier_population_key', return_value=self.snapshot['population_key']) as key:
            rows = get_editor_review_facts([chunk, self.chunks[1]], lambda path: str(self.root / path))
        self.assertEqual(1, key.call_count)
        self.assertIn('outliers', rows[0]['issues'])
        self.assertEqual('unmeasured', rows[1]['outlier']['status'])

    def test_sibling_mutation_invalidates_existing_facts(self):
        self.manager.apply_outlier_results(self.snapshot, self.results, lambda: None)
        rows = self.manager.load_chunks()
        rows[1]['text'] = 'changed'
        facts = get_editor_review_facts(rows, lambda path: path)
        self.assertEqual(['unavailable', 'unavailable'], [row['outlier']['status'] for row in facts])

    def test_missing_script_and_corrupt_voices_have_safe_typed_refusals(self):
        token = get_book_operation_token(self.root)
        script = self.root / 'annotated_script.json'
        script.unlink()
        with self.assertRaisesRegex(OutlierCheckError, 'annotated script'):
            get_outlier_snapshot(self.manager, token)
        script.write_text('[]', encoding='utf-8')
        for raw in ('{broken', '[]', '{"A": NaN}', '{"A": {}, "A": {}}'):
            with self.subTest(raw=raw):
                (self.root / 'voice_config.json').write_text(raw, encoding='utf-8')
                with self.assertRaisesRegex(OutlierCheckError, 'voice_config.json'):
                    get_outlier_snapshot(self.manager, get_book_operation_token(self.root))

    def test_population_uses_canonical_generation_projection(self):
        with patch('project.get_chunk_generation_inputs', return_value={'pronunciation_rules': ['changed']}) as projection:
            changed = get_outlier_population_key(self.chunks)
        self.assertEqual(len(self.chunks), projection.call_count)
        self.assertNotEqual(self.snapshot['population_key'], changed)
