"""Real cue CRUD, CAS, file transactions and owned ASGI cancellation."""
import asyncio
import copy
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
import core
import book_state_transaction as books
import sound_design_context as context
import sound_design_edit as edits
from sound_design_assets import SoundDesignAssetStore
from sound_design_registry import ACTIVE_CUES,get_sound_design_registry
from routers import sound_design as api
from tests import test_sound_design_context as fixtures

BASE = '/api/sound_design/cues'


class SoundDesignCueApiTests(unittest.IsolatedAsyncioTestCase):
    write = fixtures.SoundDesignContextTests.write
    files = fixtures.SoundDesignContextTests.files

    async def asyncSetUp(self):
        fixtures.SoundDesignContextTests.setUp(self)
        for module,name,value in ((api,'DATA_DIR',str(self.root)),(core,'DATA_DIR',str(self.root)),
                                   (core,'process_state',{})):
            guard=patch.object(module,name,value);guard.start();self.addCleanup(guard.stop)
        self.asset=SoundDesignAssetStore(self.root).import_asset('audio.wav')
        app=FastAPI();app.include_router(api.router)
        @app.get('/fixture-ping')
        async def ping():return {'ready':True}
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture')
        self.addAsyncCleanup(self.client.aclose)

    async def metadata(self):
        response=await self.client.get(BASE);self.assertEqual(200,response.status_code,response.text)
        return response.json()

    async def request(self,metadata=None,identity='cue'):
        value=metadata or await self.metadata()
        anchor=await self.client.get(BASE+'/anchor',params={
            'context_revision':value['context_revision'],'chunk_uid':self.row['uid']})
        self.assertEqual(200,anchor.status_code,anchor.text)
        return dict(context_revision=value['context_revision'],registry_revision=value['registry_revision'],
            anchor_revision=anchor.json()['anchor_revision'],cue_id=identity,parent_version=None,
            chunk_uid=self.row['uid'],asset_id=self.asset['asset_id'],asset_sha256=self.asset['sha256'],
            parameters=dict(boundary='start',offset_frames=0,frame_rate=24000,kind='effect',
                duration=dict(mode='native',frames=None,loop_crossfade_frames=None),
                mix=dict(gain_db=-12,duck_db=0,fade_in_frames=0,fade_out_frames=0)))

    def selection(self,metadata,version):
        return dict(context_revision=metadata['context_revision'],registry_revision=metadata['registry_revision'],
                    cue_id='cue',version=version)

    async def test_actual_create_revise_select_deselect_preserves_originals_and_history(self):
        original={name:(self.root/name).read_bytes() for name in ('source.txt','audio.wav','chunks.json','annotated_script.json')}
        payload=await self.request();response=await self.client.post(BASE+'/revise',json=payload)
        self.assertEqual(200,response.status_code,response.text);parent=response.json()['version']
        current=await self.metadata();payload=await self.request(current)
        payload['parent_version']=parent;payload['parameters']['mix']['gain_db']=-24
        second=await self.client.post(BASE+'/revise',json=payload)
        self.assertEqual(200,second.status_code,second.text);latest=second.json()['version']
        for version in (parent,None,latest):
            response=await self.client.post(BASE+'/select',json=self.selection(await self.metadata(),version))
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual(version,response.json()['version'])
            document=get_sound_design_registry((self.root/ACTIVE_CUES).read_bytes())
            self.assertEqual(2,len(document['versions']))
        for name,raw in original.items():self.assertEqual(raw,(self.root/name).read_bytes())

    async def test_public_metadata_and_errors_do_not_expose_paths_or_private_configuration(self):
        private='private-fixture-credential-value';self.write('config.json',{'api_key':private})
        response=await self.client.get(BASE);self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('no-store',response.headers['cache-control'])
        for secret in (private,str(self.root),'absolute_path','binding','api_key'):
            self.assertNotIn(secret,response.text)
        with patch.object(edits,'capture_sound_design_context',side_effect=ValueError(private+str(self.root))):
            failed=await self.client.get(BASE)
        self.assertEqual(409,failed.status_code)
        self.assertNotIn(private,failed.text);self.assertNotIn(str(self.root),failed.text)

    async def test_recorded_take_metadata_does_not_claim_current_audio_availability(self):
        self.audio.unlink()
        value=await self.metadata()
        self.assertTrue(value['chunks'][0]['has_recorded_take'])
        self.assertNotIn('has_selected_audio',value['chunks'][0])
        response=await self.client.get(BASE+'/anchor',params={
            'context_revision':value['context_revision'],'chunk_uid':self.row['uid']})
        self.assertEqual(409,response.status_code)

    async def test_anchor_only_review_refuses_late_source_metadata_and_audio_changes(self):
        capture=edits.capture_sound_design_anchor
        for name in ('source.txt','chunks.json','audio.wav'):
            path=self.root/name;original=path.read_bytes();value=await self.metadata()
            def change_after_anchor(*args,**kwargs):
                result=capture(*args,**kwargs);path.write_bytes(original+b' ');return result
            try:
                with patch.object(edits,'capture_sound_design_anchor',side_effect=change_after_anchor):
                    response=await self.client.get(BASE+'/anchor',params={
                        'context_revision':value['context_revision'],'chunk_uid':self.row['uid']})
                self.assertEqual(409,response.status_code,response.text)
                self.assertFalse((self.root/ACTIVE_CUES).exists())
            finally:path.write_bytes(original)

    async def test_asset_duration_errors_refuse_and_explicit_valid_loop_saves(self):
        changes=[{'duration':dict(mode='fixed',frames=2,loop_crossfade_frames=1)},
                 {'duration':dict(mode='fixed',frames=6,loop_crossfade_frames=None)},
                 {'duration':dict(mode='fixed',frames=6,loop_crossfade_frames=3)},
                 {'mix':dict(gain_db=0,duck_db=0,fade_in_frames=2,fade_out_frames=2)}]
        for change in changes:
            payload=await self.request();payload['parameters']['kind']='ambience';payload['parameters'].update(change)
            response=await self.client.post(BASE+'/revise',json=payload)
            self.assertEqual(409,response.status_code,response.text)
            self.assertFalse((self.root/ACTIVE_CUES).exists())
        payload=await self.request();payload['parameters'].update(kind='ambience',
            duration=dict(mode='fixed',frames=6,loop_crossfade_frames=1))
        response=await self.client.post(BASE+'/revise',json=payload)
        self.assertEqual(200,response.status_code,response.text)

    async def test_book_selection_generation_fences_requests_but_preserves_matching_history(self):
        saved=await self.client.post(BASE+'/revise',json=await self.request())
        self.assertEqual(200,saved.status_code,saved.text);version=saved.json()['version']
        old=await self.metadata()
        self.write('state.json',{**self.state,'book_generation':'explicitly-reselected'})
        stale=await self.client.post(BASE+'/select',json=self.selection(old,version))
        self.assertEqual(409,stale.status_code)
        current=await self.metadata()
        self.assertNotEqual(old['book_token'],current['book_token'])
        self.assertNotEqual(old['context_revision'],current['context_revision'])
        self.assertEqual(old['book'],current['book'])
        selected=await self.client.post(BASE+'/select',json=self.selection(current,version))
        self.assertEqual(200,selected.status_code,selected.text)

    async def test_historical_invalid_loop_can_be_excluded_but_not_selected(self):
        from sound_design_cues import get_sound_design_cue_version
        from sound_design_registry import get_sound_design_registry_bytes
        saved=await self.client.post(BASE+'/revise',json=await self.request())
        self.assertEqual(200,saved.status_code,saved.text)
        path=self.root/ACTIVE_CUES;document=get_sound_design_registry(path.read_bytes())
        recipe=document['versions'][0]['recipe'];recipe.update(kind='ambience',
            duration=dict(mode='fixed',frames=2,loop_crossfade_frames=1))
        historical=get_sound_design_cue_version('cue',recipe)
        document.update(versions=[historical],selected={'cue':historical['version']})
        path.write_bytes(get_sound_design_registry_bytes(document));before=path.read_bytes()
        response=await self.client.post(BASE+'/select',json=self.selection(await self.metadata(),historical['version']))
        self.assertEqual(409,response.status_code,response.text);self.assertEqual(before,path.read_bytes())
        response=await self.client.post(BASE+'/select',json=self.selection(await self.metadata(),None))
        self.assertEqual(200,response.status_code,response.text)
        result=get_sound_design_registry(path.read_bytes());self.assertEqual({},result['selected'])
        self.assertEqual([historical],result['versions'])

    async def test_asset_changed_during_final_context_recapture_never_publishes(self):
        saved=await self.client.post(BASE+'/revise',json=await self.request())
        self.assertEqual(200,saved.status_code,saved.text);version=saved.json()['version']
        pcm=self.root/'.sound_design_assets'/(self.asset['asset_id']+'.pcm')
        original=pcm.read_bytes();before=(self.root/ACTIVE_CUES).read_bytes()
        capture=edits.capture_sound_design_context
        for operation in ('revise','select'):
            calls=0
            def change_after_recapture(*args,**kwargs):
                nonlocal calls
                value=capture(*args,**kwargs);calls+=1
                if calls==2:
                    raw=bytearray(original);raw[-1]^=1;pcm.write_bytes(raw)
                return value
            payload=(await self.request(identity='second') if operation=='revise'
                     else self.selection(await self.metadata(),version))
            try:
                with patch.object(edits,'capture_sound_design_context',side_effect=change_after_recapture):
                    response=await self.client.post(BASE+'/'+operation,json=payload)
                self.assertEqual(409,response.status_code,response.text)
                self.assertEqual(before,(self.root/ACTIVE_CUES).read_bytes())
            finally:pcm.write_bytes(original)

    async def test_stale_context_and_changed_audio_refuse_before_registry_publication(self):
        payload=await self.request()
        raw=bytearray(self.audio.read_bytes());raw[-1]^=1;self.audio.write_bytes(raw)
        response=await self.client.post(BASE+'/revise',json=payload)
        self.assertEqual(409,response.status_code,response.text)
        self.assertFalse((self.root/ACTIVE_CUES).exists())
        payload=await self.request();self.write('annotated_script.json',[{**self.row,'text':'Changed words.'}])
        response=await self.client.post(BASE+'/revise',json=payload)
        self.assertEqual(409,response.status_code,response.text)
        self.assertFalse((self.root/ACTIVE_CUES).exists())

    async def test_hostile_body_fields_and_size_refuse_before_task_or_source_admission(self):
        good=await self.request();cases=[]
        for key,value in (('book',{'book_id':'injected'}),('selection',{}),('asset_id','../outside')):
            candidate=copy.deepcopy(good);candidate[key]=value;cases.append(json.dumps(candidate).encode())
        for value in (True,10**1000,float('nan')):
            candidate=copy.deepcopy(good);candidate['parameters']['frame_rate']=value
            cases.append(json.dumps(candidate).encode())
        cases.extend([b'{"context_revision":0,"context_revision":1}', b'['*1100+b']'*1100,b'\xff'])
        with patch.object(edits,'ensure_scope_admission',side_effect=AssertionError('admitted invalid body')):
            for raw in cases:
                response=await self.client.post(BASE+'/revise',content=raw)
                self.assertEqual(400,response.status_code,response.text)
            response=await self.client.post(BASE+'/revise',content=b' '*16385)
            self.assertEqual(413,response.status_code)
        self.assertFalse((self.root/ACTIVE_CUES).exists())

    async def test_busy_task_guard_refuses_cue_metadata_without_source_scan(self):
        with patch.object(core,'process_state',{'audio':{'running':True}}), \
                patch.object(edits,'capture_sound_design_context') as capture:
            response=await self.client.get(BASE)
            self.assertEqual(409,response.status_code,response.text);capture.assert_not_called()

    async def test_source_changed_during_final_anchor_read_cannot_publish_old_context(self):
        payload=await self.request();capture=edits.capture_sound_design_anchor;calls=0
        def change_at_last_anchor(*args,**kwargs):
            nonlocal calls
            anchor=capture(*args,**kwargs);calls+=1
            if calls==2:self.source.write_text('Source changed during final selected-audio verification.')
            return anchor
        with patch.object(edits,'capture_sound_design_anchor',side_effect=change_at_last_anchor):
            response=await self.client.post(BASE+'/revise',json=payload)
        self.assertEqual(409,response.status_code,response.text);self.assertEqual(2,calls)
        self.assertFalse((self.root/ACTIVE_CUES).exists())

    async def test_stale_historical_selection_refuses_but_explicit_deselection_still_works(self):
        saved=await self.client.post(BASE+'/revise',json=await self.request())
        self.assertEqual(200,saved.status_code,saved.text);version=saved.json()['version']
        self.write('annotated_script.json',[{**self.row,'text':'An explicitly edited script.'}])
        before=(self.root/ACTIVE_CUES).read_bytes()
        response=await self.client.post(BASE+'/select',json=self.selection(await self.metadata(),version))
        self.assertEqual(409,response.status_code,response.text)
        self.assertEqual(before,(self.root/ACTIVE_CUES).read_bytes())
        removed=await self.client.post(BASE+'/select',json=self.selection(await self.metadata(),None))
        self.assertEqual(200,removed.status_code,removed.text)
        document=get_sound_design_registry((self.root/ACTIVE_CUES).read_bytes())
        self.assertEqual({},document['selected']);self.assertEqual(1,len(document['versions']))

    async def test_competing_same_revision_edits_cannot_silently_overwrite_each_other(self):
        first=await self.request();second={**first,'cue_id':'other-cue'}
        responses=await asyncio.gather(self.client.post(BASE+'/revise',json=first),
                                       self.client.post(BASE+'/revise',json=second))
        self.assertEqual([200,409],sorted(r.status_code for r in responses))
        document=get_sound_design_registry((self.root/ACTIVE_CUES).read_bytes())
        self.assertEqual(1,len(document['versions']));self.assertEqual(1,len(document['selected']))

    async def test_failed_atomic_replacement_restores_previous_exact_registry(self):
        saved=await self.client.post(BASE+'/revise',json=await self.request());self.assertEqual(200,saved.status_code,saved.text)
        original=(self.root/ACTIVE_CUES).read_bytes();payload=await self.request()
        payload['parent_version']=saved.json()['version'];payload['parameters']['mix']['gain_db']=-24
        move=books._move;failed=False
        def fail_install(source,destination):
            nonlocal failed
            if not failed and Path(source).name.startswith('new-') and Path(destination).name==ACTIVE_CUES:
                failed=True;raise OSError('synthetic install failure')
            return move(source,destination)
        with patch.object(books,'_move',side_effect=fail_install):
            response=await self.client.post(BASE+'/revise',json=payload)
        self.assertEqual(409,response.status_code,response.text);self.assertTrue(failed)
        self.assertEqual(original,(self.root/ACTIVE_CUES).read_bytes())

    async def test_cancelled_async_request_waits_for_blocked_book_worker_and_keeps_loop_responsive(self):
        entered=threading.Event();release=threading.Event();source=context.get_scope_source_snapshot
        def blocked(*args,**kwargs):
            self.assertTrue(books.is_book_state_admitted(self.root));entered.set()
            if not release.wait(5):raise TimeoutError('fixture source wait')
            return source(*args,**kwargs)
        with patch.object(context,'get_scope_source_snapshot',side_effect=blocked):
            task=asyncio.create_task(self.client.get(BASE))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                ping=await self.client.get('/fixture-ping');self.assertEqual(200,ping.status_code)
                task.cancel();await asyncio.sleep(.05);self.assertFalse(task.done())
                competing=await self.client.get(BASE);self.assertEqual(409,competing.status_code)
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(200,(await self.client.get(BASE)).status_code)
