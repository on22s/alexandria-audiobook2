"""Native recovery keeps its receipt and predecessor authority after crashes."""
from contextlib import ExitStack
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import test_review14_regressions as gap
from tests.test_support import capture_script_worker_starts
from utils import atomic_json_write


class Review15Regressions(unittest.TestCase):
    def _harness(self, source=gap.fixtures.SOURCE):
        self.native = gap.Review14Regressions()
        self.h = self.native._harness(source)
        self.addCleanup(self.native.doCleanups)
        stack = ExitStack()
        self.addCleanup(stack.close)
        api = FastAPI()
        api.add_middleware(gap.core.TaskClaimMiddleware)
        api.include_router(gap.script.router)
        stack.enter_context(patch.object(gap.script, 'register_claimed_background_task',
                                         side_effect=gap.core.register_claimed_background_task))
        self.dispatched = stack.enter_context(capture_script_worker_starts())
        self.client = stack.enter_context(TestClient(api))
        self.manifest = Path(self.h.artifact_paths()['manifest'])
        self.receipt = Path(self.h.artifact_paths()['receipt'])

    def _get(self, suffix):
        return self.client.get('/api/generate_script/' + suffix)

    def _post(self, suffix, payload=None):
        return self.client.post('/api/generate_script' + ('/' + suffix if suffix else ''),
                                json={} if payload is None else payload)

    def _start(self, suffix=''):
        count = self.dispatched.call_count
        response = self._post(suffix)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(count + 1, self.dispatched.call_count)
        return self.dispatched.call_args.args[0]

    def _start_over(self):
        with capture_script_worker_starts() as worker:
            response = self._post('', {'start_over': True})
            self.assertEqual(200, response.status_code, response.text)
        worker.assert_called_once()
        self.assertNotIn('script', gap.core._task_claims)
        return worker.call_args.args[0]

    def _worker(self, command, *, failure='', boundary='settled', stage=None, exit_code=0):
        source = gap._WORKER
        if stage is not None:
            source = source.replace("args[2] == failure + '_failed'", "args[2] == %r" % stage)
        child = gap.prior.Review12Regressions()._child(
            self.h, source, [failure, boundary, *command[3:]])
        self.assertEqual(exit_code, child.returncode, child.stderr + child.stdout)
        return child.stdout

    def _crash_after_predecessor(self, previous, *, failure='', stage=None):
        self._worker(self._start(), failure=previous, exit_code=1)
        predecessor = self.manifest.read_bytes()
        saved = gap.script.run_spec_receipt.load_receipt(self.h.root)
        self.assertEqual(saved['run_id'], json.loads(predecessor)['run_id'])
        command = self._start('retry')
        issued = gap.script.run_spec_receipt.load_receipt(self.h.root)
        self.assertNotEqual(saved['run_id'], issued['run_id'])
        self.assertEqual(gap.script.three_pass_run_spec.spec_digest(json.loads(predecessor)),
                         issued['prior_manifest_sha256'])
        self._worker(command, failure=failure, stage=stage, boundary='before-manifest', exit_code=87)
        self.assertEqual(predecessor, self.manifest.read_bytes())
        checkpoint = gap.script.get_current_generation_checkpoint()
        self.assertEqual(stage or failure + '_failed', checkpoint['stage'])
        return checkpoint, issued, predecessor

    def _assert_refused(self, checkpoint):
        before = self.native._snapshot()
        count = self.dispatched.call_count
        combined = self._get('recovery?include_detail=true')
        self.assertEqual(409, combined.status_code, combined.text)
        self.native._panel(combined, [False, False, False])
        self.assertEqual(409, self._get('recovery').status_code)
        self.assertEqual(409, self._get('recovery/detail').status_code)
        failed = checkpoint.get('failed') or {}
        chunk = failed.get('chunk', 1)
        entries = [{'type': 'NARRATOR', 'text': text}
                   for text in re.split(r'(?<=[.!?])\s+', failed.get('source', '').strip()) if text]
        for suffix, body in (
                ('inject', {'chunk': chunk, 'entries': entries}), ('skip', {'chunk': chunk}),
                ('retry', {}), ('', {'start_over': False})):
            response = self._post(suffix, body)
            self.assertEqual(409, response.status_code, (suffix, response.text))
        self.assertEqual(count, self.dispatched.call_count)
        self.assertEqual(before, self.native._snapshot())

    def test_missing_receipt_refuses_manual_recovery_from_compatible_predecessor(self):
        self._harness(gap.MULTI_SOURCE)
        checkpoint, _, _ = self._crash_after_predecessor('segment', failure='segment')
        self.receipt.unlink()
        self._assert_refused(checkpoint)

    def test_missing_receipt_refuses_retry_from_contradictory_predecessor(self):
        self._harness()
        checkpoint, _, _ = self._crash_after_predecessor('attribute', failure='instruct')
        self.receipt.unlink()
        response = self._post('retry')
        self.assertEqual(409, response.status_code, response.text)
        self._assert_refused(checkpoint)

    def test_missing_receipt_refuses_manual_recovery_from_current_attempt(self):
        self._harness(gap.MULTI_SOURCE)
        self._worker(self._start(), failure='segment', exit_code=1)
        actual = self._get('recovery?include_detail=true')
        self.native._panel(actual, [True, True, True])
        self.receipt.unlink()
        self._assert_refused(gap.script.get_current_generation_checkpoint())

    def test_missing_pinned_manifest_refuses_cross_stage_retry_and_generate(self):
        self._harness()
        checkpoint, _, _ = self._crash_after_predecessor('attribute', failure='instruct')
        self.manifest.unlink()
        self._assert_refused(checkpoint)

    def test_successor_worker_manifest_allows_current_manual_recovery(self):
        self._harness(gap.MULTI_SOURCE)
        self._worker(self._start(), failure='segment', exit_code=1)
        self._worker(self._start('retry'), failure='segment', exit_code=1)
        receipt = gap.script.run_spec_receipt.load_receipt(self.h.root)
        manifest = json.loads(self.manifest.read_bytes())
        self.assertEqual((receipt['run_id'], receipt['sha256']),
                         (manifest['run_id'], manifest.get('run_spec_sha256')))
        self.native._panel(self._get('recovery?include_detail=true'), [True, True, True])

    def test_ordinary_progress_keeps_predecessor_pin_on_both_resume_routes(self):
        for route in ('retry', ''):
            with self.subTest(route=route):
                case = Review15Regressions()
                try:
                    case._harness()
                    checkpoint, issued, predecessor = case._crash_after_predecessor(
                        'attribute', stage='instruct')
                    manifest = json.loads(predecessor)
                    variants = (
                        dict(manifest, run_id='e' * 32),
                        dict(manifest, run_id=issued['run_id']),
                        dict(manifest, counts=dict(manifest['counts'], quote_repairs=999)),
                    )
                    for variant in variants:
                        atomic_json_write(variant, case.manifest)
                        case._assert_refused(checkpoint)
                    case.manifest.unlink()
                    case._assert_refused(checkpoint)

                    case.manifest.write_bytes(predecessor)
                    combined = case._get('recovery?include_detail=true')
                    case.assertEqual(200, combined.status_code, combined.text)
                    case.native._panel(combined, [True, False, False])
                    command = case._start(route)
                    next_receipt = gap.script.run_spec_receipt.load_receipt(case.h.root)
                    case.assertEqual(issued['prior_manifest_sha256'], next_receipt['prior_manifest_sha256'])
                    logs = case._worker(command)
                    case.assertIn("chunks already split, stage 'instruct'", logs)
                    rows = case.h.rows()
                    case.assertEqual(4, len(rows))
                    control = gap.fixtures._Harness({'case': 'undamaged'}, config=case.h.config)
                    try:
                        control_command, *_ = control.admit()
                        child = gap.prior.Review12Regressions()._child(
                            control, gap._WORKER, ['', 'settled', *control_command[3:]])
                        case.assertEqual(0, child.returncode, child.stdout + child.stderr)
                        case.assertEqual(control.rows(), rows)
                    finally:
                        control.close()
                finally:
                    case.doCleanups()
