"""Local asset identities must describe the bytes actually admitted to load."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

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

    def test_asset_observation_checks_required_files_without_opening_or_hashing_weights(self):
        original_open = os.open
        def metadata_only(path, *args, **kwargs):
            self.assertFalse(str(path).endswith('.safetensors'), 'weights must remain unopened')
            return original_open(path, *args, **kwargs)
        with patch.object(identity.os, 'open', side_effect=metadata_only), \
             patch.object(identity.hashlib, 'sha256', side_effect=AssertionError('no content hashing')):
            report = identity.get_model_asset_observation(self.root)
        self.assertEqual('required_assets_observed', report['state'])
        self.assertIsNone(report['content_sha256'])
        self.assertIsNone(report['snapshot_revision'])
        self.assertGreater(report['bytes'], 0)
        self.assertNotIn(str(self.root), json.dumps(report))
        (self.root / 'model.safetensors').unlink()
        with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable, 'missing_local_weights'):
            identity.get_model_asset_observation(self.root)

    def test_asset_observation_refuses_mutation_during_metadata_validation(self):
        validate = identity._validate_model_inventory
        def changed(root, files):
            validate(root, files)
            (root / 'model.safetensors').write_bytes(b'changed after validation')
        with patch.object(identity, '_validate_model_inventory', side_effect=changed):
            with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable, 'model_asset_changed'):
                identity.get_model_asset_observation(self.root)

    def test_metadata_reader_closes_raw_descriptor_on_wrapper_failure_success_and_parse_failure(self):
        original_open = os.open
        original_fdopen = os.fdopen
        for mode in ('allocation_failure', 'success', 'invalid_json', 'read_failure'):
            with self.subTest(mode=mode):
                path = self.root / 'config.json'
                path.write_text('not JSON' if mode == 'invalid_json' else '{}')
                opened = []
                def record_open(*args, **kwargs):
                    fd = original_open(*args, **kwargs)
                    opened.append(fd)
                    return fd
                with patch.object(identity.os, 'open', side_effect=record_open):
                    if mode == 'allocation_failure':
                        with patch.object(identity.os, 'fdopen', side_effect=MemoryError('wrapper allocation')):
                            with self.assertRaises(MemoryError):
                                identity._get_bounded_model_json(path)
                    elif mode == 'read_failure':
                        def failed_read(*args, **kwargs):
                            stream = original_fdopen(*args, **kwargs)
                            stream.read = Mock(side_effect=OSError('fixture read failed'))
                            return stream
                        with patch.object(identity.os, 'fdopen', side_effect=failed_read):
                            with self.assertRaisesRegex(OSError, 'fixture read failed'):
                                identity._get_bounded_model_json(path)
                    elif mode == 'invalid_json':
                        with self.assertRaises(json.JSONDecodeError):
                            identity._get_bounded_model_json(path)
                    else:
                        self.assertEqual({}, identity._get_bounded_model_json(path))
                self.assertEqual(1, len(opened))
                with self.assertRaises(OSError):
                    os.fstat(opened[0])

        if sys.platform == 'linux':
            guard = identity.RecoveryModelNamespaceGuard(self.root)
            with patch.object(identity, 'set', side_effect=MemoryError('watch set allocation'), create=True):
                with self.assertRaises(MemoryError):
                    guard.__enter__()
            self.assertIsNone(guard.fd)

    def test_metadata_reader_bounds_regular_files_and_refuses_fifo(self):
        path = self.root / 'config.json'
        path.write_bytes(b' ' * (1024 * 1024 + 1))
        with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable, 'model_metadata_limit'):
            identity._get_bounded_model_json(path)
        if hasattr(os, 'mkfifo'):
            path.unlink()
            os.mkfifo(path)
            with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable, 'model_metadata_limit'):
                identity._get_bounded_model_json(path)

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
        if sys.platform == 'linux':
            with patch.object(identity, '_ensure_local_model_filesystem',
                              side_effect=identity.RecoveryIdentityUnavailable('unsupported_model_namespace_filesystem')):
                guard = identity.RecoveryModelNamespaceGuard(self.root)
                with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable, 'unsupported_model_namespace_filesystem'):
                    guard.__enter__()
                self.assertIsNone(guard.fd)
            weights = self.root / 'model.safetensors'
            original_mode = weights.stat().st_mode & 0o777
            try:
                weights.chmod(original_mode | 0o020)
                guard = identity.RecoveryModelNamespaceGuard(self.root)
                with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable, 'permissions_or_clock'):
                    guard.__enter__()
                self.assertIsNone(guard.fd)
            finally:
                weights.chmod(original_mode)
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
        for raw in ('{', '{"weight_map":{},"weight_map":{}}', '{"weight_map":{"weight":NaN}}',
                    '{"weight_map":{"weight":"'+shard.name+'"},"size":1e999}',
                    '{"weight_map":{"weight":"'+shard.name+'"},"size":-1e999}',
                    '{"weight_map":{"weight":"'+shard.name+'"},"size":'+'9'*129+'}',
                    '{"nested":'+'['*2000+'0'+']'*2000+'}'):
            index.write_text(raw)
            for inspect in (identity.get_model_asset_observation, identity.capture_recovery_model_snapshot):
                with self.subTest(malformed_index=raw, inspect=inspect.__name__), self.assertRaisesRegex(
                        identity.RecoveryIdentityUnavailable, 'invalid_weight_index'):
                    inspect(self.root)
        index.write_text(json.dumps({'weight_map': {'weight': shard.name}}))
        identity.capture_recovery_model_snapshot(self.root)

    def test_tokenizer_and_processor_external_dependency_overrides_are_ineligible(self):
        path = self.root / 'config.json'
        original = path.read_bytes()
        try:
            for raw in ('{', '{"field":1,"field":2}', '{"field":NaN}',
                        '{"field":1e999}', '{"field":-1e999}',
                        '{"field":'+'9'*129+'}',
                        '{"nested":'+'['*2000+'0'+']'*2000+'}'):
                path.write_text(raw)
                for inspect in (identity.get_model_asset_observation, identity.capture_recovery_model_snapshot):
                    with self.subTest(malformed_config=raw, inspect=inspect.__name__), self.assertRaisesRegex(
                            identity.RecoveryIdentityUnavailable, 'invalid_model_config'):
                        inspect(self.root)
        finally:
            path.write_bytes(original)
        # Large normal HF tokenizer sentinels still fit the local numeric bound.
        path.write_text('{"field":'+'9'*128+'}')
        try:
            self.assertIsNotNone(identity.capture_recovery_model_snapshot(self.root))
        finally:
            path.write_bytes(original)
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

    def test_unrelated_ancestor_sibling_activity_does_not_invalidate_snapshot(self):
        # The model root is a child of the system temporary parent. An unrelated
        # sibling there changes that ancestor's mtime/ctime but no model binding.
        siblings = []
        def create_unrelated_sibling():
            if not siblings:
                siblings.append(tempfile.TemporaryDirectory(prefix='unrelated-sibling-'))
        try:
            snapshot = identity.capture_recovery_model_snapshot(
                self.root, check_active=create_unrelated_sibling)
            identity.ensure_recovery_model_snapshot_current(snapshot)
        finally:
            for sibling in siblings:
                sibling.cleanup()
        # The same holds one level further out, where the root has its own parent.
        with tempfile.TemporaryDirectory() as holder:
            outer = Path(holder) / 'outer'
            root = outer / 'model'
            shutil.copytree(self.root, root)
            snapshot = identity.capture_recovery_model_snapshot(root)
            (outer / 'unrelated').mkdir()
            (Path(holder) / 'another').mkdir()
            identity.ensure_recovery_model_snapshot_current(snapshot)
            self.assertTrue((outer / 'unrelated').is_dir())
            if sys.platform == 'linux':
                with identity.RecoveryModelNamespaceGuard(root) as guard:
                    snapshot = identity.capture_recovery_model_snapshot(root)
                    guard.bind(snapshot)
                    self.assertFalse(os.get_blocking(guard.fd))
                    self.assertFalse(os.get_inheritable(guard.fd))
                    (outer / 'second-unrelated').mkdir()
                    identity.ensure_recovery_model_snapshot_current(snapshot)
                    guard.check()
                with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable, 'changed_or_closed'):
                    guard.check()


    def test_ancestor_replacement_invalidates_snapshot(self):
        # Replacing an ancestor with a different directory tree is still caught:
        # the model root under the canonical path is a different inode/identity.
        with tempfile.TemporaryDirectory() as holder:
            outer = Path(holder) / 'outer'
            root = outer / 'model'
            shutil.copytree(self.root, root)
            snapshot = identity.capture_recovery_model_snapshot(root)
            moved = Path(holder) / 'moved'
            outer.rename(moved)
            shutil.copytree(moved / 'model', outer / 'model')
            with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable,
                                        'model_snapshot_changed'):
                identity.ensure_recovery_model_snapshot_current(snapshot)

        if sys.platform == 'linux':
            for external in (False, True):
                with self.subTest(transient_ancestor_external=external), tempfile.TemporaryDirectory() as holder:
                    base=Path(holder);outer=base/'outer';root=outer/'model'
                    shutil.copytree(self.root,root)
                    blob_outer=base/'blob-outer';blobs=blob_outer/'blobs';blobs.mkdir(parents=True)
                    blob=blobs/'weight';blob.write_bytes(b'original A')
                    if external:
                        (root/'model.safetensors').unlink();(root/'model.safetensors').symlink_to(blob)
                    with identity.RecoveryModelNamespaceGuard(root) as guard:
                        snapshot=identity.capture_recovery_model_snapshot(root);guard.bind(snapshot)
                        target=blob_outer if external else outer;parked=base/'parked'
                        target.rename(parked);shutil.copytree(parked,target,symlinks=False)
                        changed=blob if external else root/'model.safetensors';changed.write_bytes(b'other B')
                        self.assertEqual(b'other B',(root/'model.safetensors').read_bytes())
                        shutil.rmtree(target);parked.rename(target)
                        identity.ensure_recovery_model_snapshot_current(snapshot)
                        with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable,'changed_or_closed'):guard.check()
                        # Reading the event cannot permit a second certification.
                        with self.assertRaises(identity.RecoveryIdentityUnavailable):guard.check()
                    self.assertIsNone(guard.fd)
            with identity.RecoveryModelNamespaceGuard(self.root) as guard:
                # A new external target after watch setup must not enter the hash scope.
                other=self.root.parent/(self.root.name+'-other-blob')
                try:
                    other.write_bytes(b'other target')
                    path=self.root/'model.safetensors';original=path.read_bytes();path.unlink();path.symlink_to(other)
                    snapshot=identity.capture_recovery_model_snapshot(self.root)
                    with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable,'inventory_changed'):guard.bind(snapshot)
                    path.unlink();path.write_bytes(original)
                finally:other.unlink()
            for event in (b'\x00' * 16, OSError('observer read unavailable')):
                with self.subTest(uncertain_observer=type(event).__name__), identity.RecoveryModelNamespaceGuard(self.root) as guard:
                    with patch.object(identity.os, 'read', **({'return_value': event} if isinstance(event, bytes)
                                                               else {'side_effect': event})):
                        with self.assertRaises((identity.RecoveryIdentityUnavailable, OSError)):
                            guard.check()
                    with self.assertRaises(identity.RecoveryIdentityUnavailable):
                        guard.check()
            with patch.object(identity,'MAX_MODEL_NAMESPACE_WATCHES',0):
                refused=identity.RecoveryModelNamespaceGuard(self.root)
                with self.assertRaisesRegex(identity.RecoveryIdentityUnavailable,'namespace_limit'):refused.__enter__()
                self.assertIsNone(refused.fd)

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
