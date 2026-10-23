"""Actual ASGI/flat-file character review, import/revision and async boundaries."""
import asyncio
import copy
import json
import threading
import unittest
from unittest.mock import patch
import wave

import httpx
from fastapi import FastAPI
from routers import character_roster as api
from character_roster_snapshot import MAX_ROSTER_BYTES
from tests import test_character_roster_snapshot as fixtures
from tests import test_character_roster_apply as apply_fixtures
from tests.test_character_roster import SOURCE1, SOURCE2, A, B, C, D, entry, decision, proposal

BASE = '/api/character_roster'


class CharacterRosterApiTests(unittest.IsolatedAsyncioTestCase):
    files = apply_fixtures.CharacterRosterApplyTests.files

    async def asyncSetUp(self):
        fixtures.CharacterRosterSnapshotTests.setUp(self)
        for name, value in (('DATA_DIR', str(self.root)), ('process_state', {})):
            guard=patch.object(api.core,name,value);guard.start();self.addCleanup(guard.stop)
        application=FastAPI();application.include_router(api.router)
        @application.get('/fixture-ping')
        async def ping():return {'ready':True}
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url='http://fixture')
        self.addAsyncCleanup(self.client.aclose)

    async def metadata(self):
        result=await self.client.get(BASE);self.assertEqual(200,result.status_code,result.text)
        return result.json()

    def request(self, metadata, value=None, imported=None, mapping=None):
        return {'context_revision':metadata['context_revision'],'proposal':value or copy.deepcopy(self.proposal),
                'imported_roster':imported,'import_id_mapping':mapping}

    async def approve(self, request):
        preview=await self.client.post(BASE+'/preview',json=request)
        self.assertEqual(200,preview.status_code,preview.text)
        self.assertTrue(preview.json()['review']['can_approve'])
        result=await self.client.post(BASE+'/apply',json={**request,'expected_revision':preview.json()['revision']})
        self.assertEqual(200,result.status_code,result.text);return preview.json()

    async def discard(self):
        status=(await self.client.get(BASE+'/undo')).json()
        result=await self.client.post(BASE+'/discard_undo',json={'expected_revision':status['revision']})
        self.assertEqual(200,result.status_code,result.text)

    async def test_http_approval_and_exact_undo_preserve_accepted_content_and_only_expose_public_fields(self):
        (self.root/'annotated_script.json').write_bytes(b'[{"text":"accepted words","speaker":"ALICE"}]')
        (self.root/'voice_config.json').write_bytes(b'{"ALICE":"voice-one"}')
        (self.root/'chunks.json').write_bytes(b'[{"uid":"stable-one","audio_revision":"take-one","audio_path":"one.wav"}]')
        with wave.open(str(self.root/'one.wav'),'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(8000);output.writeframes(b'\x01\x00'*80)
        before=self.files();metadata=await self.metadata()
        self.assertNotIn(str(self.root),json.dumps(metadata));self.assertFalse((self.root/'cast_lists').exists())
        preview=await self.approve(self.request(metadata))
        public=json.dumps(preview)
        for private in ('absolute_path','cast_bytes','roster_bytes','before_sha256',str(self.root)):
            self.assertNotIn(private,public)
        current=await self.metadata();self.assertTrue(current['approval_current'])
        self.assertEqual(current['roster']['entries'][0]['id'],A)
        self.assertEqual(current['last_review']['decisions'],self.proposal['decisions'])
        for name,raw in before.items():self.assertEqual(raw,(self.root/name).read_bytes())
        status=(await self.client.get(BASE+'/undo')).json();self.assertTrue(status['can_undo'])
        undo=await self.client.post(BASE+'/undo',json={'expected_revision':status['revision']})
        self.assertEqual(200,undo.status_code,undo.text);self.assertEqual(before,self.files())

    async def test_changed_book_or_cast_refuses_stale_drafts_without_overwrite(self):
        metadata=await self.metadata();request=self.request(metadata)
        preview=(await self.client.post(BASE+'/preview',json=request)).json()
        self.state['book_generation']='new-selection';(self.root/'state.json').write_text(json.dumps(self.state))
        before=self.files()
        stale=await self.client.post(BASE+'/apply',json={**request,'expected_revision':preview['revision']})
        self.assertEqual(409,stale.status_code);self.assertEqual(before,self.files())
        await self.approve(self.request(await self.metadata()));current=await self.metadata()
        await self.discard()
        # A legacy editor's byte change invalidates roster approval even if JSON meaning stays.
        path=next((self.root/'cast_lists').glob('*.json'))
        if path.name.endswith('.roster.json'):path=next(p for p in path.parent.glob('*.json') if not p.name.endswith('.roster.json'))
        path.write_bytes(path.read_bytes()+b' ')
        self.assertFalse((await self.metadata())['approval_current'])
        stale=await self.client.post(BASE+'/preview',json=self.request(current,proposal(SOURCE1,current['roster']['entries'],[])))
        self.assertEqual(409,stale.status_code)

    async def test_quote_candidates_use_unicode_character_coordinates_and_never_approve_unanchored_claims(self):
        self.source.write_text('😀'+SOURCE1)
        metadata=await self.metadata()
        result=await self.client.get(BASE+'/source',params={'context_revision':metadata['context_revision'],'quote':'Alice'})
        self.assertEqual(200,result.status_code,result.text);match=result.json()['matches'][0]
        self.assertEqual(('😀'+SOURCE1).index('Alice'),match['start_char'])
        value=proposal('😀'+SOURCE1,[entry('😀'+SOURCE1,A,'ALICE','Alice')],[decision('add',[],[A])])
        value['entries'][0]['evidence'][0].update(start_char=None,end_char=None)
        request=self.request(metadata,value)
        preview=(await self.client.post(BASE+'/preview',json=request)).json()
        self.assertFalse(preview['review']['can_approve'])
        before=self.files();refused=await self.client.post(BASE+'/apply',json={**request,'expected_revision':preview['revision']})
        self.assertEqual(409,refused.status_code);self.assertEqual(before,self.files())
        value['entries'][0]['evidence'][0]= {k:match[k] for k in ('source_quote','start_char','end_char')}
        await self.approve(self.request(metadata,value))

    async def test_quote_body_preflight_rejects_invalid_fields_before_admission(self):
        metadata=await self.metadata()
        valid={'context_revision':metadata['context_revision'],'quote':'Alice','offset':0}
        for fields in ({'quote':'😀'*4001},{'quote':[]},{'quote':'\ud800'},
                       {'offset':True},{'offset':-1},{'context_revision':'bad'},{'extra':1}):
            with self.subTest(fields=list(fields)),patch.object(api,'ensure_scope_admission',side_effect=AssertionError('invalid search must not scan source')):
                result=await self.client.post(BASE+'/source',content=json.dumps({**valid,**fields}).encode())
                self.assertEqual(409,result.status_code,result.text)

    async def test_current_roster_import_requires_explicit_mapping_retains_tombstones_and_exact_undo(self):
        await self.approve(self.request(await self.metadata()));await self.discard()
        metadata=await self.metadata();before=self.files()
        imported={'schema':1,'source_sha256':metadata['source_sha256'],'source_characters':len(SOURCE1),
                  'entries':[entry(SOURCE1,C,'IMPORTED ALICE','Alice')],'retired_ids':[D]}
        value=proposal(SOURCE1,[entry(SOURCE1,A,'ALICE','Alice',['MISS REED'])],[decision('update',[A],[A])])
        missing=await self.client.post(BASE+'/preview',json=self.request(metadata,value,imported))
        self.assertEqual(409,missing.status_code);self.assertEqual(before,self.files())
        request=self.request(metadata,value,imported,{C:A});preview=await self.approve(request)
        self.assertEqual(preview['review']['import_review']['mappings'][0]['target_id'],A)
        current=await self.metadata();self.assertEqual(current['roster']['retired_ids'],[C,D])
        self.assertEqual(current['last_review']['import_review']['mappings'][0]['imported_id'],C)
        status=(await self.client.get(BASE+'/undo')).json()
        self.assertEqual(200,(await self.client.post(BASE+'/undo',json={'expected_revision':status['revision']})).status_code)
        self.assertEqual(before,self.files())

    async def test_source_revision_import_keeps_canonical_id_and_undo_preserves_user_source_revision(self):
        await self.approve(self.request(await self.metadata()));old=(await self.metadata())['roster'];await self.discard()
        self.source.write_text(SOURCE2);metadata=await self.metadata();self.assertIsNone(metadata['roster'])
        value=proposal(SOURCE2,[entry(SOURCE2,A,'ALICE','Alice'),entry(SOURCE2,B,'BOB','Bob')],
                       [decision('update',[A],[A]),decision('add',[],[B])])
        before=self.files();await self.approve(self.request(metadata,value,old))
        self.assertEqual([A,B],[row['id'] for row in (await self.metadata())['roster']['entries']])
        status=(await self.client.get(BASE+'/undo')).json()
        restored=await self.client.post(BASE+'/undo',json={'expected_revision':status['revision']})
        self.assertEqual(200,restored.status_code,restored.text);self.assertEqual(before,self.files())
        self.assertEqual(SOURCE2,self.source.read_text())

    async def test_import_mapping_can_follow_an_explicit_merge_to_the_surviving_identity(self):
        rows=[entry(SOURCE1,A,'ALICE','Alice'),entry(SOURCE1,B,'MISS REED','Miss Reed')]
        await self.approve(self.request(await self.metadata(),proposal(SOURCE1,rows,[decision('add',[],[A]),decision('add',[],[B])])))
        await self.discard();metadata=await self.metadata();before=self.files()
        imported=copy.deepcopy(metadata['roster']);imported['entries']=[{**copy.deepcopy(rows[0]),'id':C}]
        survivor=copy.deepcopy(rows[1]);survivor['aliases']=['ALICE'];survivor['evidence']+=copy.deepcopy(rows[0]['evidence'])
        value=proposal(SOURCE1,[survivor],[decision('merge',[A,B],[B])])
        preview=await self.approve(self.request(metadata,value,imported,{C:B}))
        self.assertEqual(B,preview['review']['import_review']['mappings'][0]['target_id'])
        self.assertEqual([A,C],(await self.metadata())['roster']['retired_ids'])
        status=(await self.client.get(BASE+'/undo')).json()
        self.assertEqual(200,(await self.client.post(BASE+'/undo',json={'expected_revision':status['revision']})).status_code)
        self.assertEqual(before,self.files())

    async def test_malformed_body_refuses_before_admission_and_does_not_echo_private_cast_fields(self):
        metadata=await self.metadata();valid=self.request(metadata)
        invalid=copy.deepcopy(valid);invalid['proposal']['entries'][0]['evidence'][0]['start_char']=True
        values=[b'{invalid',json.dumps({**valid,'extra':True}).encode(),json.dumps(invalid).encode(),
                b'{"expected_revision":"a","expected_revision":"b"}',json.dumps(valid).encode('utf-16')]
        with patch.object(api,'ensure_scope_admission',side_effect=AssertionError('malformed body must not read source')):
            for raw in values:
                with self.subTest(raw=raw[:35]):
                    result=await self.client.post(BASE+'/preview',content=raw)
                    self.assertEqual(409,result.status_code,result.text)
        await self.approve(valid);await self.discard()
        path=next(p for p in (self.root/'cast_lists').glob('*.json') if not p.name.endswith('.roster.json'))
        path.write_text(json.dumps([{'name':'','api_key':'private-marker-in-malformed-file'}]))
        result=await self.client.get(BASE);self.assertEqual(409,result.status_code)
        self.assertNotIn('private-marker',result.text)

    async def test_invalid_imported_active_ids_are_refused_before_source_or_task_admission(self):
        metadata=await self.metadata()
        for identity in ('wrong', True, ['character_'+'c'*20]):
            imported={'schema':1,'source_sha256':metadata['source_sha256'],'source_characters':len(SOURCE1),
                      'entries':[entry(SOURCE1,C,'IMPORTED','Alice')],'retired_ids':[]}
            imported['entries'][0]['id']=identity
            request=self.request(metadata,imported=imported,mapping={'wrong':A})
            before=self.files()
            with patch.object(api,'ensure_scope_admission',side_effect=AssertionError('invalid IDs must refuse first')):
                result=await self.client.post(BASE+'/preview',json=request)
            self.assertEqual(409,result.status_code,result.text);self.assertEqual(before,self.files())

    async def test_quote_limit_is_4000_unicode_characters_and_valid_non_bmp_export_roundtrips(self):
        quote='😀'*4000;source=quote+SOURCE1;self.source.write_text(source)
        metadata=await self.metadata()
        value=proposal(source,[entry(source,A,'LONG QUOTE',quote)],[decision('add',[],[A])])
        await self.approve(self.request(metadata,value))
        exported=(await self.metadata())['roster'];self.assertEqual(4000,len(exported['entries'][0]['evidence'][0]['source_quote']))
        self.assertEqual(8000,len(quote.encode('utf-16-le'))//2)
        bad=copy.deepcopy(value);bad['entries'][0]['evidence'][0]['source_quote']+='😀'
        request=self.request(metadata,bad)
        with patch.object(api,'ensure_scope_admission',side_effect=AssertionError('overlong quote must refuse first')):
            rejected=await self.client.post(BASE+'/preview',json=request)
        self.assertEqual(409,rejected.status_code,rejected.text)

    async def test_actual_stream_limit_ignores_forged_content_length_and_blocks_worker_dispatch(self):
        async def chunks():
            for _ in range(33):yield b'x'*65536
        with patch.object(api,'_run_operation',side_effect=AssertionError('oversized body must not dispatch')):
            result=await self.client.post(BASE+'/preview',content=chunks(),headers={'Content-Length':'1'})
        self.assertEqual(413,result.status_code);self.assertGreater(33*65536,MAX_ROSTER_BYTES)

    async def test_missing_source_does_not_prevent_inspection_or_explicit_undo_discard(self):
        await self.approve(self.request(await self.metadata()));loaded=await self.metadata();self.source.unlink();before=self.files()
        status=(await self.client.get(BASE+'/undo')).json();self.assertTrue(status['available']);self.assertFalse(status['can_undo'])
        self.assertEqual(loaded['undo_revision'],status['revision'])
        result=await self.client.post(BASE+'/discard_undo',json={'expected_revision':loaded['undo_revision']})
        self.assertEqual(200,result.status_code,result.text)
        self.assertEqual({n:r for n,r in before.items() if n!='.character_roster_undo.json'},self.files())

    async def test_undo_inspection_redacts_operating_system_paths_and_diagnostics(self):
        import character_roster_apply as storage
        await self.approve(self.request(await self.metadata()));before=self.files()
        error=PermissionError(13,'private-auth-marker',str(self.source))
        for target in ('capture_scope_file','get_character_roster_snapshot'):
            with self.subTest(target=target),patch.object(storage,target,side_effect=error):
                result=await self.client.get(BASE+'/undo')
                self.assertEqual(200,result.status_code,result.text)
                self.assertFalse(result.json()['can_undo']);self.assertTrue(result.json()['available'])
                self.assertNotIn(str(self.root),result.text);self.assertNotIn('private-auth-marker',result.text)
                self.assertEqual('Character review input is unavailable or invalid',result.json()['reason'])
        self.assertEqual(before,self.files())

    async def require_displayed_context_for_later_undo(self, change_book):
        await self.approve(self.request(await self.metadata()));old=await self.metadata()
        if change_book:
            from routers import script
            self.client._transport.app.include_router(script.router)
            uploads=self.root/'uploads';uploads.mkdir();(uploads/'book-b.txt').write_text(SOURCE2)
            with patch.object(script,'DATA_DIR',str(self.root)),patch.object(script,'UPLOADS_DIR',str(uploads)):
                selected=await self.client.post('/api/uploads/select',json={'filename':'book-b.txt'})
                self.assertEqual(200,selected.status_code,selected.text)
        else:
            self.source.write_text(SOURCE2)
        await self.discard();metadata=await self.metadata();before=self.files()
        self.assertEqual(change_book,old['book_token']!=metadata['book_token'])
        value=proposal(SOURCE2,[entry(SOURCE2,B,'BOB','Bob')],[decision('add',[],[B])])
        await self.approve(self.request(metadata,value))
        status=(await self.client.get(BASE+'/undo')).json();current=await self.metadata()
        self.assertEqual(current['undo_revision'],status['revision'])
        self.assertNotEqual(old['undo_revision'],status['revision'])
        approved=self.files()
        for operation in ('undo','discard_undo'):
            for revision in (old['undo_revision'],None):
                refused=await self.client.post(BASE+'/'+operation,json={'expected_revision':revision})
                self.assertEqual(409,refused.status_code,refused.text);self.assertEqual(approved,self.files())
        restored=await self.client.post(BASE+'/undo',json={'expected_revision':current['undo_revision']})
        self.assertEqual(200,restored.status_code,restored.text);self.assertEqual(before,self.files())

    async def test_book_a_metadata_cannot_authorize_fresh_book_b_undo_or_discard(self):
        await self.require_displayed_context_for_later_undo(True)

    async def test_same_book_source_revision_cannot_reuse_older_metadata_for_fresh_undo(self):
        await self.require_displayed_context_for_later_undo(False)

    async def test_worker_scan_keeps_other_http_requests_responsive(self):
        entered,release=threading.Event(),threading.Event();threads=[];real=api.get_character_roster_snapshot
        def delayed(*args,**kwargs):
            threads.append(threading.get_ident());entered.set();release.wait(10)
            return real(*args,**kwargs)
        timer=threading.Timer(5,release.set);timer.start()
        with patch.object(api,'get_character_roster_snapshot',delayed):
            request=asyncio.create_task(self.client.get(BASE))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                ping=await asyncio.wait_for(self.client.get('/fixture-ping'),.5)
                self.assertEqual({'ready':True},ping.json());self.assertFalse(request.done())
                self.assertTrue(all(t!=threading.get_ident() for t in threads))
            finally:
                release.set();timer.cancel();timer.join();result=await asyncio.wait_for(request,3)
        self.assertEqual(200,result.status_code,result.text)


if __name__=='__main__':unittest.main()
