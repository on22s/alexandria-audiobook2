import asyncio
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException
import core
from routers import voices


class PersonaRecoveryClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'annotated_script.json'
        self.config = self.root / 'voice_config.json'
        self.script.write_text('[{"speaker":"Hero","text":"Hello."}]')
        self.config.write_text('{"Hero":{"type":"custom","voice":"Ryan"}}')
        self.state = copy.deepcopy(core.process_state)
        for row in self.state.values():
            row['running'] = False
        for owner, name, value in [(core, 'process_state', self.state),
                                   (voices, 'process_state', self.state),
                                   (core, '_task_claims', {}), (core, '_gpu_leases', {}),
                                   (core, 'DATA_DIR', str(self.root)),
                                   (voices, 'SCRIPT_PATH', str(self.script)),
                                   (voices, 'VOICE_CONFIG_PATH', str(self.config))]:
            context = patch.object(owner, name, value)
            context.start(); self.addCleanup(context.stop)
        for context in (patch.object(core, 'acquire_gpu_lock', return_value=None),
                        patch.object(core, 'llm_is_on_this_gpu', return_value=True)):
            context.start(); self.addCleanup(context.stop)
        self.addCleanup(self.release_claims)

    def release_claims(self):
        for task, owner in list(core._task_claims.items()):
            core.release_gpu_task_claim(task, owner['id'])

    def request(self, resume=True):
        return voices.PersonaRecoveryRequest(speaker='Hero', resume=resume,
                      persona_json='{"description":"warm and measured","ref_text":"Hello there."}')

    def test_resume_reserves_before_the_awaited_write_and_adopts_the_same_owner(self):
        arrived, release = threading.Event(), threading.Event()
        original = voices.atomic_json_write
        tasks = BackgroundTasks()
        calls = []

        def save(*args, **kwargs):
            arrived.set()
            if not release.wait(5):
                raise TimeoutError('save not released')
            return original(*args, **kwargs)

        async def run():
            recovery = asyncio.create_task(voices.recover_persona(tasks, self.request()))
            self.assertTrue(await asyncio.to_thread(arrived.wait, 3))
            other = None
            try:
                owner = core._task_claims.get('persona')
                self.assertIsNotNone(owner, 'persona is not reserved while its save is running')
                token = owner['id']
                with self.assertRaises(HTTPException):
                    other = core.claim_gpu_task('audio')
            finally:
                if other is not None:
                    core.release_gpu_task_claim('audio', other)
                release.set()
                result = await recovery
            self.assertEqual('resuming', result['status'])
            self.assertEqual(token, core._task_claims['persona']['id'])
            self.assertEqual(1, len(tasks.tasks))
            await tasks()
            self.assertFalse(self.state['persona']['running'])
            self.assertNotIn('persona', core._task_claims)

        with patch.object(voices, 'atomic_json_write', side_effect=save), \
             patch.object(voices, 'run_process', side_effect=lambda *args: calls.append(args)):
            asyncio.run(run())
        self.assertEqual(1, len(calls))
        self.assertIn('--recovered-speaker', calls[0][0])
        self.assertEqual('warm and measured', json.loads(self.config.read_text())['Hero']['description'])

    def test_save_and_registration_failure_release_only_the_pending_reservation(self):
        class RejectTasks(BackgroundTasks):
            def add_task(self, *args, **kwargs):
                raise RuntimeError('registration failed')

        for mode in ('save', 'register'):
            with self.subTest(mode=mode):
                self.config.write_text('{"Hero":{"type":"custom","voice":"Ryan"}}')
                before = self.config.read_bytes()
                tasks = RejectTasks() if mode == 'register' else BackgroundTasks()
                original = voices.atomic_json_write
                with patch.object(voices, 'atomic_json_write',
                                  side_effect=OSError('disk full') if mode == 'save' else original):
                    with self.assertRaises((OSError, RuntimeError)):
                        asyncio.run(voices.recover_persona(tasks, self.request()))
                self.assertFalse(self.state['persona']['running'])
                self.assertNotIn('persona', core._task_claims)
                self.assertEqual([], tasks.tasks)
                if mode == 'save':
                    self.assertEqual(before, self.config.read_bytes())
                new = core.claim_gpu_task('audio')
                self.assertTrue(core.release_gpu_task_claim('audio', new))

    def test_cpu_only_recovery_remains_available_during_another_gpu_task(self):
        owner = core.claim_gpu_task('audio')
        tasks = BackgroundTasks()
        result = asyncio.run(voices.recover_persona(tasks, self.request(resume=False)))
        self.assertEqual('saved', result['status'])
        self.assertEqual([], tasks.tasks)
        self.assertEqual(owner, core._task_claims['audio']['id'])
        self.assertNotIn('persona', core._task_claims)
        self.assertTrue(core.release_gpu_task_claim('audio', owner))

    def test_cancelled_request_cannot_register_work_or_release_a_replacement_owner(self):
        arrived, release, saved = threading.Event(), threading.Event(), threading.Event()
        original = voices.atomic_json_write
        tasks = BackgroundTasks()

        def save(*args, **kwargs):
            arrived.set()
            if not release.wait(5):
                raise TimeoutError('save not released')
            try:
                return original(*args, **kwargs)
            finally:
                saved.set()

        async def run():
            recovery = asyncio.create_task(voices.recover_persona(tasks, self.request()))
            self.assertTrue(await asyncio.to_thread(arrived.wait, 3))
            try:
                recovery.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await recovery
                self.assertNotIn('persona', core._task_claims)
                replacement = core.claim_gpu_task('persona')
            finally:
                release.set()
            self.assertTrue(await asyncio.to_thread(saved.wait, 3))
            self.assertEqual(replacement, core._task_claims['persona']['id'])
            self.assertEqual([], tasks.tasks)
            self.assertTrue(core.release_gpu_task_claim('persona', replacement))

        with patch.object(voices, 'atomic_json_write', side_effect=save):
            asyncio.run(run())
