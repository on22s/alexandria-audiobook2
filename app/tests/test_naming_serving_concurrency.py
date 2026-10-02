"""CPU naming may move live directories while captured serving bytes stay stable."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from adapter_checkpoint_transaction import ensure_adapter_generation_snapshot, get_adapter_generation_sha256, save_adapter_checkpoint
from tests.test_adapter_checkpoint_transaction import _write
from tests.test_adapter_naming_transaction import _invoke
from voice_manifest import get_resolved_adapter_path


class NamingServingConcurrencyTests(unittest.TestCase):
    def fixture(self, root):
        models = root / 'models'; models.mkdir()
        adapter = models / 'raw_a'; _write(adapter, 1)
        (models / 'manifest.json').write_text(json.dumps([{
            'id':'raw_a','dataset_id':'raw_a','name':'raw_a',
            'voice_profile':'Warm baritone in his 30s; best for fantasy.'}]))
        return models, adapter

    def test_named_directory_moves_while_snapshot_remains_unchanged_then_old_id_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            models, adapter = self.fixture(Path(tmp))
            with ensure_adapter_generation_snapshot(adapter) as (snapshot, generation):
                captured = {p.name:p.read_bytes() for p in Path(snapshot).iterdir()}
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, _invoke(models, '--apply'))
                self.assertFalse(adapter.exists())
                resolved = Path(get_resolved_adapter_path(str(adapter)))
                self.assertNotEqual(adapter, resolved)
                self.assertTrue(resolved.is_dir())
                self.assertEqual(generation, get_adapter_generation_sha256(snapshot))
                self.assertEqual(captured, {p.name:p.read_bytes() for p in Path(snapshot).iterdir()})
                with ensure_adapter_generation_snapshot(adapter) as (next_snapshot, next_generation):
                    self.assertEqual(generation, next_generation)
                    self.assertEqual(captured, {p.name:p.read_bytes() for p in Path(next_snapshot).iterdir()})
            self.assertFalse(Path(snapshot).exists())

    def test_checkpoint_publication_with_old_id_after_naming_updates_resolved_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            models, adapter = self.fixture(Path(tmp))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, _invoke(models, '--apply'))
            resolved = Path(get_resolved_adapter_path(str(adapter)))
            save_adapter_checkpoint(adapter, lambda path: _write(path, 2))
            self.assertFalse(adapter.exists())
            self.assertTrue(resolved.is_dir())
            with ensure_adapter_generation_snapshot(adapter) as (snapshot, generation):
                meta = json.loads((Path(snapshot) / 'training_meta.json').read_text())
                self.assertEqual('generation 2', meta['ref_sample_text'])
                self.assertEqual(get_adapter_generation_sha256(snapshot), generation)
