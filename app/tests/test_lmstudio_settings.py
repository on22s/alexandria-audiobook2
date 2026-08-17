import unittest
from unittest.mock import patch

import lmstudio_settings


class DynamicLmStudioSettingsTests(unittest.TestCase):
    MODEL = "gemma-4-e4b-uncensored-hauhaucs-aggressive"

    def test_verified_profile_selected_with_headroom(self):
        settings = lmstudio_settings.get_safe_local_settings(
            self.MODEL, True, (17 * 1024 ** 3, 10 * 1024 ** 3))
        self.assertEqual((32768, 2),
                         (settings["context_length"], settings["parallel"]))

    def test_external_vram_pressure_uses_fallback(self):
        settings = lmstudio_settings.get_safe_local_settings(
            self.MODEL, True, (17 * 1024 ** 3, 16 * 1024 ** 3))
        self.assertEqual((8192, 1),
                         (settings["context_length"], settings["parallel"]))

    def test_unknown_model_uses_fallback(self):
        settings = lmstudio_settings.get_safe_local_settings(
            "another-model", True, (17 * 1024 ** 3, 1))
        self.assertEqual((8192, 1),
                         (settings["context_length"], settings["parallel"]))

    def test_missing_gpu_metrics_use_fallback(self):
        with patch.object(lmstudio_settings, "get_local_vram_bytes", return_value=None):
            settings = lmstudio_settings.get_safe_local_settings(self.MODEL, True)
        self.assertEqual((8192, 1),
                         (settings["context_length"], settings["parallel"]))

    def test_planned_local_settings_use_verified_target_without_mutating(self):
        status = {"context_length": None, "parallel": None,
                  "ideal_context_length": 32768, "ideal_parallel": 2,
                  "settings_reason": "verified profile"}
        with patch.object(lmstudio_settings, "get_current_status",
                          return_value=status) as current:
            planned = lmstudio_settings.get_planned_ideal_settings(
                "local", "http://localhost:1234/v1", self.MODEL)

        self.assertEqual((32768, 2),
                         (planned["context_length"], planned["parallel"]))
        current.assert_called_once()

    def test_planned_remote_settings_use_remote_target_without_status_call(self):
        with patch.object(lmstudio_settings, "get_current_status") as current:
            planned = lmstudio_settings.get_planned_ideal_settings(
                "remote", "http://remote:1234/v1", self.MODEL, "tnr-0")

        self.assertEqual((98304, 2),
                         (planned["context_length"], planned["parallel"]))
        current.assert_not_called()


class RemoteServerReachabilityTests(unittest.TestCase):
    def _ssh_result(self, stdout, returncode=0):
        return type("Result", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()

    def test_server_bound_true_when_listening_on_all_interfaces(self):
        stdout = (
            "State  Recv-Q Send-Q Local Address:Port  Peer Address:PortProcess\n"
            "LISTEN 0      511          0.0.0.0:1234       0.0.0.0:*    "
            "users:((\"llmster\",pid=3945,fd=29))\n"
        )
        with patch.object(lmstudio_settings, "_ssh_run",
                          return_value=self._ssh_result(stdout)):
            self.assertTrue(lmstudio_settings._remote_server_bound("tnr-0", 1234))

    def test_server_bound_false_when_only_localhost(self):
        stdout = (
            "LISTEN 0      511        127.0.0.1:41343      0.0.0.0:*    "
            "users:((\"llmster\",pid=3945,fd=28))\n"
        )
        with patch.object(lmstudio_settings, "_ssh_run",
                          return_value=self._ssh_result(stdout)):
            self.assertFalse(lmstudio_settings._remote_server_bound("tnr-0", 1234))

    def test_server_bound_none_on_ssh_failure(self):
        with patch.object(lmstudio_settings, "_ssh_run",
                          side_effect=OSError("unreachable")):
            self.assertIsNone(lmstudio_settings._remote_server_bound("tnr-0", 1234))

    def test_ensure_remote_server_running_skips_start_when_already_bound(self):
        with patch.object(lmstudio_settings, "_remote_server_bound", return_value=True), \
             patch.object(lmstudio_settings, "_ssh_run") as ssh_run:
            ok, msg = lmstudio_settings.ensure_remote_server_running("tnr-0", 1234)
        self.assertTrue(ok)
        self.assertIn("already bound", msg)
        ssh_run.assert_not_called()

    def test_ensure_remote_server_running_starts_server_when_not_bound(self):
        with patch.object(lmstudio_settings, "_remote_server_bound", return_value=False), \
             patch.object(lmstudio_settings, "_ssh_run",
                          return_value=self._ssh_result("")) as ssh_run:
            ok, msg = lmstudio_settings.ensure_remote_server_running("tnr-0", 1234)
        self.assertTrue(ok)
        ssh_run.assert_called_once()
        self.assertIn("--bind 0.0.0.0", ssh_run.call_args.args[1])

    def test_remote_status_reports_server_reachable_when_port_given(self):
        with patch.object(lmstudio_settings, "_ssh_run",
                          return_value=self._ssh_result('{"loadedModels":[]}')), \
             patch.object(lmstudio_settings, "_remote_server_bound", return_value=True):
            status = lmstudio_settings.get_remote_lmstudio_status(
                "tnr-0", "model", port=1234)
        self.assertTrue(status["server_reachable"])

    def test_remote_status_omits_server_reachable_without_port(self):
        with patch.object(lmstudio_settings, "_ssh_run",
                          return_value=self._ssh_result('{"loadedModels":[]}')):
            status = lmstudio_settings.get_remote_lmstudio_status("tnr-0", "model")
        self.assertNotIn("server_reachable", status)

    def test_apply_remote_settings_ensures_server_before_loading(self):
        calls = []

        def fake_ensure(ssh_alias, port, timeout=30):
            calls.append(("ensure", ssh_alias, port))
            return True, "ok"

        with patch.object(lmstudio_settings, "ensure_remote_server_running",
                          side_effect=fake_ensure), \
             patch.object(lmstudio_settings, "_ssh_run",
                          return_value=self._ssh_result("")) as ssh_run, \
             patch.object(lmstudio_settings, "invalidate_remote_status_cache"):
            ok, msg = lmstudio_settings.apply_remote_lmstudio_settings(
                "tnr-0", "model", ideal=True, port=5555)
        self.assertTrue(ok)
        self.assertEqual([("ensure", "tnr-0", 5555)], calls)
        ssh_run.assert_called_once()

    def test_apply_remote_settings_fails_loud_when_server_cannot_start(self):
        with patch.object(lmstudio_settings, "ensure_remote_server_running",
                          return_value=(False, "boom")), \
             patch.object(lmstudio_settings, "_ssh_run") as ssh_run:
            ok, msg = lmstudio_settings.apply_remote_lmstudio_settings(
                "tnr-0", "model", ideal=True, port=1234)
        self.assertFalse(ok)
        self.assertIn("boom", msg)
        ssh_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
