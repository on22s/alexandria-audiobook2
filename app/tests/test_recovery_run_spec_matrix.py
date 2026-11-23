"""Admission-driven run-spec receipt architecture regressions."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
import three_pass_run_spec
from fastapi import HTTPException
from routers import script
from tests.test_script_recovery import ATTESTED, SOURCE, _long_source, _write_fingerprinted_attribute_run
from utils import atomic_json_write


class RunSpecReceiptTests(unittest.TestCase):
    def _fixture(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        path = _write_fingerprinted_attribute_run(str(root), _long_source(ATTESTED), [
            {'type': 'SPOKEN', 'text': 'Where is he?'},
            {'type': 'NARRATOR', 'text': 'asked Holmes. Watson watched the door.'},
            {'type': 'SPOKEN', 'text': 'Gone,'},
        ])
        states = dict(core.process_state)
        for value in states.values():
            value['running'] = False
        patches = [
            patch.object(script, 'SCRIPT_PATH', path),
            patch.object(script, 'DATA_DIR', str(root)),
            patch.object(script, 'CONFIG_PATH', str(root / 'config.json')),
            patch.object(script, 'process_state', states),
            patch.object(core, 'process_state', states),
        ]
        for item in patches:
            item.start()
        self.addCleanup(tmp.cleanup)
        for item in reversed(patches):
            self.addCleanup(item.stop)
        return root, path

    def test_secondary_provider_digest_binds_effective_failover_policy(self):
        primary = {'model_name': 'primary'}
        secondary = {'model_name': 'secondary'}
        digest = three_pass_run_spec.get_provider_request_digest(primary, secondary)
        self.assertEqual(digest, three_pass_run_spec.get_provider_request_digest(
            primary, dict(secondary, structured_output='auto', api_retry_limit=None,
                          retry_initial_delay_seconds=0.0, retry_multiplier=2.0,
                          retry_max_delay_seconds=30.0, retry_jitter=-0.0)))
        for changes in ({'structured_output': 'off'}, {'api_retry_limit': 0},
                        {'api_retry_limit': 2}, {'retry_multiplier': 3},
                        {'on_api_exhaustion': 'pause'}):
            with self.subTest(changes=changes):
                self.assertNotEqual(digest, three_pass_run_spec.get_provider_request_digest(
                    primary, dict(secondary, **changes)))

    def test_receipt_spec_matches_shared_resolver_and_survives_config_change(self):
        root, path = self._fixture()
        receipt = script.run_spec_receipt.load_receipt(str(root))
        self.assertIn('spec', receipt)
        self.assertTrue(receipt['spec']['prompts'])
        self.assertTrue(receipt['spec']['params'])
        state = json.loads((root / 'state.json').read_text(encoding='utf-8'))
        book = state['input_file_path']
        prepared, _ = script.get_prepared_source(
            book, state['script_generation_options']['strip_front_matter'], owning_root=str(root))
        recomputed = script.resolve_authoritative_run_spec(
            prepared, book, state['script_generation_options'],
            state['script_generation_selection'], state['script_generation_run_id'])
        self.assertEqual(receipt['spec'], recomputed['spec'])
        self.assertEqual(receipt['fingerprint'], recomputed['fingerprint'])
        config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
        config['llm']['model_name'] = 'changed-model'
        (root / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        self.assertEqual(receipt['fingerprint'],
                         script.run_spec_receipt.load_receipt(str(root))['fingerprint'])

    def test_admission_passes_receipt_seam_and_worker_uses_its_identity(self):
        import asyncio
        import tempfile as tempfile_module
        from unittest.mock import MagicMock

        with tempfile_module.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'book.txt'
            source.write_text(SOURCE, encoding='utf-8')
            atomic_json_write({'input_file_path': str(source),
                               'script_generation_input_file': str(source)},
                              root / 'state.json')
            scheduled = []
            patches = [
                patch.object(script, 'DATA_DIR', tmp),
                patch.object(script, 'SCRIPT_PATH', str(root / 'annotated_script.json')),
                patch.object(script, 'CONFIG_PATH', str(root / 'config.json')),
                patch.object(script, 'check_global_gpu_lock'),
                patch.object(script, 'get_active_reasoning_effort', return_value=None),
                patch.object(script, 'reserve_background_task', return_value='claim-1'),
                patch.object(script, 'register_claimed_background_task',
                             side_effect=lambda *args, **kwargs: scheduled.append(args)),
                patch.object(core, 'process_state', {'script': {'running': False}}),
                patch.object(core, '_task_claims', {'script': {'id': 'claim-1', 'phase': 'pending'}}),
                patch.object(core, '_gpu_leases', {}),
                patch.object(core, 'acquire_gpu_lock', return_value=None),
            ]
            for item in patches:
                item.start()
            try:
                script.start_script_generation(
                    script.BackgroundTasks(), str(source), None)
            finally:
                for item in reversed(patches):
                    item.stop()
            self.assertEqual(1, len(scheduled))
            command = scheduled[0][4]
            self.assertIn('--run-spec-receipt', command)
            self.assertIn('--run-spec-digest', command)
            receipt = script.run_spec_receipt.load_receipt(tmp)
            self.assertEqual(receipt['sha256'],
                             command[command.index('--run-spec-digest') + 1])
            self.assertEqual(receipt['fingerprint']['source_sha256'],
                             hashlib.sha256(SOURCE.encode('utf-8')).hexdigest())

    def test_receipt_mutation_is_bounded_and_preserves_all_artifacts(self):
        root, path = self._fixture()
        receipt_path = Path(script.run_spec_receipt.receipt_path(str(root)))
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        receipt['spec']['chunk_size'] = 7
        receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
        checkpoint = Path(script.three_pass_checkpoint_path(path))
        manifest = Path(script.three_pass_manifest_path(path))
        before = (checkpoint.read_bytes(), manifest.read_bytes(), receipt_path.read_bytes())
        with self.assertRaises(HTTPException) as raised:
            script.apply_manual_recovery(
                [{'n': 0, 'speaker': 'HOLMES'},
                 {'n': 1, 'speaker': 'NARRATOR'},
                 {'n': 2, 'speaker': 'WATSON'}], 'manual')
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual(before, (checkpoint.read_bytes(), manifest.read_bytes(),
                                  receipt_path.read_bytes()))

    def test_before_replace_guard_refuses_live_selection_change(self):
        root, path = self._fixture()
        checkpoint = Path(script.three_pass_checkpoint_path(path))
        manifest = Path(script.three_pass_manifest_path(path))
        receipt_path = Path(script.run_spec_receipt.receipt_path(str(root)))
        before = (checkpoint.read_bytes(), manifest.read_bytes(), receipt_path.read_bytes())
        state_path = root / 'state.json'
        original_publish = script.apply_book_state_locked
        entered = []

        def racing_publish(data_dir, replacements, removals, **kwargs):
            entered.append(True)
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state['book_generation'] = 'raced-generation'
            atomic_json_write(state, state_path)
            callback = kwargs.get('before_replace')
            if callback is not None:
                callback()
            return original_publish(data_dir, replacements, removals, **kwargs)

        state = json.loads(state_path.read_text(encoding='utf-8'))
        receipt = script.run_spec_receipt.load_receipt(str(root))
        snapshot = {'receipt_sha256': receipt['sha256'],
                    'book_generation': state.get('book_generation'),
                    'source_path': state.get('input_file_path'),
                    'selection': state.get('script_generation_selection')}
        with patch.object(script, 'apply_book_state_locked', side_effect=racing_publish):
            with self.assertRaises(HTTPException) as raised:
                script.apply_manual_recovery(
                    [{'n': 0, 'speaker': 'HOLMES'},
                     {'n': 1, 'speaker': 'NARRATOR'},
                     {'n': 2, 'speaker': 'WATSON'}], 'manual',
                    admission_snapshot=snapshot)
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual([True], entered)
        self.assertEqual(before, (checkpoint.read_bytes(), manifest.read_bytes(),
                                  receipt_path.read_bytes()))


if __name__ == '__main__':
    unittest.main()
