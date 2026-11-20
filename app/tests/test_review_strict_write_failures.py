"""Strict atomic-write failures in review must stop the run, not warn and continue.

PR #2087 made the review writers refuse a cross-device (EXDEV) publish.  These
tests pin that the two callers in review_script.py actually surface that refusal
to their caller: they record a structured diagnostic, raise a self-contained
error, leave the previously published bytes untouched, and do not process a
later batch.  Non-EXDEV failures keep their pre-existing warn-and-continue
contract.
"""

import errno
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import ANY, patch

import generation_checkpoint_deltas
import review_script


CHECKPOINT_STAGE = 'review_checkpoint_write_exdev'
REMAP_STAGE = 'voice_config_remap_write_exdev'


def _exdev():
    return OSError(errno.EXDEV, 'cross-device staged JSON publish')


class _FailingCheckpointWriter:
    """Stands in for GenerationCheckpointDeltas, which raises from inside."""

    def __init__(self, error):
        self.error = error
        self.calls = []

    def save_checkpoint(self, data, *, compact=False):
        self.calls.append((data, compact))
        raise self.error


class ReviewStrictCheckpointWriteTests(unittest.TestCase):
    @staticmethod
    def _save(output):
        return review_script.save_checkpoint(
            output, 1, 2, 10, 5, [{'text': 'new progress'}], {'entries_changed': 1},
            None, [1], [], 'source-hash', 'output-hash')

    def test_direct_writer_exdev_records_raises_and_preserves_prior_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, 'book.json'))
            path = Path(review_script._checkpoint_path(output))
            prior = b'{"prior":"checkpoint"}'
            path.write_bytes(prior)
            with ExitStack() as stack:
                write = stack.enter_context(patch.object(
                    review_script, 'atomic_json_write', side_effect=_exdev()))
                record = stack.enter_context(patch.object(review_script, 'record_failure'))
                with self.assertRaisesRegex(
                        OSError,
                        r'Review stopped: the checkpoint could not be written atomically at '
                        r'.*EXDEV.*one filesystem'):
                    self._save(output)
            write.assert_called_once()
            record.assert_called_once()
            self.assertEqual(CHECKPOINT_STAGE, record.call_args.args[0])
            self.assertEqual('review', record.call_args.kwargs['task'])
            self.assertEqual(path, Path(record.call_args.kwargs['resource']))
            self.assertEqual(prior, path.read_bytes())

    def test_shard_writer_exdev_records_raises_and_preserves_prior_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, 'book.json'))
            path = Path(review_script._checkpoint_path(output))
            prior = b'{"prior":"checkpoint"}'
            path.write_bytes(prior)
            writer = _FailingCheckpointWriter(_exdev())
            with patch.object(review_script, 'record_failure') as record:
                with self.assertRaisesRegex(
                        OSError,
                        r'Review stopped: the checkpoint could not be written atomically at '
                        r'.*EXDEV.*one filesystem'):
                    review_script.save_checkpoint(
                        output, 1, 2, 10, 5, [{'text': 'new progress'}], {}, None,
                        [1], [], 'source-hash', writer=writer)
            self.assertEqual(1, len(writer.calls))
            record.assert_called_once()
            self.assertEqual(CHECKPOINT_STAGE, record.call_args.args[0])
            self.assertEqual('review', record.call_args.kwargs['task'])
            self.assertEqual(path, Path(record.call_args.kwargs['resource']))
            self.assertEqual(prior, path.read_bytes())

    def test_non_exdev_checkpoint_error_keeps_warning_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, 'book.json'))
            path = Path(review_script._checkpoint_path(output))
            prior = b'{"prior":"checkpoint"}'
            path.write_bytes(prior)
            with ExitStack() as stack:
                stack.enter_context(patch.object(
                    review_script, 'atomic_json_write',
                    side_effect=PermissionError('fixture denied')))
                record = stack.enter_context(patch.object(review_script, 'record_failure'))
                with redirect_stdout(io.StringIO()) as output_stream:
                    self._save(output)
            text = output_stream.getvalue()
            self.assertIn('Failed to save checkpoint', text)
            self.assertIn('fixture denied', text)
            record.assert_not_called()
            self.assertEqual(prior, path.read_bytes())


class ReviewStrictVoiceConfigRemapTests(unittest.TestCase):
    @staticmethod
    def _remap(path):
        return review_script._remap_voice_config(str(path), {'ALICE': 'BETTY'})

    def test_remap_exdev_records_raises_and_preserves_prior_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp, 'voice_config.json')
            prior = b'{\n  "ALICE": {"description": "before"}\n}'
            config.write_bytes(prior)
            with ExitStack() as stack:
                write = stack.enter_context(patch.object(
                    review_script, 'atomic_json_write', side_effect=_exdev()))
                record = stack.enter_context(patch.object(review_script, 'record_failure'))
                with self.assertRaisesRegex(
                        OSError,
                        r'Review stopped: the voice config could not be written atomically at '
                        r'.*EXDEV.*one filesystem'):
                    self._remap(config)
            write.assert_called_once()
            record.assert_called_once()
            self.assertEqual(REMAP_STAGE, record.call_args.args[0])
            self.assertEqual('review', record.call_args.kwargs['task'])
            self.assertEqual(config, Path(record.call_args.kwargs['resource']))
            self.assertEqual(prior, config.read_bytes())

    def test_non_exdev_remap_error_keeps_zero_and_warning_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp, 'voice_config.json')
            prior = b'{\n  "ALICE": {"description": "before"}\n}'
            config.write_bytes(prior)
            with ExitStack() as stack:
                stack.enter_context(patch.object(
                    review_script, 'atomic_json_write',
                    side_effect=PermissionError('fixture denied')))
                record = stack.enter_context(patch.object(review_script, 'record_failure'))
                with redirect_stdout(io.StringIO()) as output_stream:
                    moved = self._remap(config)
            self.assertEqual(0, moved)
            self.assertIn('failed to write', output_stream.getvalue())
            record.assert_not_called()
            self.assertEqual(prior, config.read_bytes())


class ReviewStrictWriteMainStopTests(unittest.TestCase):
    """The error must reach main() before a later batch or the output file."""

    def test_checkpoint_exdev_stops_before_next_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [{'speaker': 'A', 'text': f'Line {index}.'} for index in range(6)]
            source = root / 'annotated_script.json'
            output = root / 'reviewed.json'
            config = root / 'config.json'
            source.write_text(json.dumps(entries), encoding='utf-8')
            config.write_text(json.dumps({
                'llm_mode': 'local',
                'llm_local': {'base_url': 'http://cpu-fixture/v1', 'model_name': 'fixture',
                              'api_key': 'local', 'on_this_gpu': True},
                'generation': {'review_batch_size': 2, 'review_target_chars': 10},
            }), encoding='utf-8')

            checkpoint = Path(review_script._checkpoint_path(str(output)))
            review_script.save_checkpoint(
                str(output), 1, 3, 2, 0, entries[:2], {}, None, [2], [],
                review_script._entries_fingerprint(entries))
            prior = checkpoint.read_bytes()

            batches = []

            def review(_client, _model, batch, _index, _total, _params, **_kwargs):
                batches.append(list(batch))
                return [dict(entry) for entry in batch]

            with ExitStack() as stack:
                stack.enter_context(redirect_stdout(io.StringIO()))
                stack.enter_context(patch.object(
                    review_script, 'get_runtime_data_dir', return_value=tmp))
                stack.enter_context(patch.object(
                    review_script, 'get_app_config_path', return_value=str(config)))
                stack.enter_context(patch.object(
                    review_script, 'ensure_ideal_settings',
                    return_value=(False, {}, 'CPU fixture settings')))
                stack.enter_context(patch.object(
                    review_script, 'get_current_status', return_value={'loaded': False}))
                stack.enter_context(patch.object(
                    review_script, 'make_run_client', return_value=object()))
                stack.enter_context(patch.object(
                    review_script, 'ensure_cached_or_benchmarked_concurrency', return_value=1))
                stack.enter_context(patch.object(
                    review_script, 'wait_for_vram_headroom', return_value=True))
                stack.enter_context(patch.object(
                    review_script, 'review_batch', side_effect=review))
                stack.enter_context(patch.object(
                    generation_checkpoint_deltas, 'atomic_json_write',
                    side_effect=_exdev()))
                record = stack.enter_context(patch.object(review_script, 'record_failure'))
                with patch.object(review_script.sys, 'argv', [
                        'review_script.py', '--input', str(source),
                        '--output', str(output), '--force-review']):
                    with self.assertRaisesRegex(
                            OSError, r'Review stopped: the checkpoint could not be written '
                            r'atomically at .*EXDEV.*one filesystem'):
                        review_script.main()

            self.assertEqual(1, len(batches), 'A later batch was reviewed after the write failed')
            self.assertFalse(output.exists(), 'Review output was published after the write failed')
            self.assertEqual(prior, checkpoint.read_bytes())
            record.assert_any_call(CHECKPOINT_STAGE, ANY, task='review',
                                   resource=str(checkpoint), operation='save_checkpoint')


if __name__ == '__main__':
    unittest.main()
