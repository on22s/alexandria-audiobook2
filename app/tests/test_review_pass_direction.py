"""A backward review pass has its own identity; a failed forward pass cannot seed it."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import core
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import script
import review_script as review
from generate_script import LLMGenParams


# Pinned for the versioned wire-auth identity (REVIEW_ALGORITHM_VERSION 11).
# The default/forward identity must stay byte-identical after adding the
# backward-only direction key.
PINNED_FORWARD_FINGERPRINT = "b31763ab45d090b0adab3faa519d1d69062295b63ea0a3f8282fb3186c542e66"
CLEAN = ['Review complete: 2 -> 2 entries', 'Total changes: 0']


def _fingerprint(review_pass=None):
    config = {'generation': {'review_batch_size': 25}, 'llm_mode': 'local'}
    profile = {'base_url': 'http://fixture/v1', 'model_name': 'model', 'api_key': 'k'}
    params = LLMGenParams(system_prompt='system', user_prompt_template='{batch}')
    if review_pass is None:
        args = SimpleNamespace(context_window=0, dedupe_speakers=True,
                               alias_registry=None, remap_voice_config=None)
    else:
        args = SimpleNamespace(context_window=0, dedupe_speakers=True,
                               alias_registry=None, remap_voice_config=None,
                               review_pass=review_pass)
    return review.get_completed_review_fingerprint(config, profile, params, args, object(), 'model')


class ReviewPassIdentityTests(unittest.TestCase):
    def test_forward_and_default_fingerprint_match_main(self):
        self.assertEqual(PINNED_FORWARD_FINGERPRINT, _fingerprint())
        self.assertEqual(PINNED_FORWARD_FINGERPRINT, _fingerprint('forward'))

    def test_backward_fingerprint_is_distinct_from_forward(self):
        self.assertNotEqual(_fingerprint('forward'), _fingerprint('backward'))

    def test_forward_checkpoint_is_refused_by_backward_run(self):
        """A forward-only execution sha cannot validate a backward run."""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'book.json'
            entries = [{'speaker': 'ALICE', 'text': 'Synthetic fixture narration.'}]
            output.write_text(json.dumps(entries))
            forward_fp = _fingerprint('forward')
            backward_fp = _fingerprint('backward')
            review.save_checkpoint(
                str(output), 1, 2, 25, 0, entries[:1],
                {'text_changed': 0, 'speaker_changed': 0, 'instruct_changed': 0,
                 'annotations_changed': 0, 'entries_changed': 0, 'entries_added': 0,
                 'entries_removed': 0},
                None, [1], [], review._entries_fingerprint(entries),
                source_offsets=[1], execution_sha256=forward_fp)
            self.assertIsNotNone(review.load_checkpoint(
                str(output), 2, 25, 0, entries, execution_sha256=forward_fp))
            self.assertIsNone(review.load_checkpoint(
                str(output), 2, 25, 0, entries, execution_sha256=backward_fp))


class BidirectionalCheckpointTests(unittest.TestCase):
    """Drive the real batch route with the subprocess patched, like the other batch tests."""

    def _run_batch(self, *, bidirectional, fwd_rc):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / 'scripts'
            reports = root / 'reports'
            scripts.mkdir()
            book = scripts / 'book.json'
            original = json.dumps([{'speaker': 'ALICE', 'text': 'Synthetic fixture narration.'}])
            book.write_text(original)
            checkpoint = Path(review._checkpoint_path(str(book)))
            state = {'batch_review': {'running': False, 'cancel': False}}
            discard_calls = []
            seen_flags = []
            def record_discard(path):
                discard_calls.append(path)

            def write_stale_checkpoint():
                # Stand in for a forward child that wrote a batch checkpoint and
                # then crashed before it could clear it on success.
                checkpoint.write_text(json.dumps({
                    'execution_sha256': 'forward-only', 'completed_batches': 1,
                    'all_corrected': [{'speaker': 'ALICE', 'text': 'Synthetic fixture narration.'}],
                    'total_stats': {}, 'previous_tail': None, 'batch_lengths': [1],
                    'failed_batches': [], 'source_sha256': 'fixture', 'source_offsets': [1],
                }))

            def stream(command, _cwd, current, **kwargs):
                phase = current['current_pass']
                if any(str(arg).endswith('find_nicknames.py') for arg in command):
                    return 0, []
                seen_flags.append((phase, '--review-pass' in command,
                                   'backward' in command))
                if phase == 'fwd':
                    write_stale_checkpoint()
                    return fwd_rc, (CLEAN if fwd_rc == 0 else ['fixture reviewer crashed'])
                # Direction identity is bound in the child fingerprint; the
                # parent must never delete a checkpoint an ordinary retry can
                # validate.
                self.assertTrue(checkpoint.exists())
                return 0, CLEAN

            app = FastAPI()
            app.include_router(script.router)
            with patch.object(script, 'SCRIPTS_DIR', str(scripts)), \
                 patch.object(script, 'CHARACTER_ALIASES_PATH', str(root / 'aliases.json')), \
                 patch.object(script, 'REPORTS_DIR', str(reports)), \
                 patch.object(script, 'ROOT_DIR', str(root)), \
                 patch.object(script, 'process_state', state), \
                 patch.object(script, 'check_global_gpu_lock'), \
                 patch.object(core, 'claim_gpu_task', wraps=core.claim_gpu_task), \
                 patch.object(core, 'process_state', state), \
                 patch.object(core, '_task_claims', {}), \
                 patch.object(core, '_gpu_leases', {}), \
                 patch.object(core, 'acquire_gpu_lock', return_value=None), \
                 patch.object(core, 'llm_is_on_this_gpu', return_value=True), \
                 patch.object(script, '_init_task_log', return_value=str(root / 'task.log')), \
                 patch.object(script, 'discard_review_progress', side_effect=record_discard), \
                 patch.object(script, '_stream_subprocess_to_logs', side_effect=stream), \
                 patch.object(script, '_insert_llm_summary', side_effect=lambda lines, *a, **k: lines), \
                 patch.object(script, '_run_claimed_background_task', side_effect=lambda name, work: work()), \
                 TestClient(app) as client:
                response = client.post('/api/review_script/batch/start', json={
                    'script_names': ['book'], 'dedupe_speakers': True,
                    'find_nicknames': True, 'bidirectional': bidirectional})
                self.assertEqual(200, response.status_code, response.text)
            self.assertFalse(state['batch_review']['running'])
            self.assertEqual(original, book.read_text())
            return discard_calls, seen_flags, [str(book)]

    def test_failed_forward_preserves_checkpoint_and_skips_backward_child(self):
        discard_calls, seen_flags, paths = self._run_batch(bidirectional=True, fwd_rc=1)
        self.assertEqual([], discard_calls)
        self.assertEqual([('fwd', False, False)], seen_flags)

    def test_clean_forward_preserves_checkpoint_and_marks_backward_child(self):
        discard_calls, seen_flags, paths = self._run_batch(bidirectional=True, fwd_rc=0)
        self.assertEqual([], discard_calls)
        self.assertEqual([('fwd', False, False), ('bwd', True, True)], seen_flags)

    def test_single_direction_review_keeps_default_behaviour(self):
        discard_calls, seen_flags, paths = self._run_batch(bidirectional=False, fwd_rc=1)
        self.assertEqual([], discard_calls)
        self.assertEqual([('fwd', False, False)], seen_flags)


if __name__ == '__main__':
    unittest.main()
