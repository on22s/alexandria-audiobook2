"""Real upload artifacts and same-event-loop HTTP responsiveness during parsing."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave
import zipfile

from fastapi import FastAPI
import httpx
from routers import lora, script, voice_design


def archive_bytes(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def epub_bytes():
    return archive_bytes({
        'mimetype':'application/epub+zip',
        'META-INF/container.xml':'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>',
        'OEBPS/book.opf':'<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>',
        'OEBPS/chapter.xhtml':'<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>',
    })


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b'\0\0' * 2400)
    return buffer.getvalue()


class UploadEventLoopTests(unittest.IsolatedAsyncioTestCase):
    async def _upload_while_paused(self, router, endpoint, filename, document, attempted, release, worker_ids, form_data=None):
        app = FastAPI()
        app.include_router(router)
        @app.get('/fixture/ping')
        async def ping():
            return {'ok':True}

        def watchdog():
            if attempted.wait(3):
                release.wait(1)
            release.set()

        watchdog_thread = threading.Thread(target=watchdog)
        watchdog_thread.start()
        loop_id = threading.get_ident()
        task = None
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                task = asyncio.create_task(client.post(endpoint,files={'file':(filename,document)},data=form_data))
                self.assertTrue(await asyncio.to_thread(attempted.wait,3))
                self.assertFalse(task.done(),'The upload blocked the event loop until parsing finished')
                response = await client.get('/fixture/ping')
                self.assertEqual({'ok':True},response.json())
                self.assertFalse(release.is_set(),'The other request only ran after parsing resumed')
                self.assertNotEqual(loop_id,worker_ids[0])
                release.set()
                response = await task
                self.assertEqual(200,response.status_code,response.text)
                return response.json()
        finally:
            release.set()
            if task is not None and not task.done():
                await task
            watchdog_thread.join(4)
            self.assertFalse(watchdog_thread.is_alive())

    async def test_epub_extraction_leaves_other_http_requests_responsive_and_preserves_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / 'uploads'
            uploads.mkdir()
            attempted, release = threading.Event(), threading.Event()
            worker_ids = []
            extract = script.extract_epub_text

            def paused_extract(path):
                worker_ids.append(threading.get_ident())
                attempted.set()
                if not release.wait(3):
                    raise AssertionError('EPUB fixture was never released')
                return extract(path)

            with patch.object(script,'UPLOADS_DIR',str(uploads)), \
                 patch.object(script,'DATA_DIR',str(root)), \
                 patch.object(script,'extract_epub_text',side_effect=paused_extract):
                result = await self._upload_while_paused(script.router,'/api/upload','book.epub',epub_bytes(),attempted,release,worker_ids)
            self.assertEqual('book.txt',result['stored_filename'])
            path = Path(result['path'])
            self.assertEqual('First paragraph.\n\nSecond paragraph.',path.read_text())
            self.assertEqual(['book.txt'],[p.name for p in uploads.iterdir()])
            self.assertEqual(str(path),json.loads((root/'state.json').read_text())['input_file_path'])

    async def test_dataset_metadata_validation_leaves_other_http_requests_responsive_and_retains_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempted, release = threading.Event(), threading.Event()
            worker_ids = []
            row = json.dumps({'audio_filepath':'sample.wav','text':'fixture metadata'})
            audio = wav_bytes()
            document = archive_bytes({'nested/metadata.jsonl':(row+'\n')*10,'nested/sample.wav':audio})
            loads = json.loads

            def paused_loads(value,*args,**kwargs):
                if value == row:
                    worker_ids.append(threading.get_ident())
                    attempted.set()
                    if not release.wait(3):
                        raise AssertionError('Metadata fixture was never released')
                return loads(value,*args,**kwargs)

            with patch.object(lora,'LORA_DATASETS_DIR',str(root)), \
                 patch.object(lora.json,'loads',side_effect=paused_loads):
                result = await self._upload_while_paused(lora.router,'/api/lora/upload_dataset','dataset.zip',document,attempted,release,worker_ids)
            self.assertEqual({'status':'uploaded','dataset_id':'dataset','sample_count':10,'metadata_count':10},result)
            dataset = root/'dataset'
            self.assertEqual(audio,(dataset/'sample.wav').read_bytes())
            self.assertEqual((row+'\n')*10,(dataset/'metadata.jsonl').read_text())
            self.assertFalse((dataset/'nested').exists())
            self.assertEqual(['.dataset-locks','dataset'],sorted(p.name for p in root.iterdir()))
            with wave.open(str(dataset/'sample.wav'),'rb') as saved:
                self.assertEqual(2400,saved.getnframes())
                self.assertEqual(24000,saved.getframerate())

    async def test_malformed_epub_is_rejected_and_does_not_select_or_leave_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root/'uploads'
            uploads.mkdir()
            before = b'{"input_file_path":"previous.txt"}'
            (root/'state.json').write_bytes(before)
            app = FastAPI()
            app.include_router(script.router)
            with patch.object(script,'UPLOADS_DIR',str(uploads)),patch.object(script,'DATA_DIR',str(root)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                    response = await client.post('/api/upload',files={'file':('bad.epub',b'not a ZIP')})
            self.assertEqual(400,response.status_code,response.text)
            self.assertIn('Failed to process EPUB',response.json()['detail'])
            self.assertEqual([],list(uploads.iterdir()))
            self.assertEqual(before,(root/'state.json').read_bytes())

    async def test_dataset_without_usable_audio_removes_reserved_dataset_and_temporary_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = FastAPI()
            app.include_router(lora.router)
            document = archive_bytes({'metadata.jsonl':json.dumps({'audio_filepath':'missing.wav','text':'fixture'})})
            with patch.object(lora,'LORA_DATASETS_DIR',str(root)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                    response = await client.post('/api/lora/upload_dataset',files={'file':('dataset.zip',document)})
            self.assertEqual(400,response.status_code,response.text)
            self.assertEqual('Dataset contains no usable training audio.',response.json()['detail'])
            self.assertEqual(['.dataset-locks'],sorted(p.name for p in root.iterdir()))


class CloneImportEventLoopTests(unittest.IsolatedAsyncioTestCase):
    _upload_while_paused = UploadEventLoopTests._upload_while_paused

    async def test_real_reference_normalization_and_measurement_leave_other_http_requests_responsive(self):
        import numpy as np
        import voice_reference_import
        from tests.test_voice_reference_import import _tone, _wav_bytes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempted, release = threading.Event(), threading.Event()
            worker_ids = []
            real_import = voice_design.import_reference_audio
            rate = 44100
            document = _wav_bytes(np.stack([_tone(4.0,rate),_tone(4.0,rate,hz=330)],axis=1),rate)
            def paused_import(source, destination):
                worker_ids.append(threading.get_ident())
                attempted.set()
                if not release.wait(3):
                    raise AssertionError('Reference fixture was never released')
                return real_import(source, destination)
            fields = {'ref_text':'Exact fixture sentence.','rights_confirmed':'true',
                      'source_title':'CPU fixture','rights_basis':'own fixture'}
            with patch.object(voice_design,'CLONE_VOICES_DIR',str(root)), \
                 patch.object(voice_design,'CLONE_VOICES_MANIFEST',str(root/'manifest.json')), \
                 patch.object(voice_design,'import_reference_audio',side_effect=paused_import):
                result = await self._upload_while_paused(voice_design.router,'/api/clone_voices/upload',
                    'Fixture Voice.wav',document,attempted,release,worker_ids,fields)
            self.assertEqual('uploaded',result['status'])
            output = root / result['filename']
            measured = voice_reference_import.measure_reference_audio(str(output))
            self.assertEqual(measured,result['measures'])
            with wave.open(str(output),'rb') as audio:
                self.assertEqual((1,2,24000),(audio.getnchannels(),audio.getsampwidth(),audio.getframerate()))
                self.assertAlmostEqual(4.0,audio.getnframes()/audio.getframerate(),places=2)
            manifest = json.loads((root/'manifest.json').read_text())
            self.assertEqual(1,len(manifest))
            self.assertEqual('Exact fixture sentence.',manifest[0]['ref_text'])
            self.assertEqual('CPU fixture',manifest[0]['source_title'])
            self.assertEqual('own fixture',manifest[0]['rights_basis'])
            self.assertIs(True,manifest[0]['rights_confirmed'])
            for key,value in measured.items():
                self.assertEqual(value,manifest[0][key])
            self.assertEqual({'manifest.json','manifest.json.lock',result['filename']},{p.name for p in root.iterdir()})

    async def test_cancelled_http_wait_does_not_leave_a_normalized_clip_without_manifest_publication(self):
        from tests.test_voice_reference_import import _tone, _wav_bytes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempted, release, published = threading.Event(), threading.Event(), threading.Event()
            real_import, real_append = voice_design.import_reference_audio, voice_design._append_manifest_entry
            def paused_import(source,destination):
                attempted.set()
                if not release.wait(3):
                    raise AssertionError('Reference fixture was never released')
                return real_import(source,destination)
            def append(path,entry):
                real_append(path,entry)
                published.set()
            app = FastAPI()
            app.include_router(voice_design.router)
            task = None
            try:
                with patch.object(voice_design,'CLONE_VOICES_DIR',str(root)), \
                     patch.object(voice_design,'CLONE_VOICES_MANIFEST',str(root/'manifest.json')), \
                     patch.object(voice_design,'import_reference_audio',side_effect=paused_import), \
                     patch.object(voice_design,'_append_manifest_entry',side_effect=append):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                        task = asyncio.create_task(client.post('/api/clone_voices/upload',
                            files={'file':('fixture.wav',_wav_bytes(_tone(4.0)))},
                            data={'ref_text':'A fixture sentence.','rights_confirmed':'true'}))
                        self.assertTrue(await asyncio.to_thread(attempted.wait,2))
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        release.set()
                        self.assertTrue(await asyncio.to_thread(published.wait,3))
                manifest = json.loads((root/'manifest.json').read_text())
                self.assertEqual(1,len(manifest))
                self.assertTrue((root/manifest[0]['filename']).is_file())
                self.assertEqual({'manifest.json','manifest.json.lock',manifest[0]['filename']},{p.name for p in root.iterdir()})
            finally:
                release.set()
                if task is not None and not task.done():
                    await task


class ZipUploadCaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_multipart_archive_accepts_suffix_case_and_preserves_stem_and_bytes(self):
        for filename in ('dataset.zip', 'DATASET.ZIP', 'MixedCase.ZiP'):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app = FastAPI(); app.include_router(lora.router)
                audio = wav_bytes()
                metadata = json.dumps({'audio':'sample.wav','text':'Fixture spoken line.'})+'\n'
                document = archive_bytes({'metadata.jsonl':metadata,'sample.wav':audio})
                with patch.object(lora,'LORA_DATASETS_DIR',str(root)):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                        response = await client.post('/api/lora/upload_dataset',files={'file':(filename,document,'application/zip')})
                        self.assertEqual(200,response.status_code,response.text)
                        stem = filename.rsplit('.',1)[0]
                        self.assertEqual(stem,response.json()['dataset_id'])
                        self.assertEqual(1,response.json()['sample_count'])
                        self.assertEqual(metadata,(root/stem/'metadata.jsonl').read_text())
                        self.assertEqual(audio,(root/stem/'sample.wav').read_bytes())
                        with wave.open(str(root/stem/'sample.wav'),'rb') as decoded:
                            self.assertEqual(2400,decoded.getnframes()); self.assertEqual(24000,decoded.getframerate())
                        duplicate = await client.post('/api/lora/upload_dataset',files={'file':(filename,document,'application/zip')})
                        self.assertEqual(400,duplicate.status_code,duplicate.text)
                        self.assertIn('already exists',duplicate.json()['detail'])
                        self.assertEqual(audio,(root/stem/'sample.wav').read_bytes())
                        self.assertEqual(metadata,(root/stem/'metadata.jsonl').read_text())
                self.assertEqual(['.dataset-locks',stem],sorted(path.name for path in root.iterdir()))

    async def test_invalid_suffix_and_nullable_filename_refuse_before_reserving_dataset(self):
        from fastapi import HTTPException, UploadFile
        for filename in ('dataset.zip.exe','dataset.tar','datasetzip'):
            with self.subTest(filename=filename),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);app=FastAPI();app.include_router(lora.router)
                with patch.object(lora,'LORA_DATASETS_DIR',str(root)):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                        response=await client.post('/api/lora/upload_dataset',files={'file':(filename,b'invalid')})
                        self.assertEqual(400,response.status_code,response.text)
                self.assertEqual([],list(root.iterdir()))
        with tempfile.TemporaryDirectory() as tmp,patch.object(lora,'LORA_DATASETS_DIR',tmp):
            upload=UploadFile(file=io.BytesIO(b'not an archive'),filename=None)
            with self.assertRaises(HTTPException) as raised:
                await lora.lora_upload_dataset(upload)
            self.assertEqual(400,raised.exception.status_code)
            self.assertEqual(0,upload.file.tell())
            self.assertEqual([],list(Path(tmp).iterdir()))
            await upload.close()
