import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx
from routers import voice_design as vd


class DesignerMutationWorkerTests(unittest.TestCase):
    def test_copy_and_contended_manifest_lock_allow_other_requests_until_atomic_completion(self):
        for operation, boundary in (('save', 'copy'), ('save', 'lock'), ('delete', 'lock')):
            with self.subTest(operation=operation, boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); (root / 'previews').mkdir()
                preview = root / 'previews' / 'p.wav'; preview.write_bytes(b'RIFF' + b'x' * 1024**2)
                audio = root / 'v.wav'; audio.write_bytes(b'old waveform')
                manifest = root / 'manifest.json'
                manifest.write_text(json.dumps([{'id': 'v', 'filename': audio.name, 'name': 'Old'}]))
                before = manifest.read_bytes()
                arrived, release = threading.Event(), threading.Event()
                threads = []
                copy, lock = vd.shutil.copy2, vd.file_lock
                def pause():
                    threads.append(threading.get_ident()); arrived.set()
                    if not release.wait(3):
                        raise TimeoutError('fixture release missing')
                def copy_audio(*args, **kwargs):
                    pause()
                    return copy(*args, **kwargs)
                @contextmanager
                def wait_for_lock(*args, **kwargs):
                    pause()
                    with lock(*args, **kwargs):
                        yield
                app = FastAPI(); app.include_router(vd.router)
                @app.get('/ping')
                async def ping():
                    return {'ok': True}
                async def run():
                    loop_thread = threading.get_ident()
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                        request = (client.post('/api/voice_design/save', json={
                            'voice_id': 'v', 'name': 'New', 'description': 'New description',
                            'sample_text': 'Hello.', 'preview_file': preview.name}) if operation == 'save'
                            else client.delete('/api/voice_design/v'))
                        task = asyncio.create_task(request)
                        watchdog = threading.Timer(0.75, release.set); watchdog.start()
                        try:
                            self.assertTrue(await asyncio.to_thread(arrived.wait, 2))
                            self.assertEqual(200, (await client.get('/ping')).status_code)
                            self.assertFalse(release.is_set(), 'event loop waited for blocking mutation')
                            self.assertFalse(task.done())
                            self.assertTrue(all(t != loop_thread for t in threads))
                            self.assertEqual(before, manifest.read_bytes())
                            self.assertEqual(b'old waveform', audio.read_bytes())
                        finally:
                            release.set(); watchdog.cancel()
                            response = await task
                        self.assertEqual(200, response.status_code, response.text)
                with patch.object(vd, 'DESIGNED_VOICES_DIR', tmp), \
                        patch.object(vd, 'DESIGNED_VOICES_MANIFEST', str(manifest)), \
                        patch.object(vd.shutil, 'copy2', side_effect=copy_audio if boundary == 'copy' else copy), \
                        patch.object(vd, 'file_lock', side_effect=wait_for_lock if boundary == 'lock' else lock):
                    asyncio.run(run())
                rows = json.loads(manifest.read_text())
                if operation == 'save':
                    self.assertEqual(preview.read_bytes(), audio.read_bytes())
                    self.assertEqual(['v'], [row['id'] for row in rows])
                    self.assertEqual('New', rows[0]['name'])
                else:
                    self.assertEqual([], rows)
                    self.assertFalse(audio.exists())
                self.assertEqual([], list(root.glob('*.tmp')) + list(root.glob('*.backup')))


if __name__ == '__main__':
    unittest.main()
