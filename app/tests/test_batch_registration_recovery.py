"""Native adapter registration retries preserve completed weights and provenance."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from lora_evidence import get_file_sha256
from tests.test_support import write_test_adapter
from tests.test_voicelab_pipeline_scripts import batch_train
from tests import test_lora_batch_preflight as fixtures

class BatchRegistrationRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixtures.apply_test_training_dependency_fixture(self)
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.zips=self.root/'zips';self.zips.mkdir();self.zip=self.zips/'Synthetic.zip';fixtures.save_valid_training_zip(self.zip)
        self.models=self.root/'models';self.manifest=self.models/'manifest.json';self.calls=[]
        self.argv=['batch','--zips_dir',str(self.zips),'--models_dir',str(self.models),'--manifest',str(self.manifest),'--datasets_dir',str(self.root/'datasets'),'--python',sys.executable,'--device','cpu']
    def train(self,source,dataset_id,adapter_id,args):
        self.calls.append(adapter_id);folder=self.models/adapter_id;write_test_adapter(folder)
        digest=get_file_sha256(str(folder/'adapter_model.safetensors'))
        (folder/'training_meta.json').write_text(json.dumps({'best_loss':1,'final_loss':1,'num_samples':1,'epochs':1,'checkpoint_sha256':digest}))
        return {'id':adapter_id,'name':dataset_id,'dataset_id':dataset_id,'zip_source':source,'checkpoint_sha256':digest,'sample_count':1}
    def run_batch(self,fail_manifest=False):
        self.last_log = io.StringIO()
        with contextlib.ExitStack() as stack:
            for context in (patch.object(sys,'argv',self.argv),patch.object(batch_train,'get_selected_interpreter_preflight',return_value={'errors':[],'datasets':[]}),patch.object(batch_train,'get_batch_output_preflight',return_value=[]),patch.object(batch_train,'train_one',side_effect=self.train),contextlib.redirect_stdout(self.last_log)):stack.enter_context(context)
            if fail_manifest:stack.enter_context(patch.object(batch_train,'save_manifest',side_effect=OSError('Synthetic manifest failure')))
            return batch_train.main()
    def test_retry_registers_completed_adapter_without_retraining_and_preserves_concurrent_row(self):
        self.assertEqual(1,self.run_batch(True));self.assertEqual(1,len(self.calls));folder=self.models/self.calls[0];prior=(folder/'adapter_model.safetensors').read_bytes()
        batch_train.save_manifest(str(self.manifest),[{'id':'other','dataset_id':'other'}]);self.assertEqual(0,self.run_batch());self.assertEqual(1,len(self.calls))
        entries=batch_train.load_manifest(str(self.manifest));self.assertEqual({'other',self.calls[0]},{row['id'] for row in entries});entry=next(row for row in entries if row['id']==self.calls[0])
        self.assertEqual('synthetic',entry['dataset_id']);self.assertEqual(str(self.zip),entry['zip_source']);self.assertEqual(get_file_sha256(str(folder/'adapter_model.safetensors')),entry['checkpoint_sha256']);self.assertEqual(prior,(folder/'adapter_model.safetensors').read_bytes());self.assertFalse(list((self.models/'.batch-registration').glob('*.json')));self.assertEqual(0,self.run_batch());self.assertEqual(1,len(self.calls))
    def test_changed_source_refuses_recovery_and_keeps_pending_evidence(self):
        self.assertEqual(1,self.run_batch(True));self.zip.write_bytes(self.zip.read_bytes()+b'source changed');self.assertEqual(1,self.run_batch());self.assertEqual(1,len(self.calls));self.assertFalse(self.manifest.exists());self.assertTrue(list((self.models/'.batch-registration').glob('*.json')))
    def test_changed_checkpoint_refuses_recovery_without_retraining_or_registration(self):
        self.assertEqual(1,self.run_batch(True));weights=self.models/self.calls[0]/'adapter_model.safetensors';weights.write_bytes(b'changed checkpoint');self.assertEqual(1,self.run_batch());self.assertEqual(1,len(self.calls));self.assertFalse(self.manifest.exists());self.assertEqual(b'changed checkpoint',weights.read_bytes())
    def test_failed_entry_receipt_write_recovers_from_original_intent_and_training_metadata(self):
        import batch_adapter_registration as registration
        original = registration.atomic_json_write
        def fail_entry(document, path, **kwargs):
            if document.get('entry') is not None:
                raise OSError('Synthetic entry receipt failure')
            return original(document, path, **kwargs)
        with patch.object(registration, 'atomic_json_write', side_effect=fail_entry):
            self.assertEqual(1, self.run_batch())
        record = registration.get_batch_registration_intent(str(self.models), 'synthetic')
        self.assertIsNone(record['entry'])
        self.assertFalse(self.manifest.exists())
        self.assertEqual(0, self.run_batch())
        self.assertIn('Done: 0 trained', self.last_log.getvalue())
        self.assertIn('Recovered registrations without retraining: 1', self.last_log.getvalue())
        self.assertEqual(1, len(self.calls))
        entry = batch_train.load_manifest(str(self.manifest))[0]
        self.assertEqual(1, entry['sample_count'])
        self.assertEqual(1, entry['best_loss'])
        self.assertEqual({}, entry['epoch_losses'])

    def test_structurally_valid_changed_weights_and_matching_metadata_still_refuse_frozen_receipt(self):
        from safetensors.numpy import load_file, save_file
        self.assertEqual(1, self.run_batch(True))
        folder = self.models / self.calls[0]
        weights = folder / 'adapter_model.safetensors'
        arrays = load_file(str(weights))
        name = next(iter(arrays))
        arrays[name] = arrays[name] + 1
        save_file(arrays, str(weights))
        metadata = json.loads((folder / 'training_meta.json').read_text())
        metadata['checkpoint_sha256'] = get_file_sha256(str(weights))
        (folder / 'training_meta.json').write_text(json.dumps(metadata))
        self.assertTrue(batch_train.is_completed_adapter(str(folder)))
        self.assertEqual(1, self.run_batch())
        self.assertEqual(1, len(self.calls))
        self.assertFalse(self.manifest.exists())
        self.assertEqual(metadata['checkpoint_sha256'], get_file_sha256(str(weights)))

    def test_malformed_identity_and_version_refuse_without_consuming_pending_record(self):
        import batch_adapter_registration as registration
        self.assertEqual(1, self.run_batch(True))
        path = Path(registration.get_batch_registration_path(str(self.models), 'synthetic'))
        original = json.loads(path.read_text())
        for changes in ({'version': True}, {'dataset_id': 'other'}, {'adapter_id': 'other_100'}, {'settings': []}):
            with self.subTest(changes=changes):
                path.write_text(json.dumps({**original, **changes}))
                before = path.read_bytes()
                self.assertEqual(1, self.run_batch())
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(1, len(self.calls))
                self.assertFalse(self.manifest.exists())

    def test_manifest_committed_before_cleanup_failure_retries_once_without_duplicate_row(self):
        with patch.object(batch_train, 'clear_batch_registration_intent', side_effect=OSError('Synthetic cleanup failure')):
            self.assertEqual(0, self.run_batch())
        self.assertTrue(list((self.models / '.batch-registration').glob('*.json')))
        self.assertEqual(0, self.run_batch())
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(batch_train.load_manifest(str(self.manifest))))
        self.assertFalse(list((self.models / '.batch-registration').glob('*.json')))
