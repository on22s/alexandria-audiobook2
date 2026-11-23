"""Admission-driven real-worker recovery matrix (Segment/Attribute/Instruct)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import book_state_transaction as books
import core
import three_pass_generate as worker
from fastapi import HTTPException
from fastapi.testclient import TestClient
from fastapi import FastAPI
from routers import script
from three_pass_generate import validate_three_pass_checkpoint
from utils import atomic_json_write

SOURCE = '"Where is he?" said Holmes. "Gone," said me. Then the door closed.'


def _failing_chat():
    def create(**_kwargs):
        raise RuntimeError('deterministic provider failure')
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _stage_chat(seg_payload, attr_payload):
    def create(**kwargs):
        text = json.dumps(kwargs.get('messages', [])).lower()
        if 'instruct' in text or 'delivery' in text:
            raise RuntimeError('deterministic pass-3 provider failure')
        if 'speaker' in text:
            payload = attr_payload
        else:
            payload = seg_payload
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)), finish_reason='stop')],
            usage=None)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _phased_chat(payload_a, payload_b):
    calls = []
    def create(**_kwargs):
        calls.append(1)
        if len(calls) == 1:
            payload = payload_a
        elif len(calls) == 2:
            payload = payload_b
        else:
            raise RuntimeError('deterministic pass-3 provider failure')
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)), finish_reason='stop')],
            usage=None)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _chat(payloads):
    responses = iter(payloads)
    def create(**_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(next(responses))),
            finish_reason='stop')], usage=None)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class AdmittedWorkerMatrix(unittest.TestCase):
    def _fixture(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        source = root / 'book.txt'
        source.write_text(SOURCE, encoding='utf-8')
        atomic_json_write({'input_file_path': str(source),
                           'script_generation_input_file': str(source)},
                          root / 'state.json')
        atomic_json_write({'llm_mode': 'local', 'generation': {},
                           'llm': {'model_name': 'fixture-model',
                                   'base_url': 'http://localhost:1234/v1'}},
                          root / 'config.json')
        states = {'script': {'running': False, 'logs': [], 'cancel': False}}
        scheduled = []

        def reserve(_name, **_kwargs):
            claim_id = 'a' * 32
            core._task_claims['script'] = {'id': claim_id, 'phase': 'pending'}
            return claim_id

        patches = [patch.object(script, 'DATA_DIR', str(root)),
                   patch.object(script, 'SCRIPT_PATH', str(root / 'annotated_script.json')),
                   patch.object(script, 'CONFIG_PATH', str(root / 'config.json')),
                   patch.object(script, 'check_global_gpu_lock'),
                   patch.object(script, 'get_active_reasoning_effort', return_value=None),
                   patch.object(script, 'reserve_background_task', side_effect=reserve),
                   patch.object(script, 'register_claimed_background_task',
                                side_effect=lambda *a, **kw: scheduled.append(a)),
                   patch.object(core, 'process_state', states),
                   patch.object(script, 'process_state', states),
                   patch.object(core, '_task_claims', {}),
                   patch.object(core, '_gpu_leases', {}),
                   patch.object(core, 'acquire_gpu_lock', return_value=None),
                   patch.object(worker, 'get_runtime_data_dir', return_value=str(root))]
        for item in patches:
            item.start()
        self.addCleanup(tmp.cleanup)
        for item in reversed(patches):
            self.addCleanup(item.stop)
        return root, source, scheduled

    def _admit(self, root, source, scheduled):
        script.start_script_generation(script.BackgroundTasks(), str(source), None)
        command = scheduled[0][4]
        receipt_path = command[command.index('--run-spec-receipt') + 1]
        digest = command[command.index('--run-spec-digest') + 1]
        self.assertTrue(Path(receipt_path).exists())
        return command, receipt_path, digest

    def _worker(self, command, payloads=None, *, client=None, receipt_path=None, digest=None,
                pass2_policy=None):
        argv = list(command[3:])
        if pass2_policy is not None and '--pass2-on-exhaustion' in argv:
            argv[argv.index('--pass2-on-exhaustion') + 1] = pass2_policy
        if receipt_path is not None:
            argv[argv.index('--run-spec-receipt') + 1] = receipt_path
            argv[argv.index('--run-spec-digest') + 1] = digest
        fake = client if client is not None else _chat(payloads)
        with patch.object(sys, 'argv', [command[0]] + argv):
            try:
                worker.main(client_factory=(lambda *_args: fake))
            except SystemExit as error:
                return error.code
            finally:
                core.release_gpu_task_claim('script', 'a' * 32, pending_only=True)
        return 0

    def _segment_payload(self):
        return [{'type': row['type'], 'text': row['text']}
                for row in worker.split_outer_quote_regions(SOURCE) if row.get('text', '').strip()] \
            if hasattr(worker, 'split_outer_quote_regions') else [
                {'type': 'SPOKEN', 'text': 'Where is he?'},
                {'type': 'NARRATOR', 'text': 'said Holmes.'},
                {'type': 'SPOKEN', 'text': 'Gone,'}]

    def test_admitted_attribute_failure_then_native_skip_resumes_to_control(self):
        root, source, scheduled = self._fixture()
        command, receipt_path, digest = self._admit(root, source, scheduled)
        seg = [{'type': 'SPOKEN', 'text': 'Where is he?'},
               {'type': 'NARRATOR', 'text': 'said Holmes.'},
               {'type': 'SPOKEN', 'text': 'Gone,'}]
        bad_attr = [[{'n': 0, 'head': 'Where is he', 'speaker': 'NARRATOR'},
                     {'n': 1, 'head': 'Gone', 'speaker': 'NARRATOR'}]] * 4
        code = self._worker(command, [seg] + bad_attr)
        self.assertIn(code, (0, 1), 'worker code %s' % code)
        checkpoint = Path(script.three_pass_checkpoint_path(str(root / 'annotated_script.json')))
        self.assertEqual('attribute_failed', json.loads(checkpoint.read_text())['stage'])
        receipt = json.loads(Path(receipt_path).read_text())
        self.assertEqual(receipt['fingerprint'],
                         json.loads(checkpoint.read_text())['fingerprint'])
        before = (checkpoint.read_bytes(), Path(
            script.three_pass_manifest_path(str(root / 'annotated_script.json'))).read_bytes(),
            Path(receipt_path).read_bytes())
        app = FastAPI()
        app.include_router(script.router)
        with TestClient(app) as client:
            detail = client.get('/api/generate_script/recovery/detail')
            self.assertEqual(200, detail.status_code)
            response = client.post('/api/generate_script/skip', json={})
            self.assertEqual(200, response.status_code, response.text)
        after = (checkpoint.read_bytes(), Path(
            script.three_pass_manifest_path(str(root / 'annotated_script.json'))).read_bytes(),
            Path(receipt_path).read_bytes())
        self.assertNotEqual(before[0], after[0])
        validate_three_pass_checkpoint(json.loads(checkpoint.read_text()),
                                       receipt['chunk_count'])

    def test_admitted_instruct_failure_and_supported_cli_segment_failure_recover_natively(self):
        seg = [{'type': 'SPOKEN', 'text': 'Where is he?'},
               {'type': 'NARRATOR', 'text': 'said Holmes.'},
               {'type': 'SPOKEN', 'text': 'Gone,'}]
        attr = [{'n': i, 'speaker': 'HOLMES' if row['type'] == 'SPOKEN' else 'NARRATOR'}
                for i, row in enumerate(seg)]
        real_builder = script.build_generate_script_command

        def fail_exhaustion_builder(*args, **kwargs):
            command = real_builder(*args, **kwargs)
            return ['fail' if item == 'keep' else item for item in command]

        scenarios = (('segment_failed', _failing_chat(), None, 'fail'),
                     ('instruct_failed', _stage_chat(seg, attr), None, None))
        for stage, client, builder, policy in scenarios:
            with self.subTest(stage=stage):
                root, source, scheduled = self._fixture()
                if stage == 'segment_failed':
                    source.write_text('The quiet river moved past the old stone bridge. ' * 40,
                                      encoding='utf-8')
                patches = ([patch.object(script, 'build_generate_script_command',
                                         side_effect=builder)] if builder else [])
                for item in patches:
                    item.start()
                try:
                    command, receipt_path, _digest = self._admit(root, source, scheduled)
                finally:
                    for item in reversed(patches):
                        item.stop()
                # Modern admission always emits 'keep'; the Segment case below is
                # a supported worker CLI 'fail' invocation, not modern admission.
                self.assertEqual('keep', command[command.index('--pass2-on-exhaustion') + 1])
                self._worker(command, client=client, pass2_policy=policy)
                script_path = root / 'annotated_script.json'
                checkpoint = Path(script.three_pass_checkpoint_path(str(script_path)))
                actual = json.loads(checkpoint.read_text())['stage']
                self.assertEqual(stage, actual, 'worker wrote %s for %s' % (actual, stage))
                app = FastAPI()
                app.include_router(script.router)
                with TestClient(app) as client_app:
                    self.assertEqual(200, client_app.get(
                        '/api/generate_script/recovery/detail').status_code)
                    if stage == 'segment_failed':
                        response = client_app.post('/api/generate_script/inject', json={
                            'chunk': 1, 'entries': [{'type': 'NARRATOR',
                                                     'text': source.read_text(encoding='utf-8')}]})
                    else:
                        response = client_app.post('/api/generate_script/skip', json={})
                    self.assertEqual(200, response.status_code, response.text)
                validate_three_pass_checkpoint(json.loads(checkpoint.read_text()),
                                               json.loads(Path(receipt_path).read_text())['chunk_count'])

    def test_admitted_cast_run_uses_receipt_cast_digest(self):
        root, source, scheduled = self._fixture()
        import hashlib
        from cast_list import CAST_LISTS_DIRNAME
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        cast_dir = root / CAST_LISTS_DIRNAME
        cast_dir.mkdir(exist_ok=True)
        cast = {'cast': [{'name': 'HOLMES', 'aliases': ['SHERLOCK']},
                         {'name': 'WATSON', 'aliases': []}]}
        (cast_dir / (digest + '.json')).write_text(json.dumps(cast), encoding='utf-8')
        command, receipt_path, _receipt_digest = self._admit(root, source, scheduled)
        receipt = json.loads(Path(receipt_path).read_text())
        self.assertIsNotNone(receipt['spec']['cast_sha256'])
        seg = [{'type': 'SPOKEN', 'text': 'Where is he?'},
               {'type': 'NARRATOR', 'text': 'said Holmes.'},
               {'type': 'SPOKEN', 'text': 'Gone,'}]
        bad_attr = [[{'n': 0, 'speaker': 'NARRATOR'},
                     {'n': 1, 'speaker': 'NARRATOR'},
                     {'n': 2, 'speaker': 'NARRATOR'}]] * 4
        self._worker(command, [seg] + bad_attr)
        checkpoint = Path(script.three_pass_checkpoint_path(str(root / 'annotated_script.json')))
        self.assertEqual('attribute_failed', json.loads(checkpoint.read_text())['stage'])

    def test_worker_refuses_mismatched_receipt_digest(self):
        root, source, scheduled = self._fixture()
        command, receipt_path, digest = self._admit(root, source, scheduled)
        code = self._worker(command, [], receipt_path=receipt_path, digest='b' * 64)
        self.assertNotEqual(0, code)
        checkpoint = Path(script.three_pass_checkpoint_path(str(root / 'annotated_script.json')))
        self.assertFalse(checkpoint.exists())


if __name__ == '__main__':
    unittest.main()
