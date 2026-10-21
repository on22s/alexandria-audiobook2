"""Identity bounds, role separation and explicit provider availability."""
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from speech_verification_runtime import (LIBRARIES, get_verifier_file_identity,
    get_verifier_identity, get_verifier_model_architecture, ensure_distinct_verifier_confirmation)


class SpeechVerifierRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.runtime = {key: str(self.root / key) for key in ('binary', 'model', 'decoder')}
        for path in self.runtime.values():
            Path(path).write_bytes(b'cached fixture')
        for name in LIBRARIES:
            (self.root / name).write_bytes(b'local library fixture')
        self.write_model()

    def write_model(self, large=False):
        vocab, state, heads, layers, mels = (51866,1280,20,32,128) if large else (51864,768,12,12,80)
        Path(self.runtime['model']).write_bytes(struct.pack('<12i', 0x67676d6c, vocab,1500,state,heads,layers,
                                                        448,state,heads,layers,mels,1))

    def test_complete_finite_identity_has_model_bundle_recipe_and_adapter(self):
        with patch('speech_verification_runtime.sys.platform', 'linux'), patch.dict(os.environ, {}, clear=True):
            primary = get_verifier_identity(self.runtime, 'primary')
        self.assertEqual(5, len(primary['libraries']))
        self.assertEqual(7, len(primary['adapter_files']))
        self.assertEqual(7, len(primary['loaded_adapter_modules']))
        self.assertEqual(12, primary['architecture']['layers'])
        self.assertIn('--no-gpu', primary['options'])
        self.assertEqual('2', primary['options'][primary['options'].index('--threads')+1])
        self.assertEqual('cpu', primary['device'])

    def test_copied_model_or_wrong_architecture_does_not_qualify_as_stronger(self):
        with patch('speech_verification_runtime.sys.platform', 'linux'), patch.dict(os.environ, {}, clear=True):
            primary = get_verifier_identity(self.runtime, 'primary')
            with self.assertRaisesRegex(ValueError, 'architectures'):
                get_verifier_identity(self.runtime, 'confirmation')
            with self.assertRaisesRegex(ValueError, 'distinct'):
                ensure_distinct_verifier_confirmation(primary, {**primary, 'role': 'confirmation'})
            self.write_model(large=True)
            confirmation = get_verifier_identity(self.runtime, 'confirmation')
            ensure_distinct_verifier_confirmation(primary, confirmation)
            self.assertNotEqual(primary['model']['sha256'], confirmation['model']['sha256'])

    def test_library_changes_are_bound_and_missing_library_is_unavailable(self):
        with patch('speech_verification_runtime.sys.platform', 'linux'), patch.dict(os.environ, {}, clear=True):
            before = get_verifier_identity(self.runtime, 'primary')
            (self.root / LIBRARIES[0]).write_bytes(b'changed native library')
            after = get_verifier_identity(self.runtime, 'primary')
            self.assertNotEqual(before['libraries'][0]['sha256'], after['libraries'][0]['sha256'])
            (self.root / LIBRARIES[0]).unlink()
            with self.assertRaises(FileNotFoundError):
                get_verifier_identity(self.runtime, 'primary')

    def test_loader_overrides_and_other_platforms_are_explicit_without_provider_launch(self):
        with patch('speech_verification_runtime.sys.platform', 'linux'), patch.dict(os.environ, {'LD_PRELOAD': '/foreign.so'}):
            with self.assertRaisesRegex(ValueError, 'loader overrides'):
                get_verifier_identity(self.runtime, 'primary')
        with patch('speech_verification_runtime.sys.platform', 'win32'):
            with self.assertRaisesRegex(ValueError, 'Linux'):
                get_verifier_identity(self.runtime, 'primary')

    def test_bad_header_and_role_are_unavailable(self):
        for contents in [b'', b'bad header' * 10, struct.pack('<12i', *([0] * 12))]:
            Path(self.runtime['model']).write_bytes(contents)
            with self.assertRaises(ValueError):
                get_verifier_model_architecture(self.runtime['model'])
        with self.assertRaisesRegex(ValueError, 'role'):
            get_verifier_identity(self.runtime, 'other')

    def test_missing_optional_loaded_owner_identity_cannot_qualify_verification(self):
        with patch('speech_verification_runtime.sys.platform','linux'),patch.dict(os.environ,{},clear=True), \
                patch('subprocess_ownership._LOADED_SPEECH_VERIFICATION_CODE_SHA256',None):
            with self.assertRaisesRegex(ValueError,'Loaded verifier adapter code identity is unavailable'):
                get_verifier_identity(self.runtime,'primary')

    def test_model_role_header_must_match_the_hashed_file_version(self):
        model = get_verifier_file_identity(self.runtime['model'], 1024, lambda: False)
        self.write_model(large=True)
        with self.assertRaisesRegex(ValueError, 'changed before'):
            get_verifier_model_architecture(self.runtime['model'], model)

    def test_file_hash_captures_resolved_alias_and_honors_size_and_cancel_bounds(self):
        path = self.root / 'asset'; path.write_bytes(b'exact bytes')
        row = get_verifier_file_identity(path, 20, lambda: False)
        self.assertEqual(11, row['size'])
        with self.assertRaises(ValueError):
            get_verifier_file_identity(path, 10, lambda: False)
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            get_verifier_file_identity(path, 20, lambda: True)
        with self.assertRaises(ValueError):
            get_verifier_file_identity(self.root, 100, lambda: False)


if __name__ == '__main__':
    unittest.main()
