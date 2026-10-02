"""CPU PCM evaluation preserves evidence while avoiding repeated production I/O."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from tests import test_lora_evaluation as lora_fixture

evaluation = lora_fixture.evaluation


class EvaluationIoTests(unittest.TestCase):
    def run_manifest(self, entries, cached=False, cached_tail=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            adapter,valid,engine_type=lora_fixture.EvaluationCacheShapeTests.make_valid_evidence(self,root)
            if cached:(adapter/'evaluation.json').write_text(json.dumps(valid))
            if cached_tail:
                import shutil
                other=root/'cached_voice';shutil.copytree(adapter,other)
                (other/'evaluation.json').write_text(json.dumps(valid))
            manifest=root/'manifest.json';manifest.write_text(json.dumps(entries));before=(adapter/'adapter_model.safetensors').read_bytes()
            classifier=SimpleNamespace(from_hparams=lambda **kwargs:SimpleNamespace(eval=lambda:None))
            argv=['evaluate_lora.py','--manifest',str(manifest),'--models-dir',tmp,'--config',str(root/'missing-config.json'),'--device','cpu']
            with patch.dict(sys.modules,{'torch':ModuleType('torch'),'speechbrain':ModuleType('speechbrain'),'speechbrain.inference':ModuleType('speechbrain.inference'),'speechbrain.inference.speaker':SimpleNamespace(EncoderClassifier=classifier)}),patch.object(sys,'argv',argv),patch.object(evaluation,'resolve_device',return_value='cpu'),patch.object(evaluation,'TTSEngine',engine_type),patch.object(evaluation,'get_speaker_similarity',return_value=.9),patch.object(evaluation,'apply_evaluation_seed'),patch.object(evaluation,'get_checkpoint_sha256',wraps=evaluation.get_checkpoint_sha256) as hash_file,patch.object(evaluation,'atomic_json_write',wraps=evaluation.atomic_json_write) as write,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0,evaluation.main())
            writes=[call for call in write.call_args_list if str(call.args[1])==str(manifest)]
            with self.subTest(kind="manifest_writes"):self.assertEqual(2 if cached_tail else 1,len(writes))
            if not cached and any(row.get('id')=='voice' for row in entries):
                with self.subTest(kind="production_hash_reads"):
                    self.assertEqual(1,len([call for call in hash_file.call_args_list if str(call.args[0])==str(adapter)]))
                result=json.loads((adapter/'evaluation.json').read_text())
                self.assertEqual(result['evidence']['checkpoint_sha256'],result['checkpoint_sha256'])
                self.assertTrue(evaluation.is_complete_evaluation(result,str(adapter)))
            if cached:self.assertIn('evaluation',json.loads(manifest.read_text())[0])
            if cached_tail:self.assertIn('evaluation',json.loads(manifest.read_text())[-1])
            self.assertEqual(before,(adapter/'adapter_model.safetensors').read_bytes())

    def test_new_evaluation_hashes_production_once_and_saves_manifest_once(self):self.run_manifest([{'id':'voice'}])
    def test_cached_updates_are_still_persisted(self):self.run_manifest([{'id':'voice'}],cached=True)
    def test_empty_manifest_still_has_one_final_write(self):self.run_manifest([])

    def test_cached_tail_updates_after_an_evaluated_row_are_not_lost(self):
        self.run_manifest([{'id':'voice'},{'id':'cached_voice'}],cached_tail=True)

    def test_hash_reuse_is_scoped_to_unchanged_native_file_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter=Path(tmp);weights=adapter/'adapter_model.safetensors';weights.write_bytes(b'original checkpoint')
            snapshot=(evaluation.get_checkpoint_sha256(tmp),evaluation.get_checkpoint_file_identity(tmp))
            with patch.object(evaluation,'get_checkpoint_sha256',wraps=evaluation.get_checkpoint_sha256) as hash_file:
                result=evaluation.partition_unique_candidates(tmp,str(adapter/'candidates'),production_snapshot=snapshot)
                self.assertEqual(snapshot[0],result[0]);hash_file.assert_not_called()
            version=weights.stat();weights.write_bytes(b'changed! checkpoint')
            self.assertEqual(version.st_size,weights.stat().st_size)
            os.utime(weights,ns=(version.st_atime_ns,version.st_mtime_ns))
            with patch.object(evaluation,'get_checkpoint_sha256',wraps=evaluation.get_checkpoint_sha256) as hash_file:
                changed=evaluation.partition_unique_candidates(tmp,str(adapter/'candidates'),production_snapshot=snapshot)
                hash_file.assert_called_once_with(tmp)
            self.assertNotEqual(snapshot[0],changed[0]);self.assertEqual(weights.read_bytes(),b'changed! checkpoint')
