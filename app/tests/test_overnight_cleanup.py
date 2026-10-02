"""Actual historical chain cleanup and admission with CPU-only providers."""
import fcntl
import os
from pathlib import Path
import shlex
import subprocess
import time
import unittest

from tests import test_stage_gpu_reclamation as reclamation

REPO=Path(__file__).resolve().parents[2]
SOURCE=Path(os.environ.get('OVERNIGHT_CHAIN_SOURCE',str(REPO/'run_chains/overnight_20260819.sh')))


class OvernightCleanupTests(unittest.TestCase):
    setUp=reclamation.StageGpuReclamationTests.setUp
    wait_marker=reclamation.StageGpuReclamationTests.wait_marker

    def launch_cleanup(self,**environment):
        source=SOURCE.read_text();a=source.index('reclaim_vram() {');b=source.index('\n}',a)+2
        body='''set -uo pipefail
source "$1"
REPO="$2"
STAGE_LOG_DIR="$2/logs"
fixture_root="$2"
# Never signal host processes in an original-code failure probe.
pgrep() { return 0; }
pkill() { touch "$fixture_root/reclaimed"; }
sleep() { :; }
'''+source[a:b]+'''\nreclaim_vram work
stage_summary overnight_fixture
'''
        return subprocess.Popen(['bash','-c',body,'fixture',str(self.stage),str(self.root)],env=dict(self.env,**environment),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)

    def test_cleanup_waits_for_another_jobs_real_kernel_lease(self):
        with open(self.env['GPU_LOCK'],'a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            process=self.launch_cleanup()
            try:
                deadline=time.monotonic()+.3
                while time.monotonic()<deadline:
                    self.assertFalse((self.root/'reclaimed').exists(),'server stopped while another job owns GPU lease')
                    time.sleep(.01)
            finally:
                fcntl.flock(lock,fcntl.LOCK_UN)
                stdout,stderr=process.communicate(timeout=10)
        self.assertEqual(0,process.returncode,stdout+stderr)
        self.assertTrue((self.root/'reclaimed').exists())
        self.assertIn('VRAM reclaimed',(self.root/'logs/work.log').read_text())

    def test_cleanup_keeps_lease_until_server_shutdown_finishes(self):
        process=self.launch_cleanup(FIXTURE_BLOCK='1')
        try:
            self.wait_marker('reclaimed')
            with open(self.env['GPU_LOCK'],'a') as lock:
                with self.assertRaises(BlockingIOError):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:
            (self.root/'release').touch();stdout,stderr=process.communicate(timeout=10)
        self.assertEqual(0,process.returncode,stdout+stderr)

    def test_existing_vram_capacity_failure_is_reported_by_final_summary(self):
        process=self.launch_cleanup(FIXTURE_USED='7516192768',STAGE_VRAM_WAIT='0')
        stdout,stderr=process.communicate(timeout=10)
        self.assertEqual(1,process.returncode,stdout+stderr)
        self.assertTrue((self.root/'reclaimed').exists())
        self.assertIn('work = failed:7',stdout)
        self.assertIn('NO_VRAM',Path(self.env['GPU_QLOG']).read_text())

    def test_expired_deadline_refuses_before_any_server_worker_or_commit(self):
        # Execute actual admission prefix up to first stage, with a deterministic
        # epoch. A marker immediately after the prefix proves admission.
        source=SOURCE.read_text();source=source[:source.index('# ---- 1.')]
        source=source.replace('REPO="$(cd "$(dirname "$0")/.." && pwd)"','REPO='+shlex.quote(str(self.root)))
        traps='''date() { if [ "${1:-}" = -d ]; then echo 100; elif [ "${1:-}" = +%s ]; then echo 200; else command date "$@"; fi; }
'''
        result=subprocess.run(['bash','-c',traps+source+'\ntouch "$FIXTURE_ROOT/admitted"\n'],env=self.env,capture_output=True,text=True,timeout=5)
        self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertIn('deadline has expired',result.stdout)
        self.assertFalse((self.root/'admitted').exists())
        self.assertFalse((self.root/'reclaimed').exists())

    def test_unexpired_deadline_admits_and_exact_deadline_refuses(self):
        source=SOURCE.read_text();source=source[:source.index('# ---- 1.')]
        source=source.replace('REPO="$(cd "$(dirname "$0")/.." && pwd)"','REPO='+shlex.quote(str(self.root)))
        for now,expected in ((99,0),(100,1)):
            with self.subTest(now=now):
                marker=self.root/'admitted'
                if marker.exists():marker.unlink()
                clock='date() { if [ "${1:-}" = -d ]; then echo 100; elif [ "${1:-}" = +%s ]; then echo '+str(now)+'; else command date "$@"; fi; }\n'
                result=subprocess.run(['bash','-c',clock+source+'\ntouch "$FIXTURE_ROOT/admitted"\n'],env=self.env,capture_output=True,text=True,timeout=5)
                self.assertEqual(expected,result.returncode,result.stdout+result.stderr)
                self.assertEqual(expected==0,marker.exists())
