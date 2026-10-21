"""Actual policy routes recover damaged bytes and reject stale authority."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from tests import test_editor_take_history as editor_cases
from book_state_transaction import get_book_operation_token
from routers import editor
from take_retention import KEY, POLICY
from tests.test_support import use_private_cpu_fixture_lock


class TakeHistoryPolicyTests(unittest.TestCase):
    fixture = editor_cases.EditorTakeHistoryTests.fixture

    def change(self, client, policy, enabled, **changes):
        return client.post('/api/take_history_policy', json={
            'expected_book_token': policy['book_token'],
            'expected_revision': policy['revision'], 'enabled': enabled, **changes})

    def test_real_routes_default_enable_strict_boolean_and_stale_revision(self):
        with self.fixture() as (root, manager, client, _):
            policy = client.get('/api/take_history_policy').json()
            self.assertFalse(policy['enabled'])
            for value in (1, 0, 'true', None):
                self.assertEqual(self.change(client, policy, value).status_code, 422)
            self.assertFalse((root/POLICY).exists())
            response = self.change(client, policy, True)
            self.assertEqual(response.status_code, 200, response.text)
            enabled = response.json(); self.assertTrue(enabled['enabled'])
            self.assertEqual(json.loads((root/'take_history/manifest.json').read_bytes())['version'], 2)
            before = (root/POLICY).read_bytes()
            self.assertEqual(self.change(client, policy, False).status_code, 409)
            self.assertEqual((root/POLICY).read_bytes(), before)
            self.assertFalse(self.change(client, enabled, False).json()['enabled'])

    def test_changed_book_refuses_policy_write(self):
        with self.fixture() as (root, manager, client, _):
            policy = client.get('/api/take_history_policy').json()
            (root/'state.json').write_text(json.dumps({'active_book_id':'another', 'book_generation':'new'}))
            self.assertNotEqual(get_book_operation_token(root), policy['book_token'])
            response = self.change(client, policy, True)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertFalse((root/POLICY).exists())
            self.assertFalse((root/'take_history').exists())

    def test_deep_or_malformed_policy_can_be_disabled_with_unreadable_archive_and_chunks(self):
        with self.fixture() as (root, manager, client, _):
            (root/'chunks.json').write_bytes(b'preserve damaged chunks')
            directory = root/'take_history'; directory.mkdir()
            manifest = directory/'manifest.json'; manifest.write_bytes(b'preserve damaged archive')
            for raw in (b'bad policy', b'', b'[' * 2000 + b'0' + b']' * 2000):
                with self.subTest(size=len(raw)):
                    (root/POLICY).write_bytes(raw)
                    response = client.get('/api/take_history_policy')
                    self.assertEqual(response.status_code, 200, response.text)
                    damaged = response.json()
                    self.assertIsNone(damaged['enabled']); self.assertTrue(damaged['error'])
                    self.assertEqual((root/POLICY).read_bytes(), raw)
                    response = self.change(client, damaged, False)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertFalse(response.json()['enabled'])
                    self.assertEqual(manifest.read_bytes(), b'preserve damaged archive')
                    self.assertEqual((root/'chunks.json').read_bytes(), b'preserve damaged chunks')

    def test_public_editor_view_redacts_private_receipt_without_mutating_saved_rows(self):
        with self.fixture() as (root, manager, client, _):
            policy = client.get('/api/take_history_policy').json()
            self.assertEqual(self.change(client, policy, True).status_code, 200)
            manager.update_chunk(0, {'text':'Edited words.'})
            rows = manager.load_chunks(); before = copy.deepcopy(rows)
            self.assertIsInstance(rows[0][KEY], dict)
            views = editor._get_editor_chunk_views(rows, get_book_operation_token(root))
            self.assertNotIn(KEY, views[0])
            self.assertEqual(rows, before); self.assertEqual(manager.load_chunks(), before)

    def test_deleted_chunk_undo_protects_archive_until_restore_or_expiration(self):
        with self.fixture() as (root, manager, client, request), \
                patch.object(editor, '_deleted_chunk_receipts', {}):
            policy = client.get('/api/take_history_policy').json()
            self.assertEqual(self.change(client, policy, True).status_code, 200)
            manager.update_chunk(0, {'text':'Edited words.'})
            captured = manager.load_chunks()[0]
            manager._update_generation_fields(captured, status='generating')
            held = manager.load_chunks()[0][KEY]; archive = held['archive']
            rows = manager.load_chunks()
            rows.append(dict(id=1, uid='remaining', speaker='NARRATOR', text='Another line.', status='pending'))
            manager.save_chunks(rows)
            response = client.delete('/api/chunks/0', params={
                'expected_book_token': request['expected_book_token'], 'expected_uid':'line'})
            self.assertEqual(response.status_code, 200, response.text)
            deleted = response.json()
            action = dict(expected_book_token=request['expected_book_token'], source_book_token=held['book_token'],
                expected_uid='line', version=archive['version'], sha256=archive['sha256'], action='delete')
            self.assertEqual(client.post('/api/take_snapshots/action', json=action).status_code, 409)
            response = client.post('/api/chunks/restore', json={
                'chunk':deleted['deleted'], 'at_index':0, 'undo_token':deleted['undo_token']})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(manager.load_chunks()[0][KEY], held)
            self.assertEqual(client.post('/api/take_snapshots/action', json=action).status_code, 409)
            response = client.delete('/api/chunks/0', params={
                'expected_book_token': request['expected_book_token'], 'expected_uid':'line'})
            self.assertEqual(response.status_code, 200, response.text)
            receipt = editor._deleted_chunk_receipts[response.json()['undo_token']]
            receipt['created'] -= 61
            self.assertEqual(client.post('/api/take_snapshots/action', json=action).status_code, 200)

    def test_deep_private_receipt_refuses_reads_and_edits_before_copy_or_mutation(self):
        for journal in (False, True):
            with self.subTest(journal=journal), self.fixture() as (root, manager, client, request), \
                    use_private_cpu_fixture_lock():
                policy = client.get('/api/take_history_policy').json()
                self.assertEqual(self.change(client, policy, True).status_code, 200)
                manager.insert_chunk(0); manager.update_chunk(0, {'text':'Edited words.'})
                if journal: manager._get_status_journal_locked().ensure_writable()
                chunks = root/'chunks.json'; rows = json.loads(chunks.read_bytes())
                deep = 0
                for _ in range(500): deep = [deep]
                rows[0][KEY]['captured']['extra_evidence'] = deep
                raw = json.dumps(rows).encode(); json.loads(raw)
                chunks.write_bytes(raw)
                status = root/'chunks.json.status.jsonl'
                if journal:
                    # Controlled corruption with a coherent snapshot binding:
                    # exercise receipt validation rather than digest rejection.
                    header = json.loads(status.read_bytes().splitlines()[0])
                    header['snapshot_sha256'] = hashlib.sha256(raw).hexdigest()
                    status.write_bytes(json.dumps(header).encode() + b'\n')
                status_before = status.read_bytes() if journal else None
                audio = (root/'voicelines/line.wav').read_bytes()
                params = dict(expected_book_token=request['expected_book_token'], expected_uid='line')
                calls = [lambda: client.get('/api/chunks'), lambda: client.get('/api/chunks/status'),
                         lambda: client.delete('/api/chunks/0', params=params),
                         lambda: client.post('/api/chunks/0/insert', params=params),
                         lambda: client.post('/api/chunks/0', params=params, json={'text':'Must not save.'})]
                for call in calls:
                    response = call()
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertIn('nested', response.text)
                    self.assertEqual(chunks.read_bytes(), raw)
                    self.assertEqual(status.read_bytes() if journal else None, status_before)
                    self.assertEqual((root/'voicelines/line.wav').read_bytes(), audio)
                # Recovery controls stay usable without loading damaged chunks.
                current = client.get('/api/take_history_policy').json()
                self.assertFalse(self.change(client, current, False).json()['enabled'])
                self.assertEqual(chunks.read_bytes(), raw)
