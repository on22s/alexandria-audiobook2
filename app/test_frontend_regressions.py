import base64
from pathlib import Path
import re
import unittest

import config_settings
import utils


class FrontendTests(unittest.TestCase):
    def test_launcher_contracts_cover_dynamic_ports_failures_and_rocm_constraints(self):
        root = Path(__file__).resolve().parent.parent
        start = (root / "start.js").read_text(encoding="utf-8")
        start_llm = (root / "start_llm.js").read_text(encoding="utf-8")
        install = (root / "install.js").read_text(encoding="utf-8")
        self.assertIn('port: "{{port}}"', start)
        self.assertIn('ALEXANDRIA_PORT: "{{local.port}}"', start)
        self.assertIn('method: "script.return"', start_llm)
        self.assertIn("triton-rocm", install)
        torch_script = (root / "torch.js").read_text(encoding="utf-8")
        for rocm_pin in ("torch==2.10.0", "torchaudio==2.10.0",
                         "triton-rocm==3.6.0", "/whl/rocm7.0"):
            self.assertIn(rocm_pin, torch_script)

    def test_readme_api_examples_do_not_reference_removed_routes(self):
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        for removed in ("/api/parse_voices", "/api/lora/generate_dataset",
                        "/api/voice_design/delete/", "/api/status/script_generation"):
            self.assertNotIn(removed, readme)

    def test_docker_image_includes_root_runtime_dependencies(self):
        root = Path(__file__).resolve().parent.parent
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        for required in ("gpu_stats.py", "persona_prompts.txt", "alexandria_alignment.py",
                         "alexandria_preparer_rocm_compatible.py",
                         "llm_enricher.py", "voice_analysis.py", "name_voices.py"):
            self.assertIn(required, dockerfile)

    def test_docker_mounts_single_persistent_runtime_root(self):
        root = Path(__file__).resolve().parent.parent
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("ALEXANDRIA_DATA_DIR=/alexandria/runtime", dockerfile)
        self.assertIn("./data/runtime:/alexandria/runtime", compose)

    def test_frontend_wires_preparer_duration_and_lora_cancel(self):
        html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("min_chunk_duration: getNumFieldValue('prep-min-chunk-duration', 2)", html)
        self.assertIn("id=\"btn-lora-cancel\"", html)
        self.assertIn("/api/lora/train/cancel", html)
        self.assertNotIn("id=\"prep-skip-annotation\"", html)

    def test_frontend_renders_config_warnings_as_text_and_refreshes_after_save(self):
        html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="config-warning-banner"', html)
        self.assertIn('id="config-warning-msg"', html)
        start = html.index("function renderConfigWarnings(config)")
        end = html.index("function populateLlmInputs", start)
        renderer = html[start:end]
        self.assertIn("message.textContent", renderer)
        self.assertNotIn("message.innerHTML", renderer)
        self.assertIn("renderConfigWarnings(config);", html)
        self.assertIn("renderConfigWarnings(savedConfig);", html)

    def test_frontend_config_controls_match_backend_schema(self):
        html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        input_tags = {
            match.group(1): match.group(0)
            for match in re.finditer(r'<input\b[^>]*\bid="([^"]+)"[^>]*>', html)
        }
        mappings = {
            "parallel-workers": (config_settings.TTSConfig, "parallel_workers"),
            "tts-max-new-tokens": (config_settings.TTSConfig, "max_new_tokens"),
            "sub-batch-min-size": (config_settings.TTSConfig, "sub_batch_min_size"),
            "sub-batch-ratio": (config_settings.TTSConfig, "sub_batch_ratio"),
            "sub-batch-max-items": (config_settings.TTSConfig, "sub_batch_max_items"),
            "pause-between-speakers": (config_settings.TTSConfig, "pause_between_speakers_ms"),
            "pause-same-speaker": (config_settings.TTSConfig, "pause_same_speaker_ms"),
            "chunk-size": (config_settings.GenerationConfig, "chunk_size"),
            "max-tokens": (config_settings.GenerationConfig, "max_tokens"),
            "temperature": (config_settings.GenerationConfig, "temperature"),
            "top-p": (config_settings.GenerationConfig, "top_p"),
            "top-k": (config_settings.GenerationConfig, "top_k"),
            "min-p": (config_settings.GenerationConfig, "min_p"),
            "presence-penalty": (config_settings.GenerationConfig, "presence_penalty"),
        }
        for control_id, (model, field_name) in mappings.items():
            with self.subTest(control_id=control_id):
                tag = input_tags[control_id]
                attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', tag))
                schema = model.model_json_schema()["properties"][field_name]
                self.assertEqual(float(schema["default"]), float(attrs["value"]))
                for schema_key, attr_name in (("minimum", "min"), ("maximum", "max")):
                    if schema_key in schema:
                        self.assertEqual(float(schema[schema_key]), float(attrs[attr_name]))
                    else:
                        self.assertNotIn(attr_name, attrs)
                step = float(attrs.get("step", "1"))
                self.assertGreater(step, 0)
                if schema["type"] == "integer":
                    self.assertTrue(step.is_integer())

        selects = {
            match.group(1): set(re.findall(r'<option\s+value="([^"]+)"', match.group(2)))
            for match in re.finditer(
                r'<select\b[^>]*\bid="([^"]+)"[^>]*>(.*?)</select>', html, re.DOTALL
            )
        }
        app_mode = config_settings.AppConfig.model_json_schema()["properties"]["llm_mode"]
        tts_mode = config_settings.TTSConfig.model_json_schema()["properties"]["mode"]
        self.assertEqual(set(app_mode["enum"]), selects["llm-mode"])
        self.assertEqual(set(tts_mode["enum"]), selects["tts-mode"])
        self.assertIn(app_mode["default"], selects["llm-mode"])
        self.assertIn(tts_mode["default"], selects["tts-mode"])

    def test_check_basic_auth_accepts_and_rejects_credentials(self):
        make = lambda raw: "Basic " + base64.b64encode(raw.encode()).decode()
        # Correct credentials pass.
        self.assertTrue(utils.check_basic_auth(
            make("alexandria:secret"), "alexandria", "secret"))
        # Wrong password and wrong username both fail.
        self.assertFalse(utils.check_basic_auth(
            make("alexandria:wrong"), "alexandria", "secret"))
        self.assertFalse(utils.check_basic_auth(
            make("intruder:secret"), "alexandria", "secret"))
        # Malformed / missing inputs fail closed rather than raising.
        self.assertFalse(utils.check_basic_auth(
            make("nocolonhere"), "alexandria", "secret"))
        self.assertFalse(utils.check_basic_auth(
            "Basic !!!not-base64!!!", "alexandria", "secret"))
        self.assertFalse(utils.check_basic_auth(
            "Bearer " + base64.b64encode(b"alexandria:secret").decode(),
            "alexandria", "secret"))
        self.assertFalse(utils.check_basic_auth("", "alexandria", "secret"))
