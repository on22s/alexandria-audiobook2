"""Declared RIFF extents are independent of decoder-clamped frame counts."""
from pathlib import Path
import struct
import tempfile
import unittest
import wave

import numpy as np
import soundfile as sf
from audio_validation import (GeneratedAudioError, GeneratedAudioCancelled,
                              validate_generated_audio, validate_wav_container)


class WavContainerValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic.wav'
        with wave.open(str(self.path), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\x01\x10' * 2400)
        self.good = self.path.read_bytes()

    def test_data_extent_refuses_even_when_decoder_info_and_decode_agree(self):
        raw = bytearray(self.good); struct.pack_into('<I', raw, 40, 9600); self.path.write_bytes(raw)
        self.assertEqual(sf.info(self.path).frames, 2400)
        self.assertEqual(sf.read(self.path)[0].shape, (2400,))
        with wave.open(str(self.path), 'rb') as stream:
            self.assertEqual(stream.getnframes(), 4800)
            self.assertEqual(len(stream.readframes(4800)), 4800)
        with self.assertRaisesRegex(GeneratedAudioError, 'truncated WAV subchunk'):
            validate_generated_audio(self.path, expected_text='Hello.')
        with self.path.open('rb') as stream:
            stream.seek(7)
            with self.assertRaises(GeneratedAudioError): validate_wav_container(stream)
            self.assertEqual(stream.tell(), 7)

    def test_healthy_integer_float_and_non_wav_keep_real_validation(self):
        for suffix, subtype in (('.wav', 'PCM_16'), ('.wav', 'FLOAT'), ('.flac', 'PCM_16')):
            with self.subTest(subtype=subtype):
                path = self.path.with_suffix(suffix)
                sf.write(path, np.full(2400, .125), 24000, subtype=subtype)
                before = path.read_bytes()
                self.assertEqual(validate_generated_audio(path, expected_text='Hello.'), path)
                self.assertEqual(path.read_bytes(), before)

    def test_metadata_padding_and_data_are_checked_against_riff_boundary(self):
        fmt, data = self.good[12:36], self.good[36:]
        junk = b'JUNK' + struct.pack('<I', 3) + b'abc\x00'
        payload = fmt + junk + data
        healthy = b'RIFF' + struct.pack('<I', len(payload) + 4) + b'WAVE' + payload
        self.path.write_bytes(healthy)
        self.assertEqual(validate_generated_audio(self.path), self.path)
        for kind in ('metadata', 'header', 'padding'):
            with self.subTest(kind=kind):
                raw = bytearray(healthy)
                if kind == 'metadata': struct.pack_into('<I', raw, 40, len(raw) * 2)
                elif kind == 'header': struct.pack_into('<I', raw, 4, 7)
                else:
                    # A terminal odd chunk requires its declared pad byte too.
                    raw = bytearray(self.good + b'JUNK' + struct.pack('<I', 1) + b'x')
                    struct.pack_into('<I', raw, 4, len(raw) - 8)
                self.path.write_bytes(raw)
                with self.assertRaises(GeneratedAudioError): validate_generated_audio(self.path)

    def test_container_scan_keeps_cancellation_and_same_descriptor_position(self):
        with self.path.open('rb') as stream:
            stream.seek(17)
            with self.assertRaises(GeneratedAudioCancelled):
                validate_wav_container(stream, cancel_check=lambda: True)
            self.assertEqual(stream.tell(), 17)
        with self.path.open('rb') as stream:
            stream.seek(17); validate_wav_container(stream); self.assertEqual(stream.tell(), 17)

    def test_metadata_chunk_scan_is_bounded_without_decoding_or_consuming_payloads(self):
        body = b'JUNK' + struct.pack('<I', 0)
        raw = b'RIFF' + struct.pack('<I', 4 + len(body) * 65537) + b'WAVE' + body * 65537
        self.path.write_bytes(raw)
        with self.path.open('rb') as stream:
            with self.assertRaisesRegex(GeneratedAudioError, 'too many subchunks'):
                validate_wav_container(stream)
        for count in (65535, 65536):
            with self.subTest(chunks=count):
                payload = self.good[12:] + body * (count - 2)
                self.path.write_bytes(b'RIFF' + struct.pack('<I', len(payload) + 4) + b'WAVE' + payload)
                self.assertEqual(sf.info(self.path).frames, 2400)
                self.assertEqual(validate_generated_audio(self.path), self.path)

    def test_inconsistent_data_declaration_preserves_prior_selected_publication(self):
        from tests import test_chunk_audio_save_failure as publication
        for storage in ('snapshot', 'journal'):
            with self.subTest(storage=storage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manager, prior, fresh = publication.ChunkAudioSaveFailureTests().create_project(root)
                if storage == 'journal': manager._get_status_journal_locked().ensure_writable()
                before, old_bytes = manager.load_chunks(), prior.read_bytes()
                raw = bytearray(fresh.read_bytes())
                self.assertEqual(raw[36:40], b'data')
                struct.pack_into('<I', raw, 40, 2 * (len(raw) - 44)); fresh.write_bytes(raw)
                with self.assertRaisesRegex(GeneratedAudioError, 'truncated WAV subchunk'):
                    manager._publish_generated_chunk_audio(before[0], str(fresh), 'NARRATOR')
                self.assertEqual(manager.load_chunks(), before)
                self.assertEqual(prior.read_bytes(), old_bytes)

    def test_short_riff_declaration_cannot_hide_data_from_container_validation(self):
        for extent in (12, 36):
            with self.subTest(extent=extent):
                raw = bytearray(self.good)
                struct.pack_into('<I', raw, 4, extent - 8)
                struct.pack_into('<I', raw, 40, 9600)
                self.path.write_bytes(raw)
                self.assertEqual(sf.info(self.path).frames, 2400)
                self.assertEqual(sf.read(self.path)[0].shape, (2400,))
                with self.assertRaisesRegex(GeneratedAudioError, 'inside its RIFF extent'):
                    validate_generated_audio(self.path)

    def test_python_wave_odd_unsigned_pcm_keeps_terminal_padding_compatibility(self):
        for frames in (2400, 2401):
            with self.subTest(frames=frames):
                with wave.open(str(self.path), 'wb') as stream:
                    stream.setparams((1, 1, 24000, 0, 'NONE', 'not compressed'))
                    stream.writeframes(b'\xa0' * frames)
                self.assertEqual(self.path.stat().st_size, 44 + frames)
                self.assertEqual(sf.info(self.path).frames, frames)
                self.assertEqual(validate_generated_audio(self.path, expected_text='Hello.'), self.path)
