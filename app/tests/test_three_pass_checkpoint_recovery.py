"""Native checkpoint replay and malformed-state tests, without inference."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import three_pass_generate as tp
from generate_script import LLMGenParams

if os.environ.get('THREE_PASS_CHECKPOINT_SOURCE'):
    spec = importlib.util.spec_from_file_location('tp_checkpoint_saved', os.environ['THREE_PASS_CHECKPOINT_SOURCE'])
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)


def delivery_client():
    def create(**kwargs):
        user = kwargs['messages'][-1]['content']
        match = re.search(r'\[\{"n":', user)
        if not match:
            raise AssertionError('Only real delivery requests are expected: ' + user)
        entries = json.JSONDecoder().raw_decode(user[match.start():])[0]
        payload = [{'n':entry['n'], 'head':' '.join(entry['text'].split()[:3]), 'instruct':'Neutral.'} for entry in entries]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)), finish_reason='stop')], usage=None)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class DiagnosticCheckpointReplayTests(unittest.TestCase):
    def test_native_process_death_after_second_delta_resumes_without_replaying_prefix(self):
        source = 'First paragraph.\n\nSecond paragraph.\n\nThird paragraph.'
        chunks = [r['text'] for r in tp.split_into_chunk_records(source, max_size=18)]
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'book.json')
            child = '''
import os,sys
from unittest.mock import patch
import three_pass_generate as tp
from generate_script import LLMGenParams
from generation_checkpoint_deltas import GenerationCheckpointDeltas
from tests.test_three_pass_checkpoint_recovery import delivery_client
original=GenerationCheckpointDeltas.save_checkpoint
def save(self,data,**kwargs):
    original(self,data,**kwargs)
    if data['stage']=='segment' and data['chunks_done']==2:os._exit(93)
def segment(client,model,chunk,params,**kwargs):return [{'type':'NARRATOR','text':chunk}]
with patch.object(GenerationCheckpointDeltas,'save_checkpoint',save),patch.object(tp,'segment_chunk_adaptively',segment):
    tp.run_three_pass(delivery_client(),'fixture',sys.argv[2],LLMGenParams(max_tokens=500,segmentation='llm',structured_output='off'),18,output_path=sys.argv[1])
'''
            stopped = subprocess.run([sys.executable, '-c', child, output, source],
                                     capture_output=True, text=True, timeout=15)
            self.assertEqual(stopped.returncode, 93, stopped.stdout + stopped.stderr)
            replayed = []
            def segment(client, model, chunk, params, **kwargs):
                replayed.append(chunk)
                return [{'type': 'NARRATOR', 'text': chunk}]
            with patch.object(tp, 'segment_chunk_adaptively', side_effect=segment),\
                 contextlib.redirect_stdout(io.StringIO()):
                result = tp.run_three_pass(delivery_client(), 'fixture', source,
                    LLMGenParams(max_tokens=500, segmentation='llm', structured_output='off'),
                    18, output_path=output)
            self.assertEqual(replayed, chunks[2:])
            self.assertEqual([entry['text'] for entry in result], chunks)
            checkpoint = json.loads(Path(tp.three_pass_checkpoint_path(output)).read_text())
            self.assertEqual(checkpoint['annotated'], result)
            self.assertEqual(checkpoint['stage'], 'done')

    def test_failed_middle_chunk_is_retried_and_tail_is_reassembled_without_duplicates(self):
        source = 'First paragraph.\n\nSecond paragraph.\n\nThird paragraph.'
        chunks = [r['text'] for r in tp.split_into_chunk_records(source, max_size=18)]
        self.assertEqual(3, len(chunks))
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as output:
                path = str(Path(tmp) / 'book.json')
                params = LLMGenParams(max_tokens=500, segmentation='llm', structured_output='off')
                first_calls, replay_calls = [], []
                def initial(client, model, chunk, params, **kwargs):
                    first_calls.append(chunk)
                    if chunk == chunks[1]:
                        kwargs['failure_sink'].append({'low_source_token_recall'})
                        return []
                    return [{'type':'NARRATOR', 'text':chunk}]
                with patch.object(tp, 'segment_chunk_adaptively', side_effect=initial):
                    partial = tp.run_three_pass(delivery_client(), 'fixture', source, params, 18,
                                               output_path=path, collect_all_failures=True)
                checkpoint_path = Path(tp.three_pass_checkpoint_path(path))
                checkpoint = json.loads(checkpoint_path.read_text())
                self.assertEqual(chunks, first_calls)
                self.assertEqual([chunks[0],chunks[2]], [entry['text'] for entry in partial])
                self.assertEqual(3, checkpoint['chunks_done'])
                if legacy:
                    checkpoint.pop('diagnostic_segment_resume', None)
                    checkpoint_path.write_text(json.dumps(checkpoint))
                def repaired(client, model, chunk, params, **kwargs):
                    replay_calls.append(chunk)
                    return [{'type':'NARRATOR', 'text':chunk}]
                with patch.object(tp, 'segment_chunk_adaptively', side_effect=repaired):
                    result = tp.run_three_pass(delivery_client(), 'fixture', source, params, 18,
                                              output_path=path, collect_all_failures=True)
                final_checkpoint = json.loads(checkpoint_path.read_text())
                manifest = json.loads(Path(tp.three_pass_manifest_path(path)).read_text())
                self.assertEqual(chunks if legacy else chunks[1:], replay_calls)
                self.assertEqual(chunks, [entry['text'] for entry in result])
                self.assertEqual(result, final_checkpoint['annotated'])
                self.assertEqual([], final_checkpoint['diagnostic_failures'])
                self.assertEqual('complete', manifest['status'])
                self.assertEqual(3, manifest['progress']['chunks_completed'])
                self.assertIn('retrying segmentation from chunk 1' if legacy else 'retaining 1 accepted source chunks', output.getvalue())


class CheckpointShapeTests(unittest.TestCase):
    def test_emitted_attribute_incomplete_stage_can_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'book.json')
            state = self.valid_state({'settings': 'wanted'})
            state.update(stage='attribute_incomplete', named=[None], annotated=[],
                         diagnostic_failures=[{'pass': 'attribute'}])
            Path(tp.three_pass_checkpoint_path(output)).write_text(json.dumps(state))
            self.assertEqual(tp._load_three_pass_checkpoint(output, state['fingerprint']), state)

    def valid_state(self, fingerprint):
        return {'fingerprint':fingerprint, 'stage':'done', 'chunks_done':1,
                'segmented':[{'type':'NARRATOR', 'text':'Target.'}],
                'named':[{'speaker':'NARRATOR', 'text':'Target.'}],
                'annotated':[{'speaker':'NARRATOR', 'text':'Target.', 'instruct':'Neutral.'}],
                'resolutions':['clean'], 'elapsed_s':{'segment':1.5}, 'diagnostic_failures':[]}

    def test_matching_invalid_state_is_rejected_without_dispatch_or_overwrite(self):
        params = LLMGenParams(max_tokens=500)
        fingerprint = tp.three_pass_fingerprint('Target.', 'fixture', 3000, params)
        mutations = [
            ('stage', []), ('stage', 'unknown'), ('chunks_done', '1'), ('chunks_done', -1), ('chunks_done', True), ('chunks_done', 2),
            ('segmented', -1), ('segmented', [{'type':[], 'text':'Target.'}]), ('segmented', [{'type':'NARRATOR','text':3}]),
            ('named', [{},{}]), ('named', [{'speaker':'NARRATOR', 'text':'Wrong.'}]),
            ('annotated', [{},{}]), ('annotated', [{'speaker':'NARRATOR','text':'Target.'}]),
            ('resolutions', {}), ('resolutions', ['clean'] * 3), ('resolutions', [3]),
            ('elapsed_s', {'segment':-1}), ('elapsed_s', {'segment':float('nan')}), ('elapsed_s', {'segment':'1'}),
            ('diagnostic_failures', None), ('diagnostic_failures', [{}]), ('diagnostic_failures',[{'pass':'segment','chunk':2}]),
            ('model_binding', []), ('failed', []), ('diagnostic_segment_resume', {'chunks_done':2,'segmented_entries':1}),
            ('diagnostic_segment_resume', {'chunks_done':0,'segmented_entries':1}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'book.json')
            path = Path(tp.three_pass_checkpoint_path(output))
            for key, value in mutations:
                with self.subTest(key=key, value=value):
                    state = self.valid_state(fingerprint); state[key] = value
                    raw = json.dumps(state); path.write_text(raw)
                    with patch.object(tp, 'segment_chunk_adaptively') as segment, patch.object(tp, 'attribute_batch_voted') as attribute, patch.object(tp, 'instruct_batch') as instruct:
                        with self.assertRaisesRegex(RuntimeError, 'Invalid three-pass checkpoint'):
                            tp.run_three_pass(None, 'fixture', 'Target.', params, 3000, output_path=output)
                        segment.assert_not_called(); attribute.assert_not_called(); instruct.assert_not_called()
                    self.assertEqual(raw, path.read_text())
                    self.assertEqual([path], list(Path(tmp).iterdir()))

    def test_loader_rejects_matching_schema_errors_before_returning_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'book.json'); path = Path(tp.three_pass_checkpoint_path(output))
            for key, value in (('chunks_done','1'), ('segmented',-1), ('named',[{},{}])):
                with self.subTest(key=key):
                    state = self.valid_state({'settings':'wanted'}); state[key] = value
                    raw = json.dumps(state); path.write_text(raw)
                    with self.assertRaisesRegex(RuntimeError, 'Invalid three-pass checkpoint'):
                        tp._load_three_pass_checkpoint(output, state['fingerprint'])
                    self.assertEqual(raw, path.read_text())

    def test_unreadable_payload_fails_clearly_and_foreign_fingerprint_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'book.json'); path = Path(tp.three_pass_checkpoint_path(output))
            for raw in ('{bad', '[]', 'null'):
                path.write_text(raw)
                with self.assertRaisesRegex(RuntimeError, str(path)):
                    tp._load_three_pass_checkpoint(output, {'settings':'wanted'})
                self.assertEqual(raw, path.read_text())
            path.write_text(json.dumps({'fingerprint':{'settings':'different'}, 'segmented':-1}))
            self.assertIsNone(tp._load_three_pass_checkpoint(output, {'settings':'wanted'}))

    def test_valid_legacy_and_pending_entry_shapes_still_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'book.json'); path = Path(tp.three_pass_checkpoint_path(output))
            state = self.valid_state({'settings':'wanted'})
            for key in ('resolutions','elapsed_s','diagnostic_failures'):
                state.pop(key)
            state['stage'] = 'attribute'; state['named'] = [None]; state['annotated'] = []
            expected = copy.deepcopy(state); path.write_text(json.dumps(state))
            self.assertEqual(expected, tp._load_three_pass_checkpoint(output, state['fingerprint']))
            self.assertEqual(expected, state)
