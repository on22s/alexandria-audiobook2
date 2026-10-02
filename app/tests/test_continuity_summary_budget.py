"""Real continuity requests enforce context headroom and bounded memory."""
import contextlib
import importlib.util
import io
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import attribution_prompt_variants as apv
from generate_script import LLMGenParams
from lmstudio_settings import get_effective_max_tokens

if os.environ.get('SUMMARY_SOURCE'):
    spec = importlib.util.spec_from_file_location('summary_saved', os.environ['SUMMARY_SOURCE'])
    apv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(apv)


class Client:
    def __init__(self, text, finish='stop'):
        self.calls = []
        self.text = text
        self.finish = finish
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.text), finish_reason=self.finish)])


class ContinuitySummaryBudgetTests(unittest.TestCase):
    def test_actual_request_clamps_completion_after_full_prompt_and_reserve(self):
        client = Client('Alice waits at the inn.')
        params = LLMGenParams(context_length=1100)
        passage = 'Alice waited. ' * 70
        result = apv.rolling_summary(client, 'model', params, 'Alice arrived.', passage)
        self.assertEqual('Alice waits at the inn.', result)
        request = client.calls[0]
        expected = get_effective_max_tokens(400, 1100, request['messages'], 400,
                                            scale_to_context=False)
        self.assertLess(expected, 400)
        self.assertEqual(expected, request['max_tokens'])
        self.assertIn(passage, request['messages'][0]['content'])
        self.assertEqual('none', request['extra_body']['reasoning_effort'])

    def test_overflow_refuses_dispatch_and_reports_retained_memory(self):
        client = Client('new summary')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = apv.rolling_summary(client, 'm', LLMGenParams(context_length=700),
                                         'Prior evidence.', 'text ' * 1000)
        self.assertEqual('Prior evidence.', result)
        self.assertEqual([], client.calls)
        self.assertIn('continuity summary unavailable', output.getvalue())
        self.assertIn('loaded context', output.getvalue())

    def test_rejects_verbose_unbroken_empty_and_truncated_responses(self):
        for text, finish in [('word ' * 151, 'stop'), ('字' * 1201, 'stop'),
                             (' ', 'stop'), ('Alice was', 'length')]:
            with self.subTest(text=text[:20], finish=finish):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = apv.rolling_summary(Client(text, finish), 'm', LLMGenParams(),
                                                 'Prior evidence.', 'Alice spoke.')
                self.assertEqual('Prior evidence.', result)
                self.assertIn('summary rejected', output.getvalue())
        self.assertEqual(' '.join(['cue'] * 150), apv.rolling_summary(
            Client(' '.join(['cue'] * 150)), 'm', LLMGenParams(), '', 'passage'))

    def test_provider_does_not_carry_rejected_summary_into_next_window(self):
        client = Client('injected ' * 151)
        provider = apv.make_provider('continuity')
        frozen = [{'type': 'SPOKEN', 'text': 'Hello.'}]
        bodies = []

        def attribute(client, model, system, body, params, **kwargs):
            bodies.append(body)
            return [{'n': 0, 'speaker': 'ALICE'}]

        with patch.object(apv, 'call_llm_for_entries', attribute), \
             contextlib.redirect_stdout(io.StringIO()):
            for _ in range(2):
                provider(client, 'm', '', '', LLMGenParams(context_length=4096),
                         'log', 'ATTRIBUTE', 0, None, None, frozen, roster=['ALICE'])
        self.assertEqual(2, len(client.calls))
        self.assertNotIn('injected', bodies[1])
        self.assertIn('ALICE: "Hello."', bodies[1])
        self.assertIn('STORY SO FAR', bodies[1])
