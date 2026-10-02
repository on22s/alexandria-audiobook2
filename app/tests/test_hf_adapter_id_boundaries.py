"""Real outside complete adapters cannot satisfy a built-in ID lookup."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hf_utils


class AdapterIDBoundaryTests(unittest.TestCase):
    def test_completed_outside_adapter_cannot_be_reached_by_unsafe_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root/'builtin'
            builtin.mkdir()
            outside = root/'outside'
            outside.mkdir()
            sizes = {}
            for name in hf_utils.REQUIRED_ADAPTER_FILES:
                payload = (name+' fixture').encode()
                (outside/name).write_bytes(payload)
                sizes[name] = len(payload)
            (outside/hf_utils._DOWNLOAD_MARKER).write_text(json.dumps(sizes))
            # Positive control proves the outside fixture meets the current
            # completion contract; rejection is its ID boundary, not absent files.
            self.assertTrue(hf_utils.is_adapter_downloaded('outside', str(root)))
            before = {p.name:p.read_bytes() for p in outside.iterdir()}
            for adapter_id in ('../outside', str(outside), '..\\outside', '.', '..', '', None, 4):
                with self.subTest(adapter_id=adapter_id):
                    self.assertFalse(hf_utils.is_adapter_downloaded(adapter_id, str(builtin)))
                    with self.assertRaisesRegex(ValueError, 'Invalid built-in adapter ID'):
                        hf_utils.download_builtin_adapter(adapter_id, str(builtin))
            self.assertEqual(before, {p.name:p.read_bytes() for p in outside.iterdir()})
            self.assertEqual([], list(builtin.iterdir()))

    def test_one_validator_policy_blocks_both_consumers_before_disk_or_hub(self):
        with patch.object(hf_utils, '_is_safe_adapter_id', return_value=False) as policy, \
                patch.object(hf_utils, 'safe_load_json') as read:
            self.assertFalse(hf_utils.is_adapter_downloaded('formerly_safe', 'unused'))
            with self.assertRaisesRegex(ValueError, 'Invalid built-in adapter ID'):
                hf_utils.download_builtin_adapter('formerly_safe', 'unused')
            self.assertEqual(2, policy.call_count)
            read.assert_not_called()
