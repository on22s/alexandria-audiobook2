"""Native dataset and serving artifacts keep one audio/transcript provenance."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import soundfile as sf

from dataset_metadata import (get_training_metadata, get_training_reference_path,
                              get_training_reference_text)
from tests.test_preparer_run_state import preparer
from tests.test_support import write_test_adapter, assert_file_lock_released
from tests.test_voicelab_pipeline_scripts import batch_train, voice_profiler
from voice_dataset_merge import merge_voice_datasets, get_source_records
import voice_dataset_merge


def wav_bytes(frequency):
    stream = io.BytesIO()
    sf.write(stream, .2 * np.sin(np.arange(2400) * frequency), 24000,
             format='WAV', subtype='PCM_16')
    return stream.getvalue()


def write_zip(path, rows, files):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('metadata.jsonl', ''.join(json.dumps(row) + '\n' for row in rows))
        for name, content in files.items():
            archive.writestr(name, content)


def training_pair(archive, folder):
    batch_train.extract_zip(str(archive), str(folder))
    rows, _ = get_training_metadata(str(folder))
    reference = get_training_reference_path(str(folder), rows)
    samples = [{**row, 'audio_path': str(folder / row['audio_filepath'])} for row in rows]
    return Path(reference).read_bytes(), get_training_reference_text(str(folder), reference, samples)


class MergedReferenceProvenanceTests(unittest.TestCase):
    def test_opened_substitute_archive_cannot_claim_the_restored_admitted_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, aside, merged = root/'source.zip', root/'original.zip', root/'merged.zip'
            original, changed = wav_bytes(.07), wav_bytes(.11)
            rows = [{'audio_filepath':'train/a.wav', 'text':'Original words.'}]
            files = {'train/a.wav':original, 'ref_text.txt':'Original words.'}
            write_zip(source, rows, files)
            write_zip(merged, rows, files)
            fingerprint, prior = get_source_records([source]), merged.read_bytes()
            constructor = zipfile.ZipFile.__init__
            def open_substitute(archive, path, mode='r', *args, **kwargs):
                if Path(path) != source or mode != 'r':
                    return constructor(archive, path, mode, *args, **kwargs)
                source.rename(aside)
                try:
                    write_zip(source, [{**rows[0], 'text':'Changed words.'}],
                              {'train/a.wav':changed, 'ref_text.txt':'Changed words.'})
                    constructor(archive, path, mode, *args, **kwargs)
                finally:
                    source.unlink()
                    aside.rename(source)
            with patch.object(zipfile.ZipFile, '__init__', open_substitute):
                try:
                    merge_voice_datasets([source], merged)
                except ValueError:
                    self.assertEqual(prior, merged.read_bytes())
            self.assertEqual(fingerprint, get_source_records([source]))
            self.assertEqual((original, 'Original words.'), training_pair(merged, root/'extracted'))
            self.assertFalse(list(root.glob('.merged.zip.*.tmp')))

    def test_many_sources_retain_only_two_candidate_owners_and_close_every_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_zip(root/'00.zip', [{'audio_filepath':'val/a.wav', 'text':'Root words.'}],
                      {'val/a.wav':wav_bytes(.03), 'ref.wav':wav_bytes(.03), 'ref_text.txt':'Root words.'})
            for index in range(1, 16):
                write_zip(root/f'{index:02}.zip', [{'audio_filepath':'train/a.wav', 'text':f'Train {index}.'}],
                          {'train/a.wav':wav_bytes(.07 + index*.01)})
            constructor = zipfile.ZipFile.__init__
            opened, peak = [], [0]
            def track_open(archive, *args, **kwargs):
                constructor(archive, *args, **kwargs)
                opened.append(archive)
                peak[0] = max(peak[0], sum(item.fp is not None for item in opened))
            with patch.object(zipfile.ZipFile, '__init__', track_open):
                merge_voice_datasets(list(root.glob('*.zip')), root/'merged.zip')
            self.assertGreaterEqual(len(opened), 17)
            self.assertLessEqual(peak[0], 4, 'two candidate owners, active source and output only')
            self.assertTrue(all(archive.fp is None for archive in opened))
            self.assertEqual((wav_bytes(.03), 'Root words.'), training_pair(root/'merged.zip', root/'extracted'))

    def test_renamed_source_rollback_cannot_substitute_reference_or_transcript(self):
        for reference_policy in ('explicit', 'root', 'first_sample'):
            with self.subTest(reference_policy=reference_policy), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, aside, merged = root/'source.zip', root/'original.zip', root/'merged.zip'
                original, changed = wav_bytes(.07), wav_bytes(.11)
                row = {'audio_filepath':'train/a.wav', 'text':'Original words.'}
                if reference_policy == 'explicit':
                    row['ref_audio'] = 'train/a.wav'
                files = {'train/a.wav':original, 'ref_text.txt':'Original words.'}
                changed_files = {'train/a.wav':changed, 'ref_text.txt':'Changed words.'}
                if reference_policy == 'root':
                    files['ref.wav'] = original
                    changed_files['ref.wav'] = changed
                write_zip(source, [row], files)
                fingerprint = get_source_records([source])
                get_pair = voice_dataset_merge._get_reference_pair
                def replace_then_rollback(*args):
                    source.rename(aside)
                    try:
                        write_zip(source, [{**row, 'text':'Changed words.'}], changed_files)
                        return get_pair(*args)
                    finally:
                        source.unlink()
                        aside.rename(source)
                with patch.object(voice_dataset_merge, '_get_reference_pair', side_effect=replace_then_rollback):
                    self.assertEqual('merged', merge_voice_datasets([source], merged)['status'])
                self.assertEqual(fingerprint, get_source_records([source]), 'path fingerprint cannot detect this rollback')
                self.assertEqual((original, 'Original words.'), training_pair(merged, root/'extracted'))

    def test_replaced_source_between_sample_copy_and_pair_selection_preserves_prior_archive(self):
        for changed_audio in (False, True):
            with self.subTest(changed_audio=changed_audio), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, merged = root/'source.zip', root/'merged.zip'
                original = wav_bytes(.07)
                rows = [{'audio_filepath':'train/a.wav', 'text':'Original words.', 'ref_audio':'train/a.wav'}]
                files = {'train/a.wav':original, 'ref_text.txt':'Original words.'}
                write_zip(source, rows, files)
                write_zip(merged, rows, files)
                prior = merged.read_bytes()
                get_pair = voice_dataset_merge._get_reference_pair
                def replace_source(*args):
                    write_zip(source, [{**rows[0], 'text':'Changed words.'}],
                              {'train/a.wav':wav_bytes(.11) if changed_audio else original,
                               'ref_text.txt':'Changed words.'})
                    return get_pair(*args)
                with patch.object(voice_dataset_merge, '_get_reference_pair', side_effect=replace_source):
                    with self.assertRaisesRegex(ValueError, 'source archive changed'):
                        merge_voice_datasets([source], merged)
                self.assertEqual(prior, merged.read_bytes())
                self.assertEqual((original, 'Original words.'), training_pair(merged, root/'extracted'))
                self.assertFalse(list(root.glob('.merged.zip.*.tmp')))

    def test_missing_optional_text_never_borrows_another_archive_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = wav_bytes(.07), wav_bytes(.11)
            write_zip(root/'a.zip', [{'audio_filepath':'train/a.wav', 'text':'First source words.'}],
                      {'train/a.wav':first, 'ref.wav':first})
            write_zip(root/'b.zip', [{'audio_filepath':'train/b.wav', 'text':'Second source words.'}],
                      {'train/b.wav':second, 'ref.wav':second, 'ref_text.txt':'Second source words.'})
            merge_voice_datasets([root/'b.zip', root/'a.zip'], root/'merged.zip')
            self.assertEqual((first, 'First source words.'), training_pair(root/'merged.zip', root/'extracted'))

    def test_explicit_first_train_reference_owns_text_including_later_sample(self):
        for explicit in ('train/later.wav', 'reference.wav'):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                first, later = wav_bytes(.07), wav_bytes(.11)
                write_zip(root/'a.zip', [
                    {'audio_filepath':'train/first.wav', 'text':'First words.', 'ref_audio':explicit},
                    {'audio_filepath':'train/later.wav', 'text':'Actual reference words.'}],
                    {'train/first.wav':first, 'train/later.wav':later, 'reference.wav':later, 'ref.wav':first})
                merge_voice_datasets([root/'a.zip'], root/'merged.zip')
                self.assertEqual((later, 'Actual reference words.'), training_pair(root/'merged.zip', root/'extracted'))

    def test_rootless_fallback_and_validation_first_do_not_borrow_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            validation, first, second = [wav_bytes(x) for x in (.03, .07, .11)]
            write_zip(root/'a.zip', [
                {'audio_filepath':'val/v.wav', 'text':'Validation words.'},
                {'audio_filepath':'train/a.wav', 'text':'First training words.'}],
                {'val/v.wav':validation, 'train/a.wav':first})
            write_zip(root/'b.zip', [{'audio_filepath':'train/b.wav', 'text':'Second words.'}],
                      {'train/b.wav':second, 'ref_text.txt':'Second words.'})
            merge_voice_datasets([root/'a.zip', root/'b.zip'], root/'merged.zip')
            self.assertEqual((first, 'First training words.'), training_pair(root/'merged.zip', root/'extracted'))

    def test_first_training_explicit_reference_overrides_an_earlier_validation_root_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            validation, training = wav_bytes(.03), wav_bytes(.07)
            write_zip(root/'a.zip', [{'audio_filepath':'val/v.wav', 'text':'Validation words.'}],
                      {'val/v.wav':validation, 'ref.wav':validation, 'ref_text.txt':'Validation words.'})
            write_zip(root/'b.zip', [{'audio_filepath':'train/b.wav', 'text':'Training words.',
                                     'ref_audio':'train/b.wav'}],
                      {'train/b.wav':training, 'ref_text.txt':'Explicit training reference words.'})
            merge_voice_datasets([root/'a.zip', root/'b.zip'], root/'merged.zip')
            self.assertEqual((training, 'Explicit training reference words.'),
                             training_pair(root/'merged.zip', root/'extracted'))

    def test_unmatched_reference_without_own_text_or_training_fallback_refuses_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_zip(root/'a.zip', [{'audio_filepath':'val/v.wav', 'text':'Validation words.'}],
                      {'val/v.wav':wav_bytes(.03), 'ref.wav':wav_bytes(.07)})
            write_zip(root/'b.zip', [{'audio_filepath':'train/b.wav', 'text':'Training words.'}],
                      {'train/b.wav':wav_bytes(.11)})
            merged = root/'merged.zip'
            merged.write_bytes(b'prior archive')
            with self.assertRaisesRegex(ValueError, 'source-owned transcript'):
                merge_voice_datasets([root/'a.zip', root/'b.zip'], merged)
            self.assertEqual(b'prior archive', merged.read_bytes())
            self.assertFalse(list(root.glob('.merged.zip.*.tmp')))

    def test_root_pair_from_validation_only_owner_retains_its_explicit_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference = wav_bytes(.07)
            write_zip(root/'a.zip', [{'audio_filepath':'val/v.wav', 'text':'Other validation words.'}],
                      {'val/v.wav':wav_bytes(.03), 'ref.wav':reference, 'ref_text.txt':'Standalone reference words.'})
            write_zip(root/'b.zip', [{'audio_filepath':'train/b.wav', 'text':'Training words.'}],
                      {'train/b.wav':wav_bytes(.11), 'ref_text.txt':'Other training words.'})
            merge_voice_datasets([root/'a.zip', root/'b.zip'], root/'merged.zip')
            self.assertEqual((reference, 'Standalone reference words.'),
                             training_pair(root/'merged.zip', root/'extracted'))

    def test_matching_pcm_with_different_wav_encoding_derives_the_later_owned_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, later = wav_bytes(.03), wav_bytes(.07)
            decoded, rate = sf.read(io.BytesIO(later), dtype='float32')
            reencoded = io.BytesIO()
            sf.write(reencoded, decoded, rate, format='WAV', subtype='FLOAT')
            reference = reencoded.getvalue()
            self.assertNotEqual(later, reference)
            write_zip(root/'a.zip', [{'audio_filepath':'train/first.wav', 'text':'First words.'},
                                   {'audio_filepath':'train/later.wav', 'text':'Later reference words.'}],
                      {'train/first.wav':first, 'train/later.wav':later, 'ref.wav':reference})
            merge_voice_datasets([root/'a.zip'], root/'merged.zip')
            self.assertEqual((reference, 'Later reference words.'),
                             training_pair(root/'merged.zip', root/'extracted'))

    def test_source_local_explicit_text_and_legacy_unmatched_reference_remain_supported(self):
        for explicit_text in (None, 'Known standalone reference words.'):
            with self.subTest(explicit_text=explicit_text), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                first, reference = wav_bytes(.07), wav_bytes(.11)
                files = {'train/a.wav':first, 'ref.wav':reference}
                if explicit_text:
                    files['ref_text.txt'] = explicit_text
                write_zip(root/'a.zip', [{'audio_filepath':'train/a.wav', 'text':'Legacy first words.'}], files)
                merge_voice_datasets([root/'a.zip'], root/'merged.zip')
                self.assertEqual((reference, explicit_text or 'Legacy first words.'),
                                 training_pair(root/'merged.zip', root/'extracted'))

    def test_v6_mispaired_cache_is_rebuilt_then_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = wav_bytes(.07)
            source, merged = root/'a.zip', root/'merged.zip'
            rows = [{'audio_filepath':'train/a.wav', 'text':'Correct words.'}]
            write_zip(source, rows, {'train/a.wav':first, 'ref.wav':first})
            write_zip(merged, rows, {'train/a.wav':first, 'ref.wav':first, 'ref_text.txt':'Wrong foreign words.',
                'merge_manifest.json':json.dumps({'version':6, 'sources':get_source_records([source])})})
            self.assertEqual('merged', merge_voice_datasets([source], merged)['status'])
            self.assertEqual((first, 'Correct words.'), training_pair(merged, root/'extracted'))
            self.assertEqual('reused', merge_voice_datasets([source], merged)['status'])


class ProfilerServingReferenceTests(unittest.TestCase):
    def make_adapter(self, root, name='voice', *, declared_hashes=True):
        import train_lora
        adapter = root/name
        write_test_adapter(adapter)
        reference = root/'source.wav'
        reference.write_bytes(wav_bytes(.07))
        train_lora.save_training_checkpoint(None, str(adapter), str(reference), 'Served reference words.',
            {'best_loss':2.5, 'final_loss':2.5}, [], 0, 1, 2.5)
        if not declared_hashes:
            metadata = json.loads((adapter/'training_meta.json').read_text())
            for field in ('checkpoint_sha256', 'reference_audio_sha256'):
                metadata.pop(field)
            (adapter/'training_meta.json').write_text(json.dumps(metadata))
        return adapter

    def run_profiler(self, root, rows, *, dry=False):
        manifest = root/'manifest.json'
        manifest.write_text(json.dumps(rows), encoding='utf-8')
        model, output = root/'model.gguf', root/'profiles.csv'
        model.write_bytes(b'CPU provider stand-in')
        output.write_bytes(b'prior CSV')
        argv = ['profiler', '--manifest', str(manifest), '--model', str(model),
                '--output_csv', str(output), '--device', 'cpu']
        if dry:
            argv.append('--dry_run')
        features = dict(mean_f0=120., std_f0=20., mean_rms=.1, speaking_rate=2.,
                        mean_centroid=1000., mean_rolloff=2000., smoothness=.5, flatness=.1)
        def analyze_after_admission(wav):
            assert_file_lock_released(str(root/'manifest.json'))
            for row in rows:
                if row.get('adapter_path'):
                    assert_file_lock_released(str(Path(row['adapter_path']).parent/'manifest.json'))
            return features
        with patch.object(sys, 'argv', argv), \
             patch.dict(sys.modules, {'llama_cpp':SimpleNamespace(Llama=lambda **kw: object())}), \
             patch.object(voice_profiler, 'analyze_ref_wav', side_effect=analyze_after_admission) as acoustic, \
             patch.object(voice_profiler, 'llm_describe', return_value='Known measured voice.') as describe, \
             patch.object(voice_profiler, 'find_epub', return_value=None), \
             contextlib.redirect_stdout(io.StringIO()) as logs:
            rc = voice_profiler.main()
        return rc, acoustic, describe, logs.getvalue(), json.loads(manifest.read_text())

    def test_actual_preparer_training_readers_checkpoint_and_profiler_full_and_dry(self):
        import train_lora
        for dry in (False, True):
            with self.subTest(dry=dry), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                work = root/'dataset_temp'
                work.mkdir()
                audio = wav_bytes(.07)
                (work/'sample_0000.wav').write_bytes(audio)
                rows = [{'audio_filepath':'sample_0000.wav', 'speaker':'UNKNOWN',
                         'speaker_labels':['UNKNOWN'], 'duration':.1, 'text':'Native producer words.'}]
                previous = Path.cwd()
                try:
                    os.chdir(root)
                    preparer._create_zip_dataset(rows, str(root/'dataset.zip'), val_split=0)
                finally:
                    os.chdir(previous)
                with zipfile.ZipFile(root/'dataset.zip') as archive:
                    self.assertNotIn('ref.wav', archive.namelist())
                    self.assertNotIn('ref_text.txt', archive.namelist())
                reference, text = training_pair(root/'dataset.zip', root/'extracted')
                self.assertEqual((audio, 'Native producer words.'), (reference, text))
                args = SimpleNamespace(datasets_dir=str(root/'datasets'), models_dir=str(root/'separate_models'),
                    manifest=str(root/'manifest.json'), python=sys.executable, train_script='CPU-provider-fixture',
                    max_epochs=1, lr=1e-6, lora_r=2, lora_alpha=4, grad_accum=1,
                    language='English', target_loss=2.5, device='cpu', keep_datasets=True)
                def native_checkpoint_provider(command, **kwargs):
                    data = command[command.index('--data_dir') + 1]
                    output = command[command.index('--output_dir') + 1]
                    train_rows, _ = get_training_metadata(data)
                    ref_path = get_training_reference_path(data, train_rows)
                    samples = [{**row, 'audio_path':str(Path(data)/row['audio_filepath'])} for row in train_rows]
                    self.assertEqual(1, len(samples))
                    ref_text = get_training_reference_text(data, ref_path, samples)
                    write_test_adapter(output)
                    train_lora.save_training_checkpoint(None, output, ref_path, ref_text,
                        {'best_loss':2.5, 'final_loss':2.5, 'num_samples':len(samples)}, [], 0, 1, 2.5)
                    return SimpleNamespace(stdout=io.StringIO('[EPOCH] 1/1 avg_loss=2.5\n'),
                                           returncode=0, wait=lambda:0, poll=lambda:0)
                with patch.object(batch_train.subprocess, 'Popen', side_effect=native_checkpoint_provider):
                    row = batch_train.train_one(str(root/'dataset.zip'), 'Native_dataset', 'voice', args)
                self.assertIsNotNone(row)
                self.assertEqual(1, row['sample_count'])
                adapter = root/'separate_models'/'voice'
                self.assertEqual(str(adapter), row['adapter_path'])
                before = {p:p.read_bytes() for p in adapter.iterdir() if p.is_file()}
                rc, acoustic, describe, logs, saved = self.run_profiler(root, [row], dry=dry)
                self.assertEqual(0, rc, logs)
                acoustic.assert_called_once_with(audio)
                if dry:
                    describe.assert_not_called()
                    self.assertNotIn('voice_profile', saved[0])
                    self.assertEqual(b'prior CSV', (root/'profiles.csv').read_bytes())
                else:
                    self.assertEqual(text, describe.call_args.kwargs['ref_text'])
                    self.assertEqual('Known measured voice.', saved[0]['voice_profile'])
                self.assertEqual(before, {p:p.read_bytes() for p in before})
                assert_file_lock_released(str(root/'manifest.json'))

    def test_explicit_separated_adapter_and_alias_use_served_pair_not_zip_pair(self):
        from voice_manifest import ADAPTER_ID_ALIASES_FILE
        for alias in (False, True):
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                models = root/'models'
                adapter = self.make_adapter(models)
                old_path = models/'old_voice'
                if alias:
                    (models/ADAPTER_ID_ALIASES_FILE).write_text(json.dumps(
                        {'version':1, 'aliases':{'old_voice':'voice'}}))
                foreign = wav_bytes(.11)
                write_zip(root/'dataset.zip', [], {'ref.wav':foreign, 'ref_text.txt':'Foreign ZIP words.'})
                rc, acoustic, describe, logs, _ = self.run_profiler(root, [{'id':'old_voice' if alias else 'voice',
                    'adapter_path':str(old_path if alias else adapter), 'zip_source':str(root/'dataset.zip')}])
                self.assertEqual(0, rc, logs)
                acoustic.assert_called_once_with((adapter/'ref_sample.wav').read_bytes())
                self.assertEqual('Served reference words.', describe.call_args.kwargs['ref_text'])

    def test_standard_manifest_resolves_historical_id_before_zip_fallback(self):
        from voice_manifest import ADAPTER_ID_ALIASES_FILE
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = self.make_adapter(root)
            (root/ADAPTER_ID_ALIASES_FILE).write_text(json.dumps(
                {'version':1, 'aliases':{'old_voice':'voice'}}))
            write_zip(root/'dataset.zip', [], {'ref.wav':wav_bytes(.11), 'ref_text.txt':'Foreign ZIP words.'})
            rc, acoustic, describe, logs, _ = self.run_profiler(root,
                [{'id':'old_voice', 'zip_source':str(root/'dataset.zip')}])
            self.assertEqual(0, rc, logs)
            acoustic.assert_called_once_with((adapter/'ref_sample.wav').read_bytes())
            self.assertEqual('Served reference words.', describe.call_args.kwargs['ref_text'])

    def test_incomplete_or_hash_mismatched_trained_reference_refuses_zip_fallback(self):
        for fault in ('missing_audio', 'missing_metadata', 'empty_text', 'weights_hash', 'reference_hash'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                adapter = self.make_adapter(root)
                metadata_path = adapter/'training_meta.json'
                metadata = json.loads(metadata_path.read_text())
                if fault.startswith('missing'):
                    (adapter/('ref_sample.wav' if fault == 'missing_audio' else 'training_meta.json')).unlink()
                else:
                    field = {'empty_text':'ref_sample_text', 'weights_hash':'checkpoint_sha256',
                             'reference_hash':'reference_audio_sha256'}[fault]
                    metadata[field] = '' if fault == 'empty_text' else '0'*64
                    metadata_path.write_text(json.dumps(metadata))
                write_zip(root/'dataset.zip', [], {'ref.wav':wav_bytes(.11), 'ref_text.txt':'ZIP fallback.'})
                rc, acoustic, describe, logs, saved = self.run_profiler(root,
                    [{'id':'voice', 'zip_source':str(root/'dataset.zip')}])
                self.assertEqual(1, rc, logs)
                acoustic.assert_not_called()
                describe.assert_not_called()
                self.assertNotIn('voice_profile', saved[0])

    def test_legacy_optional_hashes_remain_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = self.make_adapter(root, declared_hashes=False)
            write_zip(root/'dataset.zip', [], {'ref.wav':wav_bytes(.11), 'ref_text.txt':'Foreign ZIP words.'})
            rc, acoustic, describe, logs, _ = self.run_profiler(root,
                [{'id':'voice', 'zip_source':str(root/'dataset.zip')}])
            self.assertEqual(0, rc, logs)
            acoustic.assert_called_once_with((adapter/'ref_sample.wav').read_bytes())
            self.assertEqual('Served reference words.', describe.call_args.kwargs['ref_text'])

    def test_serving_audio_and_utf8_text_caps_refuse_before_acoustic_or_zip_fallback(self):
        for fault in ('audio_size', 'text_size', 'metadata_size'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                adapter = self.make_adapter(root)
                if fault == 'audio_size':
                    with (adapter/'ref_sample.wav').open('r+b') as handle:
                        handle.truncate(voice_profiler.MAX_REF_WAV_BYTES + 1)
                elif fault == 'text_size':
                    meta = json.loads((adapter/'training_meta.json').read_text())
                    meta['ref_sample_text'] = '声' * (voice_profiler.MAX_REF_TEXT_BYTES // 3 + 1)
                    (adapter/'training_meta.json').write_text(json.dumps(meta, ensure_ascii=False))
                else:
                    with (adapter/'training_meta.json').open('r+b') as handle:
                        handle.truncate(voice_profiler.MAX_REF_WAV_BYTES + voice_profiler.MAX_REF_TEXT_BYTES + 1)
                write_zip(root/'dataset.zip', [], {'ref.wav':wav_bytes(.11), 'ref_text.txt':'Fallback.'})
                rc, acoustic, describe, logs, _ = self.run_profiler(root,
                    [{'id':'voice', 'zip_source':str(root/'dataset.zip')}])
                self.assertEqual(1, rc, logs)
                self.assertIn('byte limit', logs)
                acoustic.assert_not_called()
                describe.assert_not_called()

    def test_native_generation_change_during_capture_refuses_and_releases_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = self.make_adapter(root)
            write_zip(root/'dataset.zip', [], {'ref.wav':wav_bytes(.11), 'ref_text.txt':'Fallback.'})
            read_metadata = voice_profiler.get_adapter_serving_metadata
            def mutate_after_metadata(*args, **kwargs):
                meta = read_metadata(*args, **kwargs)
                (adapter/'ref_sample.wav').write_bytes(wav_bytes(.11))
                return meta
            with patch.object(voice_profiler, 'get_adapter_serving_metadata', side_effect=mutate_after_metadata):
                rc, acoustic, describe, logs, _ = self.run_profiler(root,
                    [{'id':'voice', 'zip_source':str(root/'dataset.zip')}])
            self.assertEqual(1, rc, logs)
            self.assertIn('changed during reference capture', logs)
            acoustic.assert_not_called()
            describe.assert_not_called()
            assert_file_lock_released(str(root/'manifest.json'))

    def test_missing_explicit_adapter_path_refuses_even_when_valid_legacy_zip_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_zip(root/'dataset.zip', [], {'ref.wav':wav_bytes(.11), 'ref_text.txt':'Fallback.'})
            for path in ('', str(root/'missing')):
                with self.subTest(path=path):
                    rc, acoustic, describe, logs, _ = self.run_profiler(root,
                        [{'id':'voice', 'adapter_path':path, 'zip_source':str(root/'dataset.zip')}])
                    self.assertEqual(1, rc, logs)
                    acoustic.assert_not_called()
                    describe.assert_not_called()
