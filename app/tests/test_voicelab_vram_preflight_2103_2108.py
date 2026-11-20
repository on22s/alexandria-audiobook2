"""Fail-first coverage for #2103 (VRAM floor) and #2108 (device-indexed probe)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from routers import voicelab


GB = 1024 ** 3


def _write_torch_stub(folder, observed_path):
    """Write an importable torch stub that records mem_get_info's real argument."""
    (Path(folder) / 'torch.py').write_text(
        "import types\n"
        "def _mem_get_info(*args):\n"
        "    with open(%r, 'w') as handle:\n"
        "        handle.write(repr(args))\n"
        "    return (12 * 1024 ** 3, 16 * 1024 ** 3)\n"
        "__version__ = 'synthetic'\n"
        "version = types.SimpleNamespace(hip=None)\n"
        "cuda = types.SimpleNamespace(is_available=lambda: True,\n"
        "                             device_count=lambda: 2,\n"
        "                             get_device_name=lambda n: 'Synthetic GPU',\n"
        "                             mem_get_info=_mem_get_info)\n"
        "backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))\n"
        % str(observed_path),
        encoding='utf-8')


def _run_probe(device):
    """Execute the real probe subprocess code against a torch stub.

    Returns (probe_report, repr of the tuple passed to mem_get_info). The stub
    records the actual argument, so this verifies behaviour, not just a code
    string or a mocked return value.
    """
    with tempfile.TemporaryDirectory() as tmp:
        observed = Path(tmp) / 'observed.txt'
        _write_torch_stub(tmp, observed)
        path = tmp + os.pathsep + os.environ.get('PYTHONPATH', '')
        with patch.dict(os.environ, {'PYTHONPATH': path}):
            report = voicelab._probe_voicelab_interpreter(sys.executable, device)
        return report, observed.read_text(encoding='utf-8') if observed.exists() else None


def _build_report(free_bytes, device='cuda'):
    """Build the real preflight report with a controlled interpreter probe."""
    with tempfile.TemporaryDirectory() as tmp:
        probe = {
            'python': 'synthetic', 'torch': 'synthetic', 'hip': None,
            'gpu': 'Synthetic GPU', 'cuda_available': True, 'mps_available': False,
            'vram': [free_bytes, 16 * GB],
            'deps': {'librosa': True, 'peft': True, 'speechbrain': True,
                     'llama_cpp': True},
        }
        cfg = {'zips_dir': tmp, 'rocm_python': sys.executable,
               'profiler_model': '', 'epub_dirs': []}
        request = voicelab.VoiceLabRequest(stages=['train'], device='cuda')
        request = request.model_copy(update={'device': device})
        with patch.object(voicelab, 'DATA_DIR', tmp), \
             patch.object(voicelab, '_probe_voicelab_interpreter', return_value=probe), \
             patch.object(voicelab.shutil, 'disk_usage',
                          return_value=SimpleNamespace(free=30 * GB)):
            return voicelab._build_voicelab_preflight(request, cfg)


class VoiceLabVramPreflight21031008Tests(unittest.TestCase):
    def test_2103_vram_floor_uses_unrounded_bytes(self):
        # 7.96 GiB rounds to 8.0 at one decimal; the floor must still reject it.
        for free, expect_low in ((int(7.96 * GB), True),
                                 (8 * GB, False),
                                 (int(7.94 * GB), True)):
            with self.subTest(free_gb=free / GB):
                codes = [item['code'] for item in _build_report(free)['blockers']]
                self.assertEqual(expect_low, 'vram_low' in codes, (free, codes))

    def test_2103_displayed_free_vram_gb_is_still_rounded(self):
        for free in (int(7.96 * GB), 8 * GB, int(7.94 * GB)):
            with self.subTest(free_gb=free / GB):
                shown = _build_report(free)['runtime']['free_vram_gb']
                self.assertEqual(round(free / GB, 1), shown)
                self.assertIsInstance(shown, float)

    def test_2108_cuda_device_index_reaches_mem_get_info(self):
        report, observed = _run_probe('cuda:1')
        self.assertEqual('synthetic', report.get('torch'), report)
        self.assertTrue(report.get('cuda_available'), report)
        self.assertEqual('(1,)', observed)

    def test_2108_cuda_and_auto_keep_current_device_default(self):
        for device in ('cuda', 'auto', 'rocm', 'hip'):
            with self.subTest(device=device):
                report, observed = _run_probe(device)
                self.assertEqual('synthetic', report.get('torch'), report)
                # Unindexed mem_get_info() == current device, exactly as before.
                self.assertEqual('()', observed)

    def test_2108_probe_and_identity_are_device_specific(self):
        report0, observed0 = _run_probe('cuda:0')
        report1, observed1 = _run_probe('cuda:1')
        self.assertEqual('(0,)', observed0)
        self.assertEqual('(1,)', observed1)
        self.assertEqual('synthetic', report0.get('torch'))
        self.assertEqual('synthetic', report1.get('torch'))
        # No probe cache exists; the preflight token is the only identity used
        # by /start, so a device change must change it too.
        id0 = _build_report(12 * GB, 'cuda:0')['preflight_id']
        id1 = _build_report(12 * GB, 'cuda:1')['preflight_id']
        self.assertNotEqual(id0, id1)


if __name__ == '__main__':
    unittest.main()
