"""Fail-closed checks for directional review evidence (no sidecar artifacts)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core
from completed_review_receipt import get_review_entries_fingerprint
from routers import script

CLEAN = ['Review complete: 1 -> 1 entries', 'Total changes: 0']
INCOMPLETE = ['Review incomplete: 1 -> 1 entries', 'Total changes: 0']


class DirectionalEvidenceFailClosedTests(unittest.TestCase):
    def harness(self, rows):
        temp = tempfile.TemporaryDirectory(prefix='aa2-duplex-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        scripts = root / 'scripts'; scripts.mkdir()
        alias = root / 'aliases.json'; alias.write_text('{}')
        book = scripts / 'book.json'; book.write_text(json.dumps(rows))
        (scripts / 'book.voice_config.json').write_text('{"A":{"voice":"v"}}')
        state = {'batch_review': {'running': False, 'cancel': False}}
        calls = []

        def stream(command, _cwd, current, **kwargs):
            backward = '--review-pass' in command
            review = any(str(arg).endswith('review_script.py') for arg in command)
            if not review:
                return 0, []
            calls.append('bwd' if backward else 'fwd')
            if backward:
                Path(str(book) + '.review_checkpoint.json').write_text(json.dumps({
                    'execution_sha256': 'backward', 'completed_batches': 1,
                    'all_corrected': rows[:1], 'total_stats': {},
                    'previous_tail': None, 'batch_lengths': [1],
                    'failed_batches': [], 'source_sha256':
                        get_review_entries_fingerprint(rows),
                    'source_offsets': [1], 'accepted_source_offset': 1}))
                return 0, INCOMPLETE
            return 0, CLEAN

        app = FastAPI(); app.include_router(script.router)
        from contextlib import ExitStack
        patchers = [
            patch.object(script, 'SCRIPTS_DIR', str(scripts)),
            patch.object(script, 'CHARACTER_ALIASES_PATH', str(alias)),
            patch.object(script, 'REPORTS_DIR', str(root / 'reports')),
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
            response = entered[-1].post('/api/review_script/batch/start', json={
                'script_names': ['book'], 'dedupe_speakers': True,
                'find_nicknames': True, 'bidirectional': True})
        return response, calls, state, root, book

    def test_no_book_colliding_sidecar_and_no_unverified_forward_reuse(self):
        rows = [{'speaker': 'A', 'text': 'Synthetic sentence.'}]
        response, calls, state, root, book = self.harness(rows)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(['fwd', 'bwd'], calls)
        self.assertFalse(any(p.name.endswith('.review_forward_evidence')
                             for p in (root / 'scripts').iterdir()))
        self.assertFalse((root / 'scripts' / 'book.json.review_forward_evidence').exists())
        self.assertTrue((Path(str(book) + '.review_checkpoint.json')).exists())


if __name__ == '__main__':
    unittest.main()
