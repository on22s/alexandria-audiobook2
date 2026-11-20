"""#2104: preparer HTTP refusals carry no host paths.

Preflight, config parsing and ZIP-publication details used to interpolate
absolute interpreter, script and ZIP paths into the response body. Every one
is now a static AppError message with a shipped i18n key; the full path stays
in the server log for diagnosis.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks

import core
from alexandria_zip_publication import ZipPublicationError
import routers.preparer as preparer

I18N = Path(preparer.__file__).resolve().parent.parent / "static" / "i18n"
KEYS = (
    "error.preparer.missing_script",
    "error.preparer.missing_runtime",
    "error.preparer.invalid_config",
    "error.preparer.zip_unavailable",
)
HOST_PATH = "/host/private/mitch"


class PreparerPathRedactionTests(unittest.TestCase):
    def test_missing_script_detail_is_static_and_keyed(self):
        with patch.object(preparer, "PREPARER_SCRIPT_PATH", HOST_PATH + "/preparer.py"), \
                patch.object(preparer.os.path, "exists", return_value=False):
            with self.assertRaises(core.AppError) as raised:
                preparer._resolve_preparer_interpreter()
        error = raised.exception
        self.assertEqual(503, error.status_code)
        self.assertEqual("error.preparer.missing_script", error.message_key)
        self.assertNotIn(HOST_PATH, error.detail)
        self.assertEqual("Preparer is not installed; re-run Install.", error.detail)

    def test_missing_runtime_detail_hides_preparer_env_and_rocm_python(self):
        with patch.object(preparer, "PREPARER_SCRIPT_PATH", HOST_PATH + "/preparer.py"), \
                patch.object(preparer, "PREPARER_ENV_PYTHON", HOST_PATH + "/preparer_env/bin/python"), \
                patch.object(preparer.os.path, "exists", return_value=True), \
                patch.object(preparer.os.path, "isfile", return_value=False), \
                patch.object(preparer, "_load_voicelab_config",
                             return_value={"rocm_python": HOST_PATH + "/rocm/python"}):
            with self.assertRaises(core.AppError) as raised:
                preparer._resolve_preparer_interpreter()
        error = raised.exception
        self.assertEqual(503, error.status_code)
        self.assertEqual("error.preparer.missing_runtime", error.message_key)
        self.assertNotIn(HOST_PATH, error.detail)
        self.assertNotIn("not found", error.detail)

    def test_invalid_config_detail_names_the_field_and_reason_without_the_path(self):
        bad_config = json.dumps({"audio_filename": "a.wav", "chunk_size": HOST_PATH + "/model.gguf"})
        with self.assertRaises(core.AppError) as raised:
            asyncio.run(preparer.preparer_start(
                BackgroundTasks(), config_json=bad_config, audio_file=None, source_file=None))
        error = raised.exception
        self.assertEqual(422, error.status_code)
        self.assertEqual("error.preparer.invalid_config", error.message_key)
        self.assertIn("chunk_size", error.detail)
        self.assertIn("valid number", error.detail)
        self.assertNotIn(HOST_PATH, error.detail)
        self.assertNotIn(HOST_PATH, str(error.message_values))

    def test_batch_invalid_config_detail_names_the_field_and_reason_without_the_path(self):
        bad_config = json.dumps({
            "tasks": [{"audio_filename": "a.wav", "output_filename": "o.zip"}],
            "min_confidence": HOST_PATH + "/model.gguf",
        })
        with self.assertRaises(core.AppError) as raised:
            asyncio.run(preparer.preparer_batch_upload_start(
                BackgroundTasks(), config_json=bad_config, audio_files=[]))
        error = raised.exception
        self.assertEqual(422, error.status_code)
        self.assertEqual("error.preparer.invalid_config", error.message_key)
        self.assertIn("min_confidence", error.detail)
        self.assertIn("valid number", error.detail)
        self.assertNotIn(HOST_PATH, error.detail)
        self.assertNotIn(HOST_PATH, str(error.message_values))

    def test_invalid_config_http_body_names_field_and_reason_but_no_path(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        bad_config = json.dumps({"audio_filename": "a.wav", "chunk_size": HOST_PATH + "/model.gguf"})
        api = FastAPI()
        api.include_router(preparer.router)
        api.add_exception_handler(core.AppError, core.handle_app_error)
        with TestClient(api) as client:
            response = client.post("/api/preparer/start", data={"config_json": bad_config})
        self.assertEqual(422, response.status_code, response.text)
        self.assertNotIn(HOST_PATH, response.text)
        self.assertIn("chunk_size", response.text)
        self.assertIn("valid number", response.text)
        self.assertEqual("error.preparer.invalid_config", response.json()["message_key"])

    def test_zip_publication_detail_hides_the_archive_path(self):
        with tempfile.TemporaryDirectory() as root:
            failing = ZipPublicationError("ZIP archive is a symlink: " + root + "/secret.zip")
            with patch.object(preparer, "PREPARER_OUTPUT_DIR", root), \
                    patch.object(preparer, "get_zip_archive_family", side_effect=failing):
                with self.assertRaises(core.AppError) as raised:
                    asyncio.run(preparer.preparer_download("secret.zip"))
        error = raised.exception
        self.assertEqual(409, error.status_code)
        self.assertEqual("error.preparer.zip_unavailable", error.message_key)
        self.assertNotIn(root, error.detail)

    def test_new_error_keys_ship_in_all_four_catalogs(self):
        for locale in ("en", "zh-CN", "ja", "es-419"):
            with self.subTest(locale=locale):
                payload = json.loads((I18N / f"ui.{locale}.json").read_text(encoding="utf-8"))
                for key in KEYS:
                    self.assertIn(key, payload)
                    self.assertTrue(payload[key].strip())
                    if locale != "en":
                        self.assertNotEqual(payload["_meta"].get("locale"), "en")
                        self.assertNotIn(HOST_PATH, payload[key])
                self.assertIn("{detail}", payload["error.preparer.invalid_config"])


if __name__ == "__main__":
    unittest.main()
