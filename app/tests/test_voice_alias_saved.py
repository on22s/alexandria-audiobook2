"""An alias chosen in the Voices tab must survive the save it rides on.

The tab sends `alias_of` inside each speaker's entry to POST
/api/save_voice_config. The handler stores `VoiceConfigItem.model_dump()`, and
the model did not declare `alias_of`, so pydantic discarded it: every alias a
user set was gone by the next render, silently. These tests go through the real
handler and then through the renderer's own alias resolution.
"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from project import ProjectManager
from routers import voices as voices_module


def save(voice_path, payload):
    items = {name: voices_module.VoiceConfigItem(**entry) for name, entry in payload.items()}
    with patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path):
        asyncio.run(voices_module.save_voice_config(items))
    with open(voice_path, encoding="utf-8") as fh:
        return json.load(fh)


class AliasSurvivesSaveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "voice_config.json")
        # the shape collectVoiceConfig() in app-core.js sends
        self.payload = {
            "ELENA": {"type": "clone", "ref_audio": "designed_voices/elena.wav", "ref_text": "Hello.", "seed": "-1"},
            "YOUNG ELENA": {"type": "custom", "voice": "Ryan", "character_style": "", "seed": "-1",
                            "alias_of": "ELENA"},
        }

    def test_the_saved_file_keeps_the_alias(self):
        saved = save(self.path, self.payload)
        self.assertEqual("ELENA", saved["YOUNG ELENA"]["alias_of"])

    def test_the_renderer_resolves_the_saved_alias(self):
        saved = save(self.path, self.payload)
        pm = ProjectManager.__new__(ProjectManager)
        self.assertEqual("ELENA", pm._resolve_alias("YOUNG ELENA", saved))
        self.assertEqual("ELENA", pm._resolve_alias("ELENA", saved))

    def test_clearing_the_alias_in_the_tab_clears_it_in_the_file(self):
        save(self.path, self.payload)
        self.payload["YOUNG ELENA"]["alias_of"] = None   # the tab explicitly clears an empty alias
        saved = save(self.path, self.payload)
        pm = ProjectManager.__new__(ProjectManager)
        self.assertIsNone(saved["YOUNG ELENA"]["alias_of"])
        self.assertEqual("YOUNG ELENA", pm._resolve_alias("YOUNG ELENA", saved))


if __name__ == "__main__":
    unittest.main()
