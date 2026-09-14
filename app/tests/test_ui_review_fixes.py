"""Three UI defects from the 2026-09-14 review: designed-voice edits
duplicated the voice, dataset deletion ignored HTTP errors, and a
storage-blocked browser aborted theme initialisation."""
import asyncio
import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers import voice_design as vd

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class DesignedVoiceUpdate(unittest.TestCase):
    def _save(self, tmp, **over):
        previews = os.path.join(tmp, "previews")
        os.makedirs(previews, exist_ok=True)
        with open(os.path.join(previews, "p.wav"), "wb") as fh:
            fh.write(b"RIFF")
        fields = {"name": "Alto", "description": "d", "sample_text": "s", "preview_file": "p.wav", **over}
        req = vd.VoiceDesignSaveRequest(**fields)
        with patch.object(vd, "DESIGNED_VOICES_DIR", tmp), \
             patch.object(vd, "DESIGNED_VOICES_MANIFEST", os.path.join(tmp, "manifest.json")):
            return asyncio.run(vd.voice_design_save(req)), json.load(open(os.path.join(tmp, "manifest.json")))

    def test_editing_replaces_the_entry_instead_of_appending(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, manifest = self._save(tmp)
            self.assertEqual(1, len(manifest))
            second, manifest = self._save(tmp, description="changed", voice_id=first["voice_id"])
            self.assertEqual("updated", second["status"])
            self.assertEqual(first["voice_id"], second["voice_id"])
            self.assertEqual(1, len(manifest))
            self.assertEqual("changed", manifest[0]["description"])
            # a save WITHOUT an id is still a new voice
            _, manifest = self._save(tmp)
            self.assertEqual(2, len(manifest))

    def test_unknown_id_is_refused_not_silently_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HTTPException) as ctx:
                self._save(tmp, voice_id="nope")
            self.assertEqual(404, ctx.exception.status_code)


class FrontendGuards(unittest.TestCase):
    def test_every_localstorage_access_is_wrapped(self):
        """A storage-blocked browser throws on the first bare call, and the
        theme snippet runs before anything else - so one bare call kills the
        page. Every access must sit inside a try."""
        offenders = []
        for name in ("index.html", os.path.join("js", "app-core.js"), os.path.join("js", "app-scripts.js"),
                     os.path.join("js", "app-workbench.js"), os.path.join("js", "app-training.js"),
                     os.path.join("js", "app-reports.js"), os.path.join("js", "app-voicelab.js")):
            path = os.path.join(STATIC, name)
            if not os.path.exists(path):
                continue
            for i, line in enumerate(open(path, encoding="utf-8"), 1):
                if "localStorage." in line and "try" not in line:
                    offenders.append(f"{name}:{i}")
        self.assertEqual([], offenders)

    def test_dataset_delete_checks_the_response(self):
        src = open(os.path.join(STATIC, "js", "app-workbench.js"), encoding="utf-8").read()
        body = src[src.index("window.dsbDeleteProject"):src.index("window.dsbDeleteProject") + 800]
        self.assertIn("API._handleError(res)", body)


if __name__ == "__main__":
    unittest.main()
