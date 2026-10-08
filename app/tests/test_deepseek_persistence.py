"""HTTP refusals preserve persisted books, datasets and voice collections."""
import contextlib
import core
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import dataset_builder as db, script as reports, scripts_library as library, voices
from review_report import get_review_report_info
from tests import test_dataset_builder_regressions as builder_tests

class PersistenceTests(unittest.TestCase):
    def test_explicit_bad_reference_refused_without_replacing_dataset(self):
        for status in ('error','pending'):
            for index in (1,99):
                with self.subTest(status=status,index=index),tempfile.TemporaryDirectory() as tmp:
                    builder,work,output=builder_tests.DatasetReferenceIndexTests()._prepare(Path(tmp))
                    state=json.loads((work/'state.json').read_text());state['samples'][1]['status']=status;(work/'state.json').write_text(json.dumps(state))
                    prior=output/'voice';prior.mkdir(parents=True);(prior/'prior.txt').write_text('prior complete dataset')
                    app=FastAPI();app.include_router(db.router)
                    with patch.object(db,'DATASET_BUILDER_DIR',str(builder)),patch.object(db,'LORA_DATASETS_DIR',str(output)),patch.object(db,'process_state',{'dataset_builder':{'running':False}}),TestClient(app) as client:
                        response=client.post('/api/dataset_builder/save',json={'name':'voice','ref_index':index})
                    self.assertEqual(400,response.status_code,response.text);self.assertIn('reference',response.json()['detail'].lower())
                    self.assertEqual(['prior.txt'],[p.name for p in prior.iterdir()]);self.assertEqual('prior complete dataset',(prior/'prior.txt').read_text())
        for index in (None,0,1):
            with self.subTest(valid=index),tempfile.TemporaryDirectory() as tmp:
                builder,work,output=builder_tests.DatasetReferenceIndexTests()._prepare(Path(tmp));app=FastAPI();app.include_router(db.router)
                payload={'name':'voice'}
                if index is not None: payload['ref_index']=index
                with patch.object(db,'DATASET_BUILDER_DIR',str(builder)),patch.object(db,'LORA_DATASETS_DIR',str(output)),patch.object(db,'process_state',{'dataset_builder':{'running':False}}),TestClient(app) as client:
                    response=client.post('/api/dataset_builder/save',json=payload)
                self.assertEqual(200,response.status_code,response.text)
                self.assertEqual((work/f'sample_{(index or 0):03d}.wav').read_bytes(),(output/'voice/ref.wav').read_bytes())

    def test_reports_with_identical_clock_keep_both_native_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(reports,'REPORTS_DIR',tmp),patch.object(reports.time,'strftime',return_value='same-second'):
            paths=[]
            for name in ('first_book','second_book'):
                state={'tasks':[{'name':name,'status':'done','stats_fwd':reports._new_review_totals()}],'totals_fwd':reports._new_review_totals(),'aliases_fwd':[],'diff_pool':{'text':[],'speaker':[]}}
                paths.append(Path(reports._write_batch_review_report(state,[name,'other'],False,False)))
            self.assertNotEqual(paths[0],paths[1]);self.assertEqual(2,len(list(Path(tmp).glob('*.md'))))
            for path,name in zip(paths,('first_book','second_book')):
                self.assertIn(name,path.read_text());self.assertTrue(get_review_report_info(path)['sha256'])

    def test_corrupt_and_wrong_shape_books_refuse_without_publishing(self):
        for data in (b'{not json',b'\xff',b'{}'):
            with self.subTest(data=data),tempfile.TemporaryDirectory() as tmp,contextlib.ExitStack() as stack:
                root=Path(tmp);saved=root/'scripts';saved.mkdir();active=root/'annotated_script.json';voice=root/'voice_config.json';chunk=root/'chunks.json'
                active.write_bytes(data);(saved/'bad.json').write_bytes(data);dest=saved/'book.json';dest.write_text('[{"text":"prior"}]');before=dest.read_bytes()
                for module in (library, core):
                    for key,value in {'DATA_DIR':root,'SCRIPTS_DIR':saved,'SCRIPT_PATH':active,'VOICE_CONFIG_PATH':voice,'CHUNKS_PATH':chunk,'AUDIOBOOK_PATH':root/'book.aup3','M4B_PATH':root/'book.m4b'}.items():
                        stack.enter_context(patch.object(module,key,str(value)))
                state=copy.deepcopy(library.process_state)
                for entry in state.values():entry['running']=False
                stack.enter_context(patch.object(library,'process_state',state));app=FastAPI();app.include_router(library.router);client=stack.enter_context(TestClient(app,raise_server_exceptions=False))
                for route,name,code in (('save','book',422),('load','bad',400)):
                    response=client.post('/api/scripts/'+route,json={'name':name});self.assertEqual(code,response.status_code,response.text);self.assertIn('script',response.json()['detail'].lower())
                self.assertEqual(data,active.read_bytes());self.assertEqual(before,dest.read_bytes())
                active.write_text('[{"speaker":"ALICE","text":"valid"}]');response=client.post('/api/scripts/save',json={'name':'good'});self.assertEqual(200,response.status_code,response.text)
                response=client.post('/api/scripts/load',json={'name':'good'});self.assertEqual(200,response.status_code,response.text)

    def test_partial_voice_save_preserves_omitted_collections_and_explicit_clear(self):
        for guarded in (False,True):
            with self.subTest(guarded=guarded),tempfile.TemporaryDirectory() as tmp,contextlib.ExitStack() as stack:
                root=Path(tmp);script=root/'annotated_script.json';script.write_text('[{"speaker":"ALICE","text":"Hello."}]');path=root/'voice_config.json'
                original={'type':'custom','voice':'Ryan','alias_of':'BOB','ready':True,'versions':{'v1':{'voice':'Jenny'}},'candidates':[{'voice':'Ryan'}],'style_timeline':[{'from_index':0,'character_style':'old'}],'version_timeline':[{'from_index':0,'version_id':'v1'}]}
                path.write_text(json.dumps({'ALICE':original}));stack.enter_context(patch.object(voices,'VOICE_CONFIG_PATH',str(path)));stack.enter_context(patch.object(voices,'SCRIPT_PATH',str(script)));app=FastAPI();app.include_router(voices.router);client=stack.enter_context(TestClient(app))
                def save(changes):
                    if not guarded:return client.post('/api/save_voice_config',json=changes)
                    snapshot=client.get('/api/voice_config/snapshot').json();return client.post('/api/voice_config/save',json={'revision':snapshot['revision'],'book_token':snapshot['book_token'],'voices':changes})
                response=save({'ALICE':{'voice':'Aiden'}});self.assertEqual(200,response.status_code,response.text);current=json.loads(path.read_text())['ALICE'];self.assertEqual('Aiden',current['voice'])
                for key in ('alias_of','ready','versions','candidates','style_timeline','version_timeline'):self.assertEqual(original[key],current[key])
                response=save({'ALICE':{'alias_of':None,'ready':False,'versions':{},'candidates':[],'style_timeline':[],'version_timeline':[]},'BOB':{'voice':'Ryan'}});self.assertEqual(200,response.status_code,response.text);current=json.loads(path.read_text());self.assertIsNone(current['ALICE']['alias_of']);self.assertFalse(current['ALICE']['ready']);self.assertEqual({},current['ALICE']['versions']);self.assertEqual([],current['ALICE']['candidates']);self.assertEqual('custom',current['BOB']['type']);self.assertEqual({},current['BOB']['versions'])

    def test_narrator_strategy_resolves_legacy_without_merging_other_characters(self):
        for modern in (False,True):
            with self.subTest(modern=modern),tempfile.TemporaryDirectory() as tmp,contextlib.ExitStack() as stack:
                root=Path(tmp);script=root/'annotated_script.json';path=root/'voice_config.json';key='NARRATOR' if modern else 'Narrator';script.write_text(json.dumps([{('speaker' if modern else 'type'):key,'text':'Hello.'},{'speaker':'narrator','text':'distinct character'}]));original={key:{'voice':'Ryan'},'narrator':{'voice':'Jenny'}};path.write_text(json.dumps(original))
                stack.enter_context(patch.object(voices,'SCRIPT_PATH',str(script)));stack.enter_context(patch.object(voices,'VOICE_CONFIG_PATH',str(path)));app=FastAPI();app.include_router(voices.router);client=stack.enter_context(TestClient(app))
                response=client.post('/api/narrator/strategy',json={'strategy':'focus'});self.assertEqual(200,response.status_code,response.text);saved=json.loads(path.read_text());self.assertEqual('focus',saved[key]['narrator_strategy']);self.assertEqual(original['narrator'],saved['narrator'])
                before=path.read_bytes();response=client.post('/api/narrator/strategy',json={'strategy':'global','book_token':'0'*64});self.assertEqual(409,response.status_code);self.assertEqual(before,path.read_bytes())
                response=client.post('/api/narrator/preview',json={'strategy':'global'});self.assertEqual(200,response.status_code,response.text)
