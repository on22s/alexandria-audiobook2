"""Native Bash gates with known GPU-agent/provider fixtures, no GPU work."""
import os
from pathlib import Path
import shlex
import subprocess
import unittest

from tests import test_gpu_job as queue_tests


class RocmSelectionTests(unittest.TestCase):
    setUp = queue_tests.VramGateTest.setUp
    tearDown = queue_tests.VramGateTest.tearDown
    _log = queue_tests.VramGateTest._log

    def run_probe(self, *, selectors=None, cards=None, agents=("1", "2"), malformed=False):
        cards = cards or [(0, "1", 8192), (1, "2", 1024)]
        root = Path(self.tmp.name)
        provider = root / "bin"
        provider.mkdir(exist_ok=True)
        output = []
        for index, uid, free in cards:
            output += [f"GPU[{index}]: Unique ID: 0x{uid}",
                       f"GPU[{index}]: VRAM Total Memory (B): {16384 * 1048576}"]
            if not malformed:
                output.append(f"GPU[{index}]: VRAM Total Used Memory (B): {(16384-free) * 1048576}")
        agent_output = ["Agent 1", "  Uuid: CPU-XX", "  Device Type: CPU"]
        for index, uid in enumerate(agents, 2):
            agent_output += [f"Agent {index}", f"  Uuid: GPU-{uid.zfill(16)}", "  Device Type: GPU"]
        record = root / "rocminfo-selector"
        scripts = {
            "rocm-smi": "printf '%s\n' " + shlex.quote("\n".join(output)),
            "rocminfo": "printf '%s' \"${ROCR_VISIBLE_DEVICES-UNSET}\" > " + shlex.quote(str(record)) + "\nprintf '%s\n' " + shlex.quote("\n".join(agent_output)),
            "nvidia-smi": "echo 65536",
        }
        for name, body in scripts.items():
            path = provider / name
            path.write_text("#!/bin/sh\n" + body + "\n")
            path.chmod(0o755)
        env = queue_tests.isolated_env(self.tmp.name, ALLOW_DIRTY_TREE="1", REQUIRE_VRAM_GB="4", GPU_NOTIFY="0",
                                      PATH=str(provider) + os.pathsep + os.environ["PATH"])
        for key in ("ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES", "GPU_DEVICE_ORDINAL", "NVIDIA_VISIBLE_DEVICES"):
            env.pop(key, None)
        env.update(selectors or {})
        worker = root / "worker-ran"
        worker.unlink(missing_ok=True)
        result = subprocess.run(["bash", queue_tests.GPU_JOB, "probe", "touch", str(worker)], env=env,
                                capture_output=True, text=True, timeout=30)
        return result, worker, record

    def assert_busy(self, result, worker):
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertFalse(worker.exists())
        self.assertIn("NO_VRAM", self._log())

    def test_hip_selected_busy_card_refuses_despite_free_first_card(self):
        result, worker, _ = self.run_probe(selectors={"HIP_VISIBLE_DEVICES": "1"})
        self.assert_busy(result, worker)
        self.assertIn("1024MiB free", self._log())

    def test_rocr_numeric_and_uuid_filtered_agents_refuse_busy_card(self):
        for selector in ("1", "GPU-0000000000000002"):
            with self.subTest(selector=selector):
                result, worker, record = self.run_probe(selectors={"ROCR_VISIBLE_DEVICES": selector}, agents=("2",))
                self.assert_busy(result, worker)
                self.assertEqual(selector, record.read_text())

    def test_hip_ordinals_apply_after_rocr_reordering(self):
        for hip, expected in (("0", 7), ("1", 0)):
            with self.subTest(hip=hip):
                result, worker, _ = self.run_probe(selectors={"ROCR_VISIBLE_DEVICES": "1,0", "HIP_VISIBLE_DEVICES": hip}, agents=("2", "1"))
                self.assertEqual(expected, result.returncode, result.stderr)
                self.assertEqual(expected == 0, worker.exists())

    def test_free_selected_agent_is_not_refused_for_unrelated_busy_card(self):
        result, worker, _ = self.run_probe(selectors={"HIP_VISIBLE_DEVICES": "0"},
                                          cards=[(4, "2", 1024), (9, "1", 8192)])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(worker.exists())

    def test_unrestricted_and_multiple_visible_cards_use_least_free(self):
        for selectors in ({}, {"HIP_VISIBLE_DEVICES": "0,1"}):
            with self.subTest(selectors=selectors):
                result, worker, _ = self.run_probe(selectors=selectors)
                self.assert_busy(result, worker)

    def test_unmapped_selection_and_missing_memory_do_not_use_nvidia_card(self):
        for agents, malformed in ((("ff",), False), (("1", "2"), True)):
            with self.subTest(agents=agents, malformed=malformed):
                result, worker, _ = self.run_probe(selectors={"HIP_VISIBLE_DEVICES": "0"}, agents=agents, malformed=malformed)
                self.assert_busy(result, worker)

    def test_invalid_or_ambiguous_selected_cards_are_refused(self):
        for selector in ("-1", "9", "0,9", "bad"):
            with self.subTest(selector=selector):
                result, worker, _ = self.run_probe(selectors={"HIP_VISIBLE_DEVICES": selector})
                self.assert_busy(result, worker)
        result, worker, _ = self.run_probe(selectors={"HIP_VISIBLE_DEVICES": "0"},
                                          cards=[(0, "1", 1024), (1, "1", 8192)])
        self.assert_busy(result, worker)

    def test_cuda_alias_and_hip_precedence_match_hip_runtime(self):
        for selectors, expected in (({"CUDA_VISIBLE_DEVICES": "1"}, 7),
                                    ({"HIP_VISIBLE_DEVICES": "0", "CUDA_VISIBLE_DEVICES": "1"}, 0)):
            with self.subTest(selectors=selectors):
                result, worker, _ = self.run_probe(selectors=selectors)
                self.assertEqual(expected, result.returncode, result.stderr)
                self.assertEqual(expected == 0, worker.exists())
        # HIP uses HIP/CUDA ordinals, not the OpenCL-only ordinal path.
        result, worker, _ = self.run_probe(selectors={"GPU_DEVICE_ORDINAL": "1"},
                                          cards=[(0, "1", 1024), (1, "2", 8192)])
        self.assert_busy(result, worker)
