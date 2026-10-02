import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_gpu_job import isolated_env, copy_gpu_owner
from tests.test_gpu_lock_owner import run_owned_cpu_chain

ROOT = Path(__file__).resolve().parents[2]


class PreparerRestartLockTests(unittest.TestCase):
    def fixture(self, root, statuses='0'):
        shutil.copy2(ROOT / 'run_with_restart.sh', root / 'run_with_restart.sh')
        shutil.copy2(ROOT / 'gpu_job.sh', root / 'gpu_job.sh')
        (root / 'app/env/bin').mkdir(parents=True)
        copy_gpu_owner(str(root))
        (root / '.gitignore').write_text('/gpu.lock\n/queue.log\n/queue.log.*\n/paused\n/pending/\n/dirty_patches/\n')
        (root / 'alexandria_preparer_rocm_compatible.py').write_text('# CPU fixture only')
        child = root / 'app/env/bin/python'
        child.write_text('#!' + sys.executable + '\n' + r"""
import fcntl,json,os,subprocess,sys
from pathlib import Path
root=Path(os.environ['FIXTURE_ROOT'])
with open(os.environ['GPU_LOCK'],'a') as lock:
 try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);busy=False
 except BlockingIOError:busy=True
owner=os.environ.get('ALEXANDRIA_GPU_LOCK_PID','')
proof=subprocess.run(['bash',str(root/'gpu_job.sh'),'--check-lock-owner',owner],capture_output=True,text=True,timeout=3)
result=root/'attempts.json'
rows=json.loads(result.read_text()) if result.exists() else []
rows.append({'argv':sys.argv[1:],'busy':busy,'held':os.environ.get('ALEXANDRIA_GPU_LOCK_HELD'),'owner':owner,'proof':proof.returncode})
result.write_text(json.dumps(rows))
statuses=[int(n) for n in os.environ['CHILD_STATUSES'].split(',')]
raise SystemExit(statuses[min(len(rows)-1,len(statuses)-1)])
""")
        child.chmod(0o755)
        fake_bin = root / 'bin'
        fake_bin.mkdir()
        for name in ('rocm-smi', 'nvidia-smi'):
            target = fake_bin / name
            target.write_text('#!/bin/bash\nexit 0\n')
            target.chmod(0o755)
        env = isolated_env(str(root), GPU_NOTIFY='0', FIXTURE_ROOT=str(root),
                           CHILD_STATUSES=statuses, MAX_RETRIES='3', BACKOFF_SECONDS='0',
                           PATH=str(fake_bin) + os.pathsep + os.environ['PATH'])
        for key in ('ALEXANDRIA_GPU_LOCK_HELD', 'ALEXANDRIA_GPU_LOCK_PID'):
            env.pop(key, None)
        for args in (['git','init','-q',str(root)], ['git','-C',str(root),'add','.'],
                     ['git','-C',str(root),'-c','user.name=CPU fixture','-c','user.email=fixture@example.invalid','commit','-qm','CPU fixtures']):
            subprocess.run(args, check=True, capture_output=True, timeout=5)
        return env

    def assert_owned(self, root, count):
        rows = json.loads((root / 'attempts.json').read_text())
        self.assertEqual(count, len(rows))
        for row in rows:
            self.assertTrue(row['busy'], row)
            self.assertEqual('1', row['held'])
            self.assertEqual(0, row['proof'], row)
        self.assertEqual(1, len({row['owner'] for row in rows}))
        return rows

    def test_direct_entry_waits_for_real_lock_then_runs_under_verified_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.fixture(root)
            with open(env['GPU_LOCK'], 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                process = subprocess.Popen(['bash', str(root / 'run_with_restart.sh'), '--audio', 'a file.mp3'],
                                           env=env, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, start_new_session=True)
                try:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        qlog = root / 'queue.log'
                        if process.poll() is not None or (qlog.exists() and 'QUEUED' in qlog.read_text()):
                            break
                        time.sleep(.01)
                    self.assertIsNone(process.poll(), 'wrapper ran while another owner held lock')
                    self.assertFalse((root / 'attempts.json').exists())
                    self.assertIn('QUEUED', (root / 'queue.log').read_text())
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    stdout, stderr = process.communicate(timeout=8)
                    self.assertEqual(0, process.returncode, stdout + stderr)
                    rows = self.assert_owned(root, 1)
                    self.assertEqual(['--audio', 'a file.mp3'], rows[0]['argv'][1:])
                    self.assertIn('START', (root / 'queue.log').read_text())
                    self.assertIn('OK', (root / 'queue.log').read_text())
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    if process.poll() is None:
                        process.terminate()
                        try:process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.communicate(timeout=3)

    def test_nested_owner_preserves_retry_resume_and_exit_classification(self):
        cases = [('137,0', [], 0, 2), ('139,0', ['--resume'], 0, 2),
                 ('130,0', [], 0, 2), ('7', [], 7, 1), ('137', [], 1, 3)]
        for statuses, extra, expected, count in cases:
            with self.subTest(statuses=statuses, extra=extra), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = self.fixture(root, statuses)
                result = run_owned_cpu_chain(['bash', str(root / 'run_with_restart.sh'), '--audio', 'a file.mp3'] + extra,
                                             root, env=env, cwd=root, capture_output=True, text=True, timeout=8)
                self.assertEqual(expected, result.returncode, result.stdout + result.stderr)
                rows = self.assert_owned(root, count)
                self.assertEqual(extra.count('--resume'), rows[0]['argv'].count('--resume'))
                for row in rows[1:]:
                    self.assertEqual(1, row['argv'].count('--resume'))
                self.assertFalse((root / 'queue.log').exists(), 'nested wrapper reacquired lock')

    def test_forged_or_stale_inherited_claim_and_missing_wrapper_fail_before_child(self):
        for owner in ('', 'not-a-pid', '999999999', str(os.getpid())):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = self.fixture(root)
                env.update(ALEXANDRIA_GPU_LOCK_HELD='1', ALEXANDRIA_GPU_LOCK_PID=owner)
                result = subprocess.run(['bash', str(root / 'run_with_restart.sh')], env=env,
                                        cwd=root, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(0, result.returncode)
                self.assertIn('cannot verify inherited GPU lock', result.stderr)
                self.assertFalse((root / 'attempts.json').exists())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.fixture(root)
            (root / 'gpu_job.sh').unlink()
            result = subprocess.run(['bash', str(root / 'run_with_restart.sh')], env=env,
                                    cwd=root, capture_output=True, text=True, timeout=5)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn('GPU lock wrapper not found', result.stderr)
            self.assertFalse((root / 'attempts.json').exists())

    def test_corpus_still_dispatches_to_shared_restart_entry(self):
        source = (ROOT / 'build_test_corpus.sh').read_text()
        self.assertIn('run_with_restart.sh', source)
