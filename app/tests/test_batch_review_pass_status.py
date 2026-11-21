"""A clean hindsight pass must not erase earlier review failures."""
import json
import os
import core
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import script


CLEAN = ['Review complete: 2 -> 2 entries', 'Total changes: 0']


class BatchReviewPassStatusTests(unittest.TestCase):
    def test_actual_batch_route_and_report_keep_both_passes_completion_requirements(self):
        cases = (
            ('forward_failed_batches', True, 0, CLEAN + ['Batches failed: 1'], 0, CLEAN, False, 'incomplete'),
            ('forward_vram_abort', True, 0, CLEAN + ['Batches skipped (low GPU VRAM): 1'], 0, CLEAN, False, 'incomplete'),
            ('forward_process_failed', True, 1, ['fixture reviewer crashed'], 0, CLEAN, False, 'failed'),
            ('forward_fatal_marker', False, 1,
             ['REVIEW_FAILURE_JSON: {"message": "Checkpoint write failed: disk full", '
              '"kind": "resume_misaligned"}'],
             0, CLEAN, False, 'failed'),
            ('forward_missing_stats', True, 0, ['fixture no summary'], 0, CLEAN, False, 'incomplete'),
            ('forward_nicknames_failed', True, 0, CLEAN, 0, CLEAN, True, 'failed'),
            ('backward_failed', True, 0, CLEAN, 1, ['fixture hindsight crashed'], False, 'failed'),
            ('both_clean', True, 0, CLEAN, 0, CLEAN, False, 'done'),
            ('single_clean', False, 0, CLEAN, 0, CLEAN, False, 'done'),
        )
        for label, bidirectional, fwd_rc, fwd_lines, bwd_rc, bwd_lines, nick_failure, expected in (*cases, ('single_forced', False, 0, CLEAN, 0, CLEAN, False, 'done')):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scripts = root / 'scripts'
                reports = root / 'reports'
                scripts.mkdir()
                book = scripts / 'book.json'
                original = json.dumps([{'speaker':'ALICE','text':'Keep this exact text.'}])
                book.write_text(original)
                state = {'batch_review':{'running':False,'cancel':False}}
                calls, summary_flags = [], []

                def stream(command, _cwd, current, **kwargs):
                    phase = current['current_pass']
                    nickname = any(str(arg).endswith('find_nicknames.py') for arg in command)
                    calls.append((phase,'nickname' if nickname else 'review'))
                    if nickname:
                        return (1,['fixture discovery crashed']) if nick_failure and phase == 'fwd' else (0,[])
                    self.assertEqual(phase == 'bwd' or label == 'single_forced', '--force-review' in command)
                    return (fwd_rc,fwd_lines) if phase == 'fwd' else (bwd_rc,bwd_lines)

                def summary(lines, _insert, _totals, incomplete):
                    summary_flags.append(incomplete)
                    return [*lines, 'CPU fixture summary partial=' + str(incomplete)]

                app = FastAPI()
                app.include_router(script.router)
                with patch.object(script,'SCRIPTS_DIR',str(scripts)), \
                     patch.object(script,'CHARACTER_ALIASES_PATH',str(root/'aliases.json')), \
                     patch.object(script,'REPORTS_DIR',str(reports)), \
                     patch.object(script,'ROOT_DIR',str(root)), \
                     patch.object(script,'process_state',state), \
                     patch.object(script,'check_global_gpu_lock') as check, \
                     patch.object(core,'claim_gpu_task',wraps=core.claim_gpu_task) as claim, \
                     patch.object(core,'process_state',state), \
                     patch.object(core,'_task_claims',{}), \
                     patch.object(core,'_gpu_leases',{}), \
                     patch.object(core,'acquire_gpu_lock',return_value=None), \
                     patch.object(core,'llm_is_on_this_gpu',return_value=True), \
                     patch.object(script,'_init_task_log',return_value=str(root/'task.log')), \
                     patch.object(script,'_stream_subprocess_to_logs',side_effect=stream), \
                     patch.object(script,'_insert_llm_summary',side_effect=summary), \
                     patch.object(script,'_run_claimed_background_task',side_effect=lambda name,work:work()), \
                     TestClient(app) as client:
                    response = client.post('/api/review_script/batch/start',json={
                        'script_names':['book'],'dedupe_speakers':True,
                        'find_nicknames':True,'bidirectional':bidirectional,'force_review':label == 'single_forced'})
                    self.assertEqual(200,response.status_code,response.text)
                    check.assert_called_once_with('batch_review')
                    claim.assert_called_once_with('batch_review', resources=(os.path.normcase(str(book.resolve())),))
                current = state['batch_review']
                self.assertFalse(current['running'])
                self.assertEqual(expected,current['tasks'][0]['status'])
                if label == 'forward_fatal_marker':
                    self.assertEqual('Checkpoint write failed: disk full',
                                     current['tasks'][0]['failure_message'])
                    self.assertEqual('resume_misaligned',
                                     current['tasks'][0]['failure_kind'])
                self.assertEqual([expected != 'done'],summary_flags)
                self.assertEqual(original,book.read_text())
                self.assertEqual(1,len(current['artifacts']))
                report = Path(current['artifacts'][0]['artifact_path']).read_text()
                self.assertIn('CPU fixture summary partial=' + str(expected != 'done'),report)
                if expected == 'incomplete':
                    self.assertIn('only partially reviewed',report)
                elif expected == 'failed':
                    self.assertIn('could not be reviewed',report)
                else:
                    self.assertNotIn('only partially reviewed',report)
                    self.assertNotIn('could not be reviewed',report)
                self.assertEqual(1,sum(phase=='fwd' and kind=='review' for phase,kind in calls) if not nick_failure else sum(phase=='fwd' and kind=='nickname' for phase,kind in calls))
                self.assertEqual(int(bidirectional),sum(phase=='bwd' and kind=='review' for phase,kind in calls))
                self.assertEqual(1,len(list(reports.glob('*.md'))))


class BatchOptionAdmissionTests(unittest.TestCase):
    def test_effective_flags_keep_legacy_omitted_default_behavior(self):
        req = script.BatchReviewRequest(script_names=['book'])
        self.assertEqual((True, True, False), script._resolve_batch_review_options(req))
        req = script.BatchReviewRequest(script_names=['book'], dedupe_speakers=False)
        self.assertEqual((False, False, False), script._resolve_batch_review_options(req))
        req = script.BatchReviewRequest(script_names=['book'], find_nicknames=False,
                                        dedupe_speakers=False, bidirectional=False)
        self.assertEqual((False, False, False), script._resolve_batch_review_options(req))

    def test_contradictions_are_refused_before_gpu_or_task_claim(self):
        payloads = (
            {'dedupe_speakers': False, 'find_nicknames': True},
            {'find_nicknames': False, 'bidirectional': True},
            {'dedupe_speakers': False, 'bidirectional': True},
            {'dedupe_speakers': True, 'find_nicknames': False, 'bidirectional': True},
        )
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scripts = root / 'scripts'; scripts.mkdir()
                (scripts / 'book.json').write_text('[]')
                state = {'batch_review': {'running': False, 'cancel': False}}
                app = FastAPI(); app.include_router(script.router)
                app.add_exception_handler(core.AppError, core.handle_app_error)
                with patch.object(script, 'SCRIPTS_DIR', str(scripts)), \
                     patch.object(script, 'process_state', state), \
                     patch.object(core, 'process_state', state), \
                     patch.object(script, 'check_global_gpu_lock') as check, \
                     patch.object(core, 'claim_gpu_task') as claim, \
                     TestClient(app) as client:
                    response = client.post('/api/review_script/batch/start',
                                           json={'script_names': ['book'], **payload})
                self.assertEqual(422, response.status_code, response.text)
                body = response.json()
                self.assertIn('message_key', body)
                self.assertTrue(body['message_key'].startswith('error.review.batch.'))
                check.assert_not_called()
                claim.assert_not_called()

    def test_valid_defaults_start_and_report_effective_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / 'scripts'; scripts.mkdir()
            (scripts / 'book.json').write_text('[]')
            state = {'batch_review': {'running': False, 'cancel': False}}
            app = FastAPI(); app.include_router(script.router)
            with patch.multiple(script, DATA_DIR=str(root), SCRIPTS_DIR=str(scripts),
                                CHARACTER_ALIASES_PATH=str(root / 'aliases.json'),
                                REPORTS_DIR=str(root / 'reports'), ROOT_DIR=str(root),
                                process_state=state), \
                 patch.multiple(core, DATA_DIR=str(root), process_state=state,
                                _task_claims={}, _gpu_leases={}), \
                 patch.object(core, 'acquire_gpu_lock', return_value=None), \
                 patch.object(core, 'llm_is_on_this_gpu', return_value=True), \
                 patch.object(script, 'check_global_gpu_lock') as check, \
                 patch.object(core, 'claim_gpu_task', wraps=core.claim_gpu_task) as claim, \
                 patch.object(script, '_init_task_log', return_value=str(root / 'task.log')), \
                 patch.object(script, '_stream_subprocess_to_logs', return_value=(0, CLEAN)), \
                 patch.object(script, '_insert_llm_summary', side_effect=lambda lines, *_a, **_k: lines), \
                 patch.object(script, '_run_claimed_background_task', side_effect=lambda name, work: work()), \
                 TestClient(app) as client:
                keys = ('dedupe_speakers', 'find_nicknames', 'bidirectional')
                runs = (({}, (True, True, False)),
                        ({'bidirectional': True}, (True, True, True)),
                        ({'dedupe_speakers': False}, (False, False, False)))
                for options, expected in runs:
                    with self.subTest(options=options):
                        response = client.post('/api/review_script/batch/start',
                                               json={'script_names': ['book'], **options})
                        self.assertEqual(200, response.status_code, response.text)
                        status = client.get('/api/status/batch_review')
                        self.assertEqual(200, status.status_code, status.text)
                        self.assertEqual(expected, tuple(response.json()[key] for key in keys))
                        self.assertEqual(expected, tuple(status.json()[key] for key in keys))
            self.assertEqual(3, check.call_count)
            self.assertEqual(3, claim.call_count)
