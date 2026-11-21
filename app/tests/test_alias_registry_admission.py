"""Alias saves fail closed on pending recovery and take task admission first."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import alias_registry_apply
import book_state_transaction as books
import core
from routers import script
from utils import atomic_json_write, file_lock


class AliasRegistryAdmissionTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aliases = root / 'character_aliases.json'
            atomic_json_write({'EXISTING': 'ROOT'}, aliases)
            atomic_json_write({'input_file_path': 'book.txt'}, root / 'state.json')
            app = FastAPI()
            app.include_router(script.router)
            patches = [
                patch.object(script, 'DATA_DIR', tmp),
                patch.object(script, 'CHARACTER_ALIASES_PATH', str(aliases)),
                patch.object(core, 'DATA_DIR', tmp),
                patch.object(core, 'process_state', {}),
                patch.object(core, '_task_claims', {}),
                patch.object(core, '_gpu_leases', {}),
            ]
            for item in patches:
                item.start()
            try:
                with TestClient(app) as client:
                    yield root, aliases, client
            finally:
                for item in reversed(patches):
                    item.stop()

    def _leave_journal(self, root):
        class Crash(BaseException):
            pass

        def crash(*args, **kwargs):
            raise Crash()

        with patch.object(books, '_validate_staged_replacement', side_effect=crash), \
             patch.object(books, '_retire_unpublished_journal', side_effect=crash), \
             self.assertRaises(Crash):
            with books.ensure_book_state(root):
                books.apply_book_state_locked(
                    str(root), {'state.json': b'{"input_file_path": "book.txt"}'}, [])
        journal = root / books.JOURNAL
        self.assertTrue(journal.exists())
        return journal

    def test_pending_recovery_refuses_alias_save_without_touching_artifacts(self):
        with self.fixture() as (root, aliases, client):
            journal = self._leave_journal(root)
            journal_before = journal.read_bytes()
            aliases_before = aliases.read_bytes()
            response = client.post('/api/character_aliases', json={'BOB': 'ROBERT'})
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('recovery is required', response.json()['detail'])
            self.assertEqual(aliases_before, aliases.read_bytes())
            self.assertEqual(journal_before, journal.read_bytes())

    def test_save_succeeds_after_normal_recovery(self):
        with self.fixture() as (root, aliases, client):
            journal = self._leave_journal(root)
            with books.ensure_book_state(str(root)):
                pass
            self.assertFalse(journal.exists())
            response = client.post('/api/character_aliases', json={'BOB': 'ROBERT'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual({'BOB': 'ROBERT'}, json.loads(aliases.read_text()))

    def test_foreign_task_claim_refuses_before_book_lock(self):
        with self.fixture() as (root, aliases, client):
            before = aliases.read_bytes()
            with patch.object(core, 'acquire_task_lease',
                              side_effect=RuntimeError('foreign worker owns task')), \
                 patch.object(alias_registry_apply, 'ensure_book_state') as book_guard:
                response = client.post('/api/character_aliases', json={'BOB': 'ROBERT'})
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('Another app worker owns a model task', response.json()['detail'])
            book_guard.assert_not_called()
            self.assertEqual(before, aliases.read_bytes())

    def test_busy_book_file_lock_is_translated_without_writing(self):
        with self.fixture() as (root, aliases, client):
            before = aliases.read_bytes()
            with file_lock(str(root / 'annotated_script.json'), timeout=5):
                response = client.post('/api/character_aliases', json={'BOB': 'ROBERT'})
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('busy', response.json()['detail'])
            self.assertEqual(before, aliases.read_bytes())
