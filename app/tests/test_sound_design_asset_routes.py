"""Real asset-library routes, exact auditions, and explicit recovery."""
from contextlib import contextmanager
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import sound_design as routes
from sound_design_assets import SoundDesignAssetStore


class SoundDesignAssetRoutesTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            samples = np.tile(np.array([-1200, 0, 1200], dtype=np.int16), 100001)
            sf.write(root/'source.wav', samples, 24000, subtype='PCM_16')
            store = SoundDesignAssetStore(root)
            api = FastAPI(); api.include_router(routes.router)
            with patch.object(routes, 'DATA_DIR', str(root)), TestClient(api) as client:
                yield root, store, client, samples

    def test_uploaded_audio_is_admitted_and_private_stage_is_released(self):
        with self.fixture() as (root, store, client, samples):
            response = client.post('/api/sound_design/assets/import',
                files={'file': ('effect.wav', (root/'source.wav').read_bytes(), 'audio/wav')})
            self.assertEqual(200, response.status_code, response.text)
            descriptor = response.json()
            self.assertEqual('effect.wav', store.list_assets()['assets'][0]['display_name'])
            with store.open_canonical(descriptor['asset_id']) as (stream, facts):
                self.assertEqual(descriptor, facts)
                self.assertEqual(len(samples) * 4, len(stream.read()))
            self.assertEqual(['quota.lock'], [path.name for path in (root/'.sound_design_uploads').iterdir()])

    def test_invalid_upload_is_retained_as_incomplete_without_a_ready_asset(self):
        with self.fixture() as (root, store, client, samples):
            response = client.post('/api/sound_design/assets/import',
                files={'file': ('invalid.wav', b'not audio', 'audio/wav')})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(['incomplete'], [row['state'] for row in store.list_assets()['assets']])
            self.assertEqual(['quota.lock'], [path.name for path in (root/'.sound_design_uploads').iterdir()])

    def test_list_and_offset_audition_preserve_exact_frames_and_bound_duration(self):
        with self.fixture() as (root, store, client, samples):
            descriptor = store.import_asset('source.wav')
            listing = client.get('/api/sound_design/assets')
            self.assertEqual(200, listing.status_code, listing.text)
            self.assertEqual(descriptor['asset_id'], listing.json()['assets'][0]['asset_id'])
            for start in (1, len(samples) - 2):
                response = client.get(f"/api/sound_design/assets/{descriptor['asset_id']}/audition",
                    params={'sha256': descriptor['sha256'], 'start_frame': start})
                self.assertEqual(200, response.status_code, response.text[:200] if response.status_code != 200 else '')
                self.assertEqual('no-store', response.headers['cache-control'])
                with wave.open(io.BytesIO(response.content)) as audio:
                    self.assertEqual((1, 4, 24000), (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()))
                    expected = (samples[start:start+240000].astype(np.int32) * 65536).astype('<i4').tobytes()
                    self.assertEqual(expected, audio.readframes(audio.getnframes()))

    def test_stale_hash_outside_range_and_corrupt_media_refuse_without_mutation(self):
        with self.fixture() as (root, store, client, samples):
            descriptor = store.import_asset('source.wav')
            endpoint = f"/api/sound_design/assets/{descriptor['asset_id']}/audition"
            for parameters in ({'sha256': '0'*64}, {'sha256': descriptor['sha256'], 'start_frame': len(samples)}):
                self.assertEqual(409, client.get(endpoint, params=parameters).status_code)
            path = store.directory / (descriptor['asset_id'] + '.pcm')
            raw = path.read_bytes(); damaged = b'\xff' + raw[1:]; path.write_bytes(damaged)
            self.assertEqual(409, client.get(endpoint, params={'sha256': descriptor['sha256']}).status_code)
            self.assertEqual(damaged, path.read_bytes())

    def test_explicit_cleanup_refuses_ready_and_removes_only_incomplete(self):
        with self.fixture() as (root, store, client, samples):
            ready = store.import_asset('source.wav')
            self.assertEqual(409, client.post(f"/api/sound_design/assets/{ready['asset_id']}/purge_incomplete").status_code)
            sf.write(root/'other.wav', -samples, 24000, subtype='PCM_16')
            with patch('sound_design_decode.decode_sound_design_asset', side_effect=ValueError('Synthetic failure')):
                with self.assertRaises(ValueError):
                    store.import_asset('other.wav')
            pending = next(row for row in store.list_assets()['assets'] if row['state'] == 'incomplete')
            response = client.post(f"/api/sound_design/assets/{pending['asset_id']}/purge_incomplete")
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual([ready['asset_id']], [row['asset_id'] for row in store.list_assets()['assets']])
            self.assertTrue((root/'other.wav').exists())
