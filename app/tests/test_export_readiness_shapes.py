import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor


class ExportReadinessShapeTests(unittest.TestCase):
    def test_both_routes_refuse_unusable_json_before_any_task(self):
        cases = [('{}', '{}'), ('null', '{}'), ('not-json', '{}'), (None, '{}'),
                 ('[]', '[]'), ('[]', 'null'), ('[]', 'not-json'), ('[]', None),
                 ('[{"speaker":"Hero"}]', '{"Hero":["ready"]}')]
        with tempfile.TemporaryDirectory() as root:
            script, config = Path(root, 'script.json'), Path(root, 'voice_config.json')
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'SCRIPT_PATH', str(script)), \
                 patch.object(editor, 'DATA_DIR', root), \
                 patch.object(editor, 'schedule_claimed_background_task') as schedule, TestClient(app) as client:
                for script_text, config_text in cases:
                    for path, content in ((script, script_text), (config, config_text)):
                        if content is None:
                            path.unlink(missing_ok=True)
                        else:
                            path.write_text(content)
                    before = {p.name: p.read_bytes() for p in (script, config) if p.exists()}
                    for route in ('/api/merge_m4b', '/api/export_chapters'):
                        with self.subTest(script=script_text, config=config_text, route=route):
                            response = client.post(route, json={'require_ready': True})
                            self.assertEqual(503, response.status_code, response.text)
                            self.assertIn('readiness unavailable', response.json()['detail'])
                            schedule.assert_not_called()
                            self.assertEqual(before, {p.name: p.read_bytes() for p in (script, config) if p.exists()})

    def test_readiness_still_reports_exact_missing_speakers_and_allows_ready_state(self):
        with tempfile.TemporaryDirectory() as root:
            script, config = Path(root, 'script.json'), Path(root, 'voice_config.json')
            script.write_text(json.dumps([{'speaker': ' Hero '}, {'type': 'NARRATOR'},
                                          {'speaker': 'Hero'}, {'speaker': ''}]))
            config.write_text('{"Hero":{"ready":true}}')
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'SCRIPT_PATH', str(script)), \
                 patch.object(editor, 'DATA_DIR', root), \
                 patch.object(editor, 'schedule_claimed_background_task') as schedule, TestClient(app) as client:
                for route in ('/api/merge_m4b', '/api/export_chapters'):
                    response = client.post(route, json={'require_ready': True})
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual(['NARRATOR'], response.json()['detail']['speakers'])
                    schedule.assert_not_called()
                config.write_text('{"Hero":{"ready":true},"NARRATOR":{"ready":true}}')
                for route in ('/api/merge_m4b', '/api/export_chapters'):
                    response = client.post(route, json={'require_ready': True})
                    self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(['m4b_export', 'chapter_export'], [call.args[1] for call in schedule.call_args_list])
                # Readiness remains opt-in; exports without the flag keep their contract.
                script.write_text('{}'); config.write_text('[]')
                response = client.post('/api/merge_m4b', json={'require_ready': False})
                self.assertEqual(200, response.status_code, response.text)
