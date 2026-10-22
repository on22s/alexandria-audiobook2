"""Backend eligibility agrees across resolution, HTTP status and its warning."""
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import device_utils
from routers import system


def get_torch_fixture(cuda=False, mps=False):
    return SimpleNamespace(cuda=SimpleNamespace(is_available=Mock(return_value=cuda)),
                           backends=SimpleNamespace(mps=SimpleNamespace(is_available=Mock(return_value=mps))))


class SystemGpuAvailabilityTests(unittest.TestCase):
    def get_status(self, torch, *, physical=True, memory=None, http=False):
        with ExitStack() as stack:
            stack.enter_context(patch.object(system, '_get_torch', return_value=torch))
            stack.enter_context(patch.object(system, 'get_gpu_stats', return_value=memory))
            stack.enter_context(patch.object(system, 'check_disk_space', return_value=(True, 100.0)))
            stack.enter_context(patch.object(system, 'get_runtime_info', return_value={}))
            hardware = stack.enter_context(patch.object(system, 'system_has_gpu',
                return_value=(physical, 'Synthetic GPU' if physical else None)))
            stack.enter_context(patch.object(system, 'run_rocm_smi_json',
                side_effect=AssertionError('No extra memory/name probe expected')))
            if http:
                app = FastAPI()
                app.include_router(system.router)
                with TestClient(app) as client:
                    response = client.get('/api/system/stats')
                self.assertEqual(200, response.status_code)
                result = response.json()
            else:
                result = system.get_system_stats()
            return result, hardware.call_count

    def test_available_mps_matches_auto_resolution_and_actual_http_status(self):
        torch = get_torch_fixture(mps=True)
        with patch.dict(sys.modules, {'torch': torch}):
            self.assertEqual('mps', device_utils.resolve_device('auto'))
        report, physical_queries = self.get_status(torch, http=True)
        self.assertFalse(report['gpu_mismatch'])
        self.assertIsNone(report['gpu_mismatch_vendor'])
        self.assertIsNone(report['gpu'])
        self.assertEqual(0, physical_queries)

    def test_cuda_and_rocm_keep_priority_and_original_memory_statistics(self):
        memory = {'name': 'Synthetic CUDA device', 'total_gb': 16, 'reserved_gb': 2,
                  'allocated_percent': 10}
        for hip in (None, 'synthetic-HIP'):
            with self.subTest(hip=hip):
                torch = get_torch_fixture(cuda=True, mps=True)
                torch.version = SimpleNamespace(hip=hip)
                torch.backends.mps.is_available.side_effect = AssertionError('CUDA must win')
                with patch.dict(sys.modules, {'torch': torch}):
                    self.assertEqual('cuda', device_utils.resolve_device('auto'))
                report, physical_queries = self.get_status(torch, memory=memory)
                self.assertFalse(report['gpu_mismatch'])
                self.assertEqual(memory, report['gpu'])
                self.assertEqual(0, physical_queries)

    def test_missing_torch_unavailable_backends_and_no_gpu_controls(self):
        absent_mps = get_torch_fixture()
        absent_mps.backends = SimpleNamespace()
        for torch in (None, get_torch_fixture(), absent_mps):
            for physical in (False, True):
                with self.subTest(torch=torch, physical=physical):
                    report, queries = self.get_status(torch, physical=physical)
                    self.assertEqual(physical, report['gpu_mismatch'])
                    self.assertEqual('Synthetic GPU' if physical else None, report['gpu_mismatch_vendor'])
                    self.assertEqual(1, queries)

    def test_status_probe_errors_preserve_the_real_unavailable_gpu_warning(self):
        for failure in (RuntimeError, OSError, AttributeError):
            with self.subTest(failure=failure):
                torch = get_torch_fixture()
                torch.cuda.is_available.side_effect = failure('synthetic probe failure')
                report, queries = self.get_status(torch)
                self.assertTrue(report['gpu_mismatch'])
                self.assertEqual(1, queries)

    def test_mps_and_explicit_cpu_memory_remain_unknown_without_a_probe(self):
        for device in ('mps', 'cpu'):
            with self.subTest(device=device), \
                 patch.object(system, 'load_app_config', return_value={'tts': {'device': device}}), \
                 patch.object(system, '_get_torch', side_effect=AssertionError('No CUDA memory on MPS/CPU')):
                self.assertIsNone(system.get_gpu_stats())

    def test_explicit_device_selection_still_needs_no_torch_import(self):
        with patch.dict(sys.modules, {'torch': None}):
            for requested, expected in [('cpu', 'cpu'), ('mps', 'mps'), ('cuda:2', 'cuda:2'),
                                        ('hip', 'cuda'), ('rocm', 'cuda')]:
                with self.subTest(requested=requested):
                    self.assertEqual(expected, device_utils.resolve_device(requested))

    def test_auto_resolution_preserves_probe_errors_and_missing_torch_warning(self):
        torch = get_torch_fixture()
        torch.cuda.is_available.side_effect = RuntimeError('synthetic probe failure')
        with patch.dict(sys.modules, {'torch': torch}):
            with self.assertRaisesRegex(RuntimeError, 'synthetic probe failure'):
                device_utils.resolve_device('auto')
        with patch.dict(sys.modules, {'torch': None}), \
             patch.object(device_utils, 'system_has_gpu', return_value=(True, 'Synthetic GPU')), \
             patch('builtins.print') as warning:
            self.assertEqual('cpu', device_utils.resolve_device('auto'))
        self.assertIn('falling back to CPU', warning.call_args.args[0])

    def test_workbench_uses_actual_status_to_clear_false_mps_warning(self):
        unavailable, _ = self.get_status(get_torch_fixture())
        mps, _ = self.get_status(get_torch_fixture(mps=True))
        source = Path(__file__).resolve().parent.parent / 'static/js/app-workbench.js'
        script = r'''const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8'),reports=JSON.parse(process.argv[2]);
const elements={};const el=id=>elements[id]||(elements[id]={style:{},textContent:'',title:'',classList:{values:new Set(),add(v){this.values.add(v);},remove(v){this.values.delete(v);},contains(v){return this.values.has(v);}}});
let report;const c={document:{getElementById:el},checkStaleBuild(){},console,API:{get:async()=>report}};
vm.createContext(c);const start=source.indexOf('function applySystemStatusText(');
vm.runInContext(source.slice(start,source.indexOf('// Format a duration',start)),c);
let finished=false;process.on('beforeExit',()=>assert(finished));
(async()=>{report=reports[0];await c.updateSystemStats();assert.strictEqual(el('sys-gpu-val').textContent,'CPU fallback!');assert(el('sys-gpu').title.includes('re-run install.js'));assert(el('sys-gpu-warning').textContent.includes('using CPU'));assert(el('sys-gpu').classList.contains('text-danger'));
report=reports[1];await c.updateSystemStats();assert.strictEqual(el('sys-gpu-val').textContent,'Unavailable');assert.strictEqual(el('sys-gpu').title,'');assert.strictEqual(el('sys-gpu-warning').textContent,'');assert(!el('sys-gpu').classList.contains('text-danger'));finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});'''
        result = subprocess.run(['node', '-e', script, str(source), json.dumps([unavailable, mps])],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_device_helper_import_remains_independent_of_optional_torch(self):
        script = """import importlib.abc,sys
class RefuseTorch(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name=='torch' or name.startswith('torch.'):
   raise AssertionError('Device helper imported Torch')
sys.meta_path.insert(0,RefuseTorch())
import device_utils
assert 'torch' not in sys.modules
assert device_utils.get_available_gpu_device(None) is None
"""
        result = subprocess.run([sys.executable, '-c', script],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
