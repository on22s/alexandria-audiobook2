"""Recovery authority across native checkpoint-to-manifest publication gaps."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import patch

from tests.test_recovery_validation_matrix import core
from tests.test_review7_regressions import MULTI_SOURCE
from tests.test_review13_regressions import _ASSERT_PANEL, fixtures, prior, script
from utils import atomic_json_write


_WORKER = """
from contextlib import ExitStack
import os
import sys
from unittest.mock import patch

import three_pass_generate as worker
from tests import test_recovery_validation_matrix as fixtures

failure, boundary = sys.argv[1:3]
del sys.argv[1:3]
save = worker._save_three_pass_checkpoint
pick = fixtures._pick

def save_then_exit(*args, **kwargs):
    save(*args, **kwargs)
    if args[2] == failure + '_failed':
        os._exit(87)

def fail_second(tag, payloads, fail, calls, text=''):
    if tag == 'segment' and 'DANA walked' in text.split('SOURCE TEXT:')[-1]:
        raise RuntimeError('synthetic second chunk failure')
    return pick(tag, payloads, fail, calls, text)

with ExitStack() as stack:
    stack.enter_context(patch.object(worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')))
    if boundary != 'settled':
        stack.enter_context(patch.object(worker, '_save_three_pass_checkpoint', side_effect=save_then_exit))
    if boundary == 'after-prefix':
        stack.enter_context(patch.object(fixtures, '_pick', side_effect=fail_second))
    failed_passes = (failure,) if failure and boundary != 'after-prefix' else ()
    worker.main(client_factory=fixtures._factory(fixtures._payloads(), fail=failed_passes))
"""

_READ_PANEL = _ASSERT_PANEL.split("    assert.strictEqual(nodes['btn-retry-script']")[0].replace(
    "window: {},", "window: {}, escapeHtml: String,").replace(
    "async get(path) { calls.push(path); return path.includes('?') ? recovery : summary; }",
    "async get(path) { if (recovery.rejected) { throw Error('HTTP 409'); } return recovery; }") + """
    const panel = nodes['script-recovery-panel'];
    console.log(JSON.stringify([
        nodes['btn-retry-script'].style.display === 'inline-block',
        panel.style.display !== 'none' && panel.innerHTML.includes('onSkipRecoveryChunk()'),
        panel.style.display !== 'none' && panel.innerHTML.includes('onInjectRecoverySegmentation()'),
    ]));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class Review14Regressions(unittest.TestCase):
    def _harness(self, source=MULTI_SOURCE):
        h = fixtures._Harness(
            {'case': self.id()}, source_text=source,
            config=fixtures._config(force_segment_llm=True, custom_chunk=500))
        self.h = h
        self.addCleanup(h.close)
        for patcher in (patch.object(core, 'DATA_DIR', str(h.root)),
                        patch.object(script, 'reserve_background_task', side_effect=core.reserve_background_task)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return h

    def _launch(self, route=''):
        queued = len(self.h.scheduled)
        payload = {'start_over': False} if not route else {}
        response = fixtures._post(self.h, route, payload)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(queued + 1, len(self.h.scheduled))
        core.release_gpu_task_claim('script')
        return self.h.scheduled[-1][4]

    def _worker(self, command, *, failure='', boundary='settled', exit_code=0):
        result = prior.Review12Regressions()._child(
            self.h, _WORKER, [failure, boundary, *command[3:]])
        self.assertEqual(exit_code, result.returncode, result.stderr + result.stdout)

    def _panel(self, response, expected):
        payload = response.text if response.status_code == 200 else '{"rejected": true}'
        run = subprocess.run(
            ['node', '-e', _READ_PANEL, str(prior.APP_DIR / 'static/js/app-core.js'), payload, '{}'],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(expected, json.loads(run.stdout))

    def _snapshot(self):
        return {str(path.relative_to(self.h.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.h.root.rglob('*')
                if path.is_file() and not path.name.endswith('.lock')}

    def _manual_refused(self, checkpoint):
        before = self._snapshot()
        failed = checkpoint['failed']
        chunk = failed.get('chunk') or 1
        entries = [{'type': 'NARRATOR', 'text': text}
                   for text in re.split(r'(?<=[.!?])\s+', failed.get('source', '').strip()) if text]
        self.assertEqual(409, fixtures._get(self.h, 'recovery/detail').status_code)
        self.assertEqual(409, fixtures._post(self.h, 'skip', {'chunk': chunk}).status_code)
        self.assertEqual(409, fixtures._post(self.h, 'inject', {'chunk': chunk, 'entries': entries}).status_code)
        self.assertEqual(before, self._snapshot())

    def _retry_only(self):
        before = self._snapshot()
        combined = fixtures._get(self.h, 'recovery?include_detail=true')
        self.assertEqual(200, combined.status_code, combined.text)
        self.assertTrue(combined.json()['recoverable'])
        self.assertIsNone(combined.json()['detail'])
        self._panel(combined, [True, False, False])
        summary = fixtures._get(self.h, 'recovery')
        self.assertTrue(summary.json()['recoverable'])
        run = subprocess.run(
            ['node', '-e', _ASSERT_PANEL, str(prior.APP_DIR / 'static/js/app-core.js'),
             combined.text, summary.text], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(before, self._snapshot())

    def _refused(self, checkpoint):
        before = self._snapshot()
        queued = len(self.h.scheduled)
        combined = fixtures._get(self.h, 'recovery?include_detail=true')
        self.assertEqual(409, combined.status_code, combined.text)
        self._panel(combined, [False, False, False])
        for route in ('retry', ''):
            response = fixtures._post(self.h, route, {} if route else {'start_over': False})
            self.assertEqual(409, response.status_code, response.text)
        self._manual_refused(checkpoint)
        self.assertEqual(queued, len(self.h.scheduled))
        self.assertEqual(before, self._snapshot())

    def _fresh_control(self):
        # An independent book has no checkpoint or manifest at admission.
        control = fixtures._Harness({'case': 'undamaged-control'}, config=self.h.config,
                                    source_text=MULTI_SOURCE)
        try:
            command, *_ = control.admit()
            proc = prior.Review12Regressions()._child(control, _WORKER, ['', 'settled', *command[3:]])
            self.assertEqual(0, proc.returncode, proc.stderr + proc.stdout)
            rows = control.rows()
            self.assertEqual(20, len(rows))
            return rows
        finally:
            control.close()

    def _assert_complete(self, command, control):
        self._worker(command)
        self.assertEqual(control, self.h.rows())
        manifest = json.loads(Path(self.h.artifact_paths()['manifest']).read_bytes())
        self.assertEqual(('complete', 4, 20),
                         (manifest['status'], manifest['progress']['chunks_completed'],
                          manifest['progress']['instructed_entries']))
        self.assertEqual(script.run_spec_receipt.load_receipt(self.h.root)['run_id'], manifest['run_id'])

    def _first_manifest_gap(self, resume_route):
        h = self._harness()
        control = self._fresh_control()
        first = self._launch()
        manifest_path = Path(h.artifact_paths()['manifest'])
        self.assertFalse(manifest_path.exists())
        self._worker(first, failure='segment', boundary='after-prefix', exit_code=87)
        checkpoint = script.get_current_generation_checkpoint()
        self.assertEqual(('segment_failed', 1, 2),
                         (checkpoint['stage'], checkpoint['chunks_done'], checkpoint['failed']['chunk']))
        self.assertTrue(checkpoint['segmented'])
        self.assertFalse(manifest_path.exists())
        self._retry_only()
        self._manual_refused(checkpoint)
        self._assert_complete(self._launch(resume_route), control)

    def test_first_failure_without_manifest_resumes_with_ordinary_generate(self):
        self._first_manifest_gap('')

    def test_first_failure_without_manifest_resumes_with_retry(self):
        self._first_manifest_gap('retry')

    def test_pinned_predecessor_only_allows_retry_and_foreign_manifest_refuses(self):
        h = self._harness()
        control = self._fresh_control()
        first = self._launch()
        self._worker(first, failure='segment', exit_code=1)
        path = Path(h.artifact_paths()['manifest'])
        original = path.read_bytes()
        manifest = json.loads(original)
        old = script.run_spec_receipt.load_receipt(h.root)
        self.assertEqual(old['run_id'], manifest['run_id'])
        combined = fixtures._get(h, 'recovery?include_detail=true')
        self.assertEqual(200, combined.status_code, combined.text)
        self._panel(combined, [True, True, True])

        successor = self._launch('retry')
        issued = script.run_spec_receipt.load_receipt(h.root)
        self.assertNotEqual(old['run_id'], issued['run_id'])
        self._worker(successor, failure='segment', boundary='before-manifest', exit_code=87)
        self.assertEqual(original, path.read_bytes())
        checkpoint = script.get_current_generation_checkpoint()
        self._retry_only()
        self._manual_refused(checkpoint)
        self.assertEqual(script.three_pass_run_spec.spec_digest(manifest), issued['prior_manifest_sha256'])

        counts = dict(manifest['counts'], quote_repairs=manifest['counts']['quote_repairs'] + 1)
        for edit in ({'run_id': 'e' * 32}, {'counts': counts}):
            with self.subTest(edit=edit):
                changed = dict(manifest, **edit)
                self.assertNotEqual(manifest, changed)
                atomic_json_write(changed, path)
                self._refused(checkpoint)
        path.write_bytes(original)
        self._assert_complete(self._launch('retry'), control)

    def test_prior_attribution_manifest_conflicts_with_successor_instruction_failure(self):
        h = self._harness(fixtures.SOURCE)
        self._worker(self._launch(), failure='attribute', exit_code=1)
        path = Path(h.artifact_paths()['manifest'])
        original = path.read_bytes()
        manifest = json.loads(original)
        self.assertEqual('attribute', manifest['failed_pass'])
        successor = self._launch('retry')
        issued = script.run_spec_receipt.load_receipt(h.root)
        self.assertNotEqual(manifest['run_id'], issued['run_id'])
        self._worker(successor, failure='instruct', boundary='before-manifest', exit_code=87)
        checkpoint = script.get_current_generation_checkpoint()
        self.assertEqual('instruct_failed', checkpoint['stage'])
        self.assertEqual(original, path.read_bytes())
        self._refused(checkpoint)
        self.assertEqual(script.three_pass_run_spec.spec_digest(manifest), issued['prior_manifest_sha256'])
