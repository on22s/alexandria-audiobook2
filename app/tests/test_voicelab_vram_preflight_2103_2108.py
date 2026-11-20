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


def _write_torch_stub(folder, mem_path, name_path, device_count):
    """Write an importable torch stub that records real probe arguments.

    Both mem_get_info and get_device_name reject an out-of-range index the way
    torch does, so an unchecked index fails inside the subprocess.
    """
    (Path(folder) / 'torch.py').write_text(
        "import types\n"
        "COUNT=%d\n"
        "def _mem_get_info(*args):\n"
        "    if args and args[0]>=COUNT: raise IndexError('device index out of range')\n"
        "    with open(%r,'w') as handle: handle.write(repr(args))\n"
        "    return (12*1024**3,16*1024**3)\n"
        "def _get_device_name(*args):\n"
        "    if args and args[0]>=COUNT: raise IndexError('device index out of range')\n"
        "    with open(%r,'w') as handle: handle.write(repr(args))\n"
        "    return 'Synthetic GPU '+str(args[0]) if args else 'Synthetic GPU'\n"
        "__version__='synthetic'\n"
        "version=types.SimpleNamespace(hip=None)\n"
        "cuda=types.SimpleNamespace(is_available=lambda:True,\n"
        "                           device_count=lambda:COUNT,\n"
        "                           get_device_name=_get_device_name,\n"
        "                           mem_get_info=_mem_get_info)\n"
        "backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda:False))\n"
        % (device_count, str(mem_path), str(name_path)),
        encoding='utf-8')


def _run_probe(device, device_count=2):
    """Execute the real probe subprocess code against a torch stub.

    Returns (probe_report, mem_get_info args, get_device_name args). The stub
    records actual arguments, so these verify behaviour rather than a mocked
    return value or a code string.
    """
    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / 'mem.txt'
        name = Path(tmp) / 'name.txt'
        _write_torch_stub(tmp, mem, name, device_count)
        path = tmp + os.pathsep + os.environ.get('PYTHONPATH', '')
        with patch.dict(os.environ, {'PYTHONPATH': path}):
            report = voicelab._probe_voicelab_interpreter(sys.executable, device)
        return (report,
                mem.read_text(encoding='utf-8') if mem.exists() else None,
                name.read_text(encoding='utf-8') if name.exists() else None)


def _build_report(free_bytes, device='cuda', **probe_overrides):
    """Build the real preflight report with a controlled interpreter probe."""
    with tempfile.TemporaryDirectory() as tmp:
        probe = {
            'python': 'synthetic', 'torch': 'synthetic', 'hip': None,
            'gpu': 'Synthetic GPU', 'cuda_available': True, 'mps_available': False,
            'vram': [free_bytes, 16 * GB],
            'deps': {'librosa': True, 'peft': True, 'speechbrain': True,
                     'llama_cpp': True},
        }
        probe.update(probe_overrides)
        cfg = {'zips_dir': tmp, 'rocm_python': sys.executable,
               'profiler_model': '', 'epub_dirs': []}
        request = voicelab.VoiceLabRequest(stages=['train'], device='cuda')
        request = request.model_copy(update={'device': device})
        with patch.object(voicelab, 'DATA_DIR', tmp), \
             patch.object(voicelab, '_probe_voicelab_interpreter', return_value=probe), \
             patch.object(voicelab.shutil, 'disk_usage',
                          return_value=SimpleNamespace(free=30 * GB)):
            return voicelab._build_voicelab_preflight(request, cfg)


class VoiceLabVramPreflight2103And2108Tests(unittest.TestCase):
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

    def test_2108_cuda_device_index_reaches_mem_get_info_and_name(self):
        report, mem, name = _run_probe('cuda:1')
        self.assertEqual('synthetic', report.get('torch'), report)
        self.assertTrue(report.get('cuda_available'), report)
        self.assertEqual('(1,)', mem)
        self.assertEqual('(1,)', name)
        self.assertEqual('Synthetic GPU 1', report.get('gpu'))
        self.assertTrue(report.get('device_index_ok', True), report)

    def test_2108_cuda_and_auto_keep_current_device_default(self):
        for device in ('cuda', 'auto', 'rocm', 'hip'):
            with self.subTest(device=device):
                report, mem, name = _run_probe(device)
                self.assertEqual('synthetic', report.get('torch'), report)
                # Unindexed mem_get_info() == current device, exactly as before.
                self.assertEqual('()', mem)
                self.assertEqual('(0,)', name)
                self.assertEqual('Synthetic GPU 0', report.get('gpu'))

    def test_2108_out_of_range_index_reports_clear_blocker(self):
        report, mem, name = _run_probe('cuda:5')
        self.assertTrue(report, report)
        self.assertIs(report.get('device_index_ok'), False, report)
        self.assertIsNone(report.get('vram'), report)
        self.assertIsNone(report.get('gpu'), report)
        self.assertIsNone(mem)
        self.assertIsNone(name)
        built = _build_report(12 * GB, 'cuda:5',
                              device_index_ok=False, vram=None, gpu=None)
        codes = [item['code'] for item in built['blockers']]
        self.assertIn('gpu_unavailable', codes, built['blockers'])
        messages = [item['message'] for item in built['blockers']
                    if item['code'] == 'gpu_unavailable']
        self.assertIn('Selected CUDA/ROCm device index is unavailable', messages)
        valid_id = _build_report(12 * GB, 'cuda:1')['preflight_id']
        self.assertNotEqual(valid_id, built['preflight_id'])

    def test_2108_probe_and_identity_are_device_specific(self):
        report0, mem0, _ = _run_probe('cuda:0')
        report1, mem1, _ = _run_probe('cuda:1')
        self.assertEqual('(0,)', mem0)
        self.assertEqual('(1,)', mem1)
        self.assertEqual('synthetic', report0.get('torch'))
        self.assertEqual('synthetic', report1.get('torch'))
        # No probe cache exists; the preflight token is the only identity used
        # by /start, so a device change must change it too.
        id0 = _build_report(12 * GB, 'cuda:0')['preflight_id']
        id1 = _build_report(12 * GB, 'cuda:1')['preflight_id']
        self.assertNotEqual(id0, id1)


if __name__ == '__main__':
    unittest.main()
