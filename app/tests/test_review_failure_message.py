"""Fatal review errors reach task status, not just the CLI traceback (issue #2092).

The worker emits one ASCII marker line carrying the first line of any fatal
error and, only for the misaligned-resume refusal, a machine-readable kind.
core.run_process redacts, bounds and stores both on the task state; the status
route returns them to the SPA, which shows the message as text and offers Start
over only when the kind says the checkpoint is misaligned.
"""
import errno
import io
import json
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core
import review_script
from routers import script

APP = Path(__file__).resolve().parent.parent


def _resume_refusal_error():
    """Use the real fail-loud refusal, not a copy that can drift."""
    windows = [(0, 2), (2, 4)]
    entries = [{'speaker': 'N', 'text': 'Synthetic sentence.'}] * 4
    try:
        review_script.get_review_remaining_windows(windows, entries, 1)
    except review_script.ReviewResumeMisaligned as error:
        return error
    raise AssertionError('misaligned resume was not refused')


def _resume_refusal_message():
    return str(_resume_refusal_error())


def _emit(error_or_message):
    out = io.StringIO()
    with redirect_stdout(out):
        review_script.emit_review_failure(error_or_message)
    return out.getvalue().strip()


def _run_process_with(runs, state):
    def stream(command, cwd, current, **kwargs):
        code, lines = runs.pop(0)
        current['logs'].extend(lines)
        return code, lines

    stack = ExitStack()
    stack.enter_context(patch.object(core, 'process_state', {'review': state['review']}))
    stack.enter_context(patch.object(core, '_init_task_log', return_value=None))
    stack.enter_context(patch.object(core, '_stream_subprocess_to_logs', side_effect=stream))
    return stack


class ReviewFailureMarkerTests(unittest.TestCase):
    def test_marker_round_trips_non_ascii_text_and_redacts_secrets(self):
        message = (f'レビュー \u2770 checkpoint /home/fakemitch/.pinokio '
                   f'api_key=secret-token123; unrelated failure')
        line = _emit(message)
        self.assertTrue(line.startswith('REVIEW_FAILURE_JSON: '), line)
        line.encode('ascii', 'strict')  # safe on a cp1252 Windows pipe
        parsed = core._extract_review_failure([line])
        self.assertIn('\u30ec\u30d3\u30e5\u30fc \u2770', parsed['message'])
        self.assertNotIn('/home/fakemitch', parsed['message'])
        self.assertNotIn('secret-token123', parsed['message'])
        self.assertIsNone(parsed['kind'], 'unrelated failures must not offer Start over')
        self.assertNotIn('kind', json.loads(line.split(': ', 1)[1]))

    def test_only_the_resume_refusal_carries_a_kind(self):
        parsed = core._extract_review_failure([_emit(_resume_refusal_error())])
        self.assertEqual(review_script.RESUME_MISALIGNED_KIND, parsed['kind'])
        self.assertEqual(_resume_refusal_message(), parsed['message'])

    def test_fatal_cli_path_emits_the_marker_and_reraises(self):
        block = (APP / 'review_script.py').read_text(encoding='utf-8')
        block = block[block.index('if __name__ == "__main__":'):]
        refusal = _resume_refusal_error()

        def failing_main():
            raise refusal

        events = []
        namespace = {
            '__name__': '__main__',
            'startup_event': lambda name, **kw: events.append(name),
            'main': failing_main,
            'emit_review_failure': review_script.emit_review_failure,
        }
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(review_script.ReviewResumeMisaligned):
                exec(compile(block, 'review_script.py', 'exec'), namespace)
        self.assertIn('cli_imports_complete', events)
        parsed = core._extract_review_failure(out.getvalue().splitlines())
        self.assertEqual(_resume_refusal_message(), parsed['message'])
        self.assertEqual(review_script.RESUME_MISALIGNED_KIND, parsed['kind'])

    def test_malformed_marker_is_ignored(self):
        self.assertIsNone(core._extract_review_failure(['REVIEW_FAILURE_JSON: not json']))
        self.assertIsNone(core._extract_review_failure(['REVIEW_FAILURE_JSON: {"message": ""}']))
        self.assertIsNone(core._extract_review_failure([]))

    def test_long_first_line_is_bounded_after_redaction(self):
        line = _emit('x' * 2000)
        message = core._extract_review_failure([line])['message']
        self.assertEqual(core._MAX_REVIEW_FAILURE_MESSAGE_CHARS, len(message))
        self.assertTrue(message.endswith('\u2026'), 'a truncated message must say so')

    def test_js_start_over_decision_uses_the_kind_literal_from_python(self):
        source = (APP / 'static/js/app-core.js').read_text(encoding='utf-8')
        self.assertIn("status?.failure_kind === '" + review_script.RESUME_MISALIGNED_KIND + "'", source)
        self.assertNotIn('/start the review over/i', source)


class ReviewFailureStatusTests(unittest.TestCase):
    def test_run_process_stores_message_and_kind_and_clears_both(self):
        state = {'review': {'status': 'idle', 'running': False, 'logs': [], 'cancel': False}}
        runs = [
            (1, [_emit(_resume_refusal_error()), 'Task review failed with return code 1.']),
            (0, ['Task review completed successfully.']),
        ]
        with _run_process_with(runs, state):
            self.assertEqual(1, core.run_process(['synthetic-review'], 'review'))
            self.assertEqual('failed', state['review']['status'])
            self.assertEqual(_resume_refusal_message(), state['review']['failure_message'])
            self.assertEqual(review_script.RESUME_MISALIGNED_KIND,
                             state['review']['failure_kind'])
            core.run_process(['synthetic-review'], 'review')
        self.assertNotIn('failure_message', state['review'])
        self.assertNotIn('failure_kind', state['review'],
                         'a later non-fatal run must not keep a stale kind')

    def test_status_route_returns_both_fields(self):
        api = FastAPI()
        api.include_router(script.router)
        state = {'review': {'running': False, 'status': 'failed', 'logs': [],
                            'failure_message': _resume_refusal_message(),
                            'failure_kind': review_script.RESUME_MISALIGNED_KIND}}
        with patch.object(script, 'process_state', state), TestClient(api) as client:
            response = client.get('/api/status/review')
        self.assertEqual(200, response.status_code)
        self.assertEqual(_resume_refusal_message(), response.json()['failure_message'])
        self.assertEqual(review_script.RESUME_MISALIGNED_KIND, response.json()['failure_kind'])

    def test_resume_refusal_reaches_status_through_the_real_route(self):
        streamed = [(1, [_emit(_resume_refusal_error()),
                         'Task review failed with return code 1.'])]
        with self._app_with_fake_worker(streamed) as client:
            response = client.post('/api/review_script', json={'dedupe_speakers': False})
            self.assertEqual(200, response.status_code, response.text)
            status = client.get('/api/status/review')
        self.assertEqual(200, status.status_code)
        self.assertEqual(_resume_refusal_message(), status.json()['failure_message'])
        self.assertEqual(review_script.RESUME_MISALIGNED_KIND, status.json()['failure_kind'])

    def test_exdev_write_refusal_reaches_status_through_the_marker(self):
        """The #2094 write failures need no special case to reach the UI."""
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'book.json')
            with ExitStack() as stack:
                stack.enter_context(patch.object(
                    review_script, 'atomic_json_write',
                    side_effect=OSError(errno.EXDEV, 'cross-device staged JSON publish')))
                stack.enter_context(patch.object(review_script, 'record_failure'))
                with self.assertRaises(OSError) as refused:
                    review_script.save_checkpoint(
                        output, 1, 2, 10, 5, [{'text': 'synthetic progress'}],
                        {'entries_changed': 1}, None, [1], [], 'source-hash', 'output-hash')
            line = _emit(refused.exception)
            message = core._extract_review_failure([line])
            self.assertIn('could not be written atomically', message['message'])
            self.assertIsNone(message['kind'], 'a write failure is not a resume mismatch')

            state = {'review': {'status': 'idle', 'running': False, 'logs': [], 'cancel': False}}
            runs = [(1, [line, 'Task review failed with return code 1.'])]
            with _run_process_with(runs, state):
                core.run_process(['synthetic-review'], 'review')
            self.assertEqual(message['message'], state['review']['failure_message'])
            self.assertNotIn('failure_kind', state['review'])

    @staticmethod
    @contextmanager
    def _app_with_fake_worker(results):
        """Copy of the established review-route harness, with the real runner."""
        with ExitStack() as stack:
            yield ReviewFailureStatusTests._build_app_client(stack, results)

    @staticmethod
    def _build_app_client(stack, results):
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        source = Path(tmp) / 'annotated_script.json'
        config = Path(tmp) / 'config.json'
        entries = [{'speaker': 'N', 'text': 'Synthetic sentence.'},
                   {'speaker': 'N', 'text': 'Another synthetic sentence.'}]
        source.write_text(json.dumps(entries), encoding='utf-8')
        config.write_text(json.dumps({'llm_mode': 'local',
                                      'generation': {'review_batch_size': 25}}), encoding='utf-8')
        scripts_dir = Path(tmp) / 'scripts'
        scripts_dir.mkdir()
        (scripts_dir / 'book.json').write_text(json.dumps(entries), encoding='utf-8')
        state = {name: {**dict(row), 'running': False}
                 for name, row in core.process_state.items()}
        api = FastAPI()
        api.include_router(script.router)
        for module in (core, script):
            stack.enter_context(patch.object(module, 'process_state', state))
        stack.enter_context(patch.object(core, 'acquire_gpu_lock', return_value=None))
        stack.enter_context(patch.object(core, '_gpu_leases', {}))
        stack.enter_context(patch.object(core, '_task_claims', {}))
        stack.enter_context(patch.object(core, '_saved_book_publications', {}))
        stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=False))
        stack.enter_context(patch.object(core, '_init_task_log', return_value=None))

        def stream(command, cwd, current, **kwargs):
            code, lines = results.pop(0)
            current['logs'].extend(lines)
            return code, lines

        stack.enter_context(patch.object(core, '_stream_subprocess_to_logs', side_effect=stream))
        stack.enter_context(patch.object(script, 'SCRIPT_PATH', str(source)))
        stack.enter_context(patch.object(script, 'CONFIG_PATH', str(config)))
        stack.enter_context(patch.object(script, 'DATA_DIR', tmp))
        stack.enter_context(patch.object(script, 'SCRIPTS_DIR', str(scripts_dir)))
        stack.enter_context(patch.object(script, 'CHARACTER_ALIASES_PATH', str(Path(tmp) / 'aliases.json')))
        stack.enter_context(patch.object(script, '_init_task_log', return_value=None))
        return stack.enter_context(TestClient(api))


if __name__ == '__main__':
    unittest.main()
