"""Synthetic estimates for the finished audiobook's runtime and size (issue #1330)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import soundfile
from fastapi import FastAPI
from fastapi.testclient import TestClient

import runtime_estimate
from project import ProjectManager
from routers import editor


class BookRuntimeEstimateTests(unittest.TestCase):
    # Every synthetic row pins pause_after to 0 so the speech arithmetic stays exact;
    # test_pauses_between_chunks_follow_the_export_rule covers the pauses themselves.

    def test_configured_pauses_and_overrides(self):
        chunks = [
            {'id': i, 'speaker': speaker, 'text': 'a' * 14}
            for i, speaker in enumerate(['ANNA', 'ANNA', 'BEN', 'ANNA'])
        ]
        for overrides, silence in [({}, 5.0), ({0: 0, 1: 1500}, 3.5)]:
            with self.subTest(overrides=overrides):
                rows = [{**row, **({'pause_after': overrides[i]} if i in overrides else {})}
                        for i, row in enumerate(chunks)]
                estimate = runtime_estimate.get_book_runtime_estimate(rows, {}, 2000, 1000)
                self.assertEqual(estimate['pause_seconds'], silence)
                for field in ('total_seconds', 'low_seconds', 'high_seconds'):
                    self.assertEqual(estimate[field], 4 + silence)
                self.assertEqual(estimate['wav_bytes'], (4 + silence) * 48000)
                self.assertEqual(estimate['mp3_bytes'], (4 + silence) * 16000)
        for rows, expected in [([], 0), ([chunks[0]], 1),
                               ([chunks[0], None, chunks[1], 'odd', chunks[2]], 6)]:
            with self.subTest(rows=rows):
                self.assertEqual(runtime_estimate.get_book_runtime_estimate(
                    rows, {}, 2000, 1000)['total_seconds'], expected)

    def test_measured_speech_with_configured_pauses(self):
        chunks = [
            {'id': i, 'speaker': speaker, 'text': 'a' * chars, 'status': status}
            for i, (speaker, chars, status) in enumerate([
                ('ANNA', 30, 'done'), ('ANNA', 60, 'done'),
                ('BEN', 30, 'done'), ('ANNA', 140, 'pending')])
        ]
        estimate = runtime_estimate.get_book_runtime_estimate(
            chunks, {0: 3.0, 1: 3.0, 2: 1.0}, 2000, 1000)
        self.assertEqual(estimate['basis'], 'measured')
        self.assertEqual(estimate['pause_seconds'], 5.0)
        self.assertEqual(estimate['total_seconds'], 19.0)
        self.assertEqual(estimate['low_seconds'], 16.7)
        self.assertEqual(estimate['high_seconds'], 26.0)
        self.assertEqual(estimate['wav_bytes'], 912000)
        self.assertEqual(estimate['mp3_bytes'], 304000)

    def test_pauses_between_chunks_follow_the_export_rule(self):
        chunks = [
            {'id': 0, 'speaker': 'ANNA', 'text': 'a' * 14, 'status': 'pending'},
            {'id': 1, 'speaker': 'ANNA', 'text': 'b' * 14, 'status': 'pending'},
            {'id': 2, 'speaker': 'BEN', 'text': 'c' * 14, 'status': 'pending', 'pause_after': 1250},
            {'id': 3, 'speaker': 'ANNA', 'text': 'd' * 14, 'status': 'pending'},
        ]
        estimate = runtime_estimate.get_book_runtime_estimate(chunks, {})
        # 250 ms same speaker, 500 ms speaker change, 1250 ms explicit override.
        self.assertEqual(estimate['pause_seconds'], 2.0)
        self.assertEqual(estimate['total_seconds'], 6.0)

    def test_measured_median_and_range(self):
        chunks = [
            {'id': 0, 'text': 'a' * 30, 'pause_after': 0, 'status': 'done'},
            {'id': 1, 'text': 'b' * 60, 'pause_after': 0, 'status': 'done'},
            {'id': 2, 'text': 'c' * 30, 'pause_after': 0, 'status': 'done'},
            {'id': 3, 'text': 'd' * 140, 'pause_after': 0, 'status': 'pending'},
        ]
        estimate = runtime_estimate.get_book_runtime_estimate(
            chunks, {0: 3.0, 1: 3.0, 2: 1.0})
        self.assertEqual(estimate['basis'], 'measured')
        self.assertEqual(estimate['samples'], 3)
        self.assertEqual(estimate['chars_per_second'], 20.0)
        self.assertEqual(estimate['total_seconds'], 14.0)
        self.assertEqual(estimate['low_seconds'], 11.7)
        self.assertEqual(estimate['high_seconds'], 21.0)
        self.assertEqual(estimate['done_chunks'], 3)
        self.assertEqual(estimate['total_chunks'], 4)
        self.assertEqual(estimate['wav_bytes'], 672000)
        self.assertEqual(estimate['mp3_bytes'], 224000)

    def test_fewer_than_three_samples_uses_fallback(self):
        chunks = [
            {'id': 0, 'text': 'a' * 20, 'pause_after': 0, 'status': 'done'},
            {'id': 1, 'text': 'b' * 20, 'pause_after': 0, 'status': 'done'},
            {'id': 2, 'text': 'c' * 14, 'pause_after': 0, 'status': 'pending'},
        ]
        estimate = runtime_estimate.get_book_runtime_estimate(
            chunks, {0: 2.0, 1: 4.0})
        self.assertEqual(estimate['basis'], 'fallback')
        self.assertEqual(estimate['samples'], 2)
        self.assertEqual(estimate['chars_per_second'], 14.0)
        self.assertEqual(estimate['total_seconds'], 7.0)
        self.assertEqual(estimate['low_seconds'], 7.0)
        self.assertEqual(estimate['high_seconds'], 7.0)

    def test_done_chunks_contribute_measured_seconds(self):
        chunks = [
            {'id': 0, 'text': 'a' * 10, 'pause_after': 0, 'status': 'done'},
            {'id': 1, 'text': 'b' * 140, 'pause_after': 0, 'status': 'pending'},
        ]
        estimate = runtime_estimate.get_book_runtime_estimate(chunks, {0: 5.0})
        self.assertEqual(estimate['basis'], 'fallback')
        self.assertEqual(estimate['total_seconds'], 15.0)
        self.assertEqual(estimate['done_chunks'], 1)
        self.assertEqual(estimate['total_chunks'], 2)

    def test_zero_durations_and_empty_text_are_not_samples(self):
        chunks = [
            {'id': 0, 'text': 'a' * 40, 'pause_after': 0, 'status': 'done'},
            {'id': 1, 'text': 'b' * 40, 'pause_after': 0, 'status': 'done'},
            {'id': 2, 'text': '', 'pause_after': 0, 'status': 'done'},
            {'id': 3, 'text': 'c' * 3, 'pause_after': 0, 'status': 'done'},
            'not a chunk row',
            {'id': 4, 'pause_after': 0, 'status': 'pending'},
        ]
        estimate = runtime_estimate.get_book_runtime_estimate(
            chunks, {0: 0, 1: -2.0, 2: 2.0, 3: 3.0})
        self.assertEqual(estimate['samples'], 1)
        self.assertEqual(estimate['basis'], 'fallback')
        self.assertEqual(estimate['done_chunks'], 4)
        self.assertEqual(estimate['total_chunks'], 5)
        self.assertEqual(estimate['total_seconds'], 10.7)


class AudioEstimateEndpointTests(unittest.TestCase):
    def test_endpoint_uses_configured_pauses_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = ProjectManager(tmp)
            (root / 'state.json').write_text(json.dumps(
                {'active_book_id': 'A', 'book_generation': 'one'}))
            (root / 'config.json').write_text(json.dumps({'tts': {
                'pause_between_speakers_ms': 2000, 'pause_same_speaker_ms': 1000}}))
            manager.save_chunks([
                {'id': i, 'uid': f'row-{i}', 'speaker': speaker,
                 'text': 'a' * 14, 'status': 'pending'}
                for i, speaker in enumerate(['ANNA', 'ANNA', 'BEN'])])
            before = {name: (root / name).read_bytes()
                      for name in ('state.json', 'config.json', 'chunks.json')}
            api = FastAPI()
            api.include_router(editor.router)
            with patch.object(editor, 'project_manager', manager), patch.object(
                    editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), patch.object(
                    manager, '_load_pause_defaults', wraps=manager._load_pause_defaults) as load, patch.object(
                    manager, 'save_chunks', side_effect=AssertionError('estimate wrote chunks')), patch.object(
                    manager, 'get_engine', side_effect=AssertionError('estimate requested inference')), TestClient(api) as client:
                response = client.get('/api/audio/estimate')
            self.assertEqual(response.status_code, 200)
            estimate = response.json()
            self.assertEqual(estimate['pause_seconds'], 3.0)
            for field in ('total_seconds', 'low_seconds', 'high_seconds'):
                self.assertEqual(estimate[field], 6.0)
            self.assertEqual(estimate['wav_bytes'], 288000)
            self.assertEqual(estimate['mp3_bytes'], 96000)
            self.assertEqual(estimate['unreadable'], 0)
            load.assert_called_once_with()
            self.assertEqual(before, {name: (root / name).read_bytes() for name in before})

    def test_endpoint_counts_unreadable_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_manager = ProjectManager(tmp)
            (root / 'state.json').write_text(
                json.dumps({'active_book_id': 'A', 'book_generation': 'one'}))
            soundfile.write(str(root / 'good-0.wav'), [0.0] * 12000, 24000)
            soundfile.write(str(root / 'good-1.wav'), [0.0] * 12000, 24000)
            (root / 'broken.wav').write_text('synthetic non-audio bytes')
            project_manager.save_chunks([
                {'id': 0, 'text': 'Synthetic row zero', 'pause_after': 0, 'status': 'done',
                 'audio_path': 'good-0.wav'},
                {'id': 1, 'text': 'Synthetic row one', 'pause_after': 0, 'status': 'done',
                 'audio_path': 'good-1.wav'},
                {'id': 2, 'text': 'Synthetic row two', 'pause_after': 0, 'status': 'done',
                 'audio_path': 'broken.wav'},
                {'id': 3, 'text': 'Synthetic pending row', 'pause_after': 0, 'status': 'pending'},
            ])
            api = FastAPI()
            api.include_router(editor.router)
            with patch.object(editor, 'project_manager', project_manager), patch.object(
                    editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), TestClient(api) as client:
                response = client.get('/api/audio/estimate')
            self.assertEqual(response.status_code, 200)
            estimate = response.json()
            self.assertEqual(estimate['unreadable'], 1)
            self.assertEqual(estimate['samples'], 2)
            self.assertEqual(estimate['basis'], 'fallback')
            self.assertEqual(estimate['done_chunks'], 3)
            self.assertEqual(estimate['total_chunks'], 4)
            self.assertEqual(estimate['total_seconds'], 3.7)
            self.assertEqual(estimate['chars_per_second'], 14.0)
