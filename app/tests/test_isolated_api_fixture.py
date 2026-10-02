"""The isolated API wrapper must choose the same fixture voice on every filesystem."""

import json
from pathlib import Path
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import run_isolated_api_tests as runner


class IsolatedApiFixtureTests(unittest.TestCase):
    def run_wrapper(self, adapter_names):
        app_dir = Path(runner.__file__).resolve().parent
        builtin_dir = app_dir.parent / "builtin_lora"
        inventory = [builtin_dir / name / "adapter_model.safetensors" for name in adapter_names]
        original_glob = Path.glob
        observed = {}
        server = MagicMock()
        server.pid = 12345678
        server.poll.return_value = None

        def glob(path, pattern):
            if path == builtin_dir:
                self.assertEqual("*/adapter_model.safetensors", pattern)
                return iter(inventory)
            return original_glob(path, pattern)

        def launch(command, **kwargs):
            observed["server_command"] = command
            observed["server_kwargs"] = kwargs
            observed["data_dir"] = kwargs["env"]["ALEXANDRIA_DATA_DIR"]
            return server

        def run_suite(command, **kwargs):
            data = Path(observed["data_dir"])
            cast = data / "voice_config.json"
            observed["cast"] = json.loads(cast.read_text()) if cast.exists() else None
            observed["script"] = json.loads((data / "annotated_script.json").read_text())
            observed["state"] = json.loads((data / "state.json").read_text())
            observed["suite_command"] = command
            self.assertEqual(app_dir, kwargs["cwd"])
            return SimpleNamespace(returncode=7)

        with patch.object(Path, "glob", autospec=True, side_effect=glob), \
             patch.object(runner, "get_isolated_server_port", return_value=18765), \
             patch.object(runner, "urlopen", return_value=MagicMock()) as request, \
             patch.object(runner.subprocess, "Popen", side_effect=launch), \
             patch.object(runner.subprocess, "run", side_effect=run_suite), \
             patch.object(runner.os, "killpg") as kill, \
             patch.object(runner.sys, "argv", ["run_isolated_api_tests.py", "--full"]):
            self.assertEqual(7, runner.main())
            request.assert_called_once_with("http://127.0.0.1:18765/api/config", timeout=1)
            kill.assert_called_once_with(server.pid, signal.SIGTERM)
            server.wait.assert_called_once_with(timeout=10)
        self.assertFalse(Path(observed["data_dir"]).exists(), "disposable state must be removed")
        self.assertEqual(["-m", "tests.test_api", "--url", "http://127.0.0.1:18765", "--full"],
                         observed["suite_command"][1:])
        self.assertTrue(observed["server_kwargs"]["start_new_session"])
        self.assertEqual("isolated-fixture", observed["state"]["active_book_id"])
        self.assertEqual(["NARRATOR", "Hero"], [row["speaker"] for row in observed["script"]])
        return observed["cast"]

    def test_fixture_voice_is_stable_across_reversed_directory_orders(self):
        for inventory in (["builtin_zeta", "builtin_alpha"], ["builtin_alpha", "builtin_zeta"]):
            with self.subTest(inventory=inventory):
                cast = self.run_wrapper(inventory)
                expected = {"type": "builtin_lora", "adapter_id": "builtin_alpha",
                            "adapter_path": "builtin_lora/builtin_alpha", "seed": "-1"}
                self.assertEqual({"NARRATOR": expected, "Hero": expected}, cast)

    def test_no_downloaded_voice_keeps_the_optional_cast_absent(self):
        self.assertIsNone(self.run_wrapper([]))
