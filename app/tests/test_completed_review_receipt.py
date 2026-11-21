"""A completed unchanged native review must not dispatch another batch."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
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
            with patch.object(script, 'SCRIPT_PATH', str(path)), patch.object(script, 'check_global_gpu_lock'), patch.object(script, 'reserve_background_task', return_value='claim'), patch.object(script, 'register_claimed_background_task') as schedule, patch.object(script, 'load_app_config', return_value={}), TestClient(app) as client:
                for endpoint in ('/api/review_script', '/api/review_script_contextual'):
                    for force in (False, True):
                        response = client.post(endpoint, json={'force_review': force, 'dedupe_speakers': False})
                        self.assertEqual(200, response.status_code, response.text)
                        command = schedule.call_args.args[4]
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
                        'wait_for_vram_headroom': headroom, 'ensure_cached_or_benchmarked_concurrency': 1}
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
            for previous_version in (7, 8, 9):
                with patch.object(review, 'REVIEW_ALGORITHM_VERSION', previous_version):
                    old_review_contract = key()
                self.assertNotEqual(old_review_contract, key())
            with patch.dict('os.environ', RECEIPT_TEST_KEY='first-secret'):
                baseline = key()
                for field, value in [('system_prompt','new system'), ('user_prompt_template','new {batch}'), ('temperature',.7), ('max_tokens',300), ('context_length',8192), ('structured_output','off'), ('review_labels_only',True)]:
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
                for revision in ('1' * 32, '2' * 32):
                    self.assertEqual(baseline, key(llm={**profile, 'concurrency_revision': revision}))
                    with patch.object(review, 'get_failover_llm_config', return_value=profile):
                        failover_baseline = key()
                    with patch.object(review, 'get_failover_llm_config',
                                      return_value={**profile, 'concurrency_revision': revision}):
                        self.assertEqual(failover_baseline, key())
                aliases.write_text('{"ANN":"ANNE"}'); self.assertNotEqual(baseline, key()); aliases.write_text('{}')
                voices.write_text('{"ANN":"new voice"}'); self.assertNotEqual(baseline, key()); voices.write_text('{}')
            with patch.dict('os.environ', RECEIPT_TEST_KEY='second-secret'):
                self.assertNotEqual(baseline, key())

    def test_manual_receipt_written_with_main_fingerprint_still_matches(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import completed_review_receipt as receipts
        import review_script as review
        from generate_script import LLMGenParams
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'source.json', Path(tmp) / 'output.json'
            entries = [{'speaker': 'ANN', 'text': 'The gate opens.', 'instruct': ''}]
            source.write_text(json.dumps(entries)); output.write_text(json.dumps(entries))
            sha = receipts.get_review_entries_fingerprint(entries)
            args = SimpleNamespace(context_window=0, dedupe_speakers=False,
                                   alias_registry=None, remap_voice_config=None)
            params = LLMGenParams(system_prompt='sys', user_prompt_template='{batch}')
            config = {'generation': {'review_batch_size': 25}, 'llm_mode': 'local'}
            manual = {'transport': 'manual', 'model_name': 'fixture',
                      'base_url': 'https://hosted.invalid/v1', 'api_key': 'local'}
            current = review.get_completed_review_fingerprint(
                config, manual, params, args, object(), 'fixture')
            main_fingerprint = review.get_completed_review_fingerprint(
                config, manual, params, args, object(), 'fixture', legacy=True)
            self.assertNotEqual(current, main_fingerprint,
                                'fixture must discriminate the pre-fix digest')
            legacy = review.get_legacy_review_fingerprint(
                config, manual, params, args, object(), 'fixture')
            self.assertEqual(main_fingerprint, legacy)

            receipts.save_completed_review_receipt(str(source), str(output), sha, sha, main_fingerprint)
            self.assertFalse(receipts.get_completed_review_match(
                str(source), str(output), sha, current)[0])
            self.assertTrue(receipts.get_completed_review_match(
                str(source), str(output), sha, current, (legacy,))[0],
                'an unchanged Manual receipt from the previous release must still match')
            other = review.get_completed_review_fingerprint(
                config, {**manual, 'base_url': 'https://other.invalid/v1'}, params, args,
                object(), 'fixture', legacy=True)
            self.assertFalse(receipts.get_completed_review_match(
                str(source), str(output), sha, current, (other,))[0],
                'a legacy digest from another endpoint must not be reused')

            http = {'base_url': 'https://hosted.invalid/v1', 'model_name': 'fixture', 'api_key': 'local'}
            secondary = {'base_url': 'http://secondary/v1', 'model_name': 's', 'api_key': 'local'}
            with patch.object(review, 'get_failover_llm_config', return_value=secondary):
                self.assertIsNone(review.get_legacy_review_fingerprint(
                    config, http, params, args, object(), 'fixture'))


class LegacyReceiptContextTests(unittest.TestCase):
    def _case(self, *, base_url='https://hosted.invalid/v1', api_key='', failover=False):
        from types import SimpleNamespace
        from generate_script import LLMGenParams
        import review_script as review
        config = {'generation': {'review_batch_size': 2}, 'llm_mode': 'local'}
        primary = {'transport': 'manual', 'api_key': api_key,
                   'base_url': base_url, 'model_name': 'fixture'}
        config['llm_local'] = primary
        if failover:
            config['llm_failover'] = True
            config['llm_remote'] = {'base_url': 'http://127.0.0.1:1234/v1',
                                    'model_name': 'secondary', 'api_key': ''}
        params = LLMGenParams(system_prompt='sys', user_prompt_template='{batch}')
        args = SimpleNamespace(context_window=0, dedupe_speakers=False,
                               alias_registry=None, remap_voice_config=None)
        return review, config, primary, params, args

    def test_manual_blank_hosted_key_matches_historical_ambient_digest_only_locally(self):
        review, config, primary, params, args = self._case()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-synthetic'}, clear=False):
            canonical = review.get_completed_review_fingerprint(
                config, primary, params, args, object(), 'fixture')
            legacy = review.get_legacy_review_fingerprint(
                config, primary, params, args, object(), 'fixture')
            with patch.object(review, 'resolve_historical_api_key',
                              return_value='sk-different-fixture'):
                altered = review.get_legacy_review_fingerprint(
                    config, primary, params, args, object(), 'fixture')
        self.assertIsNotNone(legacy)
        self.assertNotEqual(canonical, legacy,
                            'the blank Manual key must reproduce the historical digest')
        self.assertNotEqual(legacy, altered,
                            'the historical resolver must be the matching source')
        for digest in (canonical, legacy, altered):
            self.assertNotIn('sk-synthetic', digest)
            self.assertNotIn('sk-different-fixture', digest)

    def test_changed_secondary_refuses_regardless_of_current_failover_flag(self):
        review, config, primary, params, args = self._case(failover=True)
        for used in (True, False):
            with self.subTest(failover_used=used), \
                    patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-synthetic'}, clear=False), \
                    patch.object(review, 'get_run_model_binding',
                                 return_value={'primary_model': 'fixture',
                                               'failover_model': 'secondary',
                                               'failover_used': used}):
                self.assertIsNone(review.get_legacy_review_fingerprint(
                    config, primary, params, args, object(), 'fixture'),
                    'the current client flag cannot prove a historical secondary was unused')
        other, other_config, other_primary, _, _ = self._case(
            base_url='https://other.invalid/v1')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-synthetic'}, clear=False):
            changed = other.get_legacy_review_fingerprint(
                other_config, other_primary, params, args, object(), 'fixture')
            unchanged = review.get_legacy_review_fingerprint(
                config, primary, params, args, object(), 'fixture')
        self.assertNotEqual(unchanged, changed,
                            'a changed endpoint must not reuse the old receipt')

    def test_secondary_credential_equivalence_decides_historical_matching(self):
        from types import SimpleNamespace
        from generate_script import LLMGenParams
        import review_script as review
        params = LLMGenParams(system_prompt='sys', user_prompt_template='{batch}')
        args = SimpleNamespace(context_window=0, dedupe_speakers=False,
                               alias_registry=None, remap_voice_config=None)
        for secondary_key, expect_legacy in (('', False), ('secondary-key', True)):
            with self.subTest(secondary_key=secondary_key), \
                    patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-synthetic'}, clear=False):
                config = {'generation': {'review_batch_size': 2}, 'llm_mode': 'local',
                          'llm_failover': True,
                          'llm_local': {'transport': 'manual', 'api_key': '',
                                        'base_url': 'https://hosted.invalid/v1',
                                        'model_name': 'fixture'},
                          'llm_remote': {'api_key': secondary_key,
                                         'base_url': 'http://127.0.0.1:1234/v1',
                                         'model_name': 'secondary'}}
                legacy = review.get_legacy_review_fingerprint(
                    config, config['llm_local'], params, args, object(), 'fixture')
                if expect_legacy:
                    self.assertIsNotNone(legacy,
                                         'an unchanged secondary keeps historical matching')
                    self.assertNotIn('sk-synthetic', legacy)
                else:
                    self.assertIsNone(legacy,
                                      'a changed secondary could have received the ambient key')

        config['llm_remote']['api_key'] = ''
        config['llm_remote']['provider_headers'] = {'Authorization': 'Token kept-fixture'}
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-synthetic'}, clear=False):
            explicit_legacy = review.get_legacy_review_fingerprint(
                config, config['llm_local'], params, args, object(), 'fixture')
        self.assertIsNotNone(explicit_legacy,
                             'an explicit header keeps dispatch equivalent across eras')

    def test_pre_pr_manual_checkpoint_with_offset_resumes_only_with_legacy_digest(self):
        import review_script as review
        review, config, primary, params, args = self._case()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-synthetic'}, clear=False):
            legacy = review.get_legacy_review_fingerprint(
                config, primary, params, args, object(), 'fixture')
        canonical = review.get_completed_review_fingerprint(
            config, primary, params, args, object(), 'fixture')
        entries = [{'speaker': 'ANN', 'text': text, 'instruct': 'Neutral.'}
                   for text in ('First fixture line.', 'Second fixture line.', 'Tail fixture line.')]
        corrected = entries[:2]
        stats = {key: 0 for key in review.REVIEW_EDIT_COUNTERS}
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'reviewed.json')
            review.save_checkpoint(output, 1, 2, 2, 0, corrected, stats, corrected[-2:],
                                   [len(corrected)], [], review._entries_fingerprint(entries),
                                   execution_sha256=legacy)
            resumed = review._load_resume_state(output, 2, 2, 0, entries, [], stats,
                execution_sha256=canonical, legacy_execution_sha256=(legacy,))
            self.assertEqual(2, resumed[6], 'a genuine pre-PR checkpoint must resume at its offset')
            fresh = review._load_resume_state(output, 2, 2, 0, entries, [], stats,
                execution_sha256=canonical)
            self.assertEqual(0, fresh[6], 'the canonical digest alone must refuse the old checkpoint')
            changed_source = [{**row, 'text': row['text'] + ' edited'} for row in entries]
            self.assertIsNone(review.load_checkpoint(output, 2, 2, 0, changed_source,
                execution_sha256=canonical, legacy_execution_sha256=(legacy,)))
            self.assertIsNone(review.load_checkpoint(output, 2, 3, 0, entries,
                execution_sha256=canonical, legacy_execution_sha256=(legacy,)))
            other, other_config, other_primary, _, _ = self._case(
                base_url='https://other.invalid/v1')
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-synthetic'}, clear=False):
                other_legacy = other.get_legacy_review_fingerprint(
                    other_config, other_primary, params, args, object(), 'fixture')
            other_canonical = other.get_completed_review_fingerprint(
                other_config, other_primary, params, args, object(), 'fixture')
            self.assertNotEqual(legacy, other_legacy)
            self.assertIsNone(review.load_checkpoint(output, 2, 2, 0, entries,
                execution_sha256=other_canonical, legacy_execution_sha256=(other_legacy,)),
                'a changed effective profile must refuse the old checkpoint')
            review.save_checkpoint(output, 2, 2, 2, 0, entries, stats, entries[-2:],
                                   [len(entries)], [], review._entries_fingerprint(entries),
                                   execution_sha256=canonical)
            written = json.loads(Path(review._checkpoint_path(output)).read_text())
            self.assertEqual(canonical, written['execution_sha256'],
                             'future writes must use the canonical digest')
