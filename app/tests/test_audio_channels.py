"""Actual channel PCM, prefix bounds, staging lifecycle and run identity."""
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import numpy as np
import soundfile as sf

from audio_channel_preview import render_audio_channel_preview, _PREVIEW_SLOTS
from audio_channel_staging import AudioChannelStaging, STAGE_SECONDS
from audio_channels import get_validated_audio_channel_filter
import voice_reference_import as reference
from tests.test_preparer_run_state import preparer
from alexandria_run_manifest import get_run_identity


class AudioChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rate = 24000
        t = np.arange(self.rate * 5) / self.rate
        self.left = .25 * np.sin(2 * np.pi * 317 * t)
        self.right = .25 * np.sin(2 * np.pi * 997 * t)
        self.path = self.root / 'stereo.wav'
        sf.write(self.path, np.column_stack((self.left, self.right)), self.rate, subtype='PCM_16')

    def test_actual_preview_import_and_asr_stream_select_same_channel(self):
        original = self.path.read_bytes()
        for channel, expected in [('left', self.left), ('right', self.right)]:
            with self.subTest(channel=channel):
                wav, count = render_audio_channel_preview(self.path, channel)
                preview, rate = sf.read(io.BytesIO(wav))
                self.assertEqual((2, self.rate), (count, rate))
                np.testing.assert_allclose(expected, preview, atol=1/32768)
                target = self.root / (channel + '.wav')
                measures, problems = reference.import_reference_audio(self.path, target, channel=channel)
                self.assertEqual([], problems)
                imported, rate = sf.read(target)
                np.testing.assert_array_equal(preview, imported)
                asr = preparer.decode_audio_to_asr_streams(str(self.path), str(self.root / 'scratch.wav'),
                    str(self.root / 'asr.f32'), channel=channel)
                asr._mmap.close()
                scratch, rate = sf.read(self.root / 'scratch.wav')
                np.testing.assert_array_equal(imported, scratch)
        self.assertEqual(original, self.path.read_bytes())

    def test_opposite_phase_mixing_does_not_silently_replace_selected_channel(self):
        sf.write(self.path, np.column_stack((self.left, -self.left)), self.rate)
        mixed, _ = render_audio_channel_preview(self.path, 'mix')
        selected, _ = render_audio_channel_preview(self.path, 'left')
        mix, _ = sf.read(io.BytesIO(mixed))
        left, _ = sf.read(io.BytesIO(selected))
        self.assertLess(np.max(np.abs(mix)), .0001)
        self.assertGreater(np.max(np.abs(left)), .24)
        _, problems = reference.import_reference_audio(self.path, self.root / 'mixed.wav')
        self.assertTrue(any('near-empty' in p for p in problems))

    def test_mono_default_parity_missing_right_and_unknown_selection(self):
        sf.write(self.path, self.left, self.rate)
        before = self.root / 'before.wav'
        explicit = self.root / 'explicit.wav'
        reference.normalize_reference_audio(self.path, before)
        reference.normalize_reference_audio(self.path, explicit, 'mix')
        self.assertEqual(before.read_bytes(), explicit.read_bytes())
        for channel in ('right', 'all', '1;not-a-command'):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                get_validated_audio_channel_filter(self.path, channel)

    def test_preview_prefix_is_bounded_and_slots_are_released_after_failure(self):
        sf.write(self.path, np.tile(self.left, 4), self.rate)
        wav, _ = render_audio_channel_preview(self.path, 'mix')
        with wave.open(io.BytesIO(wav)) as audio:
            self.assertEqual(self.rate * 10, audio.getnframes())
        with self.assertRaises(ValueError):
            render_audio_channel_preview(self.path, 'invalid')
        self.assertTrue(_PREVIEW_SLOTS.acquire(blocking=False))
        self.assertTrue(_PREVIEW_SLOTS.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(ValueError, 'already running'):
                render_audio_channel_preview(self.path, 'mix')
        finally:
            _PREVIEW_SLOTS.release(); _PREVIEW_SLOTS.release()

    def test_staging_reuses_original_expires_and_refuses_foreign_handles(self):
        store = AudioChannelStaging(self.root)
        token, path = store.create_stage('original.wav', 'clone')
        path.write_bytes(self.path.read_bytes())
        dest = self.root / 'import.wav'
        store.copy_stage(token, dest, 'clone')
        self.assertEqual(path.read_bytes(), dest.read_bytes())
        with self.assertRaisesRegex(ValueError, 'different operation'):
            store.copy_stage(token, self.root / 'bad.wav', 'preparer')
        for bad in ('../outside', 'a'*31, 'a'*33):
            with self.subTest(token=bad), self.assertRaises(ValueError):
                store.remove_stage(bad)
        with patch('audio_channel_staging.time.time', return_value=time.time() + STAGE_SECONDS + 1):
            with self.assertRaisesRegex(ValueError, 'expired'):
                with store.open_stage(token):
                    self.fail('expired input must not be read')
        self.assertFalse(path.exists())

    def test_move_is_single_use_and_failed_copy_does_not_delete_source(self):
        store = AudioChannelStaging(self.root)
        token, path = store.create_stage('original.wav', 'preparer')
        path.write_bytes(self.path.read_bytes())
        with patch('audio_channel_staging.shutil.copyfile', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                store.copy_stage(token, self.root / 'copy.wav', 'preparer')
        self.assertTrue(path.exists())
        target = self.root / 'published.wav'
        store.move_stage(token, target, 'preparer')
        self.assertEqual(self.path.read_bytes(), target.read_bytes())
        with self.assertRaises(ValueError):
            store.move_stage(token, self.root / 'again.wav', 'preparer')

    def test_selection_changes_identity_but_default_mix_keeps_old_identity(self):
        args = SimpleNamespace(audio=str(self.path), source=None, phase='asr', resume=True)
        old = get_run_identity(args)
        args.audio_channel = 'mix'
        self.assertEqual(old, get_run_identity(args))
        args.audio_channel = 'left'
        left = get_run_identity(args)
        args.audio_channel = 'right'
        self.assertNotEqual(left, get_run_identity(args))
        self.assertNotEqual(old, left)

    def test_cancelled_preview_stops_owned_child_before_returning(self):
        import subprocess
        from audio_channel_preview import run_preview_decode
        from unittest.mock import MagicMock
        process = MagicMock()
        process.poll.return_value = None
        with patch('audio_channel_preview.start_owned_subprocess', return_value=process), \
                patch('audio_channel_preview.stop_owned_subprocess') as stop:
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                run_preview_decode(['fixture'], lambda: True)
        stop.assert_called_once_with(process, timeout=1)
        process._alexandria_control.close.assert_called_once()

    def test_batch_http_consumes_previews_once_and_dispatches_selected_channel(self):
        from contextlib import ExitStack
        from tests.test_preparer_batch_uploads import PreparerBatchUploadTests
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            support = PreparerBatchUploadTests()
            client, uploads, captures, state, _, _ = support.setup_api(Path(tmp), stack)
            store = AudioChannelStaging(uploads)
            token, source = store.create_stage('one.wav', 'preparer')
            source.write_bytes(self.path.read_bytes())
            config = support.config(['one.wav', 'two.wav'])
            config['tasks'][0].update(preview_token=token, audio_channel='right')
            config['tasks'][1]['audio_channel'] = 'left'
            response = client.post('/api/preparer/batch/upload_start',
                data={'config_json': json.dumps(config)},
                files=[('audio_files', ('two.wav', b'second selected upload', 'audio/wav'))])
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(self.path.read_bytes(), captures[0][1])
            self.assertEqual(b'second selected upload', captures[1][1])
            self.assertEqual(['right', 'left'], [cmd[cmd.index('--audio-channel') + 1] for _, _, cmd in captures])
            self.assertFalse(source.exists())
            self.assertEqual(['done', 'done'], [t['status'] for t in state['batch_preparer']['tasks']])

    def test_real_cancelled_child_is_reaped(self):
        import os
        import sys
        from audio_channel_preview import run_preview_decode
        pid_file = self.root / 'pid'
        script = 'import os,pathlib,time;pathlib.Path(' + repr(str(pid_file)) + ').write_text(str(os.getpid()));time.sleep(30)'
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            run_preview_decode([sys.executable, '-c', script], lambda: pid_file.exists())
        self.assertLess(time.monotonic() - started, 10)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_stage_preview_and_clone_import_use_same_bytes_and_keep_provenance(self):
        from contextlib import ExitStack
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import voice_design
        with ExitStack() as stack:
            uploads = self.root / 'uploads'; uploads.mkdir()
            voices = self.root / 'voices'; voices.mkdir()
            for name, value in [('UPLOADS_DIR', str(uploads)), ('CLONE_VOICES_DIR', str(voices)),
                                ('CLONE_VOICES_MANIFEST', str(voices / 'manifest.json'))]:
                stack.enter_context(patch.object(voice_design, name, value))
            stack.enter_context(patch.object(voice_design, 'check_disk_space', return_value=(True, 20)))
            api = FastAPI(); api.include_router(voice_design.router)
            client = stack.enter_context(TestClient(api))
            staged = client.post('/api/audio_channels/stage', data={'purpose': 'clone'},
                files={'file': ('stereo.wav', self.path.read_bytes(), 'audio/wav')})
            self.assertEqual(200, staged.status_code, staged.text)
            token = staged.json()['token']
            preview = client.get('/api/audio_channels/preview', params={'token': token, 'channel': 'right'})
            self.assertEqual(200, preview.status_code, preview.text[:80])
            self.assertEqual('2', preview.headers['X-Audio-Channels'])
            result = client.post('/api/clone_voices/upload', data={'preview_token': token,
                'audio_channel': 'right', 'ref_text': 'Synthetic reference transcript.', 'rights_confirmed': 'true'})
            self.assertEqual(200, result.status_code, result.text)
            manifest = json.loads((voices / 'manifest.json').read_text())
            self.assertEqual(('right', 2), (manifest[0]['audio_channel'], manifest[0]['source_channels']))
            imported, _ = sf.read(voices / result.json()['filename'])
            heard, _ = sf.read(io.BytesIO(preview.content))
            np.testing.assert_array_equal(imported, heard)
            self.assertEqual(200, client.delete('/api/audio_channels/stage/' + token).status_code)
            self.assertEqual(400, client.get('/api/audio_channels/preview', params={'token': token}).status_code)
            self.assertEqual([], [p for p in voices.iterdir() if p.name.startswith('.upload_')])

    def test_metadata_wait_does_not_block_async_request_loop(self):
        import asyncio
        import threading
        from routers import voice_design
        from fastapi import HTTPException
        store = AudioChannelStaging(self.root)
        token, path = store.create_stage('stereo.wav', 'clone')
        path.write_bytes(self.path.read_bytes())
        entered = threading.Event(); release = threading.Event()
        real_metadata = store.ensure_stage_metadata
        def delayed(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError('event loop did not reach release')
            return real_metadata(*args)
        async def run():
            task = asyncio.create_task(voice_design.clone_voices_upload(None, preview_token=token,
                ref_text='', rights_confirmed=False))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(entered.is_set())
            release.set()
            with self.assertRaises(HTTPException) as error:
                await task
            self.assertEqual(400, error.exception.status_code)
        with patch.object(voice_design, 'get_audio_channel_staging', return_value=store), \
                patch.object(store, 'ensure_stage_metadata', side_effect=delayed):
            asyncio.run(run())
