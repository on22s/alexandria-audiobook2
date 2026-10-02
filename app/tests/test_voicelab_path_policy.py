"""Actual Voice Lab HTTP admission and mutable symlink path-policy cases."""
import json
import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import voicelab as v


class VoiceLabPathPolicyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.bad = self.root / 'uploads'
        self.bad.mkdir()
        self.good = self.root / 'external'
        self.good.mkdir()
        self.config = self.root / 'config.json'
        self.cfg = {'rocm_python': '', 'profiler_model': '',
                    'zips_dir': str(self.good), 'epub_dirs': []}
        self.config.write_text(json.dumps(self.cfg))
        self.stack.enter_context(patch.object(core, '_VOICELAB_FORBIDDEN_DIRS', [str(self.bad)]))
        self.stack.enter_context(patch.object(v, 'DATA_DIR', str(self.root)))
        self.stack.enter_context(patch.object(v, 'VOICELAB_CONFIG_PATH', str(self.config)))
        self.stack.enter_context(patch.object(v, '_load_voicelab_config',
                                             side_effect=lambda: json.loads(self.config.read_text())))
        app = FastAPI()
        app.include_router(v.router)
        self.client = self.stack.enter_context(TestClient(app))

    def test_save_rejects_generated_roots_and_symlinks_without_changing_config(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.bad, target_is_directory=True)
        before = self.config.read_bytes()
        for path in (str(self.bad), str(alias), 'uploads'):
            for field, value in (('zips_dir', path), ('epub_dirs', [os.path.relpath(self.bad) if path == 'uploads' else path])):
                with self.subTest(field=field, path=path):
                    response = self.client.post('/api/voicelab/config', json={field: value})
                    self.assertEqual(400, response.status_code, response.text)
                    self.assertEqual(before, self.config.read_bytes())
        response = self.client.post('/api/voicelab/config', json={
            'zips_dir': 'external', 'epub_dirs': ['  ' + str(self.good) + '  ', '']})
        self.assertEqual(200, response.status_code, response.text)
        saved = json.loads(self.config.read_text())
        self.assertEqual('external', saved['zips_dir'])
        self.assertEqual([str(self.good)], saved['epub_dirs'])

    def test_all_read_entry_points_reject_before_enumeration_or_probe(self):
        for endpoint in ('preflight', 'start', 'inspect', 'config'):
            for field in ('zips_dir', 'epub_dirs', 'rocm_python', 'profiler_model'):
                # Inspect only consumes the dataset root, rather than interpreter/EPUB paths.
                if endpoint == 'inspect' and field != 'zips_dir':
                    continue
                with self.subTest(endpoint=endpoint, field=field):
                    cfg = {**self.cfg, field: [str(self.bad)] if field == 'epub_dirs' else str(self.bad)}
                    self.config.write_text(json.dumps(cfg))
                    with patch.object(v.os, 'listdir', side_effect=AssertionError('enumerated rejected path')), \
                         patch.object(v, '_probe_voicelab_interpreter', side_effect=AssertionError('probed')), \
                         patch.object(v, '_run_profiler_preflight', side_effect=AssertionError('profiler launched')):
                        if endpoint in ('preflight', 'start'):
                            response = self.client.post('/api/voicelab/' + endpoint,
                                                        json={'stages': ['name']})
                        else:
                            response = self.client.get('/api/voicelab/' + endpoint)
                    self.assertEqual(400, response.status_code, response.text)
        self.config.write_text(json.dumps(self.cfg))
        with patch.object(v.os, 'listdir', side_effect=AssertionError('enumerated rejected override')):
            for field in ('zips_dir', 'profiler_model'):
                response = self.client.post('/api/voicelab/preflight',
                                            json={'stages': ['name'], field: str(self.bad)})
                self.assertEqual(400, response.status_code, response.text)

    def test_profile_default_model_is_checked_before_probe(self):
        paths = {**v.get_profiler_paths(v.ROOT_DIR, v.DATA_DIR), 'model': str(self.bad / 'model')}
        with patch.object(v, 'get_profiler_paths', return_value=paths), \
             patch.object(v, '_probe_voicelab_interpreter', side_effect=AssertionError('probed')):
            response = self.client.post('/api/voicelab/preflight', json={'stages': ['profile']})
        self.assertEqual(400, response.status_code, response.text)
        self.assertIn('profiler_model', response.json()['detail'])

    def test_profile_runtime_rechecks_epub_symlink_before_launch(self):
        alias = self.root / 'epubs'
        alias.symlink_to(self.good, target_is_directory=True)
        interpreter = self.good / 'python'
        interpreter.write_text('fixture')
        interpreter.chmod(0o700)
        model = self.good / 'model'
        model.write_text('fixture')
        self.assertIsNone(v._revalidate_voicelab_runtime(
            str(self.good), str(interpreter), str(model), ['profile'], [str(alias)]))
        alias.unlink()
        alias.symlink_to(self.bad, target_is_directory=True)
        error = v._revalidate_voicelab_runtime(
            str(self.good), str(interpreter), str(model), ['profile'], [str(alias)])
        self.assertIsNotNone(error)
        self.assertEqual(400, error.status_code)
        self.assertIn('epub_dirs', error.detail)
        # Naming does not consume the EPUB paths.
        self.assertIsNone(v._revalidate_voicelab_runtime(
            str(self.good), '', '', ['name'], [str(alias)]))
