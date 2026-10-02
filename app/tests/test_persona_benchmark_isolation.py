"""Concurrent real persona compilation keeps callbacks local to each run."""
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import benchmark_runner as runner
import generate_personas as personas
import generate_script as gs
from benchmark_fixtures import build_persona_generation_manifest

if os.environ.get('PERSONA_BENCHMARK_SOURCE'):
    spec = importlib.util.spec_from_file_location('saved_persona_benchmark', os.environ['PERSONA_BENCHMARK_SOURCE'])
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)


class PersonaBenchmarkIsolationTests(unittest.TestCase):
    def test_overlapping_real_compilations_preserve_shared_functions_and_independent_capture(self):
        original_sleep = time.sleep
        original_save = personas._save_generated_preview
        barrier = threading.Barrier(2)
        fixtures = [build_persona_generation_manifest([{'entries':[{'speaker':speaker,'text':text}]}])['fixtures'][0]
                    for speaker,text in [('ALICE','Hello Alice.'),('BOB','Hello Bob.')]]
        before = copy.deepcopy(fixtures)
        def discover(_client,_model,_prompt,batch,*args,**kwargs):
            return [{'name':batch[0]['speaker'],'features':['calm'], 'sample_lines':[batch[0]['text']]}]
        def run(fixture):
            speaker = fixture['speakers'][0]
            text = fixture['entries'][0]['text']
            def create(**kwargs):
                self.assertIs(original_sleep,time.sleep)
                self.assertIs(original_save,personas._save_generated_preview)
                barrier.wait(timeout=5)
                payload = {'description':speaker+' calm voice.', 'ref_text':text}
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)),finish_reason='stop')],usage=None)
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            return runner._run_persona_case(fixture,client,'fixture',4096)
        try:
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.object(personas,'_discover_batch_characters',side_effect=discover), \
                 patch.object(gs,'get_response_log_path',side_effect=lambda *args,**kwargs:str(Path(tmp)/f'{threading.get_ident()}.log')), \
                 ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(run,fixtures))
            for fixture,result in zip(fixtures,results):
                speaker = fixture['speakers'][0]
                self.assertEqual('passed',result['status'])
                self.assertEqual({speaker},set(result['personas']))
                self.assertEqual(speaker+' calm voice.',result['personas'][speaker]['description'])
                self.assertEqual(fixture['entries'][0]['text'],result['personas'][speaker]['ref_text'])
                self.assertGreaterEqual(result['elapsed_seconds'],0)
            self.assertEqual(before,fixtures)
            self.assertIs(original_sleep,time.sleep)
            self.assertIs(original_save,personas._save_generated_preview)
        finally:
            # Restore the saved baseline's global corruption after negative verification.
            time.sleep = original_sleep
            personas._save_generated_preview = original_save

    def test_case_exception_cannot_leave_global_preview_or_sleep_overridden(self):
        fixture = build_persona_generation_manifest([{'entries':[{'speaker':'ALICE','text':'Hello there.'}]}])['fixtures'][0]
        original_sleep,original_save = time.sleep,personas._save_generated_preview
        def fail(*args,**kwargs):
            self.assertIs(original_sleep,time.sleep)
            self.assertIs(original_save,personas._save_generated_preview)
            raise RuntimeError('fixture compilation failed')
        with patch.object(personas,'_discover_batch_characters',return_value=[{'name':'ALICE','features':['calm'],'sample_lines':['Hello there.']}]), \
             patch.object(personas,'_compile_persona',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'fixture compilation failed'):
                runner._run_persona_case(fixture,object(),'fixture',4096)
        self.assertIs(original_sleep,time.sleep)
        self.assertIs(original_save,personas._save_generated_preview)
