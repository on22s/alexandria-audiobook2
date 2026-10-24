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
        states = copy.deepcopy(core.process_state)
        for state in states.values(): state.update(running=False, cancel=False)
        for module,name,value in ((api,'DATA_DIR',str(self.root)),(core,'DATA_DIR',str(self.root)),
                                   (core,'process_state',states),(core,'_task_claims',{}),(core,'_gpu_leases',{})):
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


RENDITIONS = '/api/sound_design/renditions'


class SoundRenditionApiTests(unittest.IsolatedAsyncioTestCase):
    """Actual owned CPU record/consume and metadata cleanup; no model or GPU."""
    write = fixtures.SoundDesignContextTests.write
    files = fixtures.SoundDesignContextTests.files
    metadata = SoundDesignCueApiTests.metadata
    request = SoundDesignCueApiTests.request

    async def asyncSetUp(self):
        await SoundDesignCueApiTests.asyncSetUp(self)
        guard = patch.object(core, 'acquire_gpu_lock', side_effect=AssertionError('GPU claimed'))
        guard.start(); self.addCleanup(guard.stop)
        import wave
        with wave.open(str(self.audio), 'wb') as stream:
            stream.setparams((1, 2, 24000, 4800, 'NONE', 'not compressed'))
            stream.writeframes(b'\x00\x20' * 4800)

    async def history(self):
        response = await self.client.get(RENDITIONS)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def common(self, value):
        return {key: value[key] for key in ('context_revision', 'registry_revision', 'ledger_revision')}

    async def record_request(self, *, source=None, **changes):
        if source is None:
            authored = await self.request()
            created = await self.client.post(BASE+'/revise', json=authored)
            self.assertEqual(200, created.status_code, created.text)
            source = dict(cue_id='cue', version=created.json()['version'])
        row = SoundDesignAssetStore(self.root).list_assets()['assets'][0]
        payload = dict(**self.common(await self.history()), source=source,
            composition=dict(scaffold_sha256=None, arrangement_sha256=None, family_id=None),
            origin=dict(kind='imported_audio', renderer=None), derivation=dict(kind='import', parent=None),
            master=dict(asset_id=row['asset_id'], source_sha256=row['source']['sha256'],
                        canonical_sha256=row['canonical']['sha256']), stems=[])
        payload.update(changes)
        return payload

    async def record(self, payload):
        response = await self.client.post(RENDITIONS+'/record', json=payload)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()['rendition_id']

    async def consume_request(self, identity):
        value = await self.history(); cue_view = await self.metadata()
        authored = await self.request(cue_view)
        return dict(**self.common(value), cue_id='cue', rendition_id=identity,
            parent_version=cue_view['registry']['selected']['cue'], chunk_uid=self.row['uid'],
            anchor_revision=authored['anchor_revision'], parameters=authored['parameters'])

    async def test_owned_capture_consume_historical_source_and_keep_preserve_exact_provenance(self):
        from audio_generation_owner import get_audio_generation_owner
        import sound_design_rendition_ops as operations
        from sound_design_renditions import ACTIVE_RENDITIONS, get_sound_rendition_ledger
        payload = await self.record_request(); observed = []
        original = operations.ensure_sound_rendition_assets
        def observe(*args, **kwargs):
            observed.append(get_audio_generation_owner())
            return original(*args, **kwargs)
        with patch.object(operations, 'ensure_sound_rendition_assets', side_effect=observe), \
                patch.object(core, 'reserve_background_task', wraps=core.reserve_background_task) as reserve, \
                patch.object(core.project_manager, 'get_engine', side_effect=AssertionError('model initialization')):
            identity = await self.record(payload)
        self.assertTrue(observed); self.assertTrue(all(owner is not None for owner in observed))
        self.assertTrue(reserve.call_args.kwargs['cpu_only'])
        before = (self.root/ACTIVE_RENDITIONS).read_bytes()
        record = get_sound_rendition_ledger(before)['records'][0]
        self.source.write_text('An explicitly changed original source; the retained cue remains historical.')
        consumed = await self.client.post(RENDITIONS+'/consume', json=await self.consume_request(identity))
        self.assertEqual(200, consumed.status_code, consumed.text)
        self.assertEqual(before, (self.root/ACTIVE_RENDITIONS).read_bytes())
        cues = get_sound_design_registry((self.root/ACTIVE_CUES).read_bytes())
        current = cues['versions'][-1]
        self.assertEqual(2, current['schema']); self.assertEqual(identity, current['recipe']['rendition_id'])
        self.assertNotEqual(record['source']['book']['source_sha256'], current['recipe']['book']['source_sha256'])
        value = await self.history()
        self.assertEqual([identity], value['draft_selected']); self.assertFalse(value['approval_checked'])
        candidate = await self.client.post('/api/sound_design/candidates/render',
            json={'context_revision': value['context_revision']})
        self.assertEqual(200, candidate.status_code, candidate.text)
        preview = await self.client.post(RENDITIONS+'/cleanup_preview',
            json=dict(**self.common(await self.history()), rendition_ids=[identity]))
        self.assertEqual(200, preview.status_code, preview.text)
        self.assertIn('retained_candidate', preview.json()['rows'][0]['reasons'])
        with patch('sound_design_assets.SoundDesignAssetStore.open_verified_asset', side_effect=AssertionError('metadata read audio')):
            kept = await self.client.post(RENDITIONS+'/keep', json=dict(**self.common(value), rendition_id=identity, keep=True))
            self.assertEqual(200, kept.status_code, kept.text)
            listed = await self.history()
        self.assertEqual(record, listed['records'][0]); self.assertEqual([identity], listed['keep'])
        self.assertFalse(listed['approval_checked'])
        self.assertFalse(core.process_state['audio']['running'])
        # Manual timing/level revisions preserve lineage while the master stays
        # the same. Choosing a different asset explicitly starts a legacy recipe
        # without erasing the prior consuming revisions or immutable record.
        view = await self.metadata(); edit = await self.request(view)
        edit['parent_version'] = view['registry']['selected']['cue']
        edit['parameters']['mix']['gain_db'] = -18
        changed = await self.client.post(BASE+'/revise', json=edit)
        self.assertEqual(200, changed.status_code, changed.text)
        revised = get_sound_design_registry((self.root/ACTIVE_CUES).read_bytes())['versions'][-1]
        self.assertEqual(2, revised['schema']); self.assertEqual(identity, revised['recipe']['rendition_id'])
        alternative = self.root/'alternative.wav'; alternative.write_bytes(self.audio.read_bytes())
        other = SoundDesignAssetStore(self.root).import_asset(alternative.name)
        view = await self.metadata(); edit = await self.request(view)
        edit.update(parent_version=view['registry']['selected']['cue'], asset_id=other['asset_id'],
                    asset_sha256=other['sha256'])
        changed = await self.client.post(BASE+'/revise', json=edit)
        self.assertEqual(200, changed.status_code, changed.text)
        retained = get_sound_design_registry((self.root/ACTIVE_CUES).read_bytes())['versions']
        self.assertEqual(1, retained[-1]['schema']); self.assertNotIn('rendition_id', retained[-1]['recipe'])
        self.assertTrue(any(row['schema'] == 2 and row['recipe']['rendition_id'] == identity for row in retained))
        self.assertEqual(record, (await self.history())['records'][0])

    async def test_bad_shape_stale_context_and_missing_stem_refuse_without_partial_history(self):
        from sound_design_renditions import ACTIVE_RENDITIONS
        import sound_design_rendition_ops as operations
        payload = await self.record_request()
        for change in ({'unknown': 'private-do-not-return'}, {'master': {**payload['master'], 'path': str(self.root)}},
                       {'stems': [dict(stem_id='s', label='Stem', **payload['master'], offset_frames=True)]}):
            bad = {**payload, **change}
            with patch.object(core, 'reserve_background_task') as reserve:
                response = await self.client.post(RENDITIONS+'/record', json=bad)
            self.assertEqual(400, response.status_code, response.text); reserve.assert_not_called()
            self.assertNotIn('private-do-not-return', response.text); self.assertNotIn(str(self.root), response.text)
        response = await self.client.post(RENDITIONS+'/record', content=b'x' * 16385)
        self.assertEqual(413, response.status_code); self.assertFalse((self.root/ACTIVE_RENDITIONS).exists())
        for change in ({'stems': [dict(stem_id='s', label='Stem', **payload['master'], offset_frames=3000000000)]},
                       {'derivation': dict(kind='processing', parent='f' * 64)}):
            with patch('sound_design_assets.SoundDesignAssetStore.open_verified_asset') as capture:
                refused = await self.client.post(RENDITIONS+'/record', json={**payload, **change})
            self.assertEqual(409, refused.status_code, refused.text)
            capture.assert_not_called()
            self.assertFalse((self.root/ACTIVE_RENDITIONS).exists())
        extra = self.root/'stem.wav'; extra.write_bytes(self.audio.read_bytes())
        store = SoundDesignAssetStore(self.root); stem = store.import_asset(extra.name)
        row = next(row for row in store.list_assets()['assets'] if row['asset_id'] == stem['asset_id'])
        payload['stems'] = [dict(stem_id='room', label='Room', asset_id=stem['asset_id'],
            source_sha256=row['source']['sha256'], canonical_sha256=row['canonical']['sha256'], offset_frames=-24)]
        identity = await self.record(payload); before = (self.root/ACTIVE_RENDITIONS).read_bytes()
        # Capacity is metadata, so reject a novel record before media admission.
        # An exact retry at capacity remains idempotent and still verifies audio.
        retry = {**payload, **self.common(await self.history())}
        novel = {**retry, 'composition': {**retry['composition'], 'family_id': 'new-at-capacity'}}
        for limits in ({'MAX_RENDITIONS': 1}, {'MAX_RENDITION_BYTES': len(before)}):
            with patch.multiple('sound_design_renditions', **limits), \
                    patch('sound_design_assets.SoundDesignAssetStore.open_verified_asset') as capture:
                refused = await self.client.post(RENDITIONS+'/record', json=novel)
            self.assertEqual(409, refused.status_code, refused.text); capture.assert_not_called()
            original_open = SoundDesignAssetStore.open_verified_asset
            with patch.multiple('sound_design_renditions', **limits), \
                    patch.object(SoundDesignAssetStore, 'open_verified_asset', autospec=True,
                                 side_effect=original_open) as verified:
                repeated = await self.record(retry)
            self.assertEqual(identity, repeated); self.assertTrue(verified.called)
        self.assertEqual(before, (self.root/ACTIVE_RENDITIONS).read_bytes())
        stale = await self.client.post(RENDITIONS+'/record', json=payload)
        self.assertEqual(409, stale.status_code); self.assertEqual(before, (self.root/ACTIVE_RENDITIONS).read_bytes())
        stem_pcm = store._path(stem['asset_id'], '.pcm')
        original_pcm = stem_pcm.read_bytes(); stem_pcm.unlink()
        self.assertEqual(identity, (await self.history())['records'][0]['rendition_id'])
        old_cues = (self.root/ACTIVE_CUES).read_bytes()
        response = await self.client.post(RENDITIONS+'/consume', json=await self.consume_request(identity))
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(old_cues, (self.root/ACTIVE_CUES).read_bytes())
        self.assertEqual(before, (self.root/ACTIVE_RENDITIONS).read_bytes())
        # A derived master still requires its parent's original and canonical
        # stem media, even when that stem is absent from the child itself.
        stem_pcm.write_bytes(original_pcm)
        child = await self.record(await self.record_request(source=payload['source'],
            derivation=dict(kind='processing', parent=identity)))
        consumed = await self.client.post(RENDITIONS+'/consume', json=await self.consume_request(child))
        self.assertEqual(200, consumed.status_code, consumed.text)
        current = await self.history()
        rendered = await self.client.post('/api/sound_design/candidates/render',
            json={'context_revision': current['context_revision']})
        self.assertEqual(200, rendered.status_code, rendered.text)
        retained = {path.name for path in (self.root/'.sound_design_renders').iterdir()}
        unchanged_cues = (self.root/ACTIVE_CUES).read_bytes()
        unchanged_ledger = (self.root/ACTIVE_RENDITIONS).read_bytes()
        source_path = store._path(stem['asset_id'], '.source')
        damaged = bytearray(source_path.read_bytes()); damaged[-1] ^= 1; source_path.write_bytes(damaged)
        failed = await self.client.post('/api/sound_design/candidates/render',
            json={'context_revision': current['context_revision']})
        self.assertEqual(409, failed.status_code, failed.text)
        self.assertEqual(retained, {path.name for path in (self.root/'.sound_design_renders').iterdir()})
        self.assertEqual(unchanged_cues, (self.root/ACTIVE_CUES).read_bytes())
        self.assertEqual(unchanged_ledger, (self.root/ACTIVE_RENDITIONS).read_bytes())
        self.assertEqual(2, len((await self.history())['records']))

    async def test_cleanup_protects_kept_parent_and_current_cue_and_retains_empty_ledger(self):
        from sound_design_renditions import ACTIVE_RENDITIONS, get_sound_rendition_ledger
        first_request = await self.record_request(); first = await self.record(first_request)
        child_request = await self.record_request(source=first_request['source'],
            derivation=dict(kind='processing', parent=first))
        child = await self.record(child_request)
        value = await self.history(); body = dict(**self.common(value), rendition_ids=[first, child])
        preview = await self.client.post(RENDITIONS+'/cleanup_preview', json=body)
        self.assertEqual(200, preview.status_code, preview.text)
        rows = preview.json()['rows']; self.assertIn('retained_child', rows[0]['reasons'])
        self.assertEqual([child], preview.json()['removable_ids'])
        before = (self.root/ACTIVE_RENDITIONS).read_bytes()
        refused = await self.client.post(RENDITIONS+'/cleanup', json={**body, 'impact_revision': preview.json()['impact_revision']})
        self.assertEqual(409, refused.status_code); self.assertEqual(before, (self.root/ACTIVE_RENDITIONS).read_bytes())
        kept = await self.client.post(RENDITIONS+'/keep', json=dict(**self.common(await self.history()), rendition_id=child, keep=True))
        self.assertEqual(200, kept.status_code, kept.text)
        kept_preview = await self.client.post(RENDITIONS+'/cleanup_preview',
            json=dict(**self.common(await self.history()), rendition_ids=[child]))
        self.assertEqual(200, kept_preview.status_code, kept_preview.text)
        self.assertIn('kept', kept_preview.json()['rows'][0]['reasons'])
        released = await self.client.post(RENDITIONS+'/keep', json=dict(**self.common(await self.history()), rendition_id=child, keep=False))
        self.assertEqual(200, released.status_code, released.text)
        for identity in (child, first):
            body = dict(**self.common(await self.history()), rendition_ids=[identity])
            preview = await self.client.post(RENDITIONS+'/cleanup_preview', json=body)
            self.assertEqual(200, preview.status_code, preview.text)
            removed = await self.client.post(RENDITIONS+'/cleanup', json={**body, 'impact_revision': preview.json()['impact_revision']})
            self.assertEqual(200, removed.status_code, removed.text)
        self.assertTrue((self.root/ACTIVE_RENDITIONS).is_file())
        self.assertEqual([], get_sound_rendition_ledger((self.root/ACTIVE_RENDITIONS).read_bytes())['records'])
        last = await self.record(await self.record_request(source=first_request['source']))
        response = await self.client.post(RENDITIONS+'/consume', json=await self.consume_request(last))
        self.assertEqual(200, response.status_code, response.text)
        body = dict(**self.common(await self.history()), rendition_ids=[last])
        preview = await self.client.post(RENDITIONS+'/cleanup_preview', json=body)
        self.assertEqual(200, preview.status_code, preview.text)
        self.assertIn('retained_cue_history', preview.json()['rows'][0]['reasons'])
        self.assertEqual([], preview.json()['removable_ids'])


    async def test_disconnect_retains_audio_owner_until_real_worker_exits_without_publishing(self):
        import sound_design_rendition_ops as operations
        from sound_design_renditions import ACTIVE_RENDITIONS
        payload = await self.record_request(); original_audio = self.audio.read_bytes()
        entered = threading.Event(); release = threading.Event()
        original = operations.ensure_sound_rendition_assets
        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(5):raise RuntimeError('Fixture did not release record worker')
            return original(*args, **kwargs)
        with patch.object(operations, 'ensure_sound_rendition_assets', side_effect=paused):
            task = asyncio.create_task(self.client.post(RENDITIONS+'/record', json=payload))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.assertEqual(200, (await self.client.get('/fixture-ping')).status_code)
                task.cancel(); await asyncio.sleep(.05)
                self.assertFalse(task.done()); self.assertTrue(core.process_state['audio']['running'])
                self.assertEqual(409, (await self.client.get(RENDITIONS)).status_code)
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(core.process_state['audio']['running']); self.assertEqual({}, core._task_claims)
        self.assertFalse((self.root/ACTIVE_RENDITIONS).exists())
        self.assertEqual(original_audio, self.audio.read_bytes())

    async def test_final_fence_and_serialization_cancellation_prevent_record_and_consume_publication(self):
        import sound_design_rendition_ops as operations
        from sound_design_renditions import ACTIVE_RENDITIONS
        from tts import ExportCancelled
        source_request = await self.record_request()
        for operation in ('record', 'consume'):
            if operation == 'consume':
                identity = await self.record(source_request)
            for cancel_point in ('fence', 'serialization'):
                with self.subTest(operation=operation, cancel_point=cancel_point):
                    payload = source_request if operation == 'record' else await self.consume_request(identity)
                    before_cues = (self.root/ACTIVE_CUES).read_bytes()
                    ledger_path = self.root/ACTIVE_RENDITIONS
                    before_ledger = ledger_path.read_bytes() if ledger_path.exists() else None
                    cancelled = False; calls = 0
                    name = ('require_sound_design_context_files_current' if cancel_point == 'fence'
                            else 'get_sound_rendition_ledger_bytes' if operation == 'record'
                            else 'get_sound_design_registry_bytes')
                    barrier = getattr(operations, name)
                    original = getattr(operations, '_' + operation)
                    def cancel_after_barrier(*args, **kwargs):
                        nonlocal calls, cancelled
                        result = barrier(*args, **kwargs); calls += 1
                        if calls == (2 if cancel_point == 'fence' else 1):
                            cancelled = True
                        return result
                    def run(root, snapshot, payload, check):
                        def active():
                            check()
                            if cancelled:
                                raise ExportCancelled('Controlled cancellation before publication')
                        return original(root, snapshot, payload, active)
                    with patch.object(operations, name, side_effect=cancel_after_barrier), \
                            patch.object(operations, '_' + operation, side_effect=run):
                        response = await self.client.post(RENDITIONS+'/'+operation, json=payload)
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertTrue(cancelled)
                    self.assertEqual(before_cues, (self.root/ACTIVE_CUES).read_bytes())
                    self.assertEqual(before_ledger, ledger_path.read_bytes() if ledger_path.exists() else None)
                    self.assertFalse(core.process_state['audio']['running'])

    async def test_cleanup_fences_candidate_topology_without_pcm_reads_or_encoder_probes(self):
        import os
        import sound_design_candidates as candidates
        import sound_design_rendition_cleanup as cleanup
        from tts import ExportCancelled
        from sound_design_renditions import ACTIVE_RENDITIONS
        identity = await self.record(await self.record_request())
        value = await self.history(); body = dict(**self.common(value), rendition_ids=[identity])
        before = await self.client.post(RENDITIONS+'/cleanup_preview', json=body)
        self.assertEqual(200, before.status_code, before.text)
        # No candidate stages means the proof loop has no per-stage checkpoint.
        # Cancellation at the final file fence must still preserve the ledger.
        original_ledger = (self.root/ACTIVE_RENDITIONS).read_bytes()
        cancelled = False; calls = 0
        fence = cleanup.require_sound_design_context_files_current
        operation = cleanup.run_sound_rendition_cleanup
        def cancel_after_fence(*args, **kwargs):
            nonlocal cancelled, calls
            result = fence(*args, **kwargs); calls += 1
            if calls == 2:
                cancelled = True
            return result
        def run(*args, **kwargs):
            check = kwargs['check_active']
            def active():
                check()
                if cancelled:
                    raise ExportCancelled('Controlled cleanup cancellation before publication')
            return operation(*args, **{**kwargs, 'check_active': active})
        with patch.object(cleanup, 'require_sound_design_context_files_current', side_effect=cancel_after_fence), \
                patch.object(cleanup, 'run_sound_rendition_cleanup', side_effect=run):
            stopped = await self.client.post(RENDITIONS+'/cleanup',
                json={**body, 'impact_revision': before.json()['impact_revision']})
        self.assertTrue(cancelled)
        self.assertEqual(409, stopped.status_code, stopped.text)
        self.assertEqual(original_ledger, (self.root/ACTIVE_RENDITIONS).read_bytes())
        candidate = await self.client.post('/api/sound_design/candidates/render',
            json={'context_revision': value['context_revision']})
        self.assertEqual(200, candidate.status_code, candidate.text)
        ledger = (self.root/ACTIVE_RENDITIONS).read_bytes()
        stale = await self.client.post(RENDITIONS+'/cleanup', json={**body, 'impact_revision': before.json()['impact_revision']})
        self.assertEqual(409, stale.status_code); self.assertEqual(ledger, (self.root/ACTIVE_RENDITIONS).read_bytes())
        real_open = os.open
        def no_pcm_reads(path, *args, **kwargs):
            if str(path).endswith('mixed.pcm'):raise AssertionError('Cleanup read PCM')
            return real_open(path, *args, **kwargs)
        with patch.object(os, 'open', side_effect=no_pcm_reads), \
                patch.object(candidates, 'get_sound_delivery_profiles', side_effect=AssertionError('Cleanup probed encoder')):
            inspected = await self.client.post(RENDITIONS+'/cleanup_preview', json=body)
        self.assertEqual(200, inspected.status_code, inspected.text)
        # A legacy cue's candidate cannot retroactively claim a later record.
        self.assertEqual([identity], inspected.json()['removable_ids'])
        stage = self.root/'.sound_design_renders'/candidate.json()['stage']
        unknown = stage/'recover-me'; unknown.write_bytes(b'unknown retained proof')
        failed = await self.client.post(RENDITIONS+'/cleanup_preview', json=body)
        self.assertEqual(409, failed.status_code); self.assertEqual(b'unknown retained proof', unknown.read_bytes())
        self.assertEqual(ledger, (self.root/ACTIVE_RENDITIONS).read_bytes())
        # Content proofs also fence platforms with coarse/restorable stat times.
        # Pin the advertised inventory to isolate the separate metadata hashes.
        import hashlib
        unknown.unlink()
        candidate_path = stage/'candidate.json'; candidate_raw = candidate_path.read_bytes()
        inventory = cleanup._capture_stage(stage, self.root, lambda: None)
        proof = dict(stage=stage.name, inventory_revision=inventory[1],
            candidate_sha256=hashlib.sha256(candidate_raw).hexdigest(), approval_sha256=None)
        for name, changed in (('candidate.json', candidate_raw[:-1]+b' '), ('approval.json', b'{}')):
            path = stage/name
            try:
                path.write_bytes(changed)
                with patch.object(cleanup, '_capture_stage', return_value=inventory):
                    with self.assertRaisesRegex(ValueError, 'metadata proof changed'):
                        cleanup._require_candidate_proofs_current(self.root, stage.parent,
                            [stage.name], [proof], lambda: None)
                self.assertEqual(ledger, (self.root/ACTIVE_RENDITIONS).read_bytes())
            finally:
                if name == 'candidate.json':
                    path.write_bytes(candidate_raw)
                else:
                    path.unlink()
