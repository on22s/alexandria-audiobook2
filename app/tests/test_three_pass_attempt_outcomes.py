"""Reread run artifacts after real LLM parsing/recovery fills attempt outcomes."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import three_pass_generate as tp
from generate_script import LLMGenParams
from tests.test_three_pass_generate import _load_orchestration_fixture_cast

if os.environ.get('THREE_PASS_ATTEMPT_SOURCE'):
    spec = importlib.util.spec_from_file_location('tp_attempt_saved', os.environ['THREE_PASS_ATTEMPT_SOURCE'])
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)


def client_for(contents):
    calls = []
    def create(**kwargs):
        content = contents[min(len(calls), len(contents) - 1)]
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason='stop')],
                               usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


class ThreePassAttemptOutcomeTests(unittest.TestCase):
    def test_failed_attribution_diagnostics_keep_final_parse_failure(self):
        client, calls = client_for(['not JSON'])
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            output = str(Path(tmp) / 'book.json')
            tp.run_three_pass(client, 'fixture', '"Hello."',
                              LLMGenParams(max_tokens=500, segmentation='quotes', structured_output='off'),
                              3000, output_path=output, collect_all_failures=True,
                              cast=_load_orchestration_fixture_cast(('ALICE',)))
            manifest = json.loads(Path(tp.three_pass_manifest_path(output)).read_text())
            checkpoint = json.loads(Path(tp.three_pass_checkpoint_path(output)).read_text())
        self.assertTrue(calls)
        self.assertEqual('incomplete', manifest['status'])
        self.assertEqual(len(calls), manifest['progress']['llm_calls'])
        self.assertEqual(len(calls), manifest['progress']['failure_codes'].get('missing_json_array', 0))
        failures = [f for f in manifest['diagnostic_failures'] if f['pass'] == 'attribute']
        self.assertTrue(failures)
        self.assertTrue(all(f['reason'] == 'missing_json_array' for f in failures))
        self.assertEqual(manifest['diagnostic_failures'], checkpoint['diagnostic_failures'])

    def test_failed_attribution_quality_code_reaches_diagnostics(self):
        client, calls = client_for([json.dumps([{'n':0, 'head':'Hello.', 'speaker':'NARRATOR'}])])
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            output = str(Path(tmp) / 'book.json')
            tp.run_three_pass(client, 'fixture', '"Hello."',
                              LLMGenParams(max_tokens=500, segmentation='quotes', structured_output='off'),
                              3000, output_path=output, collect_all_failures=True,
                              cast=_load_orchestration_fixture_cast(('ALICE',)))
            manifest = json.loads(Path(tp.three_pass_manifest_path(output)).read_text())
        self.assertTrue(calls)
        self.assertEqual(len(calls), manifest['progress']['failure_codes'].get('spoken_not_named',0))
        failures = [f for f in manifest['diagnostic_failures'] if f['pass'] == 'attribute']
        self.assertTrue(failures)
        self.assertTrue(all(f['reason'] == 'spoken_not_named' for f in failures))

    def run_rescue(self, contents, should_fail):
        client, calls = client_for(contents)
        def initial_failure(*args, **kwargs):
            kwargs['failure_sink'].append({'context_required'})
            return []
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), \
             patch.object(tp, 'segment_chunk_adaptively', side_effect=initial_failure):
            output = str(Path(tmp) / 'book.json')
            run = lambda: tp.run_three_pass(client, 'fixture', 'Target.',
                         LLMGenParams(max_tokens=500, segmentation='llm', structured_output='off'),
                         3000, output_path=output, context_windows=[2,5], context_rescue_retries=0)
            if should_fail:
                with self.assertRaisesRegex(RuntimeError, 'pass 1'):
                    run()
                result = None
            else:
                result = run()
            manifest = json.loads(Path(tp.three_pass_manifest_path(output)).read_text())
            checkpoint = json.loads(Path(tp.three_pass_checkpoint_path(output)).read_text())
        return result, calls, manifest, checkpoint

    def test_rescue_calls_and_final_outcomes_reach_completed_manifest(self):
        result, calls, manifest, checkpoint = self.run_rescue(
            ['not JSON', json.dumps([{'type':'NARRATOR', 'text':'Target.'}]),
             json.dumps([{'n':0, 'head':'Target.', 'instruct':'Neutral.'}])], False)
        self.assertEqual(3, len(calls))
        self.assertEqual('Target.', result[0]['text'])
        self.assertEqual(result, checkpoint['annotated'])
        self.assertEqual('complete', manifest['status'])
        self.assertEqual('context_rescue:5', manifest['chunks'][0]['resolution'])
        self.assertEqual(3, manifest['progress']['llm_calls'])
        self.assertEqual(21, manifest['progress']['completion_tokens'])
        self.assertEqual(3, manifest['progress']['response_fingerprints'])
        self.assertEqual(1, manifest['progress']['failure_codes'].get('missing_json_array',0))

    def test_exhausted_rescue_writes_labeled_final_attempts_to_failure_checkpoint(self):
        _, calls, manifest, checkpoint = self.run_rescue(['not JSON'], True)
        self.assertEqual(2, len(calls))
        self.assertEqual(2, manifest['progress']['llm_calls'])
        self.assertEqual(2, manifest['progress']['failure_codes'].get('missing_json_array',0))
        attempts = checkpoint['failed']['attempts']
        self.assertEqual(2, len(attempts))
        self.assertTrue(all(a['pass'] == 'segment_context_rescue' for a in attempts))
        self.assertTrue(all(a['outcome'] == 'response_rejected' and a['failure_codes'] == ['missing_json_array'] for a in attempts))
