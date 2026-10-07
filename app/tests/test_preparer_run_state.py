import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    'test_alexandria_preparer_run_state',
    ROOT / 'alexandria_preparer_rocm_compatible.py')
preparer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparer)


class PreparerRunStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'dataset_temp'
        self.work.mkdir()

    def write_sample(self, name='sample_0000.wav'):
        path = self.work / name
        with wave.open(str(path), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(b'\x00\x00' * 10)
        return path

    def write_checkpoint(self, entry):
        (self.work / 'metadata.jsonl').write_text(
            json.dumps(entry) + '\n', encoding='utf-8')

    def test_checkpoint_rejects_missing_wav_and_path_escape(self):
        entry = {'audio_filepath': 'sample_0000.wav', 'start': 0.0,
                 'end': 1.0, 'duration': 1.0, 'text': 'hello'}
        self.write_checkpoint(entry)
        with self.assertRaisesRegex(preparer.RunStateError, 'missing'):
            preparer._load_existing_checkpoint(self.work)
        self.write_sample()
        entry['audio_filepath'] = '../outside.wav'
        self.write_checkpoint(entry)
        with self.assertRaisesRegex(preparer.RunStateError, 'Invalid sample'):
            preparer._load_existing_checkpoint(self.work)

    def test_checkpoint_rejects_malformed_fields_and_symlink(self):
        sample = self.write_sample()
        entry = {'audio_filepath': sample.name, 'start': 0.0,
                 'end': 1.0, 'duration': 1.0, 'text': 'hello'}
        for changed in ({'end': 'bad'}, {'start': float('nan')},
                        {'text': None}, {'audio_filepath': None}):
            with self.subTest(changed=changed):
                self.write_checkpoint({**entry, **changed})
                with self.assertRaises(preparer.RunStateError):
                    preparer._load_existing_checkpoint(self.work)
        sample.unlink()
        sample.symlink_to(self.root / 'outside.wav')
        self.write_checkpoint(entry)
        with self.assertRaisesRegex(preparer.RunStateError, 'symlink'):
            preparer._load_existing_checkpoint(self.work)

    def test_failed_tail_repair_preserves_checkpoint_and_samples_then_retry_recovers(self):
        real_open=open;real_temporary=tempfile.NamedTemporaryFile
        class FailedWrite:
            def __init__(self,stream):self.stream=stream
            def __getattr__(self,name):return getattr(self.stream,name)
            def __enter__(self):return self
            def __exit__(self,*args):self.stream.close()
            def writelines(self,lines):
                self.stream.write(lines[0][:10]);self.stream.flush()
                raise OSError('injected disk full')
        for failure in ('write','replace'):
            with self.subTest(failure=failure):
                rows=[];samples=[]
                for index in range(2):
                    name=f'sample_{index:04d}.wav';sample=self.write_sample(name)
                    samples.append((sample,sample.read_bytes()))
                    rows.append(json.dumps({'audio_filepath':name,'text':'synthetic',
                        'start':index,'end':index+1,'duration':1})+'\n')
                checkpoint=self.work/'metadata.jsonl';original=''.join(rows)+'{\n'
                checkpoint.write_text(original,encoding='utf-8')
                def broken_open(path,mode='r',*args,**kwargs):
                    stream=real_open(path,mode,*args,**kwargs)
                    return FailedWrite(stream) if Path(path)==checkpoint and mode=='w' else stream
                def broken_stage(*args,**kwargs):return FailedWrite(real_temporary(*args,**kwargs))
                from contextlib import ExitStack
                with ExitStack() as contexts:
                    if failure=='write':
                        contexts.enter_context(patch.object(preparer,'open',side_effect=broken_open,create=True))
                        contexts.enter_context(patch.object(preparer.tempfile,'NamedTemporaryFile',side_effect=broken_stage))
                    else:contexts.enter_context(patch.object(preparer.os,'replace',side_effect=OSError('publication refused')))
                    with self.assertRaises(preparer.RunStateError):preparer._load_existing_checkpoint(str(self.work))
                self.assertEqual(original.encode(),checkpoint.read_bytes())
                self.assertFalse(list(self.work.glob('.metadata-repair-*')))
                for sample,raw in samples:self.assertEqual(raw,sample.read_bytes())
                entries,resume,next_index=preparer._load_existing_checkpoint(str(self.work))
                self.assertEqual((2,2),(resume,next_index));self.assertEqual(2,len(entries))
                self.assertEqual(''.join(rows).encode(),checkpoint.read_bytes())

    def test_checkpoint_valid_entry_recovers_timeline(self):
        self.write_sample()
        self.write_checkpoint({'audio_filepath': 'sample_0000.wav',
                               'start': 2.0, 'end': 3.0, 'duration': 1.0,
                               'text': 'hello'})
        entries, resume_time, next_index = preparer._load_existing_checkpoint(
            self.work)
        self.assertEqual((3.0, 1), (resume_time, next_index))
        self.assertEqual(str(self.work / 'sample_0000.wav'), entries[0]['wav_path'])

    def test_zip_refuses_missing_wav_before_touching_output(self):
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        output = self.root / 'dataset.zip'
        output.write_bytes(b'prior valid output')
        metadata = [{'audio_filepath': 'sample_0000.wav',
                     'speaker': 'narrator'}]
        with self.assertRaisesRegex(preparer.RunStateError, 'Missing sample WAV'):
            preparer._create_zip_dataset(metadata, str(output))
        self.assertEqual(b'prior valid output', output.read_bytes())

    def test_zip_volume_manifest_removes_obsolete_volumes(self):
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        output = self.root / 'dataset.zip'
        for index in range(3):
            self.write_sample(f'sample_{index:04d}.wav')
        rows = [{'audio_filepath': f'sample_{index:04d}.wav',
                 'speaker_labels': ['UNKNOWN'], 'duration': 1.0} for index in range(3)]
        preparer._create_zip_dataset(rows, str(output), zip_max_files=1)
        self.assertTrue((self.root / 'dataset_vol03.zip').is_file())
        preparer._create_zip_dataset(rows[:1], str(output), zip_max_files=1)
        self.assertFalse((self.root / 'dataset_vol03.zip').exists())
        self.assertEqual(['dataset.zip'],
                         json.loads((self.root / 'dataset.zip.volumes.json').read_text())['volumes'])

    def test_zip_disambiguates_sanitized_group_collision(self):
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        for index in range(2):
            self.write_sample(f'sample_{index:04d}.wav')
        rows = [{'audio_filepath': f'sample_{index:04d}.wav',
                 'speaker_labels': ['UNKNOWN'], 'duration': 1.0, 'character': char}
                for index, char in enumerate(('李', '王'))]
        preparer._create_zip_dataset(rows, str(self.root / 'dataset.zip'))
        names = json.loads((self.root / 'dataset.zip.volumes.json').read_text())['volumes']
        self.assertEqual(2, len(set(names)))
        self.assertTrue(all((self.root / name).is_file() for name in names))

    def test_zip_refuses_unmanaged_stale_volume(self):
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.write_sample()
        stale = self.root / 'dataset_vol02.zip'
        stale.write_bytes(b'old volume')
        rows = [{'audio_filepath': 'sample_0000.wav', 'speaker_labels': ['UNKNOWN'], 'duration': 1.0}]
        with self.assertRaisesRegex(preparer.RunStateError, 'Unmanaged old ZIP volume'):
            preparer._create_zip_dataset(rows, str(self.root / 'dataset.zip'))
        self.assertEqual(b'old volume', stale.read_bytes())
        self.assertFalse((self.root / 'dataset.zip').exists())

    def test_zip_refuses_empty_dataset(self):
        with self.assertRaisesRegex(preparer.RunStateError, 'No metadata'):
            preparer._create_zip_dataset([], str(self.root / 'dataset.zip'))
        self.assertFalse((self.root / 'dataset.zip').exists())

    def test_phase_command_keeps_values_named_like_phases(self):
        command = preparer.build_phase_command(
            'annotate', ['--audio', 'book.wav', '--book-title', 'asr',
                         '--character', 'enrich', '--phase', 'asr'])
        self.assertEqual(['--phase', 'annotate', '--audio', 'book.wav',
                          '--book-title', 'asr', '--character', 'enrich'],
                         command[2:])

    def test_invalid_cli_numbers_fail_before_starting_phases(self):
        for option, value in (('--zip-max-files', '0'), ('--val-split', 'nan'),
                              ('--chunk-size', 'inf'), ('--source-threshold', '2'),
                              ('--batch-size', '-1')):
            with self.subTest(option=option):
                with patch.object(sys, 'argv', ['preparer', '--audio', 'book.wav',
                                                option, value]), \
                     patch.object(preparer.subprocess, 'run') as run:
                    with self.assertRaises(SystemExit) as result:
                        preparer.main()
                self.assertEqual(2, result.exception.code)
                run.assert_not_called()

    def test_diarize_without_token_fails_before_starting_phases(self):
        with patch.object(sys, 'argv', ['preparer', '--audio', 'book.wav',
                                        '--model', 'model.gguf', '--diarize']), \
             patch.dict(os.environ, {'HF_TOKEN': ''}), \
             patch.object(preparer.subprocess, 'run') as run:
            with self.assertRaises(SystemExit) as result:
                preparer.main()
        self.assertEqual(2, result.exception.code)
        run.assert_not_called()

    def test_enrichment_uses_requested_chunk_size_and_script_location(self):
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        (self.root / 'book.wav').write_bytes(b'audio')
        (self.work / 'asr_segments.json').write_text(json.dumps({
            'word_segments': [
                {'word': 'first', 'start': 0.0, 'end': 0.5},
                {'word': 'second', 'start': 3.0, 'end': 3.5}],
            'detected_lang': 'en'}))

        def run_enricher(command):
            self.assertEqual(ROOT / 'llm_enricher.py', Path(command[1]))
            chunks = json.loads((self.work / 'asr_chunks_for_enrich.json').read_text())
            self.assertEqual(2, len(chunks))
            (self.work / 'enriched_segments.json').write_text(json.dumps(chunks))
            return SimpleNamespace(returncode=0)

        with patch.object(sys, 'argv', ['preparer', '--audio', 'book.wav',
                                        '--model', 'model.gguf', '--phase', 'enrich',
                                        '--chunk-size', '2', '--llm-model-path', 'llm.gguf']), \
             patch.object(preparer, 'LLAMA_CPP_AVAILABLE', True), \
             patch.object(preparer, 'acquire_run_lock', return_value=1), \
             patch.object(preparer, 'get_run_identity', return_value={}), \
             patch.object(preparer, 'ensure_run_manifest'), \
             patch.object(preparer, 'is_verified_artifact', return_value=True), \
             patch.object(preparer, 'mark_artifact_complete'), \
             patch.object(preparer.subprocess, 'run', side_effect=run_enricher):
            self.assertEqual(0, preparer.main())

    def test_keep_unaligned_allows_low_alignment_preflight(self):
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        (self.root / 'book.wav').write_bytes(b'audio')
        (self.root / 'book.txt').write_text('source')
        (self.work / 'audio_24k_scratch.wav').write_bytes(b'scratch')
        (self.work / 'asr_segments.json').write_text(json.dumps({
            'word_segments': [{'word': 'text', 'start': 0, 'end': 1}],
            'detected_lang': 'en'}))
        source_state = {'orig_match': [], 'cursor': 0,
                        'anchor_entry_idx': 0, 'proper_nouns': set()}
        common = ['preparer', '--audio', 'book.wav', '--model', 'model.gguf',
                  '--phase', 'annotate', '--source', 'book.txt']
        with patch.object(preparer, 'LLAMA_CPP_AVAILABLE', True), \
             patch.object(preparer, 'acquire_run_lock', return_value=1), \
             patch.object(preparer, 'get_run_identity', return_value={}), \
             patch.object(preparer, 'ensure_run_manifest'), \
             patch.object(preparer, 'is_verified_artifact', return_value=True), \
             patch.object(preparer, 'validate_scratch_path'), \
             patch.object(preparer, 'extract_metadata_for_naming', return_value=(None, None)), \
             patch.object(preparer, '_build_provisional_entries_for_anchor', return_value=[{}] * 10), \
             patch.object(preparer, '_build_source_state', return_value=source_state), \
             patch.object(preparer.alignment, 'estimate_alignment_quality',
                          return_value=(0.2, 10, 8, None)), \
             patch.object(preparer, 'annotate_chunks', return_value=[{'duration': 1}]) as annotate, \
             patch.object(preparer, '_create_zip_dataset'):
            with patch.object(sys, 'argv', common):
                with self.assertRaises(SystemExit):
                    preparer.main()
            annotate.assert_not_called()
            (self.work / 'audio_24k_scratch.wav').write_bytes(b'scratch')
            with patch.object(sys, 'argv', common + ['--keep-unaligned']):
                self.assertEqual(0, preparer.main())
            annotate.assert_called_once()

    def test_long_audio_asr_uses_disk_backed_samples_and_cleans_them(self):
        import numpy as np

        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        audio_path = self.root / 'book.wav'
        with wave.open(str(audio_path), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b'\x00\x20' * 16000)
        mapped_paths = []

        def transcribe(samples, _device, _language, limit=None):
            self.assertIsInstance(samples, np.memmap)
            self.assertEqual(16000, len(samples))
            self.assertAlmostEqual(0.25, float(samples[0]), places=2)
            mapped_paths.append(samples.filename)
            return ([{'word': 'test', 'start': 0.0, 'end': 1.0,
                      'confidence': 1.0}], 'en')

        with patch.object(sys, 'argv', ['preparer', '--audio', str(audio_path),
                                        '--phase', 'asr']), \
             patch.object(preparer, 'acquire_run_lock', return_value=1), \
             patch.object(preparer, 'get_run_identity', return_value={}), \
             patch.object(preparer, 'ensure_run_manifest'), \
             patch.object(preparer, 'validate_scratch_path'), \
             patch.object(preparer, '_wav_overflow_info', return_value=(False, 601, 601)), \
             patch.object(preparer, '_lazy_import_torch'), \
             patch.object(preparer, 'resolve_cuda_device', return_value='cpu'), \
             patch.object(preparer, 'log_torch_info'), \
             patch.object(preparer, 'mark_artifact_complete'), \
             patch.object(preparer, 'clear_vram'), \
             patch.object(preparer, 'choose_and_transcribe', side_effect=transcribe):
            self.assertEqual(0, preparer.main())
        self.assertEqual(1, len(mapped_paths))
        self.assertFalse(Path(mapped_paths[0]).exists())
        self.assertTrue((self.work / 'audio_24k_scratch.wav').exists())

    def test_wave_tags_replace_existing_frames(self):
        from mutagen.wave import WAVE

        sample = self.write_sample()
        entry = {'audio_filepath': sample.name, 'book_title': 'Book',
                 'character': 'Narrator', 'narrator_style': 'calm'}
        preparer.apply_wave_tags(sample, entry)
        preparer.apply_wave_tags(sample, entry)
        tags = WAVE(sample).tags
        for frame in ('TALB', 'TPE1', 'COMM', 'TIT2'):
            with self.subTest(frame=frame):
                self.assertEqual(1, len(tags.getall(frame)))

    def test_windows_directory_refusal_keeps_receipt_and_chunk_file_sync(self):
        import numpy as np
        import alexandria_run_manifest as manifest
        from alexandria_batch_processor import save_batch_receipt
        real_open=os.open;real_fsync=os.fsync
        def directory_refusal(path,flags,*args,**kwargs):
            if os.path.isdir(path):raise PermissionError('simulated Windows directory refusal')
            return real_open(path,flags,*args,**kwargs)
        for operation in ('receipt','chunk'):
            synced=[]
            def sync(fd):
                synced.append(stat.S_IFMT(os.fstat(fd).st_mode));real_fsync(fd)
            proxy=SimpleNamespace(name='nt',open=directory_refusal,fsync=sync,close=os.close)
            with self.subTest(operation=operation), (self.work/'metadata.jsonl').open('w') as checkpoint, \
                 patch.object(manifest,'os',proxy),patch.object(os,'open',side_effect=directory_refusal),patch.object(os,'fsync',side_effect=sync):
                if operation=='receipt':
                    receipt=str(self.work/'receipt.json');save_batch_receipt(receipt,{'complete':True})
                    self.assertEqual({'complete':True},json.loads(Path(receipt).read_text()))
                    self.assertEqual([stat.S_IFREG],synced)
                else:
                    rows=[];stats={'chunk_durations':[]};timing={'kept_chunks':0,'wav_write':0.0}
                    preparer._save_chunk_metadata({'audio_slice':np.zeros(2400,dtype=np.float32),
                        'chunk_word_data':[{'speaker':'narrator'}],'current_start':0.0,'chunk_end_time':0.1},
                        'hello',None,None,None,rows,checkpoint,stats,timing,0,str(self.work))
                    self.assertEqual([stat.S_IFREG,stat.S_IFREG],synced)
                    self.assertEqual(1,len(rows))
                    self.assertEqual(rows[0],json.loads((self.work/'metadata.jsonl').read_text()))
                    with wave.open(str(self.work/'sample_0000.wav')) as audio:
                        self.assertEqual(2400,audio.getnframes())

    def test_posix_directory_sync_failures_propagate_and_close_the_descriptor(self):
        import alexandria_run_manifest as manifest
        proxy=SimpleNamespace(name='posix',open=os.open,fsync=os.fsync,close=os.close,O_RDONLY=os.O_RDONLY)
        with patch.object(manifest,'os',proxy),patch.object(proxy,'open',side_effect=PermissionError('directory denied')):
            with self.assertRaises(PermissionError):manifest.sync_run_directory(str(self.work))
        descriptor=os.open(str(self.work),os.O_RDONLY)
        with patch.object(manifest,'os',proxy),patch.object(proxy,'open',return_value=descriptor), \
             patch.object(proxy,'fsync',side_effect=OSError('disk sync failed')):
            with self.assertRaisesRegex(OSError,'disk sync failed'):manifest.sync_run_directory(str(self.work))
        with self.assertRaises(OSError):os.fstat(descriptor)

    def test_wav_directory_is_synced_before_checkpoint_record(self):
        import numpy as np

        item = {'audio_slice': np.zeros(24000, dtype=np.float32),
                'chunk_word_data': [{'speaker': 'narrator'}],
                'current_start': 0.0, 'chunk_end_time': 1.0}
        stats = {'chunk_durations': []}
        timing = {'kept_chunks': 0, 'wav_write': 0.0}
        synced_types = []
        real_fsync = os.fsync

        def record_fsync(fd):
            synced_types.append(stat.S_IFMT(os.fstat(fd).st_mode))
            real_fsync(fd)

        with (self.work / 'metadata.jsonl').open('w') as checkpoint:
            with patch.object(preparer.os, 'fsync', side_effect=record_fsync):
                preparer._save_chunk_metadata(
                    item, 'hello', None, None, None, [], checkpoint,
                    stats, timing, 0, str(self.work))
        self.assertEqual([stat.S_IFREG, stat.S_IFDIR, stat.S_IFREG],
                         synced_types)
        self.assertTrue((self.work / 'sample_0000.wav').is_file())

    def test_annotation_checks_manifest_before_loading_model(self):
        from alexandria_run_manifest import get_run_identity, ensure_run_manifest

        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        audio = self.root / 'book.wav'
        audio.write_bytes(b'input')
        identity = get_run_identity(SimpleNamespace(
            audio=str(audio), source=None, phase=None, resume=False,
            hf_token=None, chunk_size=10))
        ensure_run_manifest(self.work, identity, fresh=True)
        sentinel = RuntimeError('model reached')
        with patch.object(preparer, '_load_llm', side_effect=sentinel):
            with self.assertRaisesRegex(RuntimeError, 'model reached'):
                preparer.annotate_chunks([{'word': 'pending', 'start': 0, 'end': 1}],
                                         'model.gguf', 10, 'scratch.wav',
                                         resume=True, run_identity=identity)
            changed = dict(identity)
            changed['options'] = {'chunk_size': 20}
            with self.assertRaisesRegex(preparer.RunStateError, 'changed'):
                preparer.annotate_chunks([{'word': 'pending', 'start': 0, 'end': 1}],
                                         'model.gguf', 10, 'scratch.wav',
                                         resume=True, run_identity=changed)

    def test_orchestrator_passes_lock_and_completed_asr_to_child(self):
        from alexandria_run_manifest import (
            LOCK_ENV, acquire_run_lock, get_manifest, mark_artifact_complete)

        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        (self.work / 'unrelated.txt').write_text('keep')
        audio = self.root / 'book.wav'
        audio.write_bytes(b'input')
        phases = []

        def phase_child(command, **kwargs):
            phase = command[command.index('--phase') + 1]
            phases.append(phase)
            self.assertEqual(str(kwargs['pass_fds'][0]), kwargs['env'][LOCK_ENV])
            with patch.dict(os.environ, {LOCK_ENV: kwargs['env'][LOCK_ENV]}):
                self.assertEqual(kwargs['pass_fds'][0],
                                 acquire_run_lock(self.work))
            if phase == 'asr':
                asr = self.work / 'asr_segments.json'
                asr.write_text('{"detected_lang":"en","word_segments":[]}')
                mark_artifact_complete(self.work, get_manifest(self.work)['identity'],
                                       'asr', asr)
            return SimpleNamespace(returncode=0)

        with patch.object(sys, 'argv', ['preparer', '--audio', str(audio),
                                        '--model', 'model.gguf']), \
             patch.object(preparer, 'LLAMA_CPP_AVAILABLE', True), \
             patch.object(preparer.subprocess, 'run', side_effect=phase_child):
            with self.assertRaises(SystemExit) as result:
                preparer.main()
        self.assertEqual(0, result.exception.code)
        self.assertEqual(['asr', 'annotate'], phases)
        self.assertEqual('keep', (self.work / 'unrelated.txt').read_text())
        self.assertFalse((self.work / '.run_manifest.json').exists())

    def test_orchestrator_refuses_changed_input_before_phases(self):
        from alexandria_run_manifest import ensure_run_manifest, get_run_identity

        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        audio = self.root / 'book.wav'
        audio.write_bytes(b'old')
        identity = get_run_identity(SimpleNamespace(
            audio=str(audio), source=None, phase=None, resume=False,
            hf_token=None, model='model.gguf'))
        ensure_run_manifest(self.work, identity, fresh=True)
        audio.write_bytes(b'new')
        with patch.object(sys, 'argv', ['preparer', '--audio', str(audio),
                                        '--model', 'model.gguf', '--resume']), \
             patch.object(preparer.subprocess, 'run') as run:
            with self.assertRaises(SystemExit) as result:
                preparer.main()
        self.assertEqual(2, result.exception.code)
        run.assert_not_called()

    def test_requested_diarization_failure_is_not_empty_success(self):
        with patch.object(preparer, '_load_diarization_pipeline', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                preparer.diarize_audio('book.wav')

        def failing_pipeline(_audio):
            raise RuntimeError('decode failed')

        with patch.object(preparer, 'get_diarization_audio_input', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'decode failed'):
                preparer.diarize_audio('book.wav', pipeline=failing_pipeline)


class PreparerBatchAnnotationContractTests(unittest.TestCase):
    def annotate(self, content, merge=None):
        from unittest.mock import Mock
        llm = SimpleNamespace(create_chat_completion=Mock(return_value={
            "choices": [{"message": {"content": content}}]}))
        batch = [{"segment_idx": 17, "text": "First original."},
                 {"segment_idx": 42, "text": "Second original."}]
        if merge is not None:
            for row in batch:
                row["source_words_for_merge"] = row["text"].split()
        timing = {"llm_infer": 0, "sanitize": 0}
        stats = {"llm_success": 0, "llm_fail": 0, "sanitize_changed": 0}
        alignment = SimpleNamespace(merge_annotations_with_source=merge)
        with patch.object(preparer, "_check_llm_fail_rate"):
            result = preparer._annotate_batch(llm, batch, alignment, 2, timing, stats)
        self.assertEqual("First original.", batch[0]["text"])
        self.assertEqual("Second original.", batch[1]["text"])
        return result, stats

    def test_numbered_annotations_follow_labels_instead_of_response_order(self):
        for content in ("2. *Second* original.\n1. *First* original.",
                        "1) *First* original.\n2) *Second* original."):
            with self.subTest(content=content):
                result, stats = self.annotate(content)
                self.assertEqual([(17, "*First* original."), (42, "*Second* original.")], result)
                self.assertEqual(2, stats["llm_success"])
                self.assertEqual(0, stats["llm_fail"])

    def test_invalid_numbered_contract_falls_back_without_partial_success(self):
        for content in ("1. First.\n1. Duplicate.", "1. First.",
                        "1. First.\n3. Outside.", "0. Outside.\n2. Second."):
            with self.subTest(content=content):
                result, stats = self.annotate(content)
                self.assertIsNone(result)
                self.assertEqual(0, stats["llm_success"])
                self.assertEqual(0, stats["llm_fail"])
                self.assertEqual(1, stats["llm_batch_fail"])

    def test_malformed_array_items_refuse_before_merging_any_annotation(self):
        from unittest.mock import Mock
        for invalid in ({"text": "junk"}, None, ["junk"], 123, True, "", "  "):
            with self.subTest(invalid=invalid):
                merge = Mock(return_value="merged")
                result, stats = self.annotate(json.dumps(["Valid first.", invalid]), merge)
                self.assertIsNone(result)
                merge.assert_not_called()
                self.assertEqual(0, stats["llm_success"])
                self.assertEqual(0, stats["llm_fail"])
                self.assertEqual(1, stats["llm_batch_fail"])
        result, stats = self.annotate('["*First* original.", "*Second* original."]')
        self.assertEqual([(17, "*First* original."), (42, "*Second* original.")], result)
        self.assertEqual(2, stats["llm_success"])


class PreparerFallbackContextTests(unittest.TestCase):
    def test_full_and_tail_fallbacks_use_only_preceding_text_and_save_audio(self):
        import numpy as np
        import soundfile as sf
        from unittest.mock import Mock
        words = [{"word": word, "start": index * 1.2, "end": (index + 1) * 1.2,
                  "confidence": 1, "speaker": "ONLY"}
                 for index, word in enumerate(("First.", "Second.", "Third."))]
        seen = []
        def complete(**request):
            prompt = request["messages"][1]["content"]
            seen.append(prompt)
            if "2. Annotate this segment:" in prompt:
                content = "not a valid batch"
            else:
                content = prompt.split("Annotate this segment:\n", 1)[1]
            return {"choices": [{"message": {"content": content}}]}
        llm = SimpleNamespace(create_chat_completion=Mock(side_effect=complete))
        samples = np.sin(np.arange(86400) * 0.05).astype("float32") * 0.1
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with patch.object(preparer, "ensure_run_manifest"),                      patch.object(preparer, "_load_llm", return_value=llm),                      patch.object(preparer, "log_gpu_stats"),                      patch.object(preparer, "_calculate_chunk_snr", return_value=80):
                    metadata = preparer.annotate_chunks(words, "mock.gguf", 1, samples,
                        min_chunk_duration=1, batch_size=2, run_identity={"test": "fixture"})
                self.assertEqual(4, len(seen))
                self.assertEqual("Annotate this segment:\nFirst.", seen[1])
                self.assertEqual("Previous context: First.\n\nAnnotate this segment:\nSecond.", seen[2])
                self.assertEqual("Previous context: First. Second.\n\nAnnotate this segment:\nThird.", seen[3])
                self.assertEqual(["First.", "Second.", "Third."], [row["text"] for row in metadata])
                persisted = [json.loads(line) for line in Path("dataset_temp/metadata.jsonl").read_text().splitlines()]
                self.assertEqual(["First.", "Second.", "Third."], [row["text"] for row in persisted])
                for row in persisted:
                    values, rate = sf.read(Path("dataset_temp", row["audio_filepath"]))
                    self.assertEqual(24000, rate)
                    self.assertAlmostEqual(1.2, len(values) / rate)
            finally:
                os.chdir(previous)


class MixedSpeakerTrainingZipTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.work=self.root/'dataset_temp';self.work.mkdir()
        previous=Path.cwd();os.chdir(self.root);self.addCleanup(os.chdir,previous)

    def save_chunks(self, labels):
        import numpy as np
        rows=[];stats={'chunk_durations':[],'audio_short':0};timing={'kept_chunks':0,'wav_write':0.0}
        with (self.work/'metadata.jsonl').open('w') as checkpoint:
            for i, speakers in enumerate(labels):
                item={'audio_slice':np.full(2400,0.1,dtype='float32'),'chunk_word_data':[{'speaker':speaker} for speaker in speakers],
                      'current_start':i*0.1,'chunk_end_time':(i+1)*0.1}
                preparer._save_chunk_metadata(item,'Line '+str(i),None,None,None,rows,checkpoint,stats,timing,i,str(self.work))
        return rows

    def test_actual_chunk_writer_records_complete_word_speaker_evidence(self):
        rows=self.save_chunks([['BOB','ALICE','ALICE'],['UNKNOWN']])
        self.assertEqual(['ALICE','BOB'],rows[0].get('speaker_labels'))
        self.assertEqual('ALICE',rows[0]['speaker'])
        self.assertEqual(['UNKNOWN'],rows[1].get('speaker_labels'))
        self.assertEqual(rows,[json.loads(line) for line in (self.work/'metadata.jsonl').read_text().splitlines()])

    def test_actual_chunk_writer_records_all_labels_and_zip_excludes_mixed_and_legacy_audio(self):
        import copy,zipfile
        labels=[['ALICE']]*4+[['ALICE','BOB','ALICE'],['UNKNOWN']]
        rows=self.save_chunks(labels)
        for row,observed in zip(rows,labels):row['speaker_labels']=sorted(set(observed))
        rows.append(dict(rows[0],audio_filepath='sample_0006.wav',wav_path=str(self.work/'sample_0006.wav')))
        rows[-1].pop('speaker_labels');(self.work/'sample_0006.wav').write_bytes((self.work/'sample_0000.wav').read_bytes())
        before=copy.deepcopy(rows);artifacts={p.name:p.read_bytes() for p in self.work.iterdir()}
        output=self.root/'dataset.zip'
        preparer._create_zip_dataset(rows,str(output),val_split=0.5)
        names=json.loads(Path(str(output)+'.volumes.json').read_text())['volumes']
        observed=[];partitions=[]
        for name in names:
            with zipfile.ZipFile(self.root/name) as archive:
                metadata=[json.loads(line) for line in archive.read('metadata.jsonl').decode().splitlines()]
                observed.extend(metadata)
                for entry in metadata:
                    self.assertEqual(artifacts[Path(entry['audio_filepath']).name],archive.read(entry['audio_filepath']))
                    self.assertNotEqual('sample_0004.wav',Path(entry['audio_filepath']).name)
                    self.assertNotEqual('sample_0006.wav',Path(entry['audio_filepath']).name)
                    partitions.append(entry['audio_filepath'].split('/')[0])
        self.assertEqual(5,len(observed));self.assertEqual(2,partitions.count('val'));self.assertEqual(3,partitions.count('train'))
        self.assertEqual({'ALICE','UNKNOWN'},{row['speaker'] for row in observed})
        self.assertEqual(before,rows)
        self.assertEqual(artifacts,{p.name:p.read_bytes() for p in self.work.iterdir()})

    def test_all_excluded_rows_refuse_before_any_archive_or_manifest_changes(self):
        rows=self.save_chunks([['ALICE','BOB'],['ALICE']])
        rows[1].pop('speaker_labels',None)
        for preexisting in (False,True):
            with self.subTest(preexisting=preexisting):
                output=self.root/'dataset.zip';manifest=Path(str(output)+'.volumes.json')
                if preexisting:
                    output.write_bytes(b'previous archive');manifest.write_bytes(b'{"volumes":["dataset.zip"]}')
                artifacts={p.name:p.read_bytes() for p in self.work.iterdir()}
                with self.assertRaisesRegex(preparer.RunStateError,'No eligible single-speaker'):
                    preparer._create_zip_dataset(rows,str(output))
                if preexisting:
                    self.assertEqual(b'previous archive',output.read_bytes());self.assertEqual(b'{"volumes":["dataset.zip"]}',manifest.read_bytes())
                else:self.assertFalse(output.exists());self.assertFalse(manifest.exists())
                self.assertFalse(Path(str(output)+'.tmp').exists())
                self.assertEqual(artifacts,{p.name:p.read_bytes() for p in self.work.iterdir()})

    def test_checkpoint_resume_keeps_legacy_and_label_evidence_without_claiming_it_is_trainable(self):
        rows=self.save_chunks([['ALICE'],['BOB','ALICE']]);rows[0].pop('speaker_labels',None)
        checkpoint=self.work/'metadata.jsonl';checkpoint.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        original=checkpoint.read_bytes()
        loaded,resume,index=preparer._load_existing_checkpoint(str(self.work))
        self.assertEqual(rows,loaded);self.assertEqual(0.2,resume);self.assertEqual(2,index)
        self.assertEqual(original,checkpoint.read_bytes())
        with self.assertRaisesRegex(preparer.RunStateError,'No eligible single-speaker'):
            preparer._create_zip_dataset(loaded,str(self.root/'resumed.zip'))
        self.assertEqual(original,checkpoint.read_bytes())
