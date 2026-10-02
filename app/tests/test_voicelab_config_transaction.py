"""Concurrent Voice Lab settings requests must retain both sets of edits."""
from tests.test_support import assert_file_lock_released
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
from routers import voicelab
from utils import atomic_json_write, file_lock


class VoicelabConfigTransactionTests(unittest.TestCase):
    def test_actual_concurrent_settings_posts_merge_latest_config_under_one_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp,'voicelab_config.json')
            initial = {'rocm_python':'','profiler_model':'','epub_dirs':['old books'],'zips_dir':'old datasets'}
            path.write_text(json.dumps(initial))
            before = path.read_bytes()
            paused, release, second_reached = threading.Event(), threading.Event(), threading.Event()
            observed = {'loads':0,'locks':0}
            counter_lock = threading.Lock()
            responses, errors = {}, []
            load = voicelab._load_voicelab_config

            def observed_load():
                result = load()
                with counter_lock:
                    observed['loads'] += 1
                    if observed['loads'] == 2:
                        second_reached.set()
                return result

            @contextmanager
            def observed_lock(target):
                with counter_lock:
                    observed['locks'] += 1
                    if observed['locks'] == 2:
                        second_reached.set()
                with file_lock(target):
                    yield

            def publish(data,target):
                if data['zips_dir'] == 'new datasets' and data['epub_dirs'] == ['old books']:
                    paused.set()
                    if not release.wait(4):
                        raise AssertionError('Test did not release first settings publisher')
                return atomic_json_write(data,target)

            app = FastAPI()
            app.include_router(voicelab.router)
            def save(label,payload):
                try:
                    # Separate portals let us reproduce concurrent server workers too.
                    with TestClient(app) as client:
                        responses[label] = client.post('/api/voicelab/config',json=payload)
                except BaseException as exc:
                    errors.append(exc)

            first = threading.Thread(target=save,args=('first',{'zips_dir':' new datasets '}))
            second = threading.Thread(target=save,args=('second',{'epub_dirs':[' new books ','']}))
            with patch.object(core,'VOICELAB_CONFIG_PATH',str(path)), \
                 patch.object(voicelab,'VOICELAB_CONFIG_PATH',str(path)), \
                 patch.object(voicelab,'_load_voicelab_config',side_effect=observed_load), \
                 patch.object(voicelab,'atomic_json_write',side_effect=publish), \
                 patch.object(voicelab,'file_lock',observed_lock,create=True):
                first.start()
                try:
                    self.assertTrue(paused.wait(2))
                    second.start()
                    self.assertTrue(second_reached.wait(2))
                    second.join(0.1)
                    waited = second.is_alive()
                    preserved = path.read_bytes() == before
                finally:
                    release.set()
                    first.join(5)
                    if second.ident is not None:
                        second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual([],errors)
            for response in responses.values():
                self.assertEqual(200,response.status_code,response.text)
            self.assertEqual({'first','second'},set(responses))
            saved = json.loads(path.read_text())
            self.assertEqual('new datasets',saved['zips_dir'])
            self.assertEqual(['new books'],saved['epub_dirs'])
            self.assertEqual('',saved['rocm_python'])
            self.assertEqual('',saved['profiler_model'])
            self.assertTrue(waited,'Second publisher bypassed first request transaction')
            self.assertTrue(preserved,'Second request wrote before the first transaction completed')
            assert_file_lock_released(str(path))
            self.assertEqual('new datasets',responses['second'].json()['config']['zips_dir'])

    def test_write_failure_preserves_config_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp,'voicelab_config.json')
            before = b'{"rocm_python":"","profiler_model":"","epub_dirs":["old books"],"zips_dir":"old datasets"}'
            path.write_bytes(before)
            app = FastAPI()
            app.include_router(voicelab.router)
            with patch.object(core,'VOICELAB_CONFIG_PATH',str(path)), \
                 patch.object(voicelab,'VOICELAB_CONFIG_PATH',str(path)), \
                 patch.object(voicelab,'atomic_json_write',side_effect=PermissionError('fixture denied')), \
                 TestClient(app,raise_server_exceptions=False) as client:
                response = client.post('/api/voicelab/config',json={'zips_dir':'new datasets'})
            self.assertEqual(500,response.status_code)
            self.assertEqual(before,path.read_bytes())
            assert_file_lock_released(str(path))
