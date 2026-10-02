import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parent.parent.parent
LIB = REPO/'run_chains/lib/queue.sh'
GATE = REPO/'gpu_job.sh'


class QueueSourcePreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='queue source fixture '); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init','-q'); self.git('config','user.name','Fixture'); self.git('config','user.email','fixture@example.test')
        self.git('config','core.hooksPath',str(self.root/'no-hooks'))
        for name in ('app/source.py','app/config.json','RESULTS_INDEX.md','results_index.csv','ab_test_runtime/experiments/out.json'):
            path = self.root/name; path.parent.mkdir(parents=True,exist_ok=True);path.write_text('baseline')
        self.git('add','.');self.git('commit','-qm','baseline')

    def git(self,*args):
        return subprocess.run(['git','-C',str(self.root),*args],capture_output=True,text=True,check=True)

    def compare(self, clean):
        status = subprocess.run(['bash',str(GATE),'--print-source-state',str(self.root)],capture_output=True,timeout=5)
        self.assertEqual(0,status.returncode,status.stderr)
        state = status.stdout.split(b'\0')[0].decode()
        result = subprocess.run(['bash','-c','set -euo pipefail; source "$1"; refuse_if_dirty "$2"',
            'fixture',str(LIB),str(self.root)],capture_output=True,text=True,timeout=5)
        self.assertEqual(clean,state=='clean',(state,result.stdout,result.stderr))
        self.assertEqual(clean,result.returncode==0,(state,result.stdout,result.stderr))
        self.assertFalse((self.root/'ab_test_runtime/logs/alexandria_gpu.lock').exists())
        self.assertFalse((self.root/'ab_test_runtime/logs/gpu_jobq.log').exists())
        return result

    def test_untracked_source_including_odd_names_is_rejected_by_both_gates(self):
        self.compare(True)
        for name in ('README.md','input.json','worker','source with spaces.py','source\nnewline.py'):
            with self.subTest(name=name):
                path=self.root/name;path.write_text('changed source')
                result=self.compare(False)
                self.assertIn('REFUSING',result.stdout)
                self.assertEqual('changed source',path.read_text())
                path.unlink()

    def test_generated_outputs_are_excluded_but_tracked_config_and_removals_are_not(self):
        (self.root/'RESULTS_INDEX.md').write_text('regenerated')
        (self.root/'results_index.csv').write_text('regenerated')
        (self.root/'ab_test_runtime/experiments/out.json').write_text('regenerated')
        (self.root/'ab_test_runtime/new-output.json').write_text('runtime output')
        self.compare(True)
        config=self.root/'app/config.json';config.write_text('modified config');self.compare(False)
        self.assertEqual('modified config',config.read_text())
        config.write_text('baseline');(self.root/'app/source.py').unlink();self.compare(False)

    def test_missing_canonical_policy_refuses_before_inspecting_sources(self):
        helper=self.root/'run_chains/lib/queue.sh';helper.parent.mkdir(parents=True);shutil.copyfile(LIB,helper)
        result=subprocess.run(['bash','-c','source "$1"; refuse_if_dirty "$2"',
            'fixture',str(helper),str(self.root)],capture_output=True,text=True,timeout=5)
        self.assertNotEqual(0,result.returncode)
        self.assertIn('could not inspect',result.stderr)
