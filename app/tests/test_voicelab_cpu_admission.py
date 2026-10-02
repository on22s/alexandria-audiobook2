"""Actual naming HTTP and pending claims coexist with unrelated GPU work."""
import asyncio
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
import core
from routers import voicelab as voice_lab
from tests.test_adapter_checkpoint_transaction import _write


class VoiceLabCpuAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.models=self.root/'models';self.models.mkdir()
        _write(self.models/'raw_a',1)
        (self.models/'manifest.json').write_text(json.dumps([{'id':'raw_a','dataset_id':'raw_a','name':'raw_a',
            'voice_profile':'Warm baritone in his 30s; best for fantasy.'}]))
        self.states=copy.deepcopy(core.process_state)
        for state in self.states.values():state['running']=False
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for module,name,value in ((core,'DATA_DIR',str(self.root)),(voice_lab,'DATA_DIR',str(self.root)),
                (core,'API_LOG_DIR',str(self.root/'logs')),(voice_lab,'API_LOG_DIR',str(self.root/'logs')),
                (core,'RUN_HISTORY_DIR',str(self.root/'history')),(voice_lab,'RUN_HISTORY_DIR',str(self.root/'history')),
                (voice_lab,'LORA_MODELS_DIR',str(self.models)),(voice_lab,'LORA_MODELS_MANIFEST',str(self.models/'manifest.json')),
                (core,'process_state',self.states),(voice_lab,'process_state',self.states),
                (core,'_task_claims',{}),(core,'_gpu_leases',{})):
            self.stack.enter_context(patch.object(module,name,value))
        self.gpu=self.stack.enter_context(patch.object(core,'acquire_gpu_lock',return_value=None))
        self.stack.enter_context(patch.object(core,'llm_is_on_this_gpu',return_value=True))
        self.stack.enter_context(patch.object(voice_lab,'_load_voicelab_config',return_value={'zips_dir':str(self.root),'rocm_python':sys.executable,'profiler_model':'','epub_dirs':[]}))
        self.addCleanup(lambda:[core.release_gpu_task_claim(name,owner['id']) for name,owner in list(core._task_claims.items())])

    def request(self):
        request=voice_lab.VoiceLabRequest(stages=['name'])
        preflight=voice_lab._build_voicelab_preflight(request,voice_lab._load_voicelab_config())
        self.assertTrue(preflight['ready'],preflight)
        return request.model_copy(update={'preflight_id':preflight['preflight_id']})

    def test_actual_http_name_apply_finishes_without_claiming_gpu_while_audio_stays_owned(self):
        audio=core.claim_gpu_task('audio')
        app=FastAPI();app.add_middleware(core.TaskClaimMiddleware);app.include_router(voice_lab.router)
        with TestClient(app) as client:
            request=self.request()
            response=client.post('/api/voicelab/start',json=request.model_dump())
            self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('done',self.states['voicelab']['status'],self.states['voicelab']['logs'])
        self.assertTrue(self.states['voicelab']['cpu_only'])
        self.assertNotIn('voicelab',core._task_claims)
        self.assertEqual(audio,core._task_claims['audio']['id'])
        self.assertTrue(core.is_task_running('audio'))
        self.assertFalse((self.models/'raw_a').exists())
        rows=json.loads((self.models/'manifest.json').read_text())
        self.assertNotEqual('raw_a',rows[0]['id'])
        self.assertTrue((self.models/rows[0]['id']/'adapter_model.safetensors').is_file())
        self.assertEqual(1,self.gpu.call_count,'naming claimed a GPU lease')

    def test_pending_name_claim_allows_reverse_gpu_admission_but_duplicate_and_mixed_stages_stay_blocked(self):
        request=self.request();tasks=BackgroundTasks()
        result=asyncio.run(voice_lab.voicelab_start(request,tasks))
        self.assertEqual('started',result['status'])
        self.assertEqual('pending',core._task_claims['voicelab']['phase'])
        self.gpu.assert_not_called()
        audio=core.claim_gpu_task('audio')
        with self.assertRaises(HTTPException):asyncio.run(voice_lab.voicelab_start(request,BackgroundTasks()))
        core.release_gpu_task_claim('voicelab',pending_only=True)
        mixed=voice_lab.VoiceLabRequest(stages=['quality','name'],preflight_id='fixture')
        fake={'preflight_id':'fixture','blockers':[],'_zips_dir':str(self.root),'_profiler_model':''}
        with patch.object(voice_lab,'_build_voicelab_preflight',return_value=fake), \
                patch.object(voice_lab,'_voicelab_build_commands',return_value=[]):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(voice_lab.voicelab_start(mixed,BackgroundTasks()))
        self.assertEqual(400,error.exception.status_code)
        self.assertEqual(audio,core._task_claims['audio']['id'])
        asyncio.run(tasks()) # Stale pending token must not start naming after release.
        self.assertTrue((self.models/'raw_a').exists())
