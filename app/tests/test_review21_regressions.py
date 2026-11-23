"""Admission agrees with native chunking, SDK requests and Start Over."""
import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import book_state_transaction as books

from tests import test_review15_regressions as recovery_fixture
from tests import test_review20_regressions as prior
from utils import atomic_json_write

gap = prior.gap

_CONTEXT_WORKER = gap._WORKER.replace(
    'failure, boundary = sys.argv[1:3]', 'context = int(sys.argv.pop(1))\nfailure, boundary = sys.argv[1:3]'
).replace(
    "return_value=(None, {}, 'offline')", "return_value=(None, {'context_length': context}, 'offline')"
).replace(
    "if args[2] == failure + '_failed':\n        os._exit(87)",
    "global saved_prefix\n    if args[2] == 'segment' and args[4] == 1:\n        saved_prefix = True"
).replace(
    'pick = fixtures._pick', 'pick = fixtures._pick\nsaved_prefix = False'
).replace(
    "'DANA walked' in text.split('SOURCE TEXT:')[-1]", 'saved_prefix'
)

_STALE_WORKER = gap._WORKER.replace(
    "args[2] == failure + '_failed'", "args[2] == 'instruct' and len(args[6]) > 0"
).replace(
    'with ExitStack() as stack:', """
def earlier(tag, payloads, fail, calls, text=''):
    rows = pick(tag, payloads, fail, calls, text)
    if tag == 'instruct':
        for row in rows:
            row['instruct'] = 'Quiet and wary.'
    return rows

fixtures._pick = earlier
with ExitStack() as stack:"""
)


class Review21Regressions(unittest.TestCase):
    def _case(self):
        suite = prior.Review20Regressions()
        self.addCleanup(suite.doCleanups)
        return suite, suite._case()

    def _child(self, case, source, arguments, expected):
        child = gap.prior.Review12Regressions()._child(case.h, source, arguments)
        logs = child.stderr + child.stdout
        self.assertEqual(expected, child.returncode, logs)
        return logs

    def test_modern_loaded_context_pins_chunk_plan_across_both_resume_routes(self):
        source_text = (gap.MULTI_SOURCE + '\n\n') * 24

        def start_context_case():
            case = recovery_fixture.Review15Regressions()
            self.addCleanup(case.doCleanups)
            case._harness(source_text)
            case.h.config['generation']['three_pass_chunk_size'] = 40000
            case.h.config['llm']['context_length'] = 32768
            atomic_json_write(case.h.config, case.h.root / 'config.json')
            return case

        def child(case, command, params, expected):
            return self._child(case, _CONTEXT_WORKER, [*params, *command[3:]], expected)

        for route in ('control', '', 'retry'):
            with self.subTest(route=route or 'generate'):
                case = start_context_case()
                with patch.object(gap.script, 'get_planned_request_context',
                                  return_value={'known': True, 'context': 4096, 'parallel': 1}) as planned:
                    command = case._start()
                    mounted = gap.script.run_spec_receipt.load_receipt(case.h.root)
                    spec = mounted['spec']
                    if route == 'control':
                        child(case, command, ('4096', '', 'settled'), 0)
                        control_rows = case.h.rows()
                        control_bytes = case.h.script_path.read_bytes()
                        self.assertEqual(480, len(control_rows))
                        control_plan = spec['chunk_digests']
                        continue

                    child(case, command, ('4096', 'segment', 'after-prefix'), 1)
                    checkpoint = gap.script.get_current_generation_checkpoint()
                    self.assertEqual(('segment_failed', 1, 16), (
                        checkpoint['stage'], checkpoint['chunks_done'], checkpoint['failed']['chunks_total']))
                    self.assertEqual((32768, 4096, 16), (
                        spec['llm_profile']['context_length'], spec['params']['context_length'],
                        mounted['chunk_count']))
                    self.assertEqual(control_plan, spec['chunk_digests'])
                    self.assertEqual(16, len(control_plan))
                    self.assertIsNone(case.h.rows())

                    planned.return_value = {'known': True, 'context': 3072, 'parallel': 1}
                    prior.Review20Regressions()._assert_http_refused(case)
                    planned.return_value = {'known': True, 'context': 8192, 'parallel': 1}
                    command = case._start(route)
                rebound = gap.script.run_spec_receipt.load_receipt(case.h.root)
                self.assertEqual((4096, 16, control_plan), (
                    rebound['spec']['params']['context_length'], rebound['chunk_count'],
                    rebound['spec']['chunk_digests']))
                before = case.native._snapshot()
                process = copy.deepcopy(gap.core.process_state)
                logs = child(case, command, ('3072', '', 'settled'), 1)
                self.assertIn('The loaded context cannot serve the admitted chunk plan', logs)
                self.assertNotIn('asking the model', logs)
                self.assertEqual(before, case.native._snapshot())
                self.assertEqual(process, gap.core.process_state)
                self.assertIsNone(case.h.rows())
                logs = child(case, command, ('8192', '', 'settled'), 0)
                self.assertIn('1/16 chunks already split', logs)
                self.assertEqual(control_rows, case.h.rows())
                self.assertEqual(control_bytes, case.h.script_path.read_bytes())
                self.assertEqual('complete', json.loads(case.manifest.read_bytes())['status'])

    def test_sdk_binds_both_profiles_limits_and_routing_headers(self):
        for failover in (False, True):
            with self.subTest(failover=failover):
                self._exercise_wire(failover)

    def _exercise_wire(self, failover):
        suite, (oracle, case) = self._case()
        requests = []
        outage = threading.Event()
        routing = {'matrix-primary': uuid4().hex, 'matrix-secondary': uuid4().hex}
        drifted_header = uuid4().hex
        authorization = uuid4().hex

        class Gateway(prior.FramedHttpFixtureHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                model = body['model']
                variant_matches = self.headers.get('X-Model-Variant') == routing[model]
                requests.append((model, body['max_tokens'], variant_matches))
                if model == 'matrix-primary' and outage.is_set():
                    status, payload = 503, {'error': {'message': 'synthetic outage'}}
                else:
                    raw = prior.fixtures._factory(prior.fixtures._payloads())().chat.completions.create(
                        **body).choices[0].message.content
                    if not variant_matches:
                        raw = raw.replace('Calm and measured.', 'Warm and cheerful.')
                    status, payload = 200, {
                        'id': 'synthetic', 'object': 'chat.completion', 'created': 0, 'model': model,
                        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': raw},
                                     'finish_reason': 'stop'}],
                    }
                self.send_fixture_response(
                    status, json.dumps(payload).encode(), [('Content-Type', 'application/json')])

        server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        config = case.h.config
        primary = config['llm']
        primary.update(base_url=f'http://127.0.0.1:{server.server_port}/v1', max_output_tokens=2048,
                       provider_headers={'X-Model-Variant': routing['matrix-primary'],
                                         'Authorization': f'Bearer {authorization}'})
        profile = 'llm_remote' if failover else 'llm'
        if failover:
            config['llm_failover'] = True
            secondary = copy.deepcopy(primary)
            secondary.update(model_name='matrix-secondary')
            secondary['provider_headers']['X-Model-Variant'] = routing['matrix-secondary']
            config[profile] = secondary
        config_path = case.h.root / 'config.json'
        atomic_json_write(config, config_path)
        command = case._start_over()
        self._child(case, prior.wire._RUN_WORKER, ['segment', *command[3:]], 87)
        self.assertEqual([('matrix-primary', 512, True)], requests)

        drift_cases = [copy.deepcopy(config), copy.deepcopy(config)]
        drift_cases[0][profile]['max_output_tokens'] = 768
        drift_cases[1][profile]['provider_headers']['X-Model-Variant'] = drifted_header
        for changed in drift_cases:
            atomic_json_write(changed, config_path)
            suite._assert_http_refused(case)
            self.assertEqual(1, len(requests))
        atomic_json_write(config, config_path)
        command = case._start('retry')
        if failover:
            outage.set()
        for changed in drift_cases:
            atomic_json_write(changed, config_path)
            before = case.native._snapshot()
            process = copy.deepcopy(gap.core.process_state)
            logs = self._child(case, prior.wire._RUN_WORKER, ['finish', *command[3:]], 1)
            self.assertIn('Provider request settings changed after admission', logs)
            self.assertEqual(1, len(requests))
            self.assertEqual(before, case.native._snapshot())
            self.assertEqual(process, gap.core.process_state)
            self.assertIsNone(case.h.rows())
            for value in (*routing.values(), authorization, drifted_header):
                self.assertNotIn(value, logs)
        atomic_json_write(config, config_path)
        self._child(case, prior.wire._RUN_WORKER, ['finish', *command[3:]], 0)
        model = config[profile]['model_name']
        expected = [512] * 3 + [2048] + [1024] * 4
        self.assertEqual([(model, cap, True) for cap in expected],
                         [item for item in requests[1:] if item[0] == model])
        self.assertTrue(all(match for _, _, match in requests))
        oracle._assert_complete(case)
        for label, filename in case.h.artifact_paths().items():
            if label == 'script':
                continue
            data = Path(filename).read_bytes()
            for value in (*routing.values(), authorization, drifted_header):
                self.assertNotIn(value.encode(), data)

    def test_start_over_rejects_genuine_older_shards_and_retry_keeps_current_progress(self):
        suite, (oracle, case) = self._case()
        original = case._start_over()
        self._child(case, _STALE_WORKER, ['', 'before-manifest', *original[3:]], 87)
        old_receipt = gap.script.run_spec_receipt.load_receipt(case.h.root)
        old_progress = gap.script.get_current_generation_checkpoint()
        checkpoint_path = Path(case.h.artifact_paths()['checkpoint'])
        old_shards = list(case.h.root.glob(checkpoint_path.name + '.parts-*/*.json'))
        self.assertTrue(old_shards, 'fixture must restore genuine native shard data')
        restored = {path: path.read_bytes() for path in (checkpoint_path, *old_shards)}
        directions = [row['instruct'] for row in old_progress['annotated'] if row is not None]
        self.assertEqual(['Quiet and wary.'] * 4, directions)

        retry = case._start('retry')
        rebound = gap.script.run_spec_receipt.load_receipt(case.h.root)
        self.assertNotEqual(old_receipt['run_id'], rebound['run_id'])
        self.assertEqual(old_receipt.get('checkpoint_generation'), rebound.get('checkpoint_generation'))
        self.assertIn("4/4 chunks already split, stage 'instruct'", case._worker(retry))
        prior_rows = case.h.rows()
        self.assertEqual(4, sum(row['instruct'] == 'Quiet and wary.' for row in prior_rows))

        replacement = case._start_over()
        new_receipt = gap.script.run_spec_receipt.load_receipt(case.h.root)
        seed = checkpoint_path.read_bytes()
        marker = case.manifest.read_bytes()
        sealed = case.receipt.read_bytes()
        # Install the old shards after the replacement worker has accepted the
        # verified seed. Before handoff, admission handles the pending journal.
        self.assertEqual(new_receipt['run_id'], replacement[replacement.index('--active-run-id') + 1])
        self.assertTrue((case.h.root / books.DISPATCH_PROOF).is_file())
        books.finish_book_worker_handoff(case.h.root, new_receipt['run_id'])
        self.assertFalse((case.h.root / books.DISPATCH_PROOF).exists())
        for path, data in restored.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.assertEqual(old_progress, gap.script.get_current_generation_checkpoint())
        self.assertEqual(marker, case.manifest.read_bytes())
        self.assertEqual(sealed, case.receipt.read_bytes())

        suite._assert_http_refused(case)
        before = case.native._snapshot()
        state = copy.deepcopy(gap.core.process_state)
        logs = self._child(case, gap._WORKER, ['', 'settled', *replacement[3:]], 1)
        self.assertIn('Saved progress cannot be verified against the admitted run spec', logs)
        self.assertNotIn('asking the model', logs)
        self.assertEqual(before, case.native._snapshot())
        self.assertEqual(state, gap.core.process_state)
        self.assertEqual(prior_rows, case.h.rows())
        self.assertNotEqual(old_receipt['checkpoint_generation'], new_receipt['checkpoint_generation'])

        checkpoint_path.write_bytes(seed)
        for path in old_shards:
            path.unlink()
        for directory in {path.parent for path in old_shards}:
            directory.rmdir()
        logs = case._worker(replacement)
        self.assertIn('chunk 1 of 4 - asking the model', logs)
        oracle._assert_complete(case)
