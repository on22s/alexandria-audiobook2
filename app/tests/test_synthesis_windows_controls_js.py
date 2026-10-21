"""Actual Setup helper roundtrips the authoritative server policy/capabilities."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from synthesis_settings import get_synthesis_window_settings
from tts_capabilities import get_tts_capability_modes


class SynthesisWindowControlsTests(unittest.TestCase):
    def test_server_policy_roundtrip_disabled_preservation_errors_and_labels(self):
        app = Path(__file__).resolve().parents[1]
        modes = get_tts_capability_modes()
        settings = get_synthesis_window_settings({'enabled': True, 'budgets': {
            f'{mode}.{category}': {'max_chars': 80 + index, 'max_words': None if index % 2 else 20}
            for mode, capabilities in modes.items() for index, category in enumerate(capabilities['voice_categories'])
        }, 'seams': {'sentence_gap_ms': 0, 'clause_gap_ms': 17, 'paragraph_gap_ms': None,
                     'max_edge_trim_ms': 23, 'fade_ms': 5, 'continuation_crossfade_ms': 9,
                     'quiet_threshold_db': -42.125}})
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)/'settings.json'
            fixture.write_text(json.dumps(dict(settings=settings, modes=modes)))
            result = subprocess.run(['node', str(app/'tests/synthesis_windows_controls_fixture.js'),
                str(app/'static/js/app-synthesis-windows.js'), str(fixture), str(app/'static/js/app-core.js')],
                capture_output=True, text=True, timeout=20, check=True)
        evidence = json.loads(result.stdout)
        self.assertEqual(19, evidence['passed'])
        self.assertEqual(settings, get_synthesis_window_settings(evidence['roundtrip']))
        changed = get_synthesis_window_settings(evidence['changed'])
        self.assertEqual(777, changed['budgets']['local.custom']['max_chars'])
        self.assertEqual(settings['budgets']['external.clone'], changed['budgets']['external.clone'])
