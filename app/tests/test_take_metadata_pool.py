"""Real archive persistence, corruption refusal and reversible pool lifetime."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch

import numpy as np
import soundfile as sf

from audio_take_history import AudioTakeHistory, _encode


class TakeMetadataPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'selected.wav'
        sf.write(self.source, np.zeros(2400, dtype='float32'), 24000)
        self.history = AudioTakeHistory(self.root)
        self.text = 'A synthetic guide greeted the visitor beside the quiet garden. ' * 40
        self.rows = []
        for take in range(8):
            self.rows.append(self.history.capture(self.source, 'a' * 64, 'line',
                dict(text=self.text, generation_inputs=dict(text=self.text, speaker='NARRATOR'),
                     take=take, provenance={'literal': {'$payload': 'unchanged authored metadata'}})))
        self.raw = self.history.manifest.read_bytes()
        self.document = self.history._load()
        self.assertEqual(json.loads(self.raw)['version'], 3)
        self.audio = {p.name: p.read_bytes() for p in self.history.directory.glob('*.wav')}

    def assert_audio_unchanged(self):
        self.assertEqual({p.name: p.read_bytes() for p in self.history.directory.glob('*.wav')}, self.audio)
        self.assertTrue(all(hashlib.sha256(v).hexdigest() == self.rows[0]['sha256'] for v in self.audio.values()))

    def test_actual_pooled_archive_roundtrip_and_explicit_legacy_conversion(self):
        self.assertLess(len(self.raw), len(_encode(self.document)) * .7)
        self.assertEqual(_encode(self.history._load()), _encode(self.document))
        self.history.apply_metadata_format(pooled=False)
        self.assertEqual(self.history.manifest.read_bytes(), _encode(self.document))
        self.assertEqual(json.loads(self.history.manifest.read_bytes())['version'], 1)
        self.history.enable_automatic_format()
        self.assertEqual(self.history._load()['version'], 2)
        self.history.apply_metadata_format(pooled=False)
        self.assertEqual(json.loads(self.history.manifest.read_bytes())['version'], 2)
        self.history.apply_metadata_format(pooled=True)
        self.assertEqual(json.loads(self.history.manifest.read_bytes())['record_version'], 2)
        self.assert_audio_unchanged()

    def test_missing_tampered_conflicting_wrong_type_unknown_and_orphan_payload_refuse(self):
        base = json.loads(self.raw)
        def missing(d): d['payloads'].pop(next(iter(d['payloads'])))
        def tampered(d): d['payloads'][next(iter(d['payloads']))] = 'tampered'
        def conflict(d): d['records'][0]['record']['captured']['text'] = 'must not be overwritten'
        def wrong_type(d): d['records'][0]['references']['text'] = d['records'][0]['references']['generation_inputs']
        def unknown(d): d['records'][0]['references']['unknown'] = next(iter(d['payloads']))
        def orphan(d): d['payloads'][hashlib.sha256(_encode('orphan')).hexdigest()] = 'orphan'
        for mutate in (missing, tampered, conflict, wrong_type, unknown, orphan):
            with self.subTest(case=mutate.__name__):
                damaged = copy.deepcopy(base); mutate(damaged)
                encoded = _encode(damaged); self.history.manifest.write_bytes(encoded)
                with self.assertRaises(ValueError): self.history.list()
                with self.assertRaises(ValueError): self.history.apply_metadata_format(pooled=False)
                self.assertEqual(self.history.manifest.read_bytes(), encoded)
                self.assert_audio_unchanged()
        self.history.manifest.write_bytes(self.raw)

    def test_expanded_manifest_and_record_bounds_cannot_be_bypassed_by_references(self):
        self.assertLess(len(self.raw), len(_encode(self.document)))
        with patch('audio_take_history.MAX_MANIFEST_BYTES', len(self.raw)):
            with self.assertRaisesRegex(ValueError, 'Expanded'): self.history._load()
        with patch('audio_take_history.MAX_RECORD_BYTES', 1000):
            with self.assertRaisesRegex(ValueError, 'size|limits'): self.history._load()
        self.assertEqual(self.history.manifest.read_bytes(), self.raw)
        self.assert_audio_unchanged()

    def test_protection_delete_undo_and_final_purge_remove_only_unreferenced_values(self):
        row = self.rows[0]
        change = lambda action: self.history.change(row['version'], row['sha256'], action, lambda _: False)
        change('protect')
        with self.assertRaises(ValueError): change('delete')
        change('unprotect'); change('delete'); change('undo')
        self.assertEqual(self.history._load()['records'][0]['captured'], self.document['records'][0]['captured'])
        with self.history.open(row['version'], row['sha256'])[0] as stream:
            self.assertEqual(stream.read(), self.source.read_bytes())
        for row in self.rows:
            change('delete'); change('purge')
        self.assertEqual(self.history._load(), dict(version=1, records=[]))
        self.assertEqual(json.loads(self.history.manifest.read_bytes()), dict(version=1, records=[]))
        self.assertEqual(list(self.history.directory.glob('*.wav')), [])

    def test_failed_conversion_replace_preserves_authority_and_retries_pending(self):
        with patch('audio_take_history.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError): self.history.apply_metadata_format(pooled=False)
        self.assertEqual(self.history.manifest.read_bytes(), self.raw)
        self.assertTrue(self.history.pending.exists())
        self.assertEqual(self.history._load(), self.document)
        self.history.apply_metadata_format(pooled=False)
        self.assertFalse(self.history.pending.exists())
        self.assertEqual(self.history.manifest.read_bytes(), _encode(self.document))
        self.assert_audio_unchanged()

    def test_cli_expansion_and_compaction_preserve_actual_archive(self):
        for flag, expected_version in (('--expand-metadata', 1), ('--compact-metadata', 3)):
            result = subprocess.run([sys.executable, '-m', 'audio_take_history', str(self.root), flag],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(self.history.manifest.read_bytes())['version'], expected_version)
            self.assertEqual(self.history._load(), self.document)
            self.assert_audio_unchanged()

    def test_mutable_expanded_payloads_do_not_alias_other_records_or_input(self):
        first = self.history._load()
        first['records'][0]['captured']['generation_inputs']['text'] = 'Changed in memory.'
        self.assertEqual(first['records'][1]['captured']['generation_inputs']['text'], self.text)
        self.assertEqual(self.history._load(), self.document)
        before = _encode(self.document)
        self.history._save(self.document)
        self.assertEqual(_encode(self.document), before)

    def test_new_wrapper_does_not_make_valid_deep_legacy_metadata_unwritable(self):
        doc = copy.deepcopy(self.document)
        last_valid = None
        for depth in range(70):
            value = 'leaf'
            for _ in range(depth): value = {'nested': value}
            doc['records'][0]['captured']['extra'] = value
            try:
                self.history._validate(doc)
            except ValueError:
                break
            last_valid = copy.deepcopy(doc)
        self.assertIsNotNone(last_valid)
        self.history._save(last_valid)
        self.assertEqual(self.history._load(), last_valid)
        self.assertEqual(json.loads(self.history.manifest.read_bytes())['version'], 1)

    def test_actual_child_exit_before_and_after_conversion_replace_retries_in_both_directions(self):
        child = '''import os,sys
from audio_take_history import AudioTakeHistory
original=os.replace
def stop(*args,**kwargs):
 if sys.argv[3]=='after': original(*args,**kwargs)
 os._exit(79)
os.replace=stop
AudioTakeHistory(sys.argv[1]).apply_metadata_format(pooled=sys.argv[2]=='compact')
raise AssertionError('conversion did not reach replacement')
'''
        for pooled in (False, True):
            for phase in ('before', 'after'):
                with self.subTest(pooled=pooled, phase=phase):
                    self.history.apply_metadata_format(pooled=not pooled)
                    before = self.history.manifest.read_bytes()
                    process = subprocess.run([sys.executable, '-c', child, str(self.root),
                        'compact' if pooled else 'expand', phase], capture_output=True, text=True, timeout=20)
                    self.assertEqual(process.returncode, 79, process.stderr)
                    self.assertEqual(self.history._load(), self.document)
                    if phase == 'before':
                        self.assertEqual(self.history.manifest.read_bytes(), before)
                        self.assertTrue(self.history.pending.exists())
                    else:
                        self.assertNotEqual(self.history.manifest.read_bytes(), before)
                        self.assertFalse(self.history.pending.exists())
                    self.history.apply_metadata_format(pooled=pooled)
                    self.assertFalse(self.history.pending.exists())
                    self.assertEqual(self.history._load(), self.document)
                    self.assert_audio_unchanged()

    def test_compaction_encodes_each_eligible_field_at_most_once_per_save_attempt(self):
        from take_metadata_pool import compact_metadata
        calls = []
        fields = {id(value) for row in self.document['records'] for value in row['captured'].values()}
        def encode(value):
            if id(value) in fields:
                calls.append(type(value))
            return _encode(value)
        candidate = compact_metadata(self.document, encode=encode)
        self.assertIsNotNone(candidate)
        self.assertLessEqual(len(calls), 4 * len(self.document['records']))
        self.history.manifest.write_bytes(candidate.encoded)
        self.assertEqual(self.history._load(), self.document)
        self.assert_audio_unchanged()

    def test_savings_shortcut_never_discards_a_candidate_meeting_the_whole_file_threshold(self):
        from take_metadata_pool import compact_metadata
        candidate = compact_metadata(self.document, encode=_encode)
        saving = len(_encode(self.document)) - len(candidate.encoded)
        self.assertGreater(saving, 4096)
        for threshold in (0, saving, saving + 1, len(_encode(self.document))):
            with self.subTest(threshold=threshold):
                result = compact_metadata(self.document, encode=_encode, minimum_saving=threshold)
                if result is None:
                    self.assertLess(saving, threshold)
                else:
                    self.assertEqual(result.encoded, candidate.encoded)

    def test_unchanged_record_bytes_reuse_requires_fresh_complete_semantic_fingerprints(self):
        self.history._save(self.document)
        cached = dict(self.history._compact_records)
        self.assertEqual(len(cached), 8)
        with patch('take_metadata_pool._copy_parent', side_effect=AssertionError('unchanged record rebuilt')):
            self.history._save(self.document)
        for key, record in cached.items():
            self.assertIs(self.history._compact_records[key].encoded, record.encoded)
        # UID, archive version and audio hash stay the same. A changed nested
        # input must invalidate its cache entry and persist its actual value.
        changed = copy.deepcopy(self.document)
        changed['records'][0]['captured']['generation_inputs']['text'] = 'New synthetic authored input.'
        self.history._save(changed)
        self.assertEqual(self.history._load(), changed)
        keys = set(cached) & set(self.history._compact_records)
        self.assertEqual(len(keys), 7)
        self.assertEqual(sum(self.history._compact_records[key].encoded is cached[key].encoded for key in keys), 7)
        self.assert_audio_unchanged()

    def test_cached_records_cannot_bypass_validation_or_survive_failed_conversion_as_current(self):
        self.history._save(self.document)
        cached = self.history._compact_records
        before = self.history.manifest.read_bytes()
        with patch('audio_take_history.MAX_RECORD_BYTES', 1000):
            with self.assertRaises(ValueError): self.history._save(self.document)
        self.assertEqual(self.history.manifest.read_bytes(), before)
        changed = copy.deepcopy(self.document)
        changed['records'][0]['protected'] = True
        with patch('audio_take_history.os.replace', side_effect=OSError('failed replacement')):
            with self.assertRaises(OSError): self.history._save(changed)
        self.assertIs(self.history._compact_records, cached)
        self.assertEqual(self.history._load(), self.document)
        self.history.apply_metadata_format(pooled=False)
        self.assertEqual(self.history._compact_records, {})
        self.assert_audio_unchanged()

    def test_duplicate_recapture_leaves_pool_and_manifest_identical(self):
        row = self.rows[3]
        captured = copy.deepcopy(self.document['records'][3]['captured'])
        before = self.history.manifest.read_bytes()
        repeated = self.history.capture(self.source, 'a' * 64, 'line', captured)
        self.assertEqual(repeated, row)
        self.assertEqual(self.history.manifest.read_bytes(), before)
        self.assert_audio_unchanged()

    def test_partial_purge_preserves_deleted_and_protected_last_payload_references(self):
        protected, deleted = self.rows[:2]
        self.history.change(protected['version'], protected['sha256'], 'protect', lambda _: False)
        self.history.change(deleted['version'], deleted['sha256'], 'delete', lambda _: False)
        for row in self.rows[2:]:
            self.history.change(row['version'], row['sha256'], 'delete', lambda _: False)
            self.history.change(row['version'], row['sha256'], 'purge', lambda _: False)
        document = self.history._load()
        self.assertEqual(len(document['records']), 2)
        for row in document['records']:
            self.assertEqual(row['captured']['text'], self.text)
            self.assertEqual(row['captured']['generation_inputs']['text'], self.text)
        self.history.change(deleted['version'], deleted['sha256'], 'purge', lambda _: False)
        self.assertEqual(self.history._load()['records'][0]['captured'], self.document['records'][0]['captured'])
        with self.history.open(protected['version'], protected['sha256'])[0] as stream:
            self.assertEqual(stream.read(), self.source.read_bytes())

    def test_unchanged_row_gets_new_compact_bytes_when_only_its_reference_choices_change(self):
        document = copy.deepcopy(self.document)
        provenance = {'source': 'Synthetic shared provenance. ' * 80}
        for row in document['records'][:2]: row['captured']['provenance'] = provenance
        self.history._save(document)
        first = document['records'][0]
        fingerprint = hashlib.sha256(_encode(first)).digest()
        before = self.history._compact_records[fingerprint]
        self.assertIn('provenance', dict(before.references))
        removed = self.rows[1]
        self.history.change(removed['version'], removed['sha256'], 'delete', lambda _: False)
        self.history.change(removed['version'], removed['sha256'], 'purge', lambda _: False)
        # Other repeated fields still justify v3. This row's complete semantic
        # identity is unchanged, but its provenance is now inline, not pooled.
        self.assertEqual(json.loads(self.history.manifest.read_bytes())['version'], 3)
        after = self.history._compact_records[fingerprint]
        self.assertNotIn('provenance', dict(after.references))
        self.assertIsNot(after.encoded, before.encoded)
        self.assertEqual(self.history._load()['records'][0], first)
        self.assertEqual(self.source.read_bytes(), next(iter(self.audio.values())))
        remaining = {p.name: p.read_bytes() for p in self.history.directory.glob('*.wav')}
        expected = {k: v for k, v in self.audio.items() if k != removed['version'] + '.wav'}
        self.assertEqual(remaining, expected)

    def test_late_aggregate_guards_exhaust_validation_before_file_or_cache_commit(self):
        self.history._save(self.document)
        cached = self.history._compact_records
        before = self.history.manifest.read_bytes()
        too_many = copy.deepcopy(self.document)
        for index in range(8, 17):
            row = copy.deepcopy(too_many['records'][0]); row['version'] = f'{index:032x}'
            too_many['records'].append(row)
        with self.assertRaisesRegex(ValueError, 'storage limits'): self.history._save(too_many)
        with self.assertRaisesRegex(ValueError, 'storage limits'): self.history._validate(too_many)
        with patch('audio_take_history.MAX_PAYLOAD_BYTES', sum(row['bytes'] for row in self.document['records']) - 1):
            with self.assertRaisesRegex(ValueError, 'storage limits'): self.history._save(self.document)
            with self.assertRaisesRegex(ValueError, 'storage limits'): self.history._validate(self.document)
        self.assertIs(self.history._compact_records, cached)
        self.assertEqual(self.history.manifest.read_bytes(), before)
        self.assert_audio_unchanged()
