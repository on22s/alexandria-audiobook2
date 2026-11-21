"""Roster approval, Retry and Start Over under one book admission."""
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import book_state_transaction as books
from cast_list import get_cast_list_path
from character_roster_apply import (ensure_character_roster_admission, apply_character_roster,
    apply_character_roster_undo, get_character_roster_undo_status)
from character_roster_snapshot import get_character_roster_preview
from tests import test_script_cast_routes as generation
from tests.test_character_roster import SOURCE1, A, entry, decision, proposal
from tests.test_support import intercept_book_publications
from utils import atomic_json_write


class RecoveryFenceRaceTests(generation.CastRoutesTest):
    def _prepare(self):
        root = Path(self.dir)
        Path(self.source).write_text(SOURCE1, encoding='utf-8')
        self.list_path = get_cast_list_path(self.source, self.dir)
        self.write_list([{'name': 'ALICE', 'aliases': []}])
        self.start()
        module = generation.script_module
        atomic_json_write({'status': 'failed', 'fingerprint': 'fp'},
                          module.three_pass_manifest_path(module.SCRIPT_PATH))
        atomic_json_write({'stage': 'instruct', 'chunks_done': 1, 'segmented': [],
                           'named': [], 'annotated': [], 'resolutions': ['clean']},
                          module.three_pass_checkpoint_path(module.SCRIPT_PATH))
        return root

    def _changed_proposal(self):
        return proposal(SOURCE1,
                        [entry(SOURCE1, A, 'ALICE', 'Alice', aliases=['ALLY'])],
                        [decision('add', [], [A])])

    def _artifacts(self):
        module = generation.script_module
        paths = [Path(self.dir) / 'state.json', Path(module.get_run_cast_path()),
                 Path(module.three_pass_checkpoint_path(module.SCRIPT_PATH)),
                 Path(module.three_pass_manifest_path(module.SCRIPT_PATH))]
        return {str(path): path.read_bytes() if path.exists() else None for path in paths}

    def _admit(self):
        return patch.object(generation.core, 'DATA_DIR', self.dir), \
               patch.object(generation.core, 'process_state', {})

    def _approve(self, root, value, *, changed=True):
        with self._admit()[0], self._admit()[1]:
            with ensure_character_roster_admission(root):
                plan = get_character_roster_preview(root, value)
                self.assertEqual(changed, plan['cast_changed'])
                apply_character_roster(root, value, plan['revision'])

    def _undo(self, root):
        with self._admit()[0], self._admit()[1]:
            with ensure_character_roster_admission(root):
                revision = get_character_roster_undo_status(root)['revision']
                apply_character_roster_undo(root, revision)

    def _start_over(self):
        return generation.script_module.start_script_generation(
            generation.script_module.BackgroundTasks(), self.source,
            generation.script_module.GenerateScriptRequest(start_over=True))

    def test_approval_before_admission_blocks_retry_and_keeps_evidence(self):
        root = self._prepare()
        frozen = Path(generation.script_module.get_run_cast_path())
        original_cast = frozen.read_bytes()
        observed = {}
        original_ensure = generation.script_module.ensure_book_state
        approved = False

        def approve_before_admission(*args, **kwargs):
            nonlocal approved
            if not approved:
                approved = True
                self._approve(root, self._changed_proposal())
                observed['bytes'] = self._artifacts()
            return original_ensure(*args, **kwargs)

        self.scheduled.clear()
        with patch.object(generation.script_module, 'ensure_book_state',
                          side_effect=approve_before_admission):
            with self.assertRaises(HTTPException) as caught:
                self.start(recovery=True)
        self.assertEqual(409, caught.exception.status_code)
        self.assertIn('approved cast changed', caught.exception.detail)
        self.assertEqual([], self.scheduled)
        self.assertEqual(observed['bytes'], self._artifacts())
        self.assertEqual(original_cast, frozen.read_bytes())
        self.assertIsNotNone(observed['bytes'][str(frozen)])
        approved_cast = Path(self.list_path).read_bytes()
        self._start_over()
        self.assertEqual(approved_cast, frozen.read_bytes())

    def test_undo_then_ordinary_generate_keeps_fence_until_start_over(self):
        root = self._prepare()
        self._approve(root, self._changed_proposal())
        self._undo(root)
        self.start()
        with self.assertRaises(HTTPException) as caught:
            self.start(recovery=True)
        self.assertEqual(409, caught.exception.status_code)
        self.assertIn('approved cast changed', caught.exception.detail)
        self._start_over()
        self.start(recovery=True)

    def test_undo_after_newer_failed_run_refences_retry(self):
        root = self._prepare()
        self._approve(root, self._changed_proposal())
        self._start_over()
        self._prepare_manifest_only()
        self._undo(root)
        with self.assertRaises(HTTPException) as caught:
            self.start(recovery=True)
        self.assertEqual(409, caught.exception.status_code)
        self.assertIn('approved cast changed', caught.exception.detail)

    def _prepare_manifest_only(self):
        module = generation.script_module
        atomic_json_write({'status': 'failed', 'fingerprint': 'fp'},
                          module.three_pass_manifest_path(module.SCRIPT_PATH))

    def test_identical_cast_approval_keeps_retry_and_run_evidence(self):
        root = self._prepare()
        value = proposal(SOURCE1, [entry(SOURCE1, A, 'ALICE', 'Alice')],
                         [decision('add', [], [A])])
        artifacts = self._artifacts()
        self._approve(root, value, changed=False)
        self.start(recovery=True)
        after = self._artifacts()
        for name, raw in artifacts.items():
            if name.endswith('state.json'):
                continue
            self.assertEqual(raw, after[name], name)

    def test_injected_rollback_keeps_artifacts_and_retry(self):
        root = self._prepare()
        artifacts = self._artifacts()
        with self._admit()[0], self._admit()[1]:
            with ensure_character_roster_admission(root):
                plan = get_character_roster_preview(root, self._changed_proposal())
                def fail(target):
                    raise OSError('synthetic roster publication failure')
                with intercept_book_publications(books, before=fail):
                    with self.assertRaises(OSError):
                        apply_character_roster(root, self._changed_proposal(), plan['revision'])
        self.assertEqual(artifacts, self._artifacts())
        self.start(recovery=True)
