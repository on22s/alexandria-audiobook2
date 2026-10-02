"""Native durability/resume artifacts and serialized corrected-prefix work."""
import contextlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import review_script as review
import generation_checkpoint_deltas as deltas
from routers import editor, scripts_library
from utils import atomic_json_write

if os.environ.get('REVIEW_SOURCE'):
    spec = importlib.util.spec_from_file_location('review_before', os.environ['REVIEW_SOURCE'])
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)


def save(output, rows, batch, writer, failed=None, output_hash=None):
    kwargs = {'writer': writer} if 'writer' in inspect.signature(review.save_checkpoint).parameters else {}
    review.save_checkpoint(str(output), batch, 20, 10, 0, rows,
        {'entries_changed': batch, 'batches_failed': len(failed or [])}, rows[-2:] or None,
        [10] * batch, failed or [], 'source', output_hash, **kwargs)


class ReviewCheckpointDeltaTests(unittest.TestCase):
    def test_serializes_each_corrected_entry_once_and_native_resume_preserves_every_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'book.json'
            writer = deltas.GenerationCheckpointDeltas(review._checkpoint_path(str(output)))
            corrected = []
            serialized = 0

            def capture(data, path, *args, **kwargs):
                nonlocal serialized
                if 'all_corrected' in data:
                    serialized += len(data['all_corrected'])
                elif 'base' in data:
                    serialized += len(data['base'].get('all_corrected', []))
                else:
                    change = data.get('changes', {}).get('fields', {}).get('all_corrected', {})
                    serialized += len(change.get('entries', change.get('value', [])))
                return atomic_json_write(data, path, *args, **kwargs)

            with patch.object(review, 'atomic_json_write', side_effect=capture), \
                 patch.object(deltas, 'atomic_json_write', side_effect=capture), \
                 patch.object(review, '_entries_fingerprint', return_value='source'):
                for batch in range(1, 21):
                    corrected.extend({'speaker': 'ANN', 'text': f'証拠 {i}', 'instruct': 'Plain.'}
                                     for i in range((batch - 1) * 10, batch * 10))
                    save(output, corrected, batch, writer)
                    resumed = review.load_checkpoint(str(output), 20, 10, 0, [])
                    self.assertEqual(corrected, resumed['all_corrected'])
                    self.assertEqual(batch, resumed['completed_batches'])
                    self.assertEqual(corrected[-2:], resumed['previous_tail'])
                    self.assertEqual(batch, resumed['total_stats']['entries_changed'])
                    summary = editor._summarize_review_checkpoint(review._checkpoint_path(str(output)))
                    self.assertEqual(batch * 10, summary['entries_done'])
            print(f'Measured serialized corrected entries for20batches/200rows: {serialized}')
            self.assertEqual(200, serialized, 'corrected prefixes still serialized repeatedly')

    def test_actual_full_review_cli_uses_one_delta_writer_and_retires_its_scope(self):
        from tests import test_llm_review_regressions as fixtures
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'book.json'
            rows = [{'speaker': 'ANN', 'text': f'Original line {i}.', 'instruct': 'Plain.'}
                    for i in range(3)]
            output.write_text(json.dumps(rows))
            native_save = review.save_checkpoint
            writers, formats = [], []

            def observe(*args, **kwargs):
                native_save(*args, **kwargs)
                writers.append(kwargs.get('writer'))
                formats.append(json.loads(Path(review._checkpoint_path(str(output))).read_text()).get('storage'))

            with patch.object(review, 'save_checkpoint', side_effect=observe):
                fixtures.ReviewVramTests().run_review(output, [True] * 4,
                    lambda client, model, batch, *args, **kwargs: batch,
                    lambda *args, **kwargs: ({}, 0, []))
            self.assertEqual([deltas.STORAGE] * 3, formats)
            self.assertEqual(1, len({id(writer) for writer in writers}))
            self.assertTrue(all(isinstance(writer, deltas.GenerationCheckpointDeltas) for writer in writers))
            self.assertEqual(rows, json.loads(output.read_text()))
            path = Path(review._checkpoint_path(str(output)))
            self.assertFalse(path.exists())
            self.assertEqual([], list(path.parent.glob(path.name + '.parts-*')))

    def test_failed_rewind_compaction_and_owned_artifact_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'book.json'
            path = Path(review._checkpoint_path(str(output)))
            writer = deltas.GenerationCheckpointDeltas(path)
            rows = [{'speaker': 'ANN', 'text': str(i)} for i in range(20)]
            save(output, rows[:10], 1, writer)
            save(output, rows, 2, writer, failed=[2])
            with patch.object(review, '_entries_fingerprint', return_value='source'):
                resumed = review.load_checkpoint(str(output), 20, 10, 0, [])
            self.assertEqual(rows[:10], resumed['all_corrected'])
            self.assertEqual(1, resumed['completed_batches'])
            self.assertEqual([], resumed['failed_batches'])
            save(output, rows[:10], 1, writer)
            self.assertEqual(rows[:10], deltas.load_generation_checkpoint_document(path)['all_corrected'])
            save(output, rows, 2, writer, output_hash='partial-output')
            raw = json.loads(path.read_text())
            self.assertEqual(rows, raw['all_corrected'])
            self.assertEqual('partial-output', raw['output_sha256'])
            epochs = list(path.parent.glob(path.name + '.parts-*'))
            self.assertTrue(epochs)
            companions = scripts_library._get_saved_book_companions(str(output))
            self.assertTrue(set(map(str, epochs)).issubset(companions))
            unrelated = path.parent / 'other.review_checkpoint.json.parts-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            unrelated.mkdir()
            (unrelated / 'keep').write_text('untouched')
            review.clear_checkpoint(str(output))
            self.assertFalse(path.exists())
            self.assertFalse(any(epoch.exists() for epoch in epochs))
            self.assertEqual('untouched', (unrelated / 'keep').read_text())

    def test_atomic_failure_keeps_previous_or_published_batch_and_next_save_recovers(self):
        for published in (False, True):
            with self.subTest(published=published), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / 'book.json'
                path = Path(review._checkpoint_path(str(output)))
                writer = deltas.GenerationCheckpointDeltas(path)
                first = [{'speaker': 'ANN', 'text': str(i)} for i in range(10)]
                second = first + [{'speaker': 'ANN', 'text': str(i)} for i in range(10, 20)]
                save(output, first, 1, writer)

                def fail(data, target, *args, **kwargs):
                    if published:
                        atomic_json_write(data, target, *args, **kwargs)
                    raise OSError('disk failure')

                captured = io.StringIO()
                with patch.object(deltas, 'atomic_json_write', side_effect=fail), contextlib.redirect_stdout(captured):
                    save(output, second, 2, writer)
                self.assertIn('WARNING: Failed to save checkpoint', captured.getvalue())
                self.assertEqual(second if published else first,
                                 deltas.load_generation_checkpoint_document(path)['all_corrected'])
                save(output, second, 2, writer)
                self.assertEqual(second, deltas.load_generation_checkpoint_document(path)['all_corrected'])

    def test_ui_timestamp_tracks_delta_append_and_orphan_cleanup_retries_loudly(self):
        from generation_checkpoint_shards import shutil
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'book.json'
            path = Path(review._checkpoint_path(str(output)))
            writer = deltas.GenerationCheckpointDeltas(path)
            save(output, [{'speaker': 'ANN', 'text': str(i)} for i in range(10)], 1, writer)
            save(output, [{'speaker': 'ANN', 'text': str(i)} for i in range(20)], 2, writer)
            directory = next(path.parent.glob(path.name + '.parts-*'))
            os.utime(path, (100, 100))
            os.utime(directory, (250, 250))
            summary = editor._summarize_review_checkpoint(str(path))
            self.assertEqual(250, summary['mtime'])
            self.assertEqual(20, summary['entries_done'])
            captured = io.StringIO()
            with patch.object(shutil, 'rmtree', side_effect=OSError('cleanup refused')), \
                 contextlib.redirect_stdout(captured):
                review.clear_checkpoint(str(output))
            self.assertFalse(path.exists())
            self.assertTrue(directory.exists())
            self.assertIn('WARNING: Failed to clear checkpoint', captured.getvalue())
            review.clear_checkpoint(str(output))
            self.assertFalse(directory.exists())

    def test_rejected_corrupt_checkpoint_starts_a_new_durable_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'book.json'
            path = Path(review._checkpoint_path(str(output)))
            path.write_text('not JSON')
            self.assertIsNone(review.load_checkpoint(str(output), 20, 10, 0, []))
            writer = deltas.GenerationCheckpointDeltas(path, reset=True)
            rows = [{'speaker': 'ANN', 'text': str(i)} for i in range(10)]
            save(output, rows, 1, writer)
            self.assertFalse(writer.reset)
            self.assertEqual(rows, deltas.load_generation_checkpoint_document(path)['all_corrected'])
            save(output, rows + rows, 2, writer)
            self.assertEqual(rows + rows, deltas.load_generation_checkpoint_document(path)['all_corrected'])
            self.assertEqual(1, len(list(path.parent.glob(path.name + '.parts-*'))))

    def test_tampered_delta_is_rejected_by_resume_and_ui_without_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'book.json'
            path = Path(review._checkpoint_path(str(output)))
            writer = deltas.GenerationCheckpointDeltas(path)
            save(output, [{'speaker': 'ANN', 'text': str(i)} for i in range(10)], 1, writer)
            save(output, [{'speaker': 'ANN', 'text': str(i)} for i in range(20)], 2, writer)
            record = next(path.parent.glob(path.name + '.parts-*')).joinpath('00000000.json')
            data = json.loads(record.read_text());data['sha256'] = 'tampered'
            record.write_text(json.dumps(data))
            before = record.read_bytes()
            with patch.object(review, '_entries_fingerprint', return_value='source'):
                self.assertIsNone(review.load_checkpoint(str(output), 20, 10, 0, []))
            self.assertIsNone(editor._summarize_review_checkpoint(str(path)))
            self.assertEqual(before, record.read_bytes())
