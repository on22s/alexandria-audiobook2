"""Character targets preserve source binding and refuse unsafe requests."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import three_pass_generate as tp
from config_settings import GenerationConfig
from generate_script import LLMGenParams
from lmstudio_settings import TokenBudgetError


class CharacterBatchTests(unittest.TestCase):
    def test_unicode_boundaries_duplicates_and_context(self):
        entries=[{'text':t} for t in ['猫猫','Yes','Yes','x'*12,'z']]
        before=copy.deepcopy(entries)
        self.assertEqual([[0,1],[2],[3],[4]],[[i for i,_ in w] for w in tp.iter_entry_windows(entries,25,5)])
        self.assertEqual([0,1,3,4,2],[i for b in tp.iter_unique_entry_batches(entries,25,100) for i,_ in b])
        bounds=tp.get_entry_window_bounds(entries,25,100)
        contexts=tp.get_missing_attribute_contexts(entries,[0,1,3,4],25,bounds)
        self.assertEqual(entries[2],contexts[1]['next_context'])
        self.assertEqual(entries[2],contexts[2]['previous_context'])
        self.assertEqual(before,entries)

    def test_defaults_ranges_and_legacy_windows(self):
        entries=[{'text':str(i)} for i in range(53)]
        self.assertEqual([25,25,3],[len(b) for b in tp.iter_unique_entry_batches(entries)])
        g=GenerationConfig()
        self.assertEqual((3000,25,2000,0,0),(g.three_pass_chunk_size,g.three_pass_attribute_batch_size,
            g.three_pass_attribute_context_chars,g.three_pass_attribute_target_chars,g.three_pass_instruct_target_chars))
        for key in ('three_pass_chunk_size','three_pass_attribute_context_chars',
                    'three_pass_attribute_target_chars','three_pass_instruct_target_chars'):
            self.assertEqual(1000000,getattr(GenerationConfig(**{key:1000000}),key))
            with self.assertRaises(ValueError): GenerationConfig(**{key:1000001})

    def test_invalid_targets_stop_before_client_or_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            for target in (-1,True,'500',1000001):
                with self.subTest(target=target),patch.object(tp,'get_run_model_binding') as binding:
                    with self.assertRaises(ValueError):
                        tp.run_three_pass(None,'fixture','Text.',LLMGenParams(),3000,
                            attribute_target_chars=target,output_path=str(Path(tmp)/'out.json'))
                    binding.assert_not_called()
            self.assertEqual([],list(Path(tmp).iterdir()))

    def test_full_prompt_output_and_reasoning_budget(self):
        p=LLMGenParams(max_tokens=1024,hard_max_tokens=1024,context_length=1024)
        self.assertTrue(tp.does_request_fit_context('S','U',p,256))
        self.assertFalse(tp.does_request_fit_context('S','x'*1500,p,256))
        p.reasoning_allowance=400
        self.assertFalse(tp.does_request_fit_context('S','U',p,256))
        self.assertFalse(tp.does_request_fit_context('S','x'*4000,p,1))

    def test_fingerprint_compatibility_and_target_changes(self):
        args=('Text.','fixture',3000,LLMGenParams())
        old=tp.three_pass_fingerprint(*args)
        self.assertEqual(old,tp.three_pass_fingerprint(*args,attribute_target_chars=0,instruct_target_chars=0))
        self.assertNotEqual(old,tp.three_pass_fingerprint(*args,attribute_target_chars=100))
        self.assertNotEqual(old,tp.three_pass_fingerprint(*args,instruct_target_chars=100))

    def get_params(self):
        return LLMGenParams(max_tokens=512,context_length=1100,hard_max_tokens=512,
            segmentation='quotes',structured_output='off',attribute_system_prompt='Attribute.',
            user_prompt_template='{batch}',instruct_system_prompt='Instruct.',instruct_user_prompt_template='{batch}')

    def test_actual_pipeline_subdivides_and_replays_checkpoint_without_calls(self):
        texts=[f'Line {i} '+('word '*32) for i in range(8)]
        source=' '.join(texts); segmented=[{'type':'SPOKEN','text':t} for t in texts]
        params=self.get_params(); requests=[]
        def create(**kwargs):
            rows=json.loads(kwargs['messages'][-1]['content']);requests.append(rows)
            result=[{'n':r['n'],'head':' '.join(r['text'].split()[:3]),
                     **({'speaker':'UNKNOWN'} if 'type' in r else {'instruct':'Natural.'})} for r in rows]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)),finish_reason='stop')],usage=None)
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        opts=dict(attribute_target_chars=1000000,instruct_target_chars=1000000,on_exhaustion='fallback')
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            path=str(Path(tmp)/'out.json')
            fp=tp.three_pass_fingerprint(source,'fixture',3000,params,**opts)
            tp._save_three_pass_checkpoint(path,fp,'segment',segmented,1,[],[],['quote_presegmented'])
            result=tp.run_three_pass(client,'fixture',source,params,3000,output_path=path,**opts)
            self.assertEqual(texts,[r['text'] for r in result])
            self.assertEqual(result,json.loads(Path(tp.three_pass_checkpoint_path(path)).read_text())['annotated'])
            self.assertGreater(len(requests),2);self.assertTrue(all(len(r)<8 for r in requests))
            count=len(requests)
            self.assertEqual(result,tp.run_three_pass(client,'fixture',source,params,3000,output_path=path,**opts))
            self.assertEqual(count,len(requests))

    def test_preflight_subdivides_and_refuses_single_oversized_entry(self):
        settings=dict(chunk_size=3000,max_tokens=512,segment_output_ratio=1.5,segmentation='quotes',
                      attribute_target_chars=1000000,instruct_target_chars=1000000)
        source=' '.join('"Line '+str(i)+' '+('word '*32)+'"' for i in range(8))
        report=tp.build_three_pass_request_preflight(source,settings,1100,1,params=self.get_params())
        self.assertGreater(tp.planned_calls_from_preflight(report)[2],1)
        self.assertGreater(tp.planned_calls_from_preflight(report)[3],1)
        self.assertEqual(tp.planned_calls_from_preflight(report),
                         tp.get_three_pass_planned_calls(source,settings,self.get_params()))
        with self.assertRaises(TokenBudgetError):
            tp.build_three_pass_request_preflight('"'+('word '*5000)+'"',settings,1100,1,params=self.get_params())

    def test_large_pass1_target_splits_using_real_prompt_and_output_budget(self):
        source='Short sentence. '*6000
        p=LLMGenParams(context_length=4096,max_tokens=2048,hard_max_tokens=2048,
                       segment_system_prompt='Segment.',segment_user_prompt_template='{chunk}')
        records=tp.get_segment_chunk_records(source,1000000,p)
        self.assertGreater(len(records),1)
        self.assertEqual(source.split(),[word for r in records for word in r['text'].split()])
        for r in records:
            self.assertTrue(tp.does_request_fit_context('Segment.',r['text'],p,
                            tp.resolve_completion_ceiling(len(r['text'].split()),p)))

    def test_preflight_cache_context_binding_only_for_new_sizing_modes(self):
        from routers import script
        for settings in ({'attribute_target_chars':1000},{'instruct_target_chars':1000},{'chunk_size':1000000},{'attribute_context_chars':1000000}):
            self.assertNotEqual(script.get_batch_script_sizing_identity('Text.',settings,None,4096),
                                script.get_batch_script_sizing_identity('Text.',settings,None,32768))
        self.assertEqual(script.get_batch_script_sizing_identity('Text.',{},None,4096),
                         script.get_batch_script_sizing_identity('Text.',{},None,32768))

    def test_larger_context_is_budgeted_even_in_legacy_line_mode(self):
        settings=dict(chunk_size=3000,max_tokens=512,segment_output_ratio=1.5,
                      segmentation='quotes',attribute_context_chars=1000000)
        source=' '.join('"Line '+str(i)+' '+('word '*32)+'"' for i in range(8))
        with self.assertRaises(TokenBudgetError):
            tp.build_three_pass_request_preflight(source,settings,1100,1,params=self.get_params())
