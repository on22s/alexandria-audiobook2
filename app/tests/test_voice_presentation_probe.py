"""Known accepted/rejected cases calibrate the voice-presentation instrument."""
import json
from pathlib import Path
import unittest
from experiments.voice_presentation_probe import get_probe_verdict


class VoicePresentationProbeTests(unittest.TestCase):
    def test_rejects_identity_only_wrong_speaker_and_invented_evidence(self):
        cases=json.loads((Path(__file__).parent.parent/'fixtures/voice_presentation_733.json').read_text())
        for case in cases:
            with self.subTest(case=case['id']):
                if case['presentation']=='unknown':
                    self.assertTrue(get_probe_verdict(case, {'voice_presentation':'unknown','evidence':''}))
                    self.assertFalse(get_probe_verdict(case, {'voice_presentation':'masculine','evidence':case['text']}))
                else:
                    phrase='masculine voice' if case['presentation']=='masculine' else 'womanish'
                    self.assertTrue(get_probe_verdict(case, {'voice_presentation':case['presentation'],'evidence':phrase}))
                    self.assertFalse(get_probe_verdict(case, {'voice_presentation':case['presentation'],'evidence':'invented vocal evidence'}))
                    self.assertFalse(get_probe_verdict(case, {'voice_presentation':'unknown','evidence':''}))
                self.assertFalse(get_probe_verdict(case, None))
