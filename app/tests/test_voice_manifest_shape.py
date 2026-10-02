"""Standalone writer boundaries reject malformed manifests without data loss."""
import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from tests import test_lora_batch_preflight as training_fixtures

ROOT=Path(__file__).resolve().parent.parent.parent

def load_script(name):
    spec=importlib.util.spec_from_file_location('manifest_shape_'+name,ROOT/'tools'/'voice_lab'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

batch=load_script('batch_train_lora');profiler=load_script('voice_profiler')
BAD_DOCUMENTS=(b'{}',b'null',b'"scalar"',b'3',b'true',b'[{"id":"keep"},null]',b'[{},"bad"]',b'[{},[]]',b'[{},true]',b'not JSON',b'\xff')

class VoiceManifestShapeTests(unittest.TestCase):
    def get_batch_args(self,root):
        zips=root/'zips';zips.mkdir();training_fixtures.save_valid_training_zip(zips/'Speaker.zip')
        return ['batch_train_lora.py','--zips_dir',str(zips),'--datasets_dir',str(root/'datasets'),
                '--models_dir',str(root/'models'),'--manifest',str(root/'manifest.json'),
                '--python',sys.executable,'--device','cpu']

    def test_batch_reader_rejects_bad_containers_rows_and_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'manifest.json'
            self.assertEqual([],batch.load_manifest(str(path)));self.assertFalse(path.exists())
            for document in BAD_DOCUMENTS:
                with self.subTest(document=document):
                    path.write_bytes(document)
                    with self.assertRaises((ValueError,OSError)):
                        batch.load_manifest(str(path))
                    self.assertEqual(document,path.read_bytes())
            for data in ([],[{}],[{'custom':{'keep':1}},{'id':'current','dataset_id':'speaker'}]):
                document=json.dumps(data,ensure_ascii=False).encode();path.write_bytes(document)
                self.assertEqual(data,batch.load_manifest(str(path)));self.assertEqual(document,path.read_bytes())

    def test_batch_cli_bad_initial_manifest_fails_before_training_with_diagnostic(self):
        for document in BAD_DOCUMENTS:
            for dry in (False,True):
                with self.subTest(document=document,dry=dry),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);argv=self.get_batch_args(root)+(['--dry_run'] if dry else [])
                    path=root/'manifest.json';path.write_bytes(document);out=io.StringIO()
                    with patch.object(sys,'argv',argv),patch.object(batch,'train_one') as train,contextlib.redirect_stdout(out):
                        self.assertEqual(1,batch.main())
                    train.assert_not_called();self.assertIn('ERROR: manifest unreadable:',out.getvalue())
                    self.assertEqual(document,path.read_bytes())

    def test_batch_malformed_locked_reload_preserves_concurrent_bytes_and_accounts_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);argv=self.get_batch_args(root);path=root/'manifest.json';path.write_text('[]');out=io.StringIO()
            concurrent=b'[{"id":"human","custom":{"keep":7}},null]'
            adapter=root/'models'/'completed_adapter';adapter.mkdir(parents=True);weights=adapter/'adapter_model.safetensors';weights.write_bytes(b'CPU adapter stand-in')
            def train(*args):
                path.write_bytes(concurrent);return {'id':'completed_adapter','dataset_id':'speaker'}
            with patch.object(sys,'argv',argv),patch.object(batch,'train_one',side_effect=train),contextlib.redirect_stdout(out):
                self.assertEqual(1,batch.main())
            self.assertEqual(concurrent,path.read_bytes());self.assertEqual(b'CPU adapter stand-in',weights.read_bytes())
            self.assertIn('manifest',out.getvalue());self.assertIn('0 trained, 0 skipped, 1 errors',out.getvalue())
            self.assertEqual([],list(root.glob('.tmp_*')))

    def test_batch_missing_initialization_and_legacy_rows_preserved_on_success(self):
        for initial in (None,[{'custom':{'keep':'legacy'}}]):
            with self.subTest(initial=initial),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);argv=self.get_batch_args(root);path=root/'manifest.json'
                if initial is not None:path.write_text(json.dumps(initial))
                result={'id':'speaker_adapter','dataset_id':'speaker','custom':{'new':1}}
                with patch.object(sys,'argv',argv),patch.object(batch,'train_one',return_value=result),contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0,batch.main())
                self.assertEqual((initial or [])+[result],json.loads(path.read_text()))
                self.assertEqual([],list(root.glob('.tmp_*')))

    def test_profiler_bad_manifest_fails_check_dry_and_normal_before_model_or_artifacts(self):
        for document in BAD_DOCUMENTS:
            for mode in ('--check','--dry_run',''):
                with self.subTest(document=document,mode=mode),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);path=root/'manifest.json';path.write_bytes(document)
                    model=root/'model.gguf';model.write_bytes(b'GGUF stand-in');output=root/'profiles.csv';output.write_bytes(b'prior CSV')
                    argv=['voice_profiler.py','--manifest',str(path),'--model',str(model),'--output_csv',str(output)]+([mode] if mode else [])
                    llama=Mock(side_effect=AssertionError('bad manifest reached model'));out=io.StringIO()
                    with patch.object(sys,'argv',argv),patch.dict(sys.modules,{'llama_cpp':SimpleNamespace(Llama=llama)}), \
                         patch.object(profiler,'DEPENDENCY_ERROR',None),patch.object(profiler,'analyze_ref_wav') as acoustic,contextlib.redirect_stdout(out):
                        self.assertEqual(1,profiler.main())
                    self.assertIn('manifest',out.getvalue());llama.assert_not_called();acoustic.assert_not_called()
                    if document in (b'[{"id":"keep"},null]',b'[{},"bad"]',b'[{},[]]',b'[{},true]'):
                        self.assertIn('row 2',out.getvalue())
                    self.assertEqual(document,path.read_bytes());self.assertEqual(b'prior CSV',output.read_bytes());self.assertEqual(b'GGUF stand-in',model.read_bytes())
                    self.assertEqual({'manifest.json','profiles.csv','model.gguf'},{p.name for p in root.iterdir()})

    def test_profiler_empty_and_legacy_shapes_stay_valid_without_model(self):
        for data in ([],[{}, {'id':'legacy'},{'id':'current','voice_profile':'steady','voice_features':{'mean_f0':120}}]):
            for mode in ('--check','--dry_run',''):
                with self.subTest(data=data,mode=mode),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);path=root/'manifest.json';document=json.dumps(data).encode();path.write_bytes(document)
                    model=root/'model.gguf';model.write_bytes(b'GGUF stand-in');output=root/'profiles.csv';output.write_bytes(b'prior CSV')
                    argv=['voice_profiler.py','--manifest',str(path),'--model',str(model),'--output_csv',str(output)]+([mode] if mode else [])
                    llama=Mock(side_effect=AssertionError('no pending rows should load model'))
                    with patch.object(sys,'argv',argv),patch.dict(sys.modules,{'llama_cpp':SimpleNamespace(Llama=llama)}), \
                         patch.object(profiler,'DEPENDENCY_ERROR',None),patch.object(profiler,'analyze_ref_wav') as acoustic,contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(0,profiler.main())
                    llama.assert_not_called();acoustic.assert_not_called();self.assertEqual(document,path.read_bytes())
                    if mode:self.assertEqual(b'prior CSV',output.read_bytes())
                    else:
                        with output.open(newline='') as f:rows=list(csv.DictReader(f))
                        self.assertEqual(['current'] if data else [],[r['id'] for r in rows])

    def test_real_profiler_process_rejects_mixed_rows_without_traceback(self):
        for mode in ('--check','--dry_run',''):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);path=root/'manifest.json';document=b'[{"id":"keep"},null]';path.write_bytes(document)
                model=root/'model.gguf';model.write_bytes(b'GGUF stand-in');output=root/'profiles.csv';output.write_bytes(b'prior CSV')
                proc=subprocess.run([sys.executable,str(ROOT/'tools/voice_lab/voice_profiler.py'),'--manifest',str(path),
                    '--model',str(model),'--output_csv',str(output)]+([mode] if mode else []),capture_output=True,text=True,timeout=30)
                self.assertEqual(1,proc.returncode);self.assertIn('row 2',proc.stdout);self.assertNotIn('Traceback',proc.stderr)
                self.assertEqual(document,path.read_bytes());self.assertEqual(b'prior CSV',output.read_bytes());self.assertEqual(b'GGUF stand-in',model.read_bytes())
