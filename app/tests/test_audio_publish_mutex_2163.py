"""Deterministic regression for issue #2163.

Audio publication must not hold the global task mutex across publish I/O, and a
cancel arriving before the final commit must not rename staged audio. Synthetic
artifacts and the real Editor publish entry point only; no GPU, no live app.
"""
import asyncio
import contextlib
from contextlib import nullcontext
import copy
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
import types
from unittest.mock import patch
import numpy as np
import soundfile as sf
from fastapi import BackgroundTasks
import audio_validation
import core
import project
from audio_generation_owner import AudioGenerationCancelled
from project import ProjectManager
from routers import editor
from tests.test_support import use_private_cpu_fixture_lock


def _m4b_settlement_ok(outcome):
    """True only if the worker surfaced no exception and released its claim."""
    response = outcome.get('response')
    return ('exc' not in outcome
            and getattr(response, 'status_code', None) == 200
            and 'm4b_export' not in core._task_claims
            and not core.get_task_lifecycle('m4b_export')['running'])


_REPLACEMENT_CALLS = ('replace_export_output', 'publish_audio_output')


_COMMIT_VALIDATOR = 'ensure_audio_generation_commit_active'


def _calls_commit_validator(function):
    """True when the function's executable body calls the commit validator.

    Comment text and a function's name are not evidence: a ``is_cancelled``
    that only mentions the validator in a comment and returns a constant must
    be rejected (F12).
    """
    import ast
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, 'id', None))
        if name == _COMMIT_VALIDATOR:
            return True
    return False


def _native_replace_wiring_violations(source):
    """Return violations of the production replacement-callback contract.

    A ``replace_export_output``/``publish_audio_output`` call must pass an
    effective ``cancel_check`` keyword whose value is a real callable: either
    the commit validator itself or a local function whose executable body
    calls it. A named no-op (even with the validator's name in a comment) is
    inert, not evidence of wiring.
    """
    import ast
    import textwrap
    text = textwrap.dedent(source)
    tree = ast.parse(text)
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, 'id', None))
        if name not in _REPLACEMENT_CALLS:
            continue
        keyword = next((kw for kw in node.keywords if kw.arg == 'cancel_check'), None)
        if keyword is None:
            violations.append(f'{name} without cancel_check')
            continue
        value = keyword.value
        if isinstance(value, ast.Constant):
            violations.append(f'{name} cancel_check is a literal {value.value!r}')
            continue
        target = (value.id if isinstance(value, ast.Name)
                  else value.attr if isinstance(value, ast.Attribute) else None)
        if target == _COMMIT_VALIDATOR:
            continue
        function = functions.get(target)
        if function is None:
            violations.append(f'{name} cancel_check {target!r} is not a local callable')
        elif not _calls_commit_validator(function):
            violations.append(f'{name} cancel_check {target!r} is inert')
    return violations


def _owner_binding_complete(owner):
    from audio_generation_owner import AudioGenerationOwner
    return (type(owner) is AudioGenerationOwner
            and callable(getattr(owner, 'validate', None))
            and callable(getattr(owner, 'validate_claim', None)))


class AudioPublishMutex2163Tests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            (root/'annotated_script.json').write_text('[]')
            (root/'state.json').write_text(json.dumps({'active_book_id': 'synthetic-2163'}))
            config = root/'config.json'
            config.write_text(json.dumps({'tts': {'mode': 'local', 'parallel_workers': 1}}))
            manager = ProjectManager(tmp)
            (root/'voice_config.json').write_text(json.dumps({'NARRATOR': {'type': 'custom', 'voice': 'Ryan'}}))
            sf.write(root/'prior-0.wav', np.full(2400, .1), 24000)
            rows = [dict(id=0, uid='line-0', speaker='NARRATOR', text='Synthetic line 0.',
                instruct='', status='done', audio_path='prior-0.wav',
                audio_revision='prior-revision-0', error=None)]
            manager.save_chunks(rows)
            before = copy.deepcopy(manager.load_chunks())
            state = copy.deepcopy(core.process_state)
            for entry in state.values():
                entry['running'] = False
            for module, name, value in (
                (core, 'process_state', state), (editor, 'process_state', state),
                (core, '_task_claims', {}), (core, '_gpu_leases', {}), (core, 'DATA_DIR', tmp),
                (editor, 'DATA_DIR', tmp), (editor, 'SCRIPT_PATH', str(root/'annotated_script.json')),
                (editor, 'CONFIG_PATH', str(config)), (editor, 'project_manager', manager)):
                stack.enter_context(patch.object(module, name, value))
            stack.enter_context(use_private_cpu_fixture_lock())
            stack.enter_context(patch('project._export_audio_segment',
                                      side_effect=RuntimeError('force real WAV fallback')))
            try:
                yield root, manager, before, state
            finally:
                for name, claim in list(core._task_claims.items()):
                    core.release_gpu_task_claim(name, claim['id'])

    def engine(self, manager):
        def voice(text, instruct, speaker, voices, path):
            sf.write(path, np.full(2400, .2), 24000)
            return True
        manager.engine = SimpleNamespace(generate_voice=voice)

    def run_publish(self):
        async def exercise():
            tasks = BackgroundTasks()
            result = await editor.generate_chunk_endpoint(0, tasks, expected_uid='line-0')
            await tasks()
            return result
        return asyncio.run(exercise())

    def test_unrelated_admission_does_not_block_during_publication_io(self):
        with self.fixture() as (root, manager, before, state):
            self.engine(manager)
            io_entered = threading.Event()
            release_io = threading.Event()
            observed = []
            real_publish = project.publish_audio_output
            def blocked_publish(source, destination, **kwargs):
                # Only the final chunk rename is the publication commit; the
                # private render conversion also calls publish_audio_output.
                if '.slot-' not in str(destination):
                    return real_publish(source, destination)
                io_entered.set()
                self.assertTrue(release_io.wait(5))
                return real_publish(source, destination)
            def observer():
                self.assertTrue(io_entered.wait(5))
                acquired = core._gpu_lock.acquire(timeout=0.5)
                if acquired:
                    core._gpu_lock.release()
                observed.append(acquired)
                release_io.set()
            watcher = threading.Thread(target=observer)
            with patch.object(project, 'publish_audio_output', side_effect=blocked_publish):
                watcher.start()
                try:
                    self.run_publish()
                finally:
                    release_io.set()
                    watcher.join(5)
            self.assertFalse(watcher.is_alive())
            self.assertEqual([True], observed)

    def test_cancel_before_commit_refuses_publication(self):
        with self.fixture() as (root, manager, before, state):
            self.engine(manager)
            entered = threading.Event()
            cancel_arrived = threading.Event()
            errors = []
            original = manager._update_chunk_fields_by_uid
            def update(*args, **kwargs):
                if kwargs.get('audio_source'):
                    entered.set()
                    cancel_arrived.wait(2)
                return original(*args, **kwargs)
            def cancel():
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual({'status': 'cancelling'}, editor._apply_audio_cancel())
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    cancel_arrived.set()
            worker = threading.Thread(target=cancel)
            with patch.object(manager, '_update_chunk_fields_by_uid', side_effect=update):
                worker.start()
                try:
                    self.run_publish()
                finally:
                    cancel_arrived.set()
                    worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(before, manager.load_chunks())
            self.assertFalse(list(root.glob('voicelines/*.wav')))

    def test_stale_claim_before_commit_refuses_publication(self):
        with self.fixture() as (root, manager, before, state):
            self.engine(manager)
            entered = threading.Event()
            replace_done = threading.Event()
            errors = []
            original = manager._update_chunk_fields_by_uid
            def update(*args, **kwargs):
                if kwargs.get('audio_source'):
                    entered.set()
                    self.assertTrue(replace_done.wait(5))
                return original(*args, **kwargs)
            def replace():
                try:
                    self.assertTrue(entered.wait(5))
                    core._task_claims['audio']['id'] = 'stale-replacement-owner'
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    replace_done.set()
            worker = threading.Thread(target=replace)
            with patch.object(manager, '_update_chunk_fields_by_uid', side_effect=update):
                worker.start()
                try:
                    self.run_publish()
                finally:
                    replace_done.set()
                    worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            # A stale claim has no cleanup authority: the generating row is
            # left for recovery, but no staged audio is published over the
            # prior artifact.
            saved = manager.load_chunks()[0]
            self.assertEqual('prior-0.wav', saved['audio_path'])
            self.assertEqual('prior-revision-0', saved['audio_revision'])
            self.assertFalse(list(root.glob('voicelines/*.wav')))

    def test_legitimate_publish_commits_audio(self):
        with self.fixture() as (root, manager, before, state):
            self.engine(manager)
            self.run_publish()
            saved = manager.load_chunks()
            self.assertNotEqual(before[0]['audio_revision'], saved[0]['audio_revision'])
            self.assertEqual('done', saved[0]['status'])
            self.assertTrue(list(root.glob('voicelines/voiceline_*.wav')))

    def test_commit_check_does_not_reenter_owner_or_task_mutex(self):
        from audio_generation_owner import (AudioGenerationOwner, ensure_audio_generation_commit_active,
                                            ensure_audio_generation_owner)
        mutex = threading.Lock()
        admits = []
        validations = []
        @contextlib.contextmanager
        def admit(**kwargs):
            if not mutex.acquire(timeout=.25):
                raise TimeoutError('owner mutex reentry')
            try:
                admits.append(1)
                yield
            finally:
                mutex.release()
        owner = AudioGenerationOwner('a' * 32, admit, validate=lambda: validations.append(1))
        # The task mutex is held by an admission path (idle Cancel also takes
        # it); the commit check must neither reacquire it nor reenter admit.
        with core._gpu_lock, ensure_audio_generation_owner(owner), admit():
            ensure_audio_generation_commit_active()
        self.assertEqual([1], admits)
        self.assertEqual([1], validations)

    def test_commit_check_without_validation_is_a_no_op_for_direct_callers(self):
        from audio_generation_owner import (AudioGenerationOwner, ensure_audio_generation_commit_active,
                                            ensure_audio_generation_owner)
        mutex = threading.Lock()
        admits = []
        @contextlib.contextmanager
        def admit(**kwargs):
            if not mutex.acquire(timeout=.25):
                raise TimeoutError('owner mutex reentry')
            try:
                admits.append(1)
                yield
            finally:
                mutex.release()
        with ensure_audio_generation_owner(AudioGenerationOwner('a' * 32, admit)), admit():
            ensure_audio_generation_commit_active()
        self.assertEqual([1], admits)

    def _interrupt_at_output_admission(self, root, manager, before, interrupt, settled=True):
        self.engine(manager)
        entered = threading.Event()
        release = threading.Event()
        errors = []
        real_admission = audio_validation.ensure_audio_output
        @contextlib.contextmanager
        def blocked(path):
            if '.slot-' in str(path):
                entered.set()
                self.assertTrue(release.wait(5))
            with real_admission(path):
                yield
        def worker():
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(core.claim_gpu_task('m4b_export', cpu_only=True))
                interrupt()
            except BaseException as exc:
                errors.append(exc)
            finally:
                release.set()
        watcher = threading.Thread(target=worker)
        with patch.object(audio_validation, 'ensure_audio_output', blocked):
            watcher.start()
            try:
                self.run_publish()
            finally:
                release.set()
                watcher.join(5)
        self.assertFalse(watcher.is_alive())
        self.assertEqual([], errors)
        saved = manager.load_chunks()[0]
        self.assertEqual('prior-0.wav', saved['audio_path'])
        self.assertEqual('prior-revision-0', saved['audio_revision'])
        if settled:
            self.assertEqual(before[0], saved)
        self.assertFalse(list(root.glob('voicelines/*.wav')))

    def test_cancel_after_precommit_io_but_at_output_admission_refuses_swap(self):
        with self.fixture() as (root, manager, before, state):
            def interrupt():
                self.assertEqual({'status': 'cancelling'}, editor._apply_audio_cancel())
            self._interrupt_at_output_admission(root, manager, before, interrupt)

    def test_replacement_after_precommit_io_but_at_output_admission_refuses_swap(self):
        with self.fixture() as (root, manager, before, state):
            def interrupt():
                with core._gpu_lock:
                    claim = dict(core._task_claims['audio'])
                    claim['id'] = 'replacement-owner'
                    core._task_claims['audio'] = claim
            self._interrupt_at_output_admission(root, manager, before, interrupt, settled=False)

    def test_commit_validation_observes_one_coherent_generation(self):
        old = 'a' * 32
        claims = {'audio': dict(id=old, phase='started')}
        states = {'audio': {'cancel': True}}
        inside = threading.Event()
        attempt = threading.Event()
        release = threading.Event()
        swapped = threading.Event()
        def swap():
            attempt.set()
            with core._gpu_lock:
                claims['audio'] = dict(id='b' * 32, phase='started')
                states['audio'] = {'cancel': False}
            swapped.set()
        worker = threading.Thread(target=swap)
        def trace(frame, event, arg):
            if (frame.f_code is core._audio_task_owner_validation.__code__
                    and event == 'line' and 'owner' in frame.f_locals
                    and not inside.is_set()):
                inside.set()
                worker.start()
                self.assertTrue(attempt.wait(3))
                self.assertFalse(swapped.wait(.2))
                release.set()
            return trace
        with patch.object(core, '_task_claims', claims), patch.object(core, 'process_state', states):
            sys.settrace(trace)
            try:
                with self.assertRaises(AudioGenerationCancelled):
                    core._audio_task_owner_validation(old)
            finally:
                sys.settrace(None)
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(swapped.is_set())

    def test_direct_validation_runs_after_fsync_inside_output_admission(self):
        import synthesis_workspace
        from audio_generation_owner import ensure_audio_generation_commit_active
        with self.fixture() as (root, manager, before, state):
            self.engine(manager)
            events = []
            real_admission = audio_validation.ensure_audio_output
            real_publish = audio_validation.publish_audio_output
            real_fsync = os.fsync
            @contextlib.contextmanager
            def admission(path):
                if '.slot-' in str(path):
                    events.append('output-lock')
                    self.assertEqual({'status': 'cancelling'}, editor._apply_audio_cancel())
                with real_admission(path):
                    yield
            def publish(source, destination, **kwargs):
                if '.slot-' in str(destination):
                    self.assertIs(ensure_audio_generation_commit_active, kwargs.get('cancel_check'))
                    events.append('publish-call')
                return real_publish(source, destination, **kwargs)
            def fsync(fd):
                events.append('fsync')
                return real_fsync(fd)
            with patch.object(audio_validation, 'ensure_audio_output', admission), \
                 patch.object(project, 'publish_audio_output', publish), \
                 patch.object(synthesis_workspace, 'get_registered_encoding_stage', lambda path: None), \
                 patch.object(os, 'fsync', fsync):
                self.run_publish()
            self.assertLess(events.index('fsync'), events.index('output-lock'))
            self.assertLess(events.index('publish-call'), events.index('output-lock'))
            self.assertEqual(before, manager.load_chunks())
            self.assertFalse(list(root.glob('voicelines/*.wav')))

    def test_registered_validation_runs_after_fsync_before_rename(self):
        import synthesis_workspace as synthesis_workspace
        ws = synthesis_workspace
        from audio_generation_owner import (AudioGenerationOwner, AudioGenerationCancelled,
                                            ensure_audio_generation_owner)
        from contextlib import nullcontext
        with self.fixture() as (root, manager, before, state):
            events = []
            cancelled = [False]
            def admit(**kwargs):
                return nullcontext()
            def validate():
                self.assertTrue(cancelled[0])
                raise AudioGenerationCancelled()
            owner = AudioGenerationOwner('a' * 32, admit, validate=validate)
            def publisher(name, destination, **kwargs):
                events.append('publisher')
                self.assertIsNotNone(kwargs.get('cancel_check'))
                kwargs['cancel_check']()
                self.fail('cancelled publication reached the rename')
            def fsync(fd):
                events.append('fsync')
                cancelled[0] = True
            source = SimpleNamespace(stage=SimpleNamespace(directory=root / 'a' / 'b'),
                                     path='encoded.wav', source_fd=0, directory_fd=0,
                                     version=(0,) * 8, destination_fd=0,
                                     publisher=publisher, check=lambda: None)
            with ensure_audio_generation_owner(owner), \
                 patch.object(audio_validation, 'publish_audio_output', publisher), \
                 patch.object(os, 'fsync', fsync):
                with self.assertRaises(AudioGenerationCancelled):
                    ws.RegisteredEncodingSource.publish(source, str(root / 'voicelines' / 'out.wav'))
            self.assertEqual(['fsync', 'publisher'], events)

    def test_no_task_scope_holds_the_global_mutex_across_publication(self):
        from audio_generation_owner import AudioGenerationOwner
        claim = 'a' * 32
        tasks = ('audio', 'speech_verification', 'm4b_export', 'chapter_export',
                 'audacity_export', 'readalong_sidecar', 'readaloud_epub')
        for task in tasks:
            with self.subTest(task=task), \
                 patch.object(core, '_task_claims', {task: dict(id=claim, phase='started')}), \
                 patch.object(core, 'process_state', {task: {'cancel': False}}):
                with core._ensure_audio_task_owner(claim, task_name=task):
                    self.assertFalse(core._gpu_lock.locked())
            # Inspect the owner the real worker actually binds, and invoke its
            # validators against the live claim (R18). No constructor or
            # binding-context mock is used here.
            owner = self._observe_bound_owner(task, claim)
            self.assertTrue(_owner_binding_complete(owner), task)

    def test_owner_binding_oracle_rejects_admission_only_binding(self):
        # Bad-implementation control: a worker that binds admission only must
        # be rejected by the same binding gate (R18).
        from audio_generation_owner import AudioGenerationOwner
        claim = 'a' * 32
        tasks = ('audio', 'speech_verification', 'm4b_export', 'chapter_export',
                 'audacity_export', 'readalong_sidecar', 'readaloud_epub')
        def admission_only(claim_id, admit, **_kwargs):
            return AudioGenerationOwner(claim_id, admit)
        for task in tasks:
            with self.subTest(task=task):
                owner = self._observe_bound_owner(task, claim, admission_only)
                self.assertFalse(_owner_binding_complete(owner), task)

    def _observe_bound_owner(self, task, claim, constructor=None):
        from audio_generation_owner import AudioGenerationOwner
        seen = {}
        def callback():
            owner = core.get_audio_generation_owner()
            seen['owner'] = owner
            validate = getattr(owner, 'validate', None)
            validate_claim = getattr(owner, 'validate_claim', None)
            if callable(validate):
                validate()
            if callable(validate_claim):
                validate_claim()
        stack = contextlib.ExitStack()
        with stack:
            stack.enter_context(patch.object(core, '_task_claims',
                                             {task: dict(id=claim, phase='started')}))
            stack.enter_context(patch.object(core, 'process_state', {task: {'cancel': False}}))
            if constructor is not None:
                stack.enter_context(patch.object(core, 'AudioGenerationOwner', constructor))
            core._run_owned_background_task(task, claim, callback, already_started=True)
        return seen['owner']

    def test_windows_fallback_validates_after_lstat_before_native_replace(self):
        import export_file_io as eio
        with self.fixture() as (root, manager, before, state):
            source = root / 'win-source.tmp'
            destination = root / 'win-destination.tmp'
            source.write_bytes(b'new')
            destination.write_bytes(b'prior')
            events = []
            cancelled = [False]
            def cancel_check():
                events.append(('cancel_check', len([e for e in events if e[0] == 'lstat'])))
                return cancelled[0]
            class FakeOs(types.ModuleType):
                name = 'nt'
                path = os.path
                def replace(self, src, dst):
                    events.append(('replace_attempt', None))
                    error = PermissionError('sharing violation')
                    error.winerror = 5
                    raise error
                def lstat(self, path):
                    events.append(('lstat', None))
                    cancelled[0] = True
                    return os.stat(path)
            api = SimpleNamespace(ReplaceFileW=lambda *args: events.append(('ReplaceFileW', None)) or 1)
            with patch.object(eio, 'os', FakeOs('os')), patch.object(eio, '_windows_api', lambda: api):
                self.assertIs(False, eio.replace_export_output(str(source), str(destination),
                                                               cancel_check=cancel_check))
            check = [event for event in events if event[0] == 'cancel_check']
            self.assertEqual([('cancel_check', 2)], check)
            self.assertEqual(2, len([event for event in events if event[0] == 'lstat']))
            self.assertNotIn(('ReplaceFileW', None), events)
            self.assertEqual(b'prior', destination.read_bytes())

    def test_mp3_merge_threads_commit_check_into_native_replace(self):
        import project as project_module
        from audio_generation_owner import ensure_audio_generation_commit_active
        from tests.test_export_quality_api import ExportQualityApiTests
        case = ExportQualityApiTests()
        with case.fixture() as (root, manager, states, client, token):
            calls = []
            real = project_module.replace_export_output
            def capture(source, destination, **kwargs):
                calls.append(kwargs.get('cancel_check'))
                return real(source, destination, **kwargs)
            with patch.object(project_module, 'replace_export_output', capture):
                self.assertEqual(200, case.merge(client, {}).status_code)
            self.assertIn(ensure_audio_generation_commit_active, calls)

    def test_m4b_windows_fallback_cancel_preserves_prior_and_settles_nothing(self):
        import export_file_io as eio
        import project_scope_exports as scope_exports
        from tests import test_export_task_results as export_tasks
        from tests import test_m4b_export_quality as m4b_media
        with export_tasks.ExportTaskResultTests().fixture() as (root, states, client):
            manager = m4b_media.M4BExportQualityTests().fixture(root)
            output = Path(root) / 'audiobook.m4b'
            output.write_bytes(b'PRIOR-M4B')
            # Seed a prior receipt so the test can prove exact ledger bytes and
            # journal retirement instead of only bytes/status.
            scope_exports.record_export_publication(root, output)
            ledger_path = Path(root) / scope_exports.PUBLICATIONS_FILE
            prior_ledger = ledger_path.read_bytes()
            self.assertIsNone(scope_exports.read_publication_journal(root))
            at_lstat = threading.Event()
            released = threading.Event()
            seen = []
            class FakeOs(types.ModuleType):
                name = 'nt'
                path = os.path
                def replace(self, source, destination):
                    seen.append('replace-attempt')
                    error = PermissionError('sharing violation')
                    error.winerror = 5
                    raise error
                def lstat(self, path):
                    seen.append('lstat')
                    at_lstat.set()
                    assert released.wait(8), 'lstat never released'
                    return os.stat(path)
            class Api:
                def ReplaceFileW(self, *args):
                    seen.append('ReplaceFileW')
                    return 1
            outcome = {}
            def publish():
                try:
                    outcome['response'] = client.post('/api/merge_m4b', json={})
                except BaseException as exc:
                    outcome['exc'] = exc
            def cancel():
                assert at_lstat.wait(10), 'publication never reached lstat'
                outcome['reply'] = client.post('/api/merge_m4b/cancel').json()
                released.set()
            writer = threading.Thread(target=publish)
            watcher = threading.Thread(target=cancel)
            with patch.object(eio, 'os', FakeOs('os')), \
                 patch.object(eio, '_windows_api', lambda: Api()), \
                 patch.object(editor, 'project_manager', manager):
                watcher.start()
                writer.start()
                try:
                    writer.join(20)
                    watcher.join(20)
                finally:
                    released.set()
            self.assertFalse(writer.is_alive())
            self.assertFalse(watcher.is_alive())
            self.assertEqual({'status': 'cancelling'}, outcome.get('reply'))
            self.assertEqual(2, len([event for event in seen if event == 'lstat']))
            self.assertNotIn('ReplaceFileW', seen)
            self.assertEqual(b'PRIOR-M4B', output.read_bytes())
            self.assertEqual('cancelled', states['m4b_export']['result'].get('status'))
            # The cancellation rolled the receipt transaction back exactly.
            self.assertIsNone(scope_exports.read_publication_journal(root))
            self.assertEqual(prior_ledger, ledger_path.read_bytes())
            # R17: a swallowed worker exception or a still-started claim must
            # fail this gate even after the bytes rolled back correctly.
            self.assertNotIn('exc', outcome)
            self.assertEqual(200, outcome['response'].status_code)
            self.assertTrue(_m4b_settlement_ok(outcome), (outcome, core._task_claims))

    def test_m4b_settlement_gate_rejects_worker_exception_and_lingering_claim(self):
        # Injected bad-implementation controls for the M4B settlement gate: a
        # forced release error or a started-but-unreleased claim must be
        # rejected even when the HTTP response itself is 200 (R17).
        ok = {'response': SimpleNamespace(status_code=200)}
        lingering = {'m4b_export': {'id': 'a' * 32, 'phase': 'started'}}
        states = {'m4b_export': {'running': True, 'status': 'running', 'cancel': False}}
        with patch.object(core, '_task_claims', lingering), \
             patch.object(core, 'process_state', states):
            self.assertFalse(_m4b_settlement_ok({**ok, 'exc': OSError('forced release error')}))
            self.assertFalse(_m4b_settlement_ok(ok))
        with patch.object(core, '_task_claims', {}), \
             patch.object(core, 'process_state', {'m4b_export': {
                 'running': False, 'status': 'cancelled', 'cancel': False}}):
            self.assertTrue(_m4b_settlement_ok(ok))

    def test_m4b_receipt_restore_failure_reports_pending_then_recovers(self):
        import project_scope_exports as scope_exports
        from book_state_transaction import ensure_book_state
        from tests import test_export_task_results as export_tasks
        from tests import test_m4b_export_quality as m4b_media
        with export_tasks.ExportTaskResultTests().fixture() as (root, states, client):
            manager = m4b_media.M4BExportQualityTests().fixture(root)
            output = Path(root) / 'audiobook.m4b'
            output.write_bytes(b'PRIOR-M4B')
            scope_exports.record_export_publication(root, output)
            ledger_path = Path(root) / scope_exports.PUBLICATIONS_FILE
            prior_ledger = ledger_path.read_bytes()
            failures = []
            real_write = scope_exports.write_publication_ledger
            def failing_write(target_root, raw, **kwargs):
                if raw == prior_ledger:
                    failures.append(1)
                    raise PermissionError('temporary sharing violation at receipt restore')
                return real_write(target_root, raw, **kwargs)
            def cancel_then_refuse(source, destination, *, cancel_check):
                reply = client.post('/api/merge_m4b/cancel')
                self.assertEqual(200, reply.status_code)
                cancel_check()
                raise AssertionError('expected cancellation was not recognized')
            with patch.object(editor, 'project_manager', manager), \
                 patch.object(project, 'replace_export_output', cancel_then_refuse), \
                 patch.object(scope_exports, 'write_publication_ledger', failing_write):
                self.assertEqual(200, client.post('/api/merge_m4b', json={}).status_code)
            result = states['m4b_export']['result']
            self.assertTrue(failures)
            self.assertEqual(b'PRIOR-M4B', output.read_bytes())
            self.assertEqual('intent', (scope_exports.read_publication_journal(root) or {}).get('phase'))
            self.assertNotEqual(prior_ledger, ledger_path.read_bytes())
            # Guarded artifacts remain, so the outcome must not claim a clean
            # cancellation; it is pending recovery.
            self.assertNotEqual('cancelled', result.get('status'))
            self.assertIn('pending recovery', result.get('message', ''))
            with ensure_book_state(root):
                pass
            self.assertEqual(prior_ledger, ledger_path.read_bytes())
            self.assertIsNone(scope_exports.read_publication_journal(root))

    def test_all_native_replace_call_sites_thread_commit_validation(self):
        # Inspect the effective keyword value (not a spelling/substring match)
        # for every production native-replacement helper, including the shared
        # audio publisher's nested predicate (R15).
        import project as project_module
        import sound_design_delivery
        import audio_validation
        cases = (
            ('ProjectManager.merge_audio', project_module.ProjectManager.merge_audio),
            ('ProjectManager.merge_m4b', project_module.ProjectManager.merge_m4b),
            ('sound_design_delivery.save_sound_delivery',
             sound_design_delivery.save_sound_delivery),
            ('audio_validation.publish_audio_output', audio_validation.publish_audio_output),
        )
        for label, function in cases:
            with self.subTest(label=label):
                violations = _native_replace_wiring_violations(inspect.getsource(function))
                self.assertEqual([], violations, f'{label}: {violations}')

    def test_native_replace_wiring_gate_rejects_inert_validators(self):
        # Controls: the historical missing keyword, an explicit None plus a
        # comment that merely mentions the validator, and a dead call must all
        # be rejected by the same gate (R15).
        import project as project_module
        source = inspect.getsource(project_module.ProjectManager.merge_m4b)
        self.assertEqual([], _native_replace_wiring_violations(source))
        missing = source.replace(
            'cancel_check=ensure_audio_generation_commit_active)', ')')
        self.assertTrue(_native_replace_wiring_violations(missing))
        inert = source.replace(
            'cancel_check=ensure_audio_generation_commit_active)',
            'cancel_check=None)').replace(
                'def replace():', 'def replace():\n                # ensure_audio_generation_commit_active')
        self.assertTrue(_native_replace_wiring_violations(inert))
        # A named no-op with the validator's name in a comment is still a
        # disconnected callback, not executable evidence of wiring (F12).
        named_noop = source.replace(
            'cancel_check=ensure_audio_generation_commit_active)',
            'cancel_check=is_cancelled)').replace(
                'def replace():',
                'def is_cancelled():\n'
                '                    # ensure_audio_generation_commit_active\n'
                '                    return False\n'
                '                def replace():')
        self.assertTrue(_native_replace_wiring_violations(named_noop))

    def _run_speech_lock_child(self, *, inverted):
        import os
        import subprocess
        env = dict(os.environ)
        env.pop('AA2_SPEECH_ORACLE_INVERT', None)
        if inverted:
            env['AA2_SPEECH_ORACLE_INVERT'] = '1'
        try:
            completed = subprocess.run(
                [sys.executable, '-c',
                 'from tests.test_audio_publish_mutex_2163 import _run_speech_lock_schedule as run; run()'],
                cwd=str(Path(__file__).resolve().parent.parent), env=env,
                capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired as error:
            self.fail('speech/Editor lock order schedule did not return (containment '
                      f'timeout; oracle did not report a cycle): {(error.stdout or "")!r}')
        return completed

    def test_speech_recheck_and_editor_publish_do_not_invert_locks(self):
        # Fail-before/pass-after deadlock gate: both real routes must reach
        # their intended lock gates, and the speech worker must be observed
        # inside the real chunk read before the Editor is released. Runs in a
        # child so a live lock cycle cannot wedge the test runner (R12).
        completed = self._run_speech_lock_child(inverted=False)
        self.assertEqual(0, completed.returncode,
                         'speech/Editor lock inversion gate failed:\n'
                         f'{completed.stdout}\n{completed.stderr}')
        # Require the structured positive marker so an unrelated early exit
        # (for example an unrelated storage 409) cannot satisfy the gate (F11).
        self.assertIn('SPEECH_LOCK_ORACLE OK', completed.stdout)

    def test_speech_lock_oracle_detects_injected_inversion(self):
        # Bad-implementation control for the rewritten oracle: reintroduce the
        # r5-style inversion (speech holds the task mutex across its chunk
        # read) and require the child to report a genuine live lock cycle with
        # the cycle exit code, not pass because its own "no cycle" diagnostic
        # contains the word "cycle" (F11).
        completed = self._run_speech_lock_child(inverted=True)
        self.assertEqual(2, completed.returncode,
                         'inverted schedule must report the cycle exit code:\n'
                         f'{completed.stdout}\n{completed.stderr}')
        combined = completed.stdout + completed.stderr
        self.assertIn('SPEECH_LOCK_ORACLE CYCLE', combined)
        self.assertIn('live lock cycle', combined)
        self.assertNotIn('SPEECH_LOCK_ORACLE NO_CYCLE', combined)

    def test_mp3_merge_cancel_before_receipt_io_preserves_prior(self):
        import project_scope_exports
        from tests.test_export_quality_api import ExportQualityApiTests
        case = ExportQualityApiTests()
        with case.fixture() as (root, manager, states, client, token):
            target = root / 'cloned_audiobook.mp3'
            target.write_bytes(b'previous MP3')
            entered, attempted, answered = (threading.Event() for _ in range(3))
            observed = {}
            def cancel():
                assert entered.wait(8)
                attempted.set()
                observed['reply'] = client.post('/api/cancel_audio').json()
                answered.set()
            real = project_scope_exports.publish_export_with_receipt
            def gate(*args, **kwargs):
                entered.set()
                assert attempted.wait(4)
                observed['ack_before_io'] = answered.wait(.8)
                return real(*args, **kwargs)
            worker = threading.Thread(target=cancel)
            with patch.object(project_scope_exports, 'publish_export_with_receipt', gate):
                worker.start()
                try:
                    self.assertEqual(200, case.merge(client, {}).status_code)
                finally:
                    worker.join(8)
            self.assertFalse(worker.is_alive())
            self.assertEqual({'status': 'cancelling'}, observed['reply'])
            self.assertTrue(observed['ack_before_io'])
            self.assertEqual(b'previous MP3', target.read_bytes())
            self.assertIn('Merge cancelled', '\n'.join(states['audio']['logs']))
            self.assertNotIn('Merge complete', '\n'.join(states['audio']['logs']))

    def test_m4b_cancel_during_final_source_validation_preserves_prior(self):
        from export_publication import ExportSourceSnapshot
        from tests import test_export_task_results as tasks
        from tests import test_m4b_export_quality as media
        with tasks.ExportTaskResultTests().fixture() as (root, states, client):
            manager = media.M4BExportQualityTests().fixture(root)
            output = Path(root) / 'audiobook.m4b'
            output.write_bytes(b'prior')
            entered = threading.Event()
            release = threading.Event()
            errors = []
            original = ExportSourceSnapshot._require_publication_current
            def held(snapshot, *args):
                original(snapshot, *args)
                entered.set()
                assert release.wait(5), 'publication hook never released'
            def publish():
                try:
                    response = client.post('/api/merge_m4b', json={})
                    self.assertEqual(200, response.status_code)
                except BaseException as exc:
                    errors.append(exc)
            with patch.object(editor, 'project_manager', manager), \
                 patch.object(ExportSourceSnapshot, '_require_publication_current', held):
                worker = threading.Thread(target=publish)
                worker.start()
                try:
                    self.assertTrue(entered.wait(10))
                    self.assertEqual(200, client.post('/api/merge_m4b/cancel').status_code)
                    self.assertTrue(states['m4b_export']['cancel'])
                finally:
                    release.set()
                    worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertTrue(entered.is_set())
            self.assertEqual(b'prior', output.read_bytes())
            self.assertEqual('cancelled', states['m4b_export']['result'].get('status'))

    # --- Dedicated regressions for the final Astra review findings ----------

    def test_speech_recheck_cancel_before_receipt_preserves_prior(self):
        # Finding 1: the final receipt write must revalidate exact ownership
        # under the chunk lock, so an acknowledged cancel cannot replace the
        # saved assessment even when the audio bytes are unchanged.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from tests.test_editor_speech_verification import EditorSpeechVerificationTests
        from tests.test_speech_verification_receipt import get_report
        with EditorSpeechVerificationTests().fixture() as (root, manager, before, _prior, _flags, _run):
            api = FastAPI()
            api.include_router(editor.router)
            token = editor.get_snapshot_digest(editor.get_editor_book_identity(root))
            body = dict(expected_book_token=token, expected_uid=before['uid'],
                        expected_audio_revision=before['audio_revision'])
            reached, requested, answered = (threading.Event() for _ in range(3))
            observed = {}
            real = manager._get_speech_staged_version
            def identity(path):
                reached.set()
                self.assertTrue(requested.wait(5))
                observed['ack_before_final_stat'] = answered.wait(1.0)
                return real(path)
            with TestClient(api) as client:
                def cancel():
                    self.assertTrue(reached.wait(6))
                    requested.set()
                    observed['reply'] = client.post('/api/speech_verification/cancel').json()
                    answered.set()
                watcher = threading.Thread(target=cancel, daemon=True)
                with patch('speech_verification_runtime.get_verification_runtime', return_value={}), \
                     patch('editor_speech_verification.verify_finished_speech',
                           side_effect=lambda path, text, *a, **kw: get_report(
                               path, text, 'A different sentence.',
                               status='corroborated_content_difference')), \
                     patch.object(manager, '_get_speech_staged_version', side_effect=identity):
                    watcher.start()
                    try:
                        response = client.post('/api/chunks/0/speech_verification', json=body)
                    finally:
                        watcher.join(8)
            self.assertFalse(watcher.is_alive())
            self.assertTrue(reached.is_set() and answered.is_set())
            self.assertEqual({'status': 'cancellation requested'}, observed['reply'])
            self.assertNotEqual(200, response.status_code)
            self.assertEqual(before['speech_verification'],
                             manager.load_chunks()[0]['speech_verification'])

    def test_set_member_revalidates_exact_owner_before_move(self):
        # R16: build a real staged set (bytes, identities and parent proofs)
        # with the normal journal writer. Prove an uncancelled control can
        # move, then require cancellation/replacement to refuse before any
        # mutation and leave exact bytes on disk.
        from contextlib import nullcontext
        from functools import partial
        from audio_generation_owner import (AudioGenerationOwner, AudioGenerationCancelled,
                                            ensure_audio_generation_owner)
        from project_scope_exports import publish_export_set_member
        for mode in ('current', 'cancel', 'replace'):
            with self.subTest(mode=mode), self.fixture() as (root, manager, _before, states):
                member, report, target, source, backup = self._seed_set_member(root)
                claims = {'audio': {'id': 'a' * 32, 'phase': 'started'}}
                owner = AudioGenerationOwner(
                    'a' * 32, lambda **kwargs: nullcontext(),
                    validate=partial(core._audio_task_owner_validation, 'a' * 32))
                with patch.object(core, '_task_claims', claims), \
                     patch.object(core, 'process_state', states), \
                     ensure_audio_generation_owner(owner):
                    if mode == 'cancel':
                        states['audio']['cancel'] = True
                    elif mode == 'replace':
                        claims['audio'] = {'id': 'b' * 32, 'phase': 'started'}
                    if mode == 'current':
                        result = publish_export_set_member(root, member, report)
                        self.assertEqual(report['sha256'], result['sha256'])
                        self.assertEqual(b'new', target.read_bytes())
                        self.assertEqual(b'old', backup.read_bytes())
                    else:
                        with self.assertRaises(AudioGenerationCancelled):
                            publish_export_set_member(root, member, report)
                        self.assertEqual(b'old', target.read_bytes())
                        self.assertEqual(b'new', source.read_bytes())

    def _seed_set_member(self, root):
        from export_quality import get_export_file_identity
        from project_scope_exports import read_set_journal, write_set_journal
        target_dir = root / 'chapter_exports'
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / 'manifest.json'
        target.write_bytes(b'old')
        stage = root / 'staging'
        (stage / 'old').mkdir(parents=True, exist_ok=True)
        source = stage / 'manifest.json'
        source.write_bytes(b'new')
        backup = stage / 'old' / 'manifest.json'
        report = get_export_file_identity(source)
        write_set_journal(root, [str(target)], [str(backup)],
                          staged=[report], sources=[str(source)])
        member = read_set_journal(root)['members'][0]
        return member, report, target, source, backup

    def test_cleanup_revalidates_claim_under_chunk_lock(self):
        # Finding 3: the obsolete cleanup worker must revalidate the claim
        # under the chunk mutex immediately before writing, not only before it
        # waits for that mutex. The successor's generating row survives.
        with self.fixture() as (root, manager, before, states):
            read, held, reset_entering = (threading.Event() for _ in range(3))
            result = {}
            def competitor():
                self.assertTrue(read.wait(6))
                with manager._chunks_lock:
                    held.set()
                    self.assertTrue(reset_entering.wait(6))
                    acquired = core._gpu_lock.acquire(timeout=.8)
                    result['replaced_while_reset_waiting'] = acquired
                    if acquired:
                        try:
                            core._task_claims['audio'] = {
                                **core._task_claims['audio'], 'id': 'b' * 32}
                            states['audio']['cancel'] = False
                        finally:
                            core._gpu_lock.release()
            real = manager._update_chunk_fields_by_uid
            def observe(*args, **kwargs):
                if 'status' not in kwargs:
                    value = real(*args, **kwargs)
                    self.assertEqual('generating', value['status'])
                    read.set()
                    self.assertTrue(held.wait(5))
                    return value
                reset_entering.set()
                return real(*args, **kwargs)
            def callback():
                manager._update_generation_fields(before[0], status='generating')
                self.assertEqual({'status': 'cancelling'}, editor._apply_audio_cancel())
                with patch.object(manager, '_update_chunk_fields_by_uid', observe):
                    result['reset'] = manager._reset_captured_generations(
                        {0: before[0]}, [0], set())
            tasks = BackgroundTasks()
            core.schedule_claimed_background_task(tasks, 'audio', callback)
            competitor_thread = threading.Thread(target=competitor, daemon=True)
            competitor_thread.start()
            try:
                asyncio.run(tasks())
            finally:
                competitor_thread.join(8)
            self.assertFalse(competitor_thread.is_alive())
            self.assertTrue(result['replaced_while_reset_waiting'])
            self.assertEqual('generating', manager.load_chunks()[0]['status'])
            self.assertEqual('b' * 32, core._task_claims['audio']['id'])
            self.assertEqual((0, [(0, 'Chunk removed or generation inputs changed during generation')]),
                             result['reset'])

    def test_owned_window_synthesis_cancel_refuses_parent_wav(self):
        # Finding 4: the owned window-synthesis publication must revalidate the
        # live claim/cancel instead of selecting checker=None. A cancel during
        # output admission leaves the prior parent WAV intact.
        from synthesis_render import render_synthesis_windows, get_synthesis_window_plan
        from tests.test_synthesis_render import SynthesisRenderTests
        from tts import ExportCancelled
        with self.fixture() as (root, manager, _before, states):
            writer = SynthesisRenderTests()
            target = root / 'window-parent.wav'
            target.write_bytes(b'prior')
            at, started, acked = (threading.Event() for _ in range(3))
            facts = {}
            real_admission = audio_validation.ensure_audio_output
            @contextlib.contextmanager
            def gated(path):
                with real_admission(path):
                    if Path(path) == target:
                        at.set()
                        self.assertTrue(started.wait(5))
                        facts['ack_before_publish'] = acked.wait(1.0)
                    yield
            def make():
                policy = writer.policy()
                plan = get_synthesis_window_plan('One. Two.', policy, ['local.custom'])
                try:
                    facts['result'] = render_synthesis_windows(
                        plan, lambda text, path: writer.write(path) or True,
                        target, policy, lambda: states['audio']['cancel'])
                except BaseException as error:  # noqa: BLE001 - asserted below
                    facts['error'] = error
            def cancel():
                self.assertTrue(at.wait(8))
                started.set()
                facts['reply'] = editor._apply_audio_cancel()
                acked.set()
            tasks = BackgroundTasks()
            core.schedule_claimed_background_task(tasks, 'audio', make)
            watcher = threading.Thread(target=cancel, daemon=True)
            with patch.object(audio_validation, 'ensure_audio_output', gated):
                watcher.start()
                try:
                    asyncio.run(tasks())
                finally:
                    watcher.join(8)
            self.assertFalse(watcher.is_alive())
            self.assertTrue(at.is_set() and acked.is_set())
            self.assertEqual({'status': 'cancelling'}, facts['reply'])
            self.assertTrue(facts['ack_before_publish'])
            self.assertNotIn('result', facts)
            self.assertIsInstance(facts.get('error'), (ExportCancelled, AudioGenerationCancelled))
            self.assertEqual(b'prior', target.read_bytes())

    def test_mp3_cancel_settles_terminal_api_status_cancelled(self):
        # Finding 5: after a real Editor render reports done, a cancelled merge
        # must settle the public status endpoint as cancelled, not done.
        from concurrent.futures import ThreadPoolExecutor
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import export_file_io as eio
        import project_scope_exports as scope_exports
        from routers import script
        from tests.test_export_quality_api import ExportQualityApiTests
        with self.fixture() as (root, manager, _before, states):
            self.engine(manager)
            self.run_publish()
            api = FastAPI()
            api.include_router(editor.router)
            api.include_router(script.router)
            (root / 'annotated_script.json').write_text(json.dumps(manager.load_chunks()))
            target = root / 'cloned_audiobook.mp3'
            target.write_bytes(b'prior')
            scope_exports.record_export_publication(root, target)
            ledger_path = Path(root) / scope_exports.PUBLICATIONS_FILE
            prior_ledger = ledger_path.read_bytes()
            with TestClient(api) as client, \
                 patch.object(script, 'process_state', states), \
                 patch.object(script, 'DATA_DIR', str(root)):
                self.assertEqual('done', client.get('/api/status/audio').json().get('status'))
                facts = {}
                def initial(source, destination):
                    error = PermissionError('sharing violation')
                    error.winerror = 32
                    raise error
                def lstat(path):
                    if Path(path) == target:
                        with ThreadPoolExecutor(1) as pool:
                            facts['ack'] = pool.submit(client.post, '/api/cancel_audio').result(5)
                    return os.lstat(path)
                def native(*args):
                    facts['native'] = True
                    os.replace(args[1], args[0])
                    return 1
                real = eio.replace_export_output
                def windows(*args, **kwargs):
                    with patch.object(eio, 'os', SimpleNamespace(
                            name='nt', path=os.path, replace=initial, lstat=lstat)), \
                         patch.object(eio, '_windows_api',
                                      return_value=SimpleNamespace(ReplaceFileW=native)):
                        return real(*args, **kwargs)
                with patch.object(project, 'replace_export_output', windows):
                    response = ExportQualityApiTests().merge(client, {})
                status = client.get('/api/status/audio').json()
            self.assertEqual(200, response.status_code)
            self.assertEqual((200, {'status': 'cancelling'}),
                             (facts['ack'].status_code, facts['ack'].json()))
            self.assertNotIn('native', facts)
            self.assertEqual(b'prior', target.read_bytes())
            self.assertEqual(prior_ledger, ledger_path.read_bytes())
            self.assertIsNone(scope_exports.read_publication_journal(root))
            self.assertEqual('cancelled', status.get('status'))
            self.assertFalse(status.get('running'))
            self.assertEqual('Merge cancelled', status['logs'][-1])

    def test_request_cancel_vetoes_sound_approval_swap(self):
        # Finding 6: the HTTP request-cancellation event is independent of the
        # task owner's flag and must veto the approval swap at its final guard.
        from tests.test_sound_design_approval import SoundApprovalTests
        from sound_design_approval import save_sound_candidate_approval
        from tts import ExportCancelled
        case = SoundApprovalTests()
        case.setUp()
        try:
            saved = {}
            def setup(stage, revision):
                saved.update(stage=stage, revision=revision,
                             approval=case.approve(stage, revision, full_mix_reviewed=True))
            case.run_owned(setup)
            stage, revision = saved['stage'], saved['revision']
            target = stage / 'approval.json'
            prior = target.read_bytes()
            cancelled = threading.Event()
            real_fsync = os.fsync
            def synced(fd):
                result = real_fsync(fd)
                frame = sys._getframe(1)
                if str(frame.f_locals.get('path')) == str(target):
                    cancelled.set()
                return result
            def republish():
                save_sound_candidate_approval(
                    case.root, stage.name, revision,
                    {'format': 'wav', 'export_quality': None, 'mastering': None},
                    full_mix_reviewed=True,
                    expected_approval=saved['approval']['approval_revision'],
                    cancel_check=cancelled.is_set)
            with patch('adapter_publication.os.fsync', synced), \
                 self.assertRaises((ExportCancelled, AudioGenerationCancelled)):
                asyncio.run(core.run_claimed_task_worker('audio', republish, cpu_only=True))
            self.assertTrue(cancelled.is_set())
            self.assertEqual(prior, target.read_bytes())
        finally:
            case.doCleanups()

    def test_event_only_external_writer_refuses_windows_fallback(self):
        # Finding 7: the legacy Event passed by external-engine writers must be
        # honored after the fallback's blocking lstat, not only on entry.
        import export_file_io as eio
        from tests.test_synthesis_render import SynthesisRenderTests
        from tts import TTSEngine
        for code in (5, 32):
            with self.subTest(winerror=code), self.fixture() as (root, manager, _before, _states):
                writer = SynthesisRenderTests()
                target = root / 'external-target.wav'
                target.write_bytes(b'prior')
                source = root / 'valid-response.wav'
                writer.write(source)
                at, started, acked = (threading.Event() for _ in range(3))
                event = threading.Event()
                facts = {'winerror': code}
                def block():
                    at.set()
                    self.assertTrue(started.wait(5))
                    facts['ack_before_publish'] = acked.wait(1.0)
                def cancel():
                    self.assertTrue(at.wait(8))
                    started.set()
                    event.set()
                    acked.set()
                def fault(src, dst):
                    error = PermissionError('modeled sharing error')
                    error.winerror = code
                    raise error
                def info(path):
                    if Path(path) == target:
                        block()
                    return os.lstat(path)
                def native(dst, src, *unused):
                    facts['ReplaceFileW'] = True
                    os.replace(src, dst)
                    return 1
                original = audio_validation.replace_export_output
                def windows_writer(src, dst, **options):
                    with patch.object(eio, 'os', SimpleNamespace(
                            name='nt', path=os.path, lstat=info, replace=fault)), \
                         patch.object(eio, '_windows_api',
                                      return_value=SimpleNamespace(ReplaceFileW=native)):
                        return original(src, dst, **options)
                tasks = BackgroundTasks()
                def make():
                    facts['success'] = TTSEngine._save_external_audio(source, target, event)
                core.schedule_claimed_background_task(tasks, 'audio', make)
                watcher = threading.Thread(target=cancel, daemon=True)
                # Cancel during the fallback's second lstat, not at output
                # admission: only the fixed writer rechecks the Event there.
                with patch.object(audio_validation, 'replace_export_output', windows_writer):
                    watcher.start()
                    try:
                        asyncio.run(tasks())
                    finally:
                        watcher.join(8)
                self.assertFalse(watcher.is_alive())
                self.assertTrue(at.is_set() and acked.is_set())
                self.assertTrue(facts['ack_before_publish'])
                self.assertFalse(facts['success'])
                self.assertNotIn('ReplaceFileW', facts)
                self.assertEqual(b'prior', target.read_bytes())

    def test_journal_append_revalidates_after_descriptor_open(self):
        # F01: a stop acknowledged after the real append descriptor is open
        # but before the record is written must refuse the update. Calling the
        # guard before open (the reviewed behaviour) misses this window.
        import chunk_status_journal as journal_module
        from audio_generation_owner import AudioGenerationCancelled
        with self.fixture() as (root, manager, before, states):
            journal = manager._get_status_journal_locked()
            journal.ensure_writable()
            path = journal.path
            original_bytes = path.read_bytes()
            opened = {'descriptor': False}
            real_open = Path.open

            def tracking_open(target, *args, **kwargs):
                stream = real_open(target, *args, **kwargs)
                if Path(target) == path and args and args[0] == 'ab':
                    opened['descriptor'] = True
                return stream

            def guard():
                self.assertTrue(opened['descriptor'],
                                'guard ran before the real append descriptor existed')
                raise AudioGenerationCancelled()

            with patch.object(Path, 'open', tracking_open):
                with self.assertRaises(AudioGenerationCancelled):
                    journal.apply_update(before[0]['uid'], {'status': 'done'},
                                         before_append=guard)
            self.assertEqual(original_bytes, path.read_bytes())

    def test_set_forward_move_revalidates_after_inner_preparation(self):
        # F03: cancellation acknowledged after the inner source proof must
        # still refuse the first exclusive move. Without the lowest-boundary
        # validator the predecessor is moved and rolled back (ctime/ledger
        # churn) even though the stop was acknowledged.
        from contextlib import nullcontext
        from functools import partial
        from audio_generation_owner import (AudioGenerationOwner,
                                            AudioGenerationCancelled,
                                            ensure_audio_generation_owner)
        import project_scope_exports as ex
        with self.fixture() as (root, manager, _before, states):
            member, report, target, source, backup = self._seed_set_member(root)
            claims = {'audio': {'id': 'a' * 32, 'phase': 'started'}}
            owner = AudioGenerationOwner(
                'a' * 32, lambda **kwargs: nullcontext(),
                validate=partial(core._audio_task_owner_validation, 'a' * 32))
            real = ex._require_set_version

            def inner(root_, name_, *args, **kwargs):
                info = real(root_, name_, *args, **kwargs)
                if sys._getframe(1).f_code is ex._move_guarded_set_file.__code__:
                    states['audio']['cancel'] = True
                return info

            with patch.object(core, '_task_claims', claims), \
                 patch.object(core, 'process_state', states), \
                 patch.object(ex, '_require_set_version', inner), \
                 ensure_audio_generation_owner(owner):
                with self.assertRaises(AudioGenerationCancelled):
                    ex.publish_export_set_member(root, member, report)
            self.assertEqual(b'old', target.read_bytes())
            self.assertEqual(b'new', source.read_bytes())

    def test_event_set_during_owner_wait_refuses_publication(self):
        # F05: an independent Event set while the writer waits on the real
        # owner validator must refuse publication. Sampling the Event before
        # that blocking wait (the reviewed order) loses the stop.
        from audio_generation_owner import AudioGenerationOwner, ensure_audio_generation_owner
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source, destination = tmp / 'source', tmp / 'destination'
            source.write_bytes(b'new')
            destination.write_bytes(b'prior')
            event, entered, release = (threading.Event() for _ in range(3))

            def validate():
                entered.set()
                self.assertTrue(release.wait(5))

            owner = AudioGenerationOwner(
                'a' * 32, lambda **kwargs: nullcontext(), validate=validate)

            def stop():
                self.assertTrue(entered.wait(5))
                event.set()
                release.set()

            worker = threading.Thread(target=stop)
            with ensure_audio_generation_owner(owner):
                worker.start()
                try:
                    published = audio_validation.publish_audio_output(
                        str(source), str(destination), cancelled=event)
                finally:
                    worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(published)
            self.assertEqual(b'prior', destination.read_bytes())

    def test_refused_audio_reservation_restores_prior_lifecycle(self):
        # F07: a reservation that never starts worker-owned publication (a
        # refused admission here) must restore the lifecycle it displaced, not
        # leave status=running with no claim.
        with self.fixture() as (root, manager, before, states):
            states['audio'].update(running=False, status='done', error=None)
            with patch.object(core, 'process_state', states), \
                 patch.object(editor, 'process_state', states):
                claim = core.reserve_background_task('audio')
                self.assertEqual('running', states['audio']['status'])
                core.release_gpu_task_claim('audio', claim, pending_only=True)
            self.assertFalse(states['audio']['running'])
            self.assertEqual('done', states['audio']['status'])
            self.assertIsNone(states['audio'].get('error'))

    def test_single_worker_completion_does_not_overwrite_successor(self):
        # F08: an exact claim replacement is the explicit fault model. A
        # superseded single-chunk worker must not publish a terminal status,
        # clear the successor's cancel flag or touch its running bit.
        with self.fixture() as (root, manager, before, states):
            self.engine(manager)
            real = audio_validation.replace_export_output

            def hook(source, destination, **kwargs):
                if '.slot-' in str(destination):
                    with core._gpu_lock:
                        core._task_claims['audio'] = {
                            **core._task_claims['audio'], 'id': 'a' * 32}
                        states['audio'].update(status='running', error=None,
                                               cancel=True, running=True,
                                               logs=['successor'])
                return real(source, destination, **kwargs)

            try:
                with patch.object(audio_validation, 'replace_export_output', hook):
                    self.run_publish()
            except AudioGenerationCancelled:
                pass
            self.assertEqual('a' * 32, core._task_claims['audio']['id'])
            self.assertEqual('running', states['audio']['status'])
            self.assertTrue(states['audio']['running'])
            self.assertTrue(states['audio']['cancel'])
            self.assertEqual(['successor'], states['audio']['logs'])
            self.assertEqual(before[0]['audio_revision'],
                             manager.load_chunks()[0]['audio_revision'])


def _run_speech_lock_schedule():
    """Drive both real routes and require the intended lock gates.

    The Editor holds the chunk mutex at output admission. The speech worker
    must be observed attempting the real chunk read *before* the Editor is
    released, proving both participants reached the gates rather than the
    speech route exiting early with an unrelated HTTP 409. A genuine cycle is
    reported as ``live lock cycle`` instead of hanging indefinitely.
    """
    import os
    import traceback

    from fastapi import HTTPException
    from routers import editor as editor_routes

    case = AudioPublishMutex2163Tests()
    inverted = os.environ.get('AA2_SPEECH_ORACLE_INVERT') == '1'
    with case.fixture() as (root, manager, before, states):
        case.engine(manager)
        token = editor_routes.get_snapshot_digest(editor_routes.get_editor_book_identity(root))
        request = editor_routes.SpeechVerificationRequest(
            expected_book_token=token, expected_uid=before[0]['uid'],
            expected_audio_revision=before[0]['audio_revision'])
        at_output = threading.Event()
        speech_attempt = threading.Event()
        observed = {}
        real_admission = audio_validation.ensure_audio_output
        real_load = manager.load_chunks

        @contextlib.contextmanager
        def gated(path):
            with real_admission(path):
                if '.slot-' in str(path):
                    observed['output_gate_chunks_locked'] = manager._chunks_lock.locked()
                    at_output.set()
                    assert speech_attempt.wait(10), (
                        'speech worker never attempted the real chunk read')
                yield

        def is_speech_worker():
            owner = core.get_audio_generation_owner()
            claim = core._task_claims.get('speech_verification') or {}
            return (owner is not None and claim.get('phase') == 'started'
                    and claim.get('id') == owner.claim_id)

        def load():
            if is_speech_worker():
                # Record the executor thread that actually attempts the chunk
                # lock, not the ASGI caller that awaits it (F11).
                observed.setdefault('speech_thread', threading.get_ident())
                # Record the first real chunk-read attempt, which the Editor
                # gate is holding the chunk mutex across.
                observed.setdefault('speech_read_chunks_locked',
                                    manager._chunks_lock.locked())
                speech_attempt.set()
                if inverted:
                    # Injected r5-style inversion: hold the task mutex across
                    # the chunk read so the Editor's final owner snapshot on
                    # the chunk-holding thread cannot proceed.
                    core._gpu_lock.acquire()
                    try:
                        return real_load()
                    finally:
                        core._gpu_lock.release()
            return real_load()

        writer_errors, reader_errors, reader_results = [], [], []

        def publish():
            try:
                case.run_publish()
            except BaseException as exc:  # noqa: BLE001 - reported below
                writer_errors.append(exc)

        def recheck():
            try:
                reader_results.append(asyncio.run(
                    editor_routes.recheck_chunk_speech(0, request)))
            except BaseException as exc:  # noqa: BLE001 - classified below
                reader_errors.append(exc)

        def stack_text(thread):
            frame = sys._current_frames().get(thread.ident)
            return '' if frame is None else ''.join(traceback.format_stack(frame))

        def live_stacks():
            # Both participants run in pool executor threads, not the caller
            # threads and not the ASGI caller that awaits them. Locate the two
            # waiting frames by their actual lock sites (F11).
            found = []
            for ident, frame in sys._current_frames().items():
                text = ''.join(traceback.format_stack(frame))
                if '_gpu_lock' in text and 'core.py' in text:
                    found.append(('task-mutex waiter', ident, text))
                elif '_chunks_lock' in text and 'project.py' in text:
                    found.append(('chunk-mutex waiter', ident, text))
            return found

        def report_cycle(writer, reader):
            stacks = live_stacks()
            task_waiter = next((item for item in stacks if item[0] == 'task-mutex waiter'), None)
            chunk_waiter = next((item for item in stacks if item[0] == 'chunk-mutex waiter'), None)
            cycle = (task_waiter is not None and chunk_waiter is not None
                     and task_waiter[1] != chunk_waiter[1])
            # Use distinct structured markers and exit codes: the parent gates
            # must accept a genuine cycle, reject an unrelated stall, and never
            # pass merely because the word "cycle" appears in a diagnostic.
            if cycle:
                message = (
                    'SPEECH_LOCK_ORACLE CYCLE\n'
                    'live lock cycle: one participant blocks on the task mutex '
                    'while the other blocks on the chunk mutex\n'
                    f'--- task-mutex waiter ---\n{task_waiter[2]}\n'
                    f'--- chunk-mutex waiter ---\n{chunk_waiter[2]}')
                code = 2
            else:
                message = (
                    'SPEECH_LOCK_ORACLE NO_CYCLE\n'
                    'speech/Editor workers did not return and no live lock cycle '
                    f'was observed\n--- Editor thread ---\n{stack_text(writer)}\n'
                    f'--- speech thread ---\n{stack_text(reader)}')
                code = 3
            # A real cycle leaves the task mutex held by a blocked thread, so
            # fixture cleanup (which takes it) would hang. Report and exit the
            # child directly; this oracle is always run out-of-process.
            print(message, flush=True)
            os._exit(code)

        writer = threading.Thread(target=publish, daemon=True)
        reader = threading.Thread(target=recheck, daemon=True)
        with patch.object(audio_validation, 'ensure_audio_output', gated), \
             patch.object(manager, 'load_chunks', load), \
             patch('speech_verification_runtime.get_verification_runtime', return_value={}):
            # Start the Editor first: it holds the chunk mutex at its output
            # gate and waits there until the speech worker is observed inside
            # the real chunk read, so the interleaving is deterministic.
            writer.start()
            assert at_output.wait(10), 'Editor never reached output admission'
            reader.start()
            writer.join(6)
            reader.join(6)
            if writer.is_alive() or reader.is_alive():
                report_cycle(writer, reader)

        assert at_output.is_set(), 'Editor never reached output admission'
        assert observed.get('output_gate_chunks_locked') is True, (
            'Editor did not hold the chunk mutex at its output gate')
        assert speech_attempt.is_set(), 'speech route never attempted the chunk read'
        assert observed.get('speech_read_chunks_locked') is True, (
            'speech attempted the chunk read without the Editor holding it')
        assert not writer_errors, f'Editor publish raised: {writer_errors!r}'
        assert len(reader_errors) == 1, (
            f'speech route must report exactly one changed-take conflict, got {reader_errors!r}')
        error = reader_errors[0]
        assert isinstance(error, HTTPException) and error.status_code == 409, (
            f'speech route returned an unexpected failure: {error!r}')
        # Accept only the exact changed-take conflicts the real race can
        # produce, not any 409 that happens to contain "changed" (F11).
        assert error.detail in (
            'The selected audio take changed. Refresh Editor.',
            'The selected line, input or take changed. Refresh Editor.',
        ), f'speech route failed for an unrelated reason: {error.detail!r}'
        row = manager.load_chunks()[0]
        assert row['status'] == 'done', 'Editor publish did not commit'
        assert row['audio_revision'] != before[0]['audio_revision'], (
            'Editor publish did not install a new revision')
        print('SPEECH_LOCK_ORACLE OK', flush=True)
