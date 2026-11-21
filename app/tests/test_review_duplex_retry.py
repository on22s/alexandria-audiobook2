"""Durable two-pass retry dispatch through the real batch-start route."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core
from completed_review_receipt import get_review_entries_fingerprint, get_review_sidecar_identity
from routers import script

CLEAN = ['Review complete: 1 -> 1 entries', 'Total changes: 0']
INCOMPLETE = ['Review incomplete: 1 -> 1 entries', 'Total changes: 0']


class TwoPassRetryTests(unittest.TestCase):
    def harness(self, entries, *, bidirectional=True, start_over=False,
                evidence_override=None, alias_bytes=None, root=None):
        root = Path(root) if root else Path(tempfile.mkdtemp(prefix='aa2-duplex-', dir='/home/fakemitch/.cache'))
        scripts = root / 'scripts'; scripts.mkdir(exist_ok=True)
        reports = root / 'reports'
        alias = root / 'aliases.json'
        if alias_bytes is not None:
            alias.write_bytes(alias_bytes)
        elif not alias.exists():
            alias.write_text('{}')
        names = []
        for index, rows in enumerate(entries):
            name = f'book{index}'
            names.append(name)
            if not (scripts / f'{name}.json').exists():
                (scripts / f'{name}.json').write_text(json.dumps(rows))
            (scripts / f'{name}.voice_config.json').write_text('{"A":{"voice":"v"}}')
        state = {'batch_review': {'running': False, 'cancel': False}}
        calls = []

        def write_evidence(path, rows):
            digest = get_review_entries_fingerprint(rows)
            voice = scripts / (Path(path).stem + '.voice_config.json')
            evidence = {
                'version': 1, 'input_path': str(path), 'output_path': str(path),
                'input_sha256': digest, 'output_sha256': digest,
                'fingerprint': 'forward-fingerprint',
                'aliases': get_review_sidecar_identity(str(alias)),
                'voice': get_review_sidecar_identity(str(voice)),
                'stats': {'text_changed': 1, 'total_changes': 1},
            }
            if evidence_override:
                evidence.update(evidence_override)
            Path(str(path) + '.review_forward_evidence.json').write_text(json.dumps(evidence))

        def stream(command, _cwd, current, **kwargs):
            backward = '--review-pass' in command
            review = any(str(arg).endswith('review_script.py') for arg in command)
            if not review:
                return 0, []
            book = Path(command[command.index('--input') + 1])
            phase = 'bwd' if backward else 'fwd'
            calls.append((book.name, phase))
            if backward:
                # Mimic one accepted backward batch and an interruption that
                # leaves a valid backward checkpoint on disk.
                checkpoint = Path(str(book) + '.review_checkpoint.json')
                rows = json.loads(book.read_text())
                checkpoint.write_text(json.dumps({
                    'execution_sha256': 'backward', 'completed_batches': 1,
                    'all_corrected': rows[:1], 'total_stats': {},
                    'previous_tail': None, 'batch_lengths': [1],
                    'failed_batches': [], 'source_sha256':
                        get_review_entries_fingerprint(rows),
                    'source_offsets': [1], 'accepted_source_offset': 1}))
                return 0, INCOMPLETE
            if evidence_override is None:
                write_evidence(book, json.loads(book.read_text()))
            return 0, CLEAN

        app = FastAPI(); app.include_router(script.router)
        from contextlib import ExitStack
        patchers = [
            patch.object(script, 'SCRIPTS_DIR', str(scripts)),
            patch.object(script, 'CHARACTER_ALIASES_PATH', str(alias)),
            patch.object(script, 'REPORTS_DIR', str(reports)),
            patch.object(script, 'ROOT_DIR', str(root)),
            patch.object(script, 'process_state', state),
            patch.object(core, 'process_state', state),
            patch.object(core, '_task_claims', {}),
            patch.object(core, '_gpu_leases', {}),
            patch.object(core, 'acquire_gpu_lock', return_value=None),
            patch.object(core, 'llm_is_on_this_gpu', return_value=True),
            patch.object(script, 'check_global_gpu_lock'),
            patch.object(script, '_require_review_restart_book'),
            patch.object(core, 'claim_gpu_task', wraps=core.claim_gpu_task),
            patch.object(script, '_init_task_log', return_value=str(root / 'task.log')),
            patch.object(script, '_stream_subprocess_to_logs', side_effect=stream),
            patch.object(script, '_insert_llm_summary', side_effect=lambda lines, *_a, **_k: lines),
            patch.object(script, '_run_claimed_background_task', side_effect=lambda name, work: work()),
            TestClient(app),
        ]
        with ExitStack() as stack:
            entered = [stack.enter_context(item) for item in patchers]
            client = entered[-1]
            response = client.post('/api/review_script/batch/start', json={
                'script_names': names, 'dedupe_speakers': True,
                'find_nicknames': True, 'bidirectional': bidirectional,
                'start_over': start_over})
        return response, calls, state['batch_review'], root

    def test_second_run_skips_forward_and_resumes_backward_only(self):
        rows = [[{'speaker': 'A', 'text': 'Synthetic sentence.'}]]
        first, calls1, state1, root = self.harness(rows)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual([('book0.json', 'fwd'), ('book0.json', 'bwd')], calls1)
        second, calls2, state2, _root = self.harness(rows, root=root)
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual([('book0.json', 'bwd')], calls2)
        self.assertTrue(any('Reusing verified forward completion' in line
                            for line in state2['logs']))
        self.assertEqual(1, state2['tasks'][0]['stats_fwd']['total_changes'])

    def test_changed_book_or_companion_forces_forward(self):
        rows = [[{'speaker': 'A', 'text': 'Synthetic sentence.'}]]
        self.harness(rows)
        changed = [[{'speaker': 'A', 'text': 'A different synthetic sentence.'}]]
        response, calls, state, _root = self.harness(changed)
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn(('book0.json', 'fwd'), calls)
        self.harness(rows, alias_bytes=b'{}')
        response, calls, _state, _root = self.harness(rows)
        self.assertIn(('book0.json', 'fwd'), calls)

    def test_missing_or_invalid_evidence_forces_forward(self):
        rows = [[{'speaker': 'A', 'text': 'Synthetic sentence.'}]]
        response, calls, state, root = self.harness(rows, evidence_override={'version': 99})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([('book0.json', 'fwd'), ('book0.json', 'bwd')], calls)

    def test_start_over_removes_evidence_and_runs_forward(self):
        rows = [[{'speaker': 'A', 'text': 'Synthetic sentence.'}]]
        first, _calls, _state, root = self.harness(rows)
        response, calls, state, _root = self.harness(rows, start_over=True, root=root)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([('book0.json', 'fwd'), ('book0.json', 'bwd')], calls)
        self.assertTrue(any('Reusing verified forward completion' in line
                            for line in state['logs']) is False)

    def test_two_books_dispatch_independently(self):
        rows = [[{'speaker': 'A', 'text': 'First synthetic sentence.'}],
                [{'speaker': 'B', 'text': 'Second synthetic sentence.'}]]
        first, _calls, _state, root = self.harness(rows)
        response, calls, state, _root = self.harness(rows, root=root)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], [call for call in calls if call[1] == 'fwd'])
        self.assertEqual(2, len([call for call in calls if call[1] == 'bwd']))


if __name__ == '__main__':
    unittest.main()
