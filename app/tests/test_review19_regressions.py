"""Execution policy and predecessor evidence stay bound from admission to startup."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from tests import test_review15_regressions as prior
from tests import test_review16_regressions as start_over_control
from tests import test_review18_regressions as lifecycle
from utils import atomic_json_write

gap = prior.gap

_PREFIX_FAILURE = gap._WORKER.replace(
    "if args[2] == failure + '_failed':", "if False:")
_OBSERVE_POLICY = gap._WORKER.replace("with ExitStack() as stack:", """
import json
run = worker.run_three_pass

def observe_policy(*args, **kwargs):
    print("RESCUE POLICY", json.dumps([kwargs['context_windows'], kwargs['context_rescue_retries']]))
    return run(*args, **kwargs)

with ExitStack() as stack:
    stack.enter_context(patch.object(worker, 'run_three_pass', side_effect=observe_policy))""")


class Review19Regressions(unittest.TestCase):
    def _case(self):
        case = prior.Review15Regressions()
        case._harness(gap.MULTI_SOURCE)
        self.addCleanup(case.doCleanups)
        return case

    def _fail_after_prefix(self, case, command):
        worker = gap.prior.Review12Regressions()._child(
            case.h, _PREFIX_FAILURE, ['segment', 'after-prefix', *command[3:]])
        self.assertEqual(1, worker.returncode, worker.stderr + worker.stdout)
        checkpoint = gap.script.get_current_generation_checkpoint()
        self.assertEqual(('segment_failed', 1), (checkpoint['stage'], checkpoint['chunks_done']))
        self.assertTrue(case.manifest.is_file())
        self.assertIsNone(case.h.rows())

    def _assert_complete(self, case):
        self.assertEqual(case.native._fresh_control(), case.h.rows())
        self.assertEqual(20, len(case.h.rows()))
        manifest = json.loads(case.manifest.read_bytes())
        receipt = gap.script.run_spec_receipt.load_receipt(case.h.root)
        self.assertEqual(('complete', receipt['run_id'], receipt['sha256']),
                         (manifest['status'], manifest['run_id'], manifest['run_spec_sha256']))

    def test_native_legacy_instruction_context_resumes_both_http_routes(self):
        for route in ('', 'retry'):
            with self.subTest(route=route or 'generate'):
                case = self._case()
                case.h.config['generation']['three_pass_instruct_context_chars'] = 20
                atomic_json_write(case.h.config, case.h.root / 'config.json')
                self._fail_after_prefix(case, case.h.admit_legacy())
                self.assertFalse(case.receipt.exists())

                command = case._start(route)
                receipt = gap.script.run_spec_receipt.load_receipt(case.h.root)
                self.assertEqual(20, receipt['spec']['attribution']['instruct_context_chars'])
                logs = case._worker(command)
                self.assertIn("1/4 chunks already split, stage 'segment_failed'", logs)
                self._assert_complete(case)

    def test_rescue_policy_drift_refuses_resume_and_worker_uses_admitted_policy(self):
        case = self._case()
        config_path = case.h.root / 'config.json'
        policy = {'context_rescue_windows': [500, 1000], 'context_rescue_retries': 3}
        case.h.config['generation'].update(policy)
        atomic_json_write(case.h.config, config_path)
        self._fail_after_prefix(case, case._start())

        variants = {'context_rescue_windows': [100, 200], 'context_rescue_retries': 9}
        for key, value in variants.items():
            drifted = copy.deepcopy(case.h.config)
            drifted['generation'][key] = value
            atomic_json_write(drifted, config_path)
            before = case.native._snapshot()
            state = copy.deepcopy(gap.core.process_state)
            starts = case.dispatched.call_count
            for route in ('', 'retry'):
                with self.subTest(setting=key, route=route or 'generate'):
                    response = case._post(route)
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual(before, case.native._snapshot())
                    self.assertEqual(state, gap.core.process_state)
                    self.assertEqual(starts, case.dispatched.call_count)

        atomic_json_write(case.h.config, config_path)
        command = case._start('retry')
        receipt = gap.script.run_spec_receipt.load_receipt(case.h.root)
        self.assertEqual(policy, receipt['spec'].get('context_rescue'))
        drifted['generation'].update(variants)
        atomic_json_write(drifted, config_path)
        child = gap.prior.Review12Regressions()._child(
            case.h, _OBSERVE_POLICY, ['', 'settled', *command[3:]])
        self.assertEqual(0, child.returncode, child.stderr + child.stdout)
        self.assertIn('RESCUE POLICY [[500, 1000], 3]', child.stdout)
        self.assertIn("1/4 chunks already split, stage 'segment_failed'", child.stdout)
        self._assert_complete(case)

    def test_worker_refuses_removed_or_changed_admitted_predecessor(self):
        case = self._case()
        checkpoint, _, predecessor = case._crash_after_predecessor('segment', stage='instruct')
        case.manifest.unlink()
        case._assert_refused(checkpoint)
        case.manifest.write_bytes(predecessor)
        command = case._start('retry')
        self.assertIn(gap.script.run_spec_receipt.CHECKPOINT_DIGEST, checkpoint)

        for changed in (None, dict(json.loads(predecessor), run_id='f' * 32)):
            with self.subTest(changed=changed is not None):
                if changed is None:
                    case.manifest.unlink()
                else:
                    atomic_json_write(changed, case.manifest)
                before = case.native._snapshot()
                process = copy.deepcopy(gap.core.process_state)
                child = gap.prior.Review12Regressions()._child(
                    case.h, gap._WORKER, ['', 'settled', *command[3:]])
                self.assertEqual(1, child.returncode, child.stderr + child.stdout)
                self.assertIn('The admitted predecessor manifest is missing or changed', child.stderr + child.stdout)
                self.assertEqual(before, case.native._snapshot())
                self.assertEqual(process, gap.core.process_state)
                self.assertIsNone(case.h.rows())

        case.manifest.write_bytes(predecessor)
        logs = case._worker(command)
        self.assertIn("4/4 chunks already split, stage 'instruct'", logs)
        self._assert_complete(case)

    def test_checkpoint_lost_during_admission_returns_translatable_refusal(self):
        api = FastAPI(exception_handlers={gap.core.AppError: gap.core.handle_app_error})
        with patch.object(prior, 'FastAPI', return_value=api):
            case = self._case()
        self._fail_after_prefix(case, case._start())
        before = case.native._snapshot()
        process = copy.deepcopy(gap.core.process_state)
        starts = case.dispatched.call_count
        with patch.object(gap.script, 'get_current_generation_checkpoint', return_value=None):
            response = case._post('retry')
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual({
            'detail': 'Recovery checkpoint changed before start.',
            'message_key': 'safety.generation.recovery_changed',
        }, response.json())
        self.assertEqual(before, case.native._snapshot())
        self.assertEqual(process, gap.core.process_state)
        self.assertEqual(starts, case.dispatched.call_count)

    def test_donor_regression_waits_for_delayed_start_over_workers(self):
        start = gap.core.start_claimed_task_thread
        workers = []
        dispatched = []

        def delay_until_join(name, claim, callback, *args, **kwargs):
            release = threading.Event()

            def delayed(*worker_args, **worker_kwargs):
                if not release.wait(10):
                    raise AssertionError('Start Over worker was never joined')
                callback(*worker_args, **worker_kwargs)
                dispatched.append(claim)

            thread = start(name, claim, delayed, *args, **kwargs)
            workers.append(thread)
            join = thread.join

            def release_and_join(timeout=None):
                release.set()
                join(timeout)

            thread.join = release_and_join
            return thread

        cases = (
            lifecycle.Review18Regressions('test_same_selection_attribution_donor_preserves_process_and_artifacts'),
            start_over_control.Review16Regressions('test_intact_resume_and_explicit_start_over_match_twenty_control_rows'),
        )
        with patch.object(gap.core, 'start_claimed_task_thread', side_effect=delay_until_join):
            for case in cases:
                with self.subTest(case=case.id()):
                    try:
                        getattr(case, case._testMethodName)()
                    finally:
                        case.doCleanups()
        self.assertEqual(3, len(workers))
        self.assertEqual(3, len(set(dispatched)))
        self.assertTrue(all(not thread.is_alive() for thread in workers))
