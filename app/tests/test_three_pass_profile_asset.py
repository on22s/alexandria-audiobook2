"""Measured-profile deployment errors must stop rather than choose generic defaults."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import three_pass_generate as tp

if os.environ.get('THREE_PASS_PROFILE_SOURCE'):
    spec = importlib.util.spec_from_file_location('tp_profile_saved', os.environ['THREE_PASS_PROFILE_SOURCE'])
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)


class ThreePassProfileAssetTests(unittest.TestCase):
    def test_missing_corrupt_and_wrong_shape_assets_report_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profiles.json'
            for raw in (None, b'{bad', b'[]', b'null', b'{"model": 3}', b'\xff'):
                with self.subTest(raw=raw):
                    if raw is not None:
                        path.write_bytes(raw)
                    with self.assertRaisesRegex(ValueError, str(path)):
                        tp.load_default_model_profiles(str(path))

    def test_cli_stops_before_model_healing_or_client_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.txt'
            source.write_text('A short source.', encoding='utf-8')
            asset = Path(tmp) / 'missing.json'
            with patch.object(tp.sys, 'argv', ['three_pass_generate', str(source)]), \
                 patch.object(tp, 'load_app_config', return_value={'llm_local':{'model_name':'fixture'}}), \
                 patch.object(tp, 'DEFAULT_MODEL_PROFILES_PATH', str(asset)), \
                 patch.object(tp, 'ensure_ideal_settings') as heal, \
                 patch.object(tp, 'make_run_client') as client:
                with self.assertRaises(SystemExit) as exited:
                    tp.main()
                self.assertEqual(1, exited.exception.code)
                heal.assert_not_called()
                client.assert_not_called()

    def test_shipped_measured_defaults_and_per_key_override_are_preserved(self):
        asset = Path(__file__).parent.parent / 'three_pass_model_profiles.json'
        expected = json.loads(asset.read_text())
        self.assertTrue(expected)
        self.assertEqual(expected, tp.load_default_model_profiles(str(asset)))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profiles.json'
            measured = {'fixture':{'chunk_size':2400, 'segment_output_ratio':4.0}}
            path.write_text(json.dumps(measured))
            loaded = tp.load_default_model_profiles(str(path))
            self.assertEqual({'chunk_size':2400, 'segment_output_ratio':5.0},
                             tp.resolve_model_profile('fixture', {'fixture':{'segment_output_ratio':5.0}}, loaded))
            self.assertEqual(measured, loaded)
