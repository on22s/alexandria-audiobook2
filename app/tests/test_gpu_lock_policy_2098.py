"""One GPU-lock policy per admission (issue #2098).

claim_gpu_task used to decide "is the LLM on this GPU?" three times: the
conflict set and the pre-check read config.json, while needs_local_gpu used
the passed llm_config. A disagreeing config could therefore exclude `audio`
from the conflicts and still take the local GPU lock. These tests assert the
conflict set and the lock decision, not just that the claim returned.
"""
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import core


class GpuLockPolicy2098Test(unittest.TestCase):
    TASK = 'script'

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(core.process_state, {
            self.TASK: {'running': False, 'logs': []},
        }))
        stack.enter_context(patch.object(core, '_task_claims', {}))
        stack.enter_context(patch.object(core, '_gpu_leases', {}))
        # No reaper thread: the test owns the lifetime of the fake lease.
        stack.enter_context(patch.object(core.threading, 'Thread', MagicMock()))
        self.running = []
        stack.enter_context(patch.object(
            core, 'is_task_running',
            side_effect=lambda name, states=None: name in self.running))
        self.lease = MagicMock()
        self.lease_call = stack.enter_context(patch.object(
            core, 'acquire_task_lease', return_value=self.lease))
        self.gpu_handle = object()
        self.gpu_lock = stack.enter_context(patch.object(
            core, 'acquire_gpu_lock', return_value=self.gpu_handle))
        stack.enter_context(patch.object(core, 'release_gpu_lock'))
        stack.enter_context(patch.object(
            core.project_manager, 'ensure_idle_local_models_released', return_value=True))
        stack.enter_context(patch.object(core, 'record_failure'))

    def _claim(self, **kwargs):
        """Claim while recording the policy handed to each lock decision."""
        check = MagicMock(wraps=core.check_global_gpu_lock)
        conflicts = MagicMock(wraps=core.gpu_lock_conflicts)
        with patch.object(core, 'check_global_gpu_lock', check), \
                patch.object(core, 'gpu_lock_conflicts', conflicts):
            claim_id = core.claim_gpu_task(self.TASK, **kwargs)
        self.assertTrue(claim_id)
        return check, conflicts

    def _assert_threaded_policy(self, check, conflicts, expected):
        self.assertEqual(expected, check.call_args.kwargs.get('llm_on_gpu'))
        self.assertTrue(conflicts.call_args_list)
        for call in conflicts.call_args_list:
            self.assertEqual(expected, call.kwargs.get('llm_on_gpu'))

    def _assert_claimed_conflicts(self, on_gpu):
        """Assert the real conflict set passed to the task lease."""
        claimed = self.lease_call.call_args.args[2]
        expected = core.GPU_TASKS - {self.TASK}
        if not on_gpu:
            expected &= core.LLM_TASKS
        self.assertEqual(expected, claimed)
        self.assertEqual('audio' in claimed, on_gpu)
        self.assertIn('review', claimed)

    def _assert_lock_decision(self, on_gpu, running, should_raise):
        """Exercise check_global_gpu_lock with the policy the claim resolved."""
        self.running[:] = running
        try:
            core.check_global_gpu_lock(self.TASK, llm_on_gpu=on_gpu)
        except core.HTTPException as error:
            self.assertTrue(should_raise, f'unexpected lock refusal: {error}')
            self.assertEqual(400, error.status_code)
        else:
            self.assertFalse(should_raise, 'a conflicting running task was admitted')

    def test_passed_local_config_governs_conflicts_and_device(self):
        config = {'llm_mode': 'local'}
        with patch.object(core, 'llm_is_on_this_gpu', return_value=False) as disk, \
                patch.object(core, 'is_llm_on_this_gpu', return_value=True) as passed:
            check, conflicts = self._claim(llm_config=config)
        disk.assert_not_called()
        passed.assert_called_once_with(config)
        self._assert_threaded_policy(check, conflicts, True)
        self._assert_claimed_conflicts(True)
        self.assertTrue(self.gpu_lock.called)
        self._assert_lock_decision(True, ['audio'], should_raise=True)
        self._assert_lock_decision(True, [], should_raise=False)

    def test_passed_remote_config_governs_conflicts_and_device(self):
        config = {'llm_mode': 'remote'}
        with patch.object(core, 'llm_is_on_this_gpu', return_value=True) as disk, \
                patch.object(core, 'is_llm_on_this_gpu', return_value=False) as passed:
            check, conflicts = self._claim(llm_config=config)
        disk.assert_not_called()
        passed.assert_called_once_with(config)
        self._assert_threaded_policy(check, conflicts, False)
        self._assert_claimed_conflicts(False)
        self.assertFalse(self.gpu_lock.called)
        self._assert_lock_decision(False, ['audio'], should_raise=False)
        self._assert_lock_decision(False, ['review'], should_raise=True)

    def test_rewritten_config_is_read_once_per_admission(self):
        values = iter([True, False, False, False])
        with patch.object(core, 'llm_is_on_this_gpu',
                          side_effect=lambda: next(values)) as disk, \
                patch.object(core, 'is_llm_on_this_gpu',
                             side_effect=AssertionError('no config was passed')) as passed:
            check, conflicts = self._claim()
        disk.assert_called_once_with()
        passed.assert_not_called()
        self._assert_threaded_policy(check, conflicts, True)
        self._assert_claimed_conflicts(True)
        self.assertTrue(self.gpu_lock.called)
        self._assert_lock_decision(True, ['audio'], should_raise=True)

    def test_no_passed_config_keeps_the_original_outcome(self):
        with patch.object(core, 'llm_is_on_this_gpu', return_value=True) as disk, \
                patch.object(core, 'is_llm_on_this_gpu') as passed:
            self._claim()
        disk.assert_called()
        passed.assert_not_called()
        self._assert_claimed_conflicts(True)
        self.assertTrue(self.gpu_lock.called)
        # The original call shape still refuses the same conflicting task.
        self.running[:] = ['audio']
        with self.assertRaises(core.HTTPException) as caught:
            core.check_global_gpu_lock(self.TASK)
        self.assertEqual(400, caught.exception.status_code)

    def test_config_read_error_still_applies_the_full_lock(self):
        with patch.object(core, 'load_app_config', side_effect=OSError('unreadable')), \
                patch.object(core, 'record_failure') as failure, \
                patch.object(core, 'is_llm_on_this_gpu') as passed:
            check, conflicts = self._claim()
        self.assertFalse(passed.called)
        self.assertTrue(failure.called)
        self._assert_threaded_policy(check, conflicts, True)
        self._assert_claimed_conflicts(True)
        self.assertTrue(self.gpu_lock.called)
        self._assert_lock_decision(True, ['audio'], should_raise=True)


if __name__ == '__main__':
    unittest.main()
