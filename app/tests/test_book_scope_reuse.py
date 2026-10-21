"""Explicit book reuse never grants copied or expired contexts authority."""
import asyncio
from contextlib import contextmanager
from contextvars import copy_context
import tempfile
import threading
import unittest
from unittest.mock import patch

from book_state_transaction import (ensure_book_state, reuse_book_state,
    is_book_state_admitted)
from audio_generation_owner import (AudioGenerationOwner, AudioGenerationCancelled,
    ensure_audio_generation_owner, ensure_audio_generation_book,
    ensure_audio_generation_book_work, reuse_audio_generation_book)


class BookScopeReuseTests(unittest.TestCase):
    def test_only_explicit_reuse_is_reentrant_and_nested_roots_remain_admitted(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with ensure_book_state(first):
                with reuse_book_state(first, timeout=0):
                    self.assertTrue(is_book_state_admitted(first))
                with self.assertRaises(TimeoutError), ensure_book_state(first, timeout=0): pass
                with self.assertRaises(TimeoutError), ensure_audio_generation_book(first, timeout=0): pass
                with ensure_book_state(second):
                    self.assertTrue(is_book_state_admitted(first))
                    self.assertTrue(is_book_state_admitted(second))
                self.assertFalse(is_book_state_admitted(second))
            self.assertFalse(is_book_state_admitted(first))

    def test_copied_thread_cannot_reuse_parent_and_expired_context_has_no_admission(self):
        with tempfile.TemporaryDirectory() as root:
            results = []
            def worker():
                results.append(is_book_state_admitted(root))
                try:
                    with reuse_book_state(root, timeout=0): results.append('unexpected admission')
                except TimeoutError: results.append('contended')
            with ensure_book_state(root):
                context = copy_context()
                thread = threading.Thread(target=context.run, args=(worker,))
                thread.start(); thread.join(2)
                self.assertFalse(thread.is_alive())
                expired = copy_context()
            self.assertEqual(results, [False, 'contended'])
            self.assertFalse(expired.run(is_book_state_admitted, root))
            with ensure_book_state(root):
                with self.assertRaises(TimeoutError): expired.run(lambda: self.acquire(root))

    @staticmethod
    def acquire(root):
        with reuse_book_state(root, timeout=0): pass

    def test_async_child_and_changed_pid_cannot_inherit_kernel_admission(self):
        with tempfile.TemporaryDirectory() as root:
            async def child():
                self.assertFalse(is_book_state_admitted(root))
                with self.assertRaises(TimeoutError), reuse_book_state(root, timeout=0): pass
            async def parent():
                with ensure_book_state(root):
                    self.assertTrue(is_book_state_admitted(root))
                    await asyncio.create_task(child())
                    with patch('book_state_transaction.os.getpid', return_value=-1):
                        self.assertFalse(is_book_state_admitted(root))
            asyncio.run(parent())

    def test_long_work_releases_owner_mutex_and_brief_publication_reacquires_it(self):
        with tempfile.TemporaryDirectory() as root:
            mutex = threading.Lock()
            @contextmanager
            def admit(*, allow_cancelled=False):
                self.assertTrue(mutex.acquire(timeout=.5))
                try: yield
                finally: mutex.release()
            with ensure_audio_generation_owner(AudioGenerationOwner('a'*32, admit)):
                with ensure_audio_generation_book_work(root):
                    self.assertTrue(is_book_state_admitted(root))
                    self.assertTrue(mutex.acquire(blocking=False)); mutex.release()
                    with reuse_audio_generation_book(root):
                        self.assertFalse(mutex.acquire(blocking=False))
                    self.assertTrue(mutex.acquire(blocking=False)); mutex.release()
            self.assertFalse(is_book_state_admitted(root))

    def test_cancelled_work_does_not_leak_owner_or_book_during_contention(self):
        with tempfile.TemporaryDirectory() as root:
            entered = threading.Event(); cancel = threading.Event(); errors = []; mutex = threading.Lock()
            @contextmanager
            def admit(*, allow_cancelled=False):
                with mutex:
                    entered.set()
                    if cancel.is_set(): raise AudioGenerationCancelled()
                    yield
            def worker():
                try:
                    with ensure_audio_generation_owner(AudioGenerationOwner('a'*32, admit)):
                        with ensure_audio_generation_book_work(root, timeout=1):
                            self.fail('Cancelled work was admitted')
                except BaseException as error: errors.append(error)
            with ensure_book_state(root):
                thread = threading.Thread(target=worker); thread.start()
                self.assertTrue(entered.wait(1)); cancel.set(); thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertTrue(mutex.acquire(timeout=.5)); mutex.release()
            self.assertEqual(len(errors), 1); self.assertIsInstance(errors[0], AudioGenerationCancelled)
            with ensure_book_state(root, timeout=0): pass
