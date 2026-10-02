"""Persisted resume, skipped windows, and timing denominators must agree."""
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

import three_pass_generate as tp
from generate_script import LLMGenParams

if os.environ.get('THREE_PASS_PROGRESS_SOURCE'):
    spec = importlib.util.spec_from_file_location('tp_progress_saved', os.environ['THREE_PASS_PROGRESS_SOURCE'])
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)


class ThreePassResumeProgressTests(unittest.TestCase):
    def test_new_interval_rate_does_not_use_historical_completed_calls(self):
        progress = tp.RunProgress({1:5,2:3,3:2})
        if hasattr(progress, 'restore_done'):
            progress.restore_done(1,5)
        else:
            progress.done[1] = 5  # original runtime restore behaviour
        progress.started = 100
        progress.note_done(2)
        self.assertIn('[eta_seconds=120 fraction=0.600]', progress.eta_line(2, now=130))

    def test_real_instruction_resume_excludes_narrator_attribution_and_counts_finished_window(self):
        texts = [f'Narration line {index}.' for index in range(26)]
        source = '\n\n'.join(texts)
        params = LLMGenParams(max_tokens=4096,segmentation='quotes',structured_output='off')
        calls=[]
        def create(**kwargs):
            user=kwargs['messages'][-1]['content']; calls.append(user)
            match=re.search(r'\[\{"n":', user)
            if match is None:
                raise AssertionError('Resume must make only delivery requests')
            rows=json.JSONDecoder().raw_decode(user[match.start():])[0]
            payload=[{'n':row['n'],'head':' '.join(row['text'].split()[:3]),'instruct':'Neutral.'} for row in rows]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)),finish_reason='stop')],usage=None)
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as transcript:
            output=str(Path(tmp)/'book.json')
            segmented=[{'type':'NARRATOR','text':text} for text in texts]
            named=[{'speaker':'NARRATOR','text':text} for text in texts]
            annotated=[{**entry,'instruct':'Earlier.'} for entry in named[:25]] + [None]
            fingerprint=tp.three_pass_fingerprint(source,'fixture',6000,params)
            tp._save_three_pass_checkpoint(output,fingerprint,'instruct',segmented,1,named,annotated,
                                           ['quote_presegmented'],elapsed_s={'segment':1800,'instruct':900})
            entries=tp.run_three_pass(client,'fixture',source,params,6000,output_path=output,
                                      planned_calls={1:0,2:2,3:2})
            checkpoint=json.loads(Path(tp.three_pass_checkpoint_path(output)).read_text())
            manifest=json.loads(Path(tp.three_pass_manifest_path(output)).read_text())
        etas=[line for line in transcript.getvalue().splitlines() if line.startswith('ETA:')]
        self.assertEqual(1,len(calls))
        self.assertEqual(texts,[entry['text'] for entry in entries])
        self.assertEqual(['Earlier.']*25+['Neutral.'],[entry['instruct'] for entry in entries])
        self.assertEqual(entries,checkpoint['annotated'])
        self.assertEqual('complete',manifest['status'])
        self.assertTrue(etas)
        self.assertIn('2 of 2 model calls done',etas[-1])
        self.assertIn('[eta_seconds=0 fraction=1.000]',etas[-1])

    def test_real_attribution_resume_counts_completed_window_before_remaining_requests(self):
        narration = 'Alice walked. Alice waited. Alice left.'
        spoken = [f'Utterance {index}.' for index in range(26)]
        source = narration + ' ' + ' '.join('"' + text + '"' for text in spoken)
        segmented = [{'type':'NARRATOR','text':narration}] + [{'type':'SPOKEN','text':text} for text in spoken]
        named = [{'speaker':'NARRATOR' if index==0 else 'ALICE','text':entry['text']}
                 for index,entry in enumerate(segmented[:25])] + [None,None]
        params = LLMGenParams(max_tokens=4096,segmentation='quotes',structured_output='off')
        calls=[]
        def create(**kwargs):
            user=kwargs['messages'][-1]['content']; calls.append(user)
            match=re.search(r'\[\{"n":', user)
            rows=json.JSONDecoder().raw_decode(user[match.start():])[0]
            attribute = 'type' in rows[0]
            payload=[{'n':row['n'],'head':' '.join(row['text'].split()[:3]),
                      **({'speaker':'ALICE' if row['type']=='SPOKEN' else 'NARRATOR'}
                         if attribute else {'instruct':'Neutral.'})} for row in rows]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)),finish_reason='stop')],usage=None)
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as transcript:
            output=str(Path(tmp)/'book.json')
            fingerprint=tp.three_pass_fingerprint(source,'fixture',6000,params)
            tp._save_three_pass_checkpoint(output,fingerprint,'attribute',segmented,1,named,[],
                                           ['quote_presegmented'],elapsed_s={'attribute':900})
            entries=tp.run_three_pass(client,'fixture',source,params,6000,output_path=output,
                                      planned_calls={1:0,2:2,3:2})
            checkpoint=json.loads(Path(tp.three_pass_checkpoint_path(output)).read_text())
        etas=[line for line in transcript.getvalue().splitlines() if line.startswith('ETA:')]
        self.assertEqual(3,len(calls),transcript.getvalue())
        self.assertEqual([entry['text'] for entry in segmented],[entry['text'] for entry in entries])
        self.assertEqual(entries,checkpoint['annotated'])
        self.assertIn('2 of 4 model calls done',etas[0])
        self.assertIn('fraction=0.500',etas[0])
        self.assertIn('4 of 4 model calls done',etas[-1])
        self.assertIn('[eta_seconds=0 fraction=1.000]',etas[-1])

    def test_nonverbal_run_finishes_progress_without_any_provider_call(self):
        with contextlib.redirect_stdout(io.StringIO()) as transcript:
            entries=tp.run_three_pass(None,'fixture','...',
                       LLMGenParams(max_tokens=500,segmentation='quotes'),6000,
                       planned_calls={1:0,2:1,3:1})
        self.assertEqual('...',entries[0]['text'])
        etas=[line for line in transcript.getvalue().splitlines() if line.startswith('ETA:')]
        self.assertEqual(1,len(etas))
        self.assertIn('0 of 0 model calls done',etas[0])
        self.assertIn('[eta_seconds=0 fraction=1.000]',etas[0])
