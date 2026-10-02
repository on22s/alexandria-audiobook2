"""Exercise LoRA route claims and preview publication without GPU inference."""

import asyncio
import copy
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
import numpy as np
import soundfile as sf

import core
from routers import lora


class LoraRouteSafetyTests(unittest.TestCase):
    def get_state(self):
        state = copy.deepcopy(core.process_state)
        for entry in state.values():
            entry['running'] = False
        return state

    def test_training_registers_metadata_only_after_successful_uncancelled_subprocess(self):
        from fastapi import BackgroundTasks
        for rc, cancelled, registered in ((0, False, True), (17, False, False),
                                           (0, True, False), (None, False, False)):
            with self.subTest(rc=rc, cancelled=cancelled), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp); datasets = root / 'datasets'; models = root / 'models'
                (datasets / 'dataset').mkdir(parents=True)
                manifest = models / 'manifest.json'
                state = self.get_state()
                stack.enter_context(patch.object(lora, 'LORA_DATASETS_DIR', str(datasets)))
                stack.enter_context(patch.object(lora, 'LORA_MODELS_DIR', str(models)))
                stack.enter_context(patch.object(lora, 'LORA_MODELS_MANIFEST', str(manifest)))
                stack.enter_context(patch.object(lora, 'process_state', state))
                stack.enter_context(patch.object(core, 'process_state', state))
                stack.enter_context(patch.object(lora, 'project_manager', SimpleNamespace(engine=None)))
                stack.enter_context(patch.object(lora, 'check_global_gpu_lock'))
                stack.enter_context(patch.object(lora, 'claim_gpu_task'))
                stack.enter_context(patch.object(lora, 'get_unique_id', return_value='fixture'))
                def train(_command, task):
                    output = models / 'fixture'; output.mkdir(parents=True,exist_ok=True)
                    (output / 'training_meta.json').write_text(json.dumps({'epochs':1,'num_samples':2}))
                    state[task]['cancel'] = cancelled
                    return rc
                stack.enter_context(patch.object(lora, 'run_process', side_effect=train))
                background = BackgroundTasks()
                response = asyncio.run(lora.lora_start_training(
                    lora.LoraTrainingRequest(name='Voice', dataset_id='dataset'), background))
                self.assertEqual('fixture', response['adapter_id'])
                asyncio.run(background())
                self.assertTrue((models / 'fixture/training_meta.json').is_file())
                if registered:
                    self.assertEqual(['fixture'], [r['id'] for r in json.loads(manifest.read_text())])
                else:
                    self.assertFalse(manifest.exists(), 'failed training must not publish the adapter')

    def test_adapter_delete_holds_gpu_claim_and_unloads_engine_before_removing_files(self):
        for cleanup_error in (False, True):
            with self.subTest(cleanup_error=cleanup_error), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp); adapter = root / 'voice'; adapter.mkdir()
                data = root / 'data'; data.mkdir(); (data / 'scripts').mkdir()
                for name, value in (('VOICE_CONFIG_PATH', str(data / 'voice_config.json')),
                                    ('VOICE_LIBRARY_PATH', str(data / 'voice_library.json')),
                                    ('SCRIPTS_DIR', str(data / 'scripts'))):
                    stack.enter_context(patch.object(lora, name, value))
                (adapter / 'adapter.safetensors').write_bytes(b'fixture')
                manifest = root / 'manifest.json'; manifest.write_text(json.dumps([{'id':'voice'}]))
                state = self.get_state(); manager = SimpleNamespace(engine=object())
                for module in (core, lora):
                    stack.enter_context(patch.object(module, 'process_state', state))
                stack.enter_context(patch.object(lora, 'LORA_MODELS_DIR', str(root)))
                stack.enter_context(patch.object(lora, 'LORA_MODELS_MANIFEST', str(manifest)))
                stack.enter_context(patch.object(lora, 'project_manager', manager))
                stack.enter_context(patch.object(lora, '_load_builtin_lora_manifest', return_value=[]))
                stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=True))
                def collect():
                    self.assertTrue(state['lora_training']['running'])
                    self.assertIsNone(manager.engine)
                    if cleanup_error:
                        raise RuntimeError('cleanup failed')
                cleanup = stack.enter_context(patch.object(lora.gc, 'collect', side_effect=collect))
                real_delete = lora.shutil.rmtree
                def delete(path):
                    self.assertTrue(state['lora_training']['running'])
                    self.assertIsNone(manager.engine)
                    return real_delete(path)
                remove = stack.enter_context(patch.object(lora.shutil, 'rmtree', side_effect=delete))
                if cleanup_error:
                    with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                        asyncio.run(lora.lora_delete_model('voice'))
                    remove.assert_not_called()
                    self.assertTrue(adapter.is_dir())
                    self.assertEqual([{'id':'voice'}], json.loads(manifest.read_text()))
                else:
                    self.assertEqual('deleted', asyncio.run(lora.lora_delete_model('voice'))['status'])
                    self.assertFalse(adapter.exists())
                    self.assertEqual([], json.loads(manifest.read_text()))
                cleanup.assert_called_once()
                self.assertFalse(state['lora_training']['running'])

    def test_adapter_delete_refuses_when_an_audio_task_owns_the_gpu(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); adapter = root / 'voice'; adapter.mkdir()
            manifest = root / 'manifest.json'; manifest.write_text(json.dumps([{'id':'voice'}]))
            state = self.get_state(); state['audio']['running'] = True
            engine = object(); manager = SimpleNamespace(engine=engine)
            for module in (core, lora):
                stack.enter_context(patch.object(module, 'process_state', state))
            stack.enter_context(patch.object(lora, 'LORA_MODELS_DIR', str(root)))
            stack.enter_context(patch.object(lora, 'LORA_MODELS_MANIFEST', str(manifest)))
            stack.enter_context(patch.object(lora, 'project_manager', manager))
            stack.enter_context(patch.object(lora, '_load_builtin_lora_manifest', return_value=[]))
            stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=True))
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(lora.lora_delete_model('voice'))
            self.assertEqual(400, raised.exception.status_code)
            self.assertIs(engine, manager.engine)
            self.assertTrue(adapter.is_dir())
            self.assertEqual([{'id':'voice'}], json.loads(manifest.read_text()))

    def test_checkpoint_changes_hold_claim_through_engine_cleanup(self):
        routes = [(lora.lora_promote_candidate, '_promote_lora_candidate'),
                  (lora.lora_rollback_promotion, '_rollback_lora_promotion'),
                  (lora.lora_recover_checkpoint_swap, '_recover_checkpoint_swap')]
        for route, operation_name in routes:
            for outcome in ['success', 'operation_error', 'cleanup_error']:
                with self.subTest(route=route.__name__, outcome=outcome), ExitStack() as stack:
                    state = self.get_state()
                    engine = object()
                    manager = SimpleNamespace(engine=engine)
                    stack.enter_context(patch.object(core, 'process_state', state))
                    stack.enter_context(patch.object(lora, 'process_state', state))
                    stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=True))
                    stack.enter_context(patch.object(lora, 'project_manager', manager))

                    def operation(*_args, **kwargs):
                        self.assertEqual({'expected_candidate_id': None} if operation_name == '_promote_lora_candidate' else {}, kwargs)
                        self.assertTrue(state['lora_training']['running'])
                        if outcome == 'operation_error':
                            raise RuntimeError('checkpoint change failed')
                        return {'candidate':'candidate'}

                    def collect():
                        self.assertTrue(state['lora_training']['running'])
                        self.assertIsNone(manager.engine)
                        if outcome == 'cleanup_error':
                            raise RuntimeError('cleanup failed')

                    stack.enter_context(patch.object(lora, operation_name, side_effect=operation))
                    cleanup = stack.enter_context(patch.object(lora.gc, 'collect', side_effect=collect))
                    if outcome == 'success':
                        asyncio.run(route('voice'))
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'failed'):
                            asyncio.run(route('voice'))
                    self.assertFalse(state['lora_training']['running'])
                    if outcome == 'operation_error':
                        cleanup.assert_not_called()
                        self.assertIs(manager.engine, engine)
                    else:
                        cleanup.assert_called_once()

    def test_checkpoint_routes_already_reject_other_gpu_tasks(self):
        state = self.get_state()
        state['audio']['running'] = True
        with ExitStack() as stack:
            stack.enter_context(patch.object(core, 'process_state', state))
            stack.enter_context(patch.object(lora, 'process_state', state))
            stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=True))
            for route, operation_name in [
                    (lora.lora_promote_candidate, '_promote_lora_candidate'),
                    (lora.lora_rollback_promotion, '_rollback_lora_promotion'),
                    (lora.lora_recover_checkpoint_swap, '_recover_checkpoint_swap')]:
                with self.subTest(route=route.__name__), patch.object(lora, operation_name) as operation:
                    with self.assertRaises(HTTPException) as raised:
                        asyncio.run(route('voice'))
                    self.assertEqual(400, raised.exception.status_code)
                    self.assertFalse(state['lora_training']['running'])
                    operation.assert_not_called()

    def test_failed_preview_cannot_publish_partial_cache_and_next_attempt_recovers(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            adapter = Path(tmp, 'voice'); adapter.mkdir()
            preview = adapter / 'preview_sample.wav'
            state = self.get_state()
            calls = []
            samples = np.full(4000, 0.1, dtype='float32')

            def generate(output_path, **_kwargs):
                self.assertTrue(state['lora_test']['running'])
                calls.append(output_path)
                if len(calls) == 1:
                    Path(output_path).write_bytes(b'partial output')
                    raise RuntimeError('synthesis failed')
                sf.write(output_path, samples, 16000)

            engine = SimpleNamespace(generate_voice=generate)
            manager = SimpleNamespace(get_engine=Mock(return_value=engine))
            stack.enter_context(patch.object(core, 'process_state', state))
            stack.enter_context(patch.object(lora, 'process_state', state))
            stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=True))
            stack.enter_context(patch.object(lora, 'LORA_MODELS_DIR', tmp))
            stack.enter_context(patch.object(lora, '_load_builtin_lora_manifest', return_value=[]))
            stack.enter_context(patch.object(lora, '_load_manifest', return_value=[{'id':'voice'}]))
            stack.enter_context(patch.object(lora, 'project_manager', manager))
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(lora.lora_preview('voice'))
            self.assertEqual(500, raised.exception.status_code)
            self.assertFalse(preview.exists())
            self.assertEqual([], list(adapter.iterdir()))
            self.assertFalse(state['lora_test']['running'])
            result = asyncio.run(lora.lora_preview('voice'))
            self.assertEqual('generated', result['status'])
            actual, rate = sf.read(preview)
            self.assertEqual(16000, rate)
            np.testing.assert_allclose(samples, actual, atol=1e-4)
            self.assertEqual([preview], list(adapter.iterdir()))
            self.assertFalse(state['lora_test']['running'])
            # A valid cache remains usable while another GPU task is running.
            state['audio']['running'] = True
            self.assertEqual('cached', asyncio.run(lora.lora_preview('voice'))['status'])
            self.assertEqual(2, len(calls))

    def test_legacy_invalid_preview_is_rebuilt_instead_of_served(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            adapter = Path(tmp, 'voice'); adapter.mkdir()
            preview = adapter / 'preview_sample.wav'; preview.write_bytes(b'old incomplete output')
            state = self.get_state()
            def generate(output_path, **_kwargs):
                sf.write(output_path, np.full(4000, 0.1), 16000)
            generate = Mock(side_effect=generate)
            stack.enter_context(patch.object(core, 'process_state', state))
            stack.enter_context(patch.object(lora, 'process_state', state))
            stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=True))
            stack.enter_context(patch.object(lora, 'LORA_MODELS_DIR', tmp))
            stack.enter_context(patch.object(lora, '_load_builtin_lora_manifest', return_value=[]))
            stack.enter_context(patch.object(lora, '_load_manifest', return_value=[{'id':'voice'}]))
            stack.enter_context(patch.object(lora, 'project_manager', SimpleNamespace(
                get_engine=lambda: SimpleNamespace(generate_voice=generate))))
            self.assertEqual('generated', asyncio.run(lora.lora_preview('voice'))['status'])
            generate.assert_called_once()
            self.assertEqual(4000, sf.info(preview).frames)
            self.assertFalse(state['lora_test']['running'])


if __name__ == '__main__':
    unittest.main()


class BuiltinLoraDispatchEquivalenceTests(unittest.TestCase):
    def test_builtin_test_route_and_saved_builtin_assignment_use_same_real_audio_path(self):
        import contextlib
        import io
        import sys
        from types import ModuleType
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import tts
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            adapter = root/'builtin_fixture'
            from tests.test_support import write_test_adapter
            write_test_adapter(adapter)
            reference = np.sin(np.arange(2400, dtype=np.float32)*0.1)*0.1
            generated = np.sin(np.arange(12000, dtype=np.float32)*0.2)*0.2
            sf.write(adapter/'ref_sample.wav', reference, 24000)
            (adapter/'training_meta.json').write_text(json.dumps({'ref_sample_text': 'Known reference.'}))
            source_bytes = {p: p.read_bytes() for p in adapter.iterdir()}
            prompt = object()
            model = SimpleNamespace(
                create_voice_clone_prompt=Mock(return_value=prompt),
                _tokenize_texts=Mock(side_effect=lambda texts: tuple(texts)),
                generate_voice_clone=Mock(side_effect=lambda **kwargs: ([generated.copy()], 24000)))
            engine = tts.TTSEngine.__new__(tts.TTSEngine)
            engine._mode = 'local'
            engine._max_new_tokens = 100
            engine._lora_prompt_cache = {}
            engine._init_local_lora = Mock(return_value=model)
            generate = stack.enter_context(patch.object(engine, 'generate_voice', wraps=engine.generate_voice))
            fake_torch = ModuleType('torch')
            fake_torch.manual_seed = Mock()
            stack.enter_context(patch.dict(sys.modules, {'torch': fake_torch}))
            state = copy.deepcopy(core.process_state)
            stack.enter_context(patch.object(lora, 'process_state', state))
            check = stack.enter_context(patch.object(lora, 'check_global_gpu_lock'))
            claim = stack.enter_context(patch.object(lora, 'claim_gpu_task'))
            stack.enter_context(patch.object(lora, '_load_builtin_lora_manifest',
                return_value=[{'id': 'builtin_fixture', 'builtin': True}]))
            stack.enter_context(patch.object(lora, '_load_manifest', return_value=[]))
            stack.enter_context(patch.object(lora, 'BUILTIN_LORA_DIR', str(root)))
            def get_engine():
                self.assertTrue(claim.called)
                return engine
            stack.enter_context(patch.object(lora, 'project_manager', SimpleNamespace(get_engine=get_engine)))
            api = FastAPI()
            api.include_router(lora.router)
            request = {'adapter_id': 'builtin_fixture', 'text': 'Known test line.', 'instruct': 'calm'}
            with TestClient(api) as client, contextlib.redirect_stdout(io.StringIO()):
                response = client.post('/api/lora/test', json=request)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual('ok', response.json()['status'])
                self.assertTrue(response.json()['audio_url'].startswith('/builtin_lora/builtin_fixture/'))
                output = adapter/Path(response.json()['audio_url']).name
                self.assertTrue(output.is_file())
                first_bytes = output.read_bytes()
                route_voice = generate.call_args.kwargs['voice_config']['_lora_test_']
                self.assertEqual('lora', route_voice['type'])
                builtin_voice = {**route_voice, 'type': 'builtin_lora'}
                second = adapter/'saved_assignment.wav'
                self.assertTrue(engine.generate_voice(request['text'], request['instruct'], '_lora_test_',
                                                      {'_lora_test_': builtin_voice}, str(second)))
                self.assertEqual(first_bytes, second.read_bytes())
                decoded, rate = sf.read(second)
                self.assertEqual(24000, rate)
                np.testing.assert_allclose(generated, decoded, atol=1e-4)
                self.assertEqual(model.generate_voice_clone.call_args_list[0],
                                 model.generate_voice_clone.call_args_list[1])
                engine._init_local_lora.assert_has_calls([unittest.mock.call(unittest.mock.ANY,generation_sha256=unittest.mock.ANY,source_adapter_path=str(adapter)),
                                                         unittest.mock.call(unittest.mock.ANY,generation_sha256=unittest.mock.ANY,source_adapter_path=str(adapter))])
                self.assertEqual(1, model.create_voice_clone_prompt.call_count)
                self.assertEqual('Known reference.', model.create_voice_clone_prompt.call_args.kwargs['ref_text'])
                check.assert_called_once_with('lora_test')
                claim.assert_called_once_with('lora_test')
                self.assertFalse(state['lora_test']['running'])
                self.assertEqual('lora', tts.voice_category(route_voice))
                self.assertEqual('lora', tts.voice_category(builtin_voice))
                for path, original in source_bytes.items():
                    self.assertEqual(original, path.read_bytes())
                # Both type spellings also reject missing reference metadata before model work.
                (adapter/'training_meta.json').unlink()
                engine._init_local_lora.reset_mock()
                for voice_type in ('lora', 'builtin_lora'):
                    bad_output = adapter/('bad_'+voice_type+'.wav')
                    self.assertFalse(engine.generate_voice(request['text'], '', '_lora_test_',
                        {'_lora_test_': {**route_voice, 'type': voice_type}}, str(bad_output)))
                    self.assertFalse(bad_output.exists())
                engine._init_local_lora.assert_not_called()


class LoraTestAudioPublicationTests(unittest.TestCase):
    def test_rejected_and_partial_test_generations_do_not_return_or_reuse_a_saved_url(self):
        import contextlib
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from types import SimpleNamespace
        for mode in ('rejected','missing','partial','valid-none'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
                root=Path(tmp);adapter=root/'voice';adapter.mkdir();old=adapter/'test_voice_123.wav';sf.write(old,np.full(2400,.1),24000);before=old.read_bytes()
                state=copy.deepcopy(core.process_state);paths=[]
                def generate(output_path,**kwargs):
                    paths.append(Path(output_path))
                    if mode=='partial':Path(output_path).write_bytes(b'partial WAV')
                    if mode=='valid-none':sf.write(output_path,np.full(2400,.2),24000)
                    return False if mode=='rejected' else None
                stack.enter_context(patch.object(lora,'process_state',state));stack.enter_context(patch.object(lora,'check_global_gpu_lock'));stack.enter_context(patch.object(lora,'claim_gpu_task'));stack.enter_context(patch.object(lora,'_load_builtin_lora_manifest',return_value=[]));stack.enter_context(patch.object(lora,'_load_manifest',return_value=[{'id':'voice'}]));stack.enter_context(patch.object(lora,'LORA_MODELS_DIR',str(root)));stack.enter_context(patch.object(lora,'LORA_MODELS_MANIFEST',str(root/'manifest.json')));stack.enter_context(patch.object(lora,'project_manager',SimpleNamespace(get_engine=lambda:SimpleNamespace(generate_voice=generate))));stack.enter_context(patch.object(lora.time,'time',return_value=123))
                app=FastAPI();app.include_router(lora.router)
                with TestClient(app) as client:response=client.post('/api/lora/test',json={'adapter_id':'voice','text':'known line'})
                self.assertEqual(before,old.read_bytes());self.assertEqual(1,len(paths));self.assertFalse(state['lora_test']['running'])
                if mode=='valid-none':
                    self.assertEqual(200,response.status_code,response.text)
                    published=adapter/paths[0].name
                    self.assertTrue(published.is_file())
                    audio,rate=sf.read(published);self.assertEqual(24000,rate)
                    np.testing.assert_allclose(audio,.2,atol=4e-5)
                    self.assertFalse(paths[0].exists(),'private staging was not cleaned')
                    self.assertTrue(response.json()['audio_url'].endswith(published.name))
                else:self.assertEqual(500,response.status_code,response.text);self.assertNotIn('audio_url',response.json());self.assertFalse(paths[0].exists())
                self.assertNotEqual(old,paths[0])
