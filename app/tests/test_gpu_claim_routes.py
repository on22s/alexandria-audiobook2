"""Routes using claim_gpu_task inherit its atomic global conflict check."""

import asyncio
import copy
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException
import core
from routers import system, voices, editor


class GpuClaimRouteTests(unittest.TestCase):
    def test_connectivity_probe_claims_slot_and_releases_on_success_and_error(self):
        state = copy.deepcopy(core.process_state)
        for entry in state.values():
            entry["running"] = False
        state.setdefault("llm_test", {"running": False})
        def probe(_profile):
            self.assertTrue(state["llm_test"]["running"])
            with self.assertRaises(HTTPException):
                core.claim_gpu_task("audio")
            return {"ok": True}
        with patch.object(core, "process_state", state), \
             patch.object(system, "process_state", state), \
             patch.object(core, "llm_is_on_this_gpu", return_value=True), \
             patch.object(system, "_load_llm_config", return_value={"base_url": "http://localhost:1234/v1"}), \
             patch.object(system, "_run_llm_test", side_effect=probe) as run:
            state["audio"]["running"] = True
            with self.assertRaises(HTTPException):
                asyncio.run(system.llm_test())
            run.assert_not_called()
            self.assertFalse(state["llm_test"]["running"])
            state["audio"]["running"] = False
            self.assertEqual({"ok": True}, asyncio.run(system.llm_test()))
            self.assertFalse(state["llm_test"]["running"])
            run.side_effect = RuntimeError("probe failed")
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                asyncio.run(system.llm_test())
            self.assertFalse(state["llm_test"]["running"])

    def test_cancelled_request_keeps_connectivity_slot_until_worker_finishes(self):
        state = copy.deepcopy(core.process_state)
        for entry in state.values():
            entry["running"] = False
        state.setdefault("llm_test", {"running": False})
        started, release, stopped = threading.Event(), threading.Event(), threading.Event()
        def probe(_profile):
            started.set()
            try:
                if not release.wait(5):
                    raise AssertionError("probe was not released")
                return {"ok": True}
            finally:
                stopped.set()
        async def exercise():
            request = asyncio.create_task(system.llm_test())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                self.assertTrue(state["llm_test"]["running"])
                with self.assertRaises(HTTPException):
                    core.claim_gpu_task("audio")
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(stopped.wait, 2))
                for _ in range(100):
                    if not state["llm_test"]["running"]:
                        break
                    await asyncio.sleep(0.01)
            self.assertFalse(state["llm_test"]["running"])
        with patch.object(core, "process_state", state), \
             patch.object(system, "process_state", state), \
             patch.object(core, "llm_is_on_this_gpu", return_value=True), \
             patch.object(system, "_load_llm_config", return_value={"base_url": "http://localhost:1234/v1"}), \
             patch.object(system, "_run_llm_test", side_effect=probe):
            asyncio.run(exercise())

    def test_editor_mutations_reject_busy_audio_before_touching_project(self):
        from unittest.mock import Mock
        state = copy.deepcopy(core.process_state)
        for entry in state.values():
            entry["running"] = False
        project = Mock()
        project.update_chunk.return_value = {"instruct": "neutral"}
        project.insert_chunk.return_value = [{}]
        project.restore_chunk.return_value = [{}]
        project.delete_chunk.return_value = ({}, [{}])
        with patch.object(core, "process_state", state), \
             patch.object(core, "llm_is_on_this_gpu", return_value=True), \
             patch.object(editor, "process_state", state), \
             patch.object(editor, "project_manager", project):
            operations = [lambda: editor.update_chunk(0, editor.ChunkUpdate(text="new")),
                          lambda: editor.insert_chunk(0),
                          lambda: editor.restore_chunk(editor.ChunkRestoreRequest(chunk={}, at_index=0)),
                          lambda: editor.delete_chunk(0)]
            state["audio"]["running"] = True
            for operation in operations:
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(operation())
                self.assertEqual(400, error.exception.status_code)
            self.assertEqual([], project.mock_calls)
            state["audio"]["running"] = False
            for operation in operations:
                asyncio.run(operation())
            self.assertEqual(4, len(project.mock_calls))

    def test_suggestion_and_optimize_refuse_a_conflicting_gpu_task(self):
        state = copy.deepcopy(core.process_state)
        for entry in state.values():
            entry["running"] = False
        state["audio"]["running"] = True
        with patch.object(core, "process_state", state), \
             patch.object(voices, "process_state", state), \
             patch.object(system, "process_state", state), \
             patch.object(core, "llm_is_on_this_gpu", return_value=True), \
             patch.object(system, "load_app_config", return_value={
                 "llm": {"model_name": "test", "base_url": "http://localhost:1234/v1"}}), \
             patch.object(system, "apply_lmstudio_settings", return_value=(True, "ok")) as optimize, \
             patch.object(system, "get_lmstudio_status", return_value={"loaded": True}), \
             patch.object(system, "get_current_status", return_value={"loaded": True, "optimized": True,
                                                                       "management_verified": True}), \
             patch.object(voices, "_suggest_voices_impl", return_value={"suggestions": []}) as suggest:
            for name, call in (
                ("voices", lambda: voices.suggest_voices(voices.SuggestVoicesRequest())),
                ("lmstudio_optimize", lambda: system.lmstudio_optimize(
                    system.LMStudioOptimizeRequest(enable=True))),
            ):
                with self.subTest(task=name):
                    with self.assertRaises(HTTPException) as raised:
                        asyncio.run(call())
                    self.assertEqual(400, raised.exception.status_code)
                    self.assertFalse(state[name]["running"])
                    self.assertTrue(state["audio"]["running"])
            optimize.assert_not_called()
            suggest.assert_not_called()
            state["audio"]["running"] = False
            asyncio.run(voices.suggest_voices(voices.SuggestVoicesRequest()))
            asyncio.run(system.lmstudio_optimize(system.LMStudioOptimizeRequest(enable=True)))
            suggest.assert_called_once()
            optimize.assert_called_once_with("test", ideal=True)
            self.assertFalse(state["voices"]["running"])
            self.assertFalse(state["lmstudio_optimize"]["running"])


class NicknameReservedSubprocessTests(unittest.TestCase):
    def test_nicknames_claim_before_subprocess_and_conflicts_cannot_launch(self):
        import json
        from pathlib import Path
        import sys
        import tempfile
        from unittest.mock import Mock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import script
        state=copy.deepcopy(core.process_state)
        for entry in state.values():entry['running']=False
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'annotated_script.json'
            source.write_text(json.dumps([{'speaker':'ALICE','text':'Hello.'}]),encoding='utf-8')
            aliases=root/'character_aliases.json'
            aliases.write_text('{}',encoding='utf-8')
            log=root/'nicknames.log'
            api=FastAPI();api.include_router(script.router)
            stream=core._stream_subprocess_to_logs
            launches=[]
            exit_codes=[]
            def cpu_child(command,cwd,current,**kwargs):
                launches.append(command)
                self.assertIs(current,state['nicknames'])
                self.assertTrue(current['running'])
                self.assertEqual('find_nicknames.py',command[2])
                for task in ('audio','nicknames'):
                    with self.assertRaises(HTTPException):core.claim_gpu_task(task)
                result=stream([sys.executable,'-c','print("CPU-only reserved nickname fixture",flush=True)'],tmp,current,**kwargs)
                exit_codes.append(result[0])
                return result
            with patch.object(core,'process_state',state), \
                 patch.object(script,'process_state',state), \
                 patch.object(core,'llm_is_on_this_gpu',return_value=True), \
                 patch.object(core,'acquire_gpu_lock',return_value=None) as acquire, \
                 patch.object(core,'_gpu_leases',{}), \
                 patch.object(core,'_init_task_log',return_value=str(log)), \
                 patch.object(core,'_stream_subprocess_to_logs',side_effect=cpu_child), \
                 patch.object(script,'SCRIPT_PATH',str(source)), \
                 patch.object(script,'CHARACTER_ALIASES_PATH',str(aliases)), \
                 TestClient(api) as client:
                state['audio']['running']=True
                denied=client.post('/api/find_nicknames')
                self.assertEqual(400,denied.status_code,denied.text)
                self.assertEqual([],launches)
                acquire.assert_not_called()
                state['audio']['running']=False
                response=client.post('/api/find_nicknames')
                self.assertEqual(200,response.status_code,response.text)
                acquire.assert_called_once()
                self.assertEqual(1,len(launches))
                self.assertFalse(state['nicknames']['running'])
                self.assertEqual([0],exit_codes)
                self.assertIn('Task nicknames completed successfully.',state['nicknames']['logs'])
                self.assertIn('CPU-only reserved nickname fixture',log.read_text())
            self.assertEqual('{}',aliases.read_text())
            self.assertEqual([{'speaker':'ALICE','text':'Hello.'}],json.loads(source.read_text()))


class PersonaStartPreflightTests(unittest.TestCase):
    def test_missing_script_and_unknown_speaker_leave_engine_and_task_unchanged(self):
        import json
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        for source_kind, payload, expected in (
                ('missing', {}, 422), ('missing', {'advanced':True}, 422),
                ('missing', {'speaker':'Hero'}, 422), ('directory', {}, 422),
                ('valid', {'speaker':'Absent'}, 404)):
            with self.subTest(source=source_kind,payload=payload), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp,'script.json')
                if source_kind == 'directory':
                    path.mkdir()
                elif source_kind == 'valid':
                    path.write_text(json.dumps([{'speaker':'Hero','text':'Hello.'}]))
                engine = object()
                manager = SimpleNamespace(engine=engine)
                state = {'persona':{'running':False,'cancel':True,'logs':['previous run']}}
                before = copy.deepcopy(state)
                app = FastAPI()
                app.include_router(voices.router)
                with patch.object(voices,'SCRIPT_PATH',str(path)), \
                     patch.object(voices,'project_manager',manager), \
                     patch.object(voices,'process_state',state), \
                     patch.object(voices,'check_global_gpu_lock') as check, \
                     patch.object(core,'claim_gpu_task',wraps=core.claim_gpu_task) as claim, \
                     patch.object(core,'process_state',state), \
                     patch.object(core,'_task_claims',{}), \
                     patch.object(core,'_gpu_leases',{}), \
                     patch.object(core,'acquire_gpu_lock',return_value=None), \
                     patch.object(core,'llm_is_on_this_gpu',return_value=True), \
                     patch.object(voices,'run_process') as run, \
                     patch.object(voices.gc,'collect') as collect, \
                     TestClient(app) as client:
                    response = client.post('/api/generate_personas',json=payload)
                    self.assertEqual(expected,response.status_code,response.text)
                    claim.assert_not_called()
                    run.assert_not_called()
                    collect.assert_not_called()
                self.assertIs(engine,manager.engine)
                self.assertEqual(before,state)

    def test_existing_script_still_checks_gpu_and_queues_requested_persona_command(self):
        import json
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        for payload in ({}, {'speaker':'Hero','context_lines':50,'age_group':'teen','new_only':True},
                        {'advanced':True,'batch_size':17}):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp,'script.json')
                original = json.dumps([{'speaker':'Hero','text':'Hello.'}])
                path.write_text(original)
                manager = SimpleNamespace(engine=object())
                state = {'persona':{'running':False,'cancel':True,'logs':['previous run']}}
                app = FastAPI()
                app.include_router(voices.router)
                with patch.object(voices,'SCRIPT_PATH',str(path)), \
                     patch.object(voices,'project_manager',manager), \
                     patch.object(voices,'process_state',state), \
                     patch.object(voices,'check_global_gpu_lock') as check, \
                     patch.object(core,'claim_gpu_task',wraps=core.claim_gpu_task) as claim, \
                     patch.object(core,'process_state',state), \
                     patch.object(core,'_task_claims',{}), \
                     patch.object(core,'_gpu_leases',{}), \
                     patch.object(core,'acquire_gpu_lock',return_value=None), \
                     patch.object(core,'llm_is_on_this_gpu',return_value=True), \
                     patch.object(voices,'run_process') as run, \
                     patch.object(voices.gc,'collect') as collect, \
                     TestClient(app) as client:
                    response = client.post('/api/generate_personas',json=payload)
                    self.assertEqual(200,response.status_code,response.text)
                    check.assert_called_once_with('persona')
                    claim.assert_called_once_with('persona')
                    run.assert_called_once()
                    collect.assert_called_once_with()
                command, task = run.call_args.args
                self.assertEqual('persona',task)
                self.assertEqual('generate_personas.py',command[2])
                self.assertEqual(str(payload.get('context_lines',8)),command[command.index('--context-lines')+1])
                if payload.get('speaker'):
                    self.assertEqual('Hero',command[command.index('--speakers')+1])
                    self.assertEqual('teen',command[command.index('--age-group')+1])
                    self.assertIn('--new-only',command)
                if payload.get('advanced'):
                    self.assertIn('--advanced',command)
                    self.assertEqual('17',command[command.index('--batch-size')+1])
                self.assertIsNone(manager.engine)
                self.assertFalse(state['persona']['cancel'])
                self.assertEqual(original,path.read_text())


class SingleChunkTaskReportingTests(unittest.TestCase):
    def run_generation(self, outcome):
        import json
        from pathlib import Path
        import tempfile
        import time
        from types import SimpleNamespace
        import wave
        import numpy as np
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from project import ProjectManager
        from routers import script
        state = copy.deepcopy(core.process_state)
        for entry in state.values():entry['running'] = False
        state['audio'].update(logs=['stale task error'],status='failed',error='stale error',
                              start_time=1,cancel=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = ProjectManager(tmp)
            chunks = [{'uid':'stable-fixture','speaker':'ALICE','text':'A real CPU fixture.',
                       'instruct':'calm','status':'pending','audio_path':None}]
            Path(manager.chunks_path).write_text(json.dumps(chunks))
            Path(manager.script_path).write_text(json.dumps(chunks))
            Path(manager.voice_config_path).write_text(json.dumps({'ALICE':{'voice':'Ryan'}}))
            original_script = Path(manager.script_path).read_bytes()
            original_config = Path(manager.voice_config_path).read_bytes()
            observed = []
            api = FastAPI();api.include_router(editor.router);api.include_router(script.router)

            def generate(text,instruct,speaker,config,path):
                current = client.get('/api/status/audio')
                self.assertEqual(200,current.status_code,current.text)
                snapshot = current.json()
                observed.append(snapshot)
                self.assertTrue(snapshot['running'])
                self.assertFalse(snapshot['cancel'])
                self.assertGreater(snapshot['start_time'],1)
                self.assertEqual('running',snapshot['status'])
                self.assertIsNone(snapshot['error'])
                self.assertEqual(['Generating chunk 0...'],snapshot['logs'])
                conflict = client.post('/api/chunks/0/generate')
                self.assertEqual(400,conflict.status_code,conflict.text)
                self.assertEqual(1,len(observed))
                if outcome == 'engine_error':raise RuntimeError('decoder failed')
                if outcome == 'false':return False
                samples = (0.2 * np.sin(2*np.pi*220*np.arange(96000)/24000)*32767).astype('<i2')
                with wave.open(path,'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
                    wav.writeframes(samples.tobytes())
                return True

            manager.engine = SimpleNamespace(generate_voice=generate)
            def export(temp_path,filename_base):
                # Keep fixture PCM; conversion is outside task reporting's scope.
                destination = Path(manager.voicelines_dir,filename_base+'.wav')
                destination.write_bytes(Path(temp_path).read_bytes())
                return str(destination.relative_to(root))
            with patch.object(core,'process_state',state), \
                 patch.object(editor,'process_state',state), \
                 patch.object(script,'process_state',state), \
                 patch.object(core,'llm_is_on_this_gpu',return_value=True), \
                 patch.object(core,'acquire_gpu_lock',return_value=None), \
                 patch.object(core,'_gpu_leases',{}), \
                 patch.object(script,'read_manual_pending',return_value=None), \
                 patch.object(editor,'project_manager',manager), \
                 patch.object(manager,'_export_chunk_audio',side_effect=export), \
                 TestClient(api) as client:
                started = time.time()
                response = client.post('/api/chunks/0/generate')
                self.assertEqual(200,response.status_code,response.text)
                self.assertEqual({'status':'started'},response.json())
                terminal = client.get('/api/status/audio').json()
                self.assertFalse(terminal['running'])
                self.assertFalse(terminal['cancel'])
                self.assertIsNone(terminal['eta'])
                self.assertGreaterEqual(terminal['start_time'],started)
                self.assertEqual(1,len(observed))
                saved = json.loads(Path(manager.chunks_path).read_text())[0]
                if outcome == 'success':
                    self.assertEqual('done',terminal['status'])
                    self.assertIsNone(terminal['error'])
                    self.assertEqual('done',saved['status'])
                    audio = root / saved['audio_path']
                    with wave.open(str(audio),'rb') as wav:
                        self.assertEqual(24000,wav.getframerate());self.assertEqual(96000,wav.getnframes())
                    self.assertIn('Generation complete:',terminal['logs'][-1])
                    self.assertIn(saved['audio_path'],terminal['logs'][-1])
                else:
                    message = 'decoder failed' if outcome=='engine_error' else 'Generation returned False'
                    self.assertEqual('failed',terminal['status'])
                    self.assertEqual(message,terminal['error'])
                    self.assertEqual('error',saved['status'])
                    self.assertEqual(message,saved['error'])
                    self.assertEqual('Generation failed: '+message,terminal['logs'][-1])
                    self.assertFalse(list(Path(manager.voicelines_dir).iterdir()))
                self.assertEqual(original_script,Path(manager.script_path).read_bytes())
                self.assertEqual(original_config,Path(manager.voice_config_path).read_bytes())
                self.assertFalse(list(root.glob('chunk_*.wav')))

    def test_success_has_current_start_time_terminal_status_and_audio_artifact(self):
        self.run_generation('success')

    def test_false_result_is_reported_as_failure(self):
        self.run_generation('false')

    def test_engine_exception_message_reaches_task_status(self):
        self.run_generation('engine_error')

    def test_unexpected_project_exception_releases_claim_with_visible_failure(self):
        from unittest.mock import Mock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import script
        state = copy.deepcopy(core.process_state)
        for entry in state.values():entry['running'] = False
        manager = Mock()
        manager.load_chunks.return_value = [{'text':'Generate this.'}]
        manager.generate_chunk_audio.side_effect = RuntimeError('chunk store unavailable')
        api = FastAPI();api.include_router(editor.router);api.include_router(script.router)
        with patch.object(core,'process_state',state), \
             patch.object(editor,'process_state',state), \
             patch.object(script,'process_state',state), \
             patch.object(core,'llm_is_on_this_gpu',return_value=True), \
             patch.object(core,'acquire_gpu_lock',return_value=None), \
             patch.object(core,'_gpu_leases',{}), \
             patch.object(editor,'project_manager',manager),TestClient(api) as client:
            response = client.post('/api/chunks/0/generate')
            self.assertEqual(200,response.status_code,response.text)
            result = client.get('/api/status/audio').json()
            self.assertFalse(result['running'])
            self.assertEqual('failed',result['status'])
            self.assertEqual('chunk store unavailable',result['error'])
            self.assertEqual('Generation failed: chunk store unavailable',result['logs'][-1])
            manager.generate_chunk_audio.assert_called_once_with(0)


class NonGpuTaskReservationTests(unittest.TestCase):
    def test_cpu_task_reservations_coexist_with_gpu_jobs_in_both_start_orders(self):
        original = copy.deepcopy(core.process_state)
        for cpu_task in ('chapter_export', 'drift_check', 'audacity_export', 'm4b_export'):
            for gpu_task in ('audio', 'script'):
                for cpu_first in (False, True):
                    with self.subTest(cpu=cpu_task, gpu=gpu_task, cpu_first=cpu_first):
                        state = copy.deepcopy(original)
                        for entry in state.values():
                            entry['running'] = False
                        leases = {}
                        handle = object()
                        with patch.object(core, 'process_state', state), \
                             patch.object(core, '_gpu_leases', leases), \
                             patch.object(core, 'llm_is_on_this_gpu', return_value=True), \
                             patch.object(core, 'acquire_gpu_lock', return_value=handle) as acquire, \
                             patch.object(core, 'release_gpu_lock') as release:
                            order = (cpu_task, gpu_task) if cpu_first else (gpu_task, cpu_task)
                            for task in order:
                                core.claim_gpu_task(task)
                                if task == cpu_task and cpu_first:
                                    acquire.assert_not_called()
                            self.assertTrue(state[cpu_task]['running'])
                            self.assertTrue(state[gpu_task]['running'])
                            acquire.assert_called_once_with()
                            self.assertEqual({gpu_task: handle}, leases)
                            with self.assertRaises(HTTPException) as duplicate:
                                core.claim_gpu_task(cpu_task)
                            self.assertEqual(400, duplicate.exception.status_code)
                            self.assertIn('already running', duplicate.exception.detail)
                            with self.assertRaises(HTTPException):
                                core.claim_gpu_task('dataset_builder')
                            acquire.assert_called_once_with()
                            core.release_gpu_task_claim(cpu_task)
                            release.assert_not_called()
                            self.assertEqual({gpu_task: handle}, leases)
                            core.release_gpu_task_claim(gpu_task)
                            release.assert_called_once_with(handle)
                            self.assertEqual({}, leases)
                            self.assertFalse(state[cpu_task]['running'])
                            self.assertFalse(state[gpu_task]['running'])
        self.assertEqual(original, core.process_state)

    def test_actual_cpu_http_starts_do_not_claim_a_gpu_lease_while_audio_runs(self):
        from fastapi import BackgroundTasks, FastAPI
        from fastapi.testclient import TestClient
        import voice_drift
        state = copy.deepcopy(core.process_state)
        for entry in state.values():
            entry['running'] = False
        leases = {}
        app = FastAPI()
        app.include_router(editor.router)
        handle = object()
        with patch.object(core, 'process_state', state), \
             patch.object(editor, 'process_state', state), \
             patch.object(core, '_gpu_leases', leases), \
             patch.object(core, 'llm_is_on_this_gpu', return_value=True), \
             patch.object(core, 'acquire_gpu_lock', return_value=handle) as acquire, \
             patch.object(core, 'release_gpu_lock') as release, \
             patch.object(editor, 'load_app_config', return_value={}), \
             patch.object(editor, '_load_voicelab_config', return_value={}), \
             patch.object(voice_drift, 'get_speaker_model_python', return_value=None), \
             patch.object(BackgroundTasks, 'add_task') as dispatch, TestClient(app) as client:
            core.claim_gpu_task('audio')
            for url, body, task in (('/api/export_chapters', {'format': 'wav', 'require_ready': False}, 'chapter_export'),
                                     ('/api/chunks/drift_check', {'indices': []}, 'drift_check')):
                response = client.post(url, json=body)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual('started', response.json()['status'])
                self.assertTrue(state[task]['running'])
                duplicate = client.post(url, json=body)
                self.assertEqual(400, duplicate.status_code, duplicate.text)
            self.assertEqual(2, dispatch.call_count)
            acquire.assert_called_once_with()
            self.assertEqual({'audio': handle}, leases)
            self.assertTrue(state['audio']['running'])
            core.release_gpu_task_claim('chapter_export')
            core.release_gpu_task_claim('drift_check')
            release.assert_not_called()
            core.release_gpu_task_claim('audio')
            release.assert_called_once_with(handle)
            self.assertEqual({}, leases)
