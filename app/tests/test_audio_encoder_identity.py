"""Executable identity and version probing must fail closed before encoding."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import audio_encoder_identity as identity


class AudioEncoderIdentityTests(unittest.TestCase):
    def test_native_probe_is_finite_and_pure_and_exposes_only_a_digest(self):
        runtime=identity.get_ffmpeg_runtime_identity()
        self.assertEqual(64,len(identity.get_ffmpeg_runtime_digest(runtime)))
        self.assertEqual(runtime.binary.path,identity.require_ffmpeg_binary_identity(runtime.binary))
        self.assertTrue(Path(runtime.binary.path).is_absolute())

    def test_replaced_executable_and_changed_path_refuse_without_worker_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            first=Path(tmp)/'first';first.write_bytes(b'first executable fixture')
            second=Path(tmp)/'second';second.write_bytes(first.read_bytes())
            with patch.object(identity.shutil,'which',return_value=str(first)):
                expected=identity.get_ffmpeg_binary_identity()
                with patch.object(identity,'run_owned_media_check') as spawn:
                    with patch.object(identity.shutil,'which',return_value=str(second)):
                        with self.assertRaisesRegex(ValueError,'approved identity'):
                            identity.require_ffmpeg_binary_identity(expected)
                    first.write_bytes(b'other executable fixture')
                    with self.assertRaisesRegex(ValueError,'approved identity'):
                        identity.require_ffmpeg_binary_identity(expected)
                    spawn.assert_not_called()

    def test_version_probe_uses_absolute_captured_file_and_detects_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary=Path(tmp)/'encoder';binary.write_bytes(b'original fixture')
            def probe(command,check,timeout,**options):
                self.assertEqual([str(binary),'-version'],command)
                self.assertEqual(10,timeout)
                self.assertEqual(identity.MAX_VERSION_BYTES,options['output_limit'])
                binary.write_bytes(b'changed fixture')
                return b'ffmpeg version fixture\n'
            with patch.object(identity.shutil,'which',return_value=str(binary)), \
                    patch.object(identity,'run_owned_media_check',side_effect=probe):
                with self.assertRaisesRegex(ValueError,'approved identity'):
                    identity.get_ffmpeg_runtime_identity()

    def test_reported_version_changes_digest_and_invalid_output_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary=Path(tmp)/'encoder';binary.write_bytes(b'fixed fixture')
            with patch.object(identity.shutil,'which',return_value=str(binary)):
                captured=[]
                for raw in (b'ffmpeg version one\n',b'ffmpeg version two\n'):
                    with patch.object(identity,'run_owned_media_check',return_value=raw):
                        captured.append(identity.get_ffmpeg_runtime_digest(identity.get_ffmpeg_runtime_identity()))
                self.assertNotEqual(*captured)
                with patch.object(identity,'run_owned_media_check',return_value=b'unexpected'):
                    with self.assertRaisesRegex(ValueError,'invalid version'):
                        identity.get_ffmpeg_runtime_identity()

    def test_missing_encoder_cancellation_and_malformed_identity(self):
        with patch.object(identity.shutil,'which',return_value=None):
            with self.assertRaises(ValueError):identity.get_ffmpeg_runtime_identity()
        class Cancelled(RuntimeError):pass
        def cancelled():raise Cancelled('fixture cancel')
        with patch.object(identity,'run_owned_media_check') as spawn:
            with self.assertRaises(Cancelled):identity.get_ffmpeg_runtime_identity(cancelled)
            spawn.assert_not_called()
        runtime=identity.get_ffmpeg_runtime_identity().model_dump()
        for change in (dict(version_sha256='x'),dict(unknown=1),dict(binary={})):
            row={**copy.deepcopy(runtime),**change}
            with self.subTest(change=change),self.assertRaises(ValueError):
                identity.get_ffmpeg_runtime_digest(row)
