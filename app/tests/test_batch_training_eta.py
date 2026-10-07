"""Execute the batch CLI with simulated training times and changing resume decisions."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import wave

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('batch_eta_test', ROOT / 'tools/voice_lab/batch_train_lora.py')
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


class BatchTrainingEtaTests(unittest.TestCase):
    def setUp(self):
        from tests.test_lora_batch_preflight import apply_test_training_dependency_fixture
        apply_test_training_dependency_fixture(self)

    def run_batch(self, mode='stable'):
        clock, trained, lookups = [0], [], {}
        def train(zip_path, dataset_id, adapter_id, args):
            clock[0] += 600
            trained.append(dataset_id)
            import json
            from tests.test_support import write_test_adapter
            folder = Path(args.models_dir) / adapter_id
            write_test_adapter(folder)
            (folder / 'training_meta.json').write_text(json.dumps({'best_loss': 1.0, 'num_samples': 1}))
            return {'id': adapter_id, 'dataset_id': dataset_id}
        def exists(directory, dataset_id, manifest):
            lookups[dataset_id] = lookups.get(dataset_id, 0) + 1
            if dataset_id == 'b' and mode == 'unavailable' and lookups[dataset_id] == 1:
                raise ValueError('receipt changing')
            if dataset_id == 'b' and mode == 'changed' and lookups[dataset_id] > 1:
                return None
            return 'existing' if dataset_id in ('b', 'c') else None
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            zips = root / 'zips'
            zips.mkdir()
            pcm = io.BytesIO()
            with wave.open(pcm, 'wb') as audio:
                audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                audio.writeframes(b'\x01\x00' * 2400)
            for name in ('a', 'b', 'c', 'd'):
                with zipfile.ZipFile(zips / (name + '.zip'), 'w') as archive:
                    archive.writestr('metadata.jsonl', '{"audio_filepath":"clip.wav","text":"Hello."}\n')
                    archive.writestr('clip.wav', pcm.getvalue())
            argv = ['batch', '--zips_dir', str(zips), '--models_dir', str(root / 'models'),
                    '--datasets_dir', str(root / 'datasets'), '--manifest', str(root / 'manifest.json'),
                    '--python', sys.executable, '--device', 'cpu']
            stack.enter_context(patch.object(sys, 'argv', argv))
            stack.enter_context(patch.object(batch, 'adapter_exists', side_effect=exists))
            stack.enter_context(patch.object(batch, 'train_one', side_effect=train))
            stack.enter_context(patch.object(batch.time, 'time', side_effect=lambda: clock[0]))
            stack.enter_context(patch.object(batch, 'lock_adapter_naming', side_effect=lambda *args: contextlib.nullcontext()))
            stack.enter_context(patch.object(batch, 'get_adapter_publication_recovery_command', return_value=None))
            stack.enter_context(patch.object(batch, 'validate_adapter_registration_id_locked'))
            saved = stack.enter_context(patch.object(batch, 'save_manifest', wraps=batch.save_manifest))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(0, batch.main())
            self.assertEqual(len(trained), saved.call_count)
            return output.getvalue(), trained, lookups

    def test_eta_excludes_future_completed_adapters(self):
        output, trained, lookups = self.run_batch()
        self.assertEqual(['a', 'd'], trained)
        self.assertIn('ETA: 10 min for 1 remaining', output)
        self.assertNotIn('ETA: 30 min for 3 remaining', output)
        self.assertIn('2 trained, 2 skipped, 0 errors', output)
        self.assertGreater(lookups['b'], 1)

    def test_dispatch_rechecks_a_changed_adapter_instead_of_reusing_eta_skip(self):
        output, trained, lookups = self.run_batch('changed')
        self.assertEqual(['a', 'b', 'd'], trained)
        self.assertIn('3 trained, 1 skipped, 0 errors', output)
        self.assertGreater(lookups['b'], 1)

    def test_eta_lookup_failure_is_visible_without_stopping_independent_work(self):
        output, trained, _ = self.run_batch('unavailable')
        self.assertEqual(['a', 'd'], trained)
        self.assertIn('ETA unavailable: receipt changing', output)
        self.assertIn('2 trained, 2 skipped, 0 errors', output)
        self.assertNotIn('ETA: 30 min for 3 remaining', output)
