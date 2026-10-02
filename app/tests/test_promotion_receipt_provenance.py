"""Publish real CPU safetensors and reread receipt versus installed artifacts."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import promote_adapters as promotion
from adapter_publication import get_adapter_bundle_sha256
from tests import test_promotion_publication_transaction as transactions
from tests.test_support import write_test_adapter

if os.environ.get("PROMOTION_RECEIPT_BASELINE"):
    spec = importlib.util.spec_from_file_location("receipt_baseline", os.environ["PROMOTION_RECEIPT_BASELINE"])
    promotion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(promotion)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PromotionReceiptProvenanceTests(unittest.TestCase):
    def test_receipt_identifies_selected_campaign_source_gate_and_installed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            fixture=transactions.PromotionPublicationTransactionTests.fixture(self, root)
            models,source,gates,backups=fixture
            before=(models/'manifest.json').read_bytes()
            with patch.multiple(promotion, MODELS=str(models), SOURCE=str(source), GATES=str(gates), BACKUPS=str(backups), GATE_PREFIX='gate_promote__'):
                self.assertEqual(0,promotion.promote(['a','b'],'stamp',False))
            receipt=json.loads((backups/'stamp.json').read_text())
            self.assertEqual('promote',receipt.get('gate_campaign'))
            self.assertEqual('gate_promote__',receipt['gate_prefix'])
            self.assertEqual(hashlib.sha256(before).hexdigest(),receipt['manifest_before_sha256'])
            for row in receipt['adapters']:
                name=row['adapter'];installed=models/name
                self.assertEqual(str((source/name/'adapter').resolve()),row['source'])
                gate=gates/f'gate_promote__{name}.json'
                self.assertEqual({'path':str(gate.resolve()),'sha256':sha(gate)},row['gate_artifact'])
                self.assertEqual(get_adapter_bundle_sha256(installed),row['installed_bundle_sha256'])
                for filename,digest in row['installed_file_sha256'].items():
                    self.assertEqual(sha(installed/filename),digest)
                for filename,digest in row['source_file_sha256'].items():
                    self.assertEqual(sha(source/name/'adapter'/filename),digest)
                    self.assertEqual(sha(installed/filename),digest)
                self.assertIn('adapter_model.safetensors',row['installed_file_sha256'])
                self.assertIn('adapter_config.json',row['installed_file_sha256'])
                self.assertEqual(0.4,row['comparison_shipped_ecapa'])

    def test_late_source_and_gate_changes_cannot_relabel_installed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture=transactions.PromotionPublicationTransactionTests.fixture(self,Path(tmp))
            models,source,gates,backups=fixture
            gate=gates/'gate_promote__a.json';original_gate=sha(gate)
            original_model=sha(source/'a/adapter/adapter_model.safetensors')
            update=promotion.update_manifest
            def change_sources(*args,**kwargs):
                write_test_adapter(source/'a/adapter',value=9)
                gate.write_text('{"passed":false,"median_ecapa":0.1}')
                return update(*args,**kwargs)
            with patch.multiple(promotion,MODELS=str(models),SOURCE=str(source),GATES=str(gates),BACKUPS=str(backups),GATE_PREFIX='gate_promote__'),patch.object(promotion,'update_manifest',side_effect=change_sources):
                self.assertEqual(0,promotion.promote(['a'],'stamp',False))
            row=json.loads((backups/'stamp.json').read_text())['adapters'][0]
            self.assertEqual(original_gate,row.get('gate_artifact',{}).get('sha256'))
            self.assertNotEqual(sha(gate),original_gate)
            self.assertEqual(original_model,sha(models/'a/adapter_model.safetensors'))
            self.assertNotEqual(original_model,sha(source/'a/adapter/adapter_model.safetensors'))
            self.assertEqual(original_model,row['source_file_sha256']['adapter_model.safetensors'])
            self.assertEqual(original_model,row['installed_file_sha256']['adapter_model.safetensors'])
            self.assertEqual(get_adapter_bundle_sha256(models/'a'),row['installed_bundle_sha256'])

    def test_unseen_comparison_evidence_keeps_original_rollback_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture=transactions.PromotionPublicationTransactionTests.fixture(self,Path(tmp))
            models,source,gates,backups=fixture
            (models/'manifest.json').write_text('[{"id":"a","gate_ecapa":0.99}]')
            clean=gates/'unseen_gate__a__clean.json';shipped=gates/'unseen_gate__a__shipped.json'
            clean.write_text(json.dumps({'passed':True,'median_ecapa':0.8,'adapter':str(source/'a/adapter')}))
            shipped.write_text('{"median_ecapa":0.6}')
            with patch.multiple(promotion,MODELS=str(models),SOURCE=str(source),GATES=str(gates),BACKUPS=str(backups),GATE_PREFIX='unseen_gate__'):
                self.assertEqual(0,promotion.promote(['a'],'stamp',False))
                row=json.loads((backups/'stamp.json').read_text())['adapters'][0]
                self.assertEqual(0.99,row['shipped_ecapa'])
                self.assertEqual(0.6,row.get('comparison_shipped_ecapa'))
                self.assertEqual({'path':str(shipped.resolve()),'sha256':sha(shipped),'ecapa':0.6},row['shipped_gate_artifact'])
                self.assertEqual(0,promotion.rollback('stamp'))
            self.assertEqual(0.99,json.loads((models/'manifest.json').read_text())[0]['gate_ecapa'])
