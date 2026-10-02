"""Speaker-repair HTTP selections cover every byte of the previewed script."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import scripts_library
from tests.test_support import assert_directory_payload_names

ROWS = [{"speaker": "SUBARU", "text": "Subaru looked toward the door.", "instruct": "Calm."},
        {"speaker": "SUBARU", "text": "She said she would return.", "instruct": "Neutral."}]
SELECTION = {"entry_number": 1, "expected_speaker": "SUBARU", "new_speaker": "NARRATOR"}

class SpeakerRepairTransactionTests(unittest.TestCase):
    def test_text_changes_same_speaker_insertions_and_other_rows_refuse_old_preview(self):
        app = FastAPI()
        app.include_router(scripts_library.router)
        with TestClient(app) as client:
            for change in ("same_speaker_text", "insertion", "direction", "other_row"):
                with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    script = root / "book.json"
                    script.write_text(json.dumps(ROWS))
                    with patch.object(scripts_library, "SCRIPTS_DIR", str(root)):
                        preview = client.get("/api/scripts/book/repair/speakers/preview").json()
                        self.assertEqual("Subaru looked toward the door.", preview["candidates"][0]["text"])
                        updated = copy.deepcopy(ROWS)
                        if change == "same_speaker_text":
                            updated[0]["text"] = "I am speaking now."
                        elif change == "insertion":
                            updated.insert(0, {"speaker": "SUBARU", "text": "Different prose.", "instruct": "Calm."})
                        elif change == "direction":
                            updated[0]["instruct"] = "Whisper."
                        else:
                            updated[1]["text"] = "The other row changed."
                        current = json.dumps(updated).encode()
                        script.write_bytes(current)
                        response = client.post("/api/scripts/book/repair/speakers/apply", json={
                            "expected_sha256": preview["sha256"], "selections": [SELECTION]})
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertIn("Script changed after preview", response.text)
                    self.assertEqual(current, script.read_bytes())
                    assert_directory_payload_names(self, root, ["book.json"], lock_targets=[root / '.active_book_transaction.json', script, *scripts_library._get_saved_book_companions(str(script))])

    def test_missing_token_refuses_before_backup_or_write(self):
        app = FastAPI()
        app.include_router(scripts_library.router)
        with TestClient(app) as client, tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "book.json"
            original = json.dumps(ROWS).encode()
            script.write_bytes(original)
            with patch.object(scripts_library, "SCRIPTS_DIR", str(root)):
                for token in (None, ""):
                    response = client.post("/api/scripts/book/repair/speakers/apply", json={
                        "expected_sha256": token, "selections": [SELECTION]})
                    self.assertEqual(400, response.status_code, response.text)
            self.assertEqual(original, script.read_bytes())
            assert_directory_payload_names(self, root, ["book.json"], lock_targets=[root / '.active_book_transaction.json', script, *scripts_library._get_saved_book_companions(str(script))])

    def test_fresh_preview_changes_only_selected_speaker_and_preserves_exact_backup(self):
        app = FastAPI()
        app.include_router(scripts_library.router)
        with TestClient(app) as client, tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "book.json"
            original = json.dumps(ROWS).encode()
            script.write_bytes(original)
            with patch.object(scripts_library, "SCRIPTS_DIR", str(root)):
                preview = client.get("/api/scripts/book/repair/speakers/preview").json()
                response = client.post("/api/scripts/book/repair/speakers/apply", json={
                    "expected_sha256": preview["sha256"], "selections": [SELECTION]})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual([{**ROWS[0], "speaker": "NARRATOR"}, ROWS[1]], json.loads(script.read_bytes()))
            self.assertEqual(original, (root / response.json()["backup"]).read_bytes())
