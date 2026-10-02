"""Actual driver admission and first generation under native CPU queue ownership."""
import fcntl
import os
from pathlib import Path
import shlex
import subprocess
import time
import unittest

from tests import test_stage_gpu_reclamation as reclamation

REPO=Path(__file__).resolve().parents[2]
SOURCE=Path(os.environ.get('REFERENCE_SPREAD_SOURCE',str(REPO/'run_chains/reference_spread_20260821.sh')))


class ReferenceSpreadReclamationTests(unittest.TestCase):
    setUp=reclamation.StageGpuReclamationTests.setUp
    wait_marker=reclamation.StageGpuReclamationTests.wait_marker

    def launch_generation(self,**environment):
        experiments=self.root/'app/experiments';experiments.mkdir(exist_ok=True)
        for name in ('reference_spread.py','reference_spread_compare.py'):(experiments/name).write_text('# CPU fixture prerequisite\n')
        (experiments/'ljspeech_generate.py').write_text('import os,pathlib\npathlib.Path(os.environ["FIXTURE_ROOT"],"dispatched").touch()\n')
        python=self.root/'app/env/bin/python';python.parent.mkdir(parents=True,exist_ok=True)
        if not python.exists():
            import sys
            python.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n');python.chmod(0o755)
        base=self.root/'ab_test_runtime/ljspeech_eval';base.mkdir(parents=True,exist_ok=True);(base/'build.json').write_text('{}');(base/'adapter').mkdir(exist_ok=True)
        source=SOURCE.read_text();prefix=source[:source.index('ARMS=4')]
        prefix=prefix.replace('REPO="$(cd "$(dirname "$0")/.." && pwd)"','REPO='+shlex.quote(str(self.root)))
        a=source.index('    REQUIRE_VRAM_GB=4 run_stage') if '    REQUIRE_VRAM_GB=4 run_stage' in source else source.index('    run_stage "spread_gen_')
        b=source.index('\n    run_stage "spread_score_',a) if '    REQUIRE_VRAM_GB=4 run_stage' not in source else source.index('\n    stage_commit_artifacts',a)
        generation=source[a:b]
        body='''set -uo pipefail
# Original failure probe never signals a host process or sleeps twenty seconds.
fixture_root="$1"
pkill() { touch "$fixture_root/reclaimed"; }
sleep() { :; }
'''+prefix+'''\ni=0
build="$work/build_spread0.json"
STAGE_RESULT[spread_build]=ok
'''+generation+'''\nstage_summary reference_fixture
'''
        return subprocess.Popen(['bash','-c',body,'fixture',str(self.root)],env=dict(self.env,**environment),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)

    def test_held_lease_blocks_cleanup_then_allows_generation(self):
        with open(self.env['GPU_LOCK'],'a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX);process=self.launch_generation()
            try:
                deadline=time.monotonic()+.3
                while time.monotonic()<deadline:
                    self.assertFalse((self.root/'reclaimed').exists(),'cleanup ran while another job owns GPU lease')
                    self.assertFalse((self.root/'dispatched').exists());time.sleep(.01)
            finally:
                fcntl.flock(lock,fcntl.LOCK_UN);stdout,stderr=process.communicate(timeout=10)
        self.assertEqual(0,process.returncode,stdout+stderr);self.assertTrue((self.root/'reclaimed').exists());self.assertTrue((self.root/'dispatched').exists())

    def test_cleanup_holds_lease_until_shutdown_finishes(self):
        process=self.launch_generation(FIXTURE_BLOCK='1')
        try:
            self.wait_marker('reclaimed')
            with open(self.env['GPU_LOCK'],'a') as lock:
                with self.assertRaises(BlockingIOError):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:
            (self.root/'release').touch();stdout,stderr=process.communicate(timeout=10)
        self.assertEqual(0,process.returncode,stdout+stderr)

    def test_original_four_gb_limit_still_rejects_generation_after_cleanup(self):
        process=self.launch_generation(FIXTURE_USED='7516192768',STAGE_VRAM_WAIT='0');stdout,stderr=process.communicate(timeout=10)
        self.assertEqual(1,process.returncode,stdout+stderr);self.assertTrue((self.root/'reclaimed').exists());self.assertFalse((self.root/'dispatched').exists())
        self.assertIn('spread_gen_0 = failed:7',stdout)
