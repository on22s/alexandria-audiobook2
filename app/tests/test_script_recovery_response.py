import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import script
from tests import test_script_recovery as fixtures
from tests import test_script_recovery_transaction as transactions


class ScriptRecoveryResponseTests(unittest.TestCase):
    def test_legacy_retry_requires_current_explicit_confirmation_and_keeps_saved_options(self):
        import core
        with transactions.ScriptRecoveryTransactionTests().fixture() as (root, path):
            source = root / 'source.txt'; source.write_text('Source for this book.')
            (root / 'state.json').write_text(json.dumps({
                'input_file_path': str(source), 'script_generation_input_file': str(source),
                'script_generation_options': {'strip_front_matter': False}}))
            api = FastAPI(); api.add_middleware(core.TaskClaimMiddleware); api.include_router(script.router)
            calls = []
            with TestClient(api) as client, patch.object(core, 'acquire_gpu_lock', return_value=None), \
                    patch.object(script, 'build_generate_script_command', return_value=['stand-in']) as build, \
                    patch.object(script, 'run_process', side_effect=lambda *args: calls.append(args)):
                tokens = client.get('/api/generate_script/recovery').json()['legacy_confirmation']
                self.assertEqual(409, client.post('/api/generate_script/retry', json={}).status_code)
                checkpoint = Path(script.three_pass_checkpoint_path(path))
                changed = json.loads(checkpoint.read_text()); changed['elapsed_s'] = {'segment': 1}
                checkpoint.write_text(json.dumps(changed))
                payload = {'legacy_checkpoint_token':tokens['checkpoint_token'],
                           'legacy_book_token':tokens['book_token']}
                self.assertEqual(409, client.post('/api/generate_script/retry', json=payload).status_code)
                self.assertEqual([], calls)
                tokens = client.get('/api/generate_script/recovery').json()['legacy_confirmation']
                payload['legacy_checkpoint_token'] = tokens['checkpoint_token']
                self.assertEqual(200, client.post('/api/generate_script/retry', json=payload).status_code)
                self.assertFalse(build.call_args.kwargs['strip_front_matter'])
                self.assertIn('--require-matching-checkpoint', calls[0][0])

    def test_legacy_retry_confirmation_ui_requires_explicit_choice(self):
        code = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync(process.argv[1],'utf8');let posts=[],confirm=false;
const buttons={};for(const id of ['btn-gen-script','btn-retry-script','btn-cancel-script','btn-pause-script']) buttons[id]={disabled:false,style:{}};
const ctx={window:{},document:{getElementById:id=>buttons[id]},API:{get:async()=>({legacy_confirmation:{checkpoint_token:'a'.repeat(64),book_token:'b'.repeat(64)}}),post:async(path,data)=>posts.push({path,data})},showConfirm:async()=>confirm,_resetPauseBtn:()=>{},pollScriptLogs:()=>{},refreshScriptRecovery:()=>{},showActionError:()=>{},scriptBatchPoller:null};vm.createContext(ctx);
vm.runInContext(src.slice(src.indexOf('window.retryScriptGeneration ='),src.indexOf('// --- Script Batch Mode ---')),ctx);
(async()=>{await ctx.window.retryScriptGeneration();assert.equal(posts.length,0);confirm=true;await ctx.window.retryScriptGeneration();assert.equal(posts.length,1);assert.equal(posts[0].data.legacy_checkpoint_token,'a'.repeat(64));assert.equal(posts[0].data.legacy_book_token,'b'.repeat(64));})().catch(e=>{console.error(e);process.exit(1)});
"""
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_recovery_renderer_leads_with_choices_and_retains_diagnostics(self):
        code = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const core=fs.readFileSync(process.argv[1],'utf8'),panel={style:{},innerHTML:''};
const ctx={window:{},document:{getElementById:()=>panel}};vm.createContext(ctx);
vm.runInContext(core.slice(core.indexOf('function escapeHtml('),core.indexOf('// Parse a numeric input')),ctx);
vm.runInContext(core.slice(core.indexOf('function renderScriptRecovery('),core.indexOf('async function onCopyRecoveryPrompt(')),ctx);
for(const pass of ['segment','attribute','instruct']){
 const detail={failed_pass:pass,source:'<script>unsafe()</script>日本語',failed_chunk:2,chunks_total:4,chunks_done:1,batch_entries:[{}],batch_indices:[8],last_error:{category:'transport',http_status:503,error:'<img onerror=unsafe()>'},attempts:[{attempt:1,http_status:503,error:'<bad>'}],retry_profile:{api_retry_limit:0,retry_initial_delay_seconds:2,retry_multiplier:2,retry_jitter:0.1}};
 const snapshot=JSON.stringify(detail);ctx.renderScriptRecovery(detail);const h=panel.innerHTML;
 assert(h.indexOf('model did not return a usable result')<h.indexOf('Technical details'));
 assert(/<details class="mb-3">/.test(h));assert(!/<details[^>]*\bopen/.test(h));
 const diagnostics=h.slice(h.indexOf('<details'),h.indexOf('</details>'));assert(diagnostics.includes('HTTP 503'));assert(diagnostics.includes('backoff 2s'));assert(diagnostics.includes('&lt;bad&gt;'));
 assert(!h.includes('<img'));assert(!h.includes('<script>'));assert(h.includes('日本語'));
 const actions=h.slice(h.indexOf('</details>'));
 for(const fn of ['retryScriptGeneration','onCopyRecoveryPrompt','onInjectRecoverySegmentation','onSkipRecoveryChunk']){assert(actions.includes('onclick="'+fn+'()"'));}
 assert(actions.includes('id="script-recovery-inject"'));assert.strictEqual(JSON.stringify(detail),snapshot);assert.strictEqual(ctx.window._scriptRecoveryDetail,detail);
}
ctx.renderScriptRecovery(null);assert.strictEqual(panel.innerHTML,'');assert.strictEqual(panel.style.display,'none');
"""
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_optional_detail_matches_dedicated_endpoint_for_failed_passes(self):
        for pass_name in ('segment', 'attribute', 'instruct'):
            with self.subTest(pass_name=pass_name), transactions.ScriptRecoveryTransactionTests().fixture() as (root, path):
                if pass_name != 'segment':
                    fixtures._write_failed_attribute_run(str(root))
                if pass_name == 'instruct':
                    checkpoint_path = Path(script.three_pass_checkpoint_path(path))
                    checkpoint = json.loads(checkpoint_path.read_text())
                    checkpoint['stage'] = 'instruct_failed'
                    checkpoint['failed']['pass'] = 'instruct'
                    for entry in checkpoint['failed']['entries']:
                        entry['speaker'] = 'HOLMES'
                    checkpoint_path.write_text(json.dumps(checkpoint))
                    manifest_path = Path(script.three_pass_manifest_path(path))
                    manifest = json.loads(manifest_path.read_text())
                    manifest['failed_pass'] = 'instruct'
                    manifest_path.write_text(json.dumps(manifest))
                api = FastAPI(); api.include_router(script.router)
                with TestClient(api) as client:
                    metadata = client.get('/api/generate_script/recovery').json()
                    combined = client.get('/api/generate_script/recovery?include_detail=true').json()
                    dedicated = client.get('/api/generate_script/recovery/detail').json()
                self.assertTrue(metadata['recoverable'])
                self.assertNotIn('detail', metadata)
                self.assertNotIn('source', metadata)
                self.assertEqual(metadata, {key: value for key, value in combined.items() if key != 'detail'})
                self.assertEqual(dedicated, combined['detail'])
                self.assertEqual(pass_name, combined['detail']['failed_pass'])

    def test_no_failed_checkpoint_keeps_retry_metadata_without_detail(self):
        with transactions.ScriptRecoveryTransactionTests().fixture() as (_root, path):
            Path(script.three_pass_checkpoint_path(path)).unlink()
            response = script.ensure_script_recovery_response(True)
            self.assertTrue(response['recoverable'])
            self.assertIsNone(response['detail'])

    def test_stale_book_and_completed_manifest_do_not_expose_detail(self):
        for change in ('book', 'complete'):
            with self.subTest(change=change), transactions.ScriptRecoveryTransactionTests().fixture() as (root, path):
                if change == 'book':
                    (root / 'state.json').write_text(json.dumps({
                        'input_file_path': 'new.txt', 'script_generation_input_file': 'old.txt'}))
                else:
                    manifest = Path(script.three_pass_manifest_path(path))
                    data = json.loads(manifest.read_text()); data['status'] = 'complete'
                    manifest.write_text(json.dumps(data))
                self.assertEqual({'recoverable': False}, script.ensure_script_recovery_response(True))

    def test_corrupt_checkpoint_fails_detail_without_hiding_metadata_or_editing_evidence(self):
        with transactions.ScriptRecoveryTransactionTests().fixture() as (_root, path):
            checkpoint = Path(script.three_pass_checkpoint_path(path))
            original = b'{"stage":'
            checkpoint.write_bytes(original)
            api = FastAPI(); api.include_router(script.router)
            with TestClient(api) as client:
                metadata = client.get('/api/generate_script/recovery')
                combined = client.get('/api/generate_script/recovery?include_detail=true')
                dedicated = client.get('/api/generate_script/recovery/detail')
            self.assertEqual(200, metadata.status_code)
            self.assertTrue(metadata.json()['recoverable'])
            self.assertEqual(409, combined.status_code)
            self.assertEqual(dedicated.json(), combined.json())
            self.assertEqual(original, checkpoint.read_bytes())

    def test_optional_reader_waits_for_complete_native_publication(self):
        with transactions.ScriptRecoveryTransactionTests().fixture() as (_root, path):
            entered, release, read = threading.Event(), threading.Event(), threading.Event()
            errors, responses = [], []
            actual_move = transactions.books._move
            def move(source, destination, **kwargs):
                result = actual_move(source, destination, **kwargs)
                if str(destination) == script.three_pass_checkpoint_path(path):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('publication not released')
                return result
            def publish():
                try:
                    script.apply_manual_recovery(transactions.ScriptRecoveryTransactionTests.entries(), 'manual')
                except BaseException as error:
                    errors.append(error)
            def observe(client):
                try:
                    responses.append(client.get('/api/generate_script/recovery?include_detail=true'))
                    read.set()
                except BaseException as error:
                    errors.append(error)
            api = FastAPI(); api.include_router(script.router)
            with TestClient(api) as client, patch.object(transactions.books, '_move', side_effect=move):
                writer = threading.Thread(target=publish); reader = threading.Thread(target=observe, args=(client,))
                writer.start()
                try:
                    self.assertTrue(entered.wait(2)); reader.start()
                    self.assertFalse(read.wait(.15), 'reader observed half-published recovery')
                finally:
                    release.set(); writer.join(5)
                    if reader.ident is not None:
                        reader.join(5)
            self.assertFalse(writer.is_alive() or reader.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(200, responses[0].status_code)

    def test_real_ui_handler_uses_one_request_and_preserves_controls(self):
        source = Path(__file__).parent.parent / 'static/js/app-core.js'
        js = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const handler=source.slice(source.indexOf('async function refreshScriptRecovery()'),source.indexOf('// Failed-request recovery panel'));
(async()=>{
 for(const mode of ['detail','no-detail','none','error']) {
  const calls=[],renders=[],toasts=[];const retry={style:{}},panel={style:{display:'block'}};
  const detail={recoverable:true,source:'frozen',prompt:{system:'system',user:'user'}};
  const context={document:{getElementById:id=>id==='btn-retry-script'?retry:panel},console:{debug(){}},
   API:{get:async path=>{calls.push(path);if(mode==='error'){throw new Error('unavailable');}
    return {recoverable:mode!=='none',failed_pass:'segment',detail:mode==='detail'?detail:null};}},
   showToast:(...args)=>toasts.push(args),renderScriptRecovery:value=>renders.push(value)};
  vm.runInNewContext(handler,context);await context.refreshScriptRecovery();
  assert.deepStrictEqual(calls,['/api/generate_script/recovery?include_detail=true']);
  assert.strictEqual(retry.style.display,(mode==='detail'||mode==='no-detail')?'inline-block':'none');
  if(mode==='error'){assert.strictEqual(panel.style.display,'none');}
  else{assert.strictEqual(renders[0],mode==='detail'?detail:null);}
  assert.strictEqual(toasts.length,(mode==='detail'||mode==='no-detail')?1:0);
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', js, str(source)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
