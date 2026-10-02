import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException
from routers import voices
from tests import test_voice_suggestion_provenance as provenance


ROWS = [None, 42, [], {'speaker': 42, 'type': 'Ignored', 'text': 'Bad'},
        {'speaker': ' Modern ', 'type': 'Ignored', 'text': 'One'},
        {'type': ' Legacy ', 'text': 'Two'},
        {'speaker': '', 'type': 'Legacy', 'text': 'Two'},
        {'speaker': '   ', 'type': 'Ignored', 'text': 'Bad'},
        {'speaker': 0, 'type': 'Legacy', 'text': 'Three'},
        {'speaker': 'Silent', 'text': ''}, {'speaker': 'Malformed', 'text': 42}]


class VoiceScriptSpeakerTests(unittest.TestCase):
    def test_listing_and_required_speaker_share_legacy_precedence(self):
        names = [row['name'] for row in voices.get_voice_rows(ROWS, {})]
        self.assertEqual(['Legacy', 'Malformed', 'Modern', 'Silent'], names)
        with tempfile.TemporaryDirectory() as root:
            script = Path(root, 'script.json')
            script.write_text(json.dumps(ROWS))
            with patch.object(voices, 'SCRIPT_PATH', str(script)):
                for name in names:
                    voices._require_script_speaker(name)
                with self.assertRaises(HTTPException) as error:
                    voices._require_script_speaker('Ignored')
                self.assertEqual(404, error.exception.status_code)
        with patch.object(voices, 'get_script_speaker', return_value='Shared'):
            self.assertEqual(['Shared'], [row['name'] for row in voices.get_voice_rows(ROWS, {})])

    def test_recovery_accepts_legacy_speaker_and_preserves_contextual_error(self):
        with tempfile.TemporaryDirectory() as root:
            script, config = Path(root, 'script.json'), Path(root, 'voices.json')
            script.write_text(json.dumps(ROWS)); config.write_text('{}')
            with patch.object(voices, 'SCRIPT_PATH', str(script)), \
                 patch.object(voices, 'VOICE_CONFIG_PATH', str(config)):
                def request(name):
                    return voices.PersonaRecoveryRequest(speaker=name, resume=False,
                        persona_json='{"description":"warm and measured","ref_text":"Hello there."}')
                asyncio.run(voices.recover_persona(BackgroundTasks(), request('Legacy')))
                self.assertEqual('warm and measured', json.loads(config.read_text())['Legacy']['description'])
                before = config.read_bytes()
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(voices.recover_persona(BackgroundTasks(), request('Ignored')))
                self.assertEqual(422, error.exception.status_code)
                self.assertEqual(before, config.read_bytes())

    def test_suggestions_skip_malformed_rows_and_require_spoken_text(self):
        fixture = provenance.VoiceSuggestionProvenanceTests()
        result, calls = fixture.run_suggestions(
            [{'characters': [provenance.pick('Legacy'), provenance.pick('Modern')]}],
            names=('Legacy', 'Modern'), script_rows=ROWS)
        self.assertEqual({'Legacy', 'Modern'}, set(result['suggestions']))
        self.assertEqual('llm', result['method'])
        self.assertEqual(1, len(calls))
