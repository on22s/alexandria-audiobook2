"""Promotion must install weights from the campaign whose gate was selected."""
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tests.test_promote_adapters import promote_adapters
from tests.test_support import write_test_adapter


class PromotionCampaignSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.roots = {}
        for attr in ('SOURCE', 'DECONTAMINATE_SOURCE', 'REFERENCE_RANK1_SOURCE',
                     'REFERENCE_RANK2_SOURCE', 'GOAL27_SMALL_SOURCE'):
            root = self.root / attr
            root.mkdir()
            self.roots[attr] = root
            self.stack.enter_context(patch.object(promote_adapters, attr, str(root)))
        self.gates = self.root / 'gates'
        self.gates.mkdir()
        self.models = self.root / 'models'
        self.models.mkdir()
        self.backups = self.root / 'backups'
        for attr, value in (('GATES', self.gates), ('MODELS', self.models), ('BACKUPS', self.backups)):
            self.stack.enter_context(patch.object(promote_adapters, attr, str(value)))
        self.stack.enter_context(patch.object(promote_adapters, 'shipped_scores', return_value={'voice': 0.4}))

    def make_adapter(self, attr):
        target = self.roots[attr] / 'voice' / 'adapter'
        target.mkdir(parents=True)
        write_test_adapter(target)
        return target

    def gate(self, campaign, adapter=None):
        prefix = promote_adapters.GATE_CAMPAIGNS[campaign]
        self.stack.enter_context(patch.object(promote_adapters, 'GATE_PREFIX', prefix))
        record = {'passed': True, 'median_ecapa': 0.8, 'generation_failures': 0}
        if adapter is not None:
            record['adapter'] = str(adapter)
        (self.gates / (prefix + 'voice.json')).write_text(json.dumps(record))

    def test_rank_campaign_refuses_other_campaign_gate_path_and_fallback(self):
        for selected, wrong_attr in (('reference-rank1', 'REFERENCE_RANK2_SOURCE'),
                                     ('reference-rank2', 'REFERENCE_RANK1_SOURCE')):
            adapter = self.make_adapter(wrong_attr)
            for explicit in (True, False):
                with self.subTest(selected=selected, explicit=explicit):
                    self.gate(selected, adapter if explicit else None)
                    self.assertIsNone(promote_adapters.get_adapter_source('voice'))
            shutil.rmtree(adapter.parent)

    def test_correct_rank_campaign_resolves_explicit_and_legacy_paths(self):
        for selected, attr in (('reference-rank1', 'REFERENCE_RANK1_SOURCE'),
                               ('reference-rank2', 'REFERENCE_RANK2_SOURCE')):
            adapter = self.make_adapter(attr)
            for explicit in (True, False):
                with self.subTest(selected=selected, explicit=explicit):
                    self.gate(selected, adapter if explicit else None)
                    self.assertEqual(str(adapter), promote_adapters.get_adapter_source('voice'))

    def test_rank_campaign_refuses_symlink_to_other_campaign(self):
        wrong = self.make_adapter('REFERENCE_RANK2_SOURCE')
        linked = self.roots['REFERENCE_RANK1_SOURCE'] / 'voice' / 'adapter'
        linked.parent.mkdir()
        linked.symlink_to(wrong, target_is_directory=True)
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                self.gate('reference-rank1', linked if explicit else None)
                self.assertIsNone(promote_adapters.get_adapter_source('voice'))

    def test_wrong_rank_gate_cannot_publish_or_backup_installed_weights(self):
        wrong = self.make_adapter('REFERENCE_RANK2_SOURCE')
        installed = self.models / 'voice'
        installed.mkdir()
        write_test_adapter(installed)
        manifest = self.models / 'manifest.json'
        manifest.write_text(json.dumps([{'id': 'voice', 'gate_ecapa': 0.4}]))
        before = {p.name: p.read_bytes() for p in installed.iterdir() if p.is_file()}
        manifest_before = manifest.read_bytes()
        self.gate('reference-rank1', wrong)
        self.assertEqual(1, promote_adapters.promote(['voice'], 'wrong-rank', False))
        self.assertEqual(before, {p.name: p.read_bytes() for p in installed.iterdir() if p.is_file()})
        self.assertEqual(manifest_before, manifest.read_bytes())
        self.assertFalse(self.backups.exists())

    def test_correct_rank_campaign_publishes_selected_bytes_and_keeps_backup(self):
        selected = self.make_adapter('REFERENCE_RANK1_SOURCE')
        write_test_adapter(selected, value=2.0)
        wrong = self.make_adapter('REFERENCE_RANK2_SOURCE')
        write_test_adapter(wrong, value=3.0)
        installed = self.models / 'voice'
        write_test_adapter(installed, value=1.0)
        weights = 'adapter_model.safetensors'
        old_bytes = (installed / weights).read_bytes()
        (self.models / 'manifest.json').write_text(json.dumps([{'id': 'voice', 'gate_ecapa': 0.4}]))
        self.gate('reference-rank1', selected)
        self.assertEqual(0, promote_adapters.promote(['voice'], 'correct-rank', False))
        self.assertEqual((selected / weights).read_bytes(), (installed / weights).read_bytes())
        self.assertNotEqual((wrong / weights).read_bytes(), (installed / weights).read_bytes())
        self.assertEqual(old_bytes, (self.backups / 'correct-rank' / 'voice' / weights).read_bytes())
        self.assertEqual(0.8, json.loads((self.models / 'manifest.json').read_text())[0]['gate_ecapa'])
