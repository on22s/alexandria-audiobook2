"""Loading a saved book must discard recovery state for the prior active book."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from routers import scripts_library


class SavedBookLoadCheckpointTests(unittest.TestCase):
    def test_load_clears_active_generation_and_recovery_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "book_b.json").write_text("[]", encoding="utf-8")
            active = root / "annotated_script.json"
            active.write_text('[{"text":"old"}]', encoding="utf-8")
            suffixes = (".threepass_checkpoint.json", ".threepass_manifest.json",
                        ".generation_checkpoint.json", ".generation_quality.json")
            for suffix in suffixes:
                Path(str(active) + suffix).write_text("{}", encoding="utf-8")
            with patch.object(scripts_library, "SCRIPT_PATH", str(active)), \
                 patch.object(scripts_library, "DATA_DIR", str(root)), \
                 patch.object(scripts_library, "AUDIOBOOK_PATH", str(root / "cloned_audiobook.mp3")), \
                 patch.object(scripts_library, "M4B_PATH", str(root / "audiobook.m4b")), \
                 patch.object(scripts_library, "SCRIPTS_DIR", str(scripts)), \
                 patch.object(scripts_library, "VOICE_CONFIG_PATH", str(root / "voice_config.json")), \
                 patch.object(scripts_library, "CHUNKS_PATH", str(root / "chunks.json")), \
                 patch.object(scripts_library, "_save_active_book_id"), \
                 patch.object(scripts_library, "_get_saved_book_id", return_value="book-b"), \
                 patch.object(scripts_library, "process_state", {}):
                asyncio.run(scripts_library.load_script(
                    scripts_library.ScriptLoadRequest(name="book_b")))
            self.assertEqual("[]", active.read_text(encoding="utf-8"))
            self.assertEqual([], [suffix for suffix in suffixes
                                  if Path(str(active) + suffix).exists()])
