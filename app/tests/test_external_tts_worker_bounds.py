"""Repeated actual engine timeouts must not create new live application pools."""
import concurrent.futures
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

import tts


class ExternalWorkerBoundsTests(unittest.TestCase):
    def test_repeated_single_and_batch_timeouts_share_a_bounded_worker_pool(self):
        for kind in ("single", "batch"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); release = threading.Event(); entered = threading.Event()
                provider = root / 'provider.wav'
                with wave.open(str(provider), 'wb') as wav:
                    wav.setparams((1,2,24000,0,'NONE','not compressed')); wav.writeframes(b'\0\0' * 2400)
                calls = []
                class Client:
                    def __init__(self, url):
                        self.url = url
                    def predict(self, **kwargs):
                        calls.append(kwargs['text']); entered.set(); release.wait(5)
                        return (str(provider),)
                fake = type('SDK', (), {'Client': Client})
                engine = tts.TTSEngine({'tts': {'mode': 'external', 'url': 'http://fixture', 'parallel_workers': 1}})
                engine._external_timeout = .05
                config = {'A': {'type': 'custom', 'voice': 'Ryan'}}
                original_threads = set(threading.enumerate()); pools = []
                original_executor = concurrent.futures.ThreadPoolExecutor
                def executor(*args, **kwargs):
                    result = original_executor(*args, **kwargs); pools.append(result); return result
                with patch.dict(sys.modules, {'gradio_client': fake}), \
                        patch.object(tts, 'create_external_client', side_effect=lambda url, timeout: Client(url), create=True), \
                        patch.object(concurrent.futures, 'ThreadPoolExecutor', side_effect=executor):
                    try:
                        for attempt in range(4):
                            if kind == 'batch':
                                output = root / 'temp_batch_0.wav'; output.write_bytes(b'previous audio')
                                result = engine._external_batch([{'index': 0, 'speaker': 'A', 'text': str(attempt)}],
                                                                config, str(root), 'custom')
                                self.assertEqual([], result['completed'])
                                self.assertEqual([0], [index for index, _ in result['failed']])
                            else:
                                output = root / 'single.wav'; output.write_bytes(b'previous audio')
                                self.assertFalse(engine.generate_custom_voice(str(attempt), '', 'A', config, str(output)))
                            self.assertEqual(b'previous audio', output.read_bytes())
                            self.assertTrue(entered.is_set())
                        workers = [t for t in threading.enumerate() if t not in original_threads]
                        self.assertLessEqual(len(workers), 1, [t.name for t in workers])
                        self.assertEqual(1, len(calls), 'Cancelled queued requests must not enter predict')
                    finally:
                        release.set()
                        for pool in pools:
                            pool.shutdown(wait=True, cancel_futures=True)
                        close = getattr(engine, 'close_external', None)
                        if close is not None:
                            close()
                        for thread in set(threading.enumerate()) - original_threads:
                            thread.join(2)
                self.assertEqual([], list(root.glob('*.pending.*')))
