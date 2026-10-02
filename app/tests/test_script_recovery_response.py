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
            def move(source, destination):
                result = actual_move(source, destination)
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
