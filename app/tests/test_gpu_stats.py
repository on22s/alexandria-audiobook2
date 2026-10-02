import subprocess
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import utils  # noqa: F401 - side effect: inserts repo root onto sys.path
import gpu_stats


class GpuStatsTests(unittest.TestCase):
    def test_nvidia_smi_utilization_parses_csv_output(self):
        result = type("Result", (), {"returncode": 0, "stdout": "37\n", "stderr": ""})()
        with patch.object(gpu_stats.subprocess, "run", return_value=result):
            self.assertEqual(37.0, gpu_stats.nvidia_smi_utilization())

    def test_nvidia_smi_utilization_returns_none_when_binary_missing(self):
        with patch.object(gpu_stats.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertIsNone(gpu_stats.nvidia_smi_utilization())

    def test_nvidia_smi_utilization_returns_none_on_unparseable_output(self):
        result = type("Result", (), {"returncode": 0, "stdout": "N/A\n", "stderr": ""})()
        with patch.object(gpu_stats.subprocess, "run", return_value=result):
            self.assertIsNone(gpu_stats.nvidia_smi_utilization())

    def test_sample_gpu_utilization_prefers_nvidia_when_available(self):
        with patch.object(gpu_stats, "nvidia_smi_utilization", return_value=42.0), \
             patch.object(gpu_stats, "run_rocm_smi_json") as rocm:
            self.assertEqual(42.0, gpu_stats.sample_gpu_utilization())
        rocm.assert_not_called()

    def test_sample_gpu_utilization_falls_back_to_rocm_smi(self):
        with patch.object(gpu_stats, "nvidia_smi_utilization", return_value=None), \
             patch.object(gpu_stats, "run_rocm_smi_json",
                          return_value={"card0": {"GPU use (%)": "7"}}):
            self.assertEqual(7.0, gpu_stats.sample_gpu_utilization())

    def test_sample_gpu_utilization_returns_none_when_both_backends_fail(self):
        with patch.object(gpu_stats, "nvidia_smi_utilization", return_value=None), \
             patch.object(gpu_stats, "run_rocm_smi_json", return_value=None):
            self.assertIsNone(gpu_stats.sample_gpu_utilization())


if __name__ == "__main__":
    unittest.main()


class RocmJsonOutputTests(unittest.TestCase):
    def test_actual_cpu_command_parses_complete_payload_after_brace_warning_or_before_trailing_warning(self):
        card = {"card0": {"GPU use (%)": "7", "VRAM Total Memory (B)": "16000000000"}}
        payload = json.dumps(card, indent=2)
        cases = (payload, "WARNING: permission notice\n" + payload,
                 "{warning: telemetry notice}\n" + payload,
                 "{not JSON\n" + payload + "\nWARNING: done",
                 "  " + payload + "\n{trailing warning}")
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "rocm-fixture"
            for output in cases:
                with self.subTest(output=output):
                    executable.write_text("#!" + sys.executable + "\nimport sys\n"
                        + "assert sys.argv[1:]==['--showuse','--json']\n"
                        + "sys.stdout.write(" + repr(output) + ")\n")
                    executable.chmod(0o755)
                    result = gpu_stats.run_rocm_smi_json(["--showuse"], str(executable), timeout=2)
                    self.assertEqual(card, result)
                    with patch.object(gpu_stats, "nvidia_smi_utilization", return_value=None):
                        self.assertEqual(7, gpu_stats.sample_gpu_utilization(str(executable), timeout=2))

    def test_malformed_nonzero_missing_and_timed_out_commands_remain_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "rocm-fixture"
            for output, code in (("{broken", 0), ("warning only", 0),
                                 ("", 0), ('{"card0":{"GPU use (%)":"7"}}', 1)):
                with self.subTest(output=output, code=code):
                    executable.write_text("#!" + sys.executable + "\nimport sys\n"
                        + "sys.stdout.write(" + repr(output) + ")\nsys.exit(" + str(code) + ")\n")
                    executable.chmod(0o755)
                    self.assertIsNone(gpu_stats.run_rocm_smi_json(["--showuse"], str(executable), timeout=2))
            self.assertIsNone(gpu_stats.run_rocm_smi_json([], str(Path(tmp) / "missing"), timeout=2))
            with patch.object(gpu_stats.subprocess, "run", side_effect=subprocess.TimeoutExpired("CPU fixture", .1)) as run:
                self.assertIsNone(gpu_stats.run_rocm_smi_json(["--showuse"], str(executable), timeout=.1))
            self.assertEqual(.1, run.call_args.kwargs["timeout"])
