"""Compound scope apply/undo retains exact saved metadata and refuses stale plans."""
import json
import copy
import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from book_state_transaction import ensure_book_state
from project import group_into_chunks
import project_scope_exports
from project_scope_apply import (apply_source_scope, apply_source_scope_undo,
                                 get_source_scope_undo_preview,
                                 get_source_scope_undo_status, UNDO)
from project_scope_plan import get_scope_preview
from project_scope_admission import ensure_scope_admission
from script_selection_binding import BINDING_NAME


class ScopeApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        script = [{'speaker':speaker, 'text':text, 'instruct':''} for speaker,text in
                  [('ALICE','First synthetic line.'), ('BOB','Retained exact line.')]]
        source = self.root/'source.txt'; source.write_text('\n'.join(r['text'] for r in script))
        rows = [dict(row, id=i, uid=f'uid-{i}', status='pending', audio_path=None)
                for i,row in enumerate(group_into_chunks(script))]
        state = {'active_book_id':'fixture', 'book_generation':'unchanged', 'input_file_path':str(source)}
        for name,value in [('state.json',state), ('annotated_script.json',script),
                           ('voice_config.json',{}), ('chunks.json',rows)]:
            (self.root/name).write_text(json.dumps(value, indent=3))

    def files(self):
        return {p.relative_to(self.root).as_posix():p.read_bytes() for p in sorted(self.root.rglob('*')) if p.is_file() and not p.name.endswith('.lock') and '.task_ownership' not in p.parts}

    @contextmanager
    def admission(self):
        with patch('core.DATA_DIR', str(self.root)), patch('core.process_state', {}), ensure_scope_admission(self.root):
            yield

    def test_compound_apply_and_exact_undo_preserve_original_bytes_and_stable_uids(self):
        before = self.files()
        with self.admission():
            plan = get_scope_preview(self.root, 1)
            result = apply_source_scope(self.root, 1, plan['revision'])
            self.assertEqual(['uid-1'], result['retained_uids'])
            rows = json.loads((self.root/'chunks.json').read_bytes())
            self.assertEqual((0,'uid-1'), (rows[0]['id'],rows[0]['uid']))
            state = json.loads((self.root/'state.json').read_bytes())
            self.assertEqual('unchanged', state['book_generation'])
            self.assertTrue((self.root/UNDO).exists())
            undo = get_source_scope_undo_preview(self.root)
            apply_source_scope_undo(self.root, undo['revision'])
        self.assertEqual(before, self.files())

    def test_stale_apply_and_changed_project_undo_are_refused_without_clobbering(self):
        with self.admission():
            before = self.files(); plan = get_scope_preview(self.root, 1)
            with self.assertRaisesRegex(ValueError, 'preview changed'):
                apply_source_scope(self.root, 1, '0'*64)
            self.assertEqual(before, self.files())
            apply_source_scope(self.root, 1, plan['revision'])
            undo = get_source_scope_undo_preview(self.root)
            path = self.root/'chunks.json'; path.write_bytes(path.read_bytes()+b' ')
            changed = self.files()
            with self.assertRaisesRegex(ValueError, 'Project changed'):
                apply_source_scope_undo(self.root, undo['revision'])
            self.assertEqual(changed, self.files())

    def test_corrupt_saved_bytes_stale_undo_and_changed_source_are_refused(self):
        import base64
        with self.admission():
            plan = get_scope_preview(self.root, 1)
            apply_source_scope(self.root, 1, plan['revision'])
            undo = get_source_scope_undo_preview(self.root)
            with self.assertRaisesRegex(ValueError, 'undo changed'):
                apply_source_scope_undo(self.root, '0'*64)
            path = self.root/UNDO; raw = path.read_bytes(); value = json.loads(raw)
            value['before']['state.json'] = base64.b64encode(b'{}').decode()
            path.write_text(json.dumps(value)); changed = self.files()
            with self.assertRaisesRegex(ValueError, 'undo bytes changed'):
                apply_source_scope_undo(self.root, undo['revision'])
            self.assertEqual(changed, self.files()); path.write_bytes(raw)
            source = self.root/'source.txt'; source.write_text(source.read_text()+' Extra.')
            changed = self.files()
            with self.assertRaisesRegex(ValueError, 'artifact changed'):
                apply_source_scope_undo(self.root, undo['revision'])
            self.assertEqual(changed, self.files())

    def test_book_lock_alone_does_not_authorize_mutation(self):
        with ensure_book_state(self.root, recover=False):
            with self.assertRaisesRegex(ValueError, 'Task and book admission'):
                apply_source_scope(self.root, 1, '0'*64)

    def test_removed_prefix_file_is_preserved_and_changed_bytes_refuse_exact_undo(self):
        path = self.root/'removed.wav'; path.write_bytes(b'original removed artifact')
        chunks_path = self.root/'chunks.json'; rows = json.loads(chunks_path.read_bytes())
        rows[0].update(status='done', audio_path='removed.wav')
        chunks_path.write_text(json.dumps(rows))
        with self.admission():
            plan = get_scope_preview(self.root, 1)
            self.assertEqual('removed.wav', plan['preserved_files'][0]['path'])
            self.assertEqual([], [item for item in plan['audio'] if item['uid']=='uid-0'])
            apply_source_scope(self.root, 1, plan['revision'])
            undo = get_source_scope_undo_preview(self.root)
            self.assertEqual(b'original removed artifact', path.read_bytes())
            path.write_bytes(b'changed after scope apply'); changed = self.files()
            with self.assertRaisesRegex(ValueError, 'artifact changed'):
                apply_source_scope_undo(self.root, undo['revision'])
            self.assertEqual(changed, self.files())

    def test_exports_are_preserved_and_new_delivery_refuses_exact_undo(self):
        path = self.root/'cloned_audiobook.mp3';path.write_bytes(b'old delivery')
        with self.admission():
            plan=get_scope_preview(self.root,1)
            apply_source_scope(self.root,1,plan['revision'])
            undo=get_source_scope_undo_preview(self.root)
            self.assertEqual(b'old delivery',path.read_bytes())
            path.write_bytes(b'new delivery after scope apply');changed=self.files()
            with self.assertRaisesRegex(ValueError,'Exports changed'):
                apply_source_scope_undo(self.root,undo['revision'])
            self.assertEqual(changed,self.files())

    def _seed_legacy_exports(self):
        (self.root/'cloned_audiobook.mp3').write_bytes(b'synthetic legacy mp3 delivery')
        (self.root/'audiobook.m4b').write_bytes(b'synthetic legacy m4b delivery')
        (self.root/'audacity_export.zip').write_bytes(b'synthetic legacy audacity delivery')
        chapter = self.root/'chapter_exports'; chapter.mkdir(exist_ok=True)
        (chapter/'chapter-01.txt').write_bytes(b'synthetic legacy chapter one')

    def _apply_legacy_exports(self, version=2):
        # Caller must hold admission. Patch EXPORTS only for preview+apply so
        # the persisted undo exports are the historical four-key inventory.
        with patch('project_scope_exports.EXPORTS', project_scope_exports.LEGACY_EXPORTS):
            plan = get_scope_preview(self.root, 1)
            self.assertEqual(set(project_scope_exports.LEGACY_EXPORTS), set(plan['export_inventory']))
            self.assertEqual(4, len(plan['export_inventory']))
            apply_source_scope(self.root, 1, plan['revision'])
        record = json.loads((self.root/UNDO).read_bytes())
        self.assertEqual(set(project_scope_exports.LEGACY_EXPORTS), set(record['exports']))
        if version == 1:
            # Legacy metadata record: drop only the binding keys, retaining
            # the real before/after bytes and hashes for the legacy files.
            self.assertEqual(2, record['version'])
            for field in ('before', 'before_sha256', 'after'):
                self.assertIn(BINDING_NAME, record[field])
                record[field].pop(BINDING_NAME)
            record['version'] = 1
            (self.root/UNDO).write_text(json.dumps(record))
        else:
            self.assertEqual(2, record['version'])

    def _assert_exports_refuse_undo(self):
        before_readonly = self.files(); undo_bytes = (self.root/UNDO).read_bytes()
        status = get_source_scope_undo_status(self.root)
        self.assertTrue(status['available'], status)
        self.assertFalse(status['can_undo'], status)
        self.assertIsNotNone(status['revision'])
        with self.assertRaisesRegex(ValueError, 'Exports changed'):
            get_source_scope_undo_preview(self.root)
        self.assertEqual(before_readonly, self.files())
        self.assertEqual(undo_bytes, (self.root/UNDO).read_bytes())
        with self.assertRaisesRegex(ValueError, 'Exports changed'):
            apply_source_scope_undo(self.root, status['revision'])
        self.assertTrue((self.root/UNDO).exists())

    def _check_historical_unchanged_undo(self, version):
        self._seed_legacy_exports()
        before = self.files()
        chapter_bytes = (self.root/'chapter_exports'/'chapter-01.txt').read_bytes()
        with self.admission():
            self._apply_legacy_exports(version=version)
            # EXPORTS is restored to the current seven-key inventory here.
            self.assertEqual(7, len(project_scope_exports.EXPORTS))
            applied = self.files(); undo_bytes = (self.root/UNDO).read_bytes()
            status = get_source_scope_undo_status(self.root)
            self.assertTrue(status['available'], status)
            self.assertTrue(status['can_undo'], status)
            undo = get_source_scope_undo_preview(self.root)
            self.assertEqual(applied, self.files())
            self.assertEqual(undo_bytes, (self.root/UNDO).read_bytes())
            apply_source_scope_undo(self.root, undo['revision'])
        self.assertEqual(before, self.files())
        self.assertEqual(chapter_bytes, (self.root/'chapter_exports'/'chapter-01.txt').read_bytes())
        self.assertFalse((self.root/UNDO).exists())

    def test_historical_legacy_exports_unchanged_exact_undo_restores_original_bytes_v2(self):
        self._check_historical_unchanged_undo(version=2)

    def test_historical_legacy_exports_unchanged_exact_undo_restores_original_bytes_v1(self):
        self._check_historical_unchanged_undo(version=1)

    def _check_legacy_export_change_refuses(self, name):
        self._seed_legacy_exports()
        with self.admission():
            self._apply_legacy_exports()
            if name == 'chapter_exports':
                path = self.root/'chapter_exports'/'chapter-01.txt'
                path.write_bytes(path.read_bytes() + b' changed')
            else:
                path = self.root/name
                path.write_bytes(path.read_bytes() + b' changed')
            changed = self.files()
            chapter_bytes = (self.root/'chapter_exports'/'chapter-01.txt').read_bytes()
            self._assert_exports_refuse_undo()
            self.assertEqual(changed, self.files())
            self.assertEqual(chapter_bytes, (self.root/'chapter_exports'/'chapter-01.txt').read_bytes())

    def test_historical_legacy_cloned_audiobook_change_refuses_undo(self):
        self._check_legacy_export_change_refuses('cloned_audiobook.mp3')

    def test_historical_legacy_audiobook_m4b_change_refuses_undo(self):
        self._check_legacy_export_change_refuses('audiobook.m4b')

    def test_historical_legacy_audacity_export_change_refuses_undo(self):
        self._check_legacy_export_change_refuses('audacity_export.zip')

    def test_historical_legacy_chapter_exports_change_refuses_undo(self):
        self._check_legacy_export_change_refuses('chapter_exports')

    def _check_new_artifact_refuses(self, name, present_before=False):
        self._seed_legacy_exports()
        if present_before:
            (self.root/name).write_bytes(b'synthetic read-along artifact before scope')
        with self.admission():
            self._apply_legacy_exports()
            if not present_before:
                (self.root/name).write_bytes(b'synthetic read-along artifact after scope')
            changed = self.files()
            self._assert_exports_refuse_undo()
            self.assertEqual(changed, self.files())

    def test_historical_new_readalong_sidecar_refuses_undo(self):
        self._check_new_artifact_refuses('readalong.sidecar.json')

    def test_historical_new_readaloud_epub_refuses_undo(self):
        self._check_new_artifact_refuses('readaloud.epub')

    def test_historical_new_readaloud_receipt_refuses_undo(self):
        self._check_new_artifact_refuses('readaloud.receipt.json')

    def test_historical_new_artifact_present_before_apply_refuses_undo(self):
        # The old producer could not record this artifact, so its presence
        # before apply cannot be verified as unchanged at undo time.
        self._check_new_artifact_refuses('readaloud.epub', present_before=True)

    def test_malformed_export_inventory_key_sets_refuse_without_changing_metadata_or_deleting_undo(self):
        with self.admission():
            self._apply_legacy_exports()
            undo_path = self.root/UNDO; original = undo_path.read_bytes()
            missing = json.loads(original); missing['exports'].pop('audiobook.m4b')
            extra = json.loads(original); extra['exports']['unexpected_export'] = {}
            renamed = json.loads(original); renamed['exports']['cloned_audiobook.bak'] = renamed['exports'].pop('cloned_audiobook.mp3')
            empty = json.loads(original); empty['exports'] = {}
            non_dict = json.loads(original); non_dict['exports'] = []
            variants = [('missing', missing), ('extra', extra), ('renamed', renamed), ('empty', empty), ('non_dict', non_dict)]
            for label, variant in variants:
                with self.subTest(variant=label):
                    undo_path.write_text(json.dumps(variant))
                    changed = self.files()
                    status = get_source_scope_undo_status(self.root)
                    self.assertTrue(status['available'], status)
                    self.assertFalse(status['can_undo'], status)
                    self.assertIsNotNone(status['revision'])
                    with self.assertRaises(ValueError):
                        get_source_scope_undo_preview(self.root)
                    with self.assertRaises(ValueError):
                        apply_source_scope_undo(self.root, status['revision'])
                    self.assertEqual(changed, self.files())
                    self.assertTrue(undo_path.exists())
                    self.assertEqual(json.dumps(variant), undo_path.read_text())
            undo_path.write_bytes(original)

    def _check_version_type_refuses(self, bad_value, legacy):
        (self.root/'cloned_audiobook.mp3').write_bytes(b'')
        with self.admission():
            if legacy:
                self._apply_legacy_exports()
            else:
                plan = get_scope_preview(self.root, 1)
                apply_source_scope(self.root, 1, plan['revision'])
            undo_path = self.root/UNDO; record = json.loads(undo_path.read_bytes())
            row = record['exports']['cloned_audiobook.mp3']['cloned_audiobook.mp3']
            self.assertEqual(0, row[2]); self.assertIs(int, type(row[2]))
            row[2] = bad_value
            undo_path.write_text(json.dumps(record))
            applied = self.files(); undo_bytes = undo_path.read_bytes()
            status = get_source_scope_undo_status(self.root)
            self.assertTrue(status['available'], status)
            self.assertFalse(status['can_undo'], status)
            self.assertEqual(applied, self.files()); self.assertEqual(undo_bytes, undo_path.read_bytes())
            with self.assertRaisesRegex(ValueError, 'Exports changed'):
                get_source_scope_undo_preview(self.root)
            self.assertEqual(applied, self.files()); self.assertEqual(undo_bytes, undo_path.read_bytes())
            with self.assertRaisesRegex(ValueError, 'Exports changed'):
                apply_source_scope_undo(self.root, status['revision'])
            self.assertEqual(applied, self.files()); self.assertEqual(undo_bytes, undo_path.read_bytes())
            self.assertTrue(undo_path.exists())

    def test_historical_legacy_false_version_refuses_undo(self):
        self._check_version_type_refuses(False, legacy=True)

    def test_historical_legacy_float_version_refuses_undo(self):
        self._check_version_type_refuses(0.0, legacy=True)

    def test_current_false_version_refuses_undo(self):
        self._check_version_type_refuses(False, legacy=False)

    def test_current_float_version_refuses_undo(self):
        self._check_version_type_refuses(0.0, legacy=False)

    def test_export_inventory_compatibility_finite_domain(self):
        legacy = set(project_scope_exports.LEGACY_EXPORTS); current_keys = list(project_scope_exports.EXPORTS)
        for size in range(len(current_keys) + 1):
            for subset in itertools.combinations(current_keys, size):
                recorded = {name: {} for name in subset}
                current = {name: {} for name in project_scope_exports.EXPORTS}
                recorded_before = copy.deepcopy(recorded); current_before = copy.deepcopy(current)
                expected = set(subset) in (legacy, set(current_keys))
                self.assertEqual(expected, project_scope_exports.is_scope_export_inventory_compatible(recorded, current), subset)
                self.assertEqual(recorded_before, recorded); self.assertEqual(current_before, current)
        for recorded in ({**{name: {} for name in legacy}, 'unknown_export': {}}, {**{name: {} for name in current_keys}, 'unknown_export': {}}, {'unknown_export': {}}):
            current = {name: {} for name in project_scope_exports.EXPORTS}
            recorded_before = copy.deepcopy(recorded); current_before = copy.deepcopy(current)
            self.assertFalse(project_scope_exports.is_scope_export_inventory_compatible(recorded, current))
            self.assertEqual(recorded_before, recorded); self.assertEqual(current_before, current)

    def test_export_inventory_compatibility_refuses_malformed_current_new_slots(self):
        for bad in (None, False, 0, '', []):
            for recorded_keys in (project_scope_exports.LEGACY_EXPORTS, project_scope_exports.EXPORTS):
                recorded = {name: {} for name in recorded_keys}
                current = {name: {} for name in project_scope_exports.EXPORTS}
                current['readaloud.epub'] = bad
                recorded_before = copy.deepcopy(recorded); current_before = copy.deepcopy(current)
                self.assertFalse(project_scope_exports.is_scope_export_inventory_compatible(recorded, current), (bad, recorded_keys))
                self.assertEqual(recorded_before, recorded); self.assertEqual(current_before, current)
