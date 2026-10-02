"""Native CPU queue owner, explicit worker samples, actual Bash status output."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_gpu_job import GPU_JOB, isolated_env

REPO = Path(__file__).resolve().parents[2]


class QueueEtaNativeTests(unittest.TestCase):
    def test_owned_cpu_job_reports_this_runs_progress_and_rejects_stale_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'run_chains/lib').mkdir(parents=True)
            shutil.copyfile(REPO / 'run_chains/lib/gpu_pending.sh', root / 'run_chains/lib/gpu_pending.sh')
            shutil.copyfile(REPO / 'run_chains/lib/gpu_queue_log.sh', root / 'run_chains/lib/gpu_queue_log.sh')
            wrapper = root / 'gpu_job.sh'
            wrapper.write_text('''#!/bin/bash
if [ "$1" = --check-lock-owner ]; then exec bash "$REAL_GPU_JOB" "$@"; fi
name="$1"; shift
exec 9>"$GPU_LOCK"
flock 9 || exit 4
export ALEXANDRIA_GPU_LOCK_HELD=1 ALEXANDRIA_GPU_LOCK_PID=$$ ALEXANDRIA_GPU_LOCK_FD=9
printf '%s START    %s\\n' "$(date -u +%FT%TZ)" "$name" > "$GPU_QLOG"
"$OWNER_PYTHON" "$OWNER_SCRIPT" "$$" "$0" "$name" "$@"
''')
            worker = root / 'worker.py'
            worker.write_text('''import pathlib,time,os
from gpu_progress import record_gpu_progress
record_gpu_progress('fixture rows',8,20)
time.sleep(.15)
record_gpu_progress('fixture rows',10,20)
pathlib.Path(os.environ['READY']).touch()
while not pathlib.Path(os.environ['RELEASE']).exists():time.sleep(.01)
''')
            for name in ('rocm-smi', 'nvidia-smi'):
                path = root / name
                path.write_text('#!/bin/sh\nexit 1\n')
                path.chmod(0o755)
            env = isolated_env(tmp, REAL_GPU_JOB=GPU_JOB, OWNER_PYTHON=sys.executable,
                               OWNER_SCRIPT=str(REPO / 'app/gpu_queue_owner.py'),
                               READY=str(root / 'ready'), RELEASE=str(root / 'release'),
                               PYTHONPATH=str(REPO / 'app'), PATH=tmp + os.pathsep + os.environ['PATH'])
            process = subprocess.Popen(['bash', str(wrapper), 'native ETA job', sys.executable, str(worker)],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            def status():
                script = REPO / 'gpu_pause.sh'
                if os.environ.get('QUEUE_ETA_BASELINE_SCRIPT'):
                    script = root / 'gpu_pause.sh'
                    shutil.copyfile(os.environ['QUEUE_ETA_BASELINE_SCRIPT'], script)
                result = subprocess.run(['bash', str(script), 'status'], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(0, result.returncode, result.stderr)
                return result.stdout
            try:
                deadline = time.monotonic() + 5
                while not (root / 'ready').exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root / 'ready').exists(), 'owned worker not admitted')
                self.assertIsNone(process.poll())
                path = root / 'progress' / f'{process.pid}.json'
                document = json.loads(path.read_text())
                self.assertEqual([8, 10], [sample['completed'] for sample in document['samples']])
                before, log_before = path.read_bytes(), Path(env['GPU_QLOG']).read_bytes()
                output = status()
                self.assertIn('running job: native ETA job', output)
                self.assertIn('ETA for fixture rows:', output)
                self.assertIn('10/20 units', output)
                self.assertIn('1 current-run intervals', output)
                self.assertRegex(output, r'estimated completion .* C[DS]T')
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(log_before, Path(env['GPU_QLOG']).read_bytes())
                document['owner_token'] = 'old-boot:wrong-birth'
                path.write_text(json.dumps(document))
                self.assertIn('ETA: unavailable (no unique current-owner progress report)', status())
                document['owner_token'] = json.loads(before)['owner_token']
                document['worker_token'] = 'old-producer-birth'
                path.write_text(json.dumps(document))
                self.assertIn('ETA: unavailable (no unique current-owner progress report)', status())
                document['worker_token'] = json.loads(before)['worker_token']
                document['samples'] = document['samples'][:1]
                path.write_text(json.dumps(document))
                self.assertIn('Not enough current-run unit measurements', status())
            finally:
                (root / 'release').touch()
                stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(0, process.returncode, stderr)
            with open(env['GPU_QLOG'], 'a') as stream:
                stream.write('2026-10-01T00:00:00Z OK       native ETA job\n')
            self.assertNotIn('ETA', status())
