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
