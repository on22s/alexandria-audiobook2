"""Invalid inputs fail before publication; cached valid inputs track edits."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import evaluation_reviews
import gold_set_builder
import gpu_progress
import prompt_loader
import utils
import voice_clustering
from pydantic import ValidationError
from routers.lora import ReviewSubmitRequest
from tests.test_evaluation_reviews import _mk, _fp


class DeepSeekInputValidationTests(unittest.TestCase):
    def test_rapid_preserved_mtime_prompt_edits_return_every_written_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'prompts.txt')
            path.write_bytes(b'alpha---SEPARATOR---user')
            original = path.stat()
            cache = {}
            load = lambda: prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache)
            self.assertEqual(('alpha', 'user'), load())
            for iteration in range(500):
                expected = 'bravo' if iteration % 2 == 0 else 'alpha'
                path.write_bytes((expected + '---SEPARATOR---user').encode())
                os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
                self.assertEqual((expected, 'user'), load(), f'write {iteration}')

    def test_recent_equal_prompt_token_revalidates_content_until_recaptured_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'prompts.txt')
            path.write_bytes(b'alpha---SEPARATOR---user')
            token = utils.get_file_cache_token(path)
            cache = {}
            load = lambda: prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache)
            with patch('utils.get_file_cache_token', return_value=token):
                self.assertEqual(('alpha', 'user'), load())
                path.write_bytes(b'bravo---SEPARATOR---user')
                self.assertEqual(('bravo', 'user'), load())
                with patch('utils.time.time_ns', return_value=time.time_ns() + 3_000_000_000):
                    with patch('utils.read_file_snapshot', wraps=utils.read_file_snapshot) as capture:
                        self.assertEqual(('bravo', 'user'), load())
                        self.assertEqual(1, capture.call_count)
                    with patch('utils.hashlib.sha256', side_effect=AssertionError('redundant hash')):
                        self.assertEqual(('bravo', 'user'), load())

    def test_recent_equal_negative_prompt_token_does_not_hide_repairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'prompts.txt')
            path.write_bytes(b'     ---SEPARATOR---user')
            original = path.stat()
            token = utils.get_file_cache_token(path)
            cache = {}
            with patch('utils.get_file_cache_token', return_value=token):
                with self.assertRaisesRegex(RuntimeError, 'malformed'):
                    prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache)
                path.write_bytes(b'bravo---SEPARATOR---user')
                os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
                self.assertEqual(('bravo', 'user'),
                                 prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache))
                self.assertNotIn('malformed_captured_at_ns', cache)

    def test_prompt_snapshot_aba_does_not_cache_parsed_bytes_under_another_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'prompts.txt')
            first = b'alpha---SEPARATOR---user'
            second = b'bravo---SEPARATOR---user'
            path.write_bytes(first)
            original = path.stat()
            cache = {}

            @contextlib.contextmanager
            def alternate(*args, **kwargs):
                path.write_bytes(second)
                with open(*args, **kwargs) as source:
                    yield source
                path.write_bytes(first)

            with patch('os.stat', return_value=original), patch('os.fstat', return_value=original), \
                 patch('utils.get_file_cache_token', return_value=None, create=True):
                with patch.object(prompt_loader, 'open', side_effect=alternate, create=True):
                    self.assertEqual(('bravo', 'user'),
                                     prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache))
                self.assertEqual(hashlib.sha256(second).digest(), cache['content_hash'])
                self.assertEqual(first, path.read_bytes())
                self.assertEqual(('alpha', 'user'),
                                 prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache))
                self.assertEqual(hashlib.sha256(first).digest(), cache['content_hash'])

    def test_warm_prompt_cache_does_not_read_or_hash_unchanged_content(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch('utils.time.time_ns', wraps=time.time_ns) as clock:
            path = Path(tmp, 'prompts.txt')
            path.write_bytes(b'alpha---SEPARATOR---user')
            clock.return_value = time.time_ns() + 3_000_000_000
            cache = {}
            expected = prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache)
            with patch('utils.open', side_effect=AssertionError('redundant content read'), create=True), \
                 patch('utils.hashlib.sha256', side_effect=AssertionError('redundant hash')):
                self.assertEqual(expected, prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache))

    def test_binary_snapshot_preserves_universal_newline_prompt_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'prompts.txt')
            payload = b'first\r\nline\rlast---SEPARATOR---user\r\nline'
            path.write_bytes(payload)
            cache = {}
            self.assertEqual(('first\nline\nlast', 'user\nline'),
                             prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache))
            self.assertEqual(hashlib.sha256(payload).digest(), cache['content_hash'])

    def test_unchanged_snapshot_refreshes_a_changed_prompt_token(self):
        for payload, token_field in ((b'alpha---SEPARATOR---user', 'file_token'),
                                     (b'malformed', 'malformed_token')):
            with self.subTest(token_field=token_field), tempfile.TemporaryDirectory() as tmp, \
                 patch('utils.time.time_ns', wraps=time.time_ns) as clock:
                path = Path(tmp, 'prompts.txt')
                path.write_bytes(payload)
                clock.return_value = time.time_ns() + 3_000_000_000
                cache = {}

                def load():
                    if token_field == 'malformed_token':
                        with self.assertRaisesRegex(RuntimeError, 'malformed'):
                            prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache)
                    else:
                        self.assertEqual(('alpha', 'user'),
                                         prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache))

                load()
                changed = list(utils.get_file_cache_token(path))
                changed[3] += 1
                with patch('utils.get_file_cache_token', return_value=tuple(changed)):
                    load()
                    self.assertEqual(tuple(changed), cache[token_field])
                    with patch('utils.hashlib.sha256', side_effect=AssertionError('redundant hash')):
                        load()

    def test_native_change_query_failure_requires_hashing_instead_of_a_cache_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'prompts.txt')
            path.write_bytes(b'alpha---SEPARATOR---user')
            cache = {}
            load = lambda: prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache)
            self.assertEqual(('alpha', 'user'), load())
            original = path.stat()
            with patch('utils.sys.platform', 'win32'), \
                 patch('utils._get_windows_file_cache_token', side_effect=OSError('query failed')), \
                 patch('os.stat', return_value=original), patch('os.fstat', return_value=original):
                self.assertIsNone(utils.get_file_cache_token(path))
                path.write_bytes(b'bravo---SEPARATOR---user')
                self.assertEqual(('bravo', 'user'), load())
                with patch('utils.open', side_effect=PermissionError('fixture denied'), create=True), \
                     patch.object(prompt_loader, 'open', side_effect=PermissionError('fixture denied'), create=True):
                    with self.assertRaisesRegex(RuntimeError, 'Error reading'):
                        load()

    def test_cheap_change_token_tracks_a_preserved_mtime_in_place_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'content.bin')
            path.write_bytes(b'alpha')
            original = path.stat()
            token = utils.get_file_cache_token(path)
            self.assertIsNotNone(token)
            time.sleep(0.05)
            path.write_bytes(b'bravo')
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertNotEqual(token, utils.get_file_cache_token(path))
            if os.name == 'nt':
                current = path.stat()
                self.assertEqual((original.st_ino, original.st_size, original.st_mtime_ns, original.st_ctime_ns),
                                 (current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns))

    def test_missing_timezone_data_keeps_measured_queue_eta(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = time.monotonic()
            document = {'owner_pid': '123', 'owner_token': 'token', 'job': 'job',
                        'run_id': 'run', 'worker_pid': '456', 'worker_token': 'worker',
                        'phase': 'test', 'total': 10, 'samples': [
                            {'completed': 0, 'monotonic': now - 10},
                            {'completed': 5, 'monotonic': now}]}
            Path(tmp, '123.json').write_text(json.dumps(document))
            with patch.object(gpu_progress, 'get_gpu_progress_process_token', return_value='worker'), \
                 patch.object(gpu_progress, 'ZoneInfo', side_effect=ZoneInfoNotFoundError('missing')):
                message = gpu_progress.get_queue_eta_message(tmp, 'job', [('123', 'token')])
            self.assertIn('ETA for test:', message)
            self.assertIn('5/10 units', message)

    def test_invalid_gold_options_fail_before_loading_or_writing(self):
        for flag, value in [('--count', '-1'), ('--batch-size', '0'), ('--batch-size', '-1')]:
            with self.subTest(flag=flag, value=value), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(gold_set_builder, 'load_run') as load, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                gold_set_builder.main(['build', 'book', flag, value, '--out', tmp])
            self.assertEqual(2, error.exception.code)
            load.assert_not_called()
        self.assertEqual([], gold_set_builder.build([], 'book', 0, 1)[0])

    def test_nonobject_narrator_override_is_a_validation_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'overrides.json')
            for override in (None, [], 'bad', 1):
                with self.subTest(override=override):
                    path.write_text(json.dumps({'version': 1, 'narrators': {'voice': override}}))
                    with self.assertRaises(ValueError):
                        voice_clustering.load_cluster_overrides(path, 'voice')
            path.write_text(json.dumps({'version': 1, 'narrators': {}}))
            self.assertEqual({'merge': [], 'split': []},
                             voice_clustering.load_cluster_overrides(path, 'voice'))

    def test_ratings_do_not_coerce_booleans_or_fractional_values(self):
        for value in (True, False, 3.5, float('inf'), float('nan')):
            with self.subTest(value=value), self.assertRaises(evaluation_reviews.ReviewError):
                evaluation_reviews._clean_rating(value)
            with self.subTest(api_value=value), self.assertRaises(ValidationError):
                ReviewSubmitRequest(choice='tie', rating=value)
        for value in (3, '3', 3.0):
            self.assertEqual(3, evaluation_reviews._clean_rating(value))
        self.assertIsNone(evaluation_reviews._clean_rating(None))

    def test_invalid_rating_does_not_consume_session_or_write_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _mk(tmp)
            before = {p.relative_to(tmp): p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()}
            for value in (True, 3.7):
                with self.assertRaises(evaluation_reviews.ReviewError):
                    evaluation_reviews.submit(tmp, 'voice_x', session['session_id'], 'tie', _fp(), rating=value)
                self.assertEqual(before, {p.relative_to(tmp): p.read_bytes()
                                          for p in Path(tmp).rglob('*') if p.is_file()})
            evaluation_reviews.submit(tmp, 'voice_x', session['session_id'], 'tie', _fp(), rating=3)
            records = evaluation_reviews.list_reviews(tmp, 'voice_x')
            self.assertEqual(3, records[0]['human']['rating'])

    def test_valid_prompt_edit_with_preserved_mtime_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'prompts.txt')
            path.write_text('first---SEPARATOR---user')
            cache = {}
            load = lambda: prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache)
            self.assertEqual(('first', 'user'), load())
            original = path.stat()
            path.write_text('other---SEPARATOR---user')
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertEqual(('other', 'user'), load())
            with patch.object(prompt_loader, 'open', side_effect=AssertionError('redundant read'), create=True):
                self.assertEqual(('other', 'user'), load())

    def test_prompt_cache_checks_content_when_all_stat_fields_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'prompts.txt')
            path.write_text('first---SEPARATOR---user')
            original = path.stat()
            cache = {}
            load = lambda: prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache)
            with patch('os.stat', return_value=original), patch('os.fstat', return_value=original), \
                 patch('utils.get_file_cache_token', return_value=None):
                self.assertEqual(('first', 'user'), load())
                path.write_text('other---SEPARATOR---user')
                self.assertEqual(('other', 'user'), load())
                path.write_text('other---SEPARATOR---    ')
                with self.assertRaisesRegex(RuntimeError, 'malformed'):
                    load()
                path.write_text('fixed---SEPARATOR---user')
                self.assertEqual(('fixed', 'user'), load())
                self.assertEqual(5, len(cache))

    def test_content_version_hashes_all_bytes_in_bounded_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'content.bin')
            payload = b'x' * (128 * 1024) + b'end'
            path.write_bytes(payload)
            with path.open('rb') as source, \
                 patch.object(source, 'read', wraps=source.read) as read, \
                 patch('utils.open', return_value=source, create=True):
                captured, (version, digest, token, captured_at_ns) = utils.read_file_snapshot(path)
            self.assertEqual(payload, captured)
            self.assertEqual(hashlib.sha256(payload).digest(), digest)
            self.assertEqual(len(payload), version[2])
            self.assertLessEqual(captured_at_ns, time.time_ns())
            self.assertEqual(4, read.call_count)
            self.assertTrue(all(call.args == (64 * 1024,) for call in read.call_args_list))

    def test_racy_cache_window_is_strict_and_clock_rollback_refuses_metadata_hits(self):
        changed_at_ns = 1_800_000_000_000_000_000
        for platform in ('win32', 'linux'):
            timestamp = (changed_at_ns // 100 + 116444736000000000
                         if platform == 'win32' else changed_at_ns)
            token = (1, 2, 3, 4, timestamp)
            captured_at_ns = changed_at_ns + 2_000_000_001
            with self.subTest(platform=platform), patch('utils.sys.platform', platform), \
                 patch('utils.time.time_ns', return_value=captured_at_ns):
                self.assertFalse(utils.is_file_cache_token_trusted(None, captured_at_ns))
                self.assertFalse(utils.is_file_cache_token_trusted(token, None))
                self.assertFalse(utils.is_file_cache_token_trusted(token, changed_at_ns))
                self.assertFalse(utils.is_file_cache_token_trusted(token, captured_at_ns - 1))
                self.assertTrue(utils.is_file_cache_token_trusted(token, captured_at_ns))
                self.assertFalse(utils.is_file_cache_token_trusted(token, captured_at_ns + 1))

    def test_content_version_refuses_a_file_changed_during_hashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'content.bin')
            path.write_bytes(b'first')
            original = path.stat()

            def changed_stat(descriptor):
                path.write_bytes(b'changed length')
                return original

            with patch('utils.os.fstat', side_effect=changed_stat):
                with self.assertRaisesRegex(OSError, 'File changed while capturing snapshot'):
                    utils.read_file_snapshot(path)

    def test_prompt_read_race_does_not_publish_a_cache_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'prompts.txt')
            path.write_text('first---SEPARATOR---user')
            cache = {}
            load = lambda: prompt_loader.load_prompts_file(path, 2, 'missing', 'malformed', cache)
            self.assertEqual(('first', 'user'), load())
            before = dict(cache)
            path.write_text('other-longer---SEPARATOR---user')

            @contextlib.contextmanager
            def read_then_edit(*args, **kwargs):
                with open(*args, **kwargs) as source:
                    yield source
                path.write_text('third---SEPARATOR---user')

            with patch('prompt_loader.open', side_effect=read_then_edit, create=True):
                with self.assertRaisesRegex(RuntimeError, 'File changed while capturing snapshot'):
                    load()
            self.assertEqual(before, cache)
            self.assertEqual(('third', 'user'), load())
