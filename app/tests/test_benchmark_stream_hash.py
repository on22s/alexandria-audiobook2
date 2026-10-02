"""Hash-only benchmark reads must stay bounded without weakening identity checks."""
import builtins
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave
import zipfile

import benchmark_fixtures as fixtures
import benchmark_runner as runner
import tts_benchmark as worker
from lora_evidence import get_file_sha256


@contextmanager
def get_bounded_reads(paths):
    original = builtins.open
    watched = {str(Path(path).resolve()) for path in paths}
    reads = []
    class Reader:
        def __init__(self, handle, path):
            self.handle, self.path = handle, path
        def read(self, size=-1):
            if not 0 <= size <= 1024 * 1024:
                raise AssertionError(f"unbounded hash read {size} for {self.path}")
            reads.append((self.path, size))
            return self.handle.read(size)
        def __getattr__(self, name):
            return getattr(self.handle, name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.handle.__exit__(*args)
    def checked_open(path, mode='r', *args, **kwargs):
        handle = original(path, mode, *args, **kwargs)
        key = str(Path(path).resolve()) if isinstance(path, (str, bytes, Path)) else ''
        return Reader(handle, key) if key in watched and mode == 'rb' else handle
    with patch('builtins.open', side_effect=checked_open):
        yield reads


class BenchmarkStreamingHashTests(unittest.TestCase):
    def get_assets(self, root):
        reference = root / 'reference.wav'
        with wave.open(str(reference), 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
            wav.writeframes(b'\x00\x10' * (2 * 1024 * 1024 + 13))
        adapter = root / 'adapter'; adapter.mkdir()
        (adapter / 'adapter_config.json').write_text('{"r":8}')
        (adapter / 'adapter_model.safetensors').write_bytes(b'tensor-fixture' * 320000)
        (adapter / 'ref_sample.wav').write_bytes(reference.read_bytes())
        (adapter / 'training_meta.json').write_text('{"ref_sample_text":"Reference words."}')
        dataset = root / 'dataset'; dataset.mkdir()
        (dataset / 'clip.wav').write_bytes(reference.read_bytes())
        (dataset / 'metadata.jsonl').write_text('\n'.join(json.dumps({
            'audio_filepath':'clip.wav', 'text':'Hello.', 'padding':'x' * (2 * 1024 * 1024)}) for _ in range(2)))
        archive = root / 'dataset.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as zip_file:
            zip_file.write(reference, 'reference.wav')
        (root / 'model.gguf').write_bytes(b'GGUF' + b'model-fixture' * 350000)
        return reference, adapter, dataset

    def get_cases(self, root):
        cases = [
            (fixtures.build_tts_clone_manifest, {'ref_audio':'reference.wav','ref_text':'Hello.','text':'Hello.'}, runner._validate_tts_fixture),
            (fixtures.build_tts_lora_manifest, {'adapter_path':'adapter','text':'Hello.'}, runner._validate_tts_fixture),
            (fixtures.build_lora_training_manifest, {'dataset_path':'dataset','sample_count':1}, runner._validate_lora_training_fixture),
            (fixtures.build_voicelab_preparer_manifest, {'audio_path':'reference.wav'}, runner._validate_preparer_fixture),
            (fixtures.build_voicelab_dedup_manifest, {'dataset_path':'dataset','samples_per_volume':1}, runner._validate_dedup_fixture),
            (fixtures.build_voicelab_profiling_manifest, {'zip_path':'dataset.zip','model_path':'model.gguf'}, runner._validate_profiling_fixture),
            (lambda items, directory: fixtures.build_export_manifest('m4b_export', items, directory),
                {'chunks':[{'audio_path':'reference.wav','text':'Hello.'}]}, runner._validate_export_fixture),
        ]
        return cases

    def test_shared_hash_matches_known_values_and_bounds_every_real_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for payload, expected in ((b'', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
                (b'abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'),
                (b'abcd' * (1024 * 1024 + 7), None)):
                path = root / 'known'; path.write_bytes(payload)
                with get_bounded_reads([path]) as reads:
                    self.assertEqual(expected or hashlib.sha256(payload).hexdigest(), get_file_sha256(str(path)))
                self.assertTrue(reads)
                if len(payload) > 3 * 1024 * 1024:
                    self.assertGreater(len(reads), 4)
            with self.assertRaises(FileNotFoundError): get_file_sha256(str(root / 'missing'))
            with self.assertRaises(IsADirectoryError): get_file_sha256(str(root))

    def test_producer_and_all_validators_keep_exact_hashes_and_reject_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.get_assets(root)
            paths = [path for path in root.rglob('*') if path.is_file()]
            before = {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
            for builder, source, validator in self.get_cases(root):
                with self.subTest(stage=validator.__name__, builder=builder.__name__):
                    # Metadata decoding is intentionally separate from hash-only reads.
                    hash_only_paths = [path for path in paths if path.name != 'metadata.jsonl']
                    with get_bounded_reads(hash_only_paths) as reads:
                        manifest = builder([source], str(root))
                    self.assertTrue(reads)
                    case = manifest['fixtures'][0]
                    with get_bounded_reads(paths) as reads:
                        validator(case, str(root))
                    self.assertTrue(reads)
                    for relative, expected in case.get('adapter_artifact_sha256', {}).items():
                        self.assertEqual(before[str(root / 'adapter' / relative)], expected)
                    for relative, expected in (case['audio_sha256'] if isinstance(case.get('audio_sha256'),dict) else {}).items():
                        base = root / case['dataset_path'] if 'dataset_path' in case else root
                        self.assertEqual(before[str(base / relative)], expected)
                    for path_key, hash_key in (('ref_audio','ref_audio_sha256'),('audio_path','audio_sha256'),
                            ('zip_path','zip_sha256'),('model_path','model_sha256')):
                        if path_key in case:self.assertEqual(before[str(root / case[path_key])],case[hash_key])
                    if 'metadata_sha256' in case:
                        self.assertEqual(before[str(root / 'dataset' / 'metadata.jsonl')],case['metadata_sha256'])
                    target = root / ('adapter/adapter_model.safetensors' if case.get('voice_type') == 'lora'
                        else 'dataset/clip.wav' if 'dataset_path' in case else 'reference.wav')
                    if 'zip_path' in case:target=root / 'model.gguf'
                    original=target.read_bytes();target.write_bytes(original+b'tampered')
                    try:
                        with get_bounded_reads(paths):
                            with self.assertRaisesRegex(ValueError,'hash changed'):
                                validator(case,str(root))
                    finally:target.write_bytes(original)
            self.assertEqual(before,{str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})

    def test_each_validator_bounds_its_reads_independently_of_producer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.get_assets(root)
            paths=[path for path in root.rglob('*') if path.is_file()]
            for builder,source,validator in self.get_cases(root):
                case=builder([source],tmp)['fixtures'][0]
                with self.subTest(validator=validator.__name__,voice_type=case.get('voice_type')):
                    with get_bounded_reads(paths) as reads:
                        validator(case,tmp)
                    self.assertTrue(reads)
                    self.assertTrue(any(count==1024*1024 for _,count in reads))
                    if 'model_path' in case:
                        (root/'model.gguf').unlink()
                        with self.assertRaisesRegex(ValueError,'missing'):
                            validator(case,tmp)
                        (root/'model.gguf').write_bytes(b'GGUF'+b'model-fixture'*350000)

    def test_actual_clone_and_lora_worker_checks_happen_before_model_and_write_real_pcm(self):
        class Engine:
            def __init__(self):self.events=[]
            def _init_local_clone(self):self.events.append('clone-load')
            def _get_clone_prompt(self,*args):self.events.append('clone-prompt')
            def _init_local_lora(self,path,**kwargs):self.events.append('lora-load');return object()
            def _ensure_lora_prompt(self,path,model,text,**kwargs):self.events.append(('lora-prompt',text))
            def generate_clone_voice(self,text,speaker,config,path):return self.save_wav(path)
            def generate_lora_voice(self,text,instruct,config,path):return self.save_wav(path)
            def save_wav(self,path):
                self.events.append('generate')
                with wave.open(path,'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
                    wav.writeframes(b'\x00\x10'*4800)
                return True
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.get_assets(root)
            paths=[path for path in root.rglob('*') if path.is_file()]
            cases=[(fixtures.build_tts_clone_manifest([{'ref_audio':'reference.wav','ref_text':'Reference words.','text':'Hello.'}],tmp)['fixtures'][0],worker.run_clone_voice_case,root/'reference.wav'),
                (fixtures.build_tts_lora_manifest([{'adapter_path':'adapter','text':'Hello.'}],tmp)['fixtures'][0],worker.run_lora_voice_case,root/'adapter/adapter_model.safetensors')]
            for case,run,target in cases:
                with self.subTest(voice_type=case['voice_type']):
                    output=root/'output.wav';engine=Engine()
                    with get_bounded_reads(paths),patch.object(worker,'sample_gpu_utilization',return_value=None),patch.dict(sys.modules,{'torch':SimpleNamespace(manual_seed=lambda value:None)}):
                        metrics=run(engine,case,str(output),tmp,load_model=True)
                    with wave.open(str(output),'rb') as wav:
                        self.assertEqual(4800,wav.getnframes());self.assertEqual(24000,wav.getframerate())
                        self.assertEqual(b'\x00\x10'*4800,wav.readframes(4800))
                    self.assertEqual(0.2,metrics['duration_seconds'])
                    self.assertEqual('generate',engine.events[-1])
                    if case['voice_type']=='lora':self.assertIn(('lora-prompt','Reference words.'),engine.events)
                    original=target.read_bytes();target.write_bytes(original+b'tampered');output.unlink();engine=Engine()
                    try:
                        with get_bounded_reads(paths):
                            with self.assertRaisesRegex(ValueError,'hash changed'):
                                run(engine,case,str(output),tmp,load_model=True)
                        self.assertEqual([],engine.events);self.assertFalse(output.exists())
                    finally:target.write_bytes(original)
