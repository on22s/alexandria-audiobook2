"""Complete checkpoint provenance and side-effect-free Script refusal."""
import copy
import json
from pathlib import Path
import shutil
import unittest

import book_state_transaction as books
from fastapi import HTTPException

from tests import test_review15_regressions as prior
from tests.test_recovery_validation_matrix import _content_spec_digest
from tests.test_support import intercept_book_publications
from utils import atomic_json_write

gap = prior.gap
CHECKPOINT_BINDING = 'run_spec_binding_sha256'

_WINDOW_WORKER = gap._WORKER.replace(
    "with ExitStack() as stack:",
    """
def choose_by_window(tag, payloads, fail, calls, text=''):
    response = pick(tag, payloads, fail, calls, text)
    if tag == 'attribute' and len(fixtures._prompt_batch(text)) < 4:
        for row in response:
            row['speaker'] = {'MIRA': 'DANA', 'DANA': 'MIRA'}.get(row['speaker'], row['speaker'])
    return response

fixtures._pick = choose_by_window

with ExitStack() as stack:""").replace(
    "args[2] == failure + '_failed'", "args[2] == 'instruct'")


class Review17Regressions(unittest.TestCase):
    def _case(self, source=gap.fixtures.SOURCE):
        case = prior.Review15Regressions()
        case._harness(source)
        self.addCleanup(case.doCleanups)
        return case

    def _replace_checkpoint(self, case, *, chunk_size=500, target_chars=0):
        config = copy.deepcopy(case.h.config)
        config['generation']['three_pass_chunk_size'] = chunk_size
        config['generation']['three_pass_attribute_target_chars'] = target_chars
        donor = gap.fixtures._Harness({'case': 'foreign-spec'}, config=config)
        try:
            command, _, _, receipt = donor.admit()
            child = gap.prior.Review12Regressions()._child(
                donor, _WINDOW_WORKER, ['', 'before-manifest', *command[3:]])
            self.assertEqual(87, child.returncode, child.stdout + child.stderr)
            header = Path(donor.artifact_paths()['checkpoint'])
            shards = list(donor.root.glob(header.name + '.parts-*'))
            self.assertTrue(shards)
            for folder in shards:
                shutil.copytree(folder, case.h.root / folder.name)
            shutil.copyfile(header, Path(case.h.artifact_paths()['checkpoint']))
        finally:
            donor.close()
        return gap.script.get_current_generation_checkpoint(), receipt

    def test_same_partial_fingerprint_donor_refuses_generate_and_retry(self):
        for route in ('', 'retry'):
            with self.subTest(route=route or 'generate'):
                case = self._case()
                original, receipt, manifest = case._crash_after_predecessor('attribute', stage='instruct')
                donor, donor_receipt = self._replace_checkpoint(case, target_chars=24)
                self.assertEqual(original['fingerprint'], donor['fingerprint'])
                self.assertEqual(original['segmented'], donor['segmented'])
                self.assertNotEqual(original['named'], donor['named'])
                self.assertNotEqual(_content_spec_digest(receipt['spec']), _content_spec_digest(donor_receipt['spec']))
                self.assertEqual((0, 24), (receipt['spec']['attribution']['attribute_target_chars'],
                                           donor_receipt['spec']['attribution']['attribute_target_chars']))
                self.assertEqual(manifest, case.manifest.read_bytes())
                before = case.native._snapshot()
                count = case.dispatched.call_count
                reply = case._post(route)
                self.assertEqual(409, reply.status_code, reply.text)
                self.assertEqual(count, case.dispatched.call_count)
                self.assertEqual(before, case.native._snapshot())
                case._assert_refused(donor)
                self.assertNotEqual(original[CHECKPOINT_BINDING], donor[CHECKPOINT_BINDING])

                # The worker checks the spec itself even if a substituted
                # header appears after an otherwise valid HTTP admission.
                worker = gap.prior.Review12Regressions()._child(
                    case.h, gap._WORKER, ['', 'settled', *case.dispatched.call_args.args[0][3:]])
                self.assertEqual(1, worker.returncode, worker.stdout + worker.stderr)
                self.assertIn('cannot be verified against the admitted run spec', worker.stdout + worker.stderr)
                self.assertEqual(before, case.native._snapshot())

    def test_different_fingerprint_refusal_preserves_the_entire_process_state(self):
        for route in ('', 'retry'):
            with self.subTest(route=route or 'generate'):
                case = self._case()
                _, receipt, _ = case._crash_after_predecessor('attribute', stage='instruct')
                donor, donor_receipt = self._replace_checkpoint(case, chunk_size=501)
                self.assertEqual((500, 501), (receipt['spec']['chunk_size'], donor_receipt['spec']['chunk_size']))
                self.assertNotEqual(receipt['fingerprint'], donor['fingerprint'])
                process = gap.core.process_state['script']
                process.update(cancel=True, _script_published_cleanup_stop=True)
                before = copy.deepcopy(process)
                files = case.native._snapshot()
                queued = case.dispatched.call_count
                response = case._post(route)
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(before, process)
                self.assertEqual(files, case.native._snapshot())
                self.assertEqual(queued, case.dispatched.call_count)
                self.assertNotIn('script', gap.core._task_claims)

    def test_refusal_inside_book_publication_preserves_process_state_and_releases_claim(self):
        case = self._case()
        case._crash_after_predecessor('attribute', stage='instruct')
        process = gap.core.process_state['script']
        process.update(cancel=True, _script_published_cleanup_stop=True)
        before = copy.deepcopy(process)
        files = case.native._snapshot()
        count = case.dispatched.call_count
        claims = []

        def refuse(_target):
            if claims:
                return
            claims.append(gap.core._task_claims['script']['id'])
            self.assertEqual(before, process)
            self.assertTrue(gap.core.is_task_running('script'))
            with self.assertRaises(HTTPException):
                gap.core.check_global_gpu_lock('script')
            raise HTTPException(status_code=409, detail='synthetic publication refusal')

        with intercept_book_publications(books, before=refuse):
            result = case._post('retry')
        self.assertEqual(409, result.status_code, result.text)
        self.assertTrue(claims)
        self.assertEqual(before, process)
        self.assertEqual(files, case.native._snapshot())
        self.assertEqual(count, case.dispatched.call_count)
        self.assertNotIn('script', gap.core._task_claims)

        started = []

        def observe_adoption(_command, task):
            self.assertEqual('script', task)
            self.assertEqual('started', gap.core._task_claims[task]['phase'])
            started.append(copy.deepcopy(process))

        case.dispatched.side_effect = observe_adoption
        command = case._start('retry')
        [active] = started
        self.assertTrue(active['running'])
        self.assertFalse(active['cancel'])
        self.assertNotIn('_script_published_cleanup_stop', active)
        # The dispatch stub never initializes a subprocess, so release restores
        # the prior terminal status after observing the admitted worker state.
        self.assertEqual(before, process)
        case._worker(command)
        self.assertEqual(4, len(case.h.rows()))

    def test_old_empty_seed_upgrades_under_its_original_receipt(self):
        case = self._case(gap.MULTI_SOURCE)
        command = case._start_over()
        header = Path(case.h.artifact_paths()['checkpoint'])
        initial = json.loads(header.read_bytes())
        self.assertEqual(('segment', 0), (initial['stage'], initial['chunks_done']))
        receipt = gap.script.run_spec_receipt.load_receipt(case.h.root)
        self.assertEqual(receipt['run_id'], command[command.index('--active-run-id') + 1])
        self.assertTrue((case.h.root / books.DISPATCH_PROOF).is_file())
        books.finish_book_worker_handoff(case.h.root, receipt['run_id'])
        self.assertFalse((case.h.root / books.DISPATCH_PROOF).exists())
        digest = initial.pop(CHECKPOINT_BINDING)
        atomic_json_write(initial, header)
        command = case._start('retry')
        rebound = gap.script.run_spec_receipt.load_receipt(case.h.root)
        self.assertEqual(digest, gap.script.run_spec_receipt.get_checkpoint_spec_digest(rebound))
        case._worker(command, failure='segment', boundary='after-prefix', exit_code=87)
        saved = gap.script.get_current_generation_checkpoint()
        self.assertEqual((digest, 1), (saved[CHECKPOINT_BINDING], saved['chunks_done']))
        case._worker(case._start('retry'))
        self.assertEqual(case.native._fresh_control(), case.h.rows())
        self.assertEqual(20, len(case.h.rows()))

    def test_old_nonempty_checkpoint_without_spec_evidence_is_preserved(self):
        case = self._case(gap.MULTI_SOURCE)
        case._worker(case._start(), failure='segment', boundary='after-prefix', exit_code=87)
        header = Path(case.h.artifact_paths()['checkpoint'])
        checkpoint = json.loads(header.read_bytes())
        checkpoint.pop(CHECKPOINT_BINDING, None)
        self.assertEqual(1, checkpoint['chunks_done'])
        atomic_json_write(checkpoint, header)
        case._assert_refused(checkpoint)
