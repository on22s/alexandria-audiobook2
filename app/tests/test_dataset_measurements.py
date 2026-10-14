"""Native audit identity and completed training counts, without model inference."""
from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import soundfile as sf

import batch_adapter_registration as registration
from lora_evidence import get_file_sha256
from task_ownership import acquire_task_slot
from tests.test_support import write_test_adapter
from tests.test_voice_dataset_quality import quality
from tests.test_voicelab_pipeline_scripts import batch_train
from voice_dataset_merge import get_pcm_hash


APP = Path(__file__).resolve().parents[1]


def get_test_wav(audio, rate=16000, subtype='FLOAT'):
    output = io.BytesIO()
    sf.write(output, audio, rate, format='WAV', subtype=subtype)
    return output.getvalue()


class DatasetAuditIdentityTests(unittest.TestCase):
    def setUp(self):
        self.samples = np.tile(np.array([0, .25, -.25, .125], dtype=np.float32), 600)

    def get_reports(self, root, waveforms):
        reports = []
        for index, waveform in enumerate(waveforms):
            path = root / f'{index}.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('train/clip.wav', waveform)
            original = path.read_bytes()
            reports.append(quality.audit_zip(path, quality.get_file_fingerprint(path)))
            self.assertEqual(original, path.read_bytes())
        return reports

    def test_different_rates_and_original_channel_shapes_do_not_group(self):
        stereo = np.column_stack((self.samples, self.samples))
        with tempfile.TemporaryDirectory() as tmp:
            reports = self.get_reports(Path(tmp), [get_test_wav(self.samples),
                get_test_wav(self.samples, 24000), get_test_wav(stereo)])
            self.assertEqual([], quality.build_summary(reports)['exact_duplicate_groups'])
            self.assertEqual([.15, .1, .15],
                             [report['clips'][0]['metrics']['duration_seconds'] for report in reports])
            self.assertEqual([1, 1, 2],
                             [report['clips'][0]['metrics']['channels'] for report in reports])

    def test_distinct_stereo_channels_with_identical_mono_metrics_do_not_group(self):
        a = np.column_stack((self.samples + .0625, self.samples - .0625))
        b = np.column_stack((self.samples + .125, self.samples - .125))
        np.testing.assert_array_equal(a.mean(axis=1), b.mean(axis=1))
        with tempfile.TemporaryDirectory() as tmp:
            reports = self.get_reports(Path(tmp), [get_test_wav(a), get_test_wav(b)])
            self.assertEqual(reports[0]['clips'][0]['metrics'], reports[1]['clips'][0]['metrics'])
            self.assertEqual([], quality.build_summary(reports)['exact_duplicate_groups'])

    def test_genuine_decoded_duplicates_group_across_encodings_and_train_val(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = get_test_wav(self.samples), get_test_wav(self.samples, subtype='PCM_16')
            self.assertNotEqual(a, b)
            reports = self.get_reports(root, [a, b])
            with zipfile.ZipFile(root / '2.zip', 'w') as archive:
                archive.writestr('val/held.wav', a)
            reports.append(quality.audit_zip(root / '2.zip', quality.get_file_fingerprint(root / '2.zip')))
            groups = quality.build_summary(reports)['exact_duplicate_groups']
            self.assertEqual(1, len(groups))
            self.assertEqual(3, len(groups[0]))
            self.assertEqual({'train/clip.wav', 'val/held.wav'}, {row['path'] for row in groups[0]})

    def test_audit_decodes_once_and_retains_exact_merger_digest(self):
        for audio, rate in ((self.samples, 16000),
                            (np.column_stack((self.samples, -self.samples)), 24000)):
            with self.subTest(rate=rate, shape=audio.shape):
                waveform = get_test_wav(audio, rate)
                decoded, actual_rate = sf.read(io.BytesIO(waveform), dtype='float32', always_2d=True)
                expected = hashlib.sha256(
                    f'{actual_rate}:{decoded.shape[0]}:{decoded.shape[1]}:'.encode('ascii')
                    + np.asarray(decoded, dtype='<f4').tobytes()).hexdigest()
                with patch.object(quality.sf, 'read', wraps=quality.sf.read) as read:
                    _metrics, digest = quality.get_clip_metrics(waveform)
                self.assertEqual(1, read.call_count)
                self.assertEqual(expected, digest)
                self.assertEqual(expected, get_pcm_hash(waveform))

    def test_cli_rebuilds_v1_mono_digest_cache_then_reuses_corrected_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reader = root / 'reader'
            reader.mkdir()
            archive_path = reader / 'clips.zip'
            with zipfile.ZipFile(archive_path, 'w') as archive:
                archive.writestr('train/a.wav', get_test_wav(self.samples))
                archive.writestr('val/b.wav', get_test_wav(self.samples, 24000))
            original = archive_path.read_bytes()
            fingerprint = quality.get_file_fingerprint(archive_path)
            old = quality.audit_zip(archive_path, fingerprint)
            old['version'] = 1
            old['clips'][1]['pcm_sha256'] = old['clips'][0]['pcm_sha256']
            before = copy.deepcopy(old)
            self.assertFalse(quality.is_reusable_report(old, fingerprint))
            self.assertEqual(before, old)
            output = root / '_quality'
            output.mkdir()
            cached = output / (hashlib.sha256(b'reader/clips.zip').hexdigest()[:16] + '.json')
            cached.write_text(json.dumps(old))
            with patch.object(sys, 'argv', ['audit', '--zips2', str(root)]), \
                 redirect_stdout(io.StringIO()), \
                 patch.object(quality, 'audit_zip', wraps=quality.audit_zip) as audit:
                self.assertEqual(0, quality.main())
                self.assertEqual(1, audit.call_count)
                self.assertEqual([], json.loads((output / 'summary.json').read_text())['exact_duplicate_groups'])
                corrected = cached.read_bytes()
                self.assertEqual(2, json.loads(corrected)['version'])
                self.assertEqual(0, quality.main())
                self.assertEqual(1, audit.call_count)
                self.assertEqual(corrected, cached.read_bytes())
            self.assertEqual(original, archive_path.read_bytes())


class BatchPreparedCountTests(unittest.TestCase):
    def get_fixture(self, root, *, split=True):
        good = {'audio_filepath': 'train/good.wav' if split else 'good.wav', 'text': 'Kept words.'}
        train = [good]
        if split:
            train += [{'audio_filepath': 'train/missing.wav', 'text': 'Missing words.'},
                      {'audio_filepath': 'train/long.wav', 'text': 'Long words.'}]
            val = [{'audio_filepath': 'val/held.wav', 'text': 'Held words.'}]
        else:
            train += [{'audio_filepath': 'second.wav', 'text': 'Second words.'}]
            val = []
        archive_path = root / 'reader.zip'
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr('metadata.jsonl', ''.join(json.dumps(row) + '\n' for row in train + val))
            if split:
                archive.writestr('train/metadata.jsonl', ''.join(json.dumps(row) + '\n' for row in train))
                archive.writestr('val/metadata.jsonl', ''.join(json.dumps(row) + '\n' for row in val))
            for row in train + val:
                path = row['audio_filepath']
                if 'missing' not in path:
                    frames = 32000 if 'long' in path else 2400
                    archive.writestr(path, get_test_wav(np.full(frames, .125, dtype=np.float32)))
        args = SimpleNamespace(datasets_dir=str(root / 'datasets'), models_dir=str(root / 'models'),
            manifest=str(root / 'models/manifest.json'), python=sys.executable,
            train_script=str(root / 'cpu_worker.py'), max_epochs=1, lr=1e-6,
            lora_r=2, lora_alpha=4, grad_accum=1, language='english', target_loss=4,
            keep_datasets=False, device='cpu')
        return archive_path, args, len(train + val)

    def save_native_worker(self, script):
        script.write_text(f'''import argparse,json,pathlib,sys
from types import SimpleNamespace
sys.path.insert(0, {str(APP)!r})
import numpy as np,torch
import train_lora
from dataset_metadata import get_training_reference_text
from lora_evidence import get_file_sha256
from tests.test_support import write_test_adapter
p=argparse.ArgumentParser();p.add_argument('--data_dir');p.add_argument('--output_dir');p.add_argument('--epochs',type=int)
args,_=p.parse_known_args()
# Only model providers are synthetic; metadata and native decoding are real.
provider='qwen_tts.core.models.modeling_qwen3_tts'
previous=sys.modules.get(provider)
sys.modules[provider]=SimpleNamespace(mel_spectrogram=lambda *a,**k:torch.ones(1,128,2))
hf=SimpleNamespace(speaker_encoder=lambda value:torch.ones(1,4),
    speech_tokenizer=SimpleNamespace(encode=lambda audio,sr:SimpleNamespace(audio_codes=[torch.ones(2,16,dtype=torch.long)])))
processor=lambda **k:{{'input_ids':torch.ones(1,4,dtype=torch.long)}}
try:
    samples,reference=train_lora.load_dataset(args.data_dir,hf,processor,'cpu',torch.float32,1)
finally:
    if previous is None:sys.modules.pop(provider,None)
    else:sys.modules[provider]=previous
text=get_training_reference_text(args.data_dir,reference,samples)
settings=SimpleNamespace(model_name='synthetic-cpu',epochs=1,lr=1e-6,lora_r=2,lora_alpha=4,
    gradient_accumulation_steps=1,batch_size=1,language='english')
metadata=train_lora.get_training_checkpoint_metadata(settings,samples,reference,text,1,2.5,2.5,0,0)
output=pathlib.Path(args.output_dir);write_test_adapter(output)
metadata['checkpoint_sha256']=get_file_sha256(str(output/'adapter_model.safetensors'))
(output/'training_meta.json').write_text(json.dumps(metadata))
print('[EPOCH] 1/1 avg_loss=2.5000',flush=True)
''')

    def run_native_training(self, root, *, split=True):
        source, args, metadata_count = self.get_fixture(root, split=split)
        self.save_native_worker(Path(args.train_script))
        original = source.read_bytes()
        models = Path(args.models_dir)
        models.mkdir()
        lease_dir = root / 'leases'
        lease_dir.mkdir()
        with acquire_task_slot(lease_dir, 'count') as lease:
            args._batch_task_lease_fd = lease.fileno()
            logs = io.StringIO()
            with redirect_stdout(logs):
                row = batch_train.train_one(str(source), 'reader', 'reader_100', args)
            self.assertIsNotNone(row, logs.getvalue())
        with acquire_task_slot(lease_dir, 'count'):
            pass
        metadata = json.loads((models / 'reader_100/training_meta.json').read_text())
        self.assertEqual(metadata['num_samples'], row['sample_count'])
        self.assertEqual(metadata_count, row['metadata_count'])
        self.assertEqual(original, source.read_bytes())
        self.assertFalse((root / 'datasets/reader').exists())
        batch_train.save_manifest(args.manifest, [row])
        self.assertEqual(row['sample_count'], batch_train.load_manifest(args.manifest)[0]['sample_count'])
        return source, args, row, logs.getvalue()

    def test_native_split_and_skips_publish_actual_prepared_count_and_recover_same_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, args, row, logs = self.run_native_training(root)
            self.assertEqual(1, row['sample_count'])
            self.assertEqual(4, row['metadata_count'])
            self.assertIn('Prepared 1 samples (2 skipped: 1 missing, 1 too long)', logs)
            self.assertIn('1 held out in val/ and NOT trained on', logs)
            registration.apply_batch_registration_intent(args.models_dir, 'reader', 'reader_100', str(source),
                {'lora_r': 2, 'lr': 1e-6, 'target_loss': 4})
            recovered = registration.get_batch_registration_entry(args.models_dir, 'reader', str(source))
            self.assertEqual(row['sample_count'], recovered['sample_count'])
            frozen = registration.save_batch_registration_entry(args.models_dir, 'reader', row)
            self.assertEqual(row['sample_count'], frozen['sample_count'])
            self.assertEqual(json.loads(json.dumps(row)),
                             registration.get_batch_registration_entry(args.models_dir, 'reader', str(source)))

    def test_legacy_root_only_native_preparation_counts_all_actual_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            _source, _args, row, logs = self.run_native_training(Path(tmp), split=False)
            self.assertEqual(2, row['sample_count'])
            self.assertEqual(2, row['metadata_count'])
            self.assertIn('Prepared 2 samples (0 skipped', logs)

    def test_missing_or_invalid_completed_count_refuses_before_result_or_manifest(self):
        for count in (None, 0, -1, True, 1.5, '1'):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, args, _count = self.get_fixture(root, split=False)
                output = root / 'models/reader_100'
                class Process:
                    def __init__(self, command, **kwargs):
                        write_test_adapter(output)
                        metadata = json.loads((output / 'training_meta.json').read_text())
                        if count is None:
                            metadata.pop('num_samples')
                        else:
                            metadata['num_samples'] = count
                        (output / 'training_meta.json').write_text(json.dumps(metadata))
                        self.stdout = io.StringIO('[EPOCH] 1/1 avg_loss=2.5000\n')
                        self.returncode = 0
                    def wait(self): return 0
                    def poll(self): return 0
                logs = io.StringIO()
                with patch.object(batch_train.subprocess, 'Popen', Process), redirect_stdout(logs):
                    self.assertIsNone(batch_train.train_one(str(source), 'reader', 'reader_100', args))
                self.assertIn('positive prepared sample count', logs.getvalue())
                self.assertFalse(output.exists())
                self.assertFalse(Path(args.manifest).exists())
                self.assertTrue(source.exists())
                write_test_adapter(output)
                metadata_path = output / 'training_meta.json'
                metadata = json.loads(metadata_path.read_text())
                metadata['checkpoint_sha256'] = get_file_sha256(str(output / 'adapter_model.safetensors'))
                if count is None:
                    metadata.pop('num_samples')
                else:
                    metadata['num_samples'] = count
                metadata_path.write_text(json.dumps(metadata))
                registration.apply_batch_registration_intent(args.models_dir, 'reader', 'reader_100', str(source), {})
                snapshot = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
                with self.assertRaisesRegex(ValueError, 'positive prepared sample count'):
                    registration.get_batch_registration_entry(args.models_dir, 'reader', str(source))
                self.assertEqual(snapshot,
                                 {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})
                self.assertFalse(Path(args.manifest).exists())

    def test_existing_frozen_legacy_counts_are_preserved_without_rewriting_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, args, _count = self.get_fixture(root, split=False)
            output = root / 'models/reader_100'
            write_test_adapter(output)
            digest = get_file_sha256(str(output / 'adapter_model.safetensors'))
            metadata_path = output / 'training_meta.json'
            metadata = json.loads(metadata_path.read_text())
            metadata['checkpoint_sha256'] = digest
            metadata_path.write_text(json.dumps(metadata))
            registration.apply_batch_registration_intent(args.models_dir, 'reader', 'reader_100', str(source), {})
            old = {'id': 'reader_100', 'dataset_id': 'reader', 'zip_source': str(source),
                   'sample_count': 200, 'checkpoint_sha256': digest}
            registration.save_batch_registration_entry(args.models_dir, 'reader', old)
            snapshot = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(old, registration.get_batch_registration_entry(args.models_dir, 'reader', str(source)))
            self.assertEqual(snapshot, {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})


if __name__ == '__main__':
    unittest.main()
