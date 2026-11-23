"""Native checkpoint and manifest identity at every Script recovery entry point."""
import json
from pathlib import Path
import re
import shutil
import unittest

from tests import test_review15_regressions as prior
from utils import atomic_json_write

gap = prior.gap
RECOVERY_READS = ('recovery', 'recovery?include_detail=true', 'recovery/detail')


class Review16Regressions(unittest.TestCase):
    def _case(self, *, cast=False):
        case = prior.Review15Regressions()
        case._harness(gap.MULTI_SOURCE)
        if cast:
            path = Path(gap.script.get_cast_list_path(str(case.h.source), str(case.h.root)))
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({'cast': [
                {'name': 'MIRA', 'aliases': ['MIREILLE']},
                {'name': 'DANA', 'aliases': ['DARIUS']},
            ]}))
        return case

    def _fixture_worker(self, config, *, stop_at_instruct=False):
        donor = gap.fixtures._Harness({'case': 'independent-native-worker'}, cast=True,
                                      config=config, source_text=gap.MULTI_SOURCE)
        try:
            command, _, _, receipt = donor.admit()
            code = gap._WORKER
            if stop_at_instruct:
                code = code.replace("args[2] == failure + '_failed'", "args[2] == 'instruct'")
            child = gap.prior.Review12Regressions()._child(
                donor, code, ['', 'before-manifest' if stop_at_instruct else 'settled', *command[3:]])
            self.assertEqual(87 if stop_at_instruct else 0, child.returncode, child.stderr + child.stdout)
            return donor, receipt
        except BaseException:
            donor.close()
            raise

    def _replace_checkpoint(self, case, active):
        original = gap.script.get_current_generation_checkpoint()
        target = Path(case.h.artifact_paths()['checkpoint'])
        donor, receipt = self._fixture_worker(
            gap.fixtures._config(force_segment_llm=True, custom_chunk=501), stop_at_instruct=True)
        try:
            donor_path = Path(donor.artifact_paths()['checkpoint'])
            directories = list(donor.root.glob(donor_path.name + '.parts-*'))
            self.assertTrue(directories, 'the donor must supply real checkpoint deltas')
            for directory in directories:
                self.assertTrue(list(directory.glob('*.json')))
                shutil.copytree(directory, case.h.root / directory.name)
            shutil.copyfile(donor_path, target)
        finally:
            donor.close()
        loaded = gap.script.get_current_generation_checkpoint()
        self.assertEqual((500, 501), (active['spec']['chunk_size'], receipt['spec']['chunk_size']))
        self.assertEqual(active['source_sha256'], receipt['source_sha256'])
        self.assertEqual(active['spec']['cast_sha256'], receipt['spec']['cast_sha256'])
        self.assertIsNotNone(active['spec']['cast_sha256'])
        self.assertEqual((original['stage'], original['chunks_done']),
                         (loaded['stage'], loaded['chunks_done']))
        self.assertEqual(('instruct', 4), (loaded['stage'], loaded['chunks_done']))
        self.assertEqual(original['segmented'], loaded['segmented'])
        self.assertEqual(receipt['fingerprint'], loaded['fingerprint'])
        self.assertNotEqual(active['fingerprint'], loaded['fingerprint'])
        return loaded

    def test_substituted_native_checkpoint_refuses_both_resume_routes(self):
        for route in ('', 'retry'):
            with self.subTest(route=route or 'generate'):
                case = self._case(cast=True)
                try:
                    _, receipt, manifest = case._crash_after_predecessor('segment', stage='instruct')
                    checkpoint = self._replace_checkpoint(case, receipt)
                    self.assertEqual(manifest, case.manifest.read_bytes())
                    self.assertEqual(receipt, gap.script.run_spec_receipt.load_receipt(case.h.root))
                    before = case.native._snapshot()
                    queued = case.dispatched.call_count
                    reply = case._post(route)
                    self.assertEqual(409, reply.status_code, reply.text)
                    self.assertEqual(queued, case.dispatched.call_count)
                    self.assertEqual(before, case.native._snapshot())
                    case._assert_refused(checkpoint)
                finally:
                    case.doCleanups()

    def test_intact_resume_and_explicit_start_over_match_twenty_control_rows(self):
        for start_over in (False, True):
            with self.subTest(start_over=start_over):
                case = self._case(cast=True)
                try:
                    control, _ = self._fixture_worker(case.h.config)
                    try:
                        rows = control.rows()
                        self.assertEqual(20, len(rows))
                    finally:
                        control.close()
                    _, receipt, _ = case._crash_after_predecessor('segment', stage='instruct')
                    if start_over:
                        self._replace_checkpoint(case, receipt)
                        command = case._start_over()
                        seed = gap.script.get_current_generation_checkpoint()
                        self.assertEqual(('segment', 0), (seed['stage'], seed['chunks_done']))
                    else:
                        case.native._panel(case._get('recovery?include_detail=true'), [True, False, False])
                        command = case._start()
                        self.assertIn('--require-matching-checkpoint', command)
                    logs = case._worker(command)
                    if start_over:
                        self.assertIn('Step 1 (split): chunk 1 of 4', logs)
                    else:
                        self.assertIn("4/4 chunks already split, stage 'instruct'", logs)
                    self.assertEqual(rows, case.h.rows())
                finally:
                    case.doCleanups()

    def test_current_first_attempt_manifest_requires_receipt_digest_for_every_action(self):
        case = self._case()
        self.addCleanup(case.doCleanups)
        case._worker(case._start(), failure='segment', exit_code=1)
        original = case.manifest.read_bytes()
        receipt = gap.script.run_spec_receipt.load_receipt(case.h.root)
        native = json.loads(original)
        self.assertNotIn('prior_manifest_sha256', receipt)
        self.assertEqual(receipt['sha256'], native['run_spec_sha256'])
        case.native._panel(case._get('recovery?include_detail=true'), [True, True, True])
        checkpoint = gap.script.get_current_generation_checkpoint()
        entries = [{'type': 'NARRATOR', 'text': text}
                   for text in re.split(r'(?<=[.!?])\s+', checkpoint['failed']['source'].strip())]
        for digest in (None, 'e' * 64):
            with self.subTest(digest=digest):
                changed = dict(native)
                if digest is None:
                    changed.pop('run_spec_sha256')
                else:
                    changed['run_spec_sha256'] = digest
                atomic_json_write(changed, case.manifest)
                before = case.native._snapshot()
                result = case._post('inject', {'chunk': 1, 'entries': entries})
                self.assertEqual(409, result.status_code, result.text)
                self.assertEqual(before, case.native._snapshot())
                case._assert_refused(checkpoint)
        case.manifest.write_bytes(original)
        case.native._panel(case._get('recovery?include_detail=true'), [True, True, True])

    def test_healthy_admission_before_first_checkpoint_has_no_recovery(self):
        case = self._case()
        self.addCleanup(case.doCleanups)
        command = case._start()
        self.assertTrue(case.receipt.is_file())
        self.assertFalse(case.manifest.exists())
        self.assertIsNone(gap.script.get_current_generation_checkpoint())
        before = case.native._snapshot()
        for route in RECOVERY_READS:
            result = case._get(route)
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual({'recoverable': False}, result.json())
        self.assertEqual(before, case.native._snapshot())
        case._worker(command)
        self.assertEqual(20, len(case.h.rows()))

    def test_missing_checkpoint_with_claimed_failure_never_starts_fresh(self):
        case = self._case()
        self.addCleanup(case.doCleanups)
        case._worker(case._start(), failure='segment', exit_code=1)
        Path(case.h.artifact_paths()['checkpoint']).unlink()
        self.assertTrue(case.manifest.exists())
        before = case.native._snapshot()
        count = case.dispatched.call_count
        response = case._post('')
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(count, case.dispatched.call_count)
        self.assertEqual(before, case.native._snapshot())
        case._assert_refused({})
