import asyncio
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
import core
from book_state_transaction import get_book_operation_token, get_book_snapshot
from editor_review_facts import get_editor_review_facts
from project import ProjectManager
from routers import editor
from tests.test_support import use_private_cpu_fixture_lock


class OutlierRouteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, value in [("annotated_script.json", []), ("voice_config.json", {}),
                            ("state.json", {"active_book_id": "synthetic"})]:
            (self.root / name).write_text(json.dumps(value), encoding="utf-8")
        self.manager = ProjectManager(str(self.root))
        self.manager.save_chunks([dict(id=index, uid=str(index), text="synthetic",
            speaker="A", status="done", audio_path=f"{index}.wav") for index in range(8)])
        for index in range(8):
            (self.root / f"{index}.wav").write_bytes(b"synthetic")
        self.token = get_book_operation_token(self.root)
        self.state = copy.deepcopy(core.process_state)
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        for entry in self.state.values():
            entry["running"] = False
        for target, name, value in [(core, "process_state", self.state),
                (editor, "process_state", self.state), (editor, "project_manager", self.manager),
                (core, "DATA_DIR", str(self.root)), (core, "_task_claims", {}),
                (core, "_gpu_leases", {})]:
            contexts.enter_context(patch.object(target, name, value))
        contexts.enter_context(use_private_cpu_fixture_lock())
        self.addCleanup(self.release_claims)

    def release_claims(self):
        for name, owner in list(core._task_claims.items()):
            core.release_gpu_task_claim(name, owner["id"])

    def start(self, background=None, token=None):
        background = background if background is not None else BackgroundTasks()
        response = asyncio.run(editor.outlier_check_endpoint(
            editor.OutlierCheckRequest(expected_book_token=token or self.token), background))
        self.assertEqual("started", response["status"])
        return background

    def decode(self, path, check_active):
        check_active()
        return dict(loudness_db=-20., syllable_rate=3., median_pitch_hz=120.,
                    voiced_ratio=.9, error=None)

    def facts(self):
        return get_editor_review_facts(self.manager.load_chunks(),
            lambda path: str(self.root / path), voice_config=get_book_snapshot(self.root)["voices"])

    def test_claim_is_cpu_only_before_worker_runs(self):
        self.state['outlier_check']['progress'] = {'done': 99, 'total': 99}
        self.start()
        self.assertEqual({'done': 0, 'total': 0}, self.state['outlier_check']['progress'])
        self.assertTrue(self.state["outlier_check"]["running"])
        self.assertEqual(self.token, self.state["outlier_check"]["book_token"])
        self.assertNotIn("outlier_check", core._gpu_leases)
        self.assertTrue(core._task_claims["outlier_check"]["cpu_only"])
        self.assertTrue(self.state["outlier_check"]["cpu_only"])

    def test_idle_cancel_refuses_without_changing_state(self):
        before = copy.deepcopy(self.state['outlier_check'])
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(editor.cancel_outlier_check(editor.OutlierCheckRequest(expected_book_token=self.token)))
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual(before, self.state['outlier_check'])

    def test_audio_running_refuses_before_admission(self):
        self.state['audio']['running'] = True
        with self.assertRaises(HTTPException) as raised:
            self.start()
        self.assertEqual(409, raised.exception.status_code)
        self.assertIn('rendering', raised.exception.detail)
        self.assertEqual({}, core._task_claims)

    def test_corrupt_voice_inputs_are_actionable_without_claim(self):
        (self.root / 'voice_config.json').write_text('{broken')
        with self.assertRaises(HTTPException) as raised:
            self.start()
        self.assertEqual(422, raised.exception.status_code)
        self.assertIn('repair', raised.exception.detail)
        self.assertNotIn(str(self.root), raised.exception.detail)
        self.assertEqual({}, core._task_claims)

    def test_corrupt_voices_do_not_hide_other_quality_evidence(self):
        from chunk_outlier_check import get_outlier_population_key
        (self.root / 'voice_config.json').write_text('{broken')
        chunks = self.manager.load_chunks()
        chunks[0]['drift'] = {'score': .9, 'flagged': False}
        chunks[0]['outlier'] = {'population_key': get_outlier_population_key(chunks),
                               'voices_key': editor.get_snapshot_digest({})}
        identity = {'synthetic': 'book'}
        token = editor.get_snapshot_digest(identity)
        with patch.object(editor, 'SCRIPT_PATH', str(self.root / 'annotated_script.json')), \
                patch.object(editor, 'get_editor_book_identity', return_value=identity), \
                patch.object(editor, '_ensure_editor_chunks', return_value=chunks), \
                patch.object(editor, '_get_editor_chunk_views', return_value=chunks), \
                patch.object(editor._editor_poll_snapshots, 'ensure_snapshot',
                    return_value={'book_token': token, 'revision': 'a' * 64}):
            result = editor._get_editor_quality_sync(token)
        self.assertIn('repair', result['outlier_error'])
        self.assertNotIn(str(self.root), result['outlier_error'])
        self.assertEqual(8, len(result['rows']))
        self.assertEqual('measured', result['rows'][0]['drift'])
        self.assertEqual('unavailable', result['rows'][0]['outlier']['status'])
        self.assertIn('unavailable', result['rows'][0]['outlier']['error'])

    def test_oversized_book_refuses_before_admission(self):
        with patch('chunk_outlier_check.MAX_SCAN_CHUNKS', 7):
            with self.assertRaises(HTTPException) as raised:
                self.start()
        self.assertEqual(422, raised.exception.status_code)
        self.assertIn('smaller book', raised.exception.detail)
        self.assertEqual({}, core._task_claims)

    def test_render_start_after_admission_refuses_without_saving(self):
        background = self.start()
        self.state['audio']['running'] = True
        with patch('chunk_outliers.decode_take_acoustics') as decode:
            asyncio.run(background())
        decode.assert_not_called()
        self.assertEqual('failed', self.state['outlier_check']['status'])
        self.assertIn('rendering started', self.state['outlier_check']['error'])
        self.assertTrue(all('outlier' not in row for row in self.manager.load_chunks()))

    def test_duplicate_refused_by_actual_claim_and_success_releases(self):
        background = self.start()
        claim = core._task_claims["outlier_check"]["id"]
        with self.assertRaises(HTTPException) as raised:
            self.start()
        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual(claim, core._task_claims["outlier_check"]["id"])
        with patch("chunk_outliers.decode_take_acoustics", side_effect=self.decode):
            asyncio.run(background())
        self.assertEqual("done", self.state["outlier_check"]["status"])
        self.assertEqual({"done": 8, "total": 8}, self.state["outlier_check"]["progress"])
        self.assertFalse(self.state["outlier_check"]["running"])
        self.assertEqual({}, core._task_claims)
        self.assertEqual(["measured"] * 8, [row["outlier"]["status"] for row in self.facts()])

    def test_start_rejects_stale_token_before_scheduling(self):
        background = BackgroundTasks()
        with self.assertRaises(HTTPException) as raised:
            self.start(background, "0" * 64)
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual([], background.tasks)
        self.assertEqual({}, core._task_claims)

    def test_stale_snapshot_fails_without_partial_publication(self):
        background = self.start()
        (self.root / "voice_config.json").write_text(json.dumps({"A": {"type": "custom"}}))
        with patch("chunk_outliers.decode_take_acoustics", side_effect=self.decode):
            asyncio.run(background())
        self.assertEqual("failed", self.state["outlier_check"]["status"])
        self.assertTrue(self.state["outlier_check"]["error"])
        self.assertTrue(all("outlier" not in row for row in self.manager.load_chunks()))
        self.assertFalse(self.state["outlier_check"]["running"])
        self.assertEqual({}, core._task_claims)

    def test_cancel_owner_and_mid_scan_cancel_publish_nothing(self):
        background = self.start()
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(editor.cancel_outlier_check(
                editor.OutlierCheckRequest(expected_book_token='0' * 64)))
        self.assertEqual(409, raised.exception.status_code)
        self.assertFalse(self.state['outlier_check']['cancel'])
        calls = []
        from threading import Lock
        calls_lock = Lock()
        def decode(path, check_active):
            with calls_lock:
                check_active()
                calls.append(path)
                if len(calls) == 2:
                    response = asyncio.run(editor.cancel_outlier_check(
                        editor.OutlierCheckRequest(expected_book_token=self.token)))
                    self.assertEqual('cancellation_requested', response['status'])
            return self.decode(path, check_active)
        with patch('chunk_outliers.decode_take_acoustics', side_effect=decode):
            asyncio.run(background())
        self.assertEqual(2, len(calls))
        self.assertEqual('cancelled', self.state['outlier_check']['status'])
        self.assertTrue(all('outlier' not in row for row in self.manager.load_chunks()))
        self.assertFalse(self.state['outlier_check']['running'])
        self.assertEqual({}, core._task_claims)

    def test_decode_failure_publishes_complete_unavailable_population(self):
        background = self.start()
        with patch('chunk_outliers.decode_take_acoustics', return_value={'error': 'decode failed'}):
            asyncio.run(background())
        self.assertEqual('done', self.state['outlier_check']['status'])
        self.assertIsNone(self.state['outlier_check']['error'])
        self.assertTrue(all(row.get('outlier') for row in self.manager.load_chunks()))
        self.assertEqual(['unavailable'] * 8, [row['outlier']['status'] for row in self.facts()])
        self.assertFalse(self.state['outlier_check']['running'])
        self.assertEqual({}, core._task_claims)

    def test_completed_facts_fence_actual_voice_change_and_missing_audio(self):
        background = self.start()
        with patch('chunk_outliers.decode_take_acoustics', side_effect=self.decode):
            asyncio.run(background())
        (self.root / 'voice_config.json').write_text(json.dumps({'A': {'type': 'custom'}}))
        self.assertEqual(['unavailable'] * 8, [row['outlier']['status'] for row in self.facts()])
        (self.root / 'voice_config.json').write_text('{}')
        (self.root / '0.wav').unlink()
        facts = self.facts()
        self.assertEqual('unavailable', facts[0]['outlier']['status'])
        self.assertEqual('missing', facts[0]['audio'])
        self.assertIn('metrics', facts[0]['outlier'])
        self.assertIn('Saved audio', facts[0]['outlier']['error'])
        self.assertEqual('measured', facts[1]['outlier']['status'])

    def test_http_routes_are_not_swallowed_by_index_route(self):
        application = FastAPI()
        application.include_router(editor.router)
        with TestClient(application) as client:
            stale = {'expected_book_token': '0' * 64}
            for path in ('/api/chunks/outlier_check', '/api/chunks/outlier_check/cancel'):
                self.assertEqual(409, client.post(path, json=stale).status_code)
            with patch('chunk_outliers.decode_take_acoustics', side_effect=self.decode):
                self.assertEqual(200, client.post('/api/chunks/outlier_check',
                    json={'expected_book_token': self.token}).status_code)
        self.assertEqual('done', self.state['outlier_check']['status'])
