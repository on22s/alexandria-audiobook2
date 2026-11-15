"""Review edits annotations while retaining source facts through real model parsing."""
import copy
import itertools
import unittest
from generate_script import LLMGenParams
from review_script import review_batch, get_review_source_binding, get_review_label_binding, get_review_partial_label_retention
from tests.test_pass_boundary_retry_regressions import client_for

class ReviewMetadataEditsTests(unittest.TestCase):
    source = [{'speaker': 'ALICE', 'text': '  The bell rang.  ', 'instruct': '',
               'speaker_gender': 'male', 'speaker_age_group': 'child', 'speaker_ageless': False,
               'spoken': False, 'type': 'NARRATOR', 'source_span': [2, 16],
               'provenance': {'fixture': [True, 1]}, 'pause_after': 125}]
    edits = {'speaker_gender': 'female', 'speaker_age_group': 'adult', 'speaker_ageless': True,
             'spoken': True, 'type': 'SPOKEN'}

    def test_classic_and_index_review_apply_explicit_annotations(self):
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                reply={'speaker':'ALICE','instruct':'Softly.',**self.edits}
                reply.update({'n':0} if indexed else {'text':'The bell rang.'})
                client,calls=client_for([([reply],'stop')])
                before=copy.deepcopy(self.source)
                result=review_batch(client,'fixture',self.source,1,1,LLMGenParams(review_labels_only=indexed),max_retries=0)
                self.assertIsNotNone(result)
                self.assertEqual({**before[0],**self.edits,'instruct':'Softly.'},result[0])
                self.assertEqual(before,self.source)
                self.assertIn('speaker_age_group',calls[0]['messages'][0]['content'])

    def test_legacy_annotations_are_not_echoed_into_request_but_remain_source_bound(self):
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                source = [{**self.source[0], 'type': 'legacy_role', 'spoken': 0}]
                before = copy.deepcopy(source)
                reply = {'speaker': 'ALICE', 'instruct': 'Softly.', 'speaker_gender': 'female'}
                reply.update({'n': 0} if indexed else {'text': source[0]['text']})
                client, calls = client_for([([reply], 'stop')])
                result = review_batch(client, 'fixture', source, 1, 1,
                    LLMGenParams(review_labels_only=indexed), previous_tail=source, max_retries=0)
                self.assertEqual([{**source[0], 'instruct': 'Softly.', 'speaker_gender': 'female'}], result)
                prompt = calls[0]['messages'][1]['content']
                self.assertNotIn('legacy_role', prompt)
                self.assertNotIn('"spoken": 0', prompt)
                self.assertIn('"speaker_gender": "male"', prompt)
                self.assertEqual(before, source)

    def test_invalid_annotation_types_and_values_are_rejected_for_both_contracts(self):
        for key,value in [('spoken','true'),('spoken',1),('speaker_ageless',0),('speaker_gender','guess'),('speaker_age_group',[]),('type','UNKNOWN')]:
            for indexed in (False,True):
                with self.subTest(key=key,value=value,indexed=indexed):
                    row={'speaker':'ALICE','instruct':'',key:value}
                    row.update({'n':0} if indexed else {'text':self.source[0]['text']})
                    report=(get_review_label_binding(self.source,[row])[1] if indexed else get_review_source_binding(self.source,[row])[1])
                    self.assertFalse(report['passed'])

    def test_generated_annotations_preserve_exact_source_facts_and_input(self):
        for gender,age,spoken in itertools.product(('male','female','unknown'),('child','adult','unknown'),(False,True)):
            source=copy.deepcopy(self.source); before=copy.deepcopy(source)
            row={'speaker':'ALICE','text':source[0]['text'],'instruct':'',
                 'speaker_gender':gender,'speaker_age_group':age,'spoken':spoken,
                 'source_span':[-1,-1],'pause_after':0,'provenance':{},'invented':True}
            result,report=get_review_source_binding(source,[row])
            self.assertTrue(report['passed'])
            self.assertEqual({**before[0],'speaker_gender':gender,'speaker_age_group':age,'spoken':spoken},result[0])
            self.assertEqual(before,source)

    def test_annotation_only_partial_edits_are_retained_only_on_exact_unique_text(self):
        source=self.source+[{'speaker':'NARRATOR','text':'The clock stopped.','instruct':''}]
        reply=[{**source[0],**self.edits}]
        result,status=get_review_partial_label_retention(source,reply,False)
        self.assertIsNotNone(result)
        self.assertEqual({**source[0],**self.edits},result[0])
        self.assertEqual(source[1],result[1])
        self.assertEqual('review_partial_label_retention',status['event'])

    def test_null_clears_only_the_requested_annotation_and_omission_preserves_others(self):
        reply={'speaker':'ALICE','text':self.source[0]['text'],'instruct':'','speaker_gender':None}
        result,report=get_review_source_binding(self.source,[reply])
        self.assertTrue(report['passed'])
        self.assertEqual({k:v for k,v in self.source[0].items() if k!='speaker_gender'},result[0])

    def test_changed_speaker_explicit_traits_override_fallback_without_copying_source_facts(self):
        source=[*self.source,{'speaker':'BOB','text':'A second line.','instruct':'','speaker_gender':'male','source_span':[19,30]}]
        replies=[{'speaker':'BOB','text':source[0]['text'],'instruct':'','speaker_gender':'female'},source[1]]
        result,report=get_review_source_binding(source,replies)
        self.assertTrue(report['passed']);self.assertEqual('female',result[0]['speaker_gender'])
        self.assertEqual(source[0]['source_span'],result[0]['source_span'])
        self.assertNotIn('speaker_age_group',result[0])

    def test_rejects_malformed_metadata_then_retries_real_parser(self):
        bad=[{'speaker':'ALICE','text':self.source[0]['text'],'instruct':'','spoken':'true'}]
        good=[{**bad[0],'spoken':True}]
        client,calls=client_for([(bad,'stop'),(good,'stop')])
        records=[]
        result=review_batch(client,'fixture',self.source,1,1,LLMGenParams(),max_retries=1,attempt_observer=records.append)
        self.assertEqual(2,len(calls));self.assertEqual(True,result[0]['spoken'])
        self.assertEqual(['quality_rejected','accepted'],[row['outcome'] for row in records])

    def test_manual_transport_accepts_annotations_in_both_response_modes(self):
        import json,tempfile,io
        from contextlib import redirect_stdout
        from llm_provider import ManualClient
        from tests.test_manual_llm import _answer_when_pending
        for indexed in (False,True):
            with self.subTest(indexed=indexed),tempfile.TemporaryDirectory() as tmp,redirect_stdout(io.StringIO()):
                client=ManualClient(tmp);client.POLL_SECONDS=.01
                reply={'speaker':'ALICE','instruct':'',**self.edits}
                reply.update({'n':0} if indexed else {'text':self.source[0]['text']})
                answer=_answer_when_pending(client,json.dumps([reply]))
                try:
                    result=review_batch(client,'manual',self.source,1,1,LLMGenParams(review_labels_only=indexed),max_retries=0)
                finally:
                    answer.join(4)
                self.assertFalse(answer.is_alive())
                self.assertEqual({**self.source[0],**self.edits},result[0])


    def test_legacy_source_annotations_do_not_block_valid_partial_corrections(self):
        source = [{**self.source[0], 'type': 'legacy_role'},
                  {'speaker': 'BOB', 'text': 'A second synthetic sentence.', 'instruct': '', 'spoken': 0}]
        reply = [{'speaker': source[0]['speaker'], 'text': source[0]['text'],
                  'instruct': '', 'speaker_gender': 'female'}]
        result, status = get_review_partial_label_retention(source, reply, False)
        self.assertIsNotNone(result)
        self.assertEqual([{**source[0], 'speaker_gender': 'female'}, source[1]], result)
        self.assertEqual('review_partial_label_retention', status['event'])


    def test_partial_annotation_correction_distinguishes_legacy_integer_from_boolean(self):
        source = [{**self.source[0], 'spoken': 0},
                  {'speaker': 'BOB', 'text': 'A second synthetic sentence.', 'instruct': ''}]
        reply = [{'speaker': source[0]['speaker'], 'text': source[0]['text'],
                  'instruct': '', 'spoken': False}]
        result, status = get_review_partial_label_retention(source, reply, False)
        self.assertIsNotNone(result)
        self.assertIs(result[0]['spoken'], False)
        self.assertIs(type(source[0]['spoken']), int)
