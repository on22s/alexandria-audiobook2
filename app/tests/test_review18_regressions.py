"""A revoked worker and an interrupted legacy upgrade retain recovery."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from script_selection_binding import publish_generated_script
from tests import test_review15_regressions as prior
from tests import test_review17_regressions as donor_fixture
from utils import atomic_json_write

gap = prior.gap

_LIFECYCLE_WORKER = gap._WORKER.replace(
    "if args[2] == failure + '_failed':\n        os._exit(87)",
    """
    if failure == 'wait' and args[2] == 'segment' and args[4] == 1:
        from pathlib import Path
        (Path(os.environ['ALEXANDRIA_DATA_DIR']) / 'worker-ready').touch()
        import time
        time.sleep(20)
    if failure == 'crash' and args[2] == 'segment' and args[4] == 2:
        os._exit(87)""").replace(
    "failed_passes = (failure,) if failure and boundary != 'after-prefix' else ()",
    "failed_passes = ()")


class Review18Regressions(unittest.TestCase):
    def _case(self, source=gap.MULTI_SOURCE):
        case = prior.Review15Regressions()
        case._harness(source)
        self.addCleanup(case.doCleanups)
        return case

    def _cancel_with_prefix(self, case):
        ready = case.h.root / 'worker-ready'
        before = len(case.h.rows() or [])
        task = gap.core.process_state['script']
        task.update(pid=None, process=None, paused=False, status='idle', return_code=None)
        commands = []

        def dispatch(command, name):
            commands.append(command)
            gap.core.run_process([sys.executable, '-u', '-c', _LIFECYCLE_WORKER, 'wait',
                                  'before-manifest', *command[3:]], name,
                                 cwd=str(gap.prior.APP_DIR))

        with patch.dict(os.environ, {'ALEXANDRIA_DATA_DIR': str(case.h.root),
                                     'PYTHONPATH': str(gap.prior.APP_DIR)}), \
                patch.object(gap.script, 'run_process', side_effect=dispatch), \
                ThreadPoolExecutor(max_workers=1) as executor:
            reply = executor.submit(case._post, '')
            deadline = time.monotonic() + 10
            while not ready.exists() and not reply.done() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists(), task['logs'])
            checkpoint = gap.script.get_current_generation_checkpoint()
            self.assertEqual(('segment', 1), (checkpoint['stage'], checkpoint['chunks_done']))
            pid = task['pid']
            self.assertIsNotNone(pid)
            stopped = case._post('cancel')
            self.assertEqual(200, stopped.status_code, stopped.text)
            self.assertEqual({'status': 'cancel signal sent', 'pid': pid}, stopped.json())
            self.assertEqual(200, reply.result(timeout=10).status_code)
            self.assertNotIn('script', gap.core._task_claims)
        self.assertEqual(1, len(commands))
        self.assertEqual(0, before)
        self.assertEqual(checkpoint, gap.script.get_current_generation_checkpoint())
        self.assertFalse(case.manifest.exists())
        self.assertIsNone(case.h.rows())
        receipt = gap.script.run_spec_receipt.load_receipt(case.h.root)
        state = json.loads((case.h.root / 'state.json').read_bytes())
        self.assertNotIn('script_generation_run_id', state)
        self.assertEqual(receipt['sha256'], state[gap.script.run_spec_receipt.STATE_DIGEST])
        selection = state['script_generation_selection']
        with self.assertRaisesRegex(ValueError, 'no longer current'):
            publish_generated_script(str(case.h.root), selection['book_token'], selection['source_path'],
                                     selection['source_sha256'], b'[{"text":"synthetic stale publication"}]',
                                     expected_run_id=receipt['run_id'])
        return checkpoint, state

    def test_native_cancel_then_generate_or_retry_survives_a_second_exit(self):
        for route in ('', 'retry'):
            with self.subTest(route=route or 'generate'):
                case = self._case()
                checkpoint, state = self._cancel_with_prefix(case)
                if not route:
                    # Older versions removed the cancel marker along with the
                    # owner, while retaining the independently bound receipt.
                    state.pop('script_generation_cancel_requested_run_id', None)
                    atomic_json_write(state, case.h.root / 'state.json')
                case.native._panel(case._get('recovery?include_detail=true'), [True, False, False])
                old_receipt = case.receipt.read_bytes()
                process = copy.deepcopy(gap.core.process_state['script'])
                case.receipt.unlink()
                case._assert_refused(checkpoint)
                self.assertEqual(process, gap.core.process_state['script'])
                atomic_json_write(dict(json.loads(old_receipt), sha256='e' * 64), case.receipt)
                case._assert_refused(checkpoint)
                self.assertEqual(process, gap.core.process_state['script'])
                case.receipt.write_bytes(old_receipt)

                command = case._start(route)
                child = gap.prior.Review12Regressions()._child(
                    case.h, _LIFECYCLE_WORKER, ['crash', 'before-manifest', *command[3:]])
                self.assertEqual(87, child.returncode, child.stderr + child.stdout)
                self.assertIn("1/4 chunks already split, stage 'segment'", child.stdout)
                self.assertEqual(2, gap.script.get_current_generation_checkpoint()['chunks_done'])
                self.assertFalse(case.manifest.exists())
                case.native._panel(case._get('recovery?include_detail=true'), [True, False, False])
                logs = case._worker(case._start('retry'))
                self.assertIn("2/4 chunks already split, stage 'segment'", logs)
                self.assertEqual(case.native._fresh_control(), case.h.rows())
                self.assertEqual(20, len(case.h.rows()))

    def test_native_legacy_upgrade_pins_predecessor_across_an_instruct_exit(self):
        for route in ('', 'retry'):
            with self.subTest(route=route or 'generate'):
                case = self._case()
                legacy = case.h.admit_legacy()
                source = gap._WORKER.replace("if args[2] == failure + '_failed':", "if False:")
                child = gap.prior.Review12Regressions()._child(
                    case.h, source, ['segment', 'after-prefix', *legacy[3:]])
                self.assertEqual(1, child.returncode, child.stderr + child.stdout)
                self.assertFalse(case.receipt.exists())
                predecessor = case.manifest.read_bytes()
                checkpoint = gap.script.get_current_generation_checkpoint()
                self.assertEqual(('segment_failed', 1), (checkpoint['stage'], checkpoint['chunks_done']))

                command = case._start('retry')
                receipt = gap.script.run_spec_receipt.load_receipt(case.h.root)
                if receipt is not None:
                    self.assertEqual(gap.script.three_pass_run_spec.spec_digest(checkpoint),
                                     receipt['legacy_checkpoint_sha256'])
                    self.assertEqual(gap.script.three_pass_run_spec.spec_digest(json.loads(predecessor)),
                                     receipt['prior_manifest_sha256'])
                    header = Path(case.h.artifact_paths()['checkpoint'])
                    saved = header.read_bytes()
                    changed = json.loads(saved)
                    changed['failed']['reason'] = 'Unrelated legacy evidence'
                    atomic_json_write(changed, header)
                    case._assert_refused(changed)
                    header.write_bytes(saved)
                case._worker(command, stage='instruct', boundary='before-manifest', exit_code=87)
                self.assertEqual(predecessor, case.manifest.read_bytes())
                self.assertEqual('instruct', gap.script.get_current_generation_checkpoint()['stage'])
                combined = case._get('recovery?include_detail=true')
                self.assertEqual(200, combined.status_code, combined.text)
                case.native._panel(combined, [True, False, False])
                before = case.native._snapshot()
                self.assertEqual(409, case._post('inject', {'chunk': 2, 'entries': []}).status_code)
                self.assertEqual(409, case._post('skip', {'chunk': 2}).status_code)
                self.assertEqual(before, case.native._snapshot())
                checkpoint = gap.script.get_current_generation_checkpoint()
                foreign = json.loads(predecessor)
                foreign['run_id'] = 'e' * 32
                atomic_json_write(foreign, case.manifest)
                case._assert_refused(checkpoint)
                case.manifest.write_bytes(predecessor)
                command = case._start(route)
                rebound = gap.script.run_spec_receipt.load_receipt(case.h.root)
                self.assertNotIn('legacy_checkpoint_sha256', rebound)
                self.assertEqual(receipt['prior_manifest_sha256'], rebound['prior_manifest_sha256'])
                logs = case._worker(command)
                self.assertIn("4/4 chunks already split, stage 'instruct'", logs)
                self.assertEqual(case.native._fresh_control(), case.h.rows())
                self.assertEqual(20, len(case.h.rows()))

    def test_same_selection_attribution_donor_preserves_process_and_artifacts(self):
        case = self._case(gap.fixtures.SOURCE)
        config_path = case.h.root / 'config.json'

        def start_over(config):
            atomic_json_write(config, config_path)
            return case._start_over()

        config = copy.deepcopy(case.h.config)
        config['generation']['three_pass_attribute_target_chars'] = 24
        command = start_over(config)
        donor = gap.prior.Review12Regressions()._child(
            case.h, donor_fixture._WINDOW_WORKER, ['', 'before-manifest', *command[3:]])
        self.assertEqual(87, donor.returncode, donor.stderr + donor.stdout)
        donor_checkpoint = gap.script.get_current_generation_checkpoint()
        donor_spec = gap.script.run_spec_receipt.load_receipt(case.h.root)['spec']
        header = Path(case.h.artifact_paths()['checkpoint'])
        donor_files = {header: header.read_bytes()}
        for folder in case.h.root.glob(header.name + '.parts-*'):
            donor_files.update({path: path.read_bytes() for path in folder.glob('*.json')})

        case._worker(start_over(case.h.config), failure='attribute', exit_code=1)
        case._worker(case._start('retry'), stage='instruct', boundary='before-manifest', exit_code=87)
        active = gap.script.get_current_generation_checkpoint()
        spec = gap.script.run_spec_receipt.load_receipt(case.h.root)['spec']
        self.assertEqual({'settings', 'attribution', 'run_id'},
                         {key for key in spec if donor_spec[key] != spec[key]})
        self.assertEqual(active['fingerprint'], donor_checkpoint['fingerprint'])
        self.assertNotEqual(active['named'], donor_checkpoint['named'])
        for path, content in donor_files.items():
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(content)
        self.assertEqual(donor_checkpoint, gap.script.get_current_generation_checkpoint())
        state = gap.core.process_state['script']
        state.update(cancel=True, _script_published_cleanup_stop=True)
        process = copy.deepcopy(state)
        case._assert_refused(donor_checkpoint)
        self.assertEqual(process, state)
