"""Explicit window controls preserve legacy model evidence while disabled."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from pydantic import ValidationError
from config_settings import TTSConfig, load_app_config_result, _get_field_adapter
from lora_evidence import get_tts_model_settings
from synthesis_settings import (get_synthesis_budget_keys, get_synthesis_window_settings,
                                get_enabled_synthesis_window_settings,
                                get_synthesis_recovery_settings,
                                get_enabled_synthesis_recovery_settings)
from tts import TTSEngine


class SynthesisRecoverySettingsTests(unittest.TestCase):
    def test_default_recovery_is_off_and_saved_controls_are_independent_copies(self):
        self.assertEqual({'enabled': False, 'max_audio_mb': 256}, get_synthesis_recovery_settings())
        value = {'enabled': True, 'max_audio_mb': 64}; original = copy.deepcopy(value)
        settings = get_enabled_synthesis_recovery_settings(value)
        self.assertEqual(original, value)
        settings['max_audio_mb'] = 128
        self.assertEqual(original, value)
        self.assertEqual(original, get_enabled_synthesis_recovery_settings(value))
        self.assertIsNone(get_enabled_synthesis_recovery_settings({'max_audio_mb': 64}))

    def test_recovery_policy_requires_bounded_integer_budget_and_boolean_enable(self):
        for budget in (16, 4096):
            self.assertEqual(budget, get_synthesis_recovery_settings({'max_audio_mb': budget})['max_audio_mb'])
        for value in ([], '', 1, {'enabled': 1}, {'enabled': 'true'}, {'unknown': 1},
                      *({'max_audio_mb': budget} for budget in (True, 0, 15, 4097, 16.0, '16', None))):
            with self.subTest(value=value), self.assertRaises(ValueError):
                get_synthesis_recovery_settings(value)

    def test_execution_policy_never_changes_acoustic_or_lora_model_identity(self):
        expected = get_tts_model_settings({'tts': {}})
        for value in (None, {}, {'enabled': True, 'max_audio_mb': 16}, {'max_audio_mb': 4096}):
            with self.subTest(value=value):
                config = {'tts': {'synthesis_recovery': value}}
                self.assertEqual(expected, get_tts_model_settings(config))
                self.assertEqual(expected, TTSEngine(config).get_model_settings())

    def test_recovery_schema_and_shape_safe_loader_preserve_strict_policy(self):
        value = {'enabled': True, 'max_audio_mb': 32}
        adapter = _get_field_adapter(TTSConfig, 'synthesis_recovery')
        self.assertEqual(get_synthesis_recovery_settings(), TTSConfig().synthesis_recovery)
        self.assertEqual(value, adapter.validate_python(value))
        for bad in ({'enabled': 'true'}, {'max_audio_mb': 16.0}, {'max_audio_mb': '16'}):
            with self.subTest(value=bad), self.assertRaises(ValidationError):
                adapter.validate_python(bad)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'tts': {'synthesis_recovery': value, 'language': 'French'}}))
            result = load_app_config_result(str(path))
            self.assertEqual(value, result.data['tts']['synthesis_recovery'])
            path.write_text(json.dumps({'tts': {'synthesis_recovery': {'max_audio_mb': 0}, 'language': 'French'}}))
            result = load_app_config_result(str(path))
            self.assertNotIn('synthesis_recovery', result.data['tts'])
            self.assertEqual('French', result.data['tts']['language'])
            self.assertIn('tts.synthesis_recovery', {warning.field for warning in result.warnings})


class SynthesisSettingsTests(unittest.TestCase):
    def policy(self):
        return dict(enabled=True, budgets={'local.custom': {'max_chars': 80}}, seams={'fade_ms': 5})

    def test_disabled_controls_omit_model_identity_and_preserve_legacy_shape(self):
        legacy = dict(mode='external', device='auto', compile_codec=False, max_new_tokens=2048, language='English')
        for value in (None, {}, {'enabled': False}, {**self.policy(), 'enabled': False}):
            with self.subTest(value=value):
                config = {'tts': {'synthesis_windows': value}}
                self.assertEqual(legacy, get_tts_model_settings(config))
                self.assertEqual(legacy, TTSEngine(config).get_model_settings())
                self.assertIsNone(get_enabled_synthesis_window_settings(value))

    def test_enabled_policy_is_canonical_copied_and_part_of_actual_engine_identity(self):
        value = self.policy(); before = copy.deepcopy(value)
        normalized = get_synthesis_window_settings(value)
        self.assertEqual(before, value)
        self.assertEqual({'max_chars': 80, 'max_words': None}, normalized['budgets']['local.custom'])
        self.assertEqual(0, normalized['seams']['max_edge_trim_ms'])
        self.assertIsNone(normalized['seams']['sentence_gap_ms'])
        engine = TTSEngine({'tts': {'synthesis_windows': value}})
        expected = get_tts_model_settings({'tts': {'synthesis_windows': value}})
        self.assertEqual(expected, engine.get_model_settings())
        exposed = engine.get_model_settings(); exposed['synthesis_windows']['budgets']['local.custom']['max_chars'] = 1
        value['budgets']['local.custom']['max_chars'] = 2
        self.assertEqual(expected, engine.get_model_settings())

    def test_budget_matrix_comes_from_existing_backend_capabilities(self):
        keys = get_synthesis_budget_keys()
        self.assertEqual(8, len(keys)); self.assertNotIn('external.lora', keys)
        settings = dict(enabled=True, budgets={key: {'max_chars': 120, 'max_words': 20} for key in keys})
        self.assertEqual(set(keys), set(get_synthesis_window_settings(settings)['budgets']))

    def test_invalid_policy_refuses_unknown_categories_shapes_and_unbounded_values(self):
        values = [[], {'unknown': 1}, {'enabled': 1}, {'enabled': True},
                  {'budgets': {'external.design': {'max_chars': 10}}}, {'budgets': []},
                  {'budgets': {'local.custom': {'max_chars': True}}},
                  {'budgets': {'local.custom': {'max_chars': 1.5}}},
                  {'budgets': {'local.custom': {'max_chars': 1000001}}},
                  {'budgets': {'local.custom': {'max_chars': 10, 'max_words': 0}}},
                  {'budgets': {'local.custom': {'max_chars': 10, 'extra': 1}}},
                  {'seams': {'sentence_gap_ms': 5001}}, {'seams': {'fade_ms': True}},
                  {'seams': {'quiet_threshold_db': float('nan')}}, {'seams': {'unknown': 1}}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError): get_synthesis_window_settings(value)

    def test_schema_and_shape_safe_loader_share_the_same_validation(self):
        value = self.policy()
        adapter = _get_field_adapter(TTSConfig, 'synthesis_windows')
        self.assertEqual(get_synthesis_window_settings(), TTSConfig().synthesis_windows)
        self.assertEqual(get_synthesis_window_settings(), adapter.validate_python(TTSConfig().synthesis_windows))
        self.assertEqual(get_synthesis_window_settings({'seams': {'quiet_threshold_db': -42}}),
                         get_synthesis_window_settings({'seams': {'quiet_threshold_db': -42.0}}))
        self.assertEqual(get_synthesis_window_settings(value), adapter.validate_python(value))
        self.assertEqual(adapter.validate_python(value), TTSConfig(synthesis_windows=value).synthesis_windows)
        with self.assertRaises(ValidationError): adapter.validate_python({'enabled': True})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'config.json'
            path.write_text(json.dumps({'tts': {'synthesis_windows': value, 'language': 'French'}}))
            result = load_app_config_result(str(path))
            self.assertEqual(adapter.validate_python(value), result.data['tts']['synthesis_windows'])
            self.assertFalse(result.warnings)
            path.write_text(json.dumps({'tts': {'synthesis_windows': {'enabled': True}, 'language': 'French'}}))
            result = load_app_config_result(str(path))
            self.assertNotIn('synthesis_windows', result.data['tts']); self.assertEqual('French', result.data['tts']['language'])
            self.assertIn('tts.synthesis_windows', {warning.field for warning in result.warnings})
