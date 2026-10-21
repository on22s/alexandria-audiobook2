"""Actual owned Editor callbacks fence late synthesis without GPU/model work."""
import asyncio
import contextlib
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
from fastapi import BackgroundTasks, HTTPException
import core
from project import ProjectManager
from routers import editor
from tests.test_support import use_private_cpu_fixture_lock
from audio_generation_owner import (AudioGenerationCancelled, get_audio_generation_owner,
                                    ensure_audio_generation_publication)


class AudioGenerationCancellationTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, count=2):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            (root/'annotated_script.json').write_text('[]')
            (root/'state.json').write_text(json.dumps({'active_book_id': 'synthetic-cancel'}))
            config = root/'config.json'
            config.write_text(json.dumps({'tts': {'mode': 'local', 'parallel_workers': 1}}))
            manager = ProjectManager(tmp)
            (root/'voice_config.json').write_text(json.dumps({'NARRATOR': {'type': 'custom', 'voice': 'Ryan'}}))
            rows = []
            for i in range(count):
                sf.write(root/f'prior-{i}.wav', np.full(2400, .1), 24000)
                rows.append(dict(id=i, uid=f'line-{i}', speaker='NARRATOR', text=f'Synthetic line {i}.',
                    instruct='', status='done', audio_path=f'prior-{i}.wav', audio_revision=f'prior-revision-{i}', error=None))
            manager.save_chunks(rows); rows = copy.deepcopy(manager.load_chunks())
            state = copy.deepcopy(core.process_state)
            for entry in state.values(): entry['running'] = False
            for module, name, value in (
                (core, 'process_state', state), (editor, 'process_state', state),
                (core, '_task_claims', {}), (core, '_gpu_leases', {}), (core, 'DATA_DIR', tmp),
                (editor, 'DATA_DIR', tmp), (editor, 'SCRIPT_PATH', str(root/'annotated_script.json')),
                (editor, 'CONFIG_PATH', str(config)), (editor, 'project_manager', manager)):
                stack.enter_context(patch.object(module, name, value))
            stack.enter_context(use_private_cpu_fixture_lock())
            stack.enter_context(patch('project._export_audio_segment', side_effect=RuntimeError('force real WAV fallback')))
            try:
                yield root, manager, rows, state
            finally:
                for name, claim in list(core._task_claims.items()): core.release_gpu_task_claim(name, claim['id'])

    def write_audio(self, path):
        sf.write(path, np.full(2400, .2), 24000)

    async def start(self, mode, tasks, indices):
        if mode == 'single':
            return await editor.generate_chunk_endpoint(indices[0], tasks, expected_uid=f'line-{indices[0]}')
        request = editor.BatchGenerateRequest(indices=indices, expected_uids=[f'line-{i}' for i in indices])
        route = editor.generate_batch_endpoint if mode == 'parallel' else editor.generate_batch_fast_endpoint
        return await route(request, tasks)

    def engine(self, manager, callback=None):
        def voice(text, instruct, speaker, voices, path):
            return callback([path]) if callback else (self.write_audio(path) or True)
        def batch(chunks, voices, output, seed):
            paths = [Path(output)/f"temp_batch_{row['index']}.wav" for row in chunks]
            success = callback(paths) if callback else ([self.write_audio(path) for path in paths] or True)
            return {'completed': [row['index'] for row in chunks] if success else [],
                    'failed': [] if success else [(row['index'], 'provider false') for row in chunks]}
        manager.engine = SimpleNamespace(generate_voice=voice, generate_batch=batch)

    def assert_prior(self, root, manager, before):
        self.assertEqual(before, manager.load_chunks())
        self.assertFalse(list(root.glob('chunk_*.wav')))
        self.assertFalse(list(root.glob('temp_batch_*.wav')))
        self.assertFalse(list((root/'voicelines').glob('.render-*')))

    def test_acknowledged_cancel_during_provider_keeps_prior_takes_and_lease_until_cleanup(self):
        for mode in ('single', 'parallel', 'native'):
            for outcome in ('success', 'false', 'oom_exception'):
                with self.subTest(mode=mode, outcome=outcome), self.fixture() as (root, manager, before, state):
                    entered = threading.Event(); release = threading.Event(); calls = []
                    prior = [(root/row['audio_path']).read_bytes() for row in before]
                    def finish(paths):
                        calls.append(1); entered.set(); self.assertTrue(release.wait(5))
                        for path in paths: self.write_audio(path)
                        if outcome == 'oom_exception': raise RuntimeError('CUDA out of memory')
                        return outcome == 'success'
                    self.engine(manager, finish)
                    async def exercise():
                        tasks = BackgroundTasks()
                        await self.start(mode, tasks, [0] if mode == 'single' else [0, 1])
                        worker = asyncio.create_task(tasks())
                        try:
                            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                            claim = core._task_claims['audio']['id']
                            self.assertEqual({'status': 'cancelling'}, await editor.cancel_audio())
                            self.assertEqual(claim, core._task_claims['audio']['id'])
                            self.assertTrue(core.is_task_running('audio'))
                            with self.assertRaises(HTTPException): core.claim_gpu_task('audio')
                        finally: release.set()
                        await asyncio.wait_for(worker, 10)
                    asyncio.run(exercise())
                    self.assertEqual([1], calls)
                    self.assert_prior(root, manager, before)
                    self.assertEqual(prior, [(root/row['audio_path']).read_bytes() for row in before])
                    self.assertFalse(core.is_task_running('audio')); self.assertEqual({}, core._task_claims)
                    if mode == 'single':
                        self.assertEqual('cancelled', state['audio']['status']); self.assertIsNone(state['audio']['error'])
                    else: self.assertIn('2 cancelled', '\n'.join(state['audio']['logs']))

    def test_cancel_after_private_conversion_cannot_promote_any_render_mode(self):
        for mode in ('single', 'parallel', 'native'):
            with self.subTest(mode=mode), self.fixture() as (root, manager, before, state):
                self.engine(manager); original = manager._export_chunk_audio
                def convert(*args):
                    result = original(*args)
                    self.assertEqual({'status': 'cancelling'}, editor._apply_audio_cancel())
                    return result
                async def exercise():
                    tasks = BackgroundTasks(); await self.start(mode, tasks, [0] if mode == 'single' else [0, 1]); await tasks()
                with patch.object(manager, '_export_chunk_audio', side_effect=convert): asyncio.run(exercise())
                self.assert_prior(root, manager, before)

    def test_queued_cancel_never_initializes_engine_or_changes_existing_takes(self):
        for mode in ('single', 'parallel', 'native'):
            with self.subTest(mode=mode), self.fixture() as (root, manager, before, state):
                async def exercise():
                    tasks = BackgroundTasks(); await self.start(mode, tasks, [0, 1] if mode != 'single' else [0])
                    self.assertEqual({'status': 'cancelling'}, await editor.cancel_audio()); await tasks()
                with patch.object(manager, 'get_engine', side_effect=AssertionError('cancelled task loaded engine')): asyncio.run(exercise())
                self.assert_prior(root, manager, before)

    def test_takes_attached_before_cancel_are_kept_and_later_results_are_discarded(self):
        for mode in ('parallel', 'native'):
            with self.subTest(mode=mode), self.fixture() as (root, manager, before, state):
                self.engine(manager); original = manager._publish_generated_chunk_audio
                def publish(chunk, *args):
                    result = original(chunk, *args)
                    if chunk['uid'] == 'line-0': self.assertEqual({'status': 'cancelling'}, editor._apply_audio_cancel())
                    return result
                async def exercise():
                    tasks = BackgroundTasks(); await self.start(mode, tasks, [0, 1]); await tasks()
                with patch.object(manager, '_publish_generated_chunk_audio', side_effect=publish): asyncio.run(exercise())
                saved = manager.load_chunks()
                self.assertNotEqual(before[0]['audio_revision'], saved[0]['audio_revision']); self.assertEqual('done', saved[0]['status'])
                self.assertEqual(before[1], saved[1])
                self.assertIn('1 succeeded, 0 failed, 1 cancelled', '\n'.join(state['audio']['logs']))

    def test_replaced_claim_cannot_revive_or_clean_up_when_cancel_flag_is_reset(self):
        with self.fixture(1) as (root, manager, before, state):
            snapshot = []
            def finish(paths):
                self.write_audio(paths[0])
                with core._gpu_lock:
                    core._task_claims['audio']['id'] = 'replacement-owner'; state['audio']['cancel'] = False
                    snapshot.extend(copy.deepcopy(manager.load_chunks()))
                return True
            self.engine(manager, finish)
            async def exercise():
                tasks = BackgroundTasks(); await self.start('single', tasks, [0]); await tasks()
            asyncio.run(exercise())
            self.assertEqual(snapshot, manager.load_chunks()); self.assertEqual('replacement-owner', core._task_claims['audio']['id'])
            self.assertEqual(before[0]['audio_revision'], manager.load_chunks()[0]['audio_revision'])
            self.assertFalse(list(root.glob('chunk_*.wav')))

    def test_admitted_publication_finishes_before_cancel_is_acknowledged(self):
        with self.fixture(1) as (root, manager, before, state):
            entered = threading.Event(); cancel_started = threading.Event(); cancelled = threading.Event()
            events = []; original = manager._update_chunk_fields_by_uid; errors = []
            def update(*args, **kwargs):
                if kwargs.get('audio_source'):
                    entered.set(); self.assertTrue(cancel_started.wait(3)); self.assertFalse(cancelled.is_set())
                    result = original(*args, **kwargs); events.append('attached'); return result
                return original(*args, **kwargs)
            def cancel():
                try:
                    self.assertTrue(entered.wait(3)); cancel_started.set()
                    editor._apply_audio_cancel(); events.append('cancel_acknowledged'); cancelled.set()
                except BaseException as exc: errors.append(exc)
            self.engine(manager)
            async def exercise():
                tasks = BackgroundTasks(); await self.start('single', tasks, [0]); await tasks()
            worker = threading.Thread(target=cancel)
            with patch.object(manager, '_update_chunk_fields_by_uid', side_effect=update):
                worker.start()
                try: asyncio.run(exercise())
                finally: worker.join(5)
            self.assertFalse(worker.is_alive()); self.assertEqual([], errors); self.assertTrue(cancelled.is_set())
            self.assertEqual(['attached', 'cancel_acknowledged'], events)
            self.assertNotEqual(before[0]['audio_revision'], manager.load_chunks()[0]['audio_revision'])

    def test_owner_context_is_scoped_and_released_claim_cannot_publish(self):
        with self.fixture(1):
            tasks = BackgroundTasks(); captured = []
            def callback():
                captured.append(get_audio_generation_owner())
                with ensure_audio_generation_publication(): pass
            core.schedule_claimed_background_task(tasks, 'audio', callback); asyncio.run(tasks())
            self.assertIsNone(get_audio_generation_owner())
            with self.assertRaises(AudioGenerationCancelled):
                with captured[0](): pass
            self.assertNotIn('audio', core._task_claims)

    def test_native_failures_committed_before_cancel_remain_in_result_counts(self):
        with self.fixture() as (root, manager, before, state):
            (root/'config.json').write_text(json.dumps({'tts': {'mode': 'local', 'parallel_workers': 2}}))
            manager.engine = SimpleNamespace(generate_batch=lambda *args: {
                'completed': [], 'failed': [(0, 'first provider error'), (1, 'second provider error')]})
            original = manager._update_generation_fields
            def update(chunk, **fields):
                result = original(chunk, **fields)
                if chunk['uid'] == 'line-0' and fields.get('status') == 'error':
                    self.assertEqual({'status': 'cancelling'}, editor._apply_audio_cancel())
                return result
            async def exercise():
                tasks = BackgroundTasks(); await self.start('native', tasks, [0, 1]); await tasks()
            with patch.object(manager, '_update_generation_fields', side_effect=update): asyncio.run(exercise())
            saved = manager.load_chunks()
            self.assertEqual(('error', 'first provider error'), (saved[0]['status'], saved[0]['error']))
            self.assertEqual(before[1], saved[1])
            self.assertIn('0 succeeded, 1 failed, 1 cancelled', '\n'.join(state['audio']['logs']))

    def test_reset_rechecks_exact_owner_at_cleanup_admission(self):
        import project
        for replacement in (False, True):
            with self.subTest(replacement=replacement), self.fixture(1) as (root, manager, before, state):
                original = project.ensure_audio_generation_publication
                @contextlib.contextmanager
                def boundary(*, allow_cancelled=False):
                    if allow_cancelled:
                        if replacement:
                            with core._gpu_lock:
                                core._task_claims['audio']['id'] = 'replacement-owner'
                                state['audio']['cancel'] = False
                        else:
                            self.assertEqual({'status': 'cancelling'}, editor._apply_audio_cancel())
                    with original(allow_cancelled=allow_cancelled): yield
                outcomes = []
                def callback():
                    manager._update_generation_fields(before[0], status='generating')
                    with patch.object(project, 'ensure_audio_generation_publication', boundary):
                        outcomes.append(manager._reset_captured_generations({0: before[0]}, [0], set()))
                tasks = BackgroundTasks(); core.schedule_claimed_background_task(tasks, 'audio', callback); asyncio.run(tasks())
                saved = manager.load_chunks()
                if replacement:
                    self.assertEqual('generating', saved[0]['status']); self.assertEqual(0, outcomes[0][0])
                    self.assertEqual(1, len(outcomes[0][1]))
                else:
                    self.assertEqual(before, saved); self.assertEqual((1, []), outcomes[0])

    def test_owned_reset_holds_admission_across_read_and_restore(self):
        with self.fixture(1) as (root, manager, before, state):
            manager._update_chunk_fields_by_uid('line-0', status='error', error='prior error')
            captured = copy.deepcopy(manager.load_chunks()[0])
            entered = threading.Event(); attempted = threading.Event(); acknowledged = threading.Event()
            original = manager._update_chunk_fields_by_uid; errors = []; events = []; results = []
            def update(*args, **kwargs):
                if 'status' not in kwargs:
                    entered.set(); self.assertTrue(attempted.wait(3)); self.assertFalse(acknowledged.is_set())
                result = original(*args, **kwargs)
                if kwargs.get('status') == 'error': events.append('restored')
                return result
            def cancel():
                try:
                    self.assertTrue(entered.wait(3)); attempted.set()
                    editor._apply_audio_cancel(); events.append('cancel_acknowledged'); acknowledged.set()
                except BaseException as exc: errors.append(exc)
            def callback():
                manager._update_generation_fields(captured, status='generating', error=None)
                with patch.object(manager, '_update_chunk_fields_by_uid', side_effect=update):
                    results.append(manager._reset_captured_generations({0: captured}, [0], set()))
            tasks = BackgroundTasks(); core.schedule_claimed_background_task(tasks, 'audio', callback)
            worker = threading.Thread(target=cancel); worker.start()
            try: asyncio.run(tasks())
            finally: worker.join(5)
            self.assertFalse(worker.is_alive()); self.assertEqual([], errors); self.assertTrue(acknowledged.is_set())
            self.assertEqual(['restored', 'cancel_acknowledged'], events)
            self.assertEqual([(1, [])], results); self.assertEqual(captured, manager.load_chunks()[0])
