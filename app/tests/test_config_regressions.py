import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import config_settings
import project as project_module
from routers import system as system_module


class ConfigTests(unittest.TestCase):
    def test_app_config_loader_returns_fresh_shape_safe_data(self):
        documents = (
            ("null", {}),
            ("[]", {}),
            ('{"llm": [], "tts": null, "prompts": "bad", '
             '"generation": 3, "llm_local": [], "llm_remote": "bad", '
             '"unknown": {"keep": true}}',
             {"prompts": None, "generation": None, "llm_local": None,
              "llm_remote": None, "unknown": {"keep": True}}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for document, expected in documents:
                with self.subTest(document=document):
                    path.write_text(document, encoding="utf-8")
                    loaded = config_settings.load_app_config(str(path))
                    self.assertEqual(expected, loaded)
                    loaded["changed"] = True
                    self.assertEqual(expected, config_settings.load_app_config(str(path)))

    def test_app_config_loader_ignores_invalid_legacy_values_without_writing(self):
        document = json.dumps({
            "llm_mode": "cloud",
            "llm": {"base_url": 5, "api_key": "key", "unknown": "keep"},
            "tts": {"parallel_workers": 0, "language": "English"},
            "generation": {
                "max_tokens": 12,
                "temperature": "0.7",
                "banned_tokens": "bad",
                "review_batch_size": 31,
            },
            "unknown_top": True,
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(document, encoding="utf-8")

            result = config_settings.load_app_config_result(str(path))

            self.assertEqual("key", result.data["llm"]["api_key"])
            self.assertEqual("keep", result.data["llm"]["unknown"])
            self.assertNotIn("base_url", result.data["llm"])
            self.assertNotIn("parallel_workers", result.data["tts"])
            self.assertNotIn("max_tokens", result.data["generation"])
            self.assertNotIn("banned_tokens", result.data["generation"])
            self.assertEqual(0.7, result.data["generation"]["temperature"])
            self.assertEqual(31, result.data["generation"]["review_batch_size"])
            self.assertTrue(result.data["unknown_top"])
            self.assertNotIn("llm_mode", result.data)
            self.assertEqual(
                {"llm.base_url", "tts.parallel_workers", "generation.max_tokens",
                 "generation.banned_tokens", "llm_mode"},
                {warning.field for warning in result.warnings},
            )
            self.assertEqual(document, path.read_text(encoding="utf-8"))

            generation = result.data["generation"]
            self.assertEqual(4096, generation.get("max_tokens", 4096))
            self.assertEqual(8000, generation.get("max_tokens", 8000))

    def test_config_models_accept_documented_boundaries(self):
        tts = system_module.TTSConfig(
            mode="external",
            parallel_workers=1,
            sub_batch_min_size=1,
            sub_batch_ratio=1,
            sub_batch_max_items=0,
            pause_between_speakers_ms=0,
            pause_same_speaker_ms=0,
        )
        profile = system_module.LLMConfig(
            base_url="http://localhost:1234/v1", api_key="key", model_name="model"
        )
        generation_boundaries = (
            system_module.GenerationConfig(
                chunk_size=500,
                max_tokens=256,
                temperature=2,
                top_p=1,
                top_k=200,
                min_p=0,
                presence_penalty=-2,
            ),
            system_module.GenerationConfig(
                chunk_size=500,
                max_tokens=256,
                temperature=0,
                top_p=0,
                top_k=0,
                min_p=1,
                presence_penalty=2,
            ),
        )

        with patch.object(system_module, "atomic_json_write") as write_config, \
             patch.object(system_module.project_manager, "engine", object()):
            client = TestClient(app_module.app)
            for generation in generation_boundaries:
                payload = system_module.AppConfig(
                    llm=profile,
                    llm_mode="local",
                    llm_local=profile,
                    tts=tts,
                    generation=generation,
                ).model_dump(mode="json")
                response = client.post("/api/config", json=payload)
                self.assertEqual(200, response.status_code, response.text)

        self.assertEqual(2, write_config.call_count)

    def test_config_api_rejects_out_of_range_values_without_writing(self):
        invalid_values = (
            ("tts", "mode", "invalid"),
            ("tts", "parallel_workers", 0),
            ("tts", "sub_batch_min_size", 0),
            ("tts", "sub_batch_ratio", 0.5),
            ("tts", "sub_batch_max_items", -1),
            ("tts", "pause_between_speakers_ms", -1),
            ("tts", "pause_same_speaker_ms", -1),
            ("generation", "chunk_size", 499),
            ("generation", "max_tokens", 255),
            ("generation", "temperature", -0.1),
            ("generation", "temperature", 2.1),
            ("generation", "top_p", -0.1),
            ("generation", "top_p", 1.1),
            ("generation", "top_k", -1),
            ("generation", "top_k", 201),
            ("generation", "min_p", -0.1),
            ("generation", "min_p", 1.1),
            ("generation", "presence_penalty", -2.1),
            ("generation", "presence_penalty", 2.1),
        )
        profile = {
            "base_url": "http://localhost:1234/v1",
            "api_key": "key",
            "model_name": "model",
        }
        base_payload = {
            "llm": profile,
            "llm_mode": "local",
            "llm_local": profile,
            "llm_remote": None,
            "tts": system_module.TTSConfig().model_dump(mode="json"),
            "generation": system_module.GenerationConfig().model_dump(mode="json"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            original = b'{"preserve": true}'
            client = TestClient(app_module.app)
            with patch.object(system_module, "CONFIG_PATH", str(config_path)):
                for section, field, value in invalid_values:
                    with self.subTest(section=section, field=field, value=value):
                        config_path.write_bytes(original)
                        payload = json.loads(json.dumps(base_payload))
                        payload[section][field] = value
                        response = client.post("/api/config", json=payload)
                        self.assertEqual(422, response.status_code, response.text)
                        self.assertEqual(original, config_path.read_bytes())

    def test_generation_config_banned_tokens_default_is_not_shared(self):
        first = system_module.GenerationConfig()
        second = system_module.GenerationConfig()
        first.banned_tokens.append("token")
        self.assertEqual([], second.banned_tokens)

    def test_llm_normalization_returns_copy_without_mutating_profile(self):
        profile = system_module.LLMConfig(
            base_url="http://localhost:1234/", api_key="key", model_name="model"
        )

        normalized = system_module._normalize_and_validate_llm(profile)

        self.assertIsNot(profile, normalized)
        self.assertEqual("http://localhost:1234/", profile.base_url)
        self.assertEqual("http://localhost:1234/v1", normalized.base_url)
        self.assertEqual(profile.api_key, normalized.api_key)
        self.assertEqual(profile.model_name, normalized.model_name)

    def test_llm_normalization_preserves_validation_behavior(self):
        normalized = system_module._normalize_and_validate_llm(
            system_module.LLMConfig(
                base_url="http://localhost:1234/v1/", api_key="key", model_name="model"
            )
        )
        self.assertEqual("http://localhost:1234/v1", normalized.base_url)

        for base_url in (" ", "https://example.com/v1"):
            with self.subTest(base_url=base_url):
                profile = system_module.LLMConfig(
                    base_url=base_url, api_key="key", model_name="model"
                )
                with self.assertRaises(system_module.HTTPException) as raised:
                    system_module._normalize_and_validate_llm(profile)
                self.assertEqual(400, raised.exception.status_code)
                self.assertEqual(base_url, profile.base_url)

    def test_save_config_persists_normalized_copy_without_mutating_request(self):
        profile = system_module.LLMConfig(
            base_url="http://localhost:1234/", api_key="key", model_name="model"
        )
        config = system_module.AppConfig(
            llm=profile,
            llm_mode="local",
            llm_local=profile,
            llm_remote=None,
            tts=system_module.TTSConfig(),
        )
        original = config.model_dump()

        with patch.object(system_module, "atomic_json_write") as write_config, \
             patch.object(system_module.project_manager, "invalidate_config_cache") as invalidate, \
             patch.object(system_module.project_manager, "engine", object()):
            asyncio.run(system_module.save_config(config))
            self.assertIsNone(system_module.project_manager.engine)

        self.assertEqual(original, config.model_dump())
        invalidate.assert_called_once_with()
        saved = write_config.call_args.args[0]
        self.assertEqual("http://localhost:1234/v1", saved["llm"]["base_url"])
        self.assertEqual("http://localhost:1234/v1", saved["llm_local"]["base_url"])

    def test_project_config_cache_can_be_invalidated_when_mtime_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = project_module.ProjectManager(tmp)
            manager.config_path = str(Path(tmp) / "config.json")
            Path(manager.config_path).write_text('{"value": "aa"}', encoding="utf-8")

            with patch.object(project_module.os.path, "getmtime", return_value=123):
                self.assertEqual("aa", manager._read_config()["value"])
                Path(manager.config_path).write_text('{"value": "bb"}', encoding="utf-8")
                self.assertEqual("aa", manager._read_config()["value"])

                manager.invalidate_config_cache()

                self.assertEqual("bb", manager._read_config()["value"])

    def test_failed_config_save_preserves_cache_and_engine(self):
        profile = system_module.LLMConfig(
            base_url="http://localhost:1234/v1", api_key="key", model_name="model"
        )
        config = system_module.AppConfig(
            llm=profile,
            llm_mode="local",
            llm_local=profile,
            tts=system_module.TTSConfig(),
        )
        engine = object()

        with patch.object(system_module, "atomic_json_write", side_effect=OSError("write failed")), \
             patch.object(system_module.project_manager, "invalidate_config_cache") as invalidate, \
             patch.object(system_module.project_manager, "engine", engine):
            with self.assertRaisesRegex(OSError, "write failed"):
                asyncio.run(system_module.save_config(config))

            invalidate.assert_not_called()
            self.assertIs(engine, system_module.project_manager.engine)

    def test_config_save_backs_up_damaged_source_before_replacing_it(self):
        profile = system_module.LLMConfig(
            base_url="http://localhost:1234/v1", api_key="key", model_name="model"
        )
        config = system_module.AppConfig(
            llm=profile, llm_mode="local", llm_local=profile,
            tts=system_module.TTSConfig(),
        )
        damaged_documents = (b"{bad", b"[]", b'{"tts": []}')

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            for damaged in damaged_documents:
                with self.subTest(damaged=damaged):
                    for old_backup in config_path.parent.glob("config.json.damaged-*.bak"):
                        old_backup.unlink()
                    config_path.write_bytes(damaged)
                    config_path.chmod(0o640)
                    with patch.object(system_module, "CONFIG_PATH", str(config_path)):
                        asyncio.run(system_module.save_config(config))

                    backups = list(config_path.parent.glob("config.json.damaged-*.bak"))
                    self.assertEqual(1, len(backups))
                    self.assertEqual(damaged, backups[0].read_bytes())
                    self.assertEqual(0o640, backups[0].stat().st_mode & 0o777)
                    self.assertIsInstance(json.loads(config_path.read_text()), dict)

    def test_config_save_does_not_back_up_valid_partial_source(self):
        profile = system_module.LLMConfig(
            base_url="http://localhost:1234/v1", api_key="key", model_name="model"
        )
        config = system_module.AppConfig(
            llm=profile, llm_mode="local", llm_local=profile,
            tts=system_module.TTSConfig(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text('{"unknown": true}', encoding="utf-8")
            with patch.object(system_module, "CONFIG_PATH", str(config_path)):
                asyncio.run(system_module.save_config(config))

            self.assertEqual([], list(config_path.parent.glob("config.json.damaged-*.bak")))

    def test_damaged_config_backup_names_are_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_bytes(b"{bad")

            first = config_settings.backup_damaged_app_config(str(config_path))
            second = config_settings.backup_damaged_app_config(str(config_path))

            self.assertNotEqual(first, second)
            self.assertEqual(b"{bad", Path(first).read_bytes())
            self.assertEqual(b"{bad", Path(second).read_bytes())

    def test_config_save_stops_when_damaged_backup_fails(self):
        profile = system_module.LLMConfig(
            base_url="http://localhost:1234/v1", api_key="key", model_name="model"
        )
        config = system_module.AppConfig(
            llm=profile, llm_mode="local", llm_local=profile,
            tts=system_module.TTSConfig(),
        )
        result = config_settings.AppConfigLoadResult({}, (), True)
        with patch.object(system_module, "load_app_config_result", return_value=result), \
             patch.object(system_module, "backup_damaged_app_config",
                          side_effect=OSError("backup failed")), \
             patch.object(system_module, "atomic_json_write") as write_config, \
             patch.object(system_module.project_manager, "invalidate_config_cache") as invalidate:
            with self.assertRaises(system_module.HTTPException) as raised:
                asyncio.run(system_module.save_config(config))

        self.assertEqual(500, raised.exception.status_code)
        write_config.assert_not_called()
        invalidate.assert_not_called()

    def test_get_config_recovers_without_rewriting_invalid_json(self):
        invalid_documents = (
            "", "{bad", "null", "[]",
            '{"llm": [], "tts": "bad", "prompts": [], '
            '"llm_local": "bad", "llm_remote": []}',
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            for document in invalid_documents:
                with self.subTest(document=document):
                    config_path.write_text(document, encoding="utf-8")
                    with patch.object(system_module, "CONFIG_PATH", str(config_path)):
                        config = asyncio.run(system_module.get_config())

                    self.assertIn("llm", config)
                    self.assertIn("tts", config)
                    self.assertTrue(config["prompts"]["system_prompt"])
                    self.assertTrue(config["config_warnings"])
                    self.assertTrue(config["config_needs_backup"])
                    self.assertEqual(document, config_path.read_text(encoding="utf-8"))

    def test_get_config_backfills_partial_top_level_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"tts": {"language": "French"}, "custom": "preserved"}),
                encoding="utf-8",
            )
            with patch.object(system_module, "CONFIG_PATH", str(config_path)):
                config = asyncio.run(system_module.get_config())

            self.assertIn("llm", config)
            self.assertEqual("French", config["tts"]["language"])
            self.assertIn("parallel_workers", config["tts"])
            self.assertEqual("preserved", config["custom"])
            self.assertEqual([], config["config_warnings"])
            self.assertFalse(config["config_needs_backup"])
