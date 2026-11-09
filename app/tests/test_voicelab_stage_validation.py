"""Voice Lab preflight and start must accept the same stage selection."""
import asyncio
import copy
import core
import inspect
import sys
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI, BackgroundTasks
from fastapi.testclient import TestClient
from routers import voicelab as v


class VoiceLabStageValidationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_support import assume_ample_free_disk
        assume_ample_free_disk(self)
        state = copy.deepcopy(core.process_state)
        for value in state.values():
            value['running'] = False
        for owner, name, value in ((core, 'process_state', state), (v, 'process_state', state),
                                   (core, '_task_claims', {}), (core, '_gpu_leases', {})):
            context = patch.object(owner, name, value)
            context.start()
            self.addCleanup(context.stop)
        for context in (patch.object(core, 'acquire_gpu_lock', return_value=None),
                        patch.object(core, 'llm_is_on_this_gpu', return_value=True)):
            context.start()
            self.addCleanup(context.stop)
        self.addCleanup(core.release_pending_task_claims)

    def test_unknown_and_empty_stages_fail_before_preflight_or_gpu_work(self):
        app = FastAPI()
        app.include_router(v.router)
        with patch.object(v, '_load_voicelab_config', return_value={}), \
             patch.object(v, '_resolve_zips_dir', side_effect=AssertionError('invalid stages reached filesystem')) as paths, \
             patch.object(v, '_probe_voicelab_interpreter', side_effect=AssertionError('invalid stages reached interpreter')) as probe, \
             patch.object(core, 'claim_gpu_task', side_effect=AssertionError('invalid stages reached claim')) as claim, \
             TestClient(app) as client:
            for stages, detail in ((['typo'], 'Unknown stage(s): typo'),
                                   (['name', 'typo'], 'Unknown stage(s): typo'),
                                   (['bad', 'bad'], 'Unknown stage(s): bad, bad'),
                                   ([], 'No stages selected.')):
                for endpoint in ('preflight', 'start'):
                    with self.subTest(stages=stages, endpoint=endpoint):
                        response = client.post('/api/voicelab/' + endpoint,
                                               json={'stages': stages, 'zips_dir': '/must-not-be-read'})
                        self.assertEqual(400, response.status_code, response.text)
                        self.assertEqual(detail, response.json()['detail'])
            paths.assert_not_called()
            probe.assert_not_called()
            claim.assert_not_called()

    def test_valid_preflight_keeps_existing_token_gate_and_request_unchanged(self):
        app = FastAPI()
        app.include_router(v.router)
        with tempfile.TemporaryDirectory() as tmp, patch.object(v, 'DATA_DIR', tmp), \
             patch.object(v, '_load_voicelab_config', return_value={'zips_dir': tmp}), \
             patch.object(core, 'claim_gpu_task') as claim, TestClient(app) as client:
            preflight = client.post('/api/voicelab/preflight', json={'stages': ['name']})
            self.assertEqual(200, preflight.status_code, preflight.text)
            self.assertTrue(preflight.json()['ready'], preflight.json())
            response = client.post('/api/voicelab/start', json={'stages': ['name']})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual('Review the Voice Lab preflight before starting.', response.json()['detail'])
            claim.assert_not_called()

    def test_same_path_input_replacement_invalidates_the_reviewed_preflight(self):
        # #1803: same path, same counts, different bytes must change the token,
        # and START must refuse the old review. An untouched rebuild keeps it.
        import os
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp, patch.object(v, 'DATA_DIR', tmp):
            root = Path(tmp)
            (root / 'Narrator').mkdir(); (root / '_deduped').mkdir()
            model = root / 'model.gguf'
            files = {'narrator zip': root / 'Narrator' / 'a.zip', 'deduped zip': root / '_deduped' / 'a.zip',
                     'profiler model': model}
            for path in files.values():
                path.write_bytes(b'original')
                os.utime(path, ns=(1_000_000_000, 1_000_000_000))
            cfg = {'zips_dir': tmp, 'rocm_python': sys.executable, 'profiler_model': str(model), 'epub_dirs': []}
            runtime = {'deps': {'librosa': True, 'peft': True, 'llama_cpp': True},
                       'gpu': 'fixture', 'vram': [32*1024**3, 1024**3]}
            def build():
                request = v.VoiceLabRequest(stages=['quality', 'profile'], zips_dir=tmp, device='cpu')
                report = v._build_voicelab_preflight(request, cfg)
                self.assertTrue(report['ready'], report)
                return report['preflight_id']
            with patch.object(v, '_probe_voicelab_interpreter', return_value=runtime), \
                 patch.object(v, '_run_profiler_preflight', return_value=None):
                reviewed = build()
                self.assertEqual(reviewed, build(), 'an untouched rebuild must keep the token')
                for label, path in files.items():
                    with self.subTest(replaced=label):
                        before = build()
                        # Same path, size and counts, and the old timestamp restored,
                        # as robocopy / cp -p / rsync -a / unzip leave it.
                        path.write_bytes(b'replaced')
                        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
                        self.assertNotEqual(before, build(), f'{label} replacement kept the token')
                stale = v.VoiceLabRequest(stages=['quality', 'profile'], zips_dir=tmp, device='cpu',
                                          preflight_id=reviewed)
                with patch.object(v, '_load_voicelab_config', return_value=cfg), \
                     patch.object(core, 'claim_gpu_task', side_effect=AssertionError('stale review admitted')):
                    with self.assertRaises(v.HTTPException) as caught:
                        asyncio.run(v.voicelab_start(stale, BackgroundTasks()))
                self.assertEqual(409, caught.exception.status_code)
                self.assertIn('inputs changed', caught.exception.detail)

    def test_canonical_copy_reaches_commands_and_queued_worker_without_input_mutation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(v, 'DATA_DIR', tmp):
            cfg = {'zips_dir': tmp, 'rocm_python': sys.executable}
            request = v.VoiceLabRequest(stages=['name', 'quality', 'name'], zips_dir=tmp, device='cpu')
            before = request.model_dump()
            runtime = {'deps': {'librosa': True, 'peft': True}, 'gpu': 'fixture', 'vram': [32*1024**3, 1024**3]}
            with patch.object(v, '_probe_voicelab_interpreter', return_value=runtime):
                report = v._build_voicelab_preflight(request, cfg)
                self.assertTrue(report['ready'], report)
                self.assertEqual(['quality', 'name'], report['stages'])
                self.assertEqual(before, request.model_dump())
                request.preflight_id = report['preflight_id']
                before = request.model_dump()
                background = BackgroundTasks()
                def commands(canonical, *args):
                    self.assertIsNot(request, canonical)
                    self.assertEqual(['quality', 'name'], canonical.stages)
                    return [(stage, ['cpu-fixture'], tmp, {}) for stage in canonical.stages]
                with patch.object(v, '_load_voicelab_config', return_value=cfg), \
                     patch.object(v, '_voicelab_build_commands', side_effect=commands) as build, \
                     patch.object(v, 'check_global_gpu_lock') as guard, \
                     patch.object(v, 'list_adapters_needing_recovery', return_value=[]), \
                     patch.object(core, 'claim_gpu_task', wraps=core.claim_gpu_task) as claim:
                    core.process_state['voicelab']['zips_dir'] = 'old-folder'
                    response = asyncio.run(v.voicelab_start(request, background))
                self.assertEqual(['quality', 'name'], response['stages'])
                self.assertEqual(tmp, core.process_state['voicelab']['zips_dir'])
                self.assertEqual(tmp, response['zips_dir'])
                self.assertEqual(before, request.model_dump())
                build.assert_called_once()
                guard.assert_called_once_with('voicelab')
                claim.assert_called_once_with('voicelab')
                self.assertEqual(1, len(background.tasks))
                self.assertEqual('voicelab', background.tasks[0].args[0])
                self.assertTrue(core.is_task_running('voicelab'))
                self.assertEqual(core._task_claims['voicelab']['id'], background.tasks[0].args[1])
                worker = background.tasks[0].args[2].args[1]
                captured = inspect.getclosurevars(worker).nonlocals
                self.assertEqual(['quality', 'name'], captured['request'].stages)
                self.assertEqual(['quality', 'name'], [step[0] for step in captured['steps']])
                self.assertEqual(before, request.model_dump())
