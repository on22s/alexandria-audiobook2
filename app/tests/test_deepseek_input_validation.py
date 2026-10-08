"""Invalid inputs fail before publication; cached valid inputs track edits."""
import contextlib
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
import voice_clustering
from pydantic import ValidationError
from routers.lora import ReviewSubmitRequest
from tests.test_evaluation_reviews import _mk, _fp


class DeepSeekInputValidationTests(unittest.TestCase):
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
