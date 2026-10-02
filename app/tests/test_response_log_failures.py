"""Local response-log failures retain responses without provider retries."""
import contextlib
import errno
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from openai import OpenAI
import generate_script as gs
import llm_provider as provider
from tests.test_failover_runtime_profile import RuntimeTransport


class ResponseLogFailureTests(unittest.TestCase):
    def run_case(self, failure, malformed=False):
        class CompletionTransport(RuntimeTransport):
            def handle_request(self, request):
                response = super().handle_request(request)
                if malformed:
                    data = response.json()
                    data['choices'][0]['message']['content'] = 'Not a JSON response.'
                    return httpx.Response(200, json=data)
                return response
        primary, secondary = CompletionTransport(8192), RuntimeTransport(8192)
        sdks = [OpenAI(base_url='http://fixture.invalid/v1', api_key='fixture', max_retries=0,
                       http_client=httpx.Client(transport=transport)) for transport in (primary, secondary)]
        for sdk in sdks:
            self.addCleanup(sdk.close)
        client = provider.FailoverClient(provider.ConfiguredOpenAI(sdks[0], {}), 'primary',
                                        provider.ConfiguredOpenAI(sdks[1], {}), 'secondary')
        source = [{'speaker': 'ALICE', 'text': 'Alice waited quietly in the room.', 'instruct': ''}]
        params = gs.LLMGenParams(system_prompt='Return entries.', user_prompt_template='',
                                context_length=8192, max_tokens=400, api_retry_limit=2,
                                on_api_exhaustion='pause')
        output = io.StringIO()
        attempts = []
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            path = Path(tmp) / 'responses.log'
            stack.enter_context(patch.object(gs, 'get_response_log_path', return_value=str(path)))
            if failure == 'open':
                path.mkdir()  # Actual IsADirectoryError from the native open call.
            elif failure == 'directory':
                stack.enter_context(patch.object(gs, 'get_response_log_path',
                    side_effect=OSError(errno.ENOSPC, 'fixture directory creation disk full')))
            elif failure == 'write':
                native_open = open
                @contextlib.contextmanager
                def failed_append(*args, **kwargs):
                    with native_open(*args, **kwargs) as handle:
                        class FailingLog:
                            def write(self, text):
                                handle.write(text[:4])
                                handle.flush()
                                raise OSError(errno.ENOSPC, 'fixture append disk full')
                        yield FailingLog()
                stack.enter_context(patch.object(gs, 'open', failed_append, create=True))
            elif failure == 'rotation':
                stack.enter_context(patch.object(gs, '_rotate_log_if_large',
                    side_effect=OSError(errno.EACCES, 'fixture rotation refused')))
            classify = stack.enter_context(patch.object(gs, 'classify_llm_error', wraps=gs.classify_llm_error))
            pause = stack.enter_context(patch.object(gs, 'pause_for_operator'))
            switch = stack.enter_context(patch.object(client, 'failover', wraps=client.failover))
            stack.enter_context(contextlib.redirect_stdout(output))
            result = gs.call_llm_for_entries(client, 'primary', params.system_prompt,
                json.dumps(source), params, "responses.log", "CPU LOG FIXTURE",
                max_retries=0, attempt_observer=attempts.append)
            if malformed:
                self.assertEqual([], result)
                self.assertEqual('missing_json_array', attempts[0]['failure_codes'][0])
            else:
                self.assertEqual(source, result)
                artifact = Path(tmp) / 'accepted.json'
                from utils import atomic_json_write
                atomic_json_write(result, str(artifact))
                self.assertEqual(source, json.loads(artifact.read_text()))
            classify.assert_not_called()
            pause.assert_not_called()
            switch.assert_not_called()
            self.assertEqual(1, len(primary.requests))
            self.assertEqual(0, len(secondary.requests))
            self.assertFalse(client.switched)
            self.assertEqual(1, len(attempts))
            self.assertIsNone(attempts[0]['error'])
            self.assertIn('response_log_error', attempts[0])
            self.assertTrue(attempts[0]['raw_response'])
            self.assertIn('WARNING: response logging failed', output.getvalue())
            self.assertNotIn('Error calling LLM API', output.getvalue())

    def test_native_log_open_failure_retains_valid_completion(self):
        self.run_case('open')

    def test_log_directory_creation_failure_does_not_switch_provider(self):
        self.run_case('directory')

    def test_partial_append_disk_full_retains_valid_completion(self):
        self.run_case('write')

    def test_rotation_failure_does_not_pause_or_retry_provider(self):
        self.run_case('rotation')

    def test_logging_failure_does_not_make_malformed_response_acceptable(self):
        self.run_case('open', malformed=True)
