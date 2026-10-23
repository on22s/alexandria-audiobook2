"""Current-book delivery proofs, bounded failed attempts and request policies."""
from contextlib import contextmanager
import asyncio
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from fastapi import BackgroundTasks

import export_quality
import core
import export_quality_session as session
from routers import editor
from tests import test_export_task_results as tasks
from tests import test_project_export_quality as media


class ExportQualityApiTests(unittest.TestCase):
    def merge(self, client, body):
        snapshot = client.get('/api/editor/integrity').json()['snapshot']
        return client.post('/api/merge', json={**body, 'integrity_confirmation': snapshot})

    @contextmanager
    def fixture(self, *, interior_silence=False):
        with tasks.ExportTaskResultTests().fixture() as (root, states, client):
            manager, _ = media.ProjectExportQualityTests().fixture(root, interior_silence)
            script = Path(root) / 'annotated_script.json'
            script.write_text(json.dumps(manager.load_chunks()))
            with patch.object(editor, 'SCRIPT_PATH', str(script)), \
                    patch.object(core, 'acquire_gpu_lock', return_value=None), \
                    patch.object(editor, 'project_manager', manager):
                token = editor.get_snapshot_digest(editor.get_editor_book_identity(root))
                yield Path(root), manager, states, client, token

    def test_actual_export_routes_publish_current_proofs_and_request_scoped_policy(self):
        options = dict(silence_dbfs=-45, max_unexpected_silence_seconds=.2,
                       reject_unexpected_silence=True)
        with self.fixture() as (root, manager, states, client, token):
            for route, body in [('/api/merge', {'export_quality': options}),
                    ('/api/merge_m4b', {'export_quality': options}),
                    ('/api/export_chapters', {'format': 'wav', 'export_quality': options}),
                    ('/api/export_audacity', {})]:
                response = self.merge(client, body) if route == '/api/merge' else client.post(route, json=body)
                self.assertEqual(200, response.status_code, response.text)
            with patch.object(export_quality, 'get_export_file_identity', side_effect=AssertionError('evidence hashed audio')), \
                    patch.object(export_quality, 'validate_finished_export', side_effect=AssertionError('evidence decoded audio')):
                result = client.get('/api/export_quality', params={'expected_book_token': token})
            self.assertEqual(200, result.status_code, result.text)
            evidence = result.json()
            self.assertEqual('metadata_only', evidence['identity_check'])
            self.assertEqual(set(session.TASKS), {row['task'] for row in evidence['reports']})
            self.assertEqual([], evidence['rejected_attempts'])
            self.assertEqual(0, evidence['unavailable'])
            for report in evidence['reports']:
                path = root / report['file']
                self.assertEqual(export_quality.get_export_file_identity(path)['sha256'], report['sha256'])
                self.assertEqual(export_quality.get_export_file_version(path), report['file_version'])
                if report['task'] == 'audacity_export':
                    self.assertEqual('audacity_structure_and_frames', report['verification_scope'])
                    self.assertNotIn('options', report)
                else:
                    self.assertEqual(options, report['options'])
            # The next omitted option uses defaults, not the last request's policy.
            self.merge(client, {})
            self.assertEqual(export_quality.get_export_quality_options(),
                             states['audio']['export_quality_reports'][-1]['options'])

    def test_invalid_options_are_rejected_before_claim_or_evidence_mutation(self):
        invalid = ({'silence_dbfs': -100}, {'silence_dbfs': 'NaN'},
            {'max_unexpected_silence_seconds': 0}, {'reject_unexpected_silence': 'yes'}, {'unknown': True})
        with self.fixture() as (_, manager, states, client, _token):
            for route in ('/api/merge', '/api/merge_m4b', '/api/export_chapters'):
                for options in invalid:
                    with self.subTest(route=route, options=options):
                        before = copy.deepcopy(states)
                        response = client.post(route, json={'export_quality': options})
                        self.assertEqual(422, response.status_code, response.text)
                        self.assertEqual(before, states)

    def test_cancelled_cpu_workers_preserve_queued_and_active_attempt_states(self):
        with self.fixture() as (root, manager, states, _client, _token):
            for name, method, start in (
                    ('audacity_export', 'export_audacity', lambda background: editor.export_audacity_endpoint(background)),
                    ('m4b_export', 'merge_m4b', lambda background: editor.merge_m4b_endpoint(editor.M4bExportRequest(), background)),
                    ('chapter_export', 'export_chapters', lambda background: editor.export_chapters(editor.ChapterExportRequest(), background))):
                with self.subTest(task=name):
                    background = BackgroundTasks()
                    self.assertEqual('started', asyncio.run(start(background))['status'])
                    before = {str(path.relative_to(root)): path.read_bytes()
                              for path in root.rglob('*') if path.is_file()}
                    states[name]['cancel'] = True
                    with patch.object(editor, 'ensure_book_state', side_effect=AssertionError('cancelled worker opened book')), \
                            patch.object(manager, method, side_effect=AssertionError('cancelled worker started export')):
                        asyncio.run(background())
                    self.assertEqual({'status': 'cancelled', 'message': 'Export cancelled'}, states[name]['result'])
                    self.assertNotIn('export_quality_attempt', states[name])
                    self.assertFalse(states[name]['running'])
                    self.assertEqual(before, {str(path.relative_to(root)): path.read_bytes()
                                             for path in root.rglob('*') if path.is_file()})
                    background = BackgroundTasks()
                    self.assertEqual('started', asyncio.run(start(background))['status'])
                    with patch.object(manager, method, side_effect=editor.ExportCancelled('cancelled provider')):
                        asyncio.run(background())
                    self.assertEqual('cancelled', states[name]['result']['status'])
                    self.assertEqual('cancelled', states[name]['export_quality_attempt']['status'])
                    self.assertFalse(states[name].get('export_quality_rejections'))
            self.assertEqual('failed', editor.get_export_task_error(RuntimeError('Export cancelled'))['status'])

    def test_failed_and_cancelled_attempts_preserve_prior_proof_and_real_delivery(self):
        with self.fixture(interior_silence=True) as (root, manager, states, client, token):
            options = {'max_unexpected_silence_seconds': .5}
            self.assertEqual(200, self.merge(client, {'export_quality': options}).status_code)
            before = (root / 'cloned_audiobook.mp3').read_bytes()
            report = copy.deepcopy(states['audio']['export_quality_reports'])
            self.assertEqual('review', report[0]['status'])
            self.merge(client, {'export_quality': {**options, 'reject_unexpected_silence': True}})
            rejected = states['audio']['export_quality_rejections']
            self.assertEqual(1, len(rejected))
            self.assertEqual(2, rejected[0]['unexpected_silence_count'])
            self.assertEqual(report, states['audio']['export_quality_reports'])
            self.assertEqual(before, (root / 'cloned_audiobook.mp3').read_bytes())
            with patch.object(manager, 'merge_audio', return_value=(False, 'Merge cancelled')):
                self.merge(client, {})
            self.assertEqual(1, len(states['audio']['export_quality_rejections']))
            evidence = client.get('/api/export_quality', params={'expected_book_token': token}).json()
            self.assertEqual(1, len(evidence['reports']))
            self.assertEqual('cancelled', evidence['attempts'][0]['status'])
            self.assertEqual('failed', evidence['rejected_attempts'][0]['status'])
            (root / 'cloned_audiobook.mp3').write_bytes(b'replaced delivery')
            evidence = client.get('/api/export_quality', params={'expected_book_token': token}).json()
            self.assertEqual([], evidence['reports'])
            self.assertEqual(1, evidence['unavailable'])
            self.assertEqual(1, len(evidence['rejected_attempts']))

    def test_endpoint_gates_both_book_reads_and_status_does_not_serialize_history(self):
        with self.fixture() as (root, manager, states, client, token):
            client.post('/api/export_audacity')
            status = client.get('/api/status/audacity_export').json()
            self.assertTrue(states['audacity_export']['export_quality_reports'])
            self.assertTrue(all(key not in status for key in session.STATE_KEYS))
            self.assertEqual(422, client.get('/api/export_quality').status_code)
            self.assertEqual(422, client.get('/api/export_quality', params={'expected_book_token': 'bad'}).status_code)
            self.assertEqual(409, client.get('/api/export_quality', params={'expected_book_token': 'f' * 64}).status_code)
            capture = editor.get_session_evidence
            def switch(*args):
                result = capture(*args)
                (root / 'state.json').write_text(json.dumps({'active_book_id': 'new', 'book_generation': 2}))
                return result
            with patch.object(editor, 'get_session_evidence', side_effect=switch):
                self.assertEqual(409, client.get('/api/export_quality', params={'expected_book_token': token}).status_code)
            current = editor.get_snapshot_digest(editor.get_editor_book_identity(root))
            self.assertEqual([], client.get('/api/export_quality', params={'expected_book_token': current}).json()['reports'])

    def test_retained_summaries_counts_and_iteration_are_bounded(self):
        state = {}
        attempt = session.ExportQualityAttempt(state, 'b' * 64)
        class Many(dict):
            def items(self):
                for index in range(1000):
                    if index >= export_quality.MAX_DIAGNOSTICS:
                        raise AssertionError('unbounded dictionary iteration')
                    yield str(index), '<script>' * 1000
        summary = session.get_report_summary({'diagnostics': [Many()] * 1000, 'seconds': float('inf')})
        self.assertEqual(export_quality.MAX_DIAGNOSTICS, len(summary['diagnostics']))
        self.assertEqual(1024, len(summary['diagnostics'][0]['0']))
        self.assertIsNone(summary['seconds'])
        for index in range(session.REPORT_LIMIT + 5):
            attempt.publish({'file': f'{index}.wav', 'status': 'verified'})
        attempt.finish(True, 'done')
        self.assertEqual(session.REPORT_LIMIT, len(state['export_quality_reports']))
        self.assertEqual(session.REPORT_LIMIT + 5, state['export_quality_attempt']['completed_files'])
        for index in range(session.REJECTION_LIMIT + 5):
            session.ExportQualityAttempt(state, 'b' * 64).fail(ValueError('failed'))
        self.assertEqual(session.REJECTION_LIMIT, len(state['export_quality_rejections']))
        self.assertEqual(session.REPORT_LIMIT, len(state['export_quality_reports']))

    def test_evidence_refuses_escaping_and_symlinked_outputs(self):
        with self.fixture() as (root, manager, states, client, token):
            client.post('/api/export_audacity')
            state = states['audacity_export']
            original = copy.deepcopy(state['export_quality_reports'][0])
            for name in ('../outside.zip', '/absolute.zip', 'sub\\outside.zip'):
                state['export_quality_reports'] = [{**original, 'file': name}]
                evidence = client.get('/api/export_quality', params={'expected_book_token': token}).json()
                self.assertEqual([], evidence['reports'])
                self.assertEqual(1, evidence['unavailable'])
            output = root / original['file']
            target = root / 'other.zip'
            output.rename(target)
            try:
                output.symlink_to(target)
            except OSError:
                return  # Windows may deny symlink creation; path checks ran above.
            state['export_quality_reports'] = [original]
            evidence = client.get('/api/export_quality', params={'expected_book_token': token}).json()
            self.assertEqual([], evidence['reports'])
            self.assertEqual(1, evidence['unavailable'])


if __name__ == '__main__':
    unittest.main()
