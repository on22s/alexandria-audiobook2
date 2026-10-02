"""UI-only status caching; CPU providers, actual CLI parsing and HTTP routing."""
import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import inspect
import json
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import system
import lmstudio_settings as settings

if os.environ.get('SETTINGS_SOURCE'):
    spec = importlib.util.spec_from_file_location('settings_before', os.environ['SETTINGS_SOURCE'])
    settings = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(settings)

BASE = 'http://127.0.0.1:8111/v1'


def poll(base=BASE, model='model', key=None):
    kwargs = {'use_cache': True, 'api_key': key}
    if 'cache_local' in inspect.signature(settings.get_current_status).parameters:
        kwargs['cache_local'] = True
    return settings.get_current_status('local', base, model, **kwargs)


class LocalPollCacheTests(unittest.TestCase):
    def setUp(self):
        settings.invalidate_remote_status_cache()
        self.addCleanup(settings.invalidate_remote_status_cache)

    def test_actual_status_http_coalesces_props_cli_parse_and_live_vram(self):
        model = next(iter(settings._VERIFIED_LOCAL_PROFILES))
        payload = [{'identifier': model, 'contextLength': 8192, 'parallel': 1}]
        config = {'llm_mode': 'local', 'llm_local': {'model_name': model, 'base_url': BASE}}
        app = FastAPI();app.include_router(system.router)

        def dispatch(*args, **kwargs):
            if 'cache_local' not in inspect.signature(settings.get_current_status).parameters:
                kwargs.pop('cache_local', None)
            return settings.get_current_status(*args, **kwargs)

        with patch.object(system, 'load_app_config', return_value=config), \
             patch.object(system, 'get_current_status', side_effect=dispatch), \
             patch.object(settings, 'get_llama_cpp_status', return_value=None) as props, \
             patch.object(settings, 'get_lmstudio_endpoint_status', return_value={'available': True}), \
             patch.object(settings, 'get_lmstudio_management_binding', return_value=(True, 8111, 'fixture')), \
             patch.object(settings, 'find_lms_binary', return_value='fixture-lms'), \
             patch.object(settings.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload))) as cli, \
             patch.object(settings, 'get_local_vram_bytes', return_value=(64 * 1024**3, 9 * 1024**3)) as vram, \
             patch.object(settings, '_parse_lms_ps_output', wraps=settings._parse_lms_ps_output) as parse, \
             TestClient(app) as client:
            first = client.get('/api/lmstudio/status');second = client.get('/api/lmstudio/status')
        self.assertEqual(200, first.status_code)
        self.assertEqual(first.json(), second.json())
        self.assertTrue(first.json()['loaded'])
        self.assertEqual(model, first.json()['model'])
        print(f'Measured two local HTTP polls: props={props.call_count}, CLI={cli.call_count}, parse={parse.call_count}, VRAM={vram.call_count}')
        self.assertEqual((1, 1, 1, 1), (props.call_count, cli.call_count, parse.call_count, vram.call_count))

    def test_worker_reads_are_live_even_with_historical_use_cache_flag(self):
        with patch.object(settings, 'get_llama_cpp_status', return_value={'runtime': 'llama.cpp', 'nested': {'rows': [1]}}) as live:
            first = poll();first['nested']['rows'].append(99)
            self.assertEqual([1], poll()['nested']['rows'])
            self.assertEqual(1, live.call_count)
            settings.get_current_status('local', BASE, 'model', use_cache=True)
            settings.get_current_status('local', BASE, 'model')
            self.assertEqual(3, live.call_count)

    def test_local_ttl_uses_monotonic_time_despite_wall_clock_reversal(self):
        with patch.object(settings.time, 'monotonic', return_value=100) as mono, \
             patch.object(settings.time, 'time', return_value=1000) as wall, \
             patch.object(settings, 'get_llama_cpp_status', return_value={'loaded': True}) as live:
            poll();mono.return_value=109.9;wall.return_value=-99999;poll()
            self.assertEqual(1, live.call_count)
            mono.return_value=110;poll()
            self.assertEqual(2, live.call_count)

    def test_concurrent_same_key_shares_fetch_and_returns_independent_snapshots(self):
        entered, release = threading.Event(), threading.Event()
        def provider(*args):
            entered.set()
            if not release.wait(3):raise AssertionError('fixture not released')
            return {'runtime': 'llama.cpp', 'nested': {'rows': [1]}}
        with patch.object(settings, 'get_llama_cpp_status', side_effect=provider) as live, ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(poll) for _ in range(8)]
            try:
                self.assertTrue(entered.wait(2))
            finally:
                release.set()
            rows = [future.result(timeout=3) for future in futures]
            self.assertEqual(1, live.call_count)
        rows[0]['nested']['rows'].append(2)
        self.assertTrue(all(row['nested']['rows'] == [1] for row in rows[1:]))

    def test_endpoint_model_credentials_and_remote_scope_have_distinct_cache_keys(self):
        with patch.object(settings, 'get_llama_cpp_status', return_value={'loaded': True}) as live:
            for args in ((BASE, 'model', 'first-secret'), (BASE, 'model', 'second-secret'),
                         (BASE, 'other', 'first-secret'), ('http://127.0.0.1:8112/v1', 'model', 'first-secret')):
                poll(*args);poll(*args)
            settings.get_current_status('remote', BASE, 'model', use_cache=True, api_key='first-secret')
            self.assertEqual(5, live.call_count)
            self.assertNotIn('first-secret', repr(settings._remote_status_cache))
            self.assertNotIn('second-secret', repr(settings._remote_status_cache))

    def test_invalidated_inflight_result_is_returned_but_cannot_repopulate_cache(self):
        entered, release = threading.Event(), threading.Event()
        def provider(*args):
            entered.set()
            if not release.wait(3):raise AssertionError('fixture not released')
            return {'context_length': 8192}
        with patch.object(settings, 'get_llama_cpp_status', side_effect=provider), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(poll)
            try:
                self.assertTrue(entered.wait(2))
                settings.invalidate_local_status_cache()
            finally:
                release.set()
            self.assertEqual(8192, future.result(timeout=3)['context_length'])
        self.assertEqual({}, settings._remote_status_cache)
        with patch.object(settings, 'get_llama_cpp_status', return_value={'context_length': 32768}) as live:
            self.assertEqual(32768, poll()['context_length'])
            live.assert_called_once()

    def test_local_reload_success_failure_and_unexpected_error_invalidate_ui_only(self):
        for outcome in (SimpleNamespace(returncode=0, stdout='', stderr=''),
                        SimpleNamespace(returncode=1, stdout='', stderr='load refused'),
                        OSError('load error'), ValueError('unexpected decode error')):
            with self.subTest(outcome=repr(outcome)):
                settings.invalidate_remote_status_cache()
                settings.save_remote_status_cache(('remote-host', 'model'), {'remote': True})
                with patch.object(settings, 'get_llama_cpp_status', return_value={'loaded': True}):poll()
                with patch.object(settings, 'find_lms_binary', return_value='fixture-lms'), \
                     patch.object(settings.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0), outcome]):
                    if isinstance(outcome, ValueError):
                        with self.assertRaises(ValueError):settings.apply_lmstudio_settings('model', ideal=False)
                    else:
                        success, _ = settings.apply_lmstudio_settings('model', ideal=False)
                        self.assertEqual(not isinstance(outcome, OSError) and outcome.returncode == 0, success)
                self.assertEqual({('remote-host', 'model')}, set(settings._remote_status_cache))
