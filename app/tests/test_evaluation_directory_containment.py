import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.test_lora_evaluation import evaluation


class EvaluationDirectoryContainmentTests(unittest.TestCase):
    def test_escaped_ids_and_symlink_adapter_reject_before_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp,'models');root.mkdir()
            outside=Path(tmp,'outside');outside.mkdir();(outside/'ref_sample.wav').write_bytes(b'fixture reference')
            (root/'linked').symlink_to(outside,target_is_directory=True)
            for adapter_id in ('../outside',str(outside),'linked'):
                with self.subTest(adapter_id=adapter_id):
                    engine=Mock();engine.generate_voice.return_value=False
                    with patch.object(evaluation,'apply_evaluation_seed') as seed:
                        with self.assertRaises(ValueError):
                            evaluation.evaluate_adapter({'id':adapter_id},str(root),engine,object(),'cpu')
                        seed.assert_not_called();engine.generate_voice.assert_not_called()
            self.assertEqual(b'fixture reference',(outside/'ref_sample.wav').read_bytes())

    def test_candidate_cleanup_rejects_escape_without_deleting_owned_or_outside_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter=Path(tmp,'voice');candidates=adapter/'candidates';candidates.mkdir(parents=True)
            owned=candidates/'owned';owned.mkdir();(owned/'marker').write_text('owned')
            outside=Path(tmp,'outside');outside.mkdir();(outside/'marker').write_text('outside')
            for name in ('../../outside',str(outside)):
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):evaluation.cleanup_candidates(str(adapter),None,['owned',name])
                    self.assertTrue((owned/'marker').is_file());self.assertTrue((outside/'marker').is_file())

    def test_symlink_candidates_root_rejects_before_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter=Path(tmp,'voice');adapter.mkdir();outside=Path(tmp,'outside');child=outside/'candidate';child.mkdir(parents=True);(child/'marker').write_text('outside')
            (adapter/'candidates').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError):evaluation.cleanup_candidates(str(adapter),None,['candidate'])
            self.assertEqual('outside',(child/'marker').read_text())

    def test_override_and_partition_cannot_escape_before_generation_or_hashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            models=Path(tmp,'models');adapter=models/'voice';adapter.mkdir(parents=True)
            outside=Path(tmp,'outside');outside.mkdir()
            engine=Mock()
            with patch.object(evaluation,'get_checkpoint_sha256') as hashing:
                with self.assertRaises(ValueError):evaluation.partition_unique_candidates(str(adapter),str(outside))
                hashing.assert_not_called()
            with self.assertRaises(ValueError):
                evaluation.evaluate_adapter({'id':'voice'},str(models),engine,object(),'cpu',str(outside))
            engine.generate_voice.assert_not_called()

    def test_main_rejects_manifest_paths_before_model_initialization(self):
        import json, sys
        from types import ModuleType
        with tempfile.TemporaryDirectory() as tmp:
            models=Path(tmp,'models');models.mkdir();manifest=Path(tmp,'manifest.json')
            manifest.write_text(json.dumps([{'id':'../outside'}]))
            argv=['evaluate_lora.py','--manifest',str(manifest),'--models-dir',str(models),'--config','unused']
            with patch.dict(sys.modules,{'torch':ModuleType('torch')}), patch.object(sys,'argv',argv), \
                 patch.object(evaluation,'resolve_device') as device, patch.object(evaluation,'TTSEngine') as engine:
                with self.assertRaises(ValueError):evaluation.main()
                device.assert_not_called();engine.assert_not_called()

    def test_internal_candidate_symlink_does_not_delete_its_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp,'candidates');target=root/'target';target.mkdir(parents=True)
            (target/'marker').write_text('owned');(root/'alias').symlink_to(target,target_is_directory=True)
            with self.assertRaises(ValueError):evaluation.cleanup_candidates(tmp,None,['alias'])
            self.assertEqual('owned',(target/'marker').read_text())
