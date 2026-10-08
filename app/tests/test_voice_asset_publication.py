import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydub import AudioSegment
from routers import voice_design as vd


class VoiceAssetPublicationTests(unittest.TestCase):
    def write_audio(self, path, value, frames):
        segment = AudioSegment(data=value.to_bytes(2, 'little') * frames,
                               sample_width=2, frame_rate=24000, channels=1)
        with path.open('wb') as handle:
            segment.export(handle, format='wav')
        return path.read_bytes()

    def test_clone_manifest_failure_preserves_audio_then_retry_deletes_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps([{'id': 'v1', 'filename': 'v1.wav'}]))
            before = manifest.read_bytes()
            wav = root / 'v1.wav'
            audio = self.write_audio(wav, 123, 2400)
            app = FastAPI(); app.include_router(vd.router)
            with patch.object(vd, 'CLONE_VOICES_DIR', str(root)), \
                    patch.object(vd, 'CLONE_VOICES_MANIFEST', str(manifest)), \
                    TestClient(app, raise_server_exceptions=False) as client:
                with patch.object(vd, '_save_manifest', side_effect=OSError('manifest refused')):
                    self.assertEqual(500, client.delete('/api/clone_voices/v1').status_code)
                self.assertTrue(wav.exists())
                self.assertEqual(audio, wav.read_bytes())
                self.assertEqual(before, manifest.read_bytes())
                self.assertEqual(200, client.delete('/api/clone_voices/v1').status_code)
                self.assertFalse(wav.exists())
                self.assertEqual([], json.loads(manifest.read_text()))

    def test_designed_rollback_failure_keeps_original_audio_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); previews = root / 'previews'; previews.mkdir()
            manifest = root / 'manifest.json'; wav = root / 'v1.wav'
            manifest.write_text(json.dumps([{'id': 'v1', 'filename': wav.name, 'name': 'Old'}]))
            before = manifest.read_bytes()
            old_audio = self.write_audio(wav, 123, 2400)
            new_audio = self.write_audio(previews / 'preview.wav', 321, 4800)
            packet = {'voice_id': 'v1', 'name': 'New', 'description': 'New',
                      'sample_text': 'New sample.', 'preview_file': 'preview.wav'}
            replace = vd.os.replace
            def refuse_restore(src, dst):
                if str(src).endswith('.backup'):
                    raise OSError('synthetic restoration refusal')
                return replace(src, dst)
            app = FastAPI(); app.include_router(vd.router)
            with patch.object(vd, 'DESIGNED_VOICES_DIR', str(root)), \
                    patch.object(vd, 'DESIGNED_VOICES_MANIFEST', str(manifest)), \
                    patch.object(vd, '_save_manifest', side_effect=OSError('manifest refused')), \
                    patch.object(vd.os, 'replace', side_effect=refuse_restore), \
                    TestClient(app) as client:
                with self.assertRaisesRegex(RuntimeError, 'recovery audio kept at') as error:
                    client.post('/api/voice_design/save', json=packet)
            backups = list(root.glob('*.backup'))
            self.assertEqual(1, len(backups))
            self.assertIn(str(backups[0]), str(error.exception))
            self.assertEqual(old_audio, backups[0].read_bytes())
            self.assertEqual(before, manifest.read_bytes())
            self.assertEqual(new_audio, wav.read_bytes())

    def test_designed_update_audio_or_manifest_failure_preserves_pair_then_retry(self):
        for boundary in ('audio', 'manifest'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); previews = root / 'previews'; previews.mkdir()
                manifest = root / 'manifest.json'; wav = root / 'v1.wav'
                original = {'id': 'v1', 'filename': wav.name, 'name': 'Old',
                            'description': 'Old description', 'sample_text': 'Old sample.'}
                manifest.write_text(json.dumps([original])); before = manifest.read_bytes()
                old_audio = self.write_audio(wav, 123, 2400)
                preview = previews / 'preview.wav'
                new_audio = self.write_audio(preview, 321, 4800)
                packet = {'voice_id': 'v1', 'name': 'New', 'description': 'New description',
                          'sample_text': 'New sample.', 'preview_file': preview.name}
                replace, save = vd.os.replace, vd._save_manifest
                def refuse_audio(src, dst):
                    if boundary == 'audio' and Path(dst) == wav:
                        raise PermissionError('synthetic audio refusal')
                    return replace(src, dst)
                def refuse_manifest(*args):
                    if boundary == 'manifest':
                        raise OSError('synthetic manifest refusal')
                    return save(*args)
                app = FastAPI(); app.include_router(vd.router)
                with patch.object(vd, 'DESIGNED_VOICES_DIR', str(root)), \
                        patch.object(vd, 'DESIGNED_VOICES_MANIFEST', str(manifest)), \
                        TestClient(app, raise_server_exceptions=False) as client:
                    with patch.object(vd.os, 'replace', side_effect=refuse_audio), \
                            patch.object(vd, '_save_manifest', side_effect=refuse_manifest):
                        self.assertEqual(500, client.post('/api/voice_design/save', json=packet).status_code)
                    self.assertEqual(before, manifest.read_bytes())
                    self.assertEqual(old_audio, wav.read_bytes())
                    self.assertEqual(new_audio, preview.read_bytes())
                    self.assertEqual([], list(root.glob('*.tmp')))
                    self.assertEqual(200, client.post('/api/voice_design/save', json=packet).status_code)
                    self.assertEqual(new_audio, wav.read_bytes())
                    self.assertEqual('New description', json.loads(manifest.read_text())[0]['description'])
