"""Native guarded row edits leave unrelated samples intact and reject stale writes."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from routers import dataset_builder as routes
from tests import test_dataset_builder_ownership as fixture


class DatasetRowEditTests(unittest.TestCase):
    fixture=fixture.DatasetBuilderOwnershipTests.fixture

    def payload(self, client, name='voice', index=0, text=' changed '):
        status=client.get('/api/dataset_builder/status/'+name).json()
        return {'name':name,'expected_count':len(status['samples']),
                'edits':[{'index':index,'expected_revision':status['row_revisions'][index],
                          'row':{'text':text,'emotion':'warm','seed':0}}]}

    def test_revision_instrument_and_native_artifact_changed_only_in_selected_sample(self):
        row={'text':'原文','seed':0,'status':'done','audio_url':'/a.wav'}
        token=routes.get_dataset_row_revision(row)
        self.assertEqual(token,routes.get_dataset_row_revision(dict(reversed(list(row.items())))))
        for field,value in [('text','other'),('seed',1),('status','pending'),('audio_url','/b.wav')]:
            self.assertNotEqual(token,routes.get_dataset_row_revision({**row,field:value}))
        with self.fixture() as (_,builder,work,_,states,api),TestClient(api) as client:
            path=work/'state.json';before=json.loads(path.read_text());before.update(description='preserved voice',global_seed='42');path.write_text(json.dumps(before));clips={p.name:p.read_bytes() for p in work.glob('*.wav')}
            request=self.payload(client);response=client.post('/api/dataset_builder/edit_rows',json=request)
            self.assertEqual(200,response.status_code,response.text)
            after=json.loads(path.read_text());self.assertEqual(before['samples'][1:],after['samples'][1:])
            self.assertEqual({k:v for k,v in before.items() if k!='samples'},{k:v for k,v in after.items() if k!='samples'})
            self.assertEqual({'text':'changed','emotion':'warm','seed':0,'status':'pending','audio_url':None},after['samples'][0])
            self.assertEqual(routes.get_dataset_row_revision(after['samples'][0]),response.json()['row_revisions']['0'])
            self.assertEqual(clips,{p.name:p.read_bytes() for p in work.glob('*.wav')});self.assertTrue(states['dataset_builder']['running'])

    def test_stale_count_revision_duplicate_bad_shapes_and_active_owner_preserve_bytes(self):
        with self.fixture() as (_,builder,work,_,states,api),TestClient(api) as client:
            request=self.payload(client);self.assertEqual(200,client.post('/api/dataset_builder/edit_rows',json=request).status_code)
            before=(work/'state.json').read_bytes()
            bad=[({**request,'edits':[]},422),(request,409),({**request,'expected_count':999},409),({**request,'edits':[request['edits'][0],request['edits'][0]]},400)]
            fresh=self.payload(client)
            for field,value,status in [('row',{'text':[]},400),('row',{'text':'valid','seed':True},400),('row',[],422),('expected_revision','bad',422),('index',999,409),('index',-1,422)]:
                bad.append(({**fresh,'edits':[{**fresh['edits'][0],field:value}]},status))
            for body,status in bad:
                with self.subTest(body=body):
                    response=client.post('/api/dataset_builder/edit_rows',json=body);self.assertEqual(status,response.status_code,response.text);self.assertEqual(before,(work/'state.json').read_bytes())
            active=(builder/'other/state.json').read_bytes();request=self.payload(client,'other');self.assertEqual(409,client.post('/api/dataset_builder/edit_rows',json=request).status_code)
            self.assertEqual(active,(builder/'other/state.json').read_bytes())

    def test_multi_edit_validates_all_before_write_and_unchanged_inputs_keep_audio(self):
        with self.fixture() as (_,builder,work,_,states,api),TestClient(api) as client:
            path=work/'state.json';before=path.read_bytes();request=self.payload(client)
            request['edits'].append({'index':1,'expected_revision':'0'*64,'row':{'text':'bad second'}})
            self.assertEqual(409,client.post('/api/dataset_builder/edit_rows',json=request).status_code);self.assertEqual(before,path.read_bytes())
            saved=json.loads(before);row=saved['samples'][0];request=self.payload(client)
            request['edits'][0]['row']={'text':row.get('text',''),'emotion':row.get('emotion',''),'seed':row.get('seed','')}
            response=client.post('/api/dataset_builder/edit_rows',json=request);self.assertEqual(200,response.status_code,response.text)
            after=json.loads(path.read_text())['samples'][0];self.assertEqual(row['status'],after['status']);self.assertEqual(row.get('audio_url'),after.get('audio_url'))
            response=client.post('/api/dataset_builder/update_rows',json={'name':'voice','rows':[{'text':'full'}]});self.assertEqual(200,response.status_code,response.text)
            status=client.get('/api/dataset_builder/status/voice').json();self.assertEqual(response.json()['row_revisions'],status['row_revisions'])

    def test_two_native_requests_with_same_revision_commit_once_under_existing_lock(self):
        with self.fixture() as (_,builder,work,_,states,api),TestClient(api) as client:
            first=self.payload(client,text='first');second=self.payload(client,text='second');entered,release=threading.Event(),threading.Event();original=routes._save_builder_state
            def held(name,state):
                entered.set()
                if not release.wait(3):raise AssertionError('owned save fixture timed out')
                return original(name,state)
            with patch.object(routes,'_save_builder_state',side_effect=held),ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(client.post,'/api/dataset_builder/edit_rows',json=first)
                try:
                    self.assertTrue(entered.wait(3));b=pool.submit(client.post,'/api/dataset_builder/edit_rows',json=second)
                finally:release.set()
                self.assertEqual(200,a.result(timeout=3).status_code);self.assertEqual(409,b.result(timeout=3).status_code)
            self.assertEqual('first',json.loads((work/'state.json').read_text())['samples'][0]['text'])
