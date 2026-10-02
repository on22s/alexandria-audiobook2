"""Structural adapter validity is checked before reuse or installation."""
import builtins
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
from safetensors.numpy import save_file
from safetensors import safe_open
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import promote_adapters as promotion
from tests.test_support import write_test_adapter

spec = importlib.util.spec_from_file_location('adapter_test_batch',ROOT/'tools/voice_lab/batch_train_lora.py')
batch = importlib.util.module_from_spec(spec); spec.loader.exec_module(batch)


def damage(path, kind):
    if kind.startswith('missing_'):
        (path/kind[len('missing_'):]).unlink()
    elif kind == 'truncated_weights':
        weights=path/'adapter_model.safetensors'; weights.write_bytes(weights.read_bytes()[:-1])
    elif kind == 'text_weights': (path/'adapter_model.safetensors').write_bytes(b'weights')
    elif kind == 'empty_weights': save_file({},str(path/'adapter_model.safetensors'))
    elif kind == 'bad_offsets':
        weights=path/'adapter_model.safetensors'; data=bytearray(weights.read_bytes())
        size=struct.unpack('<Q',data[:8])[0]
        header=json.loads(data[8:8+size]); first=next(iter(header.values())); first['data_offsets'][1]+=4
        encoded=json.dumps(header,separators=(',',':')).encode(); padding=(-len(encoded))%8
        weights.write_bytes(struct.pack('<Q',len(encoded)+padding)+encoded+b' '*padding+data[8+size:])
    elif kind == 'directory_weights':
        p=path/'adapter_model.safetensors';p.unlink();p.mkdir()
    elif kind == 'empty_config': (path/'adapter_config.json').write_text('{}')
    elif kind == 'scalar_config': (path/'adapter_config.json').write_text('1')
    elif kind == 'malformed_config': (path/'adapter_config.json').write_text('{broken')
    elif kind == 'unknown_peft': (path/'adapter_config.json').write_text('{"peft_type":"UNKNOWN"}')
    elif kind == 'invalid_lora': (path/'adapter_config.json').write_text('{"peft_type":"LORA","r":2,"lora_alpha":4,"use_dora":true,"megatron_config":{"tensor_model_parallel_size":1}}')
    elif kind == 'scalar_meta': (path/'training_meta.json').write_text('[]')
    elif kind == 'malformed_meta': (path/'training_meta.json').write_text('{broken')
    elif kind == 'invalid_utf8_meta': (path/'training_meta.json').write_bytes(b'\xff')
    else: raise AssertionError(kind)

CORE_DAMAGE=('missing_adapter_config.json','missing_adapter_model.safetensors','truncated_weights',
    'text_weights','empty_weights','bad_offsets','directory_weights','empty_config','scalar_config',
    'malformed_config','unknown_peft','invalid_lora')
META_DAMAGE=('missing_training_meta.json','scalar_meta','malformed_meta','invalid_utf8_meta')


class AdapterReusePublicationTests(unittest.TestCase):
    def test_manifest_and_prefix_resume_reject_bad_core_and_metadata_then_accept_valid_renamed_adapter(self):
        for kind in (*CORE_DAMAGE,*META_DAMAGE):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); renamed=root/'renamed'; prefix=root/'speaker_old'
                for path in (renamed,prefix): write_test_adapter(path); damage(path,kind)
                snapshot={p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
                self.assertIsNone(batch.adapter_exists(str(root),'speaker',[{'id':'renamed','dataset_id':'speaker'}]))
                self.assertIsNone(batch.adapter_exists(str(root),'speaker',[]))
                self.assertEqual({**snapshot,Path('manifest.json.lock'):b''},
                                 {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()})
                write_test_adapter(root/'complete')
                self.assertEqual(str(root/'complete'),batch.adapter_exists(str(root),'speaker',[
                    {'id':'renamed','dataset_id':'speaker'},{'id':'complete','dataset_id':'speaker'}]))
                write_test_adapter(root/'speaker_new')
                self.assertEqual(str(root/'speaker_new'),batch.adapter_exists(str(root),'speaker',[]))

    def test_actual_train_one_rejects_success_exit_with_invalid_files_and_preserves_existing_output(self):
        for kind in (None, *CORE_DAMAGE, *META_DAMAGE):
            for existing in (False, True):
                with self.subTest(kind=kind,existing=existing), tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp); archive=root/'speaker.zip'
                    with zipfile.ZipFile(archive,'w') as z: z.writestr('metadata.jsonl','{"audio":"sample.wav","text":"Sample"}\n')
                    args=SimpleNamespace(datasets_dir=str(root/'datasets'),models_dir=str(root/'models'),
                        python=sys.executable,train_script='cpu_fixture',max_epochs=3,lr=1e-6,lora_r=2,
                        lora_alpha=4,grad_accum=1,language='english',target_loss=4,keep_datasets=False)
                    output=root/'models/voice'
                    if existing:
                        output.mkdir(parents=True);(output/'prior.txt').write_bytes(b'keep existing artifact')
                    class Process:
                        def __init__(self,command,**kwargs):
                            write_test_adapter(output)
                            if kind: damage(output,kind)
                            self.stdout=io.StringIO('[EPOCH] 1/3 avg_loss=2.5000\n');self.returncode=0
                        def wait(self): return 0
                        def poll(self): return 0
                    with patch.object(batch.subprocess,'Popen',Process),redirect_stdout(io.StringIO()):
                        result=batch.train_one(str(archive),'speaker','voice',args)
                    if kind:
                        self.assertIsNone(result)
                        self.assertEqual(existing,output.exists())
                    else:
                        self.assertEqual('voice',result['id']);self.assertEqual(1,result['epochs_run'])
                        with safe_open(output/'adapter_model.safetensors',framework='numpy') as tensors:
                            self.assertEqual(2,len(tensors.keys()))
                        self.assertTrue(batch.is_completed_adapter(str(output)))
                    if existing: self.assertEqual(b'keep existing artifact',(output/'prior.txt').read_bytes())
                    self.assertFalse((root/'datasets/speaker').exists())
                    self.assertTrue(archive.exists())


class PromotionArtifactTests(unittest.TestCase):
    def _fixture(self, root, stack):
        models,source,gates,backups=root/'models',root/'source',root/'gates',root/'backups'
        shipped=models/'voice';write_test_adapter(shipped,value=1);write_test_adapter(source,value=2)
        (source/'identity_check').mkdir();(source/'identity_check/evidence.json').write_text('{"passed":true}')
        manifest=models/'manifest.json';manifest.write_text('[{"id":"voice","gate_ecapa":0.4}]')
        gates.mkdir();(gates/'gate_promote__voice.json').write_text(json.dumps({
            'adapter':str(source),'median_ecapa':.8,'passed':True,'generation_failures':0}))
        for attr,value in (('MODELS',str(models)),('SOURCE',str(root)),('GATES',str(gates)),
            ('BACKUPS',str(backups)),('GATE_PREFIX','gate_promote__')):
            stack.enter_context(patch.object(promotion,attr,value))
        stack.enter_context(patch.object(promotion,'shipped_scores',return_value={'voice':.4}))
        stack.enter_context(patch.object(promotion,'get_adapter_source',return_value=str(source)))
        return shipped,source,manifest,backups

    def test_invalid_required_source_refuses_before_backup_or_installed_changes(self):
        for kind in CORE_DAMAGE:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
                root=Path(tmp);shipped,source,manifest,backups=self._fixture(root,stack)
                damage(source,kind)
                before={p.relative_to(shipped):p.read_bytes() for p in shipped.rglob('*') if p.is_file()}
                meta=manifest.read_bytes()
                with redirect_stdout(io.StringIO()) as log:
                    self.assertEqual(1,promotion.promote(['voice'],'invalid',False))
                self.assertIn('adapter',log.getvalue())
                self.assertEqual(before,{p.relative_to(shipped):p.read_bytes() for p in shipped.rglob('*') if p.is_file()})
                self.assertEqual(meta,manifest.read_bytes());self.assertFalse(backups.exists())

    def test_staged_validation_refuses_source_mutation_and_copy_failure_before_install(self):
        real_copy=promotion.shutil.copy2
        for failure in ('source_mutation','copy'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
                root=Path(tmp);shipped,source,manifest,backups=self._fixture(root,stack)
                before={p.relative_to(shipped):p.read_bytes() for p in shipped.rglob('*') if p.is_file()};meta=manifest.read_bytes()
                def copying(src,dst,*args,**kwargs):
                    if Path(src)==source/'adapter_model.safetensors':
                        if failure=='copy': raise OSError('fixture staging copy failed')
                        Path(src).write_bytes(b'source changed after initial validation')
                    return real_copy(src,dst,*args,**kwargs)
                with patch.object(promotion.shutil,'copy2',side_effect=copying),redirect_stdout(io.StringIO()):
                    self.assertEqual(1,promotion.promote(['voice'],'raced',False))
                self.assertEqual(before,{p.relative_to(shipped):p.read_bytes() for p in shipped.rglob('*') if p.is_file()})
                self.assertEqual(meta,manifest.read_bytes());self.assertFalse(backups.exists())
                self.assertFalse(list((root/'models').glob('.promotion-*')))

    def test_valid_promotion_preserves_original_backup_gate_and_receipt(self):
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);shipped,source,manifest,backups=self._fixture(root,stack)
            before={p.relative_to(shipped):p.read_bytes() for p in shipped.rglob('*') if p.is_file()}
            candidate={p.relative_to(source):p.read_bytes() for p in source.rglob('*') if p.is_file()}
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0,promotion.promote(['voice'],'good',True))
                self.assertFalse(backups.exists());self.assertEqual(before[Path('adapter_model.safetensors')],(shipped/'adapter_model.safetensors').read_bytes())
                self.assertEqual(0,promotion.promote(['voice'],'good',False))
            self.assertEqual(before,{p.relative_to(backups/'good/voice'):p.read_bytes() for p in (backups/'good/voice').rglob('*') if p.is_file()})
            self.assertEqual(candidate,{p.relative_to(shipped):p.read_bytes() for p in shipped.rglob('*') if p.is_file()})
            with safe_open(shipped/'adapter_model.safetensors',framework='numpy') as tensors:
                np.testing.assert_equal(tensors.get_tensor(tensors.keys()[0]),2)
            entry=json.loads(manifest.read_text())[0];self.assertEqual(.8,entry['gate_ecapa']);self.assertEqual('good',entry['retrained_at'])
            receipt=json.loads((backups/'good.json').read_text());self.assertEqual(.4,receipt['adapters'][0]['shipped_ecapa'])
            self.assertEqual(.8,receipt['adapters'][0]['gate_ecapa']);self.assertFalse(list((root/'models').glob('.promotion-*')))


class AdapterValidatorTests(unittest.TestCase):
    def test_missing_dependencies_raise_configuration_error_instead_of_resume_skip_or_promotion_acceptance(self):
        from adapter_artifacts import AdapterValidationDependencyError
        original_import=builtins.__import__
        for unavailable in ('peft','safetensors'):
            with self.subTest(unavailable=unavailable),tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
                root=Path(tmp);shipped,source,manifest,backups=PromotionArtifactTests()._fixture(root,stack)
                def unavailable_import(name,*args,**kwargs):
                    if name==unavailable: raise ModuleNotFoundError('fixture missing '+name)
                    return original_import(name,*args,**kwargs)
                before=manifest.read_bytes()
                with patch('builtins.__import__',side_effect=unavailable_import):
                    with self.assertRaisesRegex(AdapterValidationDependencyError,'validation requires'):
                        batch.is_completed_adapter(str(source))
                    with self.assertRaisesRegex(AdapterValidationDependencyError,'validation requires'):
                        promotion.promote(['voice'],'unconfigured',False)
                self.assertEqual(before,manifest.read_bytes());self.assertFalse(backups.exists())


    def test_required_files_are_readable_and_validation_never_fetches_a_config(self):
        from adapter_artifacts import AdapterValidationError, validate_adapter_artifacts
        real_open=builtins.open
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); write_test_adapter(root)
            original={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
            with patch('peft.config.hf_hub_download',side_effect=AssertionError('must stay local')):
                validate_adapter_artifacts(root,require_training_meta=True)
                for filename in ('adapter_config.json','adapter_model.safetensors','training_meta.json'):
                    def denied(path,*args,**kwargs):
                        if Path(path)==root/filename: raise PermissionError('fixture unreadable file')
                        return real_open(path,*args,**kwargs)
                    with self.subTest(filename=filename),patch('builtins.open',side_effect=denied):
                        with self.assertRaisesRegex(AdapterValidationError,'unreadable'):
                            validate_adapter_artifacts(root,require_training_meta=True)
                        self.assertFalse(batch.is_completed_adapter(str(root)))
                (root/'adapter_config.json').unlink()
                with self.assertRaises(AdapterValidationError): validate_adapter_artifacts(root)
            self.assertEqual({k:v for k,v in original.items() if k!='adapter_config.json'},
                {p.name:p.read_bytes() for p in root.iterdir() if p.is_file()})
