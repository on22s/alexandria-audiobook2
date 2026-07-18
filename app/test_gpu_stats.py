import subprocess
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
