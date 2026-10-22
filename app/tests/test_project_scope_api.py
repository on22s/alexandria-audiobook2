"""Real HTTP scope transitions bind previews and refuse stale/busy operations."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import project_scope, script
from tests import test_project_scope_failures as fixtures


class ScopeAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();fixtures.get_fixture(self.root)
        stack=ExitStack();self.addCleanup(stack.close)
        for module,name,value in [(project_scope,'DATA_DIR',str(self.root)),
                (script,'DATA_DIR',str(self.root)),(script,'SCRIPT_PATH',str(self.root/'annotated_script.json'))]:
            stack.enter_context(patch.object(module,name,value))
        stack.enter_context(patch('core.DATA_DIR',str(self.root)))
        stack.enter_context(patch('core.process_state',{}))
        app=FastAPI();app.include_router(project_scope.router);app.include_router(script.router)
        self.client=stack.enter_context(TestClient(app))

    def test_preview_apply_generation_fence_and_exact_undo_through_http(self):
        before=fixtures.get_bytes(self.root)
        choices=self.client.get('/api/project/scope/entries?search=Retained&offset=1')
        self.assertEqual(200,choices.status_code,choices.text)
        self.assertEqual([1],[row['start_entry'] for row in choices.json()['entries']])
        self.assertEqual(422,self.client.post('/api/project/scope/preview',json={'start_entry':True}).status_code)
        response=self.client.post('/api/project/scope/preview',json={'start_entry':1})
        self.assertEqual(200,response.status_code,response.text);plan=response.json()
        self.assertEqual(['uid-0'],plan['removed_uids']);self.assertEqual('uid-1',plan['retained'][0]['uid'])
        self.assertTrue(plan['retained'][0]['audio_verified'])
        self.assertEqual(before,fixtures.get_bytes(self.root))
        self.assertEqual(409,self.client.post('/api/project/scope/apply',json={
            'start_entry':1,'expected_revision':'0'*64}).status_code)
        self.assertEqual(200,self.client.post('/api/project/scope/apply',json={
            'start_entry':1,'expected_revision':plan['revision']}).status_code)
        for route in ('/api/generate_script','/api/generate_script/retry'):
            response=self.client.post(route,json={})
            self.assertEqual(409,response.status_code,response.text)
            self.assertIn('shortened source scope',response.json()['detail'])
        status=self.client.get('/api/project/scope/undo').json();self.assertTrue(status['can_undo'])
        response=self.client.post('/api/project/scope/undo',json={'expected_revision':status['revision']})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(before,fixtures.get_bytes(self.root))

    def test_busy_requests_refuse_and_stale_undo_can_be_explicitly_discarded(self):
        with patch('core.process_state',{'audio':{'running':True}}):
            self.assertEqual(409,self.client.post('/api/project/scope/preview',json={'start_entry':1}).status_code)
        plan=self.client.post('/api/project/scope/preview',json={'start_entry':1}).json()
        self.client.post('/api/project/scope/apply',json={'start_entry':1,'expected_revision':plan['revision']}).raise_for_status()
        path=self.root/'source.txt';path.write_text(path.read_text()+' Changed.')
        before=fixtures.get_bytes(self.root);status=self.client.get('/api/project/scope/undo').json()
        self.assertTrue(status['available']);self.assertFalse(status['can_undo'])
        self.assertEqual(409,self.client.post('/api/project/scope/undo',json={'expected_revision':status['revision']}).status_code)
        self.assertEqual(before,fixtures.get_bytes(self.root))
        self.client.post('/api/project/scope/discard_undo',json={'expected_revision':status['revision']}).raise_for_status()
        after=fixtures.get_bytes(self.root);self.assertEqual({k:v for k,v in before.items()
            if k!='.project_scope_undo.json'},after)
