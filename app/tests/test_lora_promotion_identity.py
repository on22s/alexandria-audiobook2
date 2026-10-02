"""Confirmed candidate identity reaches the lock-protected native promotion path."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import core
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import lora
from tests import test_lora_candidate_promotion as fixture
from tests import test_training_ui_contract as ui


class PromotionIdentityTests(unittest.TestCase):
    def setup_api(self, root, stack):
        models = root / 'models'
        adapter = models / 'voice'
        fixture._write_real_checkpoint(adapter, 'production')
        fixture._write_real_checkpoint(adapter / 'candidates/epoch_002', 'candidate')
        fixture._write_real_checkpoint(adapter / 'candidates/epoch_003', 'older')
        manifest = models / 'manifest.json'
        manifest.write_text(json.dumps([{'id': 'voice', 'evaluation': {'recommended_candidate': 'epoch_002'},
                                        'evaluation_candidates': [{'id': 'epoch_002'}, {'id': 'epoch_003'}]}]))
        state = copy.deepcopy(core.process_state)
        for row in state.values():
            row['running'] = False
        engine = SimpleNamespace(engine=object())
        for owner, key, value in ((core, 'process_state', state), (lora, 'process_state', state),
                                  (core, '_task_claims', {}), (core, '_gpu_leases', {}),
                                  (lora, 'LORA_MODELS_DIR', str(models)),
                                  (lora, 'LORA_MODELS_MANIFEST', str(manifest)),
                                  (lora, 'project_manager', engine)):
            stack.enter_context(patch.object(owner, key, value))
        stack.enter_context(patch.object(core, 'acquire_gpu_lock', return_value=None))
        stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=True))
        app = FastAPI(); app.include_router(lora.router)
        client = stack.enter_context(TestClient(app))
        # Admit the test family's identities before measuring checkpoint publication.
        with lora.lock_adapter_naming(str(models), str(manifest)):
            lora._get_user_adapter_path_locked(str(models), 'voice')
        return client, models, adapter, manifest, state, engine

    def test_recommendation_changes_after_request_before_locked_promotion(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, models, adapter, manifest, state, engine = self.setup_api(Path(tmp), stack)
            entered, release = threading.Event(), threading.Event()
            original = lora._promote_lora_candidate
            def delayed(*args, **kwargs):
                entered.set()
                if not release.wait(3):
                    raise AssertionError('owned promotion fixture timed out')
                return original(*args, **kwargs)
            stack.enter_context(patch.object(lora, '_promote_lora_candidate', side_effect=delayed))
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(client.post, '/api/lora/models/voice/promote',
                                     json={'expected_candidate_id': 'epoch_002'})
                try:
                    self.assertTrue(entered.wait(3))
                    with lora.lock_adapter_naming(str(models), str(manifest)):
                        rows = json.loads(manifest.read_text())
                        rows[0]['evaluation']['recommended_candidate'] = 'epoch_003'
                        manifest.write_text(json.dumps(rows))
                    before_manifest = manifest.read_bytes()
                    before = {str(p.relative_to(adapter)): p.read_bytes() for p in adapter.rglob('*')
                              if p.is_file() and not p.name.endswith('.lock')}
                finally:
                    release.set()
                response = future.result(timeout=3)
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before_manifest, manifest.read_bytes())
            self.assertEqual(before, {str(p.relative_to(adapter)): p.read_bytes() for p in adapter.rglob('*')
                                     if p.is_file() and not p.name.endswith('.lock')})
            self.assertFalse((adapter / 'promotion_backups').exists())
            self.assertIsNotNone(engine.engine)
            self.assertFalse(state['lora_training']['running'])
            self.assertEqual({}, core._task_claims)

    def test_matching_id_promotes_exact_files_and_retains_rollback(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, _, adapter, manifest, state, engine = self.setup_api(Path(tmp), stack)
            old = {name: (adapter / name).read_bytes() for name in lora.PROMOTION_FILES}
            expected = {name: (adapter / 'candidates/epoch_002' / name).read_bytes() for name in lora.PROMOTION_FILES}
            response = client.post('/api/lora/models/voice/promote', json={'expected_candidate_id': 'epoch_002'})
            self.assertEqual(200, response.status_code, response.text)
            promotion = response.json()['promotion']
            self.assertEqual('epoch_002', promotion['candidate'])
            self.assertEqual(expected, {name: (adapter / name).read_bytes() for name in lora.PROMOTION_FILES})
            backup = adapter / 'promotion_backups' / promotion['backup_id']
            self.assertEqual(old, {name: (backup / name).read_bytes() for name in lora.PROMOTION_FILES})
            self.assertIsNone(engine.engine)
            self.assertEqual('epoch_002', json.loads(manifest.read_text())[0]['promotion']['candidate'])
            rolled = client.post('/api/lora/models/voice/rollback-promotion')
            self.assertEqual(200, rolled.status_code, rolled.text)
            self.assertEqual(old, {name: (adapter / name).read_bytes() for name in lora.PROMOTION_FILES})
            self.assertFalse(state['lora_training']['running'])
            self.assertEqual({}, core._task_claims)

    def test_malformed_expected_ids_fail_before_worker_claim(self):
        app = FastAPI(); app.include_router(lora.router)
        with TestClient(app) as client, patch.object(lora, 'run_claimed_task_worker') as claim:
            for value in ('', [], {}, 1, True):
                with self.subTest(value=value):
                    result = client.post('/api/lora/models/voice/promote', json={'expected_candidate_id': value})
                    self.assertEqual(422, result.status_code, result.text)
            claim.assert_not_called()


class PromotionIdentityJsTests(unittest.TestCase):
    run_js = ui.TrainingUiContractTests.run_js

    def test_confirmation_sends_exact_candidate_and_conflict_is_not_success(self):
        self.run_js(r"""
const posts=[],prompts=[];let loads=0,fail=false;
ctx.showConfirm=async text=>{prompts.push(text);return true;};ctx.loadLoraModels=async()=>{loads++;};
ctx.fetch=async(path,request)=>{posts.push({path,body:JSON.parse(request.body)});
 return fail?new Response(JSON.stringify({detail:'candidate changed'}),{status:409}):new Response(JSON.stringify({promotion:{candidate:'epoch_002'}}));};
await ctx.promoteLoraCandidate('voice #','epoch_002');assert(prompts[0].includes('epoch_002'));
assert.strictEqual(posts[0].path,'/api/lora/models/voice%20%23/promote');assert.strictEqual(posts[0].body.expected_candidate_id,'epoch_002');assert.strictEqual(loads,1);assert.strictEqual(toasts.at(-1)[1],'success');
fail=true;await ctx.promoteLoraCandidate('voice #','epoch_002');assert.strictEqual(loads,1);assert.strictEqual(toasts.at(-1)[1],'error');assert(toasts.at(-1)[0].includes('candidate changed'));
ctx.showConfirm=async()=>false;await ctx.promoteLoraCandidate('voice #','epoch_002');assert.strictEqual(posts.length,2);
for(const value of [undefined,null,'', ' ',1]){await ctx.promoteLoraCandidate('voice #',value);}assert.strictEqual(posts.length,2);
""")
