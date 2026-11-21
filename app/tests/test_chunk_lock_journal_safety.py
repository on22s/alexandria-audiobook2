"""Lock ordering, receipt snapshots and path-free journal error mapping."""
import asyncio
from contextlib import contextmanager
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import book_state_transaction as books
from book_state_transaction import BookTransactionError, BOOK_STATE_BUSY_DETAIL
from project import ProjectManager
from routers import editor, script


class ChunkEditLockOrderTests(unittest.TestCase):
    def test_edit_enters_book_journal_before_gpu_mutex(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            events = []
            real_book = editor.ensure_book_state
            real_gpu = editor._gpu_lock

            @contextmanager
            def recording_book(*args, **kwargs):
                events.append('book')
                with real_book(*args, **kwargs) as admitted:
                    yield admitted

            class RecordingGpu:
                def __enter__(self):
                    events.append('gpu')
                    return real_gpu.__enter__()

                def __exit__(self, *args):
                    return real_gpu.__exit__(*args)

            with patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                    patch.object(editor, 'ensure_book_state', side_effect=recording_book), \
                    patch.object(editor, '_gpu_lock', RecordingGpu()), \
                    patch.object(editor, 'check_global_gpu_lock'):
                result = editor._apply_chunk_edit(lambda: 'applied')

            self.assertEqual('applied', result)
            self.assertEqual(['book', 'gpu'], events)

    def test_operation_reuses_the_outer_book_admission(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            acquired = []
            real_operator_lock = books.book_operator_locked

            def counting_lock(*args, **kwargs):
                acquired.append(True)
                return real_operator_lock(*args, **kwargs)

            def operation():
                with editor.reuse_book_state(str(root), timeout=0.25):
                    return 'applied'

            with patch.object(books, 'book_operator_locked', side_effect=counting_lock), \
                    patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                    patch.object(editor, 'check_global_gpu_lock'):
                result = editor._apply_chunk_edit(operation)

            self.assertEqual('applied', result)
            # One kernel admission only: the operation reused the outer one
            # instead of re-entering the non-reentrant file lock.
            self.assertEqual(1, len(acquired))


class DeletedChunkReceiptsTests(unittest.TestCase):
    def test_reader_snapshots_while_writers_resize_the_dict(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            receipts = {}
            for index in range(8):
                receipts[f'initial-{index}'] = {'created': time.monotonic(),
                    'book': 'synthetic-book', 'chunk': {'id': index}}
            errors = []
            barrier = threading.Barrier(6)

            def reader():
                barrier.wait(timeout=2)
                for _ in range(200):
                    try:
                        editor._get_undo_take_rows()
                    except BaseException as error:
                        errors.append(error)

            def writer(tag):
                barrier.wait(timeout=2)
                for index in range(200):
                    with editor._deleted_chunk_receipts_lock:
                        receipts[f'{tag}-{index}'] = {'created': time.monotonic(),
                            'book': 'synthetic-book', 'chunk': {'id': index}}
                        if len(receipts) > 16:
                            for token in list(receipts)[:8]:
                                del receipts[token]

            original_interval = sys.getswitchinterval()
            sys.setswitchinterval(0.000001)
            try:
                with patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                        patch.object(editor, '_deleted_chunk_receipts', receipts), \
                        patch.object(editor, 'get_editor_book_identity', return_value='synthetic-book'):
                    threads = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
                    threads += [threading.Thread(target=writer, args=(f'w{tag}',), daemon=True)
                                for tag in range(3)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=5)
            finally:
                sys.setswitchinterval(original_interval)

            self.assertFalse(errors, errors[:1])
            self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_deletion_writer_uses_the_receipt_leaf_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = ProjectManager(folder)
            manager.save_chunks([{'id': index, 'speaker': 'SYNTHETIC',
                                  'text': f'Synthetic sentence {index}.', 'status': 'pending'}
                                 for index in range(3)])
            real_lock = editor._deleted_chunk_receipts_lock
            entered = []

            class RecordingLock:
                def __enter__(self):
                    entered.append(True)
                    return real_lock.__enter__()

                def __exit__(self, *args):
                    return real_lock.__exit__(*args)

            with patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                    patch.object(editor, 'project_manager', manager), \
                    patch.object(editor, '_deleted_chunk_receipts', {}), \
                    patch.object(editor, '_deleted_chunk_receipts_lock', RecordingLock()):
                deleted, _chunks, _token = editor.apply_chunk_deletion(1)

            self.assertTrue(entered)
            self.assertEqual('Synthetic sentence 1.', deleted['text'])


class JournalErrorMappingTests(unittest.TestCase):
    def test_lock_timeout_is_a_path_free_409(self):
        with patch.object(editor, 'ensure_book_state', side_effect=TimeoutError(
                'Could not acquire file lock on /private/data/.active_book_transaction.json.lock '
                'within 10 seconds.')):
            with self.assertRaises(HTTPException) as raised:
                editor._apply_chunk_edit(lambda: None)
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual(BOOK_STATE_BUSY_DETAIL, raised.exception.detail)
        self.assertNotIn('/', raised.exception.detail)

    def test_damaged_journal_is_a_path_free_500(self):
        with patch.object(editor, 'ensure_book_state',
                          side_effect=BookTransactionError('Invalid active-book transaction')):
            with self.assertRaises(HTTPException) as raised:
                editor._apply_chunk_edit(lambda: None)
        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual(BookTransactionError.DETAIL, raised.exception.detail)
        self.assertNotIn('/', raised.exception.detail)

    def test_generate_route_maps_the_same_journal_errors(self):
        for failure, status, detail in (
                (TimeoutError('Could not acquire file lock on /private/data/journal.lock'),
                 409, BOOK_STATE_BUSY_DETAIL),
                (BookTransactionError('Invalid active-book transaction'),
                 500, BookTransactionError.DETAIL)):
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(script, 'start_script_generation', side_effect=failure):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(script._start_script_generation_for_route(object(), 'source.json', None))
            self.assertEqual(status, raised.exception.status_code)
            self.assertEqual(detail, raised.exception.detail)
            self.assertNotIn('/', raised.exception.detail)

    def test_recovery_validation_failure_becomes_book_transaction_error(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(books, 'recover_book_state_locked',
                              side_effect=ValueError('Invalid active-book transaction')):
                with self.assertRaises(BookTransactionError) as raised:
                    with books.ensure_book_state(folder):
                        pass
            self.assertEqual('Invalid active-book transaction', str(raised.exception))
            self.assertIsInstance(raised.exception, ValueError)


if __name__ == '__main__':
    unittest.main()
