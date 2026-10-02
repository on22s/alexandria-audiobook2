"""Disposable native Bash processes and command fixtures, no GPU work."""
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from tests.test_gpu_job import isolated_env, GPU_JOB

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(os.environ.get('GPU_PAUSE_BASELINE_FILE', ROOT/'gpu_pause.sh'))
LIB = ROOT/'run_chains/lib/gpu_pending.sh'


class PauseStatusBatchTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); wrapper = root/'gpu_job.sh'
            # Retain wrapper argv in the native shell until signalled.
            wrapper.write_text('#!/bin/bash\nprintf ready > "$READY"\nwhile :; do read -r -t 1 ignored || :; done\n')
            env = {**os.environ, 'READY':str(root/'ready'), 'GPU_QLOG':str(root/'q.log'),
                   'GPU_PAUSE_FLAG':str(root/'flag'), 'GPU_PENDING_DIR':str(root/'pending'),
                   'PATH':tmp+os.pathsep+os.environ['PATH']}
            for name, body in (('rocm-smi','exit 1'),('nvidia-smi','exit 1')):
                p=root/name;p.write_text('#!/bin/sh\n'+body+'\n');p.chmod(0o755)
            child=subprocess.Popen(['bash',str(wrapper),'long job'],env=env,start_new_session=True,
                                   stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+3
                while not (root/'ready').exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue((root/'ready').exists());self.assertIsNone(child.poll())
                (root/'q.log').write_text('2026-10-01T00:00:00Z START    long job\n')
                yield root, env, child
            finally:
                if child.poll() is None:os.killpg(child.pid,signal.SIGTERM)
                child.wait(timeout=3);child.stdin.close()

    def status(self,env):
        result=subprocess.run(['bash',str(SCRIPT),'status'],env=env,text=True,capture_output=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr);return result.stdout

    def test_start_survives_more_than_200_queued_records_but_terminal_still_clears(self):
        with self.fixture() as (root,env,child):
            with (root/'q.log').open('a') as out:
                out.writelines('2026-10-01T00:00:01Z QUEUED   waiting '+str(i)+'\n' for i in range(501))
            before=(root/'q.log').read_bytes()
            self.assertIn('running job: long job\n',self.status(env))
            self.assertEqual(before,(root/'q.log').read_bytes())
            with (root/'q.log').open('a') as out:out.write('2026-10-01T00:00:02Z OK       long job\n')
            self.assertIn('running job: none\n',self.status(env))

    def test_live_job_reports_explicit_unknown_eta_without_inventing_a_rate(self):
        with self.fixture() as (root, env, child):
            # A previous whole-job duration does not establish this run's unit rate.
            (root/'q.log').write_text(
                '2026-09-30T00:00:00Z START    long job\n'
                '2026-09-30T00:01:00Z OK       long job\n'
                '2026-10-01T00:00:00Z START    long job\n')
            before = (root/'q.log').read_bytes()
            output = self.status(env)
            self.assertIn('running job: long job\n', output)
            self.assertIn('ETA: unavailable (no unique current-owner progress report)\n', output)
            self.assertEqual(before, (root/'q.log').read_bytes())
            with (root/'q.log').open('a') as log:
                log.write('2026-10-01T00:00:02Z OK       long job\n')
            output = self.status(env)
            self.assertIn('running job: none\n', output)
            self.assertNotIn('ETA:', output)

    def test_nvidia_memory_query_uses_visible_selector_and_failure_does_not_report_number(self):
        with self.fixture() as (root,env,child):
            p=root/'nvidia-smi';p.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$QUERY_ARGS"\nprintf "4096\\n2048\\n"\n')
            env.update(CUDA_VISIBLE_DEVICES='GPU-fixture',QUERY_ARGS=str(root/'args'))
            self.assertIn('VRAM in use: 6144 MiB',self.status(env))
            self.assertEqual(['-i','GPU-fixture','--query-gpu=memory.used','--format=csv,noheader,nounits'],(root/'args').read_text().splitlines())
            p.write_text('#!/bin/sh\nprintf "999\\n"\nexit 1\n')
            self.assertIn('VRAM in use: unknown MiB',self.status(env))

    def test_pending_native_start_token_rejects_reused_pid_and_legacy_marker(self):
        with self.fixture() as (root,env,child):
            pending=root/'pending';pending.mkdir();marker=pending/'waiting'
            result=subprocess.run(['bash','-c','source "$1"; save_pending_marker "$2" waiting "$3" now','fixture',str(LIB),str(marker),str(child.pid)],capture_output=True,text=True)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertIn('waiting: waiting',self.status(env))
            fields=marker.read_text().rstrip().split('\t');self.assertEqual(4,len(fields))
            fields[3]='wrong-boot:1';marker.write_text('\t'.join(fields)+'\n')
            output=self.status(env);self.assertIn('STALE:',output);self.assertNotIn('waiting: waiting',output)
            marker.write_text('\t'.join(fields[:3])+'\n')
            output=self.status(env);self.assertIn('STALE:',output);self.assertNotIn('waiting: waiting',output)

    def test_idle_notification_counts_verified_markers_and_ignores_reused_pid(self):
        with self.fixture() as (root,env,child):
            pending=root/'pending';pending.mkdir();marker=pending/'waiting'
            result=subprocess.run(['bash','-c','source "$1"; save_pending_marker "$2" waiting "$3" now','fixture',str(LIB),str(marker),str(child.pid)],capture_output=True,text=True)
            self.assertEqual(0,result.returncode,result.stderr)
            notifier=root/'pterm';notifier.write_text('#!/bin/sh\nprintf notified >> "$NOTIFICATIONS"\n');notifier.chmod(0o755)
            source=Path(GPU_JOB).read_text();start=source.index('notify_if_idle() {');end=source.index('\n}',start)+2
            script=root/'notify.sh';script.write_text('source "$LIB"\n'+source[start:end]+'\nnotify_if_idle finished\n')
            env.update(LIB=str(LIB),PENDING_DIR=str(pending),PENDING_FILE=str(pending/'current'),
                       GPU_NOTIFY='1',GPU_NOTIFY_COOLDOWN='0',NAME='fixture',NOTIFICATIONS=str(root/'notifications'))
            first=subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True,timeout=3)
            self.assertEqual(0,first.returncode,first.stderr);self.assertFalse((root/'notifications').exists())
            fields=marker.read_text().rstrip().split('\t');fields[3]='wrong-boot:1';marker.write_text('\t'.join(fields)+'\n')
            second=subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True,timeout=3)
            self.assertEqual(0,second.returncode,second.stderr)
            self.assertEqual('notified',(root/'notifications').read_text())

    def test_actual_paused_wrapper_writes_its_kernel_identity_before_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=isolated_env(tmp,ALLOW_DIRTY_TREE='1',GPU_NOTIFY='0',REQUIRE_LLM='0')
            Path(env['GPU_PAUSE_FLAG']).write_text('held')
            child=subprocess.Popen(['bash',GPU_JOB,'marker fixture','true'],env=env,
                                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            try:
                deadline=time.monotonic()+5;fields=[]
                while time.monotonic()<deadline and child.poll() is None:
                    markers=list(Path(env['GPU_PENDING_DIR']).glob('*')) if Path(env['GPU_PENDING_DIR']).exists() else []
                    if markers:
                        fields=markers[0].read_text().rstrip().split('\t')
                        if len(fields)==4:break
                    time.sleep(.01)
                self.assertIsNone(child.poll());self.assertEqual(4,len(fields))
                self.assertEqual(['marker fixture',str(child.pid)],fields[:2])
                proof=subprocess.run(['bash','-c','source "$1"; get_pending_process_token "$2"','fixture',str(LIB),str(child.pid)],text=True,capture_output=True)
                self.assertEqual(0,proof.returncode,proof.stderr);self.assertEqual(proof.stdout.strip(),fields[3])
                self.assertNotIn('START',Path(env['GPU_QLOG']).read_text())
            finally:
                if child.poll() is None:os.killpg(child.pid,signal.SIGTERM)
                child.wait(timeout=5)
            self.assertEqual([],list(Path(env['GPU_PENDING_DIR']).iterdir()))
