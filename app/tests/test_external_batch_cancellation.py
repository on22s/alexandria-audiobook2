"""Valid-owner cancellation during real owned-executor batch collection."""
from contextlib import contextmanager
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from audio_generation_owner import (AudioGenerationCancelled, AudioGenerationOwner,
                                    ensure_audio_generation_owner,
                                    is_audio_generation_cancelled)
import tts


class ExternalBatchCancellationTests(unittest.TestCase):
    def owner(self, cancelled):
        admission = threading.Lock()

        @contextmanager
        def admit(*, allow_cancelled=False):
            with admission:
                if cancelled.is_set() and not allow_cancelled:
                    raise AudioGenerationCancelled()
                yield

        return AudioGenerationOwner('a'*32, admit)

    def engine(self):
        return tts.TTSEngine({'tts': {'mode': 'external', 'url': 'http://fixture',
                                    'parallel_workers': 1, 'external_timeout_seconds': 10}})

    def test_already_cancelled_owner_submits_no_provider_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            cancelled = threading.Event()
            cancelled.set()
            engine = self.engine()
            try:
                with ensure_audio_generation_owner(self.owner(cancelled)), \
                        patch.object(engine, '_external_generate_custom', return_value=False) as provider:
                    self.assertTrue(is_audio_generation_cancelled())
                    result = engine._external_batch([{'index': 0, 'speaker': 'A', 'text': 'synthetic'}],
                        {'A': {'voice': 'Ryan'}}, temporary, 'custom')
                self.assertEqual({'completed': [], 'failed': []}, result)
                provider.assert_not_called()
                self.assertIsNone(getattr(engine, '_external_executor', None))
            finally:
                engine.close_external()

    def run_cancelled_collection(self, kind, *, retain_completed=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = self.engine()
            executor = engine._ensure_external_executor()
            self.assertEqual('warm', executor.submit(lambda: 'warm').result(2))
            cancelled = threading.Event()
            owner = self.owner(cancelled)
            release = threading.Event()
            entered = threading.Event()
            submitted = threading.Event()
            published = threading.Event()
            finished = threading.Event()
            requests, calls, outcomes, errors = [], [], [], []
            chunks = [{'index': index, 'speaker': 'A', 'text': str(index)} for index in range(3)]
            for index in range(3):
                (root/f'temp_batch_{index}.wav').write_bytes(b'prior output')
            original_submit = engine._submit_external
            original_publish = tts.publish_audio_output

            def record_submit(function, request_cancelled, *arguments):
                future = original_submit(function, request_cancelled, *arguments)
                requests.append((future, request_cancelled))
                if len(requests) == 3:
                    submitted.set()
                return future

            def render(*arguments, **keywords):
                index = int(arguments[0])
                path = Path(arguments[-1])
                calls.append(index)
                with wave.open(str(path), 'wb') as stream:
                    stream.setparams((1, 2, 24000, 8, 'NONE', 'not compressed'))
                    stream.writeframes(b'\x01\x00'*8)
                if not retain_completed or index != 0:
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('Synthetic provider was not released')
                    path.write_bytes(b'late synthetic provider result')
                return True

            def publish(staging_path, output_path, *arguments, **keywords):
                result = original_publish(staging_path, output_path, *arguments, **keywords)
                if result and Path(output_path).name == 'temp_batch_0.wav':
                    published.set()
                return result

            def collect():
                try:
                    with ensure_audio_generation_owner(owner):
                        outcomes.append(engine._external_batch(chunks, {'A': {'voice': 'Ryan'}},
                                                               str(root), kind))
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()

            collector = threading.Thread(target=collect)
            try:
                with patch.object(engine, '_external_generate_'+kind, side_effect=render), \
                        patch.object(engine, '_submit_external', side_effect=record_submit), \
                        patch.object(tts, 'publish_audio_output', side_effect=publish):
                    collector.start()
                    try:
                        self.assertTrue(entered.wait(2), 'Provider never entered')
                        self.assertTrue(submitted.wait(2), 'Batch did not submit its queued work')
                        if retain_completed:
                            self.assertTrue(published.wait(2), 'First result never published')
                        with ensure_audio_generation_owner(owner):
                            self.assertFalse(is_audio_generation_cancelled())
                        cancelled.set()
                        with ensure_audio_generation_owner(owner):
                            self.assertTrue(is_audio_generation_cancelled())
                        self.assertTrue(finished.wait(1.5), 'Collector ignored owner cancellation')
                        self.assertEqual([], errors)
                        self.assertEqual([{'completed': [0] if retain_completed else [], 'failed': []}], outcomes)
                        self.assertTrue(all(event.is_set() for _, event in requests))
                        queued = requests[2:] if retain_completed else requests[1:]
                        self.assertTrue(all(future.cancelled() for future, _ in queued))
                    finally:
                        release.set()
                        collector.join(3)
                        self.assertFalse(collector.is_alive(), 'Collector did not retire')
                        self.assertEqual('settled', executor.submit(lambda: 'settled').result(3))
                self.assertEqual([0, 1] if retain_completed else [0], calls)
                self.assertEqual([], list(root.glob('*.pending.*')))
                for index in range(1 if retain_completed else 0, 3):
                    self.assertEqual(b'prior output', (root/f'temp_batch_{index}.wav').read_bytes())
                if retain_completed:
                    self.assertNotEqual(b'prior output', (root/'temp_batch_0.wav').read_bytes())
                self.assertIs(executor, engine._external_executor)
                self.assertEqual('reusable', engine._submit_external(lambda: 'reusable',
                    threading.Event()).result(2))
            finally:
                release.set()
                executor.shutdown(wait=True, cancel_futures=True, timeout=3)
                engine.close_external()

    def test_cancelled_collection_signals_running_and_cancels_queued_custom_and_clone_work(self):
        for kind in ('custom', 'clone'):
            with self.subTest(kind=kind):
                self.run_cancelled_collection(kind)

    def test_cancelled_collection_preserves_already_published_partial_results(self):
        self.run_cancelled_collection('custom', retain_completed=True)

    def test_slice_timeout_does_not_discard_a_concurrently_completed_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = self.engine()
            future = Future()
            waits = []

            def render(*arguments, **keywords):
                with wave.open(str(arguments[-1]), 'wb') as stream:
                    stream.setparams((1, 2, 24000, 8, 'NONE', 'not compressed'))
                    stream.writeframes(b'\x01\x00'*8)
                return True

            def submit(callback, chunk, *arguments):
                future.set_result(callback(chunk, *arguments))
                return future

            def result(timeout=None):
                waits.append(timeout)
                if len(waits) == 1:
                    raise FutureTimeout()
                return Future.result(future, timeout)

            try:
                with patch.object(engine, '_external_generate_custom', side_effect=render), \
                        patch.object(engine, '_ensure_external_executor',
                                     return_value=SimpleNamespace(submit=submit)), \
                        patch.object(future, 'result', side_effect=result):
                    outcome = engine._external_batch(
                        [{'index': 0, 'speaker': 'A', 'text': 'synthetic'}],
                        {'A': {'voice': 'Ryan'}}, temporary, 'custom')
                self.assertEqual({'completed': [0], 'failed': []}, outcome)
                self.assertEqual(2, len(waits))
                self.assertTrue(all(0 <= timeout <= .1 for timeout in waits))
                self.assertEqual([], list(Path(temporary).glob('*.pending.*')))
            finally:
                engine.close_external()

    def test_provider_timeout_is_not_retried_as_a_wait_slice_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = self.engine()
            future = Future()
            future.set_exception(FutureTimeout('synthetic provider timeout'))
            try:
                with patch.object(engine, '_submit_external', return_value=future), \
                        patch.object(future, 'result', wraps=future.result) as result:
                    outcome = engine._external_batch(
                        [{'index': 0, 'speaker': 'A', 'text': 'synthetic'}],
                        {'A': {'voice': 'Ryan'}}, temporary, 'custom')
                self.assertEqual([], outcome['completed'])
                self.assertEqual([0], [index for index, _ in outcome['failed']])
                self.assertIn('timed out', outcome['failed'][0][1])
                result.assert_called_once()
            finally:
                engine.close_external()

    def test_slice_timeout_preserves_a_concurrent_provider_failure_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = self.engine()
            future = Future()
            future.set_exception(RuntimeError('synthetic provider failure'))
            try:
                with patch.object(engine, '_submit_external', return_value=future), \
                        patch.object(future, 'result', side_effect=FutureTimeout()):
                    outcome = engine._external_batch(
                        [{'index': 0, 'speaker': 'A', 'text': 'synthetic'}],
                        {'A': {'voice': 'Ryan'}}, temporary, 'custom')
                self.assertEqual({'completed': [], 'failed': [(0, 'synthetic provider failure')]},
                                 outcome)
            finally:
                engine.close_external()
