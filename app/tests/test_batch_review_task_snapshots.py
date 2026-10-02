"""Actual HTTP status derives badges from authoritative per-pass review results."""
import copy
import importlib.util
import os
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import script

if os.environ.get('BATCH_REVIEW_SOURCE'):
    spec=importlib.util.spec_from_file_location('saved_review_router',os.environ['BATCH_REVIEW_SOURCE'])
    script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)


class BatchReviewTaskSnapshotTests(unittest.TestCase):
    def status(self,state):
        app=FastAPI();app.include_router(script.router)
        with patch.object(script,'process_state',{'batch_review':state}),TestClient(app) as client:
            response=client.get('/api/status/batch_review')
        self.assertEqual(200,response.status_code,response.text)
        return response.json()

    def test_status_discards_stale_generic_copies_and_keeps_pass_results(self):
        fwd={'total_changes':2,'batches_failed':1};bwd={'total_changes':3,'batches_failed':0}
        forward={'text_rewrites':[{'before':'a','after':'b'}],'speaker_changes':[]}
        backward={'text_rewrites':[],'speaker_changes':[{'before':'A','after':'B'}]}
        failure={'sections':[{'batch':1}],'checkpoint_retained':True}
        task={'name':'Book','status':'incomplete','stats_fwd':fwd,'stats_bwd':bwd,
              'diffs_fwd':forward,'diffs_bwd':backward,'failures_fwd':failure,
              'stats':{'total_changes':999},'diffs':{'text_rewrites':[]},
              'failures':{'sections':[{'batch':999}]}}
        state={'running':False,'bidirectional':True,'tasks':[task]};before=copy.deepcopy(state)
        result=self.status(state)['tasks'][0]
        self.assertEqual(5,result['stats']['total_changes']);self.assertFalse(result['stats']['partial'])
        self.assertEqual({'text_rewrites':forward['text_rewrites'],'speaker_changes':backward['speaker_changes']},result['diffs'])
        self.assertEqual(failure,result['failures'])
        self.assertEqual(before,state)
        self.assertEqual(fwd,result['stats_fwd']);self.assertEqual(bwd,result['stats_bwd'])

    def test_missing_backward_pass_is_partial_only_for_bidirectional_runs(self):
        task={'name':'Book','status':'done','stats_fwd':{'total_changes':2}}
        for bidirectional in (False,True):
            with self.subTest(bidirectional=bidirectional):
                state={'running':False,'bidirectional':bidirectional,'tasks':[task]}
                self.assertEqual(bidirectional,self.status(state)['tasks'][0]['stats']['partial'])
                self.assertNotIn('stats',task)

    def test_legacy_generic_only_state_keeps_existing_response(self):
        task={'name':'Old','status':'done','stats':{'total_changes':4},
              'diffs':{'text_rewrites':[]},'failures':{'sections':[]}}
        self.assertEqual(task,self.status({'running':False,'tasks':[task]})['tasks'][0])

    def test_successive_status_reads_follow_pass_updates_without_generic_writes(self):
        task={'name':'Book','status':'running','stats_fwd':{'total_changes':2},
              'diffs_fwd':{'text_rewrites':[{'before':'a','after':'b'}],'speaker_changes':[]},
              'failures_fwd':{'sections':[{'batch':1}]}}
        state={'running':False,'bidirectional':True,'tasks':[task]}
        first=self.status(state)['tasks'][0]
        self.assertEqual(2,first['stats']['total_changes']);self.assertTrue(first['stats']['partial'])
        task.update(stats_bwd={'total_changes':3},diffs_bwd={'text_rewrites':[],'speaker_changes':[]},
                    failures_bwd={'sections':[]},status='incomplete')
        before=copy.deepcopy(task)
        second=self.status(state)['tasks'][0]
        self.assertEqual(5,second['stats']['total_changes']);self.assertFalse(second['stats']['partial'])
        self.assertEqual(first['diffs'],second['diffs']);self.assertEqual(first['failures'],second['failures'])
        self.assertEqual(before,task)
        for field in ('stats','diffs','failures'):self.assertNotIn(field,task)
        detached=script.get_batch_review_task_snapshot(task,True)
        detached['diffs']['text_rewrites'][0]['before']='edited response'
        self.assertEqual(before,task)
