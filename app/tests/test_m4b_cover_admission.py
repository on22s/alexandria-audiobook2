import asyncio
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor
from project import ProjectManager
from tests import test_chapter_export as fixtures


def image_bytes(fmt):
    data = io.BytesIO()
    Image.new('RGB', (32, 24), (30, 100, 170)).save(data, format=fmt)
    return data.getvalue()


class M4bCoverAdmissionTests(unittest.TestCase):
    def test_invalid_and_truncated_uploads_preserve_prior_cover_and_remove_owned_stage(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); cover = root / 'm4b_cover.jpg'
            original = image_bytes('JPEG'); cover.write_bytes(original)
            foreign = root / 'm4b_cover.jpg.upload.foreign'; foreign.write_bytes(b'keep')
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'DATA_DIR', str(root)), TestClient(app) as client:
                for data in (b'not an image', b'', image_bytes('JPEG')[:-60], image_bytes('PNG')[:-20]):
                    with self.subTest(length=len(data)):
                        response = client.post('/api/m4b_cover', files={'file': ('cover.jpg', data, 'image/jpeg')})
                        self.assertEqual(400, response.status_code, response.text)
                        self.assertEqual(original, cover.read_bytes())
                        self.assertEqual(b'keep', foreign.read_bytes())
                        self.assertEqual([foreign], list(root.glob('m4b_cover.jpg.upload.*')))

    def test_accepted_covers_survive_actual_m4b_mux_as_attached_picture(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); pm, chunks = fixtures._project(str(root)); pm.save_chunks(chunks)
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'DATA_DIR', str(root)), TestClient(app) as client:
                for fmt in ('JPEG', 'PNG', 'GIF'):
                    with self.subTest(format=fmt):
                        payload = image_bytes(fmt)
                        response = client.post('/api/m4b_cover', files={'file': ('cover.' + fmt.lower(), payload, 'image/' + fmt.lower())})
                        self.assertEqual(200, response.status_code, response.text)
                        cover = root / 'm4b_cover.jpg'
                        if fmt in ('JPEG', 'PNG'): self.assertEqual(payload, cover.read_bytes())
                        with Image.open(cover) as image:
                            image.load(); self.assertEqual((32, 24), image.size)
                            self.assertEqual('PNG' if fmt == 'PNG' else 'JPEG', image.format)
                        ok, message = pm.merge_m4b(metadata={'cover_path': str(cover), 'title': 'Cover fixture'})
                        self.assertTrue(ok, message)
                        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(root / 'audiobook.m4b')],
                                               capture_output=True, text=True, check=True, timeout=10)
                        streams = json.loads(probe.stdout)['streams']
                        pictures = [row for row in streams if row.get('disposition', {}).get('attached_pic')]
                        self.assertEqual(1, len(pictures))
                        self.assertEqual((32, 24), (pictures[0]['width'], pictures[0]['height']))
                        self.assertTrue(any(row['codec_type'] == 'audio' for row in streams))
                        self.assertFalse(list(root.glob('m4b_cover.jpg.upload.*')))

    def test_decoder_pixel_limits_and_publication_failure_preserve_prior_cover(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); cover = root / 'm4b_cover.jpg'
            original = image_bytes('JPEG'); cover.write_bytes(original)
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'DATA_DIR', str(root)), TestClient(app) as client:
                for pixel_limit in (200, 400):
                    with self.subTest(pixel_limit=pixel_limit), patch.object(Image, 'MAX_IMAGE_PIXELS', pixel_limit):
                        response = client.post('/api/m4b_cover', files={'file': ('cover.png', image_bytes('PNG'), 'image/png')})
                        self.assertEqual(400, response.status_code, response.text)
                        self.assertEqual(original, cover.read_bytes())
                        self.assertFalse(list(root.glob('m4b_cover.jpg.upload.*')))
                with patch.object(editor.os, 'replace', side_effect=OSError('fixture cover publication failure')):
                    with self.assertRaisesRegex(OSError, 'fixture cover publication failure'):
                        client.post('/api/m4b_cover', files={'file': ('cover.gif', image_bytes('GIF'), 'image/gif')})
                self.assertEqual(original, cover.read_bytes())
                self.assertFalse(list(root.glob('m4b_cover.jpg.upload.*')))

    def test_validation_does_not_block_same_event_loop_http(self):
        with tempfile.TemporaryDirectory() as root:
            app = FastAPI(); app.include_router(editor.router)
            @app.get('/ping')
            async def ping(): return {'ok': True}
            arrived, release = threading.Event(), threading.Event()
            original = Image.Image.load
            threads = []
            def decode(image, *args, **kwargs):
                threads.append(threading.get_ident()); arrived.set()
                if not release.wait(3): raise TimeoutError('decode not released')
                return original(image, *args, **kwargs)
            payload = image_bytes('JPEG')
            async def run():
                loop_id = threading.get_ident()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                    task = asyncio.create_task(client.post('/api/m4b_cover', files={'file': ('cover.jpg', payload, 'image/jpeg')}))
                    watchdog = threading.Timer(0.5, release.set); watchdog.start()
                    try:
                        self.assertTrue(await asyncio.to_thread(arrived.wait, 2))
                        self.assertEqual(200, (await client.get('/ping')).status_code)
                        self.assertFalse(release.is_set())
                        self.assertFalse(task.done())
                        self.assertTrue(all(value != loop_id for value in threads))
                    finally:
                        release.set(); watchdog.cancel(); response = await task
                    self.assertEqual(200, response.status_code, response.text)
            with patch.object(editor, 'DATA_DIR', root), patch.object(Image.Image, 'load', side_effect=decode, autospec=True):
                asyncio.run(run())
