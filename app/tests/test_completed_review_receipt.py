"""A completed unchanged native review must not dispatch another batch."""
import json
from pathlib import Path
import tempfile
import unittest
from tests import test_llm_review_regressions as fixtures

class CompletedReviewReceiptTests(unittest.TestCase):
    def test_otherwise_valid_receipt_rejects_unsupported_or_noninteger_version(self):
        import completed_review_receipt as receipts
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)/'book.json';entries=[{'text':'Unchanged.'}]
            output.write_text(json.dumps(entries));digest=receipts.get_review_entries_fingerprint(entries)
            receipts.save_completed_review_receipt(str(output),str(output),digest,digest,'settings')
            path=Path(receipts.get_review_receipt_path(str(output)));valid=json.loads(path.read_text())
            self.assertTrue(receipts.get_completed_review_match(str(output),str(output),digest,'settings')[0])
            for version in (2,-1,True,1.0,'1',None):
                with self.subTest(version=version):
                    path.write_text(json.dumps({**valid,'version':version}));before=path.read_bytes()
                    self.assertFalse(receipts.get_completed_review_match(str(output),str(output),digest,'settings')[0])
                    self.assertEqual(before,path.read_bytes())

    def test_unchanged_in_place_review_dispatches_no_second_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'book.json'
            rows = [{'speaker': 'ANN', 'text': 'The same line remains unchanged.', 'instruct': 'Plain.'}]
            output.write_text(json.dumps(rows))
            calls = []
            def batch(client, model, entries, *args, **kwargs):
                calls.append(entries)
                return entries
            runner = fixtures.ReviewVramTests()
            runner.run_review(output, [True] * 4, batch, lambda *args, **kwargs: ({}, 0, []))
            self.assertEqual(1, len(calls), 'fixture must dispatch its first native review')
            before = {p.name: p.read_bytes() for p in Path(tmp).iterdir() if p.is_file()}
            calls.clear()
            runner.run_review(output, [True] * 4, batch, lambda *args, **kwargs: ({}, 0, []))
            self.assertEqual([], calls, 'unchanged completed review still dispatches work')
            self.assertEqual(before, {p.name: p.read_bytes() for p in Path(tmp).iterdir() if p.is_file()})

    def test_receipt_verifies_separate_input_output_settings_and_checkpoint(self):
        import completed_review_receipt as receipts
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'source.json', root / 'output.json'
            original, corrected = [{'text': 'Original.'}], [{'text': 'Corrected.'}]
            source.write_text(json.dumps(original)); output.write_text(json.dumps(corrected))
            input_sha = receipts.get_review_entries_fingerprint(original)
            output_sha = receipts.get_review_entries_fingerprint(corrected)
            receipts.save_completed_review_receipt(str(source), str(output), input_sha, output_sha, 'settings')
            def match(input_hash=input_sha, fingerprint='settings'):
                return receipts.get_completed_review_match(str(source), str(output), input_hash, fingerprint)[0]
            self.assertTrue(match())
            self.assertFalse(match('changed input'))
            self.assertFalse(match(fingerprint='changed settings'))
            output.write_text(json.dumps(original)); self.assertFalse(match())
            output.write_text(json.dumps(corrected))
            checkpoint = Path(str(output) + '.review_checkpoint.json')
            checkpoint.write_text('{}'); self.assertFalse(match()); checkpoint.unlink()
            receipt = Path(receipts.get_review_receipt_path(str(output)))
            receipt.write_text('malformed'); self.assertFalse(match())
            receipts.save_completed_review_receipt(str(source), str(output), input_sha, output_sha, 'settings')
            output.unlink(); self.assertFalse(match())

    def test_standard_and_contextual_force_requests_reach_subprocess(self):
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import script
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'book.json'; path.write_text('[{"text":"Hello."}]')
            app = FastAPI(); app.include_router(script.router)
            with patch.object(script, 'SCRIPT_PATH', str(path)), patch.object(script, 'check_global_gpu_lock'), patch.object(script, 'schedule_claimed_background_task') as schedule, patch.object(script, 'load_app_config', return_value={}), TestClient(app) as client:
                for endpoint in ('/api/review_script', '/api/review_script_contextual'):
                    for force in (False, True):
                        response = client.post(endpoint, json={'force_review': force, 'dedupe_speakers': False})
                        self.assertEqual(200, response.status_code, response.text)
                        command = schedule.call_args.args[3]
                        self.assertEqual(force, '--force-review' in command)

    def test_native_transport_repeat_force_and_failed_rerun_receipts(self):
        from contextlib import ExitStack, redirect_stdout
        from io import StringIO
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        import sys
        import review_script as review
        import generate_script
        import completed_review_receipt as receipts
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'book.json'
            rows = [{'speaker': 'ANN', 'text': 'The same line remains unchanged.', 'instruct': 'Plain.'}]
            path.write_text(json.dumps(rows))
            response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(rows)), finish_reason='stop')], usage=None)
            create = Mock(return_value=response)
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=Mock())
            def run(force=False, headroom=True, sidecars=None, dedupe=None):
                with ExitStack() as stack:
                    stack.enter_context(redirect_stdout(StringIO()))
                    values = {'load_app_config': {'generation': {'review_batch_size': 1}},
                        'get_active_llm_config': {}, 'ensure_ideal_settings': (False, {}, 'fixture settings'),
                        'get_current_status': {'loaded': False}, 'make_run_client': client,
                        'wait_for_vram_headroom': headroom, 'get_cached_or_benchmarked_concurrency': 1}
                    for name, value in values.items():
                        stack.enter_context(patch.object(review, name, return_value=value))
                    stack.enter_context(patch.object(generate_script, 'get_response_log_path', return_value=str(Path(tmp) / 'response.log')))
                    argv = ['review', '--input', str(path)] + (['--force-review'] if force else [])
                    if sidecars:
                        argv += ['--dedupe-speakers', '--alias-registry', str(sidecars[0]), '--remap-voice-config', str(sidecars[1])]
                        stack.enter_context(patch.object(review, 'dedupe_speakers', side_effect=dedupe))
                    stack.enter_context(patch.object(sys, 'argv', argv))
                    review.main()
            run(); self.assertEqual(1, create.call_count)
            receipt = Path(receipts.get_review_receipt_path(str(path)))
            self.assertTrue(receipt.exists())
            original_receipt = receipt.read_bytes()
            run(); self.assertEqual(1, create.call_count)
            self.assertEqual(original_receipt, receipt.read_bytes())
            client.close.assert_called_once()
            run(force=True); self.assertEqual(2, create.call_count)
            run(force=True, headroom=False)
            self.assertEqual(2, create.call_count)
            self.assertFalse(receipt.exists(), 'failed forced rerun left a reusable success receipt')
            self.assertTrue(Path(review._checkpoint_path(str(path))).exists())
            run(); self.assertEqual(3, create.call_count)
            self.assertTrue(receipt.exists())
            with patch.object(review, 'save_completed_review_receipt', side_effect=OSError('receipt publication failed')):
                with self.assertRaisesRegex(OSError, 'receipt publication failed'):
                    run(force=True)
            self.assertEqual(4, create.call_count)
            self.assertFalse(receipt.exists())
            self.assertEqual(rows, json.loads(path.read_text()))
            self.assertTrue(Path(review._checkpoint_path(str(path))).exists())
            run()
            self.assertTrue(receipt.exists(), 'completed checkpoint could not recover receipt publication')
            aliases, voices = Path(tmp) / 'aliases.json', Path(tmp) / 'voices.json'
            aliases.write_text('{}'); voices.write_text('{"ANN":{"voice":"fixture voice"}}')
            def dedupe(*args, **kwargs):
                aliases.write_text('{"ANN":"ANNE"}')
                return {'ANN':'ANNE'}, 1, [(0, 'speaker', 'ANNE')]
            run(force=True, sidecars=(aliases, voices), dedupe=dedupe)
            count = create.call_count
            self.assertEqual('ANNE', json.loads(path.read_text())[0]['speaker'])
            self.assertEqual({'ANNE':{'voice':'fixture voice'}}, json.loads(voices.read_text()))
            run(sidecars=(aliases, voices), dedupe=dedupe)
            self.assertEqual(count, create.call_count, 'completed sidecar changes invalidated their own receipt')


    def test_key_covers_prompts_parameters_profiles_context_and_sidecars(self):
        import copy
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import patch
        import review_script as review
        from generate_script import LLMGenParams
        with tempfile.TemporaryDirectory() as tmp:
            aliases, voices = Path(tmp) / 'aliases.json', Path(tmp) / 'voices.json'
            aliases.write_text('{}'); voices.write_text('{}')
            args = SimpleNamespace(context_window=0, dedupe_speakers=True, alias_registry=str(aliases), remap_voice_config=str(voices))
            params = LLMGenParams(system_prompt='system', user_prompt_template='{batch}')
            config = {'generation': {'review_batch_size': 25}, 'llm_mode': 'local'}
            profile = {'base_url': 'http://fixture/v1', 'model_name': 'model', 'api_key': 'env:RECEIPT_TEST_KEY'}
            def key(cfg=config, llm=profile, par=params, options=args, model='model'):
                return review.get_completed_review_fingerprint(cfg, llm, par, options, object(), model)
            with patch.dict('os.environ', RECEIPT_TEST_KEY='first-secret'):
                baseline = key()
                for field, value in [('system_prompt','new system'), ('user_prompt_template','new {batch}'), ('temperature',.7), ('max_tokens',300), ('context_length',8192), ('structured_output','off')]:
                    with self.subTest(field=field): self.assertNotEqual(baseline, key(par=replace(params, **{field:value})))
                for field, value in [('base_url','http://other/v1'), ('model_name','other'), ('provider_extra_body',{'reasoning_effort':'low'})]:
                    with self.subTest(field=field): self.assertNotEqual(baseline, key(llm={**profile,field:value}))
                for field, value in [('context_window',4), ('dedupe_speakers',False), ('alias_registry',None), ('remap_voice_config',None)]:
                    changed = copy.copy(args); setattr(changed, field, value)
                    with self.subTest(field=field): self.assertNotEqual(baseline, key(options=changed))
                self.assertNotEqual(baseline, key(cfg={**config,'generation':{'review_batch_size':26}}))
                self.assertNotEqual(baseline, key(cfg={**config,'generation':{'review_batch_size':25,'merge_narrators':True}}))
                self.assertNotEqual(baseline, key(model='different binding'))
                self.assertNotEqual(baseline, key(cfg={**config,'llm_failover':True,'llm_remote':{'base_url':'http://secondary/v1','model_name':'secondary'}}))
                self.assertEqual(baseline, key(llm={**profile,'concurrency':4,'concurrency_for':'cache','concurrency_environment':{'gpu':'cache'}}))
                aliases.write_text('{"ANN":"ANNE"}'); self.assertNotEqual(baseline, key()); aliases.write_text('{}')
                voices.write_text('{"ANN":"new voice"}'); self.assertNotEqual(baseline, key()); voices.write_text('{}')
            with patch.dict('os.environ', RECEIPT_TEST_KEY='second-secret'):
                self.assertNotEqual(baseline, key())
