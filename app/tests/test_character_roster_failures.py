"""Actual foreign owners and process death during roster publication/recovery."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import book_state_transaction as transaction
from book_state_transaction import ensure_book_state
from cast_list import get_cast_list_path, save_cast_list
from character_roster_apply import (ensure_character_roster_admission, apply_character_roster,
    apply_character_roster_undo, get_character_roster_undo_status)
from character_roster_snapshot import get_character_roster_preview, UNDO
from routers import character_roster as api
from tests.test_character_roster import SOURCE1, A, entry, decision, proposal
from tests.test_project_scope_failures import get_fixture, get_bytes
from utils import file_lock


def get_proposal():
    return proposal(SOURCE1, [entry(SOURCE1, A, 'ALICE', 'Alice')], [decision('add', [], [A])])


def save_fixture(root):
    get_fixture(root); (root / 'source.txt').write_text(SOURCE1, encoding='utf-8')
    save_cast_list(get_cast_list_path(str(root / 'source.txt'), str(root)), [{'name': 'OLD NAME', 'aliases': []}])


@contextmanager
def admission(root):
    with patch.object(api.core, 'DATA_DIR', str(root)), patch.object(api.core, 'process_state', {}), ensure_character_roster_admission(root):
        yield


def apply_operation(root, mode):
    if mode == 'apply':
        value = get_proposal()
        apply_character_roster(root, value, get_character_roster_preview(root, value)['revision'])
    else:
        apply_character_roster_undo(root, get_character_roster_undo_status(root)['revision'])


def run_crash_child(root, mode, boundary):
    with admission(root):
        if boundary == 'committed':
            transaction._finish_transaction = lambda *args: os._exit(73)
        else:
            real = transaction._move; count = 0
            def crash_after_move(*args):
                nonlocal count
                result = real(*args); count += 1
                if count == int(boundary):
                    os._exit(73)
                return result
            transaction._move = crash_after_move
        apply_operation(root, mode)
    raise AssertionError('Requested roster crash boundary was not reached')


def hold_foreign_owner(root, mode, ready):
    if mode == 'task':
        from task_ownership import acquire_task_lease
        guard = acquire_task_lease(root, 'fixture', set())
    elif mode == 'cast':
        guard = file_lock(get_cast_list_path(str(root / 'source.txt'), str(root)), timeout=0)
    else:
        guard = ensure_book_state(root, timeout=0)
    if mode == 'task':
        try:
            ready.write_text('owned'); sys.stdin.readline()
        finally:
            guard.close()
    else:
        with guard:
            ready.write_text('owned'); sys.stdin.readline()


class CharacterRosterFailureTests(unittest.TestCase):
    def test_abrupt_process_death_rolls_back_precommit_and_keeps_committed_apply_and_undo(self):
        for mode in ('apply', 'undo'):
            for boundary in ('1', '2', '3', '4', 'committed'):
                with self.subTest(mode=mode, boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve(); save_fixture(root)
                    with admission(root):
                        if mode == 'undo': apply_operation(root, 'apply')
                        before = get_bytes(root)
                    command = [sys.executable, '-c',
                        'from pathlib import Path; import sys; from tests.test_character_roster_failures import run_crash_child; '
                        'run_crash_child(Path(sys.argv[1]),sys.argv[2],sys.argv[3])', str(root), mode, boundary]
                    child = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                           capture_output=True, text=True, timeout=15)
                    self.assertEqual(73, child.returncode, child.stdout + child.stderr)
                    self.assertTrue((root / transaction.JOURNAL).exists())
                    published = get_bytes(root)
                    if boundary == 'committed':
                        self.assertEqual('committed', json.loads((root / transaction.JOURNAL).read_bytes())['phase'])
                        self.assertEqual(mode == 'apply', (root / UNDO).exists())
                        roster = list((root / 'cast_lists').glob('*.roster.json'))
                        self.assertEqual(1 if mode == 'apply' else 0, len(roster))
                        if roster:
                            self.assertEqual(A, json.loads(roster[0].read_bytes())['roster']['entries'][0]['id'])
                        for name, raw in before.items():
                            if not name.startswith('cast_lists/') and name != UNDO:
                                self.assertEqual(raw, published[name])
                    with ensure_book_state(root): pass
                    self.assertEqual(published if boundary == 'committed' else before, get_bytes(root))
                    self.assertFalse((root / transaction.JOURNAL).exists())
                    self.assertEqual([], list(root.glob('.book-switch-*')))
                    with admission(root):
                        status = get_character_roster_undo_status(root)
                        self.assertEqual(mode == 'apply' if boundary == 'committed' else mode == 'undo', status['can_undo'])

    def test_http_apply_refuses_real_foreign_task_book_and_cast_owners_then_succeeds(self):
        for mode in ('task', 'book', 'cast'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve() / 'data'; root.mkdir(); save_fixture(root)
                ready = root.parent / 'owner-ready'
                application = FastAPI(); application.include_router(api.router)
                with patch.object(api.core, 'DATA_DIR', str(root)), patch.object(api.core, 'process_state', {}), TestClient(application) as client:
                    meta = client.get('/api/character_roster').json()
                    request = {'context_revision': meta['context_revision'], 'proposal': get_proposal(),
                               'imported_roster': None, 'import_id_mapping': None}
                    preview = client.post('/api/character_roster/preview', json=request)
                    self.assertEqual(200, preview.status_code, preview.text)
                    request['expected_revision'] = preview.json()['revision']
                    code = ('from pathlib import Path; import sys; from tests.test_character_roster_failures import hold_foreign_owner; '
                            'hold_foreign_owner(Path(sys.argv[1]),sys.argv[2],Path(sys.argv[3]))')
                    child = subprocess.Popen([sys.executable, '-c', code, str(root), mode, str(ready)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        cwd=Path(__file__).resolve().parents[1])
                    try:
                        deadline = time.monotonic() + 10
                        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertTrue(ready.exists(), 'Foreign owner failed to acquire the lock')
                        before = get_bytes(root)
                        refused = client.post('/api/character_roster/apply', json=request)
                        self.assertEqual(409, refused.status_code, refused.text)
                        self.assertEqual(before, get_bytes(root))
                    finally:
                        try:
                            stdout, stderr = child.communicate('\n', timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill(); child.communicate(timeout=5); raise
                    self.assertEqual(0, child.returncode, stdout + stderr)
                    approved = client.post('/api/character_roster/apply', json=request)
                    self.assertEqual(200, approved.status_code, approved.text)
                    undo = client.get('/api/character_roster/undo').json()
                    self.assertTrue(undo['can_undo'])
                    self.assertEqual(200, client.post('/api/character_roster/undo', json={'expected_revision': undo['revision']}).status_code)
                    self.assertEqual(before, get_bytes(root))
