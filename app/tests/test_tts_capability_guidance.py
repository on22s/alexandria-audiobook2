"""One dispatch matrix drives the UI contract without altering configuration."""
import copy
import unittest
from unittest.mock import patch
import tts
from tts_capabilities import get_tts_backend_capabilities, get_tts_capability_modes


class TtsCapabilityGuidanceTests(unittest.TestCase):
    def test_descriptor_and_native_dispatch_agree_for_every_voice_category(self):
        modes = get_tts_capability_modes()
        for mode, descriptor in modes.items():
            engine = tts.TTSEngine({'tts': {'mode': mode}})
            for category in ('custom', 'clone', 'lora', 'design', 'ensemble'):
                with self.subTest(mode=mode, category=category):
                    if category in descriptor['voice_categories']:
                        self.assertEqual(mode, engine.get_voice_backend(category))
                    else:
                        with self.assertRaises(tts.UnsupportedVoiceBackendError):
                            engine.get_voice_backend(category)
            self.assertEqual(descriptor['voice_types']['lora'], descriptor['voice_types']['builtin_lora'])
            self.assertEqual('buffered', descriptor['output_mode'])
            self.assertFalse(descriptor['speed_control'])

    def test_fresh_descriptors_cannot_mutate_dispatch_and_unknown_mode_refuses(self):
        before = get_tts_capability_modes()
        changed = copy.deepcopy(before)
        changed['external']['voice_categories'].append('lora')
        descriptor = get_tts_backend_capabilities('external')
        descriptor['voice_categories'].append('lora')
        descriptor['controls']['device'] = True
        self.assertEqual(before, get_tts_capability_modes())
        with self.assertRaises(ValueError):
            get_tts_backend_capabilities('unknown')
        with self.assertRaises(tts.UnsupportedVoiceBackendError):
            tts.TTSEngine({'tts': {'mode': 'unknown'}}).get_voice_backend('custom')

    def test_descriptor_never_initializes_engine_or_contacts_a_server(self):
        with patch.object(tts.TTSEngine, '__init__', side_effect=AssertionError('initialization')):
            result = get_tts_capability_modes()
        self.assertIn('not verified', result['external']['language_note'])
