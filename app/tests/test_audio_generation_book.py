"""Book contention must not retain the owner/task mutex or repeat a publication."""
from contextlib import contextmanager
import tempfile
import threading
import unittest

from audio_generation_owner import (AudioGenerationOwner, AudioGenerationCancelled,
    ensure_audio_generation_owner, ensure_audio_generation_book)
from book_state_transaction import ensure_book_state


class AudioGenerationBookTests(unittest.TestCase):
    def test_book_holder_can_acquire_owner_mutex_while_publication_retries(self):
        with tempfile.TemporaryDirectory() as root:
            mutex=threading.Lock(); entered=threading.Event(); finished=threading.Event(); errors=[]
            @contextmanager
            def admit(*,allow_cancelled=False):
                with mutex:
                    entered.set()
                    yield
            def publish():
                try:
                    with ensure_audio_generation_owner(AudioGenerationOwner('a'*32,admit)):
                        with ensure_audio_generation_book(root,timeout=1):
                            finished.set()
                except BaseException as error:
                    errors.append(error)
            with ensure_book_state(root):
                worker=threading.Thread(target=publish);worker.start()
                self.assertTrue(entered.wait(1))
                acquired=mutex.acquire(timeout=.5)
                if acquired: mutex.release()
                self.assertTrue(acquired,'Owner mutex was retained while waiting for the book')
                self.assertFalse(finished.is_set())
            worker.join(2)
            self.assertFalse(worker.is_alive());self.assertEqual([],errors)
            self.assertTrue(finished.is_set())

    def test_cancellation_is_rechecked_during_contention(self):
        with tempfile.TemporaryDirectory() as root:
            entered=threading.Event();cancel=threading.Event();errors=[]
            @contextmanager
            def admit(*,allow_cancelled=False):
                if cancel.is_set():raise AudioGenerationCancelled()
                entered.set()
                yield
            def publish():
                try:
                    with ensure_audio_generation_owner(AudioGenerationOwner('a'*32,admit)):
                        with ensure_audio_generation_book(root,timeout=1):
                            errors.append(AssertionError('Cancelled publication admitted'))
                except BaseException as error: errors.append(error)
            with ensure_book_state(root):
                worker=threading.Thread(target=publish);worker.start()
                self.assertTrue(entered.wait(1));cancel.set();worker.join(.5)
                self.assertFalse(worker.is_alive())
            self.assertEqual(1,len(errors));self.assertIsInstance(errors[0],AudioGenerationCancelled)

    def test_body_timeout_is_not_retried_or_replaced_by_admission_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            calls=[]
            with self.assertRaisesRegex(TimeoutError,'publication body'):
                with ensure_audio_generation_book(root,timeout=.1):
                    calls.append('publish')
                    raise TimeoutError('publication body')
            self.assertEqual(['publish'],calls)

    def test_persistent_contention_is_bounded(self):
        with tempfile.TemporaryDirectory() as root,ensure_book_state(root):
            with self.assertRaisesRegex(TimeoutError,'admit audio publication'):
                with ensure_audio_generation_book(root,timeout=.02):
                    self.fail('Contended book was admitted')


if __name__ == '__main__':
    unittest.main()
