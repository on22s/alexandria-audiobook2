"""Real archive extraction must refuse structural ambiguity before publication."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import lora
from tests.test_upload_event_loop import archive_bytes, wav_bytes


class LoraUploadStructureTests(unittest.TestCase):
    def upload(self, members, root):
        app = FastAPI()
        app.include_router(lora.router)
        with patch.object(lora, 'LORA_DATASETS_DIR', root), TestClient(app, raise_server_exceptions=False) as client:
            return client.post('/api/lora/upload_dataset', files={'file': ('fixture.zip', archive_bytes(members))})

    def assert_unpublished(self, root):
        self.assertFalse((Path(root) / 'fixture').exists())
        self.assertFalse(list(Path(root).glob('_tmp*')))

    def test_metadata_directory_is_a_concrete_input_error(self):
        for name in ('metadata.jsonl/', 'nested/metadata.jsonl/'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                response = self.upload({name: ''}, root)
                self.assertEqual(400, response.status_code, response.text)
                self.assertIn('metadata.jsonl', response.json()['detail'])
                self.assert_unpublished(root)

    def test_flattening_refuses_all_collisions_and_keeps_existing_datasets(self):
        for conflict in ('sample.wav', 'notes.txt', 'assets'):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as root:
                existing = Path(root) / 'existing'
                existing.mkdir()
                sentinel = existing / 'keep'
                sentinel.write_bytes(b'prior dataset')
                members = {'nested/metadata.jsonl': json.dumps({'audio': 'sample.wav', 'text': 'Synthetic sample.'})+'\n',
                           'nested/sample.wav': wav_bytes(), conflict: b'root member'}
                if conflict == 'assets':
                    members['nested/assets/notes.txt'] = 'nested member'
                else:
                    members['nested/'+conflict] = wav_bytes() if conflict == 'sample.wav' else 'nested member'
                response = self.upload(members, root)
                self.assertEqual(400, response.status_code, response.text)
                self.assertIn(conflict, response.json()['detail'])
                self.assert_unpublished(root)
                self.assertEqual(b'prior dataset', sentinel.read_bytes())

    def test_clean_nested_upload_publishes_original_pcm_and_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            pcm = wav_bytes()
            metadata = json.dumps({'audio': 'sample.wav', 'text': 'Synthetic sample.'})+'\n'
            response = self.upload({'nested/metadata.jsonl': metadata, 'nested/sample.wav': pcm}, root)
            self.assertEqual(200, response.status_code, response.text)
            folder = Path(root) / 'fixture'
            self.assertEqual(pcm, (folder / 'sample.wav').read_bytes())
            self.assertEqual(metadata, (folder / 'metadata.jsonl').read_text())
            self.assertFalse((folder / 'nested').exists())


class LoraAudioRetentionPublicationTests(unittest.TestCase):
    def test_locked_old_file_logs_cleanup_failure_after_successful_publication(self):
        import os
        for locked in (False, True):
            with self.subTest(locked=locked), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                folder = root / 'fixture'
                folder.mkdir()
                pcm = wav_bytes()
                staged = root / 'staged.wav'
                staged.write_bytes(pcm)
                for index in range(20):
                    old = folder / f'test_fixture_{index}.wav'
                    old.write_bytes(pcm)
                    os.utime(old, (index + 1, index + 1))
                oldest = folder / 'test_fixture_0.wav'
                remove = os.remove
                def remove_old(path, *args, **kwargs):
                    if locked and str(path) == str(oldest):
                        raise PermissionError('Synthetic old audition locked')
                    return remove(path, *args, **kwargs)
                with patch.object(lora, 'BUILTIN_LORA_DIR', tmp), \
                     patch.object(lora.os, 'remove', side_effect=remove_old), \
                     patch.object(lora.logger, 'warning') as warning:
                    url = lora.apply_lora_audio_publication('fixture', True, str(staged), 'test_fixture_999.wav')
                self.assertTrue(url.endswith('/test_fixture_999.wav'))
                self.assertEqual(pcm, (folder / 'test_fixture_999.wav').read_bytes())
                self.assertFalse(staged.exists())
                self.assertEqual(locked, oldest.exists())
                self.assertEqual(21 if locked else 20, len(list(folder.glob('*.wav'))))
                if locked:
                    warning.assert_called_once()
                    self.assertIn('retention', warning.call_args.args[0])
                else:
                    warning.assert_not_called()
