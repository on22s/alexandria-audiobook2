"""Execute the whole historical chain with native CPU-only worker stand-ins."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO=Path(__file__).resolve().parents[2]
CHAIN_PATH=REPO/'run_chains/overnight_2026_08_08.sh'


class OvernightStageFailuresTests(unittest.TestCase):
    def run_chain(self, codes):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'run_chains/lib').mkdir(parents=True)
            (root/'app').mkdir()
            for name in ('stage.sh','server_cleanup.sh'):
                shutil.copyfile(REPO/'run_chains/lib'/name,root/'run_chains/lib'/name)
            chain=root/'run_chains'/CHAIN_PATH.name
            # Relocate only the original historical hardcoded checkout in the baseline.
            source=CHAIN_PATH.read_text().replace('REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git',
                                                 'REPO='+str(root))
            chain.write_text(source)
            worker=root/'gpu_job.sh'
            worker.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$FIXTURE_CALLS"
case "$1" in
 library_fidelity_post_fix) exit "$CODE_LIBRARY";;
 drift_2000_husky_tenor) exit "$CODE_HUSKY";;
 drift_2000_warm_mezzo) exit "$CODE_MEZZO";;
 drift_2000_warm_baritone) exit "$CODE_BARITONE";;
 *) exit 99;;
esac
''')
            worker.chmod(0o755)
            calls=root/'calls'
            env=dict(os.environ,FIXTURE_CALLS=str(calls))
            for name,code in zip(('LIBRARY','HUSKY','MEZZO','BARITONE'),codes):env['CODE_'+name]=str(code)
            result=subprocess.run(['bash',str(chain)],env=env,cwd=root,capture_output=True,text=True,timeout=10)
            attempts=calls.read_text().splitlines()
            self.assertEqual(4,len(attempts),result.stdout+result.stderr)
            self.assertIn('timeout 21600',attempts[0])
            for attempt in attempts[1:]:self.assertIn('timeout 36000',attempt)
            self.assertEqual(codes==[0,0,0,0],result.returncode==0,result.stdout+result.stderr)
            if any(codes):self.assertIn('failed',result.stdout)
            for name in ('library_fidelity_post_fix','drift_2000_husky_tenor',
                         'drift_2000_warm_mezzo','drift_2000_warm_baritone'):
                self.assertTrue((root/'ab_test_runtime/logs'/f'{name}.log').exists())
            return result

    def test_all_workers_failing_cannot_report_success(self):self.run_chain([2,2,2,2])
    def test_one_failure_still_attempts_later_independent_workers(self):self.run_chain([0,7,0,0])
    def test_worker_timeout_status_is_not_lost(self):self.run_chain([0,0,124,0])
    def test_every_worker_succeeding_returns_success(self):self.run_chain([0,0,0,0])
