"""The actual everything-chain cleanup waits for the shared GPU lease."""
import fcntl
from pathlib import Path
import time
import unittest

from tests.test_gpu_job import REPO
from tests.test_recheck_reporting import extract
from tests import test_stage_gpu_reclamation as reclamation


class EverythingReclamationTests(unittest.TestCase):
    setUp = reclamation.StageGpuReclamationTests.setUp
    launch = reclamation.StageGpuReclamationTests.launch
    wait_marker = reclamation.StageGpuReclamationTests.wait_marker

    def test_actual_cleanup_calls_wait_then_record_independent_results(self):
        self.program = '''set -uo pipefail
source "$1"
REPO="$2"
STAGE_LOG_DIR="$2/logs"
fixture_root="$2"
# Original-policy regression providers issue no host signals.
pgrep() { return 0; }
pkill() { touch "$fixture_root/reclaimed"; }
''' + extract(['reclaim_vram']) + '''
touch "$2/entered"
reclaim_vram reclaim_before_recheck
reclaim_vram reclaim_after_replay
stage_summary fixture
'''
        with open(self.env['GPU_LOCK'], 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = self.launch()
            try:
                self.wait_marker('entered')
                deadline = time.monotonic() + .25
                while time.monotonic() < deadline:
                    self.assertFalse((self.root / 'reclaimed').exists(), 'chain interrupted the current lease holder')
                    time.sleep(.01)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(0, process.returncode, stdout + stderr)
        self.assertTrue((self.root / 'reclaimed').exists())
        self.assertIn('2/2 stages ok', stdout)
        for name in ('reclaim_before_recheck', 'reclaim_after_replay'):
            self.assertIn('VRAM reclaimed', (self.root / f'logs/{name}.log').read_text())

    def test_failed_reclamation_is_counted_without_skipping_the_later_attempt(self):
        self.program = '''set -uo pipefail
source "$1"
REPO="$2"
STAGE_LOG_DIR="$2/logs"
''' + extract(['reclaim_vram']) + '''
export FIXTURE_USED=7516192768 STAGE_VRAM_WAIT=0
reclaim_vram reclaim_before_recheck
export FIXTURE_USED=0
reclaim_vram reclaim_after_replay
stage_summary fixture
'''
        process = self.launch()
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(1, process.returncode, stdout + stderr)
        self.assertIn('1/2 stages ok', stdout)
        self.assertIn('reclaim_before_recheck = failed:7', stdout)
        self.assertIn('OK    reclaim_after_replay', stdout)

    def test_chain_calls_two_named_reclamations_before_its_final_summary(self):
        source = Path(REPO, 'run_chains/everything_20260818.sh').read_text()
        self.assertIn('\nreclaim_vram reclaim_before_recheck\n', source)
        self.assertIn('\nreclaim_vram reclaim_after_replay\nstage_summary everything', source)
        self.assertNotIn('pkill', extract(['reclaim_vram']))
