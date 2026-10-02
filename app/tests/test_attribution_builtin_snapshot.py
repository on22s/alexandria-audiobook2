"""Preset fields come from one prompt load per variant."""
import importlib.util
import os
import unittest
from unittest.mock import patch

import attribution_prompt_variants as apv

if os.environ.get('PRESET_SOURCE'):
    spec = importlib.util.spec_from_file_location('preset_saved', os.environ['PRESET_SOURCE'])
    apv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(apv)


class BuiltinPresetSnapshotTests(unittest.TestCase):
    def test_each_preset_uses_one_text_evaluation_and_keeps_order_and_metadata(self):
        calls = []

        def load(variant):
            calls.append(variant)
            return {key: f'{variant}:{len(calls)}:{key}' for key in ('system', 'user', 'example')}

        with patch.object(apv, 'builtin_texts', load):
            result = apv.builtin_presets()
        self.assertEqual(list(apv.USER_VARIANTS), calls)
        for count, (variant, preset) in enumerate(zip(apv.USER_VARIANTS, result), 1):
            self.assertEqual({'name': variant, 'description': apv.VARIANT_DESCRIPTIONS[variant],
                              'variant': variant, 'system_prompt': f'{variant}:{count}:system',
                              'user_prompt': f'{variant}:{count}:user',
                              'example': f'{variant}:{count}:example', 'builtin': True}, preset)

    def test_shipped_preset_fields_match_actual_variant_prompt_texts(self):
        for preset in apv.builtin_presets():
            texts = apv.builtin_texts(preset['variant'])
            self.assertEqual(texts, {'system': preset['system_prompt'],
                                    'user': preset['user_prompt'], 'example': preset['example']})
