"""Count-only planning and resumed fallback roster work on actual pipeline paths."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import three_pass_generate as tp
from generate_script import LLMGenParams

if os.environ.get('THREE_PASS_PLANNING_SOURCE'):
    spec = importlib.util.spec_from_file_location('saved_planning', os.environ['THREE_PASS_PLANNING_SOURCE'])
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)
    tp.DEFAULT_MODEL_PROFILES_PATH = str(Path(__file__).resolve().parent.parent / "three_pass_model_profiles.json")


class ThreePassPlanningWorkTests(unittest.TestCase):
    def test_count_only_matches_full_report_without_prompt_construction(self):
        cases = ['Plain narration. More narration.',
                 'Alice waited. "One." Alice left. "Two." "One."',
                 '"A continued speech ' + 'word ' * 110 + 'ending."', '...']
        for mode in ('quotes', 'llm'):
            for votes in (1, 3):
                for batch_size in (1, 25):
                    for source in cases:
                        with self.subTest(mode=mode, votes=votes, batch=batch_size, source=source[:20]):
                            settings = {'chunk_size':500, 'max_tokens':4096,
                                'segment_output_ratio':1.5, 'segmentation':mode,
                                'attribution_votes':votes, 'attribute_batch_size':batch_size,
                                'attribute_context_chars':2000}
                            params = LLMGenParams(max_tokens=4096, segmentation=mode)
                            expected = tp.planned_calls_from_preflight(tp.build_three_pass_request_preflight(
                                source, settings, 32768, 1, params=params))
                            with patch.object(tp, 'load_segment_prompts', side_effect=AssertionError('no prompt loading')), \
                                 patch.object(tp, 'build_attribute_request', side_effect=AssertionError('no attribution formatting')), \
                                 patch.object(tp, 'build_instruct_request', side_effect=AssertionError('no instruction formatting')):
                                actual = tp.get_three_pass_planned_calls(source, settings, params)
                            self.assertEqual(expected, actual)

    def test_actual_cli_uses_counts_without_full_preflight_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.txt'
            source.write_text('Alice waited. "Yes." Alice left.')
            output = Path(tmp) / 'book.json'
            with contextlib.redirect_stdout(io.StringIO()), \
                 patch.object(tp.sys, 'argv', ['three_pass', str(source), '--output', str(output), '--segmentation', 'quotes']), \
                 patch.object(tp, 'load_app_config', return_value={'llm_mode':'local', 'llm_local':{'model_name':'fixture'}, 'generation':{'max_tokens':4096}}), \
                 patch.object(tp, 'ensure_ideal_settings', return_value=(None, {'context_length':32768}, 'fixture')), \
                 patch.object(tp, 'make_run_client', return_value=object()), \
                 patch.object(tp, 'run_three_pass', return_value=[{'speaker':'NARRATOR','text':'result','instruct':'Neutral.'}]) as run, \
                 patch.object(tp, 'build_three_pass_request_preflight', side_effect=AssertionError('CLI formatted full report')):
                tp.main()
            self.assertEqual({1:0, 2:1, 3:1}, run.call_args.kwargs['planned_calls'])
            self.assertEqual('result', json.loads(output.read_text())[0]['text'])

    def test_resumed_fallback_preserves_source_order_without_full_named_rescans(self):
        narration = 'Alice waited. Alice left. Alice returned. Bob waited. Bob left. Bob returned. Carol waited. Carol left. Carol returned.'
        spoken = [f'Utterance {index}.' for index in range(6)]
        source = narration + ' ' + ' '.join('"' + text + '"' for text in spoken)
        segmented = [{'type':'NARRATOR','text':narration}] + [{'type':'SPOKEN','text':text} for text in spoken]
        named = [{'speaker':'NARRATOR','text':narration}, None, None,
                 {'speaker':'CAROL','text':spoken[2], 'attribution_unchecked':True},
                 {'speaker':'BOB','text':spoken[3]}, None, {'speaker':'ALICE','text':spoken[5]}]
        params = LLMGenParams(max_tokens=4096, segmentation='quotes', structured_output='off')
        scan_sizes, rosters = [], []
        original_roster, original_request = tp.build_roster, tp.build_attribute_request
        def scan(entries, *args):
            entries = list(entries)
            scan_sizes.append(len(entries))
            return original_roster(entries, *args)
        def request(batch, params, roster, *args, **kwargs):
            rosters.append(list(roster))
            return original_request(batch, params, roster, *args, **kwargs)
        def create(**kwargs):
            user = kwargs['messages'][-1]['content']
            match = re.search(r'\[\{"n":', user)
            rows = json.JSONDecoder().raw_decode(user[match.start():])[0]
            attribute = 'type' in rows[0]
            payload = []
            for row in rows:
                speaker = {'Utterance 0.':'ALICE', 'Utterance 1.':'CAROL', 'Utterance 4.':'BOB'}.get(row['text'], 'NARRATOR')
                payload.append({'n':row['n'],'head':' '.join(row['text'].split()[:3]),
                                **({'speaker':speaker} if attribute else {'instruct':'Neutral.'})})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)), finish_reason='stop')], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            output = str(Path(tmp) / 'book.json')
            fingerprint = tp.three_pass_fingerprint(source,'fixture',6000,params,on_exhaustion='fallback',attribute_batch_size=1)
            tp._save_three_pass_checkpoint(output,fingerprint,'attribute',segmented,1,named,[],['quote_presegmented'])
            with patch.object(tp, 'build_roster', side_effect=scan), patch.object(tp, 'build_attribute_request', side_effect=request):
                entries = tp.run_three_pass(client,'fixture',source,params,6000,output_path=output,on_exhaustion='fallback',attribute_batch_size=1)
            checkpoint = json.loads(Path(tp.three_pass_checkpoint_path(output)).read_text())
            self.assertEqual(entries, checkpoint['annotated'])
        self.assertEqual([['BOB','ALICE'], ['ALICE','BOB'], ['ALICE','CAROL','BOB']], rosters)
        self.assertEqual(['NARRATOR','ALICE','CAROL','CAROL','BOB','BOB','ALICE'], [entry['speaker'] for entry in entries])
        self.assertLessEqual(sum(scan_sizes), len(segmented), f'full named rescans: {scan_sizes}')
