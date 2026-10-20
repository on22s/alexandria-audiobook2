"""Bound native audio preparation and publish accepted stereo references as mono."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import librosa
import numpy as np
import soundfile as sf
import torch

import train_lora
from adapter_checkpoint_transaction import validate_adapter_checkpoint_generation
from dataset_metadata import get_training_dataset_preflight
from lora_evidence import get_file_sha256
from tests.test_support import write_test_adapter, apply_test_module_providers

class TrainingAudioAdmissionTests(unittest.TestCase):
    def dataset(self, root, sample_frames=2000, reference_frames=800):
        root.mkdir()
        sf.write(root / 'ref.wav', np.full(reference_frames, .1), 8000)
        sf.write(root / 'clip.wav', np.full(sample_frames, .2), 8000)
        (root / 'metadata.jsonl').write_text(json.dumps({'audio': 'clip.wav', 'text': 'Whole sample words.'}) + '\n')
        return root

    def prepare(self, root, limit=.25, decoder=None, mel_provider=None):
        encoder = Mock(return_value=torch.ones(1, 4))
        codec = Mock(return_value=SimpleNamespace(audio_codes=[torch.zeros(2, 16, dtype=torch.long)]))
        hf = SimpleNamespace(speaker_encoder=encoder, speech_tokenizer=SimpleNamespace(encode=codec))
        processor = Mock(return_value={'input_ids': torch.tensor([[1, 2, 3]])})
        mel = mel_provider if mel_provider is not None else Mock(return_value=torch.ones(1, 128, 4))
        original_load = librosa.load
        with apply_test_module_providers({'qwen_tts.core.models.modeling_qwen3_tts': SimpleNamespace(mel_spectrogram=mel)}), \
                patch.object(librosa, 'load', wraps=original_load) as load, contextlib.redirect_stdout(io.StringIO()):
            if decoder is not None:
                load.side_effect = decoder
            result = train_lora.load_dataset(str(root), hf, processor, 'cpu', torch.float32, limit)
        return result, load, encoder, codec

    def test_cold_native_registrations_survive_scoped_provider_then_real_pitch_analysis(self):
        import subprocess
        script = '''
import importlib, sys, tempfile, unittest
from pathlib import Path
from tests.test_training_audio_admission import TrainingAudioAdmissionTests, librosa
from tests.test_unvoiced_profile import UnvoicedProfileTests
case = TrainingAudioAdmissionTests()
loaded = []
with tempfile.TemporaryDirectory() as tmp:
    root = case.dataset(Path(tmp) / 'dataset')
    original_load = librosa.load
    def native_decoder(path, **kwargs):
        loaded.append(importlib.import_module('numba.core.boxing'))
        return original_load(path, **kwargs)
    prepared, _, _, _ = case.prepare(root, decoder=native_decoder)
    assert len(prepared[0]) == 1
assert sys.modules.get('numba.core.boxing') is loaded[0], 'provider restoration evicted native registrations'
assert importlib.import_module('numba.core.boxing') is loaded[0]
result = unittest.TextTestRunner().run(unittest.TestSuite([
    UnvoicedProfileTests('test_known_voiced_tone_remains_measured_and_near_its_frequency'),
    UnvoicedProfileTests('test_native_silence_artifact_and_csv_do_not_claim_male_or_bass')]))
raise SystemExit(not result.wasSuccessful())
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_cold_discovery_does_not_resolve_native_decoder(self):
        import subprocess
        script = '''
import importlib
from unittest.mock import patch
import librosa
assert 'load' not in librosa.__dict__, 'decoder is not cold before discovery'
original = librosa.__getattr__
def reject_decoder_resolution(name):
    if name == 'load':
        raise AssertionError('discovery resolved the native decoder')
    return original(name)
with patch.object(librosa, '__getattr__', side_effect=reject_decoder_resolution):
    importlib.import_module('tests.test_training_audio_admission')
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_oversized_sample_is_skipped_before_decoder_and_exact_boundary_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.dataset(Path(tmp) / 'dataset')
            sf.write(root / 'long.wav', np.full(2001, .3), 8000)
            with (root / 'metadata.jsonl').open('a') as handle:
                handle.write(json.dumps({'audio': 'long.wav', 'text': 'Must not tokenize.'}) + '\n')
            # Both uploaded/batch-compatible files are structurally valid.
            self.assertEqual(2, get_training_dataset_preflight(str(root))['sample_count'])
            original = {p.name: p.read_bytes() for p in root.iterdir()}
            decoded = []
            def read(path, **kwargs):
                decoded.append(Path(path).name)
                if Path(path).name == 'long.wav':
                    self.fail('oversized native WAV reached allocation')
                return original_load(path, **kwargs)
            original_load = librosa.load
            (samples, reference), load, encoder, codec = self.prepare(root, decoder=read)
            self.assertEqual(['ref.wav', 'clip.wav'], decoded)
            self.assertEqual(['Whole sample words.'], [sample['text'] for sample in samples])
            self.assertEqual(.25, samples[0]['duration'])
            self.assertEqual(str(root / 'ref.wav'), reference)
            encoder.assert_called_once()
            codec.assert_called_once()
            for call in load.call_args_list:
                self.assertGreater(call.kwargs.get('duration', 0), 0)
                self.assertLessEqual(call.kwargs['duration'], 1.25)
            self.assertEqual(original, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_oversized_reference_refuses_before_decoder_or_embedding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.dataset(Path(tmp) / 'dataset', reference_frames=4000)
            decoder = Mock(side_effect=AssertionError('oversized reference reached decoder'))
            with self.assertRaisesRegex(ValueError, 'Reference.*duration|Reference.*long'):
                self.prepare(root, decoder=decoder)
            decoder.assert_not_called()

    def test_reference_exact_native_limit_survives_resampling_frame_rounding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.dataset(Path(tmp) / 'dataset', sample_frames=100)
            sf.write(root / 'ref.wav', np.full(1000, .1), 44100)
            (samples, _), _, encoder, _ = self.prepare(root, limit=1000 / 44100)
            self.assertEqual(['Whole sample words.'], [sample['text'] for sample in samples])
            encoder.assert_called_once()

    def test_invalid_duration_limits_refuse_before_audio_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.dataset(Path(tmp) / 'dataset')
            for limit in (0, -.1, float('nan'), float('inf')):
                with self.subTest(limit=limit):
                    decoder = Mock(side_effect=AssertionError('invalid limit reached decoder'))
                    with self.assertRaisesRegex(ValueError, 'finite positive'):
                        self.prepare(root, limit=limit, decoder=decoder)
                    decoder.assert_not_called()

    def test_actual_decoder_bound_rejects_underreported_reference_without_embedding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.dataset(Path(tmp) / 'dataset', reference_frames=24000)
            frames = []
            original_load = librosa.load
            def read(path, **kwargs):
                data, rate = original_load(path, **kwargs)
                frames.append((len(data), rate))
                return data, rate
            with patch.object(train_lora, 'get_training_audio_duration', return_value=.1), \
                    patch.object(torch, 'from_numpy', side_effect=AssertionError('oversized reference reached embedding')):
                with self.assertRaisesRegex(ValueError, 'Reference.*duration'):
                    self.prepare(root, decoder=read)
            self.assertEqual([(10000, 8000)], frames, 'three seconds must decode only the bounded detection tail')

    def test_actual_decoder_bound_skips_underreported_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.dataset(Path(tmp) / 'dataset')
            sf.write(root / 'long.wav', np.full(24000, .3), 8000)
            with (root / 'metadata.jsonl').open('a') as handle:
                handle.write(json.dumps({'audio': 'long.wav', 'text': 'Must not tokenize.'}) + '\n')
            frames = []
            original_load = librosa.load
            def read(path, **kwargs):
                data, rate = original_load(path, **kwargs)
                if Path(path).name == 'long.wav':
                    frames.append((len(data), rate))
                return data, rate
            with patch.object(train_lora, 'get_training_audio_duration', return_value=.1):
                (samples, _), _, _, codec = self.prepare(root, decoder=read)
            self.assertEqual([(10000, 8000)], frames)
            self.assertEqual(['Whole sample words.'], [sample['text'] for sample in samples])
            codec.assert_called_once()

    def test_standalone_compressed_audio_keeps_native_duration_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.dataset(Path(tmp) / 'dataset')
            sf.write(root / 'clip.flac', np.full(2000, .2), 8000)
            sf.write(root / 'long.flac', np.full(4000, .3), 8000)
            (root / 'metadata.jsonl').write_text(''.join(json.dumps({'audio': name, 'text': name}) + '\n'
                for name in ('clip.flac', 'long.flac')))
            (samples, _), load, _, codec = self.prepare(root)
            self.assertEqual(['clip.flac'], [sample['text'] for sample in samples])
            self.assertEqual(['ref.wav', 'clip.flac'], [Path(call.args[0]).name for call in load.call_args_list])
            codec.assert_called_once()

    def test_same_audioread_fallback_supplies_duration_and_bounded_pcm(self):
        import audioread
        with tempfile.TemporaryDirectory() as tmp:
            root = self.dataset(Path(tmp) / 'dataset')
            opened = []
            class Reader:
                samplerate = 8000
                channels = 1
                def __init__(self, frames):
                    self.duration = frames / self.samplerate
                    self.frames = frames
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def __iter__(self):
                    yield np.full(self.frames, 1000, dtype='<i2').tobytes()
            def open_reader(path):
                opened.append(Path(path).name)
                return Reader(800 if Path(path).name == 'ref.wav' else 2000)
            class UnsupportedSoundFile:
                def __init__(self, *args, **kwargs):
                    raise sf.SoundFileRuntimeError('unsupported fixture codec')
            mel = Mock(return_value=torch.ones(1, 128, 4))
            with patch.object(sf, 'info', side_effect=sf.SoundFileRuntimeError('unsupported fixture codec')), \
                    patch.object(sf, 'SoundFile', UnsupportedSoundFile), \
                    patch.object(audioread, 'audio_open', side_effect=open_reader):
                (samples, _), load, _, codec = self.prepare(root, mel_provider=mel)
            self.assertEqual(['ref.wav', 'ref.wav', 'clip.wav', 'clip.wav'], opened)
            self.assertEqual(.25, samples[0]['duration'])
            self.assertTrue(all(call.kwargs.get('duration') == 1.25 for call in load.call_args_list))
            codec.assert_called_once()
            self.assertEqual((2000,), codec.call_args.args[0].shape)
            np.testing.assert_array_equal(codec.call_args.args[0], np.full(2000, 1000 / 32768, dtype=np.float32))
            self.assertEqual(8000, codec.call_args.kwargs['sr'])
            mel.assert_called_once()
            reference_pcm = mel.call_args.args[0].detach().cpu().numpy()
            expected = librosa.resample(np.full(800, 1000 / 32768, dtype=np.float32),
                                        orig_sr=8000, target_sr=24000)
            self.assertEqual((1, 2400), reference_pcm.shape)
            self.assertTrue(np.all(reference_pcm > 0))
            np.testing.assert_allclose(reference_pcm[0], expected, rtol=1e-6, atol=1e-7)
            self.assertEqual(24000, mel.call_args.kwargs['sampling_rate'])

    def test_stereo_reference_publishes_matching_mono_production_and_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.dataset(Path(tmp) / 'dataset')
            output = Path(tmp) / 'adapter'
            candidate = output / 'candidates' / 'epoch_001'
            write_test_adapter(output, value=1)
            write_test_adapter(candidate, value=2)
            values = np.column_stack((np.full(800, .1), np.full(800, .3)))
            reference = root / 'ref.wav'
            sf.write(reference, values, 8000, subtype='FLOAT')
            (root / 'ref_text.txt').write_text('Paired stereo reference words.')
            original = {p.name: p.read_bytes() for p in root.iterdir()}
            self.assertEqual('ref.wav', get_training_dataset_preflight(str(root))['reference'])
            (samples, path), _, _, _ = self.prepare(root)
            text = train_lora.get_training_reference_text(str(root), path, samples)
            records, _, _ = train_lora.save_training_checkpoint(None, str(output), path, text, {},
                [{'id': 'epoch_001', 'epoch': 1, 'loss': 4.5, 'path': str(candidate)}], 2, 2, 4.4)
            self.assertEqual(['epoch_001'], [record['id'] for record in records])
            for adapter in (output, candidate):
                meta = validate_adapter_checkpoint_generation(adapter)
                audio, rate = sf.read(adapter / 'ref_sample.wav')
                self.assertEqual((8000, (800,)), (rate, audio.shape))
                np.testing.assert_allclose(audio, values.mean(axis=1), atol=1e-7)
                self.assertEqual(text, meta['ref_sample_text'])
                self.assertEqual('Paired stereo reference words.', text)
                self.assertEqual(get_file_sha256(adapter / 'ref_sample.wav'), meta['reference_audio_sha256'])
            self.assertEqual((output / 'ref_sample.wav').read_bytes(), (candidate / 'ref_sample.wav').read_bytes())
            self.assertEqual(original, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_mono_reference_keeps_original_bytes_and_stereo_conversion_reads_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.dataset(Path(tmp) / 'dataset')
            output = Path(tmp) / 'adapter'
            write_test_adapter(output, value=1)
            reference = root / 'ref.wav'
            original = reference.read_bytes()
            train_lora.save_training_checkpoint(None, str(output), str(reference), 'Whole reference words.', {}, [], 0, 1, 4.5)
            self.assertEqual(original, (output / 'ref_sample.wav').read_bytes())
            sf.write(reference, np.column_stack((np.full(65700, .1), np.full(65700, .3))), 24000)
            original_read = sf.SoundFile.read
            reads = []
            def read(source, frames=-1, *args, **kwargs):
                reads.append(frames)
                return original_read(source, frames, *args, **kwargs)
            with patch.object(sf.SoundFile, 'read', autospec=True, side_effect=read):
                train_lora.save_training_checkpoint(None, str(output), str(reference), 'Whole reference words.', {}, [], 0, 2, 4.4)
            self.assertTrue(reads)
            self.assertTrue(all(0 < count <= 65536 for count in reads), reads)
            self.assertEqual((65700, 1, 24000), (sf.info(output / 'ref_sample.wav').frames,
                sf.info(output / 'ref_sample.wav').channels, sf.info(output / 'ref_sample.wav').samplerate))

    def test_stereo_first_sample_and_explicit_later_reference_keep_the_matching_transcript(self):
        for explicit_later in (False, True):
            with self.subTest(explicit_later=explicit_later), tempfile.TemporaryDirectory() as tmp:
                root = self.dataset(Path(tmp) / 'dataset')
                (root / 'ref.wav').unlink()
                rows = [{'audio': 'clip.wav', 'text': 'First sample words.'},
                        {'audio': 'later.wav', 'text': 'Later reference words.'}]
                if explicit_later:
                    rows[0]['ref_audio'] = 'later.wav'
                for index, name in enumerate(('clip.wav', 'later.wav')):
                    values = np.column_stack((np.full(2000, .1 + index * .1), np.full(2000, .3 + index * .1)))
                    sf.write(root / name, values, 8000, subtype='FLOAT')
                (root / 'metadata.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
                original = {p.name: p.read_bytes() for p in root.iterdir()}
                (samples, reference), _, _, _ = self.prepare(root)
                text = train_lora.get_training_reference_text(str(root), reference, samples)
                output = Path(tmp) / 'adapter'
                write_test_adapter(output, value=1)
                train_lora.save_training_checkpoint(None, str(output), reference, text, {}, [], 0, 1, 4.5)
                meta = validate_adapter_checkpoint_generation(output)
                self.assertEqual(rows[int(explicit_later)]['text'], meta['ref_sample_text'])
                audio, rate = sf.read(output / 'ref_sample.wav')
                self.assertEqual((8000, (2000,)), (rate, audio.shape))
                np.testing.assert_allclose(audio, .2 + int(explicit_later) * .1, atol=1e-7)
                self.assertEqual(original, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_invalid_stereo_reference_preserves_previous_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.dataset(Path(tmp) / 'dataset')
            output = Path(tmp) / 'adapter'
            write_test_adapter(output, value=1)
            reference = root / 'ref.wav'
            train_lora.save_training_checkpoint(None, str(output), str(reference), 'Original reference.', {}, [], 0, 1, 4.5)
            original = {str(p.relative_to(output)): p.read_bytes() for p in output.rglob('*') if p.is_file()}
            for kind in ('nonfinite', 'truncated'):
                with self.subTest(kind=kind):
                    values = np.full((800, 2), .1)
                    if kind == 'nonfinite':
                        values[-1, 1] = float('nan')
                    sf.write(reference, values, 8000, subtype='FLOAT')
                    if kind == 'truncated':
                        raw = reference.read_bytes()
                        reference.write_bytes(raw[:len(raw) // 2])
                    with self.assertRaises((ValueError, RuntimeError)):
                        train_lora.save_training_checkpoint(None, str(output), str(reference), 'Invalid new reference.', {}, [], 0, 2, 4.4)
                    self.assertEqual(original, {str(p.relative_to(output)): p.read_bytes() for p in output.rglob('*') if p.is_file()})
                    self.assertEqual('Original reference.', validate_adapter_checkpoint_generation(output)['ref_sample_text'])

    def test_stereo_source_change_between_admission_and_conversion_preserves_generation(self):
        import audio_validation
        import struct
        for kind in ('truncated_before_open', 'truncated_during_read', 'header_changed', 'rate_changed', 'channels_changed'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = self.dataset(Path(tmp) / 'dataset')
                output = Path(tmp) / 'adapter'
                reference = root / 'ref.wav'
                write_test_adapter(output, value=1)
                train_lora.save_training_checkpoint(None, str(output), str(reference), 'Original reference.', {}, [], 0, 1, 4.5)
                original = {str(p.relative_to(output)): p.read_bytes() for p in output.rglob('*') if p.is_file()}
                values = np.column_stack((np.full(65700, .1), np.full(65700, .3)))
                sf.write(reference, values, 24000, subtype='FLOAT')
                raw = reference.read_bytes()
                admitted = False
                changed = False
                validate = audio_validation.validate_generated_audio
                original_read = sf.SoundFile.read
                def validate_source(path, *args, **kwargs):
                    nonlocal admitted, changed
                    result = validate(path, *args, **kwargs)
                    if Path(path) == reference and not admitted:
                        admitted = True
                        if kind == 'truncated_before_open':
                            reference.write_bytes(raw[:len(raw) // 2])
                        elif kind == 'header_changed':
                            reference.write_bytes(raw[:4] + struct.pack('<I', len(raw) + 100) + raw[8:])
                        elif kind == 'rate_changed':
                            sf.write(reference, values, 16000, subtype='FLOAT')
                        elif kind == 'channels_changed':
                            sf.write(reference, values[:, 0], 24000, subtype='FLOAT')
                        changed = kind != 'truncated_during_read'
                    return result
                def read(source, frames=-1, *args, **kwargs):
                    nonlocal changed
                    block = original_read(source, frames, *args, **kwargs)
                    if admitted and not changed and Path(source.name) == reference:
                        reference.write_bytes(raw[:len(raw) // 2])
                        changed = True
                    return block
                with patch.object(audio_validation, 'validate_generated_audio', side_effect=validate_source), \
                        patch.object(sf.SoundFile, 'read', autospec=True, side_effect=read):
                    with self.assertRaisesRegex((ValueError, RuntimeError), 'changed|truncated|incomplete'):
                        train_lora.save_training_checkpoint(None, str(output), str(reference), 'Whole changed reference words.', {}, [], 0, 2, 4.4)
                self.assertTrue(changed, 'fault must run at the real validation/decode boundary')
                self.assertEqual(original, {str(p.relative_to(output)): p.read_bytes() for p in output.rglob('*') if p.is_file()})
                self.assertEqual('Original reference.', validate_adapter_checkpoint_generation(output)['ref_sample_text'])
