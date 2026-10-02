"""Compare actual HTTP estimates with the real contextual-review CLI partitions."""
import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
import review_script
from routers import script


class ContextualReviewEstimateTests(unittest.TestCase):
    def test_window_is_neighbor_context_and_does_not_partition_the_target_batch(self):
        for count, window, batch_size in ((100,4,25),(101,12,25),(7,1,3)):
            with self.subTest(entries=count,window=window,batch_size=batch_size), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'annotated_script.json'
                config = root / 'config.json'
                entries = [{'speaker':'ALICE','text':f'Entry {i} stays in the same target batch.',
                            'instruct':'calm'} for i in range(count)]
                source.write_text(json.dumps(entries))
                config.write_text(json.dumps({'llm_mode':'local',
                    'llm_local':{'base_url':'http://cpu-fixture/v1','model_name':'fixture','api_key':'local'},
                    'generation':{'review_batch_size':batch_size}}))
                original_config = config.read_bytes()
                calls,commands = [],[]
                state = copy.deepcopy(core.process_state)
                for entry in state.values():entry['running'] = False
                def review(client,model,batch,index,total,params,**kwargs):
                    start = len(calls) * batch_size
                    end = min(count,start+batch_size)
                    self.assertEqual(entries[start:end],batch)
                    self.assertEqual(index,len(calls)+1)
                    self.assertEqual((count+batch_size-1)//batch_size,total)
                    self.assertIsNone(kwargs['previous_tail'])
                    context = kwargs['source_context']
                    expected_before = entries[max(0,start-window):start]
                    expected_after = entries[end:min(count,end+window)]
                    before = context.split('--- PREVIOUS ENTRIES (Context Only) ---\n')
                    after = context.split('--- NEXT ENTRIES (Context Only) ---\n')
                    self.assertEqual(bool(expected_before),len(before)==2)
                    self.assertEqual(bool(expected_after),len(after)==2)
                    if expected_before:
                        parsed = before[1].split('\n\n--- NEXT ENTRIES')[0].splitlines()
                        self.assertEqual(expected_before,[json.loads(line) for line in parsed if line])
                    if expected_after:
                        self.assertEqual(expected_after,[json.loads(line) for line in after[1].splitlines() if line])
                    calls.append(list(batch))
                    return [dict(entry) for entry in batch]
                def dispatch(command,task_name):
                    commands.append(command)
                    self.assertEqual('review',task_name)
                    self.assertTrue(state['review']['running'])
                    try:
                        with patch.object(review_script.sys,'argv',command[2:]):
                            review_script.main()
                    finally:
                        state['review']['running'] = False
                api = FastAPI();api.include_router(script.router)
                with ExitStack() as stack:
                    stack.enter_context(redirect_stdout(io.StringIO()))
                    for module in (core,script):
                        stack.enter_context(patch.object(module,'process_state',state))
                    stack.enter_context(patch.object(core,'acquire_gpu_lock',return_value=None))
                    stack.enter_context(patch.object(core,'_gpu_leases',{}))
                    stack.enter_context(patch.object(core,'llm_is_on_this_gpu',return_value=True))
                    stack.enter_context(patch.object(script,'SCRIPT_PATH',str(source)))
                    stack.enter_context(patch.object(script,'CONFIG_PATH',str(config)))
                    stack.enter_context(patch.object(script,'run_process',side_effect=dispatch))
                    stack.enter_context(patch.object(review_script,'get_runtime_data_dir',return_value=tmp))
                    stack.enter_context(patch.object(review_script,'get_app_config_path',return_value=str(config)))
                    stack.enter_context(patch.object(review_script,'ensure_ideal_settings',return_value=(False,{},'CPU fixture settings')))
                    stack.enter_context(patch.object(review_script,'get_current_status',return_value={'loaded':False}))
                    stack.enter_context(patch.object(review_script,'make_run_client',return_value=object()))
                    stack.enter_context(patch.object(review_script,'get_cached_or_benchmarked_concurrency',return_value=1))
                    headroom = stack.enter_context(patch.object(review_script,'wait_for_vram_headroom',return_value=True))
                    stack.enter_context(patch.object(review_script,'review_batch',side_effect=review))
                    client = stack.enter_context(TestClient(api))
                    response = client.post('/api/review_script_contextual',json={'window_size':window,'dedupe_speakers':False})
                self.assertEqual(200,response.status_code,response.text)
                estimate = response.json()
                self.assertEqual(count,estimate['total_entries'])
                self.assertEqual(batch_size,estimate['batch_size'])
                self.assertEqual(window,estimate['window_size'])
                self.assertEqual(len(calls),estimate['estimated_calls'])
                self.assertEqual((count+batch_size-1)//batch_size,len(calls))
                self.assertEqual(len(calls),headroom.call_count)
                self.assertEqual(entries,[entry for batch in calls for entry in batch])
                self.assertEqual(entries,json.loads(source.read_text()))
                self.assertEqual(original_config,config.read_bytes())
                self.assertFalse(Path(str(source)+'.review_checkpoint.json').exists())
                self.assertEqual(1,len(commands))
                self.assertFalse(state['review']['running'])
