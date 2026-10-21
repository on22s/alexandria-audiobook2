"""Real predecessor bytes/metadata survive edits and generation commit failures."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from audio_take_history import AudioTakeHistory
from book_state_transaction import ensure_book_state, get_book_operation_token
from project import ProjectManager
from take_retention import (KEY, POLICY, get_take_history_policy, set_take_history_policy,
    MAX_RECEIPT_BYTES, get_retained_take_bytes)


class AutomaticTakeHistoryTests(unittest.TestCase):
    @contextmanager
    def fixture(self, journal=False, prior=True, enabled=True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'state.json').write_text(json.dumps({'active_book_id':'synthetic', 'book_generation':'one'}))
            manager = ProjectManager(tmp)
            previous, fresh = root/'voicelines/prior.wav', root/'fresh.wav'
            for path, amplitude in ((previous, .1), (fresh, .3)):
                sf.write(path, np.full(2400, amplitude), 24000)
            manager.save_chunks([dict(id=0, uid='line', text='Original synthetic words.',
                speaker='NARRATOR', instruct='', take=2, status='done' if prior else 'pending',
                audio_path='voicelines/prior.wav' if prior else None, audio_revision='original-revision')])
            if enabled:
                with ensure_book_state(tmp):
                    set_take_history_policy(manager, True, get_take_history_policy(tmp)['revision'])
            if journal:
                active = manager._get_status_journal_locked()
                active.ensure_writable()
                manager._active_journal_identity = copy.deepcopy(active.book_identity)
            with patch('project._export_audio_segment', side_effect=RuntimeError('force actual WAV fallback')):
                yield root, manager, previous, fresh

    def policy(self, manager, enabled):
        with ensure_book_state(manager.root_dir):
            return set_take_history_policy(manager, enabled, get_take_history_policy(manager.root_dir, tolerate_invalid=True)['revision'])

    def test_policy_is_off_by_default_versioned_and_disables_without_parsing_archive(self):
        with self.fixture(enabled=False) as (root, manager, _, _):
            policy = get_take_history_policy(root); self.assertFalse(policy['enabled'])
            self.assertFalse((root/'take_history').exists())
            self.policy(manager, True)
            self.assertEqual(json.loads((root/'take_history/manifest.json').read_text())['version'], 2)
            with ensure_book_state(root), self.assertRaisesRegex(ValueError, 'changed'):
                set_take_history_policy(manager, False, policy['revision'])
            (root/'take_history/manifest.json').write_bytes(b'corrupt archive bytes')
            self.assertFalse(self.policy(manager, False)['enabled'])
            self.assertEqual((root/'take_history/manifest.json').read_bytes(), b'corrupt archive bytes')
            for raw in (b'bad policy', b''):
                (root/POLICY).write_bytes(raw)
                self.assertIsNone(get_take_history_policy(root, tolerate_invalid=True)['enabled'])
                with self.assertRaisesRegex(ValueError, 'damaged'): get_take_history_policy(root)
                self.assertFalse(self.policy(manager, False)['enabled'])

    def test_original_authored_receipt_survives_repeated_edits_without_copying_payload(self):
        for journal in (False, True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, previous, _):
                original = previous.read_bytes()
                manager.update_chunk(0, {'text':'Changed words.', 'instruct':'Quietly', 'take':3})
                first = manager.load_chunks()[0][KEY]
                self.assertEqual(first['captured']['text'], 'Original synthetic words.')
                self.assertEqual(first['captured']['take'], 2)
                self.assertIsNone(first['archive'])
                manager.update_chunk(0, {'text':'Edited again.', 'speaker':'ALICE', 'take':4})
                row = manager.load_chunks()[0]
                self.assertEqual(row[KEY], first); self.assertIsNone(row['audio_path'])
                self.assertEqual(AudioTakeHistory(root).list()['total'], 0)
                self.assertEqual(previous.read_bytes(), original)

    def test_early_generation_archives_original_receipt_and_success_clears_only_hold(self):
        for journal in (False, True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, previous, fresh):
                original = previous.read_bytes(); manager.update_chunk(0, {'text':'Changed words.', 'take':7})
                captured = manager.load_chunks()[0]
                prepared = manager._update_generation_fields(captured, status='generating')
                hold = prepared[KEY]; archive = root/hold['archive']['path']
                self.assertEqual(archive.read_bytes(), original)
                self.assertEqual(hold['captured']['text'], 'Original synthetic words.')
                self.assertEqual(hold['captured']['take'], 2)
                result = manager._publish_generated_chunk_audio(captured, str(fresh), 'NARRATOR')
                row = manager.load_chunks()[0]
                self.assertIsNone(row[KEY]); self.assertEqual(row['text'], 'Changed words.')
                self.assertEqual(row['take'], 7); self.assertEqual(row['audio_path'], result)
                self.assertEqual((root/result).read_bytes(), fresh.read_bytes())
                self.assertEqual(archive.read_bytes(), original)
                self.assertEqual(AudioTakeHistory(root).list()['total'], 1)

    def test_direct_publication_commits_archive_hold_before_replacing_any_slot(self):
        for journal in (False, True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, previous, fresh):
                original = previous.read_bytes(); manager.update_chunk(0, {'text':'New authored words.'})
                captured = manager.load_chunks()[0]
                publish = __import__('project').publish_audio_output
                checked = []
                def install(source, target, *args, **kwargs):
                    if '.slot-' in str(target):
                        # The caller already owns the kernel chunk lock: read/replay
                        # persisted bytes directly instead of recursively locking.
                        hold = ProjectManager(str(root))._get_saved_chunk_rows_locked()[0][KEY]
                        self.assertIsNotNone(hold['archive'])
                        self.assertEqual((root/hold['archive']['path']).read_bytes(), original)
                        checked.append(1)
                    return publish(source, target, *args, **kwargs)
                with patch('project.publish_audio_output', side_effect=install):
                    manager._publish_generated_chunk_audio(captured, str(fresh), 'NARRATOR')
                self.assertEqual(checked, [1]); self.assertIsNone(manager.load_chunks()[0][KEY])

    def test_capacity_refuses_before_model_work_and_first_pass_ignores_archive(self):
        for journal in (False, True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, previous, _):
                original = previous.read_bytes()
                with patch('audio_take_history.MAX_RECORDS', 0), patch.object(manager, 'get_engine', side_effect=AssertionError('model must not start')) as engine:
                    ok, message = manager.generate_chunk_audio(0)
                self.assertFalse(ok); self.assertIn('full', message); engine.assert_not_called()
                self.assertEqual(previous.read_bytes(), original)
                self.assertEqual(manager.load_chunks()[0]['audio_revision'], 'original-revision')
            with self.subTest(journal=journal, first=True), self.fixture(journal, prior=False) as (root, manager, _, fresh):
                (root/'take_history/manifest.json').write_bytes(b'corrupt full archive')
                with patch('audio_take_history.MAX_RECORDS', 0):
                    row = manager.load_chunks()[0]
                    manager._update_generation_fields(row, status='generating')
                    path = manager._publish_generated_chunk_audio(row, str(fresh), 'NARRATOR')
                self.assertEqual((root/path).read_bytes(), fresh.read_bytes())
                self.assertEqual((root/'take_history/manifest.json').read_bytes(), b'corrupt full archive')
                self.assertNotIn(KEY, manager.load_chunks()[0])

    def test_file_version_refuses_changed_predecessor_without_relabeling_it(self):
        for journal in (False, True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, previous, fresh):
                manager.update_chunk(0, {'text':'Edited words.'})
                before = copy.deepcopy(manager.load_chunks()[0]); previous.write_bytes(fresh.read_bytes())
                with self.assertRaisesRegex(ValueError, 'Prior take changed'):
                    manager._update_generation_fields(copy.deepcopy(before), status='generating')
                self.assertEqual(manager.load_chunks()[0], before)
                self.assertEqual(AudioTakeHistory(root).list()['total'], 0)

    def test_held_archive_blocks_delete_and_publication_failure_can_retry_after_old_slot_changes(self):
        for journal in (False, True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, previous, fresh):
                original = previous.read_bytes(); manager.update_chunk(0, {'text':'Edited words.'})
                captured = manager.load_chunks()[0]; manager._update_generation_fields(captured, status='generating')
                held = manager.load_chunks()[0][KEY]; archive = held['archive']; history = AudioTakeHistory(root)
                from editor_take_history import change_editor_take
                from types import SimpleNamespace
                request = SimpleNamespace(expected_book_token=get_book_operation_token(root),
                    source_book_token=held['book_token'], expected_uid='line', version=archive['version'],
                    sha256=archive['sha256'], action='delete')
                with self.assertRaisesRegex(ValueError, 'referenced'): change_editor_take(manager, request)
                # Simulate an installed replacement followed by failed metadata.
                if journal:
                    owner, method = manager._get_status_journal_locked(), '_append'
                else: owner, method = manager, '_save_chunks_locked'
                original_write = getattr(owner, method)
                def fail_completed(*args, **kwargs):
                    value = args[0] if not journal else args[0].get('changes', {})
                    if (not journal and any(row.get('status') == 'done' for row in value)
                            or journal and value.get('status') == 'done'):
                        raise OSError('final metadata refused')
                    return original_write(*args, **kwargs)
                with patch.object(owner, method, side_effect=fail_completed):
                    with self.assertRaisesRegex(OSError, 'final metadata refused'):
                        manager._publish_generated_chunk_audio(captured, str(fresh), 'NARRATOR')
                previous.write_bytes(b'prior slot reused after interruption')
                self.assertEqual((root/archive['path']).read_bytes(), original)
                path = manager._publish_generated_chunk_audio(captured, str(fresh), 'NARRATOR')
                self.assertEqual((root/path).read_bytes(), fresh.read_bytes())
                self.assertEqual((root/archive['path']).read_bytes(), original)
                self.assertIsNone(manager.load_chunks()[0][KEY])

    def test_disabling_keeps_hold_until_success_and_avoids_new_archive_admission(self):
        for journal in (False, True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, previous, fresh):
                manager.update_chunk(0, {'text':'Edited words.'}); captured = manager.load_chunks()[0]
                manager._update_generation_fields(captured, status='generating')
                hold = manager.load_chunks()[0][KEY]; original = (root/hold['archive']['path']).read_bytes()
                self.policy(manager, False); self.assertEqual(manager.load_chunks()[0][KEY], hold)
                (root/'take_history/manifest.json').write_bytes(b'archive unavailable')
                with patch('audio_take_history.MAX_RECORDS', 0):
                    manager._publish_generated_chunk_audio(captured, str(fresh), 'NARRATOR')
                self.assertIsNone(manager.load_chunks()[0][KEY])
                self.assertEqual((root/hold['archive']['path']).read_bytes(), original)

    def test_journal_status_updates_keep_cached_rows_and_bound_receipt_charge(self):
        with self.fixture(True) as (root, manager, _, _):
            manager.update_chunk(0, {'text':'Edited words.'})
            journal = manager._get_status_journal_locked(); journal.ensure_writable()
            before = manager.load_chunks()[0]
            with patch.object(journal, 'get_rows', side_effect=AssertionError('per-UID full-book read')):
                manager._update_chunk_fields_by_uid('line', status='pending', error='expected fixture error')
            self.assertEqual(journal._retained_bytes, get_retained_take_bytes(before))
            current = journal.get_row('line')
            bad = copy.deepcopy(current[KEY]); bad['captured']['text'] = 'x' * MAX_RECEIPT_BYTES
            journal_bytes = journal.path.read_bytes()
            with self.assertRaises(ValueError): journal.apply_update('line', {KEY:bad})
            self.assertEqual(journal.path.read_bytes(), journal_bytes)

    def test_native_batch_capacity_preflight_leaves_no_generating_rows_or_model_load(self):
        for journal in (False, True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, previous, _):
                original = previous.read_bytes()
                rows = manager.load_chunks(); second = copy.deepcopy(rows[0])
                second.update(id=1, uid='second-line', text='Another synthetic line.')
                manager.save_chunks([*rows, second])
                with patch('audio_take_history.MAX_RECORDS', 1), \
                        patch.object(manager, 'get_engine', side_effect=AssertionError('model must not start')) as engine:
                    result = manager.generate_chunks_batch([0, 1], batch_size=2)
                engine.assert_not_called()
                self.assertEqual([index for index, _ in result['failed']], [0, 1])
                self.assertEqual(result['completed'], []); self.assertEqual(result['cancelled'], 0)
                self.assertTrue(all(row['status'] == 'done' for row in manager.load_chunks()))
                self.assertEqual(previous.read_bytes(), original)
                self.assertEqual(AudioTakeHistory(root).list()['total'], 1)

    def test_native_batch_reservation_failure_restores_already_reserved_rows(self):
        for journal in (False, True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, previous, _):
                rows = manager.load_chunks(); second = copy.deepcopy(rows[0])
                second.update(id=1, uid='second-line', text='Another synthetic line.')
                manager.save_chunks([*rows, second])
                update = manager._update_generation_fields
                def fail_second(chunk, **fields):
                    if chunk['uid'] == 'second-line' and fields.get('status') == 'generating':
                        raise ValueError('reservation refused')
                    return update(chunk, **fields)
                engine = SimpleNamespace(get_model_settings=lambda: {})
                with patch.object(manager, 'get_engine', return_value=engine), \
                        patch.object(manager, '_update_generation_fields', side_effect=fail_second):
                    result = manager.generate_chunks_batch([0, 1], batch_size=2)
                self.assertEqual([index for index, _ in result['failed']], [0, 1])
                self.assertTrue(all(row['status'] != 'generating' for row in manager.load_chunks()))
                self.assertTrue(all(row[KEY]['archive'] for row in manager.load_chunks()))

    def test_coherent_damaged_suffix_cannot_delete_actual_archive_or_publish(self):
        from editor_take_history import change_editor_take
        for journal in (False, True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, previous, fresh):
                original = previous.read_bytes()
                manager.update_chunk(0, {'text':'Edited words.'})
                captured = manager.load_chunks()[0]
                manager._update_generation_fields(captured, status='generating')
                held = copy.deepcopy(manager.load_chunks()[0][KEY]); archive = copy.deepcopy(held['archive'])
                held['path'] = 'voicelines/prior.mp3'
                held['archive']['path'] = f"take_history/{archive['version']}.mp3"
                if journal:
                    manager._get_status_journal_locked().apply_update('line', {KEY: held})
                else:
                    rows = json.loads((root/'chunks.json').read_bytes()); rows[0][KEY] = held
                    (root/'chunks.json').write_text(json.dumps(rows))
                request = SimpleNamespace(expected_book_token=get_book_operation_token(root),
                    source_book_token=held['book_token'], expected_uid='line', version=archive['version'],
                    sha256=archive['sha256'], action='delete')
                with self.assertRaisesRegex(ValueError, 'referenced'): change_editor_take(manager, request)
                with self.assertRaisesRegex(ValueError, 'path'):
                    manager._publish_generated_chunk_audio(manager.load_chunks()[0], str(fresh), 'NARRATOR')
                history = AudioTakeHistory(root)
                history.change(archive['version'], archive['sha256'], 'delete', lambda path: False,
                    book_token=held['book_token'], uid='line')
                request.action = 'purge'
                with self.assertRaisesRegex(ValueError, 'referenced'): change_editor_take(manager, request)
                self.assertEqual((root/archive['path']).read_bytes(), original)
