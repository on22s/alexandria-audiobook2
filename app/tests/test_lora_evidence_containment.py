"""Correct content hashes cannot authorize evidence outside a checkpoint."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
import lora_evidence as evidence


def make_checkpoint(root):
    root.mkdir()
    (root/'adapter_model.safetensors').write_bytes(b'known fixture checkpoint bytes')
    sf.write(root/'ref_sample.wav',np.full(200,.1),16000)
    sf.write(root/'probe.wav',np.full(200,.2),16000)
    thresholds={'minimum_similarity':.5}
    return {'version':2,'thresholds':thresholds,
            'evidence':{'checkpoint_sha256':evidence.get_file_sha256(root/'adapter_model.safetensors'),
                        'reference_audio_sha256':evidence.get_file_sha256(root/'ref_sample.wav'),
                        'evaluation_spec_sha256':evidence.get_evaluation_spec_sha256([('probe','known line')],0,thresholds)},
            'probes':[{'id':'probe','text':'known line','seed':0,'audio_file':'probe.wav',
                       'audio_sha256':evidence.get_file_sha256(root/'probe.wav')}]}


class CheckpointEvidenceContainmentTests(unittest.TestCase):
    def test_fixed_evidence_outside_symlinks_reject_even_with_matching_hashes(self):
        for filename in ('adapter_model.safetensors','ref_sample.wav'):
            with self.subTest(filename=filename),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'checkpoint';result=make_checkpoint(root)
                outside=Path(tmp)/('outside-'+filename);outside.write_bytes((root/filename).read_bytes())
                before=outside.read_bytes()
                (root/filename).unlink();(root/filename).symlink_to(outside)
                real_hash=evidence.get_file_sha256
                with patch.object(evidence,'get_file_sha256',wraps=real_hash) as hashing:
                    error=evidence.get_evidence_error(result,str(root))
                self.assertIsNotNone(error,'matching hash accepted an outside evidence file')
                self.assertTrue(all(Path(call.args[0]).resolve()!=outside.resolve() for call in hashing.call_args_list))
                self.assertEqual(before,outside.read_bytes())

    def test_internal_symlinks_keep_valid_spec_and_stale_hash_still_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'checkpoint';result=make_checkpoint(root)
            self.assertIsNone(evidence.get_evidence_error(result,str(root)))
            for filename in ('adapter_model.safetensors','ref_sample.wav','probe.wav'):
                saved=root/('actual-'+filename);(root/filename).rename(saved);(root/filename).symlink_to(saved)
            self.assertIsNone(evidence.get_evidence_error(result,str(root)))
            (root/'actual-adapter_model.safetensors').write_bytes(b'changed checkpoint')
            self.assertIn('does not match',evidence.get_evidence_error(result,str(root)))

    def test_invalid_path_shapes_and_cross_drive_boundary_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'checkpoint';result=make_checkpoint(root)
            for value in (None,3,[],{},''):
                with self.subTest(value=value):
                    result['probes'][0]['audio_file']=value
                    self.assertIn('invalid',evidence.get_evidence_error(result,str(root)))
            with patch.object(evidence.os.path,'commonpath',side_effect=ValueError('different drives')):
                self.assertIsNone(evidence.get_checkpoint_evidence_path(str(root),'ref_sample.wav'))

    def test_probe_symlink_outside_and_missing_fixed_file_remain_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'checkpoint';result=make_checkpoint(root)
            outside=Path(tmp)/'probe.wav';outside.write_bytes((root/'probe.wav').read_bytes())
            (root/'probe.wav').unlink();(root/'probe.wav').symlink_to(outside)
            self.assertIsNotNone(evidence.get_evidence_error(result,str(root)))
            (root/'adapter_model.safetensors').unlink()
            self.assertIn('missing',evidence.get_evidence_error(result,str(root)))
