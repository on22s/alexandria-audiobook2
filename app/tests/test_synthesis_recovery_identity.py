"""Local asset identities must describe the bytes actually admitted to load."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import synthesis_recovery_identity as identity


class RecoveryModelIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in identity._REQUIRED_ASSETS | {'model.safetensors', 'speech_tokenizer/model.safetensors'}:
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'{"synthetic":true}' if name.endswith('.json') else b'synthetic model fixture')

    def test_equal_content_at_different_roots_has_equal_semantic_identity(self):
        import shutil
        first = identity.capture_recovery_model_snapshot(self.root)
        with tempfile.TemporaryDirectory() as temporary:
            other = Path(temporary) / 'copied'
            shutil.copytree(self.root, other)
            second = identity.capture_recovery_model_snapshot(other)
        self.assertEqual(first.sha256, second.sha256)
        self.assertNotEqual(first.root, second.root)
        self.assertNotIn(str(self.root), first.sha256)

    def test_each_serving_asset_changes_digest_and_invalidates_prior_admission(self):
        first = identity.capture_recovery_model_snapshot(self.root)
        for item in first.files:
            with self.subTest(asset=item.name):
                path = self.root / item.name
                original = path.read_bytes()
                path.write_bytes(original + b' ')
                with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable, 'model_snapshot_changed'):
                    identity.ensure_recovery_model_snapshot_current(first)
                self.assertNotEqual(first.sha256, identity.capture_recovery_model_snapshot(self.root).sha256)
                path.write_bytes(original)
                first = identity.capture_recovery_model_snapshot(self.root)

    def test_added_removed_replaced_or_missing_assets_are_ineligible(self):
        first = identity.capture_recovery_model_snapshot(self.root)
        extra = self.root / 'new-generation-asset.json'
        extra.write_text('{}')
        with self.assertRaises(identity.RecoveryIdentityUnavailable):
            identity.ensure_recovery_model_snapshot_current(first)
        extra.unlink()
        path = self.root / 'vocab.json'
        content = path.read_bytes()
        path.unlink()
        with self.assertRaises(identity.RecoveryIdentityUnavailable):
            identity.capture_recovery_model_snapshot(self.root)
        path.write_bytes(content)
        with self.assertRaises(identity.RecoveryIdentityUnavailable):
            identity.ensure_recovery_model_snapshot_current(first)

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX HF file links')
    def test_trusted_hf_file_alias_is_bound_to_actual_target_and_rechecked(self):
        path = self.root / 'model.safetensors'
        path.unlink()
        with tempfile.TemporaryDirectory() as temporary:
            blob = Path(temporary) / 'blob'
            blob.write_bytes(b'known trusted HF blob')
            path.symlink_to(blob)
            first = identity.capture_recovery_model_snapshot(self.root)
            identity.ensure_recovery_model_snapshot_current(first)
            blob.write_bytes(b'changed trusted HF blob')
            with self.assertRaises(identity.RecoveryIdentityUnavailable):
                identity.ensure_recovery_model_snapshot_current(first)
            path.unlink()
            path.symlink_to(self.root / 'speech_tokenizer')
            with self.assertRaises(identity.RecoveryIdentityUnavailable):
                identity.capture_recovery_model_snapshot(self.root)

    def test_inventory_and_hash_limits_fail_closed(self):
        for limit, value in (('MAX_MODEL_FILES', 2), ('MAX_MODEL_DIRECTORIES', 1),
                             ('MAX_MODEL_BYTES', 1), ('MAX_MODEL_FILE_BYTES', 1)):
            with self.subTest(limit=limit), patch.object(identity, limit, value):
                with self.assertRaises(identity.RecoveryIdentityUnavailable):
                    identity.capture_recovery_model_snapshot(self.root)
        fifo = self.root / 'unknown-fifo'
        if hasattr(os, 'mkfifo'):
            os.mkfifo(fifo)
            with self.assertRaises(identity.RecoveryIdentityUnavailable):
                identity.capture_recovery_model_snapshot(self.root)

    def test_sharded_weight_index_requires_local_bounded_references(self):
        (self.root / 'model.safetensors').unlink()
        shard = self.root / 'model-00001.safetensors'
        shard.write_bytes(b'synthetic shard')
        index = self.root / 'model.safetensors.index.json'
        for reference in ('../escape.safetensors', '/escape.safetensors', 'missing.safetensors',
                          'speech_tokenizer/model.safetensors', 5):
            with self.subTest(reference=reference):
                index.write_text(json.dumps({'weight_map': {'weight': reference}}))
                with self.assertRaises(identity.RecoveryIdentityUnavailable):
                    identity.capture_recovery_model_snapshot(self.root)
        index.write_text(json.dumps({'weight_map': {'weight': shard.name}}))
        identity.capture_recovery_model_snapshot(self.root)

    def test_tokenizer_and_processor_external_dependency_overrides_are_ineligible(self):
        # Transformers accepts these even when from_pretrained's root is local.
        # The first recovery contract deliberately accepts only conventional
        # local dependencies; ordinary native loading is not restricted.
        for name, field, value in (
                ('tokenizer_config.json', 'tokenizer_file', '/outside/tokenizer.json'),
                ('tokenizer_config.json', 'fast_tokenizer_files', ['../outside/tokenizer.1.0.0.json']),
                ('tokenizer_config.json', 'vocab_file', '/outside/vocab.json'),
                ('tokenizer_config.json', 'merges_file', '../outside/merges.txt'),
                ('preprocessor_config.json', 'audio_tokenizer', {'audio_tokenizer_name_or_path': 'moving/id'}),
                ('config.json', 'auto_map', {'AutoModel': 'unproven.CustomModel'}),
                ('speech_tokenizer/config.json', 'nested', {'decoder': {'weights_file': '/outside/weights'}})):
            path = self.root / name
            original = path.read_bytes()
            with self.subTest(name=name, field=field):
                document = json.loads(original)
                document[field] = value
                path.write_text(json.dumps(document))
                with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable, 'unsupported_model_asset_reference'):
                    identity.capture_recovery_model_snapshot(self.root)
                path.write_bytes(original)
        identity.capture_recovery_model_snapshot(self.root)

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX HF file links')
    def test_restored_hf_link_and_directory_names_invalidate_load_admission(self):
        path = self.root / 'model.safetensors'
        path.unlink()
        with tempfile.TemporaryDirectory() as temporary:
            first_blob, second_blob = Path(temporary) / 'first', Path(temporary) / 'second'
            first_blob.write_bytes(b'first known payload')
            second_blob.write_bytes(b'second payload')
            path.symlink_to(first_blob)
            captured = identity.capture_recovery_model_snapshot(self.root)
            path.unlink()
            path.symlink_to(second_blob)
            path.unlink()
            path.symlink_to(first_blob)
            with self.assertRaises(identity.RecoveryIdentityUnavailable):
                identity.ensure_recovery_model_snapshot_current(captured)
            captured = identity.capture_recovery_model_snapshot(self.root)
            directory = self.root / 'speech_tokenizer'
            directory.rename(self.root / 'temporarily-renamed')
            (self.root / 'temporarily-renamed').rename(directory)
            with self.assertRaises(identity.RecoveryIdentityUnavailable):
                identity.ensure_recovery_model_snapshot_current(captured)
            chained = Path(temporary) / 'chained'
            chained.symlink_to(first_blob)
            path.unlink()
            path.symlink_to(chained)
            with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable, 'indirect_model_asset_alias'):
                identity.capture_recovery_model_snapshot(self.root)

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX HF file links')
    def test_restored_external_blob_directory_name_invalidates_load_admission(self):
        path = self.root / 'model.safetensors'
        path.unlink()
        with tempfile.TemporaryDirectory() as temporary:
            blobs = Path(temporary) / 'blobs'
            blobs.mkdir()
            blob = blobs / 'known'
            blob.write_bytes(b'known HF payload')
            path.symlink_to(blob)
            captured = identity.capture_recovery_model_snapshot(self.root)
            moved = Path(temporary) / 'moved'
            blobs.rename(moved)
            blobs.mkdir()
            (blobs/'known').write_bytes(b'another payload consumed during loading')
            (blobs/'known').unlink()
            blobs.rmdir()
            moved.rename(blobs)
            with self.assertRaises(identity.RecoveryIdentityUnavailable):
                identity.ensure_recovery_model_snapshot_current(captured)
