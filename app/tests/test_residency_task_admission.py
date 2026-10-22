"""GPU admission releases idle app residents under existing owner leases."""
import copy
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
import core
from project import ProjectManager
import tts


class ResidencyTaskAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = ProjectManager(self.tmp.name)
        self.engine = tts.TTSEngine({'tts': {}})
        self.manager.engine = self.engine
        self.engine._local_custom_model = object()
        self.engine._local_model_devices['custom'] = 'cpu'
        self.states = copy.deepcopy(core.process_state)
        for state in self.states.values():
            state['running'] = False
        for name, value in [('DATA_DIR', self.tmp.name), ('process_state', self.states),
                            ('_task_claims', {}), ('_gpu_leases', {}),
                            ('project_manager', self.manager), ('_reap_gpu_leases', Mock()),
                            ('llm_is_on_this_gpu', Mock(return_value=True)),
                            ('acquire_gpu_lock', Mock(return_value=object())),
                            ('release_gpu_lock', Mock())]:
            context = patch.object(core, name, value)
            context.start()
            self.addCleanup(context.stop)
        self.addCleanup(self.release_claims)

    def release_claims(self):
        for name, owner in list(core._task_claims.items()):
            core.release_gpu_task_claim(name, owner['id'])

    def test_gpu_child_admission_releases_idle_resident_after_host_acquisition(self):
        original = self.manager.ensure_idle_local_models_released
        def release():
            core.acquire_gpu_lock.assert_called_once_with()
            return original()
        with patch.object(self.manager, 'ensure_idle_local_models_released', side_effect=release):
            claim = core.claim_gpu_task('voicelab')
        self.assertIsNone(self.engine._local_custom_model)
        self.assertTrue(core.is_task_running('voicelab'))
        self.assertIsNotNone(core._task_claims['voicelab']['lease'])
        self.assertTrue(core.release_gpu_task_claim('voicelab', claim))

    def test_cpu_audio_and_remote_llm_admission_preserve_warm_resident(self):
        for name, cpu_only, local_llm in [('voicelab', True, True),
                                         ('audio', False, True), ('review', False, False)]:
            with self.subTest(task=name), patch.object(core, 'llm_is_on_this_gpu', return_value=local_llm):
                claim = core.claim_gpu_task(name, cpu_only=cpu_only)
                self.assertIsNotNone(self.engine._local_custom_model)
                core.release_gpu_task_claim(name, claim)

    def test_active_engine_refuses_child_and_releases_admission_handles(self):
        entered, release = threading.Event(), threading.Event()
        @tts.ensure_local_tts_serialized
        def work(owner):
            entered.set()
            release.wait(5)
        worker = threading.Thread(target=work, args=(self.engine,))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(HTTPException) as caught:
                core.claim_gpu_task('voicelab')
            self.assertEqual(409, caught.exception.status_code)
            self.assertIsNotNone(self.engine._local_custom_model)
            self.assertEqual({}, core._task_claims)
            self.assertEqual({}, core._gpu_leases)
            self.assertFalse(core.is_task_running('voicelab'))
            core.release_gpu_lock.assert_called_once_with(core.acquire_gpu_lock.return_value)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        claim = core.claim_gpu_task('voicelab')
        self.assertIsNone(self.engine._local_custom_model)
        self.assertTrue(core.release_gpu_task_claim('voicelab', claim))

    def test_partial_allocator_cleanup_failure_refuses_each_admission_until_fixed(self):
        with patch.object(self.engine, '_clear_gpu_cache', side_effect=RuntimeError('allocator cleanup failed')):
            for _ in range(2):
                with self.assertRaises(HTTPException):
                    core.claim_gpu_task('voicelab')
                self.assertEqual({}, core._task_claims)
                self.assertEqual({}, core._gpu_leases)
                self.assertTrue(self.engine.get_model_residency()['cleanup_pending'])
        self.assertIsNone(self.engine._local_custom_model)
        with patch.object(self.engine, '_clear_gpu_cache') as cleanup:
            claim = core.claim_gpu_task('voicelab')
        cleanup.assert_called_once_with()
        self.assertTrue(core.release_gpu_task_claim('voicelab', claim))

    def test_sync_failure_preserves_resident_and_releases_claim(self):
        self.engine._local_model_devices['custom'] = 'cuda:0'
        with patch('torch.cuda.synchronize', side_effect=RuntimeError('fixture sync failed')):
            with self.assertRaises(HTTPException) as caught:
                core.claim_gpu_task('voicelab')
        self.assertEqual(400, caught.exception.status_code)
        self.assertIsNotNone(self.engine._local_custom_model)
        self.assertEqual({}, core._task_claims)
        self.assertEqual({}, core._gpu_leases)
        core.release_gpu_lock.assert_called_once()
        with patch('torch.cuda.synchronize'):
            claim = core.claim_gpu_task('voicelab')
        self.assertTrue(core.release_gpu_task_claim('voicelab', claim))

    def test_manager_reads_do_not_create_engine_and_busy_construction_refuses_release(self):
        self.manager.engine = None
        with patch.object(self.manager, 'get_engine', side_effect=AssertionError('must not initialize')):
            self.assertEqual('not_initialized', self.manager.get_model_residency()['state'])
            self.assertTrue(self.manager.ensure_idle_local_models_released())
            with self.manager._engine_lock:
                self.assertEqual('busy', self.manager.get_model_residency()['state'])
                self.assertFalse(self.manager.ensure_idle_local_models_released())
        self.assertIsNone(self.manager.engine)
