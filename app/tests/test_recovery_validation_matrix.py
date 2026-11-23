"""Real admission -> receipt -> worker -> recovery validation matrix (#2207).

Every case starts a genuine ``start_script_generation`` admission, captures the
emitted ``--run-spec-receipt`` / ``--run-spec-digest`` pair, and invokes the real
worker ``main`` in-process with those exact arguments. Only process isolation
and the LLM transport are stubbed; receipts, checkpoints, manifests and final
scripts are written by production code. Each case records inputs, the exact
call/transition ordering, expected and observed results and artifact digests
into a per-case JSON artifact (see ``MATRIX_ARTIFACT``).
"""
import copy
import hashlib
import json
import os
import re
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
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pass_quality import split_outer_quote_regions
from routers import script
from three_pass_generate import (three_pass_checkpoint_path, three_pass_manifest_path,
                                 validate_three_pass_checkpoint)
from utils import atomic_json_write

MATRIX_ARTIFACT = os.environ.get(
    'AA2_RECOVERY_MATRIX_ARTIFACT', '/tmp/opencode/recovery_validation_matrix.json')
SOURCE = (
    '"Where is he?" asked MIRA. "Gone," said DANA. MIRA walked beside the river and '
    'looked at the door for a long moment. DANA waited outside in the cold. '
    'MIRA closed the door. DANA looked at the window beside the old stone bridge.'
)
SPEAKERS = ('MIRA', 'DANA')
P1_MARK = 'MATRIXPASS1-AUTHORITATIVE'
P3_MARK = 'MATRIXPASS3-AUTHORITATIVE'
_ARTIFACT = {'cases': []}


def _digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _prompt_batch(text):
    match = re.search(r'\[\s*\{\s*"n"\s*:.*?\}\s*\]', text, re.DOTALL)
    return json.loads(match.group(0)) if match else []


def _content_spec_digest(spec):
    content = {key: value for key, value in spec.items()
               if key not in ('run_id', 'book_token', 'source_path')}
    return hashlib.sha256(json.dumps(
        content, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest()


def _speaker_for(row):
    if row.get('type') != 'SPOKEN':
        return 'NARRATOR'
    return 'DANA' if 'Gone' in str(row.get('text', '')) else 'MIRA'


def _pick(tag, payloads, fail, calls, text=''):
    if calls is not None:
        calls.append(tag)
    if tag in fail:
        raise RuntimeError('deterministic matrix failure: ' + tag)
    if tag == 'attribute':
        batch = _prompt_batch(text)
        if batch:
            return [{'n': row['n'], 'speaker': _speaker_for(row)} for row in batch]
    if tag == 'instruct':
        batch = _prompt_batch(text)
        if batch:
            return [{'n': row['n'], 'instruct': 'Calm and measured.'} for row in batch]
    if tag == 'segment':
        match = re.search(r'SOURCE TEXT:\s*(.*)', text, re.DOTALL)
        if match:
            chunk = match.group(1).strip()
            if '"' not in chunk and '\u201c' not in chunk:
                sentences = [part.strip() for part in re.split(r'(?<=[.!?])\s+', chunk)
                             if part.strip()]
                if sentences:
                    return [{'type': 'NARRATOR', 'text': part} for part in sentences]
            regions = [{'type': row['type'], 'text': row['text']}
                       for row in split_outer_quote_regions(chunk) if row.get('text', '').strip()]
            if regions:
                return regions
    return payloads[tag]


def _factory(payloads, fail=(), calls=None, failover_used=False):
    """A content-addressed transport: same prompt always yields same payload."""
    def create(**kwargs):
        messages = kwargs.get('messages', [])
        text = ' '.join(str(message.get('content', '')) for message in messages
                        if isinstance(message, dict))
        if P3_MARK in text or 'instruct' in text.lower() or 'delivery' in text.lower():
            tag = 'instruct'
        elif 'speaker' in text.lower() or 'roster' in text.lower():
            tag = 'attribute'
        else:
            tag = 'segment'
        payload = _pick(tag, payloads, fail, calls, text)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)), finish_reason='stop')],
            usage=None)
    def factory(*_args, **_kwargs):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        if failover_used:
            client.secondary_model = 'matrix-failover-model'
            client.switched = True
        return client
    return factory


def _payloads():
    seg = [{'type': row['type'], 'text': row['text']}
           for row in split_outer_quote_regions(SOURCE) if row.get('text', '').strip()]
    attr = [{'n': i,
             'speaker': _speaker_for(row)}
            for i, row in enumerate(seg)]
    instruct = [{'n': i, 'instruct': 'Calm and measured.'} for i in range(len(seg))]
    return {'segment': seg, 'attribute': attr, 'instruct': instruct}


def _record_dispatch(create, dispatched):
    def wrapped(**kwargs):
        dispatched.append(kwargs.get('model'))
        return create(**kwargs)
    return wrapped


def _config(*, custom_prompts=False, custom_chunk=None, context=32768,
            small_retries=True, failover=False, force_segment_llm=False):
    config = {
        'llm_mode': 'local',
        'llm': {'model_name': 'matrix-primary', 'base_url': 'http://localhost:1234/v1',
                'context_length': context, 'max_output_tokens': 4096},
        'generation': {'three_pass_segmentation': 'llm' if force_segment_llm else 'auto',
                       'three_pass_chunk_size': custom_chunk or 3000,
                       'three_pass_attribute_context_chars': 0,
                       'three_pass_attribute_prompt_variant': 'default',
                       'three_pass_delivery_direction': 'off',
                       'temperature': 0.3},
    }
    if failover:
        config['llm_failover'] = True
        config['llm_remote'] = {
            'model_name': 'matrix-failover-model',
            'base_url': 'http://localhost:1235/v1', 'context_length': context,
            'max_output_tokens': 4096}
    if small_retries:
        config['llm'].update(retry_initial_delay_seconds=0.01, retry_multiplier=1,
                             retry_max_delay_seconds=0.01, retry_jitter=0)
    if custom_prompts:
        config['prompts'] = {
            'pass1_preset': 'matrix',
            'pass1_prompt_presets': [{'name': 'matrix', 'system_prompt': P1_MARK,
                                      'user_prompt': 'Return a JSON array.\nSOURCE TEXT:\n{chunk}'}],
            'pass3_preset': 'matrix',
            'pass3_prompt_presets': [{'name': 'matrix', 'system_prompt': P3_MARK,
                                      'user_prompt': 'Return a JSON array.\n{batch}'}],
        }
    return config


class _Harness:
    def __init__(self, case, *, cast=False, config=None, options=None, source_text=None):
        self.case = case
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'book.txt'
        self.source.write_text(source_text or SOURCE, encoding='utf-8')
        self.script_path = self.root / 'annotated_script.json'
        self.state = {'input_file_path': str(self.source),
                      'script_generation_input_file': str(self.source),
                      'script_generation_options': dict(options or {}),
                      'script_generation_run_id': 'a' * 32}
        atomic_json_write(self.state, self.root / 'state.json')
        self.config = config or _config()
        atomic_json_write(self.config, self.root / 'config.json')
        if cast:
            digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
            cast_dir = self.root / 'cast_lists'
            cast_dir.mkdir(exist_ok=True)
            cast_bytes = json.dumps({'cast': [
                {'name': 'MIRA', 'aliases': ['MIREILLE']}, {'name': 'DANA', 'aliases': ['DARIUS']}]}).encode()
            (cast_dir / (digest + '.json')).write_bytes(cast_bytes)
            self.state['script_generation_options']['cast_sha256'] = hashlib.sha256(cast_bytes).hexdigest()
            atomic_json_write(self.state, self.root / 'state.json')
        self.states = {'script': {'running': False, 'logs': [], 'cancel': False},
                       'review': {'running': False, 'logs': [], 'cancel': False}}
        self.scheduled = []
        self.worker_observed = {}

        def reserve(*_args, **_kwargs):
            core._task_claims['script'] = {'id': 'a' * 32, 'phase': 'pending'}
            return 'a' * 32

        def register(*args, **kwargs):
            self.scheduled.append(args)
            claim = core._task_claims.get('script')
            if isinstance(claim, dict):
                claim['phase'] = 'done'

        self.patches = [
            patch.object(script, 'DATA_DIR', str(self.root)),
            patch.object(script, 'SCRIPT_PATH', str(self.script_path)),
            patch.object(script, 'CONFIG_PATH', str(self.root / 'config.json')),
            patch.object(script, 'check_global_gpu_lock'),
            patch.object(script, 'get_active_reasoning_effort', return_value=None),
            patch.object(script, 'reserve_background_task', side_effect=reserve),
            patch.object(script, 'register_claimed_background_task', side_effect=register),
            patch.object(core, 'process_state', self.states),
            patch.object(script, 'process_state', self.states),
            patch.object(core, '_task_claims', {}),
            patch.object(core, '_gpu_leases', {}),
            patch.object(core, 'acquire_gpu_lock', return_value=None),
            patch.object(worker, 'get_runtime_data_dir', return_value=str(self.root)),
        ]
        for item in self.patches:
            item.start()
        self.case['inputs'] = {
            'source_text': SOURCE, 'cast': bool(cast),
            'config': self.config,
            'state_options': self.state['script_generation_options'],
        }
        self.case['order'] = []

    def close(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def admit(self):
        script.start_script_generation(script.BackgroundTasks(), str(self.source), None)
        self.case['order'].append('admission')
        command = self.scheduled[-1][4]
        receipt_path = command[command.index('--run-spec-receipt') + 1]
        digest = command[command.index('--run-spec-digest') + 1]
        receipt = script.run_spec_receipt.load_receipt_file(receipt_path)
        assert receipt['sha256'] == digest
        self.case['inputs']['receipt_digest'] = digest
        self.case['inputs']['receipt_spec_digest'] = script.three_pass_run_spec.spec_digest(receipt['spec'])
        return command, receipt_path, digest, receipt

    def admit_legacy(self):
        """Reproduce a pre-receipt app admission and run the native CLI."""
        source = str(self.source)
        options = {'strip_front_matter': True, 'first_person_narrator': None,
                   'cast_sha256': None, **self.state['script_generation_options']}
        cast = script.get_existing_cast_list(source)
        cast_path = Path(script.get_run_cast_path()) if cast else None
        if cast_path is not None:
            cast_path.write_bytes(Path(cast).read_bytes())
        selection = script.capture_generation_selection(str(self.root), source)
        run_id = self.state['script_generation_run_id']
        atomic_json_write(dict(self.state, script_generation_options=options,
                               script_generation_selection=selection,
                               script_generation_base_sha256=None), self.root / 'state.json')
        return script.build_generate_script_command(
            source, strip_front_matter=options['strip_front_matter'],
            first_person_narrator=options['first_person_narrator'],
            cast_file=str(cast_path) if cast_path is not None else None,
            selection_binding=dict(selection, run_id=run_id))

    def worker(self, command, factory, *, receipt_path=None, digest=None, policy=None):
        argv = list(command[3:])
        if policy is not None and '--pass2-on-exhaustion' in argv:
            argv[argv.index('--pass2-on-exhaustion') + 1] = policy
        if receipt_path is not None:
            argv[argv.index('--run-spec-receipt') + 1] = receipt_path
            argv[argv.index('--run-spec-digest') + 1] = digest
        real_run = worker.run_three_pass

        def capture_run(*args, **kwargs):
            self.worker_observed = {
                'model_name': args[1] if len(args) > 1 else kwargs.get('model_name'),
                'params': args[3] if len(args) > 3 else kwargs.get('params'),
                'chunk_size': args[4] if len(args) > 4 else kwargs.get('chunk_size'),
                'run_spec_receipt': kwargs.get('run_spec_receipt'),
                'output_path': kwargs.get('output_path'),
            }
            return real_run(*args, **kwargs)

        with patch.object(sys, 'argv', [command[0]] + argv), \
                patch.object(worker, 'run_three_pass', side_effect=capture_run):
            try:
                worker.main(client_factory=factory)
            except SystemExit as error:
                code = error.code
            else:
                code = 0
        self.case['order'].append('worker:%s' % ('ok' if code == 0 else 'exit-%s' % code))
        return code

    def api(self, method, route, payload=None):
        app = FastAPI()
        app.include_router(script.router)
        with TestClient(app) as client:
            response = client.get(route) if method == 'get' else client.post(route, json=payload)
        self.case['order'].append('%s %s -> %s' % (method, route, response.status_code))
        return response

    def artifact_paths(self):
        return {
            'receipt': script.run_spec_receipt.receipt_path(str(self.root)),
            'state': str(self.root / 'state.json'),
            'checkpoint': three_pass_checkpoint_path(str(self.script_path)),
            'manifest': three_pass_manifest_path(str(self.script_path)),
            'script': str(self.script_path),
        }

    def snapshot(self):
        return {name: _digest(path) for name, path in self.artifact_paths().items()}

    def rows(self):
        try:
            return json.loads(self.script_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def finish(self, expected, observed, *, paths=()):
        self.case['expected'] = expected
        self.case['observed'] = observed
        self.case['artifacts'] = self.snapshot()
        for label, path in paths:
            self.case['artifacts'][label] = _digest(path)
        snapshot_case = copy.deepcopy({key: value for key, value in self.case.items()
                                       if not key.startswith('_')})
        _ARTIFACT['cases'].append(snapshot_case)


def _write_artifact():
    Path(MATRIX_ARTIFACT).parent.mkdir(parents=True, exist_ok=True)
    Path(MATRIX_ARTIFACT).write_text(json.dumps(_ARTIFACT, indent=2, ensure_ascii=False),
                                     encoding='utf-8')


class ControlAndRecoveryMatrix(unittest.TestCase):
    def test_location_independent_spec_comparison_detects_settings_drift(self):
        left = {'source_path': '/one/book.txt', 'book_token': 'one', 'run_id': 'run-1',
                'source_sha256': 'a' * 64, 'params': {'temperature': 0.3}}
        right = dict(left, source_path='/two/book.txt', book_token='two', run_id='run-2')
        self.assertEqual(_content_spec_digest(left), _content_spec_digest(right))
        right['params'] = {'temperature': 0.7}
        self.assertNotEqual(_content_spec_digest(left), _content_spec_digest(right))

    @classmethod
    def tearDownClass(cls):
        _write_artifact()

    def _case(self, name):
        case = {'case': name}
        return case

    def _run_case(self, case, *, cast=False, config=None, fail=(), payloads=None,
                  policy=None, recovery=None, mutate_config=None, compare_to=None,
                  failover_used=False):
        harness = _Harness(case, cast=cast, config=config)
        self.addCleanup(harness.close)
        command, receipt_path, digest, receipt = harness.admit()
        calls = []
        factory = _factory(payloads or _payloads(), fail=fail, calls=calls,
                           failover_used=failover_used)
        if mutate_config is not None:
            mutate_config(harness)
        code = harness.worker(command, factory, policy=policy)
        case['order_calls'] = calls
        if recovery is None:
            intended_exit = 1 if fail else 0
            expected = {'worker_exit': intended_exit,
                        'final_rows': 'control output' if not fail else 'failed checkpoint'}
            observed = {'worker_exit': code, 'final_rows': harness.rows()}
            self.assertEqual(intended_exit, code, 'worker did not take the intended path')
            harness.finish(expected, observed, paths=(('receipt_path', receipt_path),))
            return harness, receipt, code
        checkpoint = Path(three_pass_checkpoint_path(str(harness.script_path)))
        stage = json.loads(checkpoint.read_text())['stage'] if checkpoint.exists() else None
        detail = harness.api('get', '/api/generate_script/recovery/detail')
        before = harness.snapshot()
        action = recovery(harness, detail)
        after = harness.snapshot()
        expected = {'failed_stage': stage, 'detail': 200,
                    'artifacts_preserved_on_refusal': before == after}
        observed = {'failed_stage': stage, 'detail': detail.status_code,
                    'artifacts_preserved_on_refusal': before == after,
                    'action': action}
        harness.finish(expected, observed, paths=(('receipt_path', receipt_path),))
        if compare_to is not None:
            self.assertEqual(compare_to, harness.rows())
        return harness, receipt, code

    def test_control_undamaged_run(self):
        case = self._case('control/no-cast/undamaged')
        harness, receipt, code = self._run_case(case)
        self.assertEqual(0, code)
        rows = harness.rows()
        self.assertIsNotNone(rows)
        self.assertEqual(len(_payloads()['segment']), len(rows))
        self.assertEqual(['Calm and measured.'] * len(rows), [row['instruct'] for row in rows])
        self.assertEqual(['MIRA', 'NARRATOR', 'DANA', 'NARRATOR'],
                         [row['speaker'] for row in rows])
        validate_three_pass_checkpoint(json.loads(Path(
            three_pass_checkpoint_path(str(harness.script_path))).read_text()),
            receipt['chunk_count'])

    def test_control_custom_prompts_params_and_chunking(self):
        case = self._case('control/custom-prompts-params-chunking')
        config = _config(custom_prompts=True, custom_chunk=1400, context=65536)
        harness, receipt, code = self._run_case(case, config=config)
        self.assertEqual(0, code)
        self.assertEqual(P1_MARK, receipt['spec']['prompts']['segment_system'])
        self.assertEqual(P3_MARK, receipt['spec']['prompts']['instruct_system'])
        self.assertEqual(1400, receipt['spec']['chunk_size'])
        self.assertEqual(65536, receipt['spec']['llm_profile']['context_length'])
        self.assertIsNotNone(harness.rows())

    def test_worker_uses_receipt_after_live_config_mutation(self):
        case = self._case('control/config-mutation-after-admission')
        def mutate(harness):
            config = json.loads((harness.root / 'config.json').read_text())
            config['generation']['three_pass_chunk_size'] = 97
            config['prompts'] = {
                'pass1_preset': 'live',
                'pass1_prompt_presets': [{'name': 'live', 'system_prompt': 'LIVE-P1',
                                          'user_prompt': '{batch}'}],
            }
            atomic_json_write(config, harness.root / 'config.json')
        harness, receipt, code = self._run_case(case, config=_config(custom_prompts=True),
                                                mutate_config=mutate)
        self.assertEqual(0, code)
        self.assertEqual('matrix-primary', receipt['spec']['model_name'])
        self.assertEqual(P1_MARK, receipt['spec']['prompts']['segment_system'])
        self.assertEqual(3000, receipt['spec']['chunk_size'])
        self.assertIsNotNone(harness.rows())
        checkpoint = json.loads(Path(three_pass_checkpoint_path(str(harness.script_path))).read_text())
        self.assertEqual(receipt['fingerprint']['model_name'], checkpoint['fingerprint']['model_name'])

    def test_worker_refuses_receipt_digest_mismatch_before_writing(self):
        case = self._case('receipt/digest-mismatch')
        refusal = {'case': 'receipt/digest-mismatch', 'inputs': {}, 'order': [],
                   'expected': {}, 'observed': {}, 'artifacts': {}}
        harness = _Harness(refusal, config=_config())
        self.addCleanup(harness.close)
        command, receipt_path, digest, receipt = harness.admit()
        refusal['inputs']['admitted_digest'] = digest
        receipt_before = _digest(receipt_path)
        code = harness.worker(command, _factory(_payloads()),
                              receipt_path=receipt_path, digest='b' * 64)
        refusal['order'] = harness.case['order']
        refusal['expected'] = {'worker_exit': 'nonzero',
                               'checkpoint_written': False,
                               'receipt_unchanged': True}
        refusal['observed'] = {'worker_exit': code,
                               'checkpoint_written': Path(three_pass_checkpoint_path(
                                   str(harness.script_path))).exists(),
                               'receipt_unchanged': _digest(receipt_path) == receipt_before}
        refusal['artifacts'] = harness.snapshot()
        refusal['artifacts']['receipt_path'] = _digest(receipt_path)
        self.assertNotEqual(0, code)
        self.assertFalse(refusal['observed']['checkpoint_written'])
        _ARTIFACT['cases'].append(refusal)

    def test_attribute_failure_detail_then_inject_matches_control(self):
        case = self._case('attribute/no-cast/inject')
        fail = ('attribute',)
        observed = {}
        def act(harness, detail):
            entries = [{'n': index, 'speaker': 'MIRA'} for index in detail.json()['batch_indices']]
            response = harness.api('post', '/api/generate_script/inject', {'entries': entries})
            observed['status'] = response.status_code
            return {'route': 'inject', 'status': response.status_code}
        harness, receipt, code = self._run_case(case, fail=fail, recovery=act)
        self.assertNotEqual(0, code)
        checkpoint = Path(three_pass_checkpoint_path(str(harness.script_path)))
        self.assertEqual('attribute', json.loads(checkpoint.read_text())['stage'])
        info = harness.api('get', '/api/generate_script/recovery/detail').json()
        self.assertFalse(info.get('recoverable'), 'the failed unit must be retired after injection')
        self.assertEqual(200, observed['status'])
        validate_three_pass_checkpoint(json.loads(checkpoint.read_text()), receipt['chunk_count'])

    def test_attribute_failure_skip_is_accepted(self):
        case = self._case('attribute/no-cast/skip')
        observed = {}
        def act(harness, detail):
            response = harness.api('post', '/api/generate_script/skip', {})
            observed['status'] = response.status_code
            observed['resolution'] = response.json().get('resolution')
            return {'route': 'skip', 'status': response.status_code}
        harness, receipt, code = self._run_case(case, fail=('attribute',), recovery=act)
        self.assertNotEqual(0, code)
        self.assertEqual(200, observed['status'])
        self.assertEqual('narrated_as_is', observed['resolution'])

    def test_instruct_failure_inject_and_skip(self):
        for action in ('inject', 'skip'):
            with self.subTest(action=action):
                case = self._case('instruct/no-cast/' + action)
                observed = {}
                def act(harness, detail, action=action):
                    if action == 'inject':
                        payload = {'entries': [{'n': index, 'instruct': 'Calm and measured.'}
                                               for index in detail.json()['batch_indices']]}
                    else:
                        payload = {}
                    route = '/api/generate_script/inject' if action == 'inject' \
                        else '/api/generate_script/skip'
                    response = harness.api('post', route, payload)
                    observed['status'] = response.status_code
                    return {'route': route, 'status': response.status_code}
                harness, receipt, code = self._run_case(case, fail=('instruct',), recovery=act)
                self.assertNotEqual(0, code)
                checkpoint = Path(three_pass_checkpoint_path(str(harness.script_path)))
                self.assertEqual('instruct', json.loads(checkpoint.read_text())['stage'])
                self.assertEqual(200, observed['status'])
                validate_three_pass_checkpoint(json.loads(checkpoint.read_text()),
                                               receipt['chunk_count'])


class FullStageActionMatrix(unittest.TestCase):
    """Stage x cast x supported-action matrix, each against an undamaged control."""

    @classmethod
    def tearDownClass(cls):
        _write_artifact()

    def _new_case(self, name, **kwargs):
        case = {'case': name, 'inputs': {}, 'order': [], 'expected': {},
                'observed': {}, 'artifacts': {}, 'comparison': {}}
        harness = _Harness(case, **kwargs)
        return case, harness

    def _control(self, *, cast=False, config=None, source_text=None):
        case, harness = self._new_case('control-reference', cast=cast, config=config,
                                       source_text=source_text)
        self.addCleanup(harness.close)
        command, _, _, receipt = harness.admit()
        code = harness.worker(command, _factory(_payloads()))
        self.assertEqual(0, code)
        checkpoint = json.loads(Path(three_pass_checkpoint_path(str(harness.script_path))).read_text())
        manifest = json.loads(Path(three_pass_manifest_path(str(harness.script_path))).read_text())
        return {'rows': harness.rows(), 'receipt': receipt,
                'fingerprint': receipt['fingerprint'], 'chunk_count': receipt['chunk_count'],
                'checkpoint': checkpoint, 'manifest_status': manifest.get('status'),
                'snapshot': harness.snapshot()}

    def _failed(self, case, *, cast=False, config=None, source_text=None, stage='attribute',
                failover_used=False):
        harness = _Harness(case, cast=cast, config=config, source_text=source_text)
        self.addCleanup(harness.close)
        command, receipt_path, digest, receipt = harness.admit()
        case['inputs']['modern_pass2_policy'] = command[command.index('--pass2-on-exhaustion') + 1]
        self.assertEqual('keep', case['inputs']['modern_pass2_policy'])
        policy = 'fail' if stage == 'segment' else None
        codes = {'segment': ('segment',), 'attribute': ('attribute',), 'instruct': ('instruct',)}
        code = harness.worker(command, _factory(_payloads(), fail=codes[stage],
                                                failover_used=failover_used), policy=policy)
        self.assertNotEqual(0, code)
        checkpoint_path = Path(three_pass_checkpoint_path(str(harness.script_path)))
        checkpoint = json.loads(checkpoint_path.read_text())
        self.assertEqual(stage + '_failed', checkpoint['stage'])
        case['observed']['failed_stage'] = checkpoint['stage']
        case['observed']['failed_pass'] = checkpoint['failed'].get('pass')
        case['observed']['receipt_digest'] = receipt['sha256']
        app = FastAPI()
        app.include_router(script.router)
        with TestClient(app) as client:
            detail = client.get('/api/generate_script/recovery/detail')
            case['order'].append('detail -> %s' % detail.status_code)
            self.assertEqual(200, detail.status_code, detail.text)
            info = detail.json()
            self.assertEqual(stage, info['failed_pass'])
            case['observed']['detail'] = {
                'failed_pass': info['failed_pass'], 'batch_indices': info.get('batch_indices'),
                'batch_entries': info.get('batch_entries'), 'source': info.get('source')}
        return harness, command, receipt_path, digest, receipt, info

    def _inject_entries(self, stage, detail):
        if stage == 'segment':
            entries = [{'type': row['type'], 'text': row['text']}
                       for row in split_outer_quote_regions(detail['source'])
                       if row.get('text', '').strip()]
        elif stage == 'attribute':
            entries = [{'n': index, 'speaker': _speaker_for(row)}
                       for index, row in zip(detail['batch_indices'], detail['batch_entries'])]
        else:
            entries = [{'n': index, 'instruct': 'Calm and measured.'}
                       for index, row in zip(detail['batch_indices'], detail['batch_entries'])]
        return entries

    def _apply_action(self, harness, command, stage, action, detail, receipt_path, digest):
        app = FastAPI()
        app.include_router(script.router)
        with TestClient(app) as client:
            if action == 'detail':
                response = client.get('/api/generate_script/recovery/detail')
                payload = None
            elif action == 'inject':
                payload = ({'chunk': detail.get('failed_chunk') or 1,
                            'entries': self._inject_entries(stage, detail)}
                           if stage == 'segment'
                           else {'entries': self._inject_entries(stage, detail)})
                response = client.post('/api/generate_script/inject', json=payload)
            elif action == 'skip':
                payload = {'chunk': detail.get('failed_chunk') or 1} if stage == 'segment' else {}
                response = client.post('/api/generate_script/skip', json=payload)
            elif action == 'retry':
                payload = None
                response = client.post('/api/generate_script/retry', json={})
            elif action == 'resume':
                payload = None
                response = None
            else:
                raise AssertionError(action)
        if response is None:
            harness.case['order'].append('resume requested')
            result = {'resume': True}
        else:
            harness.case['order'].append('%s -> %s' % (action, response.status_code))
            self.assertEqual(200, response.status_code, response.text)
            result = response.json() if response.content else {}
        if action == 'retry':
            command = harness.scheduled[-1][4]
            harness.case['inputs']['retried_command_digest'] = hashlib.sha256(
                json.dumps(command).encode()).hexdigest()
        return command, result

    def _finish_rows(self, harness, command):
        harness.case['inputs']['completion_command_receipt_digest'] = command[
            command.index('--run-spec-digest') + 1]
        code = harness.worker(command, _factory(_payloads()))
        harness.case['order'].append('finish-worker -> %s' % code)
        return code

    def _compare(self, case, harness, control, stage, action, *, exact_rows=True):
        rows = harness.rows()
        self.assertIsNotNone(rows)
        manifest = json.loads(Path(three_pass_manifest_path(str(harness.script_path))).read_text())
        checkpoint = json.loads(Path(three_pass_checkpoint_path(str(harness.script_path))).read_text())
        receipt = script.run_spec_receipt.load_receipt(str(harness.root))
        worker_receipt = harness.worker_observed['run_spec_receipt']
        command_digest = case['inputs']['completion_command_receipt_digest']
        control_spec_digest = _content_spec_digest(control['receipt']['spec'])
        observed_spec_digest = _content_spec_digest(receipt['spec'])
        comparison = {
            'mode': 'exact-control' if exact_rows else 'expected-fallback',
            'cardinality': {'control': len(control['rows']), 'observed': len(rows)},
            'source_text': [row['text'] for row in rows] == [row['text'] for row in control['rows']],
            'order': [row['text'] for row in rows] == [row['text'] for row in control['rows']],
            'fingerprint_equal': receipt['fingerprint'] == control['fingerprint'],
            'chunk_count_equal': receipt['chunk_count'] == control['chunk_count'],
            'location_independent_spec_equal': observed_spec_digest == control_spec_digest,
            'same_fixture_worker_receipt_digest_equal': (
                receipt['sha256'] == worker_receipt['sha256'] == command_digest),
            'same_fixture_receipt_digest_unchanged_since_failure': (
                receipt['sha256'] == case['observed']['receipt_digest']),
            'manifest_status': manifest.get('status'),
            'checkpoint_stage': checkpoint['stage'],
        }
        self.assertEqual(len(control['rows']), len(rows))
        self.assertEqual([row['text'] for row in control['rows']], [row['text'] for row in rows])
        self.assertEqual(control['fingerprint'], receipt['fingerprint'])
        self.assertEqual(control['chunk_count'], receipt['chunk_count'])
        self.assertTrue(comparison['location_independent_spec_equal'])
        self.assertTrue(comparison['same_fixture_worker_receipt_digest_equal'])
        self.assertFalse(comparison['same_fixture_receipt_digest_unchanged_since_failure'])
        self.assertEqual('complete', manifest.get('status'))
        if exact_rows:
            comparison['rows_equal'] = rows == control['rows']
            comparison['speakers_equal'] = [r['speaker'] for r in rows] == [r['speaker'] for r in control['rows']]
            comparison['instructions_equal'] = [r['instruct'] for r in rows] == [r['instruct'] for r in control['rows']]
            self.assertEqual(control['rows'], rows)
        else:
            expected_speakers = [row['speaker'] for row in control['rows']] \
                if stage == 'attribute' else None
            if expected_speakers is not None:
                for index in case['inputs'].get('failed_indices') or []:
                    if 0 <= index < len(expected_speakers):
                        expected_speakers[index] = 'UNKNOWN'
            if expected_speakers is not None:
                comparison['expected_speakers'] = expected_speakers
                comparison['speakers_equal'] = [r['speaker'] for r in rows] == expected_speakers
                self.assertEqual(expected_speakers, [r['speaker'] for r in rows])
        case['comparison'] = comparison
        case['expected'] = {
            'final_rows': control['rows'] if exact_rows else 'deterministic fallback',
            'fingerprint': control['fingerprint'], 'chunk_count': control['chunk_count'],
            'location_independent_spec_digest': control_spec_digest,
            'same_fixture_receipt_digest': command_digest, 'manifest_status': 'complete'}
        case['observed'] = {
            'final_rows': rows, 'fingerprint': receipt['fingerprint'],
            'chunk_count': receipt['chunk_count'], 'location_independent_spec_digest': observed_spec_digest,
            'same_fixture_receipt_digest': receipt['sha256'],
            'manifest_status': manifest.get('status'), 'checkpoint_stage': checkpoint['stage']}

    def test_stage_cast_action_full_matrix(self):
        stage_config = _config(force_segment_llm=True)
        for stage in ('segment', 'attribute', 'instruct'):
            source_text = SOURCE if stage == 'segment' else None
            config = stage_config if stage == 'segment' else None
            control = self._control(cast=False, config=config, source_text=source_text)
            control_cast = self._control(cast=True, config=config, source_text=source_text)
            for cast in (False, True):
                reference = control_cast if cast else control
                for action in ('detail', 'inject', 'skip', 'retry', 'resume'):
                    with self.subTest(stage=stage, cast=cast, action=action):
                        name = '%s/%s-cast/%s' % (stage, 'cast' if cast else 'no-cast', action)
                        case = {'case': name, 'inputs': {}, 'order': [], 'expected': {},
                                'observed': {}, 'artifacts': {}, 'comparison': {}}
                        harness, command, receipt_path, digest, receipt, detail = self._failed(
                            case, cast=cast, config=config, source_text=source_text, stage=stage)
                        before = harness.snapshot()
                        command, result = self._apply_action(
                            harness, command, stage, action, detail, receipt_path, digest)
                        after = harness.snapshot()
                        case['inputs']['failed_indices'] = detail.get('batch_indices') or []
                        case['inputs']['action_payload'] = {
                            'stage': stage, 'cast': cast, 'action': action,
                            'inject_entries': (self._inject_entries(stage, detail)
                                               if action == 'inject' else None)}
                        case['observed']['action_result'] = result
                        case['observed']['receipt_preserved'] = (
                            before['receipt'] == after['receipt'])
                        if action != 'retry':
                            self.assertEqual(before['receipt'], after['receipt'])
                        if action == 'detail':
                            self.assertEqual(before, after)
                            harness.finish({'artifacts_unchanged': True},
                                           {'artifacts_unchanged': before == after},
                                           paths=(('receipt_path', receipt_path),))
                            continue
                        if action != 'retry':
                            # Generate issues a new worker command for the exact
                            # manifest settled by the previous worker or action.
                            response = harness.api('post', '/api/generate_script', {})
                            self.assertEqual(200, response.status_code, response.text)
                            command = harness.scheduled[-1][4]
                        self.assertEqual(0, self._finish_rows(harness, command))
                        self._compare(case, harness, reference, stage, action,
                                      exact_rows=(action != 'skip'))
                        harness.finish(case['expected'], case['observed'],
                                       paths=(('receipt_path', receipt_path),))


class SpecIdentityAndGuardMatrix(unittest.TestCase):
    """Worker spec identity and bounded refusal matrices (#2207)."""

    @classmethod
    def tearDownClass(cls):
        _write_artifact()

    def _case(self, name):
        return {'case': name, 'inputs': {}, 'order': [], 'expected': {},
                'observed': {}, 'artifacts': {}, 'comparison': {}}

    def _failed_case(self, name, *, stage='attribute', config=None):
        case = self._case(name)
        harness, command, receipt_path, digest, receipt, detail = self._failed_via(case,
            stage=stage, config=config)
        return case, harness, command, receipt_path, digest, receipt, detail

    def _failed_via(self, case, *, stage='attribute', config=None):
        harness = _Harness(case, config=config)
        self.addCleanup(harness.close)
        command, receipt_path, digest, receipt = harness.admit()
        harness.worker(command, _factory(_payloads(), fail=(stage,)))
        app = FastAPI()
        app.include_router(script.router)
        with TestClient(app) as client:
            detail = client.get('/api/generate_script/recovery/detail').json()
        return harness, command, receipt_path, digest, receipt, detail

    def _protected(self, harness):
        return {name: _digest(path) for name, path in harness.artifact_paths().items()
                if name in ('receipt', 'checkpoint', 'manifest')}

    def _record(self, case, harness, *, expected, observed, receipt_path=None):
        case['expected'] = expected
        case['observed'] = observed
        case['artifacts'] = harness.snapshot()
        if receipt_path:
            case['artifacts']['receipt_path'] = _digest(receipt_path)
        _ARTIFACT['cases'].append(copy.deepcopy(
            {key: value for key, value in case.items() if not key.startswith('_')}))

    def test_worker_uses_admitted_spec_not_live_config(self):
        variants = (
            ('baseline', {}, False),
            ('failover-used', {}, True),
            ('custom-params-prompts-chunk-context',
             {'custom_prompts': True, 'custom_chunk': 1400, 'context': 65536}, False),
        )
        for name, options, failover in variants:
            with self.subTest(variant=name):
                case = self._case('spec-identity/' + name)
                config = _config(**options, failover=failover)
                harness = _Harness(case, config=config)
                self.addCleanup(harness.close)
                command, receipt_path, digest, receipt = harness.admit()
                calls, dispatched = [], []
                transports = []

                def factory(config_value, llm, timeout):
                    from llm_provider import FailoverClient, make_run_client
                    def make_client(profile, *_args, **_kwargs):
                        fake = _factory(_payloads(), calls=calls)()
                        create = fake.chat.completions.create
                        observed = []
                        transports.append(observed)
                        def record(**kw):
                            observed.append({'model': kw.get('model'),
                                             'base_url': profile.get('base_url')})
                            return create(**kw)
                        fake.chat.completions.create = record
                        return fake
                    with patch('llm_provider.make_llm_client', side_effect=make_client):
                        client = make_run_client(config_value, llm, timeout)
                    if failover:
                        assert isinstance(client, FailoverClient)
                        client.failover({'category': 'unavailable', 'status_code': 503})
                    return client

                harness.worker(command, factory)
                observed = harness.worker_observed
                spec = receipt['spec']
                checked = {
                    'receipt_digest': observed['run_spec_receipt']['sha256'] == receipt['sha256'],
                    'model_name': observed['model_name'] == spec['model_name'],
                    'chunk_size': observed['chunk_size'] == spec['chunk_size'],
                }
                for key in ('max_tokens', 'temperature', 'top_p'):
                    if key in spec['params']:
                        checked['params.' + key] = (
                            getattr(observed['params'], key) == spec['params'][key])
                for attr, key in (('segment_system_prompt', 'segment_system'),
                                  ('instruct_system_prompt', 'instruct_system')):
                    if (spec.get('prompts') or {}).get(key) is not None:
                        checked['prompts.' + key] = (
                            getattr(observed['params'], attr) == spec['prompts'][key])
                self.assertTrue(all(checked.values()), checked)
                secondary_calls = transports[1] if len(transports) > 1 else []
                dispatched = [row['model'] for row in secondary_calls]
                self.assertEqual(failover, bool(secondary_calls),
                                 'the secondary transport must carry the requests')
                checkpoint = json.loads(Path(harness.artifact_paths()['checkpoint']).read_text())
                binding = checkpoint.get('fingerprint', {}).get('model_binding') or {}
                case['inputs']['receipt_spec_digest'] = script.three_pass_run_spec.spec_digest(spec)
                case['inputs']['worker_observed'] = {
                    'checked_fields': checked,
                    'model_name': observed['model_name'], 'chunk_size': observed['chunk_size'],
                    'call_order': calls, 'real_transports': transports,
                    'secondary_dispatches': secondary_calls,
                    'checkpoint_binding': binding}
                self._record(case, harness, expected={
                    'all_checked_fields_match': True,
                    'failover_used': failover,
                    'dispatched_secondary': config['llm_remote']['model_name'] if failover else None},
                    observed={
                    'all_checked_fields_match': all(checked.values()),
                    'failover_used': bool(dispatched),
                    'dispatched_secondary': list(dict.fromkeys(dispatched)) or None,
                    'observed_transports': transports,
                    'checkpoint_binding': binding}, receipt_path=receipt_path)

    def test_failover_oracle_rejects_primary_transport(self):
        """Negative control: primary-only dispatch must not look like failover."""
        from llm_provider import FailoverClient, make_run_client
        config = _config(failover=True)
        case = self._case('spec-identity/failover-negative-control')
        harness = _Harness(case, config=config)
        self.addCleanup(harness.close)
        command, receipt_path, digest, receipt = harness.admit()
        transports = []

        def factory(config_value, llm, timeout):
            def make_client(profile, *_args, **_kwargs):
                fake = _factory(_payloads())()
                observed = []
                transports.append(observed)
                create = fake.chat.completions.create
                def record(**kw):
                    observed.append({'model': kw.get('model'),
                                     'base_url': profile.get('base_url')})
                    return create(**kw)
                fake.chat.completions.create = record
                return fake
            with patch('llm_provider.make_llm_client', side_effect=make_client):
                client = make_run_client(config_value, llm, timeout)
            self.assertIsInstance(client, FailoverClient)
            return client

        harness.worker(command, factory)
        secondary_calls = transports[1] if len(transports) > 1 else []
        self.assertEqual([], secondary_calls,
                         'a primary-only transport must be rejected as failover')

    def test_config_mutation_after_admission_never_changes_worker_spec(self):
        case = self._case('spec-identity/config-mutated-after-admission')
        config = _config(custom_prompts=True, custom_chunk=1400)
        harness = _Harness(case, config=config)
        self.addCleanup(harness.close)
        command, receipt_path, digest, receipt = harness.admit()
        live = json.loads((harness.root / 'config.json').read_text())
        live['generation']['three_pass_chunk_size'] = 9
        live['prompts']['pass1_prompt_presets'][0]['system_prompt'] = 'LIVE-P1'
        atomic_json_write(live, harness.root / 'config.json')
        code = harness.worker(command, _factory(_payloads()))
        self.assertEqual(0, code)
        spec = receipt['spec']
        observed = harness.worker_observed
        self.assertEqual(spec['model_name'], observed['model_name'])
        self.assertEqual(spec['chunk_size'], observed['chunk_size'])
        self.assertEqual(P1_MARK, observed['params'].segment_system_prompt)
        self.assertEqual(receipt['fingerprint'], json.loads(Path(
            three_pass_checkpoint_path(str(harness.script_path))).read_text())['fingerprint'])
        self._record(case, harness, expected={'receipt_model': spec['model_name'],
                                               'receipt_chunk_size': spec['chunk_size']},
                     observed={'worker_model': observed['model_name'],
                               'worker_chunk_size': observed['chunk_size'],
                               'worker_pass1_prompt': observed['params'].segment_system_prompt},
                     receipt_path=receipt_path)

    def _refusal_case(self, name, mutate, *, route='inject', stage='attribute'):
        case = self._case(name)
        harness, command, receipt_path, digest, receipt, detail = self._failed_via(case, stage=stage)
        mutate(harness, receipt_path)
        before = harness.snapshot()
        app = FastAPI()
        app.include_router(script.router)
        with TestClient(app) as client:
            if route == 'recovery/detail':
                response = client.get('/api/generate_script/recovery/detail')
            elif route == 'inject':
                response = client.post('/api/generate_script/inject', json={'entries': [
                    {'n': index, 'speaker': 'MIRA'} for index in detail['batch_indices']]})
            else:
                response = client.post('/api/generate_script/skip', json={})
        after = harness.snapshot()
        preserved = all(before[key] == after[key] for key in
                        ('receipt', 'checkpoint', 'manifest'))
        case['order'].append('%s -> %s' % (route, response.status_code))
        self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertTrue(preserved, 'a bounded refusal changed checkpoint/manifest/receipt bytes')
        self._record(case, harness, expected={'status': '>=400',
                                               'artifacts_preserved': True},
                     observed={'status': response.status_code,
                               'detail': str(response.json())[:300],
                               'artifacts_preserved': preserved},
                     receipt_path=receipt_path)
        return case, harness

    def test_coordinated_checkpoint_manifest_corruption_is_bounded(self):
        def coordinated(harness, receipt_path):
            for name in (three_pass_checkpoint_path(str(harness.script_path)),
                         three_pass_manifest_path(str(harness.script_path))):
                path = Path(name)
                data = json.loads(path.read_text())
                data['fingerprint']['source_sha256'] = 'c' * 64
                path.write_text(json.dumps(data), encoding='utf-8')
        def malformed(harness, receipt_path):
            for name in (three_pass_checkpoint_path(str(harness.script_path)),
                         three_pass_manifest_path(str(harness.script_path))):
                Path(name).write_text('{broken', encoding='utf-8')
        self._refusal_case('corruption/coordinated-checkpoint-manifest', coordinated)
        self._refusal_case('corruption/malformed-json-pair', malformed)

    def test_receipt_mutation_replacement_absence_and_digest_matrix(self):
        def mutate_field(harness, receipt_path):
            data = json.loads(Path(receipt_path).read_text())
            data['spec']['chunk_size'] = 7
            Path(receipt_path).write_text(json.dumps(data), encoding='utf-8')
        def replace(harness, receipt_path):
            other = _Harness(self._case('receipt-donor'), config=_config())
            self.addCleanup(other.close)
            other.admit()
            Path(receipt_path).write_bytes(Path(script.run_spec_receipt.receipt_path(
                str(other.root))).read_bytes())
        def digest(harness, receipt_path):
            data = json.loads(Path(receipt_path).read_text())
            data['sha256'] = 'd' * 64
            Path(receipt_path).write_text(json.dumps(data), encoding='utf-8')
        def duplicate_key(harness, receipt_path):
            raw = Path(receipt_path).read_text()
            Path(receipt_path).write_text(raw[:-1] + ', "schema": 1}', encoding='utf-8')
        for name, mutate in (('mutation', mutate_field), ('replacement', replace),
                             ('digest', digest), ('duplicate-key', duplicate_key)):
            with self.subTest(receipt=name):
                self._refusal_case('receipt/' + name, mutate)
        # A receipt created at admission remains required after file loss.
        case = self._case('receipt/absence')
        harness, command, receipt_path, digest, receipt, detail = self._failed_via(case)
        Path(receipt_path).unlink()
        before = self._protected(harness)
        self.assertEqual(409, harness.api('get', '/api/generate_script/recovery/detail').status_code)
        self.assertEqual(before, self._protected(harness))

    def test_task_fence_change_blocks_recovery_without_mutation(self):
        case = self._case('race/task-ownership')
        harness, command, receipt_path, digest, receipt, detail = self._failed_via(case)
        before = harness.snapshot()
        harness.states['script']['running'] = True
        app = FastAPI()
        app.include_router(script.router)
        with TestClient(app) as client:
            response = client.post('/api/generate_script/inject', json={'entries': [
                {'n': index, 'speaker': 'MIRA'} for index in detail['batch_indices']]})
        harness.states['script']['running'] = False
        after = harness.snapshot()
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(before, after)
        self._record(case, harness, expected={'status': 409, 'artifacts_preserved': True},
                     observed={'status': response.status_code, 'artifacts_preserved': before == after},
                     receipt_path=receipt_path)

    def test_before_replace_races_preserve_all_artifacts(self):
        changes = {
            'book-generation': lambda state: state.__setitem__('book_generation', 'raced'),
            'selection': lambda state: state.__setitem__('script_generation_selection', {'book_token': 'x' * 64}),
            'source-path': lambda state: state.__setitem__('input_file_path', str(Path(state['input_file_path']).parent / 'other.txt')),
            'settings': lambda state: state['script_generation_options'].__setitem__('first_person_narrator', 'RACED'),
            'profile': lambda state: None,
        }
        for name, change in changes.items():
            with self.subTest(race=name):
                case = self._case('race/before-replace/' + name)
                harness, command, receipt_path, digest, receipt, detail = self._failed_via(case)
                state_path = harness.root / 'state.json'
                before = self._protected(harness)
                original = script.apply_book_state_locked
                def racing(data_dir, replacements, removals, **kwargs):
                    state = json.loads(state_path.read_text())
                    change(state)
                    atomic_json_write(state, state_path)
                    if name == 'profile':
                        config = json.loads((harness.root / 'config.json').read_text())
                        config['llm']['model_name'] = 'live-changed'
                        atomic_json_write(config, harness.root / 'config.json')
                    callback = kwargs.get('before_replace')
                    if callback is not None:
                        callback()
                    return original(data_dir, replacements, removals, **kwargs)
                state = json.loads(state_path.read_text())
                snapshot = {'receipt_sha256': receipt['sha256'],
                            'book_generation': state.get('book_generation'),
                            'source_path': state.get('input_file_path'),
                            'selection': state.get('script_generation_selection'),
                            'options': state.get('script_generation_options'),
                            'config_sha256': hashlib.sha256(
                                (harness.root / 'config.json').read_bytes()).hexdigest()}
                with patch.object(script, 'apply_book_state_locked', side_effect=racing):
                    with self.assertRaises(HTTPException) as raised:
                        script.apply_manual_recovery([
                            {'n': index, 'speaker': 'MIRA'} for index in detail['batch_indices']],
                            'manual', admission_snapshot=snapshot)
                after = self._protected(harness)
                self.assertEqual(409, raised.exception.status_code)
                self.assertEqual(before, after)
                self._record(case, harness, expected={'status': 409, 'artifacts_preserved': True},
                             observed={'status': raised.exception.status_code,
                                       'artifacts_preserved': before == after},
                             receipt_path=receipt_path)


if __name__ == '__main__':
    unittest.main()
