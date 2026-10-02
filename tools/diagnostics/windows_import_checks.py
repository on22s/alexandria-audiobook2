"""Native Windows startup and cross-process kernel-lock regressions, CPU only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]


def main():
    assert os.name == 'nt', 'This diagnostic requires real Windows'
    result = {'platform': sys.platform, 'passed': False}
    try:
        with tempfile.TemporaryDirectory() as temporary:
            os.environ['ALEXANDRIA_DATA_DIR'] = temporary
            os.environ['PYTHONPATH'] = os.pathsep.join([str(ROOT / 'app'), str(ROOT)])
            # Model-dependent project construction is outside this startup check.
            # Application modules, file locks and subprocesses are not mocked.
            project = types.ModuleType('project')
            project.ProjectManager = lambda *args, **kwargs: None
            sys.modules['project'] = project
            imports = ('core', 'alexandria_run_manifest', 'alexandria_preparer_rocm_compatible',
                       'corpus_alignment_prescan', 'corpus_run_report')
            for name in imports:
                __import__(name)
            result['imports'] = list(imports)
            from experiments.gpu_guard import acquire_gpu_lock, release_gpu_lock
            from alexandria_run_manifest import acquire_run_lock, ensure_run_manifest, run_phase_with_lock
            root = Path(temporary)
            gpu = root / 'gpu.lock'
            lease = acquire_gpu_lock(gpu)
            probe = "from experiments.gpu_guard import gpu_is_busy; import sys; sys.exit(0 if gpu_is_busy(sys.argv[1]) else 1)"
            try:
                held = subprocess.run([sys.executable, '-c', probe, str(gpu)], capture_output=True, text=True)
                assert held.returncode == 0, held.stderr
            finally:
                release_gpu_lock(lease)
            free = subprocess.run([sys.executable, '-c', probe, str(gpu)], capture_output=True, text=True)
            assert free.returncode == 1, free.stderr
            work = root / 'dataset_temp'
            fd = acquire_run_lock(work)
            contender = "from alexandria_run_manifest import acquire_run_lock,RunStateError; import sys;\ntry: acquire_run_lock(sys.argv[1])\nexcept RunStateError: sys.exit(3)"
            try:
                refused = subprocess.run([sys.executable, '-c', contender, str(work)], cwd=ROOT, capture_output=True, text=True)
                assert refused.returncode == 3, refused.stderr
                identity = {'native': 'windows'}
                ensure_run_manifest(work, identity, fresh=True)
                ensure_run_manifest(work, identity, fresh=False)
                phase = "from alexandria_run_manifest import acquire_run_lock,ensure_run_manifest;import os,sys;fd=acquire_run_lock(sys.argv[1]);ensure_run_manifest(sys.argv[1],{'native':'windows'},fresh=False);os.close(fd)"
                completed = run_phase_with_lock([sys.executable, '-c', phase, str(work)], fd)
                assert completed.returncode == 0, completed.returncode
                still_held = subprocess.run([sys.executable, '-c', contender, str(work)], cwd=ROOT,
                                            capture_output=True, text=True)
                assert still_held.returncode == 3, still_held.stderr
            finally:
                os.close(fd)
            reacquired = acquire_run_lock(work)
            os.close(reacquired)
            result.update(passed=True, gpu_contention_verified=True, preparer_contention_verified=True,
                          manifest_write_and_resume_verified=True, phase_handle_inheritance_verified=True)
    finally:
        Path('windows_import_result.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
