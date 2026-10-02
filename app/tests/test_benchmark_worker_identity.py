"""Execute actual probe commands against disposable checkouts, without SSH or ML."""
import importlib.util
import inspect
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import benchmark_environment as env

if os.environ.get('BENCHMARK_IDENTITY_SOURCE'):
    spec = importlib.util.spec_from_file_location('saved_identity', os.environ['BENCHMARK_IDENTITY_SOURCE'])
    env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(env)

REPO = Path(__file__).resolve().parents[2]


class BenchmarkWorkerIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)/'orchestrator'
        (self.root/'app').mkdir(parents=True)
        for name in ('benchmark_environment_identity.py','runtime_info.py'):
            shutil.copyfile(REPO/'app'/name,self.root/'app'/name)
        def git(*args):
            subprocess.run(['git',*args],check=True,capture_output=True,text=True)
        (self.root/'.gitignore').write_text('__pycache__/\n*.pyc\n')
        git('init',str(self.root))
        git('-C',str(self.root),'add','app','.gitignore')
        git('-C',str(self.root),'-c','user.name=fixture','-c','user.email=fixture@example.test','commit','-m','fixture')
        self.remote = Path(temp.name)/'worker checkout'
        git('clone',str(self.root),str(self.remote))
        self.python = Path(temp.name)/'selected python'
        self.python.write_text('#!'+sys.executable+'\nimport sys,platform\nfrom importlib import metadata\n'
            'platform.python_version=lambda:"3.99.worker"\n'
            'metadata.version=lambda name:"worker-"+name\nexec(sys.argv[2])\n')
        self.python.chmod(0o755)
        self.commands = []
        def execute(_alias,command,**kwargs):
            self.commands.append(command)
            result = subprocess.run(['bash','-c',command],capture_output=True,text=True,timeout=10)
            return SimpleNamespace(returncode=result.returncode,stdout='decorative banner\n'+result.stdout,stderr=result.stderr)
        self.patchers = [patch.object(env,'_ssh_run',side_effect=execute),
            patch.object(env,'get_remote_gpu_name_and_backend',return_value=('fixture-gpu','fixture-backend')),
            patch.object(env,'get_remote_lmstudio_status',return_value={'available':True,'loaded':True})]
        for patcher in self.patchers:
            patcher.start();self.addCleanup(patcher.stop)

    def collect(self,target):
        fn = env.collect_cpu_environment if target=='cpu' else env.collect_thunder_environment
        kwargs = {'remote_root':str(self.remote),'remote_python':str(self.python)}
        kwargs = {k:v for k,v in kwargs.items() if k in inspect.signature(fn).parameters}
        return fn(str(self.root),'thunder' if target=='cpu' else 'fixture-ssh',
                  'fixture-ssh' if target=='cpu' else 'model',**kwargs)

    def test_llm_fingerprint_uses_selected_worker_python_packages_and_actual_checkout(self):
        details = self.collect('llm')['details']
        self.assertEqual('3.99.worker',details['python_version'])
        self.assertEqual('worker-torch',details['packages']['torch'])
        self.assertEqual(str(self.python),details['python_executable'])
        revision = subprocess.run(['git','-C',str(self.remote),'rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip()
        self.assertEqual(revision,details['git_commit'])
        self.assertFalse(details['worktree']['dirty'])
        self.assertNotEqual(details['packages'],details['orchestrator_packages'])
        python_command = next(c for c in self.commands if ' -c ' in c)
        self.assertEqual(str(self.python),shlex.split(python_command)[0])

    def test_cpu_fingerprint_separates_dirty_orchestrator_from_clean_worker(self):
        (self.root/'app/local_change.py').write_text('local-only fixture\n')
        details = self.collect('cpu')['details']
        self.assertEqual('3.99.worker',details['python_version'])
        self.assertFalse(details['worktree']['dirty'])
        self.assertTrue(details['orchestrator_worktree']['dirty'])
        self.assertNotEqual(details['worktree']['sha256'],details['orchestrator_worktree']['sha256'])
        self.assertEqual(str(self.remote),details['remote_root'])

    def test_dirty_and_different_worker_checkouts_are_refused(self):
        (self.remote/'app/dirty.py').write_text('dirty fixture\n')
        for target in ('cpu','llm'):
            with self.subTest(target=target),self.assertRaisesRegex(ValueError,'clean'):
                self.collect(target)
        (self.remote/'app/dirty.py').unlink()
        subprocess.run(['git','-C',str(self.remote),'-c','user.name=fixture','-c','user.email=fixture@example.test',
            'commit','--allow-empty','-m','different'],capture_output=True,check=True)
        for target in ('cpu','llm'):
            with self.subTest(target=target),self.assertRaisesRegex(ValueError,'match'):
                self.collect(target)

    def test_runtime_without_checkout_observes_packages_from_selected_python(self):
        fn = env._get_remote_runtime_observations
        kwargs = {'remote_python':str(self.python)} if 'remote_python' in inspect.signature(fn).parameters else {}
        details = fn('fixture-ssh',**kwargs)
        self.assertEqual('3.99.worker',details['python_version'])
        self.assertEqual('worker-fastapi',details['packages']['fastapi'])
