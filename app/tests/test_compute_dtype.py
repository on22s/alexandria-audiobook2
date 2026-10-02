"""device_utils.compute_dtype: bf16 everywhere a GPU is, except AMD APU iGPUs.

bf16 HIP GEMMs SIGSEGV inside torch.Linear on gfx1035 (Radeon 660M/680M,
ROCm 6.4 wheels), so those load in fp32. The discrete-card path - including
the RX 9070 XT (gfx1201) this project runs on - must stay bf16, and the
decision is by gfx id, not by marketing name."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import device_utils


def _fake_torch(hip, arch):
    props = types.SimpleNamespace(gcnArchName=arch)
    cuda = types.SimpleNamespace(get_device_properties=lambda index: props)
    return types.SimpleNamespace(bfloat16="bf16", float32="f32",
                                 version=types.SimpleNamespace(hip=hip), cuda=cuda)


class ComputeDtypeTests(unittest.TestCase):
    def _dtype(self, device, hip=None, arch=""):
        with patch.dict(sys.modules, {"torch": _fake_torch(hip, arch)}):
            return device_utils.compute_dtype(device)

    def test_cpu_and_mps_are_fp32(self):
        self.assertEqual("f32", self._dtype("cpu"))
        self.assertEqual("f32", self._dtype("mps"))

    def test_nvidia_is_bf16(self):
        self.assertEqual("bf16", self._dtype("cuda", hip=None, arch="sm_86"))

    def test_discrete_rocm_cards_stay_bf16(self):
        for arch in ("gfx1201", "gfx1100", "gfx1030", "gfx90a", "gfx942",
                     "gfx1201:sramecc+:xnack-"):
            self.assertEqual("bf16", self._dtype("cuda:0", hip="6.4.0", arch=arch), arch)

    def test_amd_apus_load_in_fp32(self):
        for arch in ("gfx1035", "gfx1103", "gfx1035:xnack-"):
            self.assertEqual("f32", self._dtype("cuda", hip="6.4.0", arch=arch), arch)

    def test_unreadable_arch_on_rocm_uses_fp32(self):
        # Failure to identify the device cannot rule out an FP32-only APU.
        broken = _fake_torch("6.4.0", "")
        broken.cuda = types.SimpleNamespace(get_device_properties=lambda i: (_ for _ in ()).throw(RuntimeError()))
        with patch.dict(sys.modules, {"torch": broken}):
            self.assertEqual("f32", device_utils.compute_dtype("cuda"))

    def test_missing_or_empty_arch_on_rocm_uses_fp32(self):
        for arch in (None, "", "   ", "unknown", "None", "gfx"):
            with self.subTest(arch=arch):
                self.assertEqual("f32", self._dtype("cuda", hip="6.4.0", arch=arch))
        missing = _fake_torch("6.4.0", "")
        missing.cuda.get_device_properties = lambda device: types.SimpleNamespace()
        with patch.dict(sys.modules, {"torch": missing}):
            self.assertEqual("f32", device_utils.compute_dtype("cuda"))

    def test_rocm_uses_properties_of_the_requested_device(self):
        fake = _fake_torch("6.4.0", "gfx1201")
        observed = []
        def properties(device):
            observed.append(device)
            return types.SimpleNamespace(gcnArchName="gfx1103" if device == "cuda:1" else "gfx1201")
        fake.cuda.get_device_properties = properties
        with patch.dict(sys.modules, {"torch": fake}):
            self.assertEqual("f32", device_utils.compute_dtype("cuda:1"))
        self.assertEqual(["cuda:1"], observed)


if __name__ == "__main__":
    unittest.main()
