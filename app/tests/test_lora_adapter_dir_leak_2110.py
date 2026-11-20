"""Regression tests for #2110: a failed GPU claim must not leave the adapter
directory this request reserved under the adapter-naming lock behind.

Every case drives the real async route with a real temporary models directory
and a real dataset directory, and asserts on the real filesystem.
"""

import asyncio
import copy
from contextlib import ExitStack
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException

import core
import review_startup
from routers import lora


class LoraAdapterDirLeak2110Tests(unittest.TestCase):
    """The directory is reserved before the claim; a failed claim must undo it."""

    def _fixture(self, stack):
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='aa2-2110-')))
        datasets = root / 'datasets'
        (datasets / 'book').mkdir(parents=True)
        models = root / 'models'
        manifest = models / 'manifest.json'
        for name, value in (('LORA_DATASETS_DIR', str(datasets)),
                            ('LORA_MODELS_DIR', str(models)),
                            ('LORA_MODELS_MANIFEST', str(manifest)),
                            ('project_manager', SimpleNamespace(engine=None))):
            stack.enter_context(patch.object(lora, name, value))
        stack.enter_context(patch.object(lora, 'check_global_gpu_lock'))
        stack.enter_context(patch.object(lora, 'get_unique_id',
                                         return_value='new_adapter'))
        state = copy.deepcopy(core.process_state)
        for entry in state.values():
            entry['running'] = False
        stack.enter_context(patch.object(lora, 'process_state', state))
        return root, models, manifest, state

    def _start(self):
        return asyncio.run(lora.lora_start_training(
            lora.LoraTrainingRequest(name='Voice', dataset_id='book'),
            BackgroundTasks()))

    def test_claim_failure_400_removes_reserved_adapter_dir(self):
        """The exact HTTPException from the failed claim reaches the caller."""
        with ExitStack() as stack:
            _root, models, _manifest, _state = self._fixture(stack)
            error = HTTPException(status_code=400, detail='GPU busy')
            schedule = stack.enter_context(patch.object(
                lora, 'schedule_claimed_background_task', side_effect=error))
            with self.assertRaises(HTTPException) as raised:
                self._start()
            self.assertIs(error, raised.exception)
            self.assertEqual(400, raised.exception.status_code)
            schedule.assert_called_once()
            self.assertFalse((models / 'new_adapter').exists())
            self.assertTrue(models.is_dir(), 'the models root itself is not a leak')

    def test_claim_failure_409_removes_reserved_adapter_dir(self):
        """409 conflicts roll back the directory just like 400 refusals."""
        with ExitStack() as stack:
            _root, models, _manifest, _state = self._fixture(stack)
            error = HTTPException(status_code=409, detail='Task reservation is no longer active')
            stack.enter_context(patch.object(
                lora, 'schedule_claimed_background_task', side_effect=error))
            with self.assertRaises(HTTPException) as raised:
                self._start()
            self.assertIs(error, raised.exception)
            self.assertEqual(409, raised.exception.status_code)
            self.assertFalse((models / 'new_adapter').exists())

    def test_worker_registration_failure_after_claim_removes_reserved_adapter_dir(self):
        """A claim that succeeds but whose worker registration fails also cleans up."""
        with ExitStack() as stack:
            _root, models, _manifest, state = self._fixture(stack)
            # Let core's real schedule_claimed_background_task run: claim first,
            # then registration fails exactly as register_claimed_background_task
            # does when the reservation is no longer active (with its rollback).
            claims = stack.enter_context(patch.object(core, '_task_claims', {}))
            stack.enter_context(patch.object(core, '_gpu_leases', {}))
            stack.enter_context(patch.object(core, 'process_state', state))

            def fake_claim(task_name, **_kwargs):
                claims[task_name] = {'id': 'claim-2110', 'phase': 'claimed',
                                     'lease': None, 'cpu_only': True}
                state[task_name]['running'] = True
                return 'claim-2110'

            stack.enter_context(patch.object(core, 'claim_gpu_task',
                                             side_effect=fake_claim))

            def reject_registration(*_args, **_kwargs):
                core.release_gpu_task_claim('lora_training', 'claim-2110',
                                            pending_only=True)
                raise HTTPException(status_code=409,
                                    detail='Task reservation is no longer active')

            stack.enter_context(patch.object(core, 'register_claimed_background_task',
                                             side_effect=reject_registration))
            with self.assertRaises(HTTPException) as raised:
                self._start()
            self.assertEqual(409, raised.exception.status_code)
            self.assertFalse((models / 'new_adapter').exists())
            self.assertNotIn('lora_training', claims, 'registration rolled its claim back')
            self.assertFalse(state['lora_training']['running'])

    def test_successful_claim_keeps_the_dir_and_schedules_the_worker(self):
        """Once the claim is owned, nothing about the existing happy path changes."""
        with ExitStack() as stack:
            _root, models, _manifest, _state = self._fixture(stack)
            schedule = stack.enter_context(patch.object(
                lora, 'schedule_claimed_background_task', return_value='claim-2110'))
            response = self._start()
            self.assertEqual('started', response['status'])
            self.assertEqual('new_adapter', response['adapter_id'])
            self.assertTrue((models / 'new_adapter').is_dir())
            schedule.assert_called_once()
            args = schedule.call_args.args
            self.assertEqual('lora_training', args[1])
            self.assertTrue(callable(args[2]))

    def test_cleanup_failure_keeps_original_error_and_records_failure(self):
        """A broken cleanup is diagnosed but never replaces the claim error."""
        with ExitStack() as stack:
            _root, models, _manifest, _state = self._fixture(stack)
            original = HTTPException(status_code=409, detail='gpu claim rejected')
            stack.enter_context(patch.object(
                lora, 'schedule_claimed_background_task', side_effect=original))
            cleanup_error = OSError('remove denied')
            stack.enter_context(patch.object(lora.os, 'rmdir', side_effect=cleanup_error))
            record = stack.enter_context(patch.object(review_startup, 'record_failure'))
            with self.assertRaises(HTTPException) as raised:
                self._start()
            self.assertIs(original, raised.exception)
            self.assertEqual(409, raised.exception.status_code)
            record.assert_called_once()
            args, kwargs = record.call_args
            self.assertEqual('lora_adapter_dir_cleanup_failed', args[0])
            self.assertIs(cleanup_error, args[1])
            self.assertEqual('lora_training', kwargs['task'])
            self.assertEqual('remove_reserved_adapter_dir', kwargs['operation'])
            self.assertEqual(str(models / 'new_adapter'), kwargs['resource'])

    def test_unrelated_adapter_dir_is_never_touched(self):
        """Only the empty directory this call created may be removed."""
        with ExitStack() as stack:
            _root, models, _manifest, _state = self._fixture(stack)
            models.mkdir()
            unrelated = models / 'other_adapter'
            unrelated.mkdir()
            sentinel = unrelated / 'training_meta.json'
            sentinel.write_text('{"id": "other"}')
            stack.enter_context(patch.object(
                lora, 'schedule_claimed_background_task',
                side_effect=HTTPException(400, 'GPU busy')))
            with self.assertRaises(HTTPException) as raised:
                self._start()
            self.assertEqual(400, raised.exception.status_code)
            self.assertTrue(unrelated.is_dir())
            self.assertEqual('{"id": "other"}', sentinel.read_text())
            self.assertFalse((models / 'new_adapter').exists())
            self.assertEqual(['other_adapter'],
                             sorted(p.name for p in models.iterdir() if p.is_dir()))


if __name__ == '__main__':
    unittest.main()
