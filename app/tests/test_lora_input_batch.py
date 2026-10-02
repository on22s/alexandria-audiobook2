import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
import httpx
from routers import lora
from tests.test_upload_event_loop import archive_bytes,wav_bytes


class LoraAudioInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_non_audio_files_cannot_publish_with_good_row(self):
        for filename,audio in [('metadata.jsonl',None),('text.txt',b'not audio'),('fake.wav',b'not audio'),
                                ('empty.wav',wav_bytes()[:44])]:
            with self.subTest(filename=filename),tempfile.TemporaryDirectory() as tmp:
                rows=[{'audio':'good.wav','text':'Good sample.'},{'audio':filename,'text':'Bad sample.'}]
                members={'metadata.jsonl':'\n'.join(map(json.dumps,rows))+'\n','good.wav':wav_bytes()}
                if audio is not None:members[filename]=audio
                app=FastAPI();app.include_router(lora.router)
                with patch.object(lora,'LORA_DATASETS_DIR',tmp):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                        result=await client.post('/api/lora/upload_dataset',files={'file':('voice.zip',archive_bytes(members))})
                self.assertEqual(400,result.status_code,result.text)
                self.assertIn('WAV',result.json()['detail'])
                self.assertEqual(['.dataset-locks'],sorted(p.name for p in Path(tmp).iterdir()))

    async def test_wrong_container_and_nonfinite_tail_refuse_publication(self):
        import numpy as np
        import soundfile as sf
        documents = []
        for format in ('FLAC', 'WAV'):
            output = io.BytesIO()
            values = np.zeros(20000, dtype='float32')
            if format == 'WAV':
                values[-1] = np.nan
            sf.write(output, values, 24000, format=format, subtype='FLOAT' if format == 'WAV' else 'PCM_16')
            documents.append(output.getvalue())
        for audio in documents:
            with self.subTest(size=len(audio)), tempfile.TemporaryDirectory() as tmp:
                document=archive_bytes({'metadata.jsonl':json.dumps({'audio':'voice.wav','text':'Good sample.'})+'\n',
                                        'voice.wav':audio})
                app=FastAPI();app.include_router(lora.router)
                with patch.object(lora,'LORA_DATASETS_DIR',tmp):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                        result=await client.post('/api/lora/upload_dataset',files={'file':('voice.zip',document)})
                self.assertEqual(400,result.status_code,result.text)
                self.assertEqual(['.dataset-locks'],sorted(p.name for p in Path(tmp).iterdir()))

    async def test_valid_nested_uppercase_wav_publishes_exact_audio_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw=json.dumps({'audio_filepath':'clips/voice.WAV','text':'Good sample.'})+'\n';audio=wav_bytes()
            document=archive_bytes({'metadata.jsonl':raw,'clips/voice.WAV':audio})
            app=FastAPI();app.include_router(lora.router)
            with patch.object(lora,'LORA_DATASETS_DIR',tmp):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                    result=await client.post('/api/lora/upload_dataset',files={'file':('voice.zip',document)})
            self.assertEqual(200,result.status_code,result.text)
            self.assertEqual(1,result.json()['sample_count'])
            self.assertEqual(audio,(Path(tmp)/'voice/clips/voice.WAV').read_bytes())
            self.assertEqual(raw,(Path(tmp)/'voice/metadata.jsonl').read_text())


class LoraRequestBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_fields_reject_before_handler(self):
        app=FastAPI();seen=[]
        @app.post('/test')
        def test(req:lora.LoraTestRequest):seen.append(req);return {'ok':True}
        @app.post('/review')
        def review(req:lora.ReviewSubmitRequest):seen.append(req);return {'ok':True}
        cases=[('/test',{'adapter_id':'voice','text':'x'*5001}),
               ('/test',{'adapter_id':'voice','text':'Hello','instruct':'x'*401}),
               ('/review',{'choice':'unknown'}),('/review',{'choice':'A','rating':0}),
               ('/review',{'choice':'B','rating':6}),('/review',{'choice':'tie','notes':'x'*1001})]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
            for endpoint,payload in cases:
                with self.subTest(payload=repr(payload)[:70]):
                    result=await client.post(endpoint,json=payload);self.assertEqual(422,result.status_code,result.text)
            self.assertEqual([],seen)
            for payload in ({'choice':'A','rating':1,'notes':'x'*1000},{'choice':'B','rating':5},{'choice':'tie','rating':None}):
                self.assertEqual(200,(await client.post('/review',json=payload)).status_code)
            self.assertEqual(200,(await client.post('/test',json={'adapter_id':'voice','text':'x'*5000,'instruct':'x'*400})).status_code)

class LoraRecoveryPathTests(unittest.TestCase):
    def test_recovery_scan_refuses_symlink_escape_before_journal_read(self):
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as tmp,tempfile.TemporaryDirectory() as outside:
            root=Path(tmp);other=Path(outside)
            manifest=root/'manifest.json';manifest.write_text('[{"id":"voice"}]')
            journal=other/'.checkpoint_swap.json';journal.write_text('{"operation":"external sentinel"}')
            before=journal.read_bytes();(root/'voice').symlink_to(other,target_is_directory=True)
            with patch.object(lora,'_get_checkpoint_swap_journal',wraps=lora._get_checkpoint_swap_journal) as read:
                with self.assertRaises(HTTPException) as raised:lora.list_adapters_needing_recovery(tmp,str(manifest))
                self.assertEqual(400,raised.exception.status_code);read.assert_not_called()
            self.assertEqual(before,journal.read_bytes())

    def test_recovery_scan_retains_normal_pending_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'voice').mkdir()
            manifest=root/'manifest.json';manifest.write_text('[{"id":"voice"}]')
            (root/'voice/.checkpoint_swap.json').write_text('{"operation":"promote"}')
            self.assertEqual([{'adapter_id':'voice','operation':'promote'}],lora.list_adapters_needing_recovery(tmp,str(manifest)))
