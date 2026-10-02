"""Cached observations are snapshots, not shared caller-owned dictionaries."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import lmstudio_settings as settings


class StatusSnapshotTests(unittest.TestCase):
    def setUp(self):
        settings.invalidate_remote_status_cache()
        self.addCleanup(settings.invalidate_remote_status_cache)

    def test_both_cache_apis_isolate_nested_caller_and_provider_changes(self):
        for runtime in (False,True):
            with self.subTest(runtime=runtime):
                settings.invalidate_remote_status_cache()
                source={'loaded':True,'nested':{'samples':[1,2]}}
                expected=copy.deepcopy(source)
                provider='get_current_status' if runtime else 'get_remote_lmstudio_status'
                call=(lambda:settings.get_remote_runtime_status_cached('http://fixture/v1','fixture','model','key')) if runtime else (lambda:settings.get_remote_lmstudio_status_cached('fixture','model'))
                with patch.object(settings,provider,return_value=source) as live:
                    first=call();first['loaded']=False;first['nested']['samples'].append(3)
                    second=call()
                    self.assertEqual(expected,second)
                    second['nested']['samples'].append(4)
                    source['nested']['samples'].append(5)
                    self.assertEqual(expected,call())
                    live.assert_called_once()

    def test_existing_ttl_refresh_and_explicit_invalidation_still_fetch(self):
        for runtime in (False,True):
            with self.subTest(runtime=runtime):
                settings.invalidate_remote_status_cache()
                provider='get_current_status' if runtime else 'get_remote_lmstudio_status'
                call=(lambda:settings.get_remote_runtime_status_cached('http://fixture/v1','fixture','model')) if runtime else (lambda:settings.get_remote_lmstudio_status_cached('fixture','model'))
                with patch.object(settings,provider,side_effect=[{'serial':1},{'serial':2},{'serial':3}]) as live, patch.object(settings.time,'time',return_value=10) as clock:
                    self.assertEqual({'serial':1},call())
                    clock.return_value=19
                    self.assertEqual({'serial':1},call())
                    self.assertEqual(1,live.call_count)
                    clock.return_value=21
                    self.assertEqual({'serial':2},call())
                    settings.invalidate_remote_status_cache('fixture')
                    self.assertEqual({'serial':3},call())
                    self.assertEqual(3,live.call_count)

    def test_same_key_threaded_polls_share_one_fetch_but_not_result_objects(self):
        barrier=threading.Barrier(8)
        def poll():
            barrier.wait(timeout=3)
            return settings.get_remote_lmstudio_status_cached('fixture','model')
        with patch.object(settings,'get_remote_lmstudio_status',return_value={'loaded':True,'nested':{'list':[]}}) as live:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results=list(pool.map(lambda _:poll(),range(8)))
            live.assert_called_once()
        results[0]['nested']['list'].append('caller mutation')
        self.assertTrue(all(not row['nested']['list'] for row in results[1:]))

    def test_local_status_parses_once_without_changing_dynamic_profile_decision(self):
        for payload,loaded in [([{'identifier':'model','contextLength':16384,'parallel':2}],True),([],False),({},False)]:
            with self.subTest(payload=payload), patch.object(settings,'find_lms_binary',return_value='fixture-lms'), \
                    patch.object(settings.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(payload))) as cli, \
                    patch.object(settings,'get_safe_local_settings',return_value={'context_length':16384,'parallel':2,'reason':'known fixture'}) as safe, \
                    patch.object(settings,'_parse_lms_ps_output',wraps=settings._parse_lms_ps_output) as parse:
                result=settings.get_lmstudio_status('model')
                self.assertEqual(loaded,result['loaded'])
                self.assertEqual(loaded,result['optimized'])
                self.assertEqual(16384,result['ideal_context_length'])
                safe.assert_called_once_with('model',loaded)
                cli.assert_called_once()
                self.assertEqual(1,parse.call_count)
