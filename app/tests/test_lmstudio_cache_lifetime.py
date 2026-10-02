"""Expired bookkeeping may go; active same-key admission may not."""
import gc
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import lmstudio_settings as settings


class StatusCacheLifetimeTests(unittest.TestCase):
    def setUp(self):
        settings.invalidate_remote_status_cache()
        self.addCleanup(settings.invalidate_remote_status_cache)

    def test_old_keys_expire_but_live_entries_keep_existing_ttl(self):
        with patch.object(settings.time,'time',return_value=100) as clock, patch.object(settings,'get_remote_lmstudio_status',return_value={'loaded':True}) as live:
            for n in range(100):
                settings.get_remote_lmstudio_status_cached('fixture',str(n))
            self.assertEqual(100,len(settings._remote_status_cache))
            clock.return_value=109
            settings.get_remote_lmstudio_status_cached('fixture','0')
            self.assertEqual(100,live.call_count)
            clock.return_value=111
            settings.get_remote_lmstudio_status_cached('fixture','fresh')
            self.assertEqual(1,len(settings._remote_status_cache))
            gc.collect()
            self.assertEqual(0,len(settings._remote_status_key_locks))

    def test_active_and_waiting_same_key_lock_survives_expiration_and_other_key_churn(self):
        entered=threading.Event();release=threading.Event();polling=threading.Event()
        calls=[]
        def provider(host,model,**kwargs):
            calls.append(model)
            if model=='active':
                entered.set()
                if not release.wait(timeout=3):
                    raise AssertionError('fixture active fetch not released')
            return {'loaded':True,'model':model}
        def second():
            polling.set()
            return settings.get_remote_lmstudio_status_cached('fixture','active')
        with patch.object(settings,'get_remote_lmstudio_status',side_effect=provider), patch.object(settings.time,'time',return_value=100) as clock, ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(settings.get_remote_lmstudio_status_cached,'fixture','active')
            self.assertTrue(entered.wait(timeout=2))
            waiter=pool.submit(second)
            try:
                self.assertTrue(polling.wait(timeout=2))
                clock.return_value=120
                for n in range(40):
                    settings.get_remote_lmstudio_status_cached('fixture',str(n))
                gc.collect()
                self.assertEqual(1,len(settings._remote_status_key_locks))
                self.assertEqual(1,calls.count('active'))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=2),waiter.result(timeout=2))
            self.assertEqual(1,calls.count('active'))

    def test_lock_reference_stays_shared_until_last_holder_releases_it(self):
        key=('fixture','held')
        first=settings.ensure_remote_status_key_lock(key)
        second=settings.ensure_remote_status_key_lock(key)
        self.assertIs(first,second)
        del first
        gc.collect()
        self.assertIs(second,settings.ensure_remote_status_key_lock(key))
        del second
        gc.collect()
        self.assertEqual(0,len(settings._remote_status_key_locks))

    def test_failed_fetch_releases_unused_lock_without_caching_success(self):
        with patch.object(settings,'get_remote_lmstudio_status',side_effect=[OSError('fixture failure'),{'loaded':True}]) as live:
            with self.assertRaisesRegex(OSError,'fixture failure'):
                settings.get_remote_lmstudio_status_cached('fixture','model')
            gc.collect()
            self.assertEqual({},settings._remote_status_cache)
            self.assertEqual(0,len(settings._remote_status_key_locks))
            self.assertEqual({'loaded':True},settings.get_remote_lmstudio_status_cached('fixture','model'))
            self.assertEqual(2,live.call_count)

    def test_two_cache_key_shapes_keep_independent_status_and_invalidation(self):
        with patch.object(settings,'get_remote_lmstudio_status',return_value={'kind':'native'}) as native, patch.object(settings,'get_current_status',return_value={'kind':'runtime'}) as runtime:
            for _ in range(2):
                self.assertEqual({'kind':'native'},settings.get_remote_lmstudio_status_cached('fixture','model'))
                self.assertEqual({'kind':'runtime'},settings.get_remote_runtime_status_cached('http://fixture/v1','fixture','model','fixture-secret'))
            native.assert_called_once();runtime.assert_called_once()
            self.assertNotIn('fixture-secret',repr(settings._remote_status_cache))
            settings.invalidate_remote_status_cache('fixture')
            self.assertEqual({},settings._remote_status_cache)
