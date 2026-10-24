"""Candidate HTTP workflow with native CPU-only task claims and audio files."""
from contextlib import ExitStack
import copy
import io
import threading
import unittest
from unittest.mock import patch
import wave
from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
from routers import sound_design as routes
from tests import test_sound_design_context as fixtures
from tests.test_export_pcm_plan import clip
from sound_design_context import capture_sound_design_context


class SoundCandidateRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SoundDesignContextTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups); self.root = self.fixture.root
        with clip(24000,4800).export(self.fixture.audio,format='wav'): pass
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        states = copy.deepcopy(core.process_state)
        for state in states.values(): state.update(running=False,cancel=False)
        for target,name,value in ((core,'DATA_DIR',str(self.root)),(routes,'DATA_DIR',str(self.root)),
                                  (core,'process_state',states),(core,'_task_claims',{}),(core,'_gpu_leases',{})):
            self.stack.enter_context(patch.object(target,name,value))
        self.stack.enter_context(patch.object(core,'acquire_gpu_lock',side_effect=AssertionError('GPU claimed')))
        api = FastAPI(); api.include_router(routes.router)
        self.client = self.stack.enter_context(TestClient(api))
        with self.fixture.admitted(): self.revision = capture_sound_design_context(self.root)['context_revision']

    def render(self):
        response = self.client.post('/api/sound_design/candidates/render',json={'context_revision':self.revision})
        self.assertEqual(200,response.status_code,response.text)
        self.assertFalse(core.process_state['audio']['running']); self.assertEqual({},core._task_claims)
        return response.json()

    def test_render_audition_approval_use_cpu_worker_and_existing_owner(self):
        worker_threads = []; real = routes.save_sound_design_candidate
        def observe(*args,**kwargs):
            worker_threads.append(threading.get_ident())
            self.assertTrue(core.is_task_cpu_only('audio'))
            self.assertEqual('started',core._task_claims['audio']['phase'])
            return real(*args,**kwargs)
        with patch.object(routes,'save_sound_design_candidate',side_effect=observe): candidate = self.render()
        self.assertNotEqual(threading.get_ident(),worker_threads[0])
        base = '/api/sound_design/candidates/'+candidate['stage']
        response = self.client.get(base+'/audition',params={
            'candidate_revision':candidate['candidate_revision'],'start_frame':10,'end_frame':20})
        self.assertEqual(200,response.status_code,response.text if response.status_code!=200 else '')
        with wave.open(io.BytesIO(response.content)) as audio:
            self.assertEqual(10,audio.getnframes()); self.assertEqual(b'\x00\x20'*10,audio.readframes(10))
        response = self.client.post(base+'/approve',json={'candidate_revision':candidate['candidate_revision'],
            'full_mix_reviewed':True,'delivery':{'format':'mp3'}})
        self.assertEqual(200,response.status_code,response.text)
        self.assertTrue((self.root/'.sound_design_renders'/candidate['stage']/'approval.json').is_file())
        self.assertNotIn(str(self.root),response.text)
        self.assertFalse(core.process_state['audio']['running']); self.assertEqual({},core._task_claims)

    def test_native_metadata_limits_accept_escaped_unicode_but_bound_request_bytes(self):
        import json
        from sound_design_approval import MAX_APPROVAL_BYTES
        candidate = self.render()
        url = '/api/sound_design/candidates/'+candidate['stage']+'/approve'
        text = '\U0001f642'*20000
        metadata = {key:text for key in ('title','author','narrator','year','description')}
        payload = {'candidate_revision':candidate['candidate_revision'],'full_mix_reviewed':True,
                   'delivery':{'format':'m4b','m4b':metadata}}
        raw = json.dumps(payload,ensure_ascii=True).encode()
        self.assertGreater(len(raw),MAX_APPROVAL_BYTES)
        response = self.client.post(url,content=raw,headers={'Content-Type':'application/json'})
        self.assertEqual(200,response.status_code,response.text[:300])
        receipt = self.root/'.sound_design_renders'/candidate['stage']/'approval.json'
        previous = receipt.read_bytes()
        self.assertLessEqual(len(previous),MAX_APPROVAL_BYTES)
        self.assertEqual(text,json.loads(previous)['delivery']['m4b']['title'])
        with patch.object(core,'reserve_background_task',side_effect=AssertionError('Claimed oversized request')):
            response = self.client.post(url,content=b'x'*(routes.MAX_APPROVAL_REQUEST_BYTES+1))
        self.assertEqual(413,response.status_code)
        self.assertEqual(previous,receipt.read_bytes())

    def test_invalid_and_oversize_requests_refuse_before_claim(self):
        with patch.object(core,'reserve_background_task',side_effect=AssertionError('claimed')):
            for content,status in ((b'{}',400),(b'{"context_revision":"x"}',400),
                                   (b'{"a":1,"a":2}',400),(b'x'*17000,413)):
                with self.subTest(content=content[:40]):
                    response = self.client.post('/api/sound_design/candidates/render',content=content)
                    self.assertEqual(status,response.status_code,response.text)

    def test_stale_context_sanitizes_failure_and_releases_claim(self):
        self.fixture.source.write_text('Changed source')
        response = self.client.post('/api/sound_design/candidates/render',json={'context_revision':self.revision})
        self.assertEqual(409,response.status_code,response.text)
        self.assertNotIn(str(self.root),response.text)
        self.assertEqual({},core._task_claims); self.assertFalse(core.process_state['audio']['running'])

    def test_cancelled_request_retains_claim_until_worker_exits(self):
        import asyncio
        from tts import ExportCancelled
        entered, release = threading.Event(), threading.Event()
        observed = []
        class Request:
            async def is_disconnected(self): return False
        def slow(cancel_check):
            entered.set()
            if not release.wait(5): raise AssertionError('Worker was not released')
            observed.append(cancel_check())
            raise ExportCancelled('Cancelled fixture')
        async def exercise():
            task = asyncio.create_task(routes._dispatch_sound_candidate(Request(),slow))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,3))
                task.cancel()
                await asyncio.sleep(.05)
                self.assertFalse(task.done())
                self.assertTrue(core.process_state['audio']['running'])
                self.assertEqual('started',core._task_claims['audio']['phase'])
            finally:
                release.set()
                result = await asyncio.gather(task,return_exceptions=True)
            self.assertIsInstance(result[0],asyncio.CancelledError)
        asyncio.run(exercise())
        self.assertEqual([True],observed)
        self.assertFalse(core.process_state['audio']['running'])
        self.assertEqual({},core._task_claims)

    def test_candidate_list_and_revision_bound_discard(self):
        candidate = self.render()
        response = self.client.get('/api/sound_design/candidates')
        self.assertEqual(200,response.status_code,response.text)
        row = response.json()['candidates'][0]
        self.assertEqual(candidate['candidate_revision'],row['candidate_revision'])
        self.assertEqual('metadata_ready',row['status']); self.assertFalse(row['media_verified'])
        response = self.client.post('/api/sound_design/candidates/'+row['stage']+'/discard',
                                    params={'recovery_revision':row['recovery_revision']})
        self.assertEqual(200,response.status_code,response.text)
        self.assertFalse((self.root/'.sound_design_renders'/row['stage']).exists())
        self.assertTrue(self.fixture.audio.exists())

    def test_real_approved_delivery_uses_saved_settings_and_cpu_owner(self):
        # A finite own tone exercises real loudness measurement and report
        # propagation through approval, the HTTP worker and publication.
        import math
        from pydub.generators import Sine
        with Sine(440,sample_rate=24000).to_audio_segment(duration=4000).apply_gain(-18).export(
                self.fixture.audio,format='wav'):pass
        with self.fixture.admitted():self.revision=capture_sound_design_context(self.root)['context_revision']
        candidate=self.render();base='/api/sound_design/candidates/'+candidate['stage']
        approved=self.client.post(base+'/approve',json={'candidate_revision':candidate['candidate_revision'],
            'full_mix_reviewed':True,'delivery':{'format':'mp3','mastering':{}}})
        self.assertEqual(200,approved.status_code,approved.text)
        approval=approved.json()
        payload=dict(candidate_revision=candidate['candidate_revision'],approval_revision=approval['approval_revision'])
        with patch.object(core,'reserve_background_task',side_effect=AssertionError('Claimed injected settings')):
            invalid=self.client.post(base+'/deliver',json={**payload,'delivery':{'format':'wav'}})
        self.assertEqual(400,invalid.status_code)
        original=routes.save_approved_sound_delivery;threads=[]
        def observed(*args,**kwargs):
            threads.append(threading.get_ident());self.assertTrue(core.is_task_cpu_only('audio'))
            self.assertEqual('started',core._task_claims['audio']['phase'])
            return original(*args,**kwargs)
        source_bytes=self.fixture.audio.read_bytes()
        with patch.object(routes,'save_approved_sound_delivery',side_effect=observed):
            response=self.client.post(base+'/deliver',json=payload)
        self.assertEqual(200,response.status_code,response.text)
        result=response.json();self.assertEqual('mixed_audiobook.mp3',result['file'])
        self.assertEqual('verified',result['quality']['status'])
        mastering=result['mastering']
        self.assertIn(mastering['mode'],('linear','dynamic'))
        self.assertEqual(approval['delivery']['mastering'],mastering['settings'])
        self.assertEqual(result['quality']['sha256'],mastering['sha256'])
        for phase in ('before','after'):
            self.assertTrue(all(math.isfinite(value) for value in mastering[phase].values()))
        self.assertNotIn(str(self.root),response.text)
        self.assertNotEqual(threading.get_ident(),threads[0])
        self.assertEqual(source_bytes,self.fixture.audio.read_bytes())
        before=(self.root/result['file']).read_bytes()
        download=self.client.get('/api/sound_design/delivery/mp3/download',params={'sha256':result['quality']['sha256']})
        self.assertEqual(200,download.status_code);self.assertEqual(before,download.content)
        self.assertEqual(str(len(before)),download.headers['content-length'])
        self.assertIn('attachment',download.headers['content-disposition'])
        self.assertEqual('no-store',download.headers['cache-control'])
        self.assertEqual(409,self.client.get('/api/sound_design/delivery/mp3/download',params={'sha256':'0'*64}).status_code)

        with patch.object(routes,'save_approved_sound_delivery',wraps=original):
            stale=self.client.post(base+'/deliver',json={**payload,'approval_revision':'0'*64})
        self.assertEqual(409,stale.status_code);self.assertEqual(before,(self.root/result['file']).read_bytes())
        self.assertEqual({},core._task_claims);self.assertFalse(core.process_state['audio']['running'])

    def test_delivery_source_differences_require_exact_report_confirmation(self):
        self.fixture.source.write_text('A different original source sentence.')
        with self.fixture.admitted():self.revision=capture_sound_design_context(self.root)['context_revision']
        candidate=self.render();base='/api/sound_design/candidates/'+candidate['stage']
        approval=self.client.post(base+'/approve',json={'candidate_revision':candidate['candidate_revision'],
            'full_mix_reviewed':True,'delivery':{'format':'wav'}}).json()
        payload=dict(candidate_revision=candidate['candidate_revision'],approval_revision=approval['approval_revision'])
        destination=self.root/'mixed_audiobook.wav';destination.write_bytes(b'previous complete delivery')
        response=self.client.post(base+'/deliver',json=payload)
        self.assertEqual(409,response.status_code,response.text)
        report=response.json()['detail'];self.assertEqual('differences',report['status'])
        self.assertTrue(report['page']);self.assertNotIn('hunks',report)
        self.assertLessEqual(len(response.content),65536)
        self.assertEqual(b'previous complete delivery',destination.read_bytes())
        review={**payload,'expected_snapshot':report['snapshot'],'offset':0}
        real=routes.get_sound_source_review;observed=[]
        def observe(*args,**kwargs):
            observed.append(threading.get_ident())
            self.assertTrue(core.is_task_cpu_only('audio'))
            self.assertEqual('started',core._task_claims['audio']['phase'])
            return real(*args,**kwargs)
        with patch.object(routes,'get_sound_source_review',side_effect=observe):
            page=self.client.post(base+'/source_review',json=review)
        self.assertEqual(200,page.status_code,page.text)
        self.assertEqual(report,page.json());self.assertNotEqual(threading.get_ident(),observed[0])
        with patch.object(core,'reserve_background_task',side_effect=AssertionError('invalid page claimed audio')):
            for fields in ({'offset':True},{'offset':-1},{'offset':1.5},{'delivery':{'format':'mp3'}}):
                invalid=self.client.post(base+'/source_review',json={**review,**fields})
                self.assertEqual(400,invalid.status_code,invalid.text)
            oversized=self.client.post(base+'/source_review',content=b'x'*16385)
            self.assertEqual(413,oversized.status_code)
        stale=self.client.post(base+'/source_review',json={**review,'expected_snapshot':'0'*64})
        self.assertEqual(409,stale.status_code)
        wrong=self.client.post(base+'/deliver',json={**payload,'integrity_confirmation':'0'*64})
        self.assertEqual(409,wrong.status_code)
        confirmed=self.client.post(base+'/deliver',json={**payload,'integrity_confirmation':report['snapshot']})
        self.assertEqual(200,confirmed.status_code,confirmed.text)
        self.assertEqual('verified',confirmed.json()['quality']['status'])
        self.assertEqual({},core._task_claims);self.assertFalse(core.process_state['audio']['running'])
