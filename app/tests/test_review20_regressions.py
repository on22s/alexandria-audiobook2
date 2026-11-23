"""Native legacy identity, provider overrides and manifest startup authority."""
import copy
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

from tests import test_review13_regressions as wire
from tests import test_review19_regressions as prior
from tests.test_support import FramedHttpFixtureHandler
from utils import atomic_json_write

gap = prior.gap
fixtures = gap.fixtures

# Literal prompts mention instructions in pass one. Classify by the source
# argument before interpreting the surrounding prose.
_LEGACY_TRANSPORT = """
import inspect
source = inspect.getsource(fixtures._factory).replace(
    "if P3_MARK in text or",
    "if 'SOURCE TEXT:' in messages[-1]['content']:\\n"
    "            tag = 'segment'\\n"
    "        elif P3_MARK in text or")
exec(source, vars(fixtures))
"""
_LEGACY_OPTIONS = prior._PREFIX_FAILURE.replace(
    'with ExitStack() as stack:', _LEGACY_TRANSPORT + '\nwith ExitStack() as stack:')


class Review20Regressions(unittest.TestCase):
    def _case(self):
        suite = prior.Review19Regressions()
        self.addCleanup(suite.doCleanups)
        return suite, suite._case()

    def _assert_http_refused(self, case, payload=None):
        before = case.native._snapshot()
        process = copy.deepcopy(gap.core.process_state)
        starts = case.dispatched.call_count
        for route in ('', 'retry'):
            response = case._post(route, payload if not route else None)
            self.assertEqual(409, response.status_code, (route, response.text))
            self.assertEqual(before, case.native._snapshot())
            self.assertEqual(process, gap.core.process_state)
            self.assertEqual(starts, case.dispatched.call_count)

    def _native(self, h, command, *, context, fail=False, expect=0):
        status = (None, {'context_length': context}, 'offline')
        source = _LEGACY_OPTIONS.replace("return_value=(None, {}, 'offline')",
                                         f"return_value={status!r}")
        child = gap.prior.Review12Regressions()._child(
            h, source, ['segment' if fail else '', 'after-prefix' if fail else 'settled',
                             *command[3:]])
        self.assertEqual(expect, child.returncode, child.stdout + child.stderr)
        return child.stdout + child.stderr

    def _legacy_command(self, h, options):
        h.state['script_generation_options'].update(options)
        command = h.admit_legacy()
        if options.get('literal_source_preserving'):
            command.append('--literal-source-preserving')
        return command

    def test_genuine_legacy_options_resume_both_routes_and_refuse_changed_settings(self):
        variants = (
            ('attribute target', {'three_pass_attribute_target_chars': 80}, {}, 32768),
            ('instruction target', {'three_pass_instruct_target_chars': 80}, {}, 32768),
            ('literal', {}, {'literal_source_preserving': True}, 32768),
            ('narrator', {}, {'first_person_narrator': 'MIRA'}, 32768),
            ('detected context', {}, {}, 16384),
            ('instruction context', {'three_pass_instruct_context_chars': 20}, {}, 32768),
        )
        for name, generation, options, context in variants:
            config = fixtures._config(force_segment_llm=True, custom_chunk=500)
            config['generation'].update(generation)
            control = fixtures._Harness({'case': 'legacy-control'}, config=config,
                                        source_text=gap.MULTI_SOURCE)
            try:
                self._native(control, self._legacy_command(control, options), context=context)
                rows = control.rows()
                self.assertEqual(20, len(rows))
            finally:
                control.close()
            for route in ('', 'retry'):
                with self.subTest(setting=name, route=route or 'generate'):
                    suite = prior.Review19Regressions()
                    try:
                        case = suite._case()
                        config_path = case.h.root / 'config.json'
                        case.h.config = copy.deepcopy(config)
                        atomic_json_write(case.h.config, config_path)
                        legacy = self._legacy_command(case.h, options)
                        self._native(case.h, legacy, context=context, fail=True, expect=1)
                        checkpoint = gap.script.get_current_generation_checkpoint()
                        self.assertEqual(('segment_failed', 1), (checkpoint['stage'], checkpoint['chunks_done']))
                        self.assertFalse(case.receipt.exists())

                        with patch.object(gap.script, 'get_planned_request_context',
                                          return_value={'known': True, 'context': context}) as planned:
                            # Change the specific setting that made this book's
                            # legacy identity distinct, while retaining its source.
                            if generation:
                                changed = copy.deepcopy(case.h.config)
                                changed['generation'].update(dict.fromkeys(generation, 0))
                                atomic_json_write(changed, config_path)
                            elif options:
                                state_path = case.h.root / 'state.json'
                                state_bytes = state_path.read_bytes()
                                state = json.loads(state_bytes)
                                for key in options:
                                    state['script_generation_options'].pop(key)
                                atomic_json_write(state, state_path)
                            else:
                                planned.return_value = {'known': True, 'context': 8192}
                            self._assert_http_refused(case)
                            if generation:
                                atomic_json_write(case.h.config, config_path)
                            elif options:
                                state_path.write_bytes(state_bytes)
                            else:
                                planned.return_value = {'known': True, 'context': context}
                            response = case._post(route, options if not route else None)
                            self.assertEqual(200, response.status_code, response.text)
                            self.assertEqual(1, case.dispatched.call_count)
                        command = case.dispatched.call_args.args[0]
                        receipt = gap.script.run_spec_receipt.load_receipt(case.h.root)
                        self.assertEqual(context, receipt['spec']['params']['context_length'])
                        self.assertEqual(checkpoint['fingerprint'], receipt['fingerprint'])
                        if context == 16384:
                            before = case.native._snapshot()
                            self.assertIn('The native recovery context changed',
                                          self._native(case.h, command, context=8192, expect=1))
                            self.assertEqual(before, case.native._snapshot())
                            self.assertIsNone(case.h.rows())
                        logs = self._native(case.h, command, context=context)
                        self.assertIn("1/4 chunks already split, stage 'segment_failed'", logs)
                        self.assertEqual(rows, case.h.rows())
                    finally:
                        suite.doCleanups()

    def test_provider_drift_before_and_after_admission_cannot_reach_the_wire(self):
        suite, case = self._case()
        requests = []

        class Provider(FramedHttpFixtureHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body['chat_template_kwargs']['enable_thinking'])
                content = fixtures._factory(fixtures._payloads())().chat.completions.create(
                    **body).choices[0].message.content
                payload = {'id': 'synthetic', 'object': 'chat.completion', 'created': 0,
                           'model': body['model'], 'choices': [{
                               'index': 0, 'message': {'role': 'assistant', 'content': content},
                               'finish_reason': 'stop'}]}
                self.send_fixture_response(
                    200, json.dumps(payload).encode(), [('Content-Type', 'application/json')])

        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        endpoint = f'http://127.0.0.1:{server.server_port}/v1'
        case.h.config['llm'].update(
            base_url=endpoint, provider_extra_body={
                'chat_template_kwargs': {'enable_thinking': False, 'fixture': 'SYNTH_PROVIDER_SECRET'}})
        case.h.config['llm_remote'] = dict(copy.deepcopy(case.h.config['llm']),
                                           model_name='matrix-secondary')
        case.h.config['llm_failover'] = True
        config_path = case.h.root / 'config.json'
        atomic_json_write(case.h.config, config_path)
        initial = case._start_over()
        child = gap.prior.Review12Regressions()._child(case.h, wire._RUN_WORKER, ['segment', *initial[3:]])
        self.assertEqual(87, child.returncode, child.stdout + child.stderr)
        checkpoint = gap.script.get_current_generation_checkpoint()
        self.assertEqual(('segment', 1), (checkpoint['stage'], checkpoint['chunks_done']))
        self.assertEqual([False], requests)
        for name in ('llm', 'llm_remote'):
            with self.subTest(profile=name, boundary='admission'):
                drift = copy.deepcopy(case.h.config)
                drift[name]['provider_extra_body']['chat_template_kwargs']['enable_thinking'] = True
                atomic_json_write(drift, config_path)
                self._assert_http_refused(case)
                self.assertEqual([False], requests)
        atomic_json_write(case.h.config, config_path)
        command = case._start('retry')
        self.assertNotIn(b'SYNTH_PROVIDER_SECRET', case.receipt.read_bytes())
        for name in ('llm', 'llm_remote'):
            with self.subTest(profile=name, boundary='worker'):
                drift = copy.deepcopy(case.h.config)
                drift[name]['provider_extra_body']['chat_template_kwargs']['enable_thinking'] = True
                atomic_json_write(drift, config_path)
                before = case.native._snapshot()
                process = copy.deepcopy(gap.core.process_state)
                child = gap.prior.Review12Regressions()._child(
                    case.h, wire._RUN_WORKER, ['finish', *command[3:]])
                logs = child.stdout + child.stderr
                self.assertEqual(1, child.returncode, logs)
                self.assertIn('Provider request settings changed after admission', logs)
                self.assertNotIn('SYNTH_PROVIDER_SECRET', logs)
                self.assertEqual([False], requests)
                self.assertEqual(before, case.native._snapshot())
                self.assertEqual(process, gap.core.process_state)
                self.assertIsNone(case.h.rows())
        atomic_json_write(case.h.config, config_path)
        child = gap.prior.Review12Regressions()._child(case.h, wire._RUN_WORKER, ['finish', *command[3:]])
        self.assertEqual(0, child.returncode, child.stderr + child.stdout)
        self.assertEqual([False] * 9, requests)
        suite._assert_complete(case)

    def test_worker_refuses_foreign_manifest_after_absent_or_marker_admission(self):
        foreign_suite = prior.Review19Regressions()
        try:
            donor = foreign_suite._case()
            foreign_suite._fail_after_prefix(donor, donor._start())
            foreign = donor.manifest.read_bytes()
        finally:
            foreign_suite.doCleanups()
        for marker in (False, True):
            for route in ('', 'retry'):
                with self.subTest(marker=marker, route=route or 'generate'):
                    suite = prior.Review19Regressions()
                    try:
                        case = suite._case()
                        initial = case._start_over() if marker else case._start()
                        case._worker(initial, failure='segment', boundary='after-prefix', exit_code=87)
                        original = case.manifest.read_bytes() if marker else None
                        self.assertEqual(marker, case.manifest.exists())
                        case.manifest.write_bytes(foreign)
                        self._assert_http_refused(case)
                        case.manifest.unlink()
                        if marker:
                            self._assert_http_refused(case)
                            case.manifest.write_bytes(original)
                        command = case._start(route)
                        successor = case.manifest.read_bytes() if marker else None
                        cases = [foreign]
                        if marker:
                            cases.extend((json.dumps(json.loads(successor)).encode(), None))
                        for variant in cases:
                            if variant is None:
                                case.manifest.unlink()
                            else:
                                case.manifest.write_bytes(variant)
                            before = case.native._snapshot()
                            process = copy.deepcopy(gap.core.process_state)
                            child = gap.prior.Review12Regressions()._child(
                                case.h, gap._WORKER, ['', 'settled', *command[3:]])
                            self.assertEqual(1, child.returncode, child.stderr + child.stdout)
                            self.assertIn('The admitted startup manifest is missing or changed',
                                          child.stdout + child.stderr)
                            self.assertEqual(before, case.native._snapshot())
                            self.assertEqual(process, gap.core.process_state)
                            self.assertIsNone(case.h.rows())
                        if marker:
                            case.manifest.write_bytes(successor)
                        else:
                            case.manifest.unlink()
                        case._worker(command)
                        suite._assert_complete(case)
                    finally:
                        suite.doCleanups()
