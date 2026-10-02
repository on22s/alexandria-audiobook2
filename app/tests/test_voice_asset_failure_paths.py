import asyncio
from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient
from routers import voice_design
from utils import file_lock
from tests import test_voice_reference_import as fixtures


class VoiceAssetFailurePathTests(unittest.TestCase):
    def test_manifest_failures_remove_only_new_normalized_clone_file(self):
        payload = fixtures._wav_bytes(fixtures._tone(5.0))
        for failure in (TimeoutError('manifest lock unavailable'), OSError('manifest write failed')):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as root:
                root = Path(root); old = root / 'old.wav'; old.write_bytes(b'prior voice')
                manifest = root / 'manifest.json'; manifest.write_text('[{"id":"old","filename":"old.wav"}]')
                before = manifest.read_bytes()
                async def upload():
                    file = UploadFile(filename='New.wav', file=io.BytesIO(payload))
                    return await voice_design.clone_voices_upload(file, ref_text='Hello there.', rights_confirmed=True)
                with patch.object(voice_design, 'CLONE_VOICES_DIR', str(root)), \
                     patch.object(voice_design, 'CLONE_VOICES_MANIFEST', str(manifest)), \
                     patch.object(voice_design, '_append_manifest_entry', side_effect=failure):
                    with self.assertRaises(type(failure)): asyncio.run(upload())
                self.assertEqual(before, manifest.read_bytes())
                self.assertEqual(b'prior voice', old.read_bytes())
                self.assertEqual({'old.wav', 'manifest.json'}, {p.name for p in root.iterdir()})

    def test_real_manifest_write_error_and_lock_timeout_leave_no_orphan(self):
        payload = fixtures._wav_bytes(fixtures._tone(5.0))
        for mode in ('lock', 'write'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                root = Path(root); manifest = root / 'manifest.json'; manifest.write_text('[]')
                async def upload():
                    return await voice_design.clone_voices_upload(
                        UploadFile(filename='New.wav', file=io.BytesIO(payload)),
                        ref_text='Hello there.', rights_confirmed=True)
                with ExitStack() as contexts:
                    contexts.enter_context(patch.object(voice_design, 'CLONE_VOICES_DIR', str(root)))
                    contexts.enter_context(patch.object(voice_design, 'CLONE_VOICES_MANIFEST', str(manifest)))
                    if mode == 'lock':
                        contexts.enter_context(file_lock(str(manifest)))
                        contexts.enter_context(patch.object(voice_design, 'file_lock',
                            side_effect=lambda path: file_lock(path, timeout=0.05)))
                        failure_type = TimeoutError
                    else:
                        contexts.enter_context(patch.object(voice_design, '_save_manifest',
                            side_effect=OSError('fixture atomic write failure')))
                        failure_type = OSError
                    with self.assertRaises(failure_type): asyncio.run(upload())
                self.assertEqual(b'[]', manifest.read_bytes())
                self.assertEqual({'manifest.json'}, {p.name for p in root.iterdir() if not p.name.endswith('.lock')})

    def test_both_deletion_routes_reject_escape_and_invalid_names_before_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); assets = root / 'voices'; assets.mkdir()
            outside = root / 'outside.wav'; outside.write_bytes(b'unrelated')
            local = assets / 'local.wav'; local.write_bytes(b'local')
            link = assets / 'escape.wav'; link.symlink_to(outside)
            manifest = assets / 'manifest.json'
            app = FastAPI(); app.include_router(voice_design.router)
            with patch.object(voice_design, 'DESIGNED_VOICES_DIR', str(assets)), \
                 patch.object(voice_design, 'CLONE_VOICES_DIR', str(assets)), \
                 patch.object(voice_design, 'DESIGNED_VOICES_MANIFEST', str(manifest)), \
                 patch.object(voice_design, 'CLONE_VOICES_MANIFEST', str(manifest)), TestClient(app) as client:
                for name in ('../outside.wav', str(outside), 'escape.wav', '', None, 42):
                    for route in ('/api/voice_design/bad', '/api/clone_voices/bad'):
                        with self.subTest(name=name, route=route):
                            manifest.write_text(json.dumps([{'id': 'bad', 'filename': name}, {'id': 'good', 'filename': 'local.wav'}]))
                            before = manifest.read_bytes()
                            response = client.delete(route)
                            self.assertEqual(400, response.status_code, response.text)
                            self.assertEqual(before, manifest.read_bytes())
                            self.assertEqual(b'unrelated', outside.read_bytes())
                            self.assertEqual(b'local', local.read_bytes())
                            self.assertTrue(link.is_symlink())

    def test_valid_delete_removes_only_requested_voice_and_unknown_voice_is_404(self):
        for route in ('/api/voice_design/one', '/api/clone_voices/one'):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as root:
                root = Path(root); manifest = root / 'manifest.json'
                manifest.write_text('[{"id":"one","filename":"one.wav"},{"id":"two","filename":"two.wav"}]')
                (root / 'one.wav').write_bytes(b'one'); (root / 'two.wav').write_bytes(b'two')
                app = FastAPI(); app.include_router(voice_design.router)
                with patch.object(voice_design, 'DESIGNED_VOICES_DIR', str(root)), \
                     patch.object(voice_design, 'CLONE_VOICES_DIR', str(root)), \
                     patch.object(voice_design, 'DESIGNED_VOICES_MANIFEST', str(manifest)), \
                     patch.object(voice_design, 'CLONE_VOICES_MANIFEST', str(manifest)), TestClient(app) as client:
                    response = client.delete(route); self.assertEqual(200, response.status_code, response.text)
                    self.assertFalse((root / 'one.wav').exists())
                    self.assertEqual(b'two', (root / 'two.wav').read_bytes())
                    self.assertEqual(['two'], [row['id'] for row in json.loads(manifest.read_text())])
                    self.assertEqual(404, client.delete(route).status_code)
